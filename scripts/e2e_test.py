#!/usr/bin/env python3
"""Local end-to-end test: `wrangler dev` + phone_client.py talking to a fake adb.

Exercises the real Worker, Durable Object and phone client together, with no
Cloudflare account and no phone. Run from the repository root after
`cd relay && npm ci`:

    python scripts/e2e_test.py
"""

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
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")
PHONE = os.path.join(ROOT, "phone")
PORT = int(os.environ.get("E2E_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"

AGENT = secrets.token_hex(32)
DEVICE = secrets.token_hex(32)
ADMIN = secrets.token_hex(32)

failures = []


def request(method, path, token=AGENT, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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
        # a low screenshot limit so the rate-limit check below hits it (default is 120/min)
        f.write(f"AGENT_TOKEN={AGENT}\nDEVICE_TOKEN={DEVICE}\nADMIN_TOKEN={ADMIN}\nRATE_SCREENSHOTS_PER_MIN=20\n")
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

        # ---- offline ----
        s, _, b = request("GET", "/state")
        check("device offline -> 503", s == 503 and j(b).get("error") == "device_offline", f"{s} {b[:200]}")
        check("validation runs before device", request("POST", "/launch", body={"package": "com.android.settings"})[0] == 400)

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
        check("GET /state", s == 200 and st.get("foreground") == "com.android.chrome" and st.get("battery") == 87, f"{s} {b[:200]}")

        s, h, b = request("GET", "/screenshot?scale=0.5")
        check("GET /screenshot is JPEG", s == 200 and h.get("Content-Type") == "image/jpeg" and b[:2] == b"\xff\xd8", f"{s} {h}")
        check("screenshot headers", h.get("X-Image-Width") == "540" and h.get("X-Screen-Width") == "1080" and h.get("X-Scale") == "0.5", str(h))

        s, _, b = request("GET", "/ui")
        check("GET /ui", s == 200 and j(b).get("count") == 3, f"{s} {b[:200]}")

        s, _, b = request("POST", "/tap", body={"x": 270, "y": 585, "scale": 0.5})
        check("POST /tap scaled", s == 200 and j(b).get("x") == 540, f"{s} {b[:200]}")
        check("POST /swipe", request("POST", "/swipe", body={"x1": 500, "y1": 1800, "x2": 500, "y2": 600})[0] == 200)
        check("POST /type", request("POST", "/type", body={"text": "hello world"})[0] == 200)
        check("type with % and %s", request("POST", "/type", body={"text": "100% and %s"})[0] == 200)
        s, _, b = request("POST", "/type", body={"text": "café"})
        check("type non-ASCII -> 400", s == 400 and j(b).get("error") == "invalid_args")
        check("KEYCODE_* key", request("POST", "/key", body={"key": "KEYCODE_PAGE_DOWN"})[0] == 200)
        check("power key -> 400", request("POST", "/key", body={"key": "KEYCODE_POWER"})[0] == 400)

        set_fake(state_path, password_focused=True)
        s, _, b = request("POST", "/type", body={"text": "hunter2"})
        check("type into password field -> 409", s == 409 and j(b).get("error") == "password_field", f"{s} {b[:200]}")
        s, _, b = request("POST", "/tap", body={"x": 100, "y": 2000})
        check("keyboard tap on password field -> 409", s == 409 and j(b).get("error") == "password_field", f"{s} {b[:200]}")
        set_fake(state_path, password_focused=False, text_field=False)
        check("type without a text field is allowed", request("POST", "/type", body={"text": "hi"})[0] == 200)
        set_fake(state_path, text_field=True, popup=True)
        check("tap in an app's popup menu", request("POST", "/tap", body={"x": 10, "y": 10})[0] == 200)
        set_fake(state_path, popup=False)

        # ---- allowlist ----
        set_fake(state_path, foreground="com.android.settings")
        s, _, b = request("POST", "/tap", body={"x": 10, "y": 10})
        check("tap in Settings -> 409", s == 409 and j(b).get("error") == "foreground_not_allowed", f"{s} {b[:200]}")
        check("screenshot in Settings -> 409", request("GET", "/screenshot")[0] == 409)
        check("ui in Settings -> 409", request("GET", "/ui")[0] == 409)
        check("back in Settings -> 409", request("POST", "/key", body={"key": "back"})[0] == 409)
        check("home in Settings -> 409", request("POST", "/key", body={"key": "home"})[0] == 409)
        check("launch over Settings -> 409", request("POST", "/launch", body={"package": "com.android.chrome"})[0] == 409)
        set_fake(state_path, foreground="com.microsoft.teams")
        check("home over Teams -> 409", request("POST", "/key", body={"key": "home"})[0] == 409)
        set_fake(state_path, foreground="com.android.chrome")
        check("home from Chrome", request("POST", "/key", body={"key": "home"})[0] == 200)
        s, _, b = request("POST", "/launch", body={"package": "com.google.android.gm"})
        check("launch Gmail from the home screen", s == 200, f"{s} {b[:200]}")
        check("Gmail now foreground", j(request("GET", "/state")[2]).get("foreground") == "com.google.android.gm")
        set_fake(state_path, foreground="NotificationShade")
        check("lock screen refused", request("POST", "/tap", body={"x": 10, "y": 10})[0] == 409)
        set_fake(state_path, foreground="com.android.chrome", awake=False)
        s, _, b = request("POST", "/tap", body={"x": 10, "y": 10})
        check("screen off refused", s == 409 and j(b).get("error") == "screen_off", f"{s} {b[:200]}")
        set_fake(state_path, awake=True)

        # ---- log ----
        s, _, b = request("GET", "/admin/log?n=100", token=ADMIN)
        entries = j(b).get("entries", [])
        check("admin log readable", s == 200 and len(entries) > 10, f"{s}")
        check("typed text not logged", b"hello world" not in b and b"hunter2" not in b)
        check("type logged as length", any(e.get("action") == "type" and e.get("detail") == {"length": 11} for e in entries))

        # ---- rate limit (screenshots: 20/min; 1 used above, plus 1 refused) ----
        statuses = [request("GET", "/screenshot?scale=0.1")[0] for _ in range(20)]
        check("screenshot rate limit -> 429", statuses.count(429) >= 1 and statuses[0] == 200, str(statuses))

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
