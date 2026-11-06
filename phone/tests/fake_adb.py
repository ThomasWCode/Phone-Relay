#!/usr/bin/env python3
"""Fake `adb` for tests. Emulates the handful of commands phone_client.py uses.

State is a JSON file named by FAKE_ADB_STATE:
    {"foreground": "com.android.chrome", "awake": true, "popup": false,
     "password_focused": false, "text_field": true}
Every invocation is appended to the file named by FAKE_ADB_LOG (one JSON list per line).

Guarded commands (`if <focus guard>; then <action>; else echo PHONE_RELAY_REFUSED; fi`)
are evaluated by applying the guard's own grep patterns to the fake dumpsys output,
so the tests exercise the real regexes. `uiautomator` is refused: the client must
never call it.
"""

import json
import os
import re
import struct
import sys
import zlib

WIDTH, HEIGHT = 1080, 2340
LAUNCHER = "com.sec.android.app.launcher"
REFUSED_MARKER = "PHONE_RELAY_REFUSED"
COMPONENTS = {
    "com.android.chrome": "com.android.chrome/com.google.android.apps.chrome.Main",
    "com.google.android.gm": "com.google.android.gm/.ConversationListActivityGmail",
}
GUARD_RE = re.compile(r"^if (?P<guard>.*); then (?P<action>.*); else echo " + REFUSED_MARKER + r"; fi$", re.S)
GREP_PATTERN_RE = re.compile(r"grep -qE '([^']*)'")


def default_state():
    return {"foreground": "com.android.chrome", "awake": True, "popup": False,
            "password_focused": False, "text_field": True}


def load_state():
    path = os.environ.get("FAKE_ADB_STATE")
    state = default_state()
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


def window_lines(state):
    fg = state["foreground"]
    if not fg:
        return "  mCurrentFocus=null\n"
    if state.get("popup"):
        return ("  mCurrentFocus=Window{4d3c2b1 u0 PopupWindow:9a8b7c6}\n"
                f"  mFocusedApp=ActivityRecord{{1f2e3d4 u0 {fg}/.Main t412}}\n")
    focus = f"  mCurrentFocus=Window{{1a2b3c u0 {fg}/{fg}.Main}}\n"
    if "/" in fg or "." not in fg:  # a window without a package, e.g. NotificationShade
        focus = f"  mCurrentFocus=Window{{1a2b3c u0 {fg}}}\n"
    return focus + f"  mFocusedApp=ActivityRecord{{146776524 u0 {fg}/.Main t593}}\n"


def editor_info(state):
    """The curEditorInfo block of `dumpsys input_method`, as Android 17 prints it."""
    if not state.get("text_field", True):
        input_type = "0x0"
    else:
        input_type = "0x81" if state.get("password_focused") else "0x1"
    return ("      curEditorInfo:\n"
            f"        inputType={input_type} imeOptions=0x6 privateImeOptions=null\n"
            "        actionLabel=null actionId=0\n"
            "        initialSelStart=0 initialSelEnd=0 initialCapsMode=0x0\n"
            "        hintText=null label=null\n"
            f"        packageName={state['foreground']} autofillId=0 fieldId=0 fieldName=null\n"
            "      mCurClient=ClientState{1}\n")


def guard_passes(guard, state):
    """Apply the guard's grep patterns (direct focus || popup && focused app) to the fake output."""
    text = window_lines(state)
    patterns = GREP_PATTERN_RE.findall(guard)
    if len(patterns) != 3:
        raise ValueError(f"unexpected guard shape: {guard}")
    direct, popup, focused_app = (re.search(p, text) is not None for p in patterns)
    return bool(state.get("awake", True)) and (direct or (popup and focused_app))


def run(cmd, state):
    """Output of one `adb shell`/`exec-out` command (bytes). Raises on commands the client must not use."""
    m = GUARD_RE.match(cmd)
    if m:
        if not guard_passes(m.group("guard"), state):
            return (REFUSED_MARKER + "\n").encode()
        return run(m.group("action"), state)
    if "uiautomator" in cmd:
        raise RuntimeError("uiautomator must never be called (it unbinds accessibility services)")
    if cmd == "screencap -p":
        return png(WIDTH, HEIGHT)
    if cmd.startswith("echo "):
        return (cmd[5:] + "\n").encode()
    out = ""
    if "dumpsys window" in cmd:
        out += window_lines(state)
    if "dumpsys power" in cmd:
        out += "  mWakefulness=" + ("Awake" if state.get("awake", True) else "Asleep") + "\n"
    if "dumpsys input_method" in cmd:
        out += editor_info(state)
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
        state["popup"] = False
        save_state(state)
        out += f"Starting: Intent {{ cmp={component} }}\nStatus: ok\n"
    if cmd.startswith("monkey"):
        state["foreground"] = cmd.split()[2].strip("'")
        state["popup"] = False
        save_state(state)
        out += "Events injected: 1\n"
    if cmd == "input keyevent KEYCODE_HOME":
        state["foreground"] = LAUNCHER
        state["popup"] = False
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
    try:
        sys.stdout.buffer.write(run(" ".join(args[1:]), state))
    except RuntimeError as e:
        sys.stderr.write(f"fake adb: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
