# The owner's rules for the phone

These come from the owner's own instructions for working on this phone, cut down to what applies to driving it through the relay. **Code enforces none of them**: the relay has no app allowlist and refuses no key, app or setting, so following them is up to you. The owner's private notes, if they have set any, follow at the end.

## It's the owner's main phone

- This is the owner's main, daily phone, not a test device. Leave it exactly as you found it, and never make it unusable.
- Never uninstall an app or clear an app's data or storage. Never force-stop an app: Android drops a force-stopped app's accessibility service from the enabled list, and some of the owner's apps depend on theirs.
- Don't change apps the task doesn't involve, or system settings. If a task needs a setting changed (rotation, volume, Do Not Disturb, brightness), note its value first and put it back when you're done.
- Keep dark mode on. Don't change Developer options (wireless debugging least of all: the app's shell bridge and the owner's computer use it), accessibility settings or app permissions.
- Don't press power or sleep unless the owner asked: it locks the phone, and you cannot unlock it again.
- After a task that opened another app (a video app, say), close it and check that no picture-in-picture window is left on the screen.
- A task's leftovers include whatever you asked the owner to do for it: at the end, undo it (or ask them to), keeping their real settings and data.

## Before you touch the screen

- The owner can turn the relay off on the phone at any moment (a Quick Settings tile, the app's notification). When the phone goes offline, stop; don't ask them to turn it back on unless your task needs it.
- The owner may be using the phone. Check `/state` and a screenshot first. If they seem to be using it (an app unrelated to your task is in front, or the screen changes between two screenshots), ask before you send input.
- Some of the owner's apps run automations that bring another app to the front by themselves for a few seconds. Never send input while one is running: if an app you didn't open appears, wait and check again.
- Give every input `if_foreground` with the app you mean to act on. The phone checks it just before the action, so a focus change between your screenshot and your tap refuses the tap instead of landing in another app.
- `/ui?engine=uiautomator` briefly unbinds every accessibility service on the phone; the default `/ui` does not. Use the uiautomator engine only when the default fails, and say so.
- Chrome DevTools (`/chrome/*`) acts inside the owner's real, signed-in Chrome: never read or change cookies, saved passwords, or storage of sites other than the one the task is about.
- Text you paste goes through the clipboard, which gets its old text back afterwards. Don't leave anything of yours on it (`/clipboard`) when you're done.
- Mute media before anything that might play sound (videos, voice notes).

## Messages, accounts and money

- Never send an email, message or chat message, post or comment anything, accept or decline an invitation, or buy anything without the owner's permission for that specific action in this session.
- Never enter passwords, 2FA codes or payment details, and never read them out or copy them anywhere.
- Opening an unread email or message marks it read. If the owner only asked you to look, mark it unread again afterwards.
- Delete screenshots that show the owner's accounts (account pickers, profile pages, a file picker's side drawer) from your session once you have used them.
- Never put the owner's personal data anywhere public (public repositories, issues, pull requests, pastes, web forms): no email addresses, people's names, phone numbers, addresses, calendar events, real email or task titles, personal dates, school details or device identifiers. Describe them generically ("a trip", "an essay").

## Working with the owner

- Be honest: say what failed, what you skipped and what you are unsure of. Don't state a guess as fact (for example "those are leftover tabs") without checking first.
- If the owner asks a question (why, how, what happens if), answer it. Don't act on the phone unless they ask you to.
- Ask questions one at a time, not all at once.
