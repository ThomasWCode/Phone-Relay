#!/usr/bin/env python3
"""Fake `adb` for tests. Emulates the handful of commands phone_client.py uses.

State is a JSON file named by FAKE_ADB_STATE:
    {"foreground": "com.android.chrome", "awake": true, "password_focused": false}
Every invocation is appended to the file named by FAKE_ADB_LOG (one JSON list per line).
"""

import json
import os
import struct
import sys
import zlib

WIDTH, HEIGHT = 1080, 2340
LAUNCHER = "com.sec.android.app.launcher"
COMPONENTS = {
    "com.android.chrome": "com.android.chrome/com.google.android.apps.chrome.Main",
    "com.google.android.gm": "com.google.android.gm/.ConversationListActivityGmail",
}


def load_state():
    path = os.environ.get("FAKE_ADB_STATE")
    state = {"foreground": "com.android.chrome", "awake": True, "password_focused": False}
    if path and os.path.exists(path):
        with open(path) as f:
            state.update(json.load(f))
    return state


def save_state(state):
    path = os.environ.get("FAKE_ADB_STATE")
    if path:
        with open(path, "w") as f:
            json.dump(state, f)


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + b"\x30\x60\x90" * width
    raw = zlib.compress(row * height, 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def ui_xml(state):
    pkg = state["foreground"]
    pw = "true" if state.get("password_focused") else "false"
    return f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.android.systemui" content-desc="" clickable="false" focused="false" password="false" scrollable="false" bounds="[0,0][1080,80]">
  <node index="0" text="12:00" resource-id="com.android.systemui:id/clock" class="android.widget.TextView" package="com.android.systemui" content-desc="" clickable="false" focused="false" password="false" scrollable="false" bounds="[40,10][160,70]" />
</node>
<node index="1" text="" resource-id="" class="android.widget.FrameLayout" package="{pkg}" content-desc="" clickable="false" focused="false" password="false" scrollable="false" bounds="[0,80][1080,2340]">
  <node index="0" text="" resource-id="{pkg}:id/url_bar" class="android.widget.EditText" package="{pkg}" content-desc="Search or type URL" clickable="true" focused="true" password="{pw}" scrollable="false" bounds="[100,120][900,220]" />
  <node index="1" text="Hello &amp; welcome" resource-id="" class="android.widget.TextView" package="{pkg}" content-desc="" clickable="false" focused="false" password="false" scrollable="false" bounds="[50,400][1030,500]" />
  <node index="2" text="" resource-id="" class="android.view.View" package="{pkg}" content-desc="" clickable="false" focused="false" password="false" scrollable="false" bounds="[0,600][1080,700]" />
  <node index="3" text="" resource-id="" class="android.widget.ScrollView" package="{pkg}" content-desc="" clickable="false" focused="false" password="false" scrollable="true" bounds="[0,800][1080,2300]" />
</node>
</hierarchy>"""


def shell(cmd, state):
    out = ""
    if "dumpsys window" in cmd:
        fg = state["foreground"]
        out += f"  mCurrentFocus=Window{{1a2b3c u0 {fg}/{fg}.Main}}\n" if fg else "  mCurrentFocus=null\n"
    if "dumpsys power" in cmd:
        out += "  mWakefulness=" + ("Awake" if state.get("awake", True) else "Asleep") + "\n"
    if cmd.startswith("dumpsys battery"):
        out += "Current Battery Service state:\n  AC powered: false\n  level: 87\n  scale: 100\n"
    if cmd == "wm size":
        out += f"Physical size: {WIDTH}x{HEIGHT}\n"
    if cmd.startswith("cmd package resolve-activity"):
        pkg = cmd.split()[-1].strip("'")
        if pkg in COMPONENTS:
            out += f"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n{COMPONENTS[pkg]}\n"
        else:
            out += "No activity found\n"
    if cmd.startswith("am start"):
        component = cmd.split()[-1].strip("'")
        state["foreground"] = component.split("/")[0]
        save_state(state)
        out += f"Starting: Intent {{ cmp={component} }}\nStatus: ok\n"
    if cmd.startswith("monkey"):
        state["foreground"] = cmd.split()[2].strip("'")
        save_state(state)
        out += "Events injected: 1\n"
    if cmd == "input keyevent KEYCODE_HOME":
        state["foreground"] = LAUNCHER
        save_state(state)
    return out.encode()


def main(argv):
    args = argv[1:]
    log_path = os.environ.get("FAKE_ADB_LOG")
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps(args) + "\n")
    if args[:1] == ["-s"]:
        args = args[2:]
    if not args:
        return 1
    state = load_state()
    if args[0] == "devices":
        sys.stdout.write("List of devices attached\nlocalhost:5555\tdevice\n")
        return 0
    if args[0] not in ("shell", "exec-out") or len(args) < 2:
        sys.stderr.write("fake adb: unsupported\n")
        return 1
    cmd = " ".join(args[1:])
    if cmd == "screencap -p":
        sys.stdout.buffer.write(png(WIDTH, HEIGHT))
        return 0
    if cmd.startswith("uiautomator dump"):
        sys.stdout.buffer.write(ui_xml(state).encode())
        return 0
    sys.stdout.buffer.write(shell(cmd, state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
