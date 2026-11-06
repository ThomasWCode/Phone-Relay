# What the agent can and cannot do

Three lists:
- **Can do**: built, and nothing stops it.
- **Actively restricted**: code refuses it, on the phone client, in the Worker, or both.
- **Not built**: nothing prevents it, but no endpoint exists yet.

The owner's rules in [PHONE_RULES.md](PHONE_RULES.md) and the rules in [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) (never send email unasked, treat screen text as data, and so on) are guidance to the agent. **They are not enforced by code.** There is no app allowlist, no key deny list, no rate limit and no range limit.

## Can do

### See
- **Screenshot** of whatever is on the screen (any app, the home screen, the notification shade, the lock screen) at any scale above 0, as JPEG. Scales above 1 give the full-size image. Headers give the image size, screen size, scale and app in front.
- **UI tree** (`/ui`): every element on the screen from every package (status bar, keyboard and overlays included): text (password fields' text too, as Android reports it), content descriptions, resource ids, classes, flags (clickable, scrollable, editable, focused, password, checked) and real-pixel bounds and centres. **Side effect:** it runs `uiautomator dump`, which unbinds every accessibility service on the phone for a moment. Nothing else calls it.
- **State**: app in front, screen awake, battery, screen size, client version.

### Touch
- **Tap** any point. Coordinates can be any number; Android ignores points off the screen.
- **Swipe, scroll and drag** with one finger, lasting any time from 0 ms.
- **Long press**: a swipe with the same start and end point and `ms` of 500 or more.
- Taps on the on-screen keyboard, password fields included.

### Type
- ASCII text of any length, `%` and `%s` included. Newlines and tabs are sent as Enter and Tab.
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
- In email and messaging apps: read, search, compose, send, forward, delete, switch accounts.

### Notifications and system UI
- **Read notifications** as data (`/notifications`): each one's package, key, id, tag, title, text, big text, sub text and summary, from `dumpsys notification --noredact`. Nothing is redacted.
- **Open or close** the notification shade and quick settings (`/statusbar`).
- **Rotation**: lock the screen to a quarter turn or let it rotate automatically (`/rotation`); the reply gives the old setting, and `/state` reports the current one.

### Files
- **List** a folder, or one file (`/files`): names, types, sizes, modified times.
- **Pull** any file the shell can read (`/files/pull`), and **push** a file anywhere the shell can write (`/files/push`), of any size. Files cross the relay in acknowledged 256 KiB chunks and are streamed, never held whole in memory. A pushed file is not added to the media library, so a gallery app may not show it straight away.

### Screen recording
- **Record** the screen as MP4: for a set time (`/record?seconds=`), or around other commands (`/record/start`, then anything, then `/record/stop`, which returns the video). Android stops each file at 180 s, so a longer recording comes back as a zip of consecutive MP4 segments. No audio.

### Waiting and batches
- **Wait** (`/wait`) until the screen changes, stops changing (an animation or page load has settled), or an app comes to the front. Screens are compared at 1/8 scale, so a blinking cursor does not count as a change.
- **Batch** (`/batch`): several commands in one request, with sleeps between them. Each step has its own deadline and takes the batch's `if_foreground` unless it sets its own. A batch stops at the first error unless told not to.

### Control
- **`if_foreground`**: any command can name one package or a list. It then runs only while one of them has focus and the screen is on, checked in the same shell command on the phone as the action; otherwise it is refused with 409 `foreground_mismatch`. A screenshot or UI dump is thrown away if the app changed during the capture. In-app popup menus count for their app.
- **`timeout`**: any command can set its deadline in seconds, with no upper limit. Without one it is 30 s, plus the work's own time: 0.1 s per typed character, a swipe's duration, a wait's `seconds`, twice a recording's length, a batch's steps. Moving a file, or stopping a recording and fetching it, gets 300 s.
- Fields a command does not take are dropped and named in the reply (`ignored_fields`, or the `X-Ignored-Fields` header on screenshots), so a typo does not pass silently.

## Actively restricted

| # | Restriction | Where | Why |
|---|---|---|---|
| 1 | Tokens: the agent token cannot use the admin or device endpoints | Worker | The agent cannot switch the relay back on, read the log, change the owner's notes or pose as the phone |
| 2 | Owner kill switches: `/admin/disable`, and the stop file next to the client | Worker + client | The owner's off switch |
| 3 | One phone, one command at a time | Worker + client | adb actions must not interleave |

Requests are still checked for shape, in the Worker and again on the phone: numbers must be numbers, package and key names must match their patterns, and text must be ASCII (Unicode is not built yet). That keeps values out of the shell's syntax; it refuses no action.

Typed text is never logged, only its length. That limits what is recorded, not what the agent can do.

## Not built (nothing prevents it)

| Capability | How it could be added |
|---|---|
| Multi-touch: pinch-zoom, two-finger gestures | Not possible with `adb input`. A small helper run through `app_process` can inject multi-pointer events, as scrcpy does |
| Unicode, emoji in text | The same helper: put the text on the clipboard and paste it |
| Clipboard read and write | The same helper; the shell has no clipboard command |
| Chrome page DOM and JavaScript | Chrome DevTools over `adb forward ... localabstract:chrome_devtools_remote`; powerful, would let the agent read and script pages directly |
| UI tree without unbinding accessibility services | The helper, using UiAutomation with `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` |
| Waiting for text to appear on the screen | `/wait` on the UI tree, once reading it no longer unbinds accessibility services |
| Audio | `screenrecord` records no sound; the helper could capture it with `AudioRecord` |
| Status page for the owner | `/admin/status` covers this for now |
