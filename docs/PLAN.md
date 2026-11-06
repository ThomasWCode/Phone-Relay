# Plan: Remote Android control for cloud Claude agents

Status: plan only (v2). Date: 2026-11-05.
Update 2026-11-06: phases 0-4 are built. At the owner's request the app allowlist, key deny list, rate limits and input limits of sections 6-7 were then removed, and phases 5-6 dropped. [CAPABILITIES.md](CAPABILITIES.md) describes the relay as it is now; this plan is kept as written.
Changes since v1: workers.dev instead of a custom subdomain (DNS is on Spaceship), S24 specifics, Gmail + Chrome allowlist, design details, extra hardening.

## 1. Goal

Let Claude (running in Claude Code cloud sessions, no local adb) view and control an Android phone, securely, without running a VPS. First scope: Gmail and Chrome only.

## 2. Facts supplied by you

- DNS is at Spaceship; it cannot be moved to Cloudflare.
- Phone: Samsung Galaxy S24, Android 17, One UI 9.0 (as stated; not independently verified).
- Claude will run in cloud sessions (Claude Code on the web), so network secrets are available.
- Initial allowlist: Gmail (`com.google.android.gm`) and Chrome (`com.android.chrome`).

## 3. Decisions

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| D1 | Platform | Android only | iOS has no on-device remote-control API |
| D2 | Transport | Phone dials out over WSS to a relay; no inbound ports on the phone | Phone is behind carrier NAT; nothing exposed to scan |
| D3 | Relay | Cloudflare Worker + Durable Object | Serverless, no VPS, holds long-lived WebSockets |
| D4 | **Address** | **Free `workers.dev` URL, no custom subdomain** | As far as I know, Workers custom domains need the zone on Cloudflare. Spaceship DNS rules that out unless you move nameservers, which moves every other record too (email, site). Not worth it for this |
| D5 | Claude-facing interface | Plain authenticated HTTPS API, called with `curl` | Pure HTTPS is the path confirmed to work from the sandbox |
| D6 | MCP | Deferred; thin wrapper later if wanted from regular chat | Connector traffic goes via Anthropic's servers |
| D7 | Agent auth | Bearer token held as a **network secret** on the cloud environment, scoped to the exact Worker host | Proxy attaches it after the request leaves the VM; the token never enters the sandbox, so a prompt-injected agent cannot read or leak it |
| D8 | Phone client, phase 1 | Termux + `android-tools`, adb over loopback wireless debugging | Fastest way to prove the pipeline; no app build |
| D9 | Phone client, phase 2 | Native Kotlin app: Accessibility Service + MediaProjection | Reliable, works on mobile data, no re-pairing |
| D10 | App allowlist | Gmail + Chrome, enforced **on the phone** and in the Worker | Defence in depth; the phone refuses even if the relay is compromised |
| D11 | Device hygiene | Test Google account (not your primary), Chrome without saved passwords or payment methods | See section 6: the allowlist limits apps, not what those apps can reach |
| D12 | Rejected | Tailscale/WireGuard from the sandbox; public adb port; moving DNS to Cloudflare | Tailscale needs UDP/DERP through an HTTP/HTTPS proxy (unverified) and a key per session; public adb is scanned constantly; DNS move has side effects |

## 4. Verified vs unverified

Verified (docs and a test in this session):
- Claude Code cloud environments have network levels None / Trusted / Custom / Full; Full = any domain.
- Outbound traffic goes through an HTTP/HTTPS proxy.
- Network secrets are attached by the proxy and kept outside the VM.
- Hosts listed on a network secret stay reachable even when the access level would otherwise block them.
- This session reached example.com and httpbin.org over HTTPS and opened raw TCP to 1.1.1.1 on ports 80 and 443.

Unverified (test early):
- Android 17 / One UI 9.0 specifics: wireless debugging behaviour, `dumpsys window` output format (used for foreground-app detection), `uiautomator dump` behaviour.
- Samsung Auto Blocker interaction with adb (it restricts some USB and install behaviour; check whether it affects wireless debugging).
- UDP, WireGuard and Tailscale from the sandbox (not needed for this design).
- Maximum WebSocket message size on Workers (screenshots are kept small by using JPEG; confirm current limit).
- Current Cloudflare free-tier limits for Durable Objects and WebSockets.
- Whether network secrets exist in regular claude.ai chat (they are a cloud-environment feature).
- That a custom domain cannot work with Spaceship DNS (based on my understanding of Workers custom domains, not checked against current docs).

