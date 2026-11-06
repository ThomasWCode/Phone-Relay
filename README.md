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
   It prints the raw `mCurrentFocus`/`mWakefulness` lines and the parsed state, runs the input guard with a harmless `echo`, shows the focused text field's `inputType`, and saves `selftest_screenshot.jpg`. It sends no input. **Check that the foreground package it reports is right**: the allowlist depends on it. Then open Settings and re-run the self-test: it must report `foreground_allowed: false` and stop.
9. **Run**:
   ```bash
   ./start.sh            # takes a wake lock and runs the client; reconnects with backoff
   ```

Wireless debugging turns off when Wi-Fi changes, and its port changes. Expect to run `adb connect` again (and sometimes re-pair) after reboots or network changes. This is a phase 1 limitation.

### Alternative for testing: run the client on a computer

The same client runs on a computer that already has adb connected to the phone (USB or wireless). Nothing is installed on the phone and no phone settings change. The phone only takes part while the computer runs the client. On Windows (PowerShell), from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r phone/requirements.txt pillow
adb devices                                        # exactly one device, or set ADB_SERIAL in phone/.env
Copy-Item phone/.env.example phone/.env            # set RELAY_URL and DEVICE_TOKEN
.\.venv\Scripts\python.exe phone/phone_client.py --selftest
.\.venv\Scripts\python.exe phone/phone_client.py   # Ctrl-C to stop
```

The stop file is `~/STOP_PHONE_AGENT` on the computer (`$HOME\STOP_PHONE_AGENT` on Windows).

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
   A `403` with `error code: 1010` (not the relay's JSON `401`) is Cloudflare's bot check rejecting the client's `User-Agent`. Python's `urllib` default is rejected; `curl` and the phone client are not. Set a `User-Agent` header.
5. Point Claude at the instructions. The relay serves them behind the agent token, so in the phone environment a session can read them with no copying and no extra hosts:
   - `GET /` and `GET /docs/AGENT_INSTRUCTIONS.md`: [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md), with `<WORKER_HOST>` replaced by the relay's host
   - `GET /docs/CAPABILITIES.md`: [docs/CAPABILITIES.md](docs/CAPABILITIES.md)

   Tell the session, for example: *Before using the phone, run `curl -sS https://WORKER_HOST/` and follow it.* The files are bundled at deploy time, so run `npx wrangler deploy` again after changing them.

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
| `GET /ui` | Elements from allowed packages only; password text is blanked. Runs `uiautomator dump`, which unbinds every accessibility service on the phone for a moment |
| `POST /tap` `{x,y,scale?}` | Refused while the keyboard is open on a password field |
| `POST /swipe` `{x1,y1,x2,y2,ms?,scale?}` | ms 50-5000; same password-keyboard rule as tap |
| `POST /type` `{text}` | 1-500 printable ASCII, `%` included; refused in password fields; the reply gives the focused field's `inputType` |
| `POST /key` `{key}` | back, home, recents, enter, tab, delete, or any `KEYCODE_*` except power, calls, camera, assistant, volume, brightness and media; only while an allowed app is in front |
| `POST /launch` `{package}` | Gmail or Chrome only, started from an allowed app or the home screen |

Errors: 400 `invalid_args`, 401 `unauthorized`, 404, 405, 409 `foreground_not_allowed` / `screen_off` / `password_field`, 429 `rate_limited`, 503 `device_offline` / `disabled` / `stopped`, 504 `device_timeout`, 502 other device errors.

The full list of what the agent can do, what is actively restricted and what is just not built is in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

## 6. Tests

Install:

```bash
cd relay && npm ci && cd ..
python3 -m pip install websockets pillow     # in a venv if your system Python is externally managed
```

On Windows use `python` (or `.\.venv\Scripts\python.exe`) for `python3`. The real-shell guard tests use Git for Windows' `sh.exe`, found next to `git`, and are skipped without it. npm 11 reports that the `esbuild` and `workerd` install scripts are not approved; `wrangler dev` and the tests work without them.

Run:

