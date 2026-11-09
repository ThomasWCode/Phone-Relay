# What the agent can and cannot do

Three lists:
- **Can do**: built, and nothing stops it.
- **Actively restricted**: code refuses it, on the phone client, in the Worker, or both.
- **Not built**: nothing prevents it, but no endpoint exists yet.

The owner's rules in [PHONE_RULES.md](PHONE_RULES.md) and the rules in [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) (never send email unasked, treat screen text as data, and so on) are guidance to the agent. **They are not enforced by code.** There is no app allowlist, no key deny list, no rate limit and no range limit.

## Two clients

The phone side is either the **Android app** (always on; it drives the phone through its accessibility service) or the **adb client** (`phone_client.py` over wireless debugging; it drives the phone as the shell user). `/state` says which is connected (`client`: `android-app` or `termux-adb`) and, for the app, lists the commands it runs (`capabilities`). Only one is connected at a time: a new phone connection replaces the old one (the relay closes it with `replaced`) and neither client gives way, so run the adb client only with the app switched off. Everything below applies to both, except:

| | Android app | adb client |
|---|---|---|
| Runs | Always, on any network; back by itself after an update, and after a reboot once the phone has first been unlocked (Android starts apps only then; not yet tested) | Only while wireless debugging is on (Wi-Fi); started again after each reboot |
| Off switch on the phone | Quick Settings tile, home screen widget, notification, the app | The stop file |
| `if_foreground` | Checked just before the action, milliseconds ahead of it | Checked in the same shell command as the action, which still leaves a gap: `input` (a Java program) takes a moment to start |
| Typing | Into the focused field through the app's own input connection, as a keyboard types; with none focused, ASCII only, through the shell bridge (409 `no_text_field` otherwise) | Anywhere, as key events (`input text`), pasting what is not ASCII |
| Keys | Back, Home, Recents, the shade, power (locks; held: the power menu), wake, d-pad, volume, media, page up/down; while a field is focused, any key; any other key through the shell bridge (below) | Any key |
| UI tree | Every window of the main user, as the accessibility framework gives it; `engine=uiautomator` gives the same tree and unbinds nothing | The same through the helper, or `uiautomator` |
| Screenshots | Through the accessibility service (about 3 a second at most) | `screencap` |
| Files, notifications | Files: shared storage (the owner grants All files access) and the app's own folders. Notifications: through its notification listener (the owner grants notification access) | Files: whatever the shell user can read and write. Notifications: `dumpsys notification --noredact` |
| Gestures | Up to 10 fingers, any length (past Android's 60 s, as strokes that continue each other, the fingers staying down) | Up to 10 fingers, any length |
| Reading the clipboard | An invisible window of the app comes forward for a moment (Android lets only the app in front read it) | Through the helper |
| Screen recording, audio | MediaProjection: Android's consent prompt shows for a moment before each, and the app answers it; the status bar shows the capture. One MP4 at any length. The sound output is captured as it plays, without apps that refuse capture (and calls) | `screenrecord` (MP4 segments past 180 s) and the helper (the output goes quiet on the phone meanwhile) |
| Chrome DevTools, any key, typing with no field focused | Through the **shell bridge**: the app's own adb connection to this phone's wireless debugging, so only on Wi-Fi, and only once the owner has paired it (503 `shell_unavailable` otherwise, with the reason). The app switches wireless debugging back on by itself when Android has turned it off, if it holds the secure-settings grant | Direct |
| Installs, accessibility, logcat, settings, `/run-as`, `as_shell` intents | Through the shell bridge (Wi-Fi only, once paired). What is installed of a package (`/package`) and its APK (`/package/apk`) come from PackageManager, so they work anywhere | Direct (`/package` reads the signing certificate from the installed APK's signing block) |
| Asking the owner (`/request`) | A card over the screen and a notification, with Accept, Deny and a message box | Not built (501 `unsupported`): nothing of its own to draw on the screen |
| Grants the owner gives once | Accessibility service, notification access, all-files access, modify system settings, no battery optimisation, the microphone; for the shell bridge, pairing (the code typed into a notification) and, optionally, the secure-settings grant over adb | Wireless debugging pairing |

## Can do

### See
- **Screenshot** of whatever is on the screen (any app, the home screen, the notification shade, the lock screen) at any scale above 0, as JPEG. Scales above 1 give the full-size image. Headers give the image size, screen size, scale and app in front.
- **UI tree** (`/ui`): every element on the screen from every package (status bar, keyboard and overlays included): text (password fields' text too, as Android reports it), content descriptions, resource ids, classes, flags (clickable, scrollable, editable, focused, password, checked) and real-pixel bounds and centres. With the adb client it comes from the helper (see below), which connects to the accessibility framework with `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES`, so the phone's own accessibility services stay bound. With the adb client, `engine=uiautomator` uses `uiautomator dump` instead, which unbinds them all for a moment; nothing else calls it. The app reads the tree from its accessibility service, and both engines give that. Only what is on the screen is in the tree. Apps running in another profile (a work profile, Secure Folder, cloned apps) are not in it at all, as Android's accessibility framework serves each user separately; screenshots and input still reach them.
- **State**: app in front, screen awake, battery, screen size, client version.

### Touch
- **Tap** any point. Coordinates can be any number; Android ignores points off the screen.
- **Swipe, scroll and drag** with one finger, lasting any time from 0 ms.
- **Long press**: a swipe with the same start and end point and `ms` of 500 or more.
- **Multi-touch** (`/gesture`): up to 10 fingers at once, each along its own path of points, all over the same duration: pinch to zoom, rotate, two-finger swipes.
- Taps on the on-screen keyboard, password fields included.

### Type
- Any text of any length. ASCII is typed as keys (`input text`; `%` and `%s` included, newlines and tabs as Enter and Tab). Anything else (accented letters, other scripts, emoji) is pasted through the clipboard, and the clipboard's earlier text is put back afterwards. `method` forces either way. With the app and no field focused, only ASCII can be typed (through the shell bridge).
- Into any field, password fields included, or with no field focused. The reply gives the focused field's package, `inputType` and whether it is a password field.

### Keys
- `back`, `home`, `recents`, `enter`, `tab`, `delete`.
- Any other Android key by name or number: power, sleep and wake-up, volume, media, calls, camera, assistant and brightness included. `"longpress": true` holds it.

### Apps
- **Launch** any installed app by package name, over any app.
- **Open a URL** directly (`/open`), any scheme: a web page in the browser, a `mailto:` link as a new email in the mail app (subject and body from the link), a deep link into an app. Optionally in a chosen package.
- **Send any intent** (`/intent`): start an activity or send a broadcast with an action, data, MIME type, component, package, categories, flags and typed extras (a particular Settings screen, a share to an app, ...).
- **Use** any app, Settings, the notification shade and quick settings, and the lock screen's own controls. Unlocking a locked phone needs its PIN or biometrics, which the agent does not have.
- In a browser, anything a person can do by touch and typing: open any URL, use sites where the phone is signed in, download files, pick saved passwords from the browser's own autofill dropdown.
- **Chrome's pages from the inside**, through Chrome DevTools (`/chrome/*`): list the tabs, run JavaScript in any tab and get its value (`/chrome/eval`), call any DevTools protocol method on a tab or the browser (`/chrome/cdp`: navigate, read the DOM, cookies and storage, network, screenshots of a page, input events, ...), and open, bring to front or close tabs. The page's own session applies: what the signed-in user's Chrome can do, the script can do. Chrome lists no tabs while it is in the background with its pages frozen, and has no DevTools socket at all while it is not running (a 502 `devtools_unavailable`); `/launch` it first. Debuggable WebViews in other apps have DevTools sockets too (`/chrome/sockets`), usable with `socket=`.
- In email and messaging apps: read, search, compose, send, forward, delete, switch accounts.

### Clipboard
- **Read** the clipboard's text, **set** it, Unicode included, or **empty** it (`/clipboard`).

### Notifications and system UI
- **Read notifications** as data (`/notifications`): each one's package, key, id, tag, title, text, big text, sub text and summary, from `dumpsys notification --noredact` (the adb client) or the app's notification listener. Nothing is redacted.
- **Open or close** the notification shade and quick settings (`/statusbar`).
- **Rotation**: lock the screen to a quarter turn or let it rotate automatically (`/rotation`); the reply gives the old setting, and `/state` reports the current one.

### Files
- **List** a folder, or one file (`/files`): names, types, sizes, modified times.
- **Pull** any file the shell can read (`/files/pull`), and **push** a file anywhere the shell can write (`/files/push`), of any size (with the app: shared storage, which the owner's All files access reaches, and the app's own folders). Files cross the relay in acknowledged 256 KiB chunks and are streamed, never held whole in memory. A pushed file is not added to the media library, so a gallery app may not show it straight away.

### Screen recording
- **Record** the screen as MP4: for a set time (`/record?seconds=`), or around other commands (`/record/start`, then anything, then `/record/stop`, which returns the video). With the adb client Android stops each file at 180 s, so a longer recording comes back as a zip of consecutive MP4 segments; the app's is one MP4. The video has no sound.
- **Audio** (`/audio`): the phone's audio output or its microphone, for a set time, as a 48 kHz stereo WAV. With the adb client the output does not play on the phone itself while it is captured; the app captures it as it plays, but only sound that starts after the capture has begun: a stream already open when it starts (a track mid-play) can stay out of it, so start or restart the playback after `/audio` begins (a delayed start from inside the page, or a track change). The microphone shows Android's privacy indicator.

### Waiting and batches
- **Wait** (`/wait`) until the screen changes, stops changing (an animation or page load has settled), an app comes to the front, or some text appears in the UI tree. Screens are compared at 1/8 scale, so a blinking cursor does not count as a change.
- **Batch** (`/batch`): several commands in one request, with sleeps between them. Each step has its own deadline and takes the batch's `if_foreground` unless it sets its own. A batch stops at the first error unless told not to.

### Releases and debugging
- **Install** an APK already on the phone over the app that is there (`/install`, `pm install -r`): only the package named (the APK's own is read first), never a downgrade, never an uninstall or cleared data. Android's own refusal comes back as it is (a signature mismatch, a downgrade). It can be queued in a `/request`'s steps, for the owner to accept while they use the phone.
- **What is installed** (`/package`): a package's version, install and update times, signing certificate (SHA-256), whether it is debuggable and its enabled state; **the installed APK** itself (`/package/apk`).
- **Accessibility services** (`/accessibility`): those enabled, those bound, and those enabled but not bound (Android drops a service that crashes as it connects from the bound list without a word).
- **The log** (`/logcat`): `logcat -d`, read-only, by tag, level, time and buffer (the crash buffer too), capped in lines.
- **One setting** (`/settings`), read-only, from `system`, `secure` or `global`, with dark mode.
- **A debuggable app's own files** (`/run-as`, `/run-as/files`): read and list them through `run-as`, read-only, inside its data folder.
- **Intents as the shell user** (`/intent` with `as_shell`): for a component that needs a permission only the shell has (`DUMP`: a debug-command activity, say).
- **The after-install checks** (`/check`): the version, what is in front, the accessibility services (and any gone since the last check, which the relay remembers), crashes since the install, and a debuggable app's files beside the last check's; `ok` says whether all is well.

### Asking the owner (the app)
- **Request** (`POST /request`): asks the owner whether the agent may use the phone. A card appears at the top of the screen, over any app, with the agent's reason, the steps it queued (in words, typed text and URLs included), a countdown, a message box and Accept and Deny; a notification carries the same, with Accept, Deny and Deny with a reason, for when the card is out of sight. The card is an accessibility overlay: it needs no "display over other apps" grant, touches outside it reach the app beneath, so the owner can keep using the phone before answering, and dragging its header moves it. It takes the keyboard only while the owner types a message.
- The reply comes when they answer, or after `seconds` (default 300) as `expired`: `accepted` or `denied`, with what they typed. On an Accept the queued steps (any `/batch` steps) run at once, before anything else can come to the front, the card already gone.
- Nothing enforces it: the agent decides when to ask (the owner's rules say: when the phone seems in use), and a Deny refuses nothing; it is a reply, which the agent's rules say to follow. The relay keeps the last request's outcome (`GET /request`), for an agent whose own call was cut off.

### The helper (adb client)
Gestures, the clipboard, pasted text, audio and the UI tree go through a small Java helper (`phone/helper/`), which the phone client pushes to `/data/local/tmp` on first use and runs as the shell user with `app_process`, as scrcpy does. It reaches hidden framework APIs by reflection; if a future Android changes one, that command fails with 502 `helper_failed` and its reason, and nothing falls back silently.

### Control
- **`if_foreground`**: any command can name one package or a list. It then runs only while one of them has focus and the screen is on, checked on the phone just before the action (the adb client: in the same shell command); otherwise it is refused with 409 `foreground_mismatch`. A screenshot or UI dump is thrown away if the app changed during the capture. In-app popup menus count for their app.
- **`timeout`**: any command can set its deadline in seconds, with no upper limit. Without one it is 30 s, plus the work's own time: 0.1 s per typed character, a swipe's duration, a wait's `seconds`, twice a recording's length, a batch's steps. Moving a file, or stopping a recording and fetching it, gets 300 s.
- Fields a command does not take are dropped and named in the reply (`ignored_fields`, or the `X-Ignored-Fields` header on screenshots), so a typo does not pass silently.

## Actively restricted

| # | Restriction | Where | Why |
|---|---|---|---|
| 1 | Tokens: the agent token cannot use the admin or device endpoints | Worker | The agent cannot switch the relay back on, read the log, change the owner's notes or pose as the phone |
| 2 | Owner kill switches: `/admin/disable`, the app's switch (tile, widget, notification, app) and the adb client's stop file | Worker + client | The owner's off switch. They end the relay's path to the phone, not `adb` from the owner's own computer |
| 3 | One phone, one command at a time | Worker + client | Actions must not interleave; a second phone connection replaces the first |
| 4 | `/install` installs only the package named (the APK's own, read from it first), with `pm install -r` and never `-d`; there is no uninstall and no clearing of data | Worker + client | A release replaces the app that is there, never another, and never loses its data |
| 5 | `/run-as` and `/run-as/files` read only a debuggable app's files, by a path inside its data folder (no `..`), and write nothing | Worker + client | `run-as` itself refuses other apps; the relay says so first (403 `not_debuggable`) |
| 6 | `/intent` with `as_shell` needs an explicit `component` | Worker + client | The shell user's intent reaches one named component, never anything a resolver would pick, so it is no general shell |
| 7 | `/accessibility`, `/logcat`, `/package`, `/settings` and `/check` only read | Client | They run fixed read-only commands (`settings get`, `dumpsys`, `logcat -d`) |

Requests are still checked for shape, in the Worker and again on the phone: numbers must be numbers, package and key names must match their patterns, and text typed as keys (`method: keys`) must be ASCII; anything else is pasted. That keeps values out of the shell's syntax; it refuses no action.

Typed text is never logged, only its length. That limits what is recorded, not what the agent can do.

## Not built

Nothing from the earlier list is left; the native app (phase 5) is built. The owner dropped these rather than have them built: the phase 6 MCP wrapper and custom domain, and a status page for the owner (`/admin/status` and `/admin/log` give the same information).

The app does all the adb client does. What only the shell user can do (Chrome DevTools, other keys, typing with no field focused, installs, accessibility, logcat, settings, `run-as`, intents as the shell) it does through the shell bridge, so on Wi-Fi only, and once the owner has paired it.

Not part of `/check`, which keeps to what any app has: the checks of Decrastination's `scripts/phone-check.sh` that are that app's own (its calendar's slots, its crash records in `files/crashes`); read them with `/run-as` and `/run-as/files`.