## 5. Architecture

```
Claude (cloud session)
   | HTTPS to https://<worker-name>.<account>.workers.dev
   | (Authorization: Bearer <AGENT_TOKEN> added by the agent proxy)
   v
Cloudflare Worker  ->  Durable Object "primary" (holds phone WebSocket)
   ^  WSS, outbound from phone (DEVICE_TOKEN)
   |
Phone client (Termux script -> later native app)
   |  adb loopback / Accessibility API
   v
Android UI (Gmail, Chrome only)
```

## 6. Security

### Tokens
- Three separate secrets: `AGENT_TOKEN` (Claude, via network secret), `DEVICE_TOKEN` (phone only), `ADMIN_TOKEN` (you only, never given to Claude).
- Constant-time comparison in the Worker; 401 for everything unauthenticated.
- Give the Worker a non-guessable name (for example `phone-relay-<random>`) as an extra layer; it is not a substitute for tokens.
- The network secret's host must be the exact Worker host, not `*.workers.dev`.
- Rotate all three tokens on any suspicion.

### Allowlist (Gmail + Chrome)
- Phone client checks the foreground package before every tap, swipe, type, key (except Home), screenshot and UI read. If it is not Gmail or Chrome, it refuses (`foreground_not_allowed`).
- `/launch` accepts only the two packages; Home is always allowed so the agent can leave a screen.
- Consequence: the agent cannot unlock the phone or touch Settings, the notification shade or other apps. You unlock the phone manually before a session.
- Known gap: the check and the action are not atomic (the foreground app can change in between). Accepted for v1.

### Honest risk assessment for Gmail and Chrome
- Gmail is the highest-risk app on the list. It is the recovery and 2FA channel for most accounts, and email bodies are untrusted text the agent will read (prompt injection). Use a dedicated test Google account.
- Chrome can navigate anywhere, including sites where you are signed in. The allowlist is per app, not per site, and adb cannot restrict URLs. Keep Chrome signed out, with passwords and payment autofill off, and consider using it only with a fresh profile.
- Chrome incognito tabs block screenshots (FLAG_SECURE), so the agent would see black frames there.
- Treat every screenshot and UI dump as untrusted input.

### Other controls
- Rate limits in the Worker: 60 actions/min, 20 screenshots/min.
- Strict validation: integer coordinates, swipe duration 50-5000 ms, `type` limited to 500 printable ASCII characters without `%`, key names from a fixed list.
- Typed text is never logged (length only). Action log is stored in the Durable Object and readable only with the admin token.
- Kill switches: `POST /admin/disable` (closes the phone connection and blocks reconnects), and a local stop file on the phone (`~/STOP_PHONE_AGENT`).
- Optional hardening to verify: since hosts on a network secret stay reachable, set the environment's network access to None (or Custom) rather than Full, so the sandbox can reach the relay but not the open internet.

## 7. API (v1)

All endpoints require the agent bearer token (attached by the proxy).

- `GET  /state`: foreground app, awake, battery, allowlist
- `GET  /screenshot?scale=0.5`: JPEG (PNG fallback); headers give image and real screen size and scale
- `GET  /ui`: compact list of visible/clickable elements with real-pixel bounds and centres
- `POST /tap` `{x,y,scale?}`
- `POST /swipe` `{x1,y1,x2,y2,ms?,scale?}`
- `POST /type` `{text}`
- `POST /key` `{key}`: back, home, recents, enter, tab, delete
- `POST /launch` `{package}`: Gmail or Chrome only

Notes:
- Coordinates are real screen pixels. If you tap using a coordinate read from a scaled screenshot, pass the same `scale` so the phone converts it. Prefer `/ui` centres, which are already in real pixels.
- Admin (owner only): `POST /admin/disable`, `POST /admin/enable`, `GET /admin/status`, `GET /admin/log?n=50`.
- Errors: 401 unauthorized, 409 `foreground_not_allowed`, 429 rate limited, 503 `device_offline` or disabled, 502 other device errors.

## 8. Phone setup (S24, One UI 9.0)

