# Phone Relay

Lets Claude, running in Claude Code cloud sessions, view and control an Android phone (any app: there is no allowlist) through a Cloudflare Worker relay. There is no VPS and no inbound port on the phone.

```
Claude (cloud session) --HTTPS, agent token added by the network secret-->  Cloudflare Worker
                                                                              |
                                                                    Durable Object "primary"
                                                                              ^
Phone (Termux: phone_client.py + adb over loopback) --outbound WSS, device token--+
```

The design and its reasoning are in [docs/PLAN.md](docs/PLAN.md). This README covers setup and use. Phases 0-4 of the plan are built, without the plan's app allowlist, key deny list, rate limits and input limits: those were removed later (see [docs/CAPABILITIES.md](docs/CAPABILITIES.md)). Phases 5-6 (native app, MCP, custom domain) were dropped.

## Layout

| Path | What |
|---|---|
| `relay/` | Cloudflare Worker + SQLite-backed Durable Object (TypeScript) |
| `relay/test/` | Worker unit tests (vitest) |
| `phone/phone_client.py` | Phone client for Termux (Python, single file) |
| `phone/helper/` | The helper the client runs on the phone with `app_process`: Java source, `build.py`, and the built `phone-relay-helper.jar` |
| `phone/tests/` | Phone client unit tests, plus `fake_adb.py` |
| `scripts/e2e_test.py` | Local end-to-end test: `wrangler dev` + phone client + fake adb |
| `docs/AGENT_INSTRUCTIONS.md` | Instructions to give Claude |
| `docs/PHONE_RULES.md` | The owner's rules for the phone, from their own instructions; the relay serves them to the agent |
| `docs/CAPABILITIES.md` | What the agent can do, what is restricted and what is not built |
| `docs/PLAN.md` | The plan this was built from |
| `private/` | Git-ignored: the owner's private notes for the agent (`PHONE_NOTES.md`) and local helper scripts |

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
8. **Self-test**: unlock the phone and open any app, then:
   ```bash
   python phone_client.py --selftest
   ```
   It prints the raw `mCurrentFocus`/`mWakefulness` lines and the parsed state, runs the `if_foreground` guard with a harmless `echo` (it must pass for the app in front and refuse another package), shows the focused text field's `inputType`, and saves `selftest_screenshot.jpg`. It sends no input. **Check that the foreground package it reports is right**: `if_foreground` depends on it.
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
   - `GET /docs/PHONE_RULES.md`: [docs/PHONE_RULES.md](docs/PHONE_RULES.md), followed by your private notes if you have set them (section 4)

   Tell the session, for example: *Before using the phone, run `curl -sS https://WORKER_HOST/` and follow it.* The files are bundled at deploy time, so run `npx wrangler deploy` again after changing them.

## 4. Owner controls (admin token: run these from your own machine, never in a Claude session)

```bash
A="Authorization: Bearer $ADMIN_TOKEN"; R=https://WORKER_HOST
curl -sS -H "$A" $R/admin/status              # connected?, device info, notes length
curl -sS -H "$A" "$R/admin/log?n=50"          # action log (typed text is stored as length only)
curl -sS -H "$A" -X POST $R/admin/disable     # kill switch: drops the phone, blocks reconnects
curl -sS -H "$A" -X POST $R/admin/enable
curl -sS -H "$A" -X PUT --data-binary @private/PHONE_NOTES.md $R/admin/notes   # private notes for the agent
curl -sS -H "$A" $R/admin/notes                # read them back; PUT an empty body to clear them
```

The private notes are appended to `/docs/PHONE_RULES.md` under "The owner's private notes". They live in the Durable Object, not in this public repository, so put anything personal there (your schedule, which apps you have installed), never in `docs/`.

On the phone, `touch ~/STOP_PHONE_AGENT` makes the client refuse all commands and exit within about 2 seconds. `start.sh` removes the file when you start the client again.

Rotate a token with `npx wrangler secret put <NAME>`. For the agent token, also update the network secret. For the device token, also update `phone/.env`.

## 5. API

All endpoints need `Authorization: Bearer <AGENT_TOKEN>`, which the network secret adds. Full agent-facing details are in [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md).