```bash
# Worker unit tests and type-check
cd relay && npm test && npm run typecheck && cd ..

# Phone client unit tests (fake adb; no phone needed)
python3 -m unittest discover -s phone/tests -v

# End-to-end: real Worker + Durable Object in `wrangler dev`, real phone client, fake adb
python3 scripts/e2e_test.py
```

What the end-to-end test covers: token separation (agent, device and admin tokens cannot stand in for each other), validation, every endpoint, the allowlist (Settings, Teams, lock screen, launcher), in-app popups, screen off, password-field refusals (typing, and keyboard taps), typing `%`, `KEYCODE_*` keys and denied keys, Home and launch refused over other apps, launch from the home screen, the screenshot rate limit, the admin kill switch and reconnect, the stop file, a bad device token, and that typed text never reaches the log.

Checked on a Galaxy S24 (Android 17) with real input in Chrome and Gmail:
- the self-test;
- taps, swipes and typing (`%` and `%s` included);
- `KEYCODE_*` keys, with `KEYCODE_POWER` refused;
- refusals: typing into a password field, keyboard taps on a password field, taps on the home screen;
- launch from Chrome and from the home screen.

- `/ui` once in Chrome; all accessibility services were still enabled afterwards and logcat showed no crashes.

Also checked through the deployed Worker on `workers.dev`, with the client running on a PC over wireless adb: `/state`, `/launch`, `/screenshot`, `/swipe` and `/key`, plus refusals on the home screen (about 0.2-1.4 s per action).

Not yet tested, because it needs the real phone and account (phase 4 of the plan): reboot and Wi-Fi-drop recovery, token rotation, and the prompt-injection test with a hostile email and web page.

## 7. Security notes and known gaps

- **Allowlist**: enforced by the client (it checks the foreground app before every action, and again after screenshots) and in the Worker (`/launch`, keys). Every input, screenshot and launch also runs inside a shell guard on the phone: `if <allowed app focused and screen awake>; then <action>; else echo PHONE_RELAY_REFUSED; fi`. The check and the action run in the same command, so a focus change between the client's check and the action is refused. A change in the milliseconds inside that one command is still possible. Focused in-app popups (`PopupWindow:…`, e.g. Chrome's menu) are attributed to the app in `mFocusedApp`; any other window without a package name (lock screen, shade) is refused.
- **`uiautomator`**: only `/ui` runs `uiautomator dump`, which briefly unbinds every accessibility service on the phone. Nothing else calls it, including `--selftest`.
- **Keyboard taps**: the on-screen keyboard never takes focus, so its keys pass the focus guard. While a password field is focused and the keyboard is open, taps and swipes are refused, so a password cannot be typed key by key. Chrome's in-page autofill dropdown is not covered.
- **Notifications**: a heads-up notification from another app can appear in a screenshot taken while Chrome or Gmail is in front. Turn on Do Not Disturb during agent sessions.
- **Password fields**: adb *can* type into password fields. This differs from the plan's "cannot enter secure fields (by design)". The client reads the focused field's `inputType` from `dumpsys input_method` and refuses `/type` for password types (text, visible, web and number passwords). That covers standard Android and HTML password inputs, but not custom fields that do not declare a password type.
- **Gmail and Chrome**: use a test Google account. Keep Chrome signed out, with saved passwords and payment autofill off. The allowlist limits apps, not what Chrome can browse to.
- **Rate limits** (300 requests/min, 120 screenshots/min by default; set `RATE_ACTIONS_PER_MIN` / `RATE_SCREENSHOTS_PER_MIN` as wrangler vars to change them) are stored in Durable Object storage, so they survive hibernation. `/state` and `/ui` count as requests.
- **Coordinates** are checked against the larger screen dimension so both orientations work. Taps outside the visible screen are possible within that square; Android ignores them.
- **Phone-side policy** (allowlist, keys) is constant in `phone_client.py`. Changing it means editing the code on the phone, which is intentional.

## 8. Not built

- Phase 5 native app (Accessibility Service + MediaProjection) and phase 6 MCP wrapper / custom domain.
- A status page for you (open question 4 in the plan); `/admin/status` covers this for now.