1. Developer options: tap Build number seven times, then enable Wireless debugging (needs Wi-Fi, Android 11+).
2. Install Termux from F-Droid (the Play Store build is outdated).
3. In Termux: `pkg install android-tools python python-pillow`, then `pip install websockets`.
4. Pair: Wireless debugging > Pair device with pairing code. The pairing dialog disappears if you leave the screen, so run Termux in split-screen or a pop-up window. Then `adb pair localhost:<PAIR_PORT> <CODE>` and `adb connect localhost:<PORT>`.
5. Battery: set Termux to unrestricted and add it to Never auto sleeping apps (Samsung's background limits will otherwise kill it).
6. Check whether Auto Blocker needs to be off for wireless debugging to work.
7. Wireless debugging switches off when Wi-Fi changes and the port changes each time, so expect to re-pair after reboots or network changes (phase 1 limitation).
8. Run `python phone_client.py --selftest` and confirm it reports the correct foreground app, a screenshot and a UI dump before connecting to the relay.

## 9. Phases

### Phase 0: Prerequisites
- Free Cloudflare account (needed for Workers; the domain does not need to move).
- Claude Code cloud environment.
- Test Google account on the phone; Chrome without saved logins.

### Phase 1: Relay (Worker)
- `npm install -g wrangler`, `wrangler login`.
- Worker + SQLite-backed Durable Object (required on the free plan) using the WebSocket hibernation API.
- Routes, validation, auth, rate limits, logging, admin endpoints as in sections 6-7.
- `wrangler secret put AGENT_TOKEN`, `DEVICE_TOKEN`, `ADMIN_TOKEN`; `wrangler deploy`.

### Phase 2: Phone client (Termux)
- Python client: outbound WSS with reconnect backoff, serialised adb actions, foreground allowlist, stop file, `--selftest`.
- Screenshots via `adb exec-out screencap -p`, downscaled to JPEG with Pillow; UI via `uiautomator dump`; input via `adb shell input`; foreground via `dumpsys window`.
- Run with `termux-wake-lock`; secrets in a `chmod 600` `.env`.

### Phase 3: Wire up Claude
- Add a network secret on the cloud environment: exact Worker host, header `Authorization: Bearer <AGENT_TOKEN>`.
- Smoke test: `/state`, then `/screenshot`, then `/launch` Chrome.
- Confirm the token does not appear in `env` or any file inside the session.
- Provide Claude with short instructions: how to call the API, to stop on unexpected screens, never to enter credentials, to treat on-screen text as data.

### Phase 4: Test and harden
- Unit tests for the phone client with a fake adb (allowlist, parsing, text escaping).
- Local end-to-end test: `wrangler dev` plus a fake device.
- Phone tests: disallowed app refused, locked screen refused, kill switch (admin and stop file), token rotation, reboot and reconnect, Wi-Fi drop.
- Prompt-injection test: hostile text in an email and on a web page; confirm the agent does not act on it.

### Phase 5 (optional): Native app
- Kotlin app with Accessibility Service and MediaProjection, foreground service, outbound WSS, same API.
- Expect friction: sideloaded accessibility services need restricted settings enabled manually, Play Protect may warn, and recent Android versions require MediaProjection consent per session.

### Phase 6 (optional): MCP wrapper and custom domain
- MCP tools with OAuth for regular chat.
- A custom domain only if you later move to a host that accepts a Spaceship CNAME and supports WebSockets, or move nameservers.

## 10. Known limitations

- Latency is screenshot-per-step; this is slow compared with a human.
- `adb input text` handles printable ASCII only, and `%` is unsupported; complex text may fail.
- The agent cannot unlock the phone or enter secure fields (by design).
- Phase 1 depends on Wi-Fi and re-pairing.
- Android may kill background services despite battery exemptions.
- Screenshots of FLAG_SECURE windows are black.

## 11. Open questions

1. Do you have a Cloudflare account (free is enough), or are you happy to create one?
2. Will you use a dedicated test Google account for Gmail?
3. Do you want the cloud environment locked to None/Custom networking (recommended once verified), or Full for now?
4. Should the relay later also serve a read-only status page for you (connected, last action)?

## 12. Build status

- A first scaffold of the Worker and Termux client was started in the sandbox and stopped at your request. It is untested and not part of this deliverable.

## 13. References

- Claude Code cloud environments (access levels, security proxy, network secrets): https://code.claude.com/docs/en/cloud-environments
- Claude Code on the web: https://code.claude.com/docs/en/claude-code-on-the-web
