# What the agent can and cannot do

Three lists:
- **Can do**: built, and nothing stops it.
- **Actively restricted**: code refuses it, on the phone client, in the Worker, or both.
- **Not built**: nothing prevents it, but no endpoint exists yet.

The rules in [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) (never send email unasked, treat screen text as data, and so on) are guidance to the agent. **They are not enforced by code.**

## Can do

### See
- **Screenshot** of Gmail or Chrome at any scale from 0.1 to 1 (JPEG). Headers give the image size, screen size, scale and app in front.
- **UI tree** (`/ui`): text, content descriptions, resource ids, classes, flags (clickable, scrollable, editable, focused, checked) and real-pixel bounds and centres of up to 300 elements. **Side effect:** it runs `uiautomator dump`, which unbinds every accessibility service on the phone for a moment. Nothing else calls it.
- **State**: app in front, whether it is allowed, screen awake, battery, screen size, allowlist, client version.

### Touch
- **Tap** any point on the screen.
- **Swipe, scroll and drag** with one finger, lasting 50-5000 ms.
- **Long press**: a swipe with the same start and end point and `ms` of 500 or more.
- Taps can land on the on-screen keyboard (it never takes focus). The one exception is when a password field is focused; see the restrictions below.

### Type
- 1-500 printable ASCII characters per call, `%` and `%s` included. Send several calls for longer text.
- With or without a text field focused. The response gives the focused field's `inputType` (or `null`), so the agent can check what it typed into.

### Keys
- `back`, `home`, `recents`, `enter`, `tab`, `delete`.
- Any other Android key by name, e.g. `KEYCODE_PAGE_DOWN`, `KEYCODE_ESCAPE`, `KEYCODE_DPAD_UP`, `KEYCODE_MOVE_END`, letter keys. The denied keys are listed below.

### Apps
- **Launch** Gmail or Chrome, from either of them or from the home screen.
- **In Chrome**, anything a person can do by touch and typing:
  - open any URL or search;
  - use sites where the phone is already signed in;
  - download files;
  - pick suggestions from Chrome's own autofill dropdown in the page. That includes saved passwords, because the dropdown is part of Chrome's window, not the keyboard.
- **In Gmail**, anything a person can do by touch and typing: read, search, compose, reply, forward, send, archive, delete, open attachments, and switch between the accounts on the phone.

## Actively restricted

| # | Restriction | Where | Why |
|---|---|---|---|
| 1 | Every action, screenshot and UI dump only while Gmail or Chrome is in front. Checked before the action and again inside the same shell command on the phone. Screenshots and UI dumps are checked again afterwards and thrown away if the app changed | Phone | Keeps the agent out of every other app, Settings, the notification shade and the lock screen |
| 2 | Home, Back, every key and `/launch` are refused while another app (or the lock screen) is in front. `/launch` also works from the home screen | Phone | Never press anything over another app, e.g. the Teams widget's automation |
| 3 | Nothing works while the screen is off. The lock screen is refused by rule 1 | Phone | The agent cannot unlock the phone |
| 4 | `/type` is refused in password fields (`inputType` text, visible, web or number password). Taps and swipes are refused while the keyboard is open on a password field, so passwords cannot be typed key by key; Back closes the keyboard | Phone | From the plan: the agent never enters credentials. Does **not** cover Chrome's autofill dropdown, or 2FA code fields that are plain number fields |
| 5 | Denied keys: power, sleep, wake-up, call, end call, camera, assistant, volume and mute, brightness, media playback, headset hook, SysRq | Phone + Worker | They act on the phone outside the app in front |
| 6 | `/ui` drops elements from other packages (status bar, keyboard, overlays) and blanks password text | Phone | Same boundary as rule 1 |
| 7 | `/launch` takes only `com.google.android.gm` and `com.android.chrome` | Phone + Worker | The allowlist |
| 8 | Input limits: integer coordinates up to 10000, within the larger screen dimension; scale 0.1-1; swipe 50-5000 ms; text 1-500 printable ASCII (no Unicode, newlines or tabs); request body 4 KB; unknown JSON fields rejected | Phone + Worker | Sanity limits. The ASCII limit comes from `adb input text` itself |
| 9 | Rate limits: 300 requests and 120 screenshots per minute (`RATE_ACTIONS_PER_MIN`, `RATE_SCREENSHOTS_PER_MIN`) | Worker | Catches runaway loops |
| 10 | Timeouts: 30 s per command, 60 s for `/type` | Worker | A stuck phone does not hang the agent |
| 11 | Tokens: the agent token cannot use the admin or device endpoints | Worker | The agent cannot switch the relay back on, read the log or pose as the phone |
| 12 | Owner kill switches: `/admin/disable`, and the stop file next to the client | Worker + client | Your off switch |
| 13 | One phone, one command at a time | Worker + client | adb actions must not interleave |

Typed text is never logged, only its length. That limits what is recorded, not what the agent can do.

## Not built (nothing prevents it)

| Capability | How it could be added |
|---|---|
| Multi-touch: pinch-zoom, two-finger gestures | Not possible with `adb input`. Needs the native app (phase 5) |
| Unicode, emoji, newlines in text | Through the clipboard and paste, or an ADB keyboard app |
| Clipboard read and write | A helper app; Android limits clipboard access from the shell |
| Open a URL directly, without typing it | `am start -a android.intent.action.VIEW -d <url> com.android.chrome`; easy |
| Compose an email from a `mailto:` link | The same kind of intent for Gmail; easy |
| Chrome page DOM and JavaScript | Chrome DevTools over `adb forward ... localabstract:chrome_devtools_remote`; powerful, would let the agent read and script pages directly |
| UI tree without unbinding accessibility services | A small `app_process` helper using UiAutomation with `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` |
| Wait for the screen to change; batches or macros of actions | Server-side loop or a batch endpoint |
| Screen recording, audio | `screenrecord`; not exposed |
| Files: push attachments in, pull downloads out | `adb push`/`pull` endpoints |
| Notifications, other apps, Settings, rotation | Blocked in practice by rule 1 and the denied keys; would need those rules changed, not just new code |
| MCP wrapper, status page, native app, custom domain | Phases 5-6 of the plan |
