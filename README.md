# Phone Relay

Lets Claude, running in Claude Code cloud sessions, view and control an Android phone (Gmail and Chrome only) through a Cloudflare Worker relay. There is no VPS and no inbound port on the phone.

```
Claude (cloud session) --HTTPS, agent token added by the network secret-->  Cloudflare Worker
                                                                              |
                                                                    Durable Object "primary"
                                                                              ^
Phone (Termux: phone_client.py + adb over loopback) --outbound WSS, device token--+
```

The design and its reasoning are in [docs/PLAN.md](docs/PLAN.md). This README covers setup and use. Phases 0-4 of the plan are built. Phases 5-6 (native app, MCP, custom domain) are not.

## Layout

| Path | What |
|---|---|
| `relay/` | Cloudflare Worker + SQLite-backed Durable Object (TypeScript) |
| `relay/test/` | Worker unit tests (vitest) |
| `phone/phone_client.py` | Phone client for Termux (Python, single file) |
| `phone/tests/` | Phone client unit tests, plus `fake_adb.py` |
| `scripts/e2e_test.py` | Local end-to-end test: `wrangler dev` + phone client + fake adb |
| `docs/AGENT_INSTRUCTIONS.md` | Instructions to give Claude |
| `docs/PLAN.md` | The plan this was built from |

## 1. Deploy the relay (on your own computer)

Requirements: Node.js 20+ and a free Cloudflare account.

```bash
cd relay
npm ci                      # use npm ci: plain `npm install` currently hits an npm peer-dependency bug (npm 10.9)
cp wrangler.example.jsonc wrangler.jsonc
# edit wrangler.jsonc: set "name" to something non-guessable, e.g. phone-relay-$(openssl rand -hex 4)
npx wrangler login

# Three separate tokens, each at least 32 characters (shorter tokens are rejected).
# Save them in your password manager; ADMIN_TOKEN is for you only.
for t in AGENT_TOKEN DEVICE_TOKEN ADMIN_TOKEN; do openssl rand -hex 32 | tee /dev/stderr | npx wrangler secret put $t; done

npx wrangler deploy
```

`wrangler deploy` prints the URL, `https://<name>.<account-subdomain>.workers.dev`. Below, its host is called `WORKER_HOST`.

`wrangler.jsonc` is git-ignored so the real Worker name stays out of the repository.

Quick check (expect `401`):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://WORKER_HOST/state
```

## 2. Set up the phone (Samsung S24, Termux)

1. **Developer options**: Settings > About phone > Software information > tap Build number seven times. Then Developer options > Wireless debugging: on (needs Wi-Fi).
2. **Termux from F-Droid** (the Play Store build is outdated).
3. In Termux:
   ```bash
   pkg update
   pkg install android-tools python python-pillow git
   git clone <this repository URL> phone-relay      # a private repo needs a GitHub token; or copy the phone/ folder over
   cd phone-relay/phone
   pip install -r requirements.txt                  # websockets
   ```
4. **Pair adb with the phone itself.** Open Termux in split screen or a pop-up window (the pairing dialog closes if you leave it). Then Wireless debugging > Pair device with pairing code:
   ```bash
   adb pair localhost:<PAIR_PORT> <CODE>
   adb connect localhost:<PORT>        # PORT is on the main Wireless debugging screen, not the pairing one
   adb devices                         # should list exactly one device
   ```
   If `adb devices` lists more than one entry, set `ADB_SERIAL=localhost:<PORT>` in `.env`.
5. **Battery**: Settings > Apps > Termux > Battery: Unrestricted. Also add Termux to Never auto sleeping apps (Battery > Background usage limits).
6. **Auto Blocker**: if adb pairing or connecting fails, check Settings > Security and privacy > Auto Blocker. I have not verified whether it affects wireless debugging on One UI 9.
7. **Config**:
   ```bash
   cp .env.example .env && chmod 600 .env
   # set RELAY_URL=wss://WORKER_HOST/device and DEVICE_TOKEN=<device token>
   ```
8. **Self-test**: open Chrome on the phone, then:
   ```bash
   python phone_client.py --selftest
   ```
   It prints the raw `mCurrentFocus`/`mWakefulness` lines, the parsed state, saves `selftest_screenshot.jpg` and lists a few UI elements. **Check that the foreground package it reports is right.** The parsing is unverified on Android 17 / One UI 9, and the allowlist depends on it. Then open Settings and re-run the self-test: it must report `foreground_allowed: false` and stop.
9. **Run**:
   ```bash
   ./start.sh            # takes a wake lock and runs the client; reconnects with backoff
   ```

Wireless debugging turns off when Wi-Fi changes, and its port changes. Expect to run `adb connect` again (and sometimes re-pair) after reboots or network changes. This is a phase 1 limitation.

## 3. Wire up Claude (cloud environment)

1. Use a **dedicated cloud environment** for phone sessions. A network secret applies to every session in its environment, whoever starts it.
2. Open the environment for editing at claude.ai/code > **Network secrets** > **Add secret**:
   - Secret type: **Bearer**
   - Allowed websites: the exact `WORKER_HOST`, not `*.workers.dev`
   - Custom headers: `Authorization`, prefix `Bearer`, value = the **agent** token
   - (Team/Enterprise without an admin role: Host = `WORKER_HOST`, Secret type = Token, Value = agent token.)
3. Optional hardening: set the environment's network access to **None**. Hosts listed on a network secret stay reachable at any level, so the session can reach the relay and nothing else. Package installs will then fail in those sessions.
4. In a new session in that environment, smoke test:
   ```bash
   curl -sS https://WORKER_HOST/state
   curl -sS "https://WORKER_HOST/screenshot?scale=0.4" -o /tmp/s.jpg -D -
   curl -sS -X POST https://WORKER_HOST/launch -H 'Content-Type: application/json' -d '{"package":"com.android.chrome"}'
   env | grep -ci bearer     # expect 0: the token is not in the session
   ```
5. Give Claude [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md). For example, copy it into the CLAUDE.md of the repository you start phone sessions from, with `WORKER_HOST` filled in.

## 4. Owner controls (admin token: run these from your own machine, never in a Claude session)

```bash
A="Authorization: Bearer $ADMIN_TOKEN"; R=https://WORKER_HOST
curl -sS -H "$A" $R/admin/status              # connected?, device info, rate counts
curl -sS -H "$A" "$R/admin/log?n=50"          # action log (typed text is stored as length only)
curl -sS -H "$A" -X POST $R/admin/disable     # kill switch: drops the phone, blocks reconnects
curl -sS -H "$A" -X POST $R/admin/enable
```

On the phone, `touch ~/STOP_PHONE_AGENT` makes the client refuse all commands and exit within about 2 seconds. `start.sh` removes the file when you start the client again.

Rotate a token with `npx wrangler secret put <NAME>`. For the agent token, also update the network secret. For the device token, also update `phone/.env`.

## 5. API

All endpoints need `Authorization: Bearer <AGENT_TOKEN>`, which the network secret adds. Full agent-facing details are in [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md).

| Endpoint | Notes |
|---|---|
| `GET /state` | foreground, foreground_allowed, awake, battery, screen, allowlist |
| `GET /screenshot?scale=0.5` | JPEG (PNG if Pillow is missing on the phone). Headers `X-Image-*`, `X-Screen-*`, `X-Scale`, `X-Foreground` |
| `GET /ui` | Elements from allowed packages only; password text is blanked |
| `POST /tap` `{x,y,scale?}` | |
| `POST /swipe` `{x1,y1,x2,y2,ms?,scale?}` | ms 50-5000 |
| `POST /type` `{text}` | 1-500 printable ASCII, no `%`; refused if the focused field is a password field |
| `POST /key` `{key}` | back, home, recents, enter, tab, delete |
| `POST /launch` `{package}` | Gmail or Chrome only |

Errors: 400 `invalid_args`, 401 `unauthorized`, 404, 405, 409 `foreground_not_allowed` / `screen_off` / `password_field`, 429 `rate_limited`, 503 `device_offline` / `disabled` / `stopped`, 504 `device_timeout`, 502 other device errors.

## 6. Tests

Install:

```bash
cd relay && npm ci && cd ..
python3 -m pip install websockets pillow     # in a venv if your system Python is externally managed
```

Run:

```bash
# Worker unit tests and type-check
cd relay && npm test && npm run typecheck && cd ..