| Endpoint | Notes |
|---|---|
| `GET /state` | foreground, awake, battery, screen, client version |
| `GET /screenshot?scale=0.5` | JPEG (PNG if Pillow is missing on the phone). Headers `X-Image-*`, `X-Screen-*`, `X-Scale`, `X-Foreground` |
| `GET /ui?engine=` | Every element on the screen, every package, through the helper (accessibility services stay bound). `engine=uiautomator` runs `uiautomator dump`, which unbinds every accessibility service on the phone for a moment |
| `POST /tap` `{x,y,scale?}` | Any coordinates |
| `POST /swipe` `{x1,y1,x2,y2,ms?,scale?}` | `ms` from 0 |
| `POST /type` `{text,method?}` | Any text: ASCII as keys (`\n` and `\t` become Enter and Tab), the rest pasted through the clipboard; the reply gives the method, the focused field's package, `inputType` and whether it is a password field |
| `POST /key` `{key,longpress?}` | back, home, recents, enter, tab, delete, any `KEYCODE_*` name or key number |
| `POST /launch` `{package}` | Any installed app |
| `POST /open` `{url,package?}` | Any URL: web pages, `mailto:` links, deep links |
| `POST /intent` `{action?,data?,type?,component?,package?,categories?,flags?,extras?,broadcast?}` | Any activity intent or broadcast, through `am` |
| `GET /notifications` | Every notification as data (`dumpsys notification --noredact`) |
| `POST /statusbar` `{action}` | Open the shade or quick settings, or close them |
| `POST /rotation` `{rotation}` | 0-3 or `"auto"`; replies with the old setting |
| `POST /wait` `{for,seconds?,...}` | Until the screen changes, settles, or an app comes to the front |
| `POST /batch` `{steps,stop_on_error?}` | Several commands, with sleeps, in one request |
| `GET /files?path=`, `GET /files/pull?path=`, `POST /files/push?path=` | List, download and upload files of any size, streamed in acknowledged 256 KiB chunks |
| `GET /record?seconds=`, `POST /record/start`, `POST /record/stop` | Screen recording (MP4; a zip of segments past 180 s) |
| `POST /gesture` `{pointers,ms?,scale?}` | Multi-touch: up to 10 fingers, each along a path |
| `GET /clipboard`, `POST /clipboard` `{text}` | Read or set the clipboard |
| `GET /audio?seconds=&source=` | The sound output or the microphone, as WAV |
| `GET /chrome/tabs`, `POST /chrome/eval`, `POST /chrome/cdp`, `POST /chrome/new`, `/chrome/activate`, `/chrome/close`, `GET /chrome/sockets` | Chrome DevTools: tabs, JavaScript in a page, any DevTools protocol call |

Every endpoint also takes `if_foreground` (run only while that app, or one of a list, has focus, checked in the same shell command as the action) and `timeout` (seconds, no upper limit; default 30). Unknown fields are dropped and named in `ignored_fields`.

Errors: 400 `invalid_args`, 401 `unauthorized`, 404 (`not_found` for files), 405, 409 `foreground_mismatch`, 422 `launch_failed`, 503 `device_offline` / `disabled` / `stopped`, 504 `timeout` / `device_timeout`, 502 other device errors (`helper_failed` among them).

The full list of what the agent can do, what is actively restricted and what is just not built is in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

### The helper

