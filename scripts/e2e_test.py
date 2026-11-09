#!/usr/bin/env python3
"""Local end-to-end test: `wrangler dev` + phone_client.py talking to a fake adb.

Exercises the real Worker, Durable Object and phone client together, with no
Cloudflare account and no phone. Run from the repository root after
`cd relay && npm ci`:

    python scripts/e2e_test.py
"""

import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")
PHONE = os.path.join(ROOT, "phone")
PORT = int(os.environ.get("E2E_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"

AGENT = secrets.token_hex(32)
DEVICE = secrets.token_hex(32)
ADMIN = secrets.token_hex(32)

failures = []


def request(method, path, token=AGENT, body=None, raw=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else raw
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if not cond and info else ""))
    if not cond:
        failures.append(name)


def j(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def wait_for(fn, timeout=60, interval=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def set_fake(state_path, **state):
    with open(state_path) as f:
        cur = json.load(f)
    cur.update(state)
    with open(state_path, "w") as f:
        json.dump(cur, f)


def main():
    tmp = tempfile.mkdtemp(prefix="phone-relay-e2e-")
    env_file = os.path.join(tmp, "dev.vars")
    with open(env_file, "w") as f:
        f.write(f"AGENT_TOKEN={AGENT}\nDEVICE_TOKEN={DEVICE}\nADMIN_TOKEN={ADMIN}\n")
    state_path = os.path.join(tmp, "fake_state.json")
    with open(state_path, "w") as f:
        json.dump({"foreground": "com.android.chrome", "awake": True, "password_focused": False}, f)
    # phone_client runs a .py ADB_BIN with its own Python, so no shell wrapper is needed (works on Windows)
    fake_adb = os.path.join(PHONE, "tests", "fake_adb.py")
    stop_file = os.path.join(tmp, "STOP_PHONE_AGENT")

    wrangler_log = open(os.path.join(tmp, "wrangler.log"), "w")
    new_group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
    wrangler = subprocess.Popen(
        [shutil.which("npx") or "npx", "wrangler", "dev", "--config", "wrangler.example.jsonc", "--local",
         "--ip", "127.0.0.1", "--port", str(PORT), "--env-file", env_file,
         "--persist-to", os.path.join(tmp, "state"), "--show-interactive-dev-session=false"],
        cwd=RELAY, stdout=wrangler_log, stderr=subprocess.STDOUT, **new_group,
    )
    client = None
    # A fake Chrome DevTools endpoint; the fake adb's `forward` points at it.
    devtools = subprocess.Popen([sys.executable, os.path.join(PHONE, "tests", "fake_devtools.py")],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    set_fake(state_path, devtools_port=int(devtools.stdout.readline()))
    try:
        ok = wait_for(lambda: request("GET", "/state", token=None)[0] == 401, timeout=90)
        check("wrangler dev started", ok, f"see {wrangler_log.name}")
        if not ok:
            return 1

        # ---- auth ----
        check("no token -> 401", request("GET", "/state", token=None)[0] == 401)
        check("wrong token -> 401", request("GET", "/state", token="x" * 64)[0] == 401)
        check("device token cannot call agent API", request("GET", "/state", token=DEVICE)[0] == 401)
        check("agent token cannot call admin API", request("GET", "/admin/status", token=AGENT)[0] == 401)
        check("unknown path without token -> 401", request("GET", "/nope", token=None)[0] == 401)
        check("unknown path with token -> 404", request("GET", "/nope")[0] == 404)
        check("wrong method -> 405", request("POST", "/state", body={})[0] == 405)
        check("device endpoint needs websocket", request("GET", "/device", token=DEVICE)[0] == 426)

        # ---- agent docs served by the Worker ----
        check("docs need the agent token", request("GET", "/", token=None)[0] == 401)
        s, h, b = request("GET", "/")
        text = b.decode(errors="replace")
        check("GET / serves the agent instructions",
              s == 200 and h.get("Content-Type", "").startswith("text/markdown") and "Instructions for Claude" in text,
              f"{s} {h.get('Content-Type')}")
        check("instructions name the real host", f"127.0.0.1:{PORT}" in text and "<WORKER_HOST>" not in text)
        s, _, b = request("GET", "/docs/CAPABILITIES.md")
        check("GET /docs/CAPABILITIES.md", s == 200 and b"Actively restricted" in b, f"{s}")
        s, _, b = request("GET", "/docs/PHONE_RULES.md")
        check("GET /docs/PHONE_RULES.md", s == 200 and b"main, daily phone" in b and b"## The owner's private notes" not in b, f"{s}")
        check("docs are GET only", request("POST", "/", body={})[0] == 405)

        # ---- the owner's private notes, appended to the phone rules ----
        notes = b"- A private note for the agent."
        check("agent token cannot set notes", request("PUT", "/admin/notes", raw=notes)[0] == 401)
        s, _, b = request("PUT", "/admin/notes", token=ADMIN, raw=notes)
        check("admin sets notes", s == 200 and j(b).get("length") == len(notes), f"{s} {b[:200]}")
        s, _, b = request("GET", "/docs/PHONE_RULES.md")
        check("phone rules end with the notes", b.rstrip().endswith(notes) and b"main, daily phone" in b, b[-200:])
        check("admin reads notes", j(request("GET", "/admin/notes", token=ADMIN)[2]).get("notes") == notes.decode())
        check("notes need PUT or GET", request("POST", "/admin/notes", token=ADMIN, raw=notes)[0] == 405)
        request("PUT", "/admin/notes", token=ADMIN, raw=b"")
        check("empty PUT clears notes", notes not in request("GET", "/docs/PHONE_RULES.md")[2])
        request("PUT", "/admin/notes", token=ADMIN, raw=notes)

        # ---- offline ----
        s, _, b = request("GET", "/state")
        check("device offline -> 503", s == 503 and j(b).get("error") == "device_offline", f"{s} {b[:200]}")
        check("validation runs before device", request("POST", "/launch", body={"package": "com.android.chrome;reboot"})[0] == 400)

        # ---- connect the phone client ----
        env = {**os.environ, "RELAY_URL": f"ws://127.0.0.1:{PORT}/device", "DEVICE_TOKEN": DEVICE,
               "ADB_BIN": fake_adb, "FAKE_ADB_STATE": state_path, "FAKE_ADB_LOG": os.path.join(tmp, "adb.log")}
        client_log = open(os.path.join(tmp, "client.log"), "w")
        client = subprocess.Popen([sys.executable, os.path.join(PHONE, "phone_client.py"), "--env", "/nonexistent",
                                   "--stop-file", stop_file], env=env, stdout=client_log, stderr=subprocess.STDOUT)
        ok = wait_for(lambda: j(request("GET", "/admin/status", token=ADMIN)[2]).get("device_online") is True, 30)
        check("phone client connected", ok, f"see {client_log.name}")
        if not ok:
            return 1
        st = j(request("GET", "/admin/status", token=ADMIN)[2])
        check("hello recorded", (st.get("device") or {}).get("client") == "termux-adb", str(st))

        # ---- agent API ----
        s, _, b = request("GET", "/state")
        st = j(b)
        check("GET /state", s == 200 and st.get("foreground") == "com.android.chrome" and st.get("battery") == 87
              and "allowlist" not in st, f"{s} {b[:200]}")

        s, h, b = request("GET", "/screenshot?scale=0.5")
        check("GET /screenshot is JPEG", s == 200 and h.get("Content-Type") == "image/jpeg" and b[:2] == b"\xff\xd8", f"{s} {h}")
        check("screenshot headers", h.get("X-Image-Width") == "540" and h.get("X-Screen-Width") == "1080" and h.get("X-Scale") == "0.5", str(h))

        s, _, b = request("GET", "/ui")
        check("GET /ui through the helper (every package)", s == 200 and j(b).get("count") == 4, f"{s} {b[:200]}")
        s, _, b = request("GET", "/ui?engine=uiautomator")
        check("GET /ui with uiautomator", s == 200 and j(b).get("count") == 4, f"{s} {b[:200]}")

        s, _, b = request("POST", "/tap", body={"x": 270, "y": 585, "scale": 0.5})
        check("POST /tap scaled", s == 200 and j(b).get("x") == 540, f"{s} {b[:200]}")
        check("POST /tap anywhere", request("POST", "/tap", body={"x": 50000, "y": -3.5})[0] == 200)
        check("POST /swipe", request("POST", "/swipe", body={"x1": 500, "y1": 1800, "x2": 500, "y2": 600})[0] == 200)
        check("POST /swipe of 0 ms", request("POST", "/swipe", body={"x1": 5, "y1": 5, "x2": 6, "y2": 6, "ms": 0})[0] == 200)
        check("POST /type", request("POST", "/type", body={"text": "hello world"})[0] == 200)
        check("type with % and %s", request("POST", "/type", body={"text": "100% and %s"})[0] == 200)
        check("type with newlines and tabs", request("POST", "/type", body={"text": "a\nb\tc"})[0] == 200)
        check("type 2000 characters", request("POST", "/type", body={"text": "x" * 2000})[0] == 200)
        s, _, b = request("POST", "/type", body={"text": "café 😀"})
        check("non-ASCII text is pasted", s == 200 and j(b).get("method") == "paste", f"{s} {b[:200]}")
        s, _, b = request("POST", "/type", body={"text": "café", "method": "keys"})
        check("non-ASCII as keys -> 400", s == 400 and j(b).get("error") == "invalid_args")
        check("KEYCODE_* key", request("POST", "/key", body={"key": "KEYCODE_PAGE_DOWN"})[0] == 200)
        check("power key accepted", request("POST", "/key", body={"key": "KEYCODE_POWER"})[0] == 200)
        check("key by number, long press", request("POST", "/key", body={"key": 82, "longpress": True})[0] == 200)
        s, _, b = request("POST", "/tap", body={"x": 1, "y": 1, "scal": 0.5})
        check("unknown fields named in the reply", s == 200 and j(b).get("ignored_fields") == ["scal"], f"{s} {b[:200]}")
        s, h, _ = request("GET", "/screenshot?scale=0.2&nope=1")
        check("unknown screenshot fields named in a header", s == 200 and h.get("X-Ignored-Fields") == "nope", str(h))

        set_fake(state_path, password_focused=True)
        s, _, b = request("POST", "/type", body={"text": "hunter2"})
        check("type into a password field works and says so", s == 200 and j(b).get("password_field") is True, f"{s} {b[:200]}")
        check("keyboard tap on a password field", request("POST", "/tap", body={"x": 100, "y": 2000})[0] == 200)
        set_fake(state_path, password_focused=False, text_field=False)
        check("type without a text field", request("POST", "/type", body={"text": "hi"})[0] == 200)
        set_fake(state_path, text_field=True, popup=True)
        check("if_foreground passes in the app's popup menu",
              request("POST", "/tap", body={"x": 10, "y": 10, "if_foreground": "com.android.chrome"})[0] == 200)
        set_fake(state_path, popup=False)

        # ---- any app ----
        set_fake(state_path, foreground="com.android.settings")
        check("tap in Settings", request("POST", "/tap", body={"x": 10, "y": 10})[0] == 200)
        s, h, _ = request("GET", "/screenshot")
        check("screenshot in Settings", s == 200 and h.get("X-Foreground") == "com.android.settings", f"{s} {h}")
        check("ui in Settings", request("GET", "/ui")[0] == 200)
        check("back in Settings", request("POST", "/key", body={"key": "back"})[0] == 200)
        set_fake(state_path, foreground="com.microsoft.teams")
        check("launch Chrome over Teams", request("POST", "/launch", body={"package": "com.android.chrome"})[0] == 200)
        check("home from Chrome", request("POST", "/key", body={"key": "home"})[0] == 200)
        s, _, b = request("POST", "/launch", body={"package": "com.android.settings"})
        check("launch Settings", s == 200 and j(request("GET", "/state")[2]).get("foreground") == "com.android.settings",
              f"{s} {b[:200]}")
        s, _, b = request("POST", "/launch", body={"package": "com.google.android.gm"})
        check("launch Gmail", s == 200 and j(request("GET", "/state")[2]).get("foreground") == "com.google.android.gm")

        # ---- if_foreground (opt-in guard) ----
        set_fake(state_path, foreground="com.android.settings")
        s, _, b = request("POST", "/tap", body={"x": 10, "y": 10, "if_foreground": ["com.android.chrome"]})
        check("if_foreground refuses another app", s == 409 and j(b).get("error") == "foreground_mismatch", f"{s} {b[:200]}")
        check("if_foreground on a screenshot", request("GET", "/screenshot?if_foreground=com.android.chrome")[0] == 409)
        check("if_foreground on Settings itself",
              request("POST", "/tap", body={"x": 10, "y": 10, "if_foreground": "com.android.settings"})[0] == 200)
        set_fake(state_path, foreground="NotificationShade")
        check("tap on the lock screen", request("POST", "/tap", body={"x": 10, "y": 10})[0] == 200)
        set_fake(state_path, foreground="com.android.chrome", awake=False)
        check("tap with the screen off", request("POST", "/tap", body={"x": 10, "y": 10})[0] == 200)
        s, _, b = request("POST", "/tap", body={"x": 10, "y": 10, "if_foreground": "com.android.chrome"})
        check("if_foreground wants the screen on", s == 409, f"{s} {b[:200]}")
        set_fake(state_path, awake=True)

        # ---- timeouts ----
        set_fake(state_path, hang=5)
        started = time.time()
        s, _, b = request("POST", "/tap", body={"x": 1, "y": 1, "timeout": 1})
        check("a command past its timeout -> 504 timeout", s == 504 and j(b).get("error") == "timeout", f"{s} {b[:200]}")
        check("the timeout stops it promptly", time.time() - started < 5, f"{time.time() - started:.1f} s")
        set_fake(state_path, hang=1)
        s, _, b = request("POST", "/tap", body={"x": 1, "y": 1, "timeout": 120})
        check("a long timeout lets a slow command finish", s == 200, f"{s} {b[:200]}")
        set_fake(state_path, hang=0)
        check("the next command runs", request("GET", "/state")[0] == 200)

        # ---- no rate limit ----
        statuses = [request("GET", "/screenshot?scale=0.1")[0] for _ in range(25)]
        check("no rate limit on a burst", set(statuses) == {200}, str(statuses))
        st = j(request("GET", "/admin/status", token=ADMIN)[2])
        check("status has no rate fields", "rate_limits" not in st and st.get("notes_length") == len(notes), str(st))

        # ---- apps: open, intents, notifications, status bar, rotation ----
        set_fake(state_path, foreground="com.sec.android.app.launcher")
        s, _, b = request("POST", "/open", body={"url": "https://example.com/?q=1&r=2"})
        check("open a URL", s == 200 and j(request("GET", "/state")[2]).get("foreground") == "com.android.chrome", f"{s} {b[:200]}")
        s, _, b = request("POST", "/open", body={"url": "mailto:someone@example.com?subject=Hi"})
        check("open a mailto: link", s == 200 and j(request("GET", "/state")[2]).get("foreground") == "com.google.android.gm",
              f"{s} {b[:200]}")
        s, _, b = request("POST", "/open", body={"url": "nosuchscheme:x"})
        check("open with nothing to handle it -> 422", s == 422 and j(b).get("error") == "launch_failed", f"{s} {b[:200]}")
        s, _, b = request("POST", "/intent", body={"action": "android.intent.action.SEND", "type": "text/plain",
                                                   "package": "com.google.android.gm",
                                                   "extras": {"android.intent.extra.TEXT": "intentsecret"}})
        check("intent", s == 200, f"{s} {b[:200]}")
        s, _, b = request("GET", "/notifications")
        n = j(b)
        check("notifications", s == 200 and n.get("count") == 2 and n["notifications"][0].get("title") == "A sender",
              f"{s} {b[:300]}")
        check("statusbar", request("POST", "/statusbar", body={"action": "notifications"})[0] == 200)
        s, _, b = request("POST", "/rotation", body={"rotation": 1})
        check("rotation returns the old setting", s == 200 and j(b).get("previous") == {"auto": True, "user_rotation": 0},
              f"{s} {b[:200]}")
        check("state shows the rotation",
              j(request("GET", "/state")[2]).get("rotation") == {"auto": False, "user_rotation": 1})
        request("POST", "/rotation", body={"rotation": "auto"})

        # ---- wait and batch ----
        set_fake(state_path, foreground="com.android.chrome")
        s, _, b = request("POST", "/wait", body={"for": "foreground", "package": "com.android.chrome"})
        check("wait for an app already in front", s == 200 and j(b).get("met") is True, f"{s} {b[:200]}")
        s, _, b = request("POST", "/wait", body={"for": "change", "seconds": 1, "interval_ms": 200})
        check("wait for a change that never comes", s == 200 and j(b).get("met") is False, f"{s} {b[:200]}")
        s, _, b = request("POST", "/batch", body={"steps": [
            {"cmd": "tap", "args": {"x": 1, "y": 1}}, {"cmd": "sleep", "args": {"ms": 50}},
            {"cmd": "type", "args": {"text": "batchsecret"}}, {"cmd": "screenshot", "args": {"scale": 0.1}}]})
        res = j(b)
        check("batch", s == 200 and res.get("all_ok") is True and res["results"][3]["result"].get("mime") == "image/jpeg",
              f"{s} {b[:300]}")
        s, _, b = request("POST", "/batch", body={"if_foreground": "com.android.settings",
                                                  "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]})
        check("batch passes if_foreground down", s == 200 and j(b)["results"][0].get("error") == "foreground_mismatch",
              f"{s} {b[:300]}")

        # ---- asking the owner: only the Android app can; the relay keeps the outcome either way ----
        s, _, b = request("GET", "/request")
        check("no request yet", s == 200 and j(b).get("request") is None, f"{s} {b[:200]}")
        s, _, b = request("POST", "/request", body={"reason": "requestsecret", "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]})
        check("the adb client cannot ask", s == 501 and j(b).get("error") == "unsupported", f"{s} {b[:200]}")
        s, _, b = request("GET", "/request")
        last = j(b).get("request") or {}
        check("the relay keeps the last request's outcome",
              s == 200 and last.get("answer") == "failed" and last.get("error") == "unsupported" and "asked_at" in last, f"{s} {b[:300]}")

        # ---- the helper: gestures, clipboard, audio, waiting for text ----
        s, _, b = request("POST", "/gesture", body={"pointers": [[[500, 1000], [300, 800]], [[580, 1080], [780, 1280]]],
                                                    "ms": 300})
        check("two-finger gesture", s == 200 and j(b).get("pointers") == 2, f"{s} {b[:200]}")
        s, _, b = request("POST", "/clipboard", body={"text": "Grüße clipsecret 👋"})
        check("set the clipboard", s == 200, f"{s} {b[:200]}")
        s, _, b = request("GET", "/clipboard")
        check("read the clipboard", s == 200 and j(b).get("text") == "Grüße clipsecret 👋", f"{s} {b[:200]}")
        check("clipboard is GET or POST", request("PUT", "/clipboard", body={})[0] == 405)
        s, h, b = request("GET", "/audio?seconds=1&source=output")
        check("audio as WAV", s == 200 and h.get("Content-Type") == "audio/wav" and b.startswith(b"RIFF"), f"{s} {h}")
        s, _, b = request("POST", "/wait", body={"for": "text", "text": "welcome"})
        check("wait for text on the screen", s == 200 and j(b).get("met") is True, f"{s} {b[:200]}")

        # ---- Chrome DevTools ----
        s, _, b = request("GET", "/chrome/sockets")
        check("DevTools sockets", s == 200 and "chrome_devtools_remote" in j(b).get("sockets", []), f"{s} {b[:200]}")
        s, _, b = request("GET", "/chrome/tabs")
        check("Chrome tabs", s == 200 and j(b).get("count") == 3, f"{s} {b[:200]}")
        s, _, b = request("POST", "/chrome/eval", body={"expression": "evalsecret && 1+1"})
        check("JavaScript in the front tab", s == 200 and j(b).get("value") == 2 and j(b).get("threw") is False,
              f"{s} {b[:200]}")
        s, _, b = request("POST", "/chrome/eval", body={"expression": "document.title", "tab": 1})
        check("JavaScript in another tab", s == 200 and j(b).get("value") == "Other", f"{s} {b[:200]}")
        s, _, b = request("POST", "/chrome/cdp", body={"method": "Page.navigate", "params": {"url": "https://example.net/"}})
        check("a raw DevTools call", s == 200 and j(b).get("result", {}).get("echo") == {"url": "https://example.net/"},
              f"{s} {b[:200]}")
        s, _, b = request("POST", "/chrome/new", body={"url": "https://example.net/"})
        new_id = (j(b).get("tab") or {}).get("id")
        check("open a tab", s == 200 and new_id, f"{s} {b[:200]}")
        check("close it", request("POST", "/chrome/close", body={"tab": new_id})[0] == 200)
        s, _, b = request("POST", "/chrome/eval", body={"expression": "1", "tab": 9})
        check("no such tab -> 404", s == 404 and j(b).get("error") == "not_found", f"{s} {b[:200]}")
        st = json.load(open(state_path))
        check("no adb forward left behind", st.get("forwards") == [], str(st.get("forwards")))

        # ---- files ----
        s, _, b = request("GET", "/files?path=/sdcard/Download")
        check("list a folder", s == 200 and [e["name"] for e in j(b).get("entries", [])] == ["a.txt"], f"{s} {b[:200]}")
        big = os.urandom(3 * 1024 * 1024 + 123)  # 13 chunks: more than the 8-chunk window
        path = urllib.parse.quote("/sdcard/Download/big file.bin")
        s, _, b = request("POST", f"/files/push?path={path}", raw=big, timeout=120)
        check("push 3 MB", s == 200 and j(b).get("bytes") == len(big), f"{s} {b[:200]}")
        s, h, b = request("GET", f"/files/pull?path={path}", timeout=120)
        check("pull it back intact", s == 200 and b == big, f"{s} {len(b)} bytes")
        check("pull headers", h.get("X-File-Size") == str(len(big)) and h.get("X-File-Path") == "/sdcard/Download/big file.bin"
              and h.get("Content-Type") == "application/octet-stream", str(h))
        s, _, b = request("POST", "/files/push?path=/sdcard/Download/empty.txt", raw=b"")
        check("push an empty file", s == 200 and j(b).get("bytes") == 0, f"{s} {b[:200]}")
        s, h, b = request("GET", "/files/pull?path=/sdcard/Download/empty.txt")
        check("pull an empty file", s == 200 and b == b"" and h.get("Content-Type") == "text/plain", f"{s} {h}")
        s, _, b = request("GET", "/files/pull?path=/sdcard/Download/missing.pdf")
        check("pull a missing file -> 404", s == 404 and j(b).get("error") == "not_found", f"{s} {b[:200]}")
        check("pull a folder -> 400", request("GET", "/files/pull?path=/sdcard/Download")[0] == 400)
        check("relative path -> 400", request("GET", "/files?path=sdcard")[0] == 400)

        # ---- a release: install, what is installed, accessibility, logs, settings, an app's own files ----
        with open(os.path.join(PHONE, "tests", "data", "tiny.apk"), "rb") as f:
            tiny = f.read()
        tiny_sha = "db0a3e2dce9ae04bcdd279f2276bcf8300c153b356de6b4d9426264eecb69a6a"
        request("POST", "/files/push?path=/sdcard/Download/tiny.apk", raw=tiny)
        s, _, b = request("GET", "/package?name=com.example.tiny")
        check("what is installed", s == 200 and j(b).get("version_code") == 41 and j(b).get("signing_sha256") == [tiny_sha]
              and j(b).get("last_update_time") == "2026-11-09 06:58:42", f"{s} {b[:300]}")
        s, _, b = request("POST", "/install", body={"path": "/sdcard/Download/tiny.apk", "package": "com.android.chrome"})
        check("install of another package -> 400", s == 400 and j(b).get("error") == "package_mismatch", f"{s} {b[:200]}")
        s, _, b = request("POST", "/install", body={"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny",
                                                    "if_foreground": "com.android.chrome"})
        check("install over the app", s == 200 and j(b).get("version_code") == 42 and j(b).get("version_name") == "1.2.3-test",
              f"{s} {b[:200]}")
        set_fake(state_path, install_error="INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match")
        s, _, b = request("POST", "/install", body={"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"})
        check("Android's install error -> 422", s == 422 and j(b).get("error") == "install_failed"
              and j(b).get("detail", "").startswith("INSTALL_FAILED_UPDATE_INCOMPATIBLE"), f"{s} {b[:200]}")
        set_fake(state_path, install_error=None)
        s, h, b = request("GET", "/package/apk?name=com.example.tiny")
        check("the installed APK", s == 200 and b == tiny and h.get("X-Version-Code") == "42"
              and h.get("X-File-Size") == str(len(tiny)), f"{s} {h}")
        s, _, b = request("GET", "/accessibility")
        check("accessibility services", s == 200 and j(b).get("missing") == [] and len(j(b).get("bound", [])) == 2, f"{s} {b[:300]}")
        s, _, b = request("GET", "/logcat?tag=Decrastination&level=W&since=60&lines=5")
        st = json.load(open(state_path))
        check("logcat", s == 200 and j(b).get("count") == 2 and "-s Decrastination:W" in st.get("last_logcat", ""), f"{s} {b[:300]}")
        s, _, b = request("GET", "/settings?namespace=global&key=stay_on_while_plugged_in")
        check("a setting and dark mode", s == 200 and j(b).get("value") == "0" and j(b).get("night_mode") == "yes", f"{s} {b[:200]}")
        s, h, b = request("GET", "/run-as?package=com.example.tiny&path=files/plan.json")
        check("a debuggable app's file", s == 200 and b == b'{"plan": []}' and h.get("Content-Type") == "application/json", f"{s} {b[:200]}")
        s, _, b = request("GET", "/run-as/files?package=com.example.tiny&path=files")
        check("its folder", s == 200 and [e["name"] for e in j(b).get("entries", [])] == ["plan.json"], f"{s} {b[:200]}")
        s, _, b = request("GET", "/run-as?package=com.android.chrome&path=files/x")
        check("not debuggable -> 403", s == 403 and j(b).get("error") == "not_debuggable", f"{s} {b[:200]}")
        check("run-as outside the data folder -> 400", request("GET", "/run-as?package=com.example.tiny&path=../x")[0] == 400)
        s, _, b = request("POST", "/intent", body={"component": "com.example.tiny/.debug.Command", "extras": {"cmd": "backup-now"},
                                                   "flags": ["FLAG_ACTIVITY_NEW_TASK"], "as_shell": True})
        st = json.load(open(state_path))
        check("intent as the shell, flags by name", s == 200 and st["last_intent"][3:5] == ["-n", "com.example.tiny/.debug.Command"]
              and "268435456" in st["last_intent"], f"{s} {b[:200]} {st.get('last_intent')}")
        check("as_shell without a component -> 400", request("POST", "/intent", body={"action": "a", "as_shell": True})[0] == 400)
        s, _, b = request("GET", "/check?package=com.example.tiny&services=com.example.tiny/.FocusService")
        report = j(b)
        check("check: a crash since the install fails it", s == 200 and report.get("ok") is False
              and report.get("crashes", {}).get("count") == 1 and report.get("package", {}).get("version_code") == 42
              and report.get("files", {}).get("entries") == [{"name": "plan.json", "size": 12, "was": None}], f"{s} {b[:600]}")
        set_fake(state_path, logcat=[], a11y_enabled=["com.b/com.b.S"], a11y_bound=["com.b/com.b.S"])
        s, _, b = request("GET", "/check?package=com.example.tiny")
        check("check: a service gone since the last check", s == 200 and j(b).get("accessibility", {}).get("gone")
              == ["com.example.tiny/com.example.tiny.FocusService"], f"{s} {b[:600]}")
        request("GET", "/check?package=com.example.tiny&accept_gone=true")
        s, _, b = request("GET", "/check?package=com.example.tiny")
        check("check: all well once the loss is accepted", s == 200 and j(b).get("ok") is True, f"{s} {b[:600]}")

        # ---- screen recording ----
        s, h, b = request("GET", "/record?seconds=1")
        check("record one second", s == 200 and h.get("Content-Type") == "video/mp4" and b == b"fake mp4 segment 0",
              f"{s} {h.get('Content-Type')} {b[:60]}")
        check("start a long recording", request("POST", "/record/start", body={"seconds": 400})[0] == 200)
        s, h, b = request("POST", "/record/stop", body={})
        names = zipfile.ZipFile(io.BytesIO(b)).namelist() if s == 200 and h.get("Content-Type") == "application/zip" else []
        check("a recording past 180 s comes back as a zip of segments", len(names) == 3 and h.get("X-Segments") == "3",
              f"{s} {h}")
        s, _, b = request("POST", "/record/stop", body={})
        check("stop with no recording -> 404", s == 404, f"{s} {b[:200]}")

        # ---- log ----
        s, _, b = request("GET", "/admin/log?n=300", token=ADMIN)
        entries = j(b).get("entries", [])
        check("admin log readable", s == 200 and len(entries) > 10, f"{s}")
        check("typed text not logged", b"hello world" not in b and b"hunter2" not in b and b"batchsecret" not in b
              and b"intentsecret" not in b and b"clipsecret" not in b and b"evalsecret" not in b and b"requestsecret" not in b)
        check("type logged as length", any(e.get("action") == "type" and e.get("detail") == {"length": 11} for e in entries))

        # ---- kill switch (admin) ----
        check("admin disable", request("POST", "/admin/disable", token=ADMIN)[0] == 200)
        s, _, b = request("GET", "/state")
        check("disabled -> 503", s == 503 and j(b).get("error") == "disabled", f"{s} {b[:200]}")
        time.sleep(3)
        st = j(request("GET", "/admin/status", token=ADMIN)[2])
        check("phone stays disconnected while disabled", st.get("device_online") is False, str(st))
        check("admin enable", request("POST", "/admin/enable", token=ADMIN)[0] == 200)
        ok = wait_for(lambda: j(request("GET", "/admin/status", token=ADMIN)[2]).get("device_online") is True, 120)
        check("phone reconnects after enable", ok)

        # ---- kill switch (stop file) ----
        open(stop_file, "w").close()
        try:
            client.wait(timeout=15)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        check("stop file makes client exit", exited and client.returncode == 0, f"rc={client.returncode}")
        check("device offline after stop file",
              wait_for(lambda: j(request("GET", "/admin/status", token=ADMIN)[2]).get("device_online") is False, 15))

        # ---- wrong device token ----
        bad = subprocess.run([sys.executable, os.path.join(PHONE, "phone_client.py"), "--env", "/nonexistent",
                              "--stop-file", os.path.join(tmp, "none")],
                             env={**env, "DEVICE_TOKEN": "y" * 64}, capture_output=True, timeout=30)
        check("bad device token -> client exits 2", bad.returncode == 2, bad.stderr.decode()[-300:])
    finally:
        devtools.kill()
        if client and client.poll() is None:
            client.terminate()
            client.wait(timeout=10)
        if os.name == "nt":  # npx -> node -> workerd: kill the whole tree
            subprocess.run(["taskkill", "/PID", str(wrangler.pid), "/T", "/F"], capture_output=True)
            wrangler.wait(timeout=15)
        else:
            try:
                os.killpg(wrangler.pid, signal.SIGTERM)
                wrangler.wait(timeout=15)
            except Exception:
                os.killpg(wrangler.pid, signal.SIGKILL)
        wrangler_log.close()

    print(f"\n{len(failures)} failure(s)" if failures else "\nall end-to-end checks passed")
    print(f"logs: {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