# Phone client unit tests (fake adb; no phone needed)
python3 -m unittest discover -s phone/tests -v

# End-to-end: real Worker + Durable Object in `wrangler dev`, real phone client, fake adb
python3 scripts/e2e_test.py
```

What the end-to-end test covers: token separation (agent, device and admin tokens cannot stand in for each other), validation, every endpoint, the allowlist (Settings, lock screen, launcher), screen off, password-field refusal, Home always allowed, launch, the screenshot rate limit, the admin kill switch and reconnect, the stop file, a bad device token, and that typed text never reaches the log.

Not yet tested, because it needs the real phone and account (phase 4 of the plan): One UI 9 output formats, reboot and Wi-Fi-drop recovery, token rotation, and the prompt-injection test with a hostile email and web page.

## 7. Security notes and known gaps

- **Allowlist**: enforced on the phone (it checks the foreground app before every action, and again after screenshots and UI dumps) and in the Worker (`/launch`, keys). The check and the action are not atomic. Focused in-app popups (`PopupWindow:…`, e.g. Chrome's menu) are attributed to the app in `mFocusedApp`; any other window without a package name (lock screen, shade) is refused.
- **Notifications**: a heads-up notification from another app can appear in a screenshot taken while Chrome or Gmail is in front. Turn on Do Not Disturb during agent sessions.
- **Password fields**: adb *can* type into password fields. This differs from the plan's "cannot enter secure fields (by design)". The client refuses `/type` when the focused node has `password="true"` in the accessibility tree. That covers standard Android and HTML password inputs, but not custom fields that do not flag themselves.
- **Gmail and Chrome**: use a test Google account. Keep Chrome signed out, with saved passwords and payment autofill off. The allowlist limits apps, not what Chrome can browse to.
- **Rate limits** (60 requests/min, 20 screenshots/min) are stored in Durable Object storage, so they survive hibernation. `/state` and `/ui` count towards the 60.
- **Coordinates** are checked against the larger screen dimension so both orientations work. Taps outside the visible screen are possible within that square; Android ignores them.
- **Phone-side policy** (allowlist, keys) is constant in `phone_client.py`. Changing it means editing the code on the phone, which is intentional.

## 8. Not built

- Phase 5 native app (Accessibility Service + MediaProjection) and phase 6 MCP wrapper / custom domain.
- A status page for you (open question 4 in the plan); `/admin/status` covers this for now.