`phone/helper/phone-relay-helper.jar` is committed, so neither Termux nor a PC running the client needs the Android SDK. To rebuild it after changing `phone/helper/src/` (needs a JDK 17+ and the Android SDK's platform and build-tools; `ANDROID_HOME` or the usual install folder):

```bash
python phone/helper/build.py          # writes the jar; commit it with the source
python phone/helper/build.py --check  # is the committed jar built from the committed source?
```

The build is reproducible (fixed timestamps), and a unit test fails if the jar was not rebuilt after a source change.

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

What the end-to-end test covers: token separation (agent, device and admin tokens cannot stand in for each other), validation, every endpoint, the docs and the owner's private notes, actions in any app (Settings, Teams, lock screen, launcher) and with the screen off, typing into password fields, typing `%`, newlines and long text, any key, launch of any app, `if_foreground` (refusals, popups, screen off), timeouts (a hung command stopped, a long one allowed), no rate limit, unknown fields named, Chrome DevTools against a fake DevTools endpoint (tabs, JavaScript, a raw call, a tab opened and closed, no forward left), opening URLs and `mailto:` links, intents, notifications, the status bar, rotation, waits, batches (and their `if_foreground`), listing files, a 3 MB push and pull compared byte for byte, empty and missing files, screen recordings (one file, and a zip past 180 s), the admin kill switch and reconnect, the stop file, a bad device token, and that typed text never reaches the log.

CI (`.github/workflows/tests.yml`) runs all three on every pull request.

Checked on a Galaxy S24 (Android 17) through the whole stack (`wrangler dev`, the phone client on a PC over wireless adb, HTTP as an agent sends it), after the allowlist was removed, every input with `if_foreground`: opening a URL and an intent, `/wait` for the foreground, a settled screen and text, screenshots, the helper's UI tree in Chrome with every accessibility service still bound during and after it, tapping and typing in Chrome's address bar (keys, then pasted Unicode, with the clipboard's old text back afterwards), setting, reading and emptying the clipboard, a two-finger pinch both ways, `/record/start`-`/record/stop` and `/record?seconds=`, 2 s of audio from the output and from the microphone, rotation to landscape and back, the shade, notifications, a 3 MB push and pull, a batch, `if_foreground` refusing, an unresolvable URL (422) and a timeout (504). Afterwards: no crash in logcat, the accessibility services enabled and bound, nothing left in `/data/local/tmp`. On that phone, `am` exits 1 for an unresolvable intent, `sha256sum` exits 1 for a missing file, and the helper needed the ActivityThread's ConfigurationController filled in for the clipboard and audio; the code allows for all three.

Checked on a Galaxy S24 (Android 17) with real input in Chrome and Gmail, before the allowlist was removed:
- the self-test;
- taps, swipes and typing (`%` and `%s` included);
- `KEYCODE_*` keys, with `KEYCODE_POWER` refused;
- refusals: typing into a password field, keyboard taps on a password field, taps on the home screen;
- launch from Chrome and from the home screen.

- `/ui` once in Chrome; all accessibility services were still enabled afterwards and logcat showed no crashes.

Also checked through the deployed Worker on `workers.dev`, with the client running on a PC over wireless adb: `/state`, `/launch`, `/screenshot`, `/swipe` and `/key`, plus refusals on the home screen (about 0.2-1.4 s per action).

Not yet tested, because it needs the real phone and account (phase 4 of the plan): reboot and Wi-Fi-drop recovery, token rotation, and the prompt-injection test with a hostile email and web page.

## 7. Security notes and known gaps

- **No allowlist**: the agent can do anything a person holding the unlocked phone can do, in any app (banking, messaging, Settings included), and press any key, power included. What stands between a session and the phone is the agent token (held by the network secret, never inside the session), the kill switches and the owner's rules in [docs/PHONE_RULES.md](docs/PHONE_RULES.md), which are guidance only.
- **`if_foreground`**: the old allowlist's same-command guard, now opt-in. `if <named app focused and screen awake>; then <action>; else echo PHONE_RELAY_REFUSED; fi` runs on the phone, so a focus change between the agent's check and its action is refused. A change in the milliseconds inside that one command is still possible. Focused in-app popups (`PopupWindow:…`) count for the app in `mFocusedApp`.
- **`uiautomator`**: only `/ui?engine=uiautomator` runs `uiautomator dump`, which briefly unbinds every accessibility service on the phone. The default `/ui`, and `/wait` for text, use the helper, which leaves them bound. Nothing else calls uiautomator, including `--selftest`.
- **The helper** runs as the shell user (`app_process`), with the shell's permissions: input injection, window content, the clipboard, audio capture. `--selftest` pushes it and runs its `version` command. Pasting puts the old clipboard text back after half a second; if Android refuses the helper's clipboard read, the paste still works but the old text is not restored. Capturing the sound output silences the phone's own speaker while it runs.
- **Notifications**: a heads-up notification from another app can appear in a screenshot. Turn on Do Not Disturb during agent sessions if that matters.
- **Password fields**: the agent can type into them and tap the keyboard over them. The reply to `/type` says when the focused field is a password field.
- **Timeouts**: each command has a deadline (default 30 s; the agent can set any). The phone kills the adb call when it passes, so a hung command does not block the queue; the relay waits 10 s longer for a reply before giving up.
- **Logging**: typed text is stored as its length only, inside batches too; an intent's extras are logged by name, not value.
- **Files**: pulls and pushes are streamed through the Worker in chunks with acknowledgements, at most 8 chunks (2 MiB) in flight, so a big file never sits whole in the Durable Object's memory. The phone client writes each to a temporary file next to it, which it deletes afterwards.
- **Chrome DevTools**: `adb forward tcp:0 localabstract:chrome_devtools_remote` for each `/chrome/*` command, removed afterwards. DevTools has the run of Chrome: the signed-in session, cookies, storage. Only the shell user (adb) can reach that socket, so nothing else on the phone gains it.
- **Recording** runs on the phone as a detached shell loop around `screenrecord`, with its state in `/data/local/tmp/phone_relay_rec.*`; `/record/stop` ends it, and `/record/start` ends any earlier one first.
- **Request shapes** are still checked (numbers are numbers, names match their patterns, text is ASCII), in the Worker and on the phone, so no value can carry shell syntax.

## 8. Not built

- Nothing from the earlier "Not built" list is left. Dropped, by the owner's choice: phase 5 (native app), phase 6 (MCP wrapper, custom domain) and a status page for the owner (open question 4 in the plan; `/admin/status` and `/admin/log` cover it).
