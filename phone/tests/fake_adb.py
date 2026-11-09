#!/usr/bin/env python3
"""Fake `adb` for tests. Emulates the handful of commands phone_client.py uses.

State is a JSON file named by FAKE_ADB_STATE:
    {"foreground": "com.android.chrome", "awake": true, "popup": false,
     "password_focused": false, "text_field": true, "keyboard": true, "hang": 0,
     "shade": 48, "files": {"/sdcard/Download/a.txt": "<base64>"}, "rotation": [1, 0]}
"hang" makes every invocation sleep that many seconds first (for timeout tests).
"shade" is the screenshot's grey level, so tests can make the screen change.
"files" is the phone's storage (path -> base64 content); its folders are implied by the paths.
"rotation" is [accelerometer_rotation, user_rotation].
"devtools_port" is where `adb forward ... localabstract:chrome_devtools_remote` points
(a FakeDevTools server); "forwards" lists the forwards still open.
The app_process helper is played too: "clipboard" holds its text, "pasted" and "gestures"
collect what it was given, and "helper_error" makes it fail with that message.
"packages" are the installed apps `dumpsys package` and `pm install` know (name -> version_code,
version_name, debuggable, enabled, apk; their APKs are in "files"), and an app's own data is in
"files" under /data/user/0/<package>/, read through `run-as`. "a11y_enabled" and "a11y_bound" are
the accessibility services (`settings get secure enabled_accessibility_services`, `dumpsys
accessibility`), "settings" maps "namespace key" to a value, "night_mode" is `cmd uimode night`'s,
and "logcat" holds the log's lines.
Every invocation is appended to the file named by FAKE_ADB_LOG (one JSON list per line).

Guarded commands (`if <focus guard>; then <action>; else echo PHONE_RELAY_REFUSED; fi`)
are evaluated by applying the guard's own grep patterns to the fake dumpsys output,
so the tests exercise the real regexes.
"""

import base64
import hashlib
import json
import math
import os
import re
import shlex
import struct
import sys
import time
import zlib

WIDTH, HEIGHT = 1080, 2340
LAUNCHER = "com.sec.android.app.launcher"
REFUSED_MARKER = "PHONE_RELAY_REFUSED"
COMPONENTS = {
    "com.android.chrome": "com.android.chrome/com.google.android.apps.chrome.Main",
    "com.google.android.gm": "com.google.android.gm/.ConversationListActivityGmail",
}
RECORD_PREFIX = "/data/local/tmp/phone_relay_rec_"
HELPER_REMOTE = "/data/local/tmp/phone-relay-helper.jar"
HELPER_PREFIX = f"CLASSPATH={HELPER_REMOTE} app_process / com.thomaswcode.phonerelay.Helper "
FAKE_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake audio"
MISSING_MARKER = "PHONE_RELAY_MISSING"
MTIME = 1700000000
SPLIT_MARKER = "PHONE_RELAY_SPLIT"
TINY_APK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tiny.apk")
# tiny.apk: com.example.tiny 1.2.3-test (42), signed with a test key whose certificate's SHA-256 is this
TINY_SHA256 = "db0a3e2dce9ae04bcdd279f2276bcf8300c153b356de6b4d9426264eecb69a6a"
TINY_INSTALLED = "/data/app/~~Zm9v==/com.example.tiny-YmFy==/base.apk"
DATA_ROOT = "/data/user/0/"
FOCUS = "com.example.tiny/com.example.tiny.FocusService"
LOG_LINES = [
    "--------- beginning of main",
    "11-09 07:00:00.000  1234  1234 I Decrastination: started",
    "11-09 07:00:01.000  1234  1234 W Decrastination: slow",
    "11-09 07:00:02.000   999   999 I ActivityManager: Start proc",
    "11-09 07:00:03.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
    "11-09 07:00:03.000  1234  1234 E AndroidRuntime: Process: com.example.tiny, PID: 1234",
]
# Made-up notifications in the shape `dumpsys notification --noredact` prints.
NOTIFICATION_DUMP = """Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0a1b2c3d: pkg=com.google.android.gm user=UserHandle{0} id=4 tag=gig:12345 importance=3 key=0|com.google.android.gm|4|gig:12345|10234: Notification(channel=mail shortcut=null contentView=null vibrate=null sound=null defaults=0x0 flags=0x218 color=0xffdb4437 category=email vis=PRIVATE))
      uid=10234 userId=0
      opPkg=com.google.android.gm
      key=0|com.google.android.gm|4|gig:12345|10234
      notification=
        extras={
          android.title=String (A sender)
          android.text=SpannableString (Meeting moved)
          android.bigText=SpannableString (Meeting moved to Thursday.
Bring the slides (final version).)
          android.subText=String (someone@example.com)
          android.showWhen=Boolean (true)
        }
    NotificationRecord(0x0d4c3b2a: pkg=com.android.chrome user=UserHandle{0} id=1 tag=null importance=2 key=0|com.android.chrome|1|null|10111: Notification(channel=downloads vis=PRIVATE))
      notification=
        extras={
          android.title=String (report.pdf)
          android.text=String (Download complete)
        }
  Snoozed notifications:
    NotificationRecord(0x0a1b2c3d: pkg=com.google.android.gm user=UserHandle{0} id=4 tag=gig:12345 importance=3 key=0|com.google.android.gm|4|gig:12345|10234: Notification(channel=mail))
"""
GUARD_RE = re.compile(r"^if (?P<guard>.*); then (?P<action>.*); else echo " + REFUSED_MARKER + r"; fi$", re.S)
GREP_PATTERN_RE = re.compile(r"grep -qE '([^']*)'")


def default_state():
    with open(TINY_APK, "rb") as f:
        tiny = base64.b64encode(f.read()).decode()
    return {"foreground": "com.android.chrome", "awake": True, "popup": False,
            "password_focused": False, "text_field": True, "keyboard": True, "hang": 0,
            "shade": 48, "files": {"/sdcard/Download/a.txt": base64.b64encode(b"hello file").decode(), TINY_INSTALLED: tiny,
                                   DATA_ROOT + "com.example.tiny/files/plan.json": base64.b64encode(b'{"plan": []}').decode()},
            "rotation": [1, 0],
            "packages": {"com.example.tiny": {"version_code": 41, "version_name": "1.2.2", "debuggable": True,
                                              "enabled": 0, "apk": TINY_INSTALLED},
                         "com.android.chrome": {"version_code": 700000000, "version_name": "140.0", "debuggable": False,
                                                "enabled": 0, "apk": "/data/app/~~Y2hy==/com.android.chrome-Y2hy==/base.apk"}},
            "a11y_enabled": [FOCUS.replace("/com.example.tiny.", "/."), "com.b/com.b.S"],
            "a11y_bound": [FOCUS, "com.b/com.b.S"],
            "settings": {"global stay_on_while_plugged_in": "0"}, "night_mode": "yes",
            "logcat": LOG_LINES}


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


def png(width, height, shade=48):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes([shade, 0x60, 0x90]) * width
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
    shown = "true" if state.get("keyboard", True) else "false"
    return (f"      mInputShown={shown}\n"
            "      curEditorInfo:\n"
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


def folders(files):
    """Every folder implied by the stored files' paths."""
    out = {"/", "/sdcard", "/sdcard/Download", "/data/local/tmp"}
    for path in files:
        parts = path.split("/")
        out.update("/".join(parts[:i]) for i in range(2, len(parts)))
    return out


def stat_line(path, files):
    if path in files:
        return f"regular file|{len(base64.b64decode(files[path]))}|{MTIME}|{os.path.basename(path)}"
    if path in folders(files):
        return f"directory|4096|{MTIME}|{os.path.basename(path) or '/'}"
    return None


def list_command(cmd, state):
    """The client's folder listing: `if [ -d PATH ]; then ... stat each entry ...; else stat PATH; fi`."""
    path = shlex.split(cmd[len("if [ -d "):cmd.index(" ]")])[0]
    files = state.get("files", {})
    if path in folders(files):
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0] for p in list(files) + list(folders(files)) if p.startswith(prefix) and p != prefix}
        return "".join(stat_line(prefix + n, files) + "\n" for n in sorted(names) if n)
    line = stat_line(path, files)
    return (line if line else MISSING_MARKER) + "\n"


def transfer(args, state):
    """`adb pull REMOTE LOCAL` / `adb push LOCAL REMOTE` against the fake storage: (exit code, stderr)."""
    files = state.setdefault("files", {})
    if args[0] == "pull":
        remote, local = args[1], args[2]
        if remote not in files:
            return 1, f"adb: error: failed to stat remote object '{remote}': No such file or directory"
        with open(local, "wb") as f:
            f.write(base64.b64decode(files[remote]))
        return 0, ""
    local, remote = args[1], args[2]
    with open(local, "rb") as f:
        files[remote] = base64.b64encode(f.read()).decode()
    save_state(state)
    return 0, ""


def forward(args, state):
    """`adb forward tcp:0 localabstract:NAME` (prints the port) and `adb forward --remove tcp:PORT`: (code, out)."""
    forwards = state.setdefault("forwards", [])
    if args[1] == "--remove":
        port = int(args[2].split(":")[1])
        if port not in forwards:
            return 1, ""
        forwards.remove(port)
        save_state(state)
        return 0, ""
    port = state.get("devtools_port")
    if port is None:
        return 1, ""
    forwards.append(port)
    save_state(state)
    return 0, f"{port}\n"


def am(cmd, state):
    """`am start` / `am broadcast`, by their -n, -a, -d and -p arguments."""
    words = shlex.split(cmd.removesuffix(" || true"))
    if words[1] == "broadcast":
        state["last_intent"] = words
        return "Broadcasting: Intent { }\nBroadcast completed: result=0\n"
    opt = {words[i]: words[i + 1] for i in range(len(words) - 1) if words[i] in ("-n", "-a", "-d", "-p")}
    state["last_intent"] = words
    if "-n" in opt:
        target = opt["-n"].split("/")[0]
    elif "-p" in opt:
        target = opt["-p"] if opt["-p"] in COMPONENTS else None
    else:
        scheme = opt.get("-d", "").split(":")[0]
        target = {"http": "com.android.chrome", "https": "com.android.chrome", "mailto": "com.google.android.gm"}.get(scheme)
    if target is None:
        return "Starting: Intent { }\nError: Activity not started, unable to resolve Intent { }\n"
    state["foreground"] = target
    state["popup"] = False
    save_state(state)
    return f"Starting: Intent {{ cmp={target}/.Main }}\nStatus: ok\n"


def package_dump(name, state):
    """`dumpsys package NAME` as Android 16 prints it, cut to what the client reads."""
    p = state.get("packages", {}).get(name)
    if p is None:
        return f"Unable to find package: {name}\n"
    flags = "DEBUGGABLE HAS_CODE ALLOW_CLEAR_USER_DATA" if p.get("debuggable") else "HAS_CODE ALLOW_CLEAR_USER_DATA"
    return (f"Packages:\n  Package [{name}] (9a8b7c6):\n    appId=10345\n    pkg=Package{{1a2b {name}}}\n"
            f"    codePath={os.path.dirname(p['apk'])}\n    resourcePath={os.path.dirname(p['apk'])}\n"
            f"    versionCode={p['version_code']} minSdk=34 targetSdk=36\n    versionName={p['version_name']}\n"
            f"    flags=[ {flags} ]\n    privateFlags=[ PRIVATE_FLAG_ACTIVITIES_RESIZE_MODE_RESIZEABLE ]\n"
            f"    timeStamp=2026-11-09 06:58:41\n    lastUpdateTime={p.get('updated', '2026-11-09 06:58:42')}\n"
            f"    signatures=PackageSignatures{{3c4d version:2, signatures:[5e6f7a8b], past signatures:[]}}\n"
            f"    User 0: ceDataInode=12345 deDataInode=0 installed=true hidden=false suspended=false distractionFlags=0 "
            f"stopped=false notLaunched=false enabled={p.get('enabled', 0)} instant=false virtual=false\n"
            f"      firstInstallTime=2026-10-01 10:00:00\n      gids=[3003]\n"
            f"Queries:\n  system apps queryable: false\n")


def accessibility_dump(state):
    bound = state.get("a11y_bound", [])
    entries = ", \n                     ".join(
        f"Service[label=App {i}, feedbackType[FEEDBACK_GENERIC], capabilities=1, id={c.replace('/' + c.split('/')[0] + '.', '/.')}, "
        f"eventTypes=TYPES_ALL_MASK, notificationTimeout=0, requestA11yBtn=false]" for i, c in enumerate(bound))
    if state.get("a11y_unnamed"):
        entries = "Service[label=App 0, feedbackType[FEEDBACK_GENERIC], capabilities=1]"
    enabled = ", ".join("{" + c + "}" for c in state.get("a11y_enabled", []))
    return ("ACCESSIBILITY MANAGER (dumpsys accessibility)\n\nUser state[\n     attributes:{id=0, touchExplorationEnabled=false}\n"
            f"     Bound services:{{{entries}}}\n     Enabled services:{{{enabled}}}\n     Binding services:{{}}\n"
            "     Crashed services:{}\n]\n")


def install(path, state):
    """`pm install -r PATH`: the APK's manifest against what is installed, as Android checks it."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import phone_client
    files = state.setdefault("files", {})
    if state.get("install_error"):
        return f"Failure [{state['install_error']}]\n"
    if path not in files:
        return f"Error: Unable to open file: {path}\nConsider using a file under /data/local/tmp/\n"
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(files[path]))) as z:
        apk = phone_client.parse_apk_manifest(z.read("AndroidManifest.xml"))
    current = state.setdefault("packages", {}).get(apk["package"])
    if current and apk["version_code"] < current["version_code"]:
        return (f"Failure [INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected: Update version code "
                f"{apk['version_code']} is older than current {current['version_code']}]\n")
    installed = current.get("apk") if current else f"/data/app/~~bmV3==/{apk['package']}-bmV3==/base.apk"
    files[installed] = files[path]
    state["packages"][apk["package"]] = {**(current or {"debuggable": False, "enabled": 0}), "apk": installed,
                                         "version_code": apk["version_code"], "version_name": apk["version_name"],
                                         "updated": "2026-11-09 07:10:00"}
    save_state(state)
    return "Success\n"


def logcat(cmd, state):
    """`logcat -d [-b B] [-t T] [-s TAG:L ...] 2>&1 | tail -n N`: the stored lines, by tag, then the last N."""
    state["last_logcat"] = cmd
    save_state(state)
    words = shlex.split(cmd.split(" 2>&1 | tail -n ")[0].replace('"$(( $(date +%s) - ', "").replace(' )).000"', ""))
    lines = state.get("logcat", [])
    if "-s" in words:
        tags = [w.split(":")[0] for w in words[words.index("-s") + 1:]]
        lines = [ln for ln in lines if any(f" {t}: " in ln for t in tags)]
    n = int(cmd.rsplit(" ", 1)[1])
    return "".join(ln + "\n" for ln in lines[-n:])


def run_as(cmd, state):
    """`run-as PACKAGE ...` in the app's data folder: a listing, a stat or a cat of a relative path."""
    words = shlex.split(cmd)
    pkg = words[1]
    if not state.get("packages", {}).get(pkg, {}).get("debuggable"):
        return f"run-as: package not debuggable: {pkg}\n".encode()
    root = DATA_ROOT + pkg
    files = state.setdefault("files", {})

    def absolute(rel):
        return root if rel in (".", "") else root + "/" + rel.rstrip("/")

    if words[2:4] == ["sh", "-c"]:  # the client's folder listing, of a relative path
        inner = words[4]
        rel = shlex.split(inner[len("if [ -d "):inner.index(" ]")])[0]
        return list_command(f"if [ -d {shlex.quote(absolute(rel))} ]", state).encode()
    if words[2] == "stat":
        line = stat_line(absolute(words[6]), files)
        return ((line if line else MISSING_MARKER) + "\n").encode()
    if words[2] == "cat":
        path = absolute(words[3])
        if path not in files:
            return f"cat: {words[3]}: No such file or directory\n".encode()
        return base64.b64decode(files[path])
    return b"run-as: unsupported\n"


def helper_text(arg, files):
    """The helper's text argument: base64, or @path of a stored file."""
    if arg.startswith("@"):
        return base64.b64decode(files[arg[1:]]).decode("utf-8")
    return base64.b64decode(arg).decode("utf-8")


def helper(cmd, state):
    """The app_process helper's commands, as phone_client calls them. Like the real helper, its last
    line is its status: PHONE_RELAY_HELPER_OK, or PHONE_RELAY_HELPER_ERROR and the reason; and like
    app_process with no jar, it prints neither when the jar is missing."""
    files = state.setdefault("files", {})
    if HELPER_REMOTE not in files:
        return "/system/bin/sh: app_process: Error: Could not find class\n"
    if state.get("helper_error"):
        return f"PHONE_RELAY_HELPER_ERROR {state['helper_error']}\n"
    args = shlex.split(cmd[len(HELPER_PREFIX):].removesuffix(" 2>&1 || true"))
    name, out = args[0], ""
    if name == "version":
        out = "1\n"
    elif name == "ui":
        out = ui_xml(state) + "\n"
    elif name == "gesture":
        state.setdefault("gestures", []).append(json.loads(helper_text(args[1], files)))
    elif name == "clip-get":
        out = json.dumps({"text": state.get("clipboard")}) + "\n"
    elif name == "clip-set":
        state["clipboard"] = helper_text(args[1], files)
    elif name == "clip-clear":
        state["clipboard"] = None
    elif name == "paste":
        state.setdefault("pasted", []).append(helper_text(args[1], files))
    elif name == "audio":
        files[args[3]] = base64.b64encode(FAKE_WAV + f" {args[1]}s {args[2]}".encode()).decode()
    else:
        return f"PHONE_RELAY_HELPER_ERROR IllegalArgumentException: unknown command: {name}\n"
    save_state(state)
    return out + "PHONE_RELAY_HELPER_OK\n"


def run(cmd, state):
    """Output of one `adb shell`/`exec-out` command (bytes)."""
    m = GUARD_RE.match(cmd)
    if m:
        if not guard_passes(m.group("guard"), state):
            return (REFUSED_MARKER + "\n").encode()
        return run(m.group("action"), state)
    if cmd.startswith("uiautomator dump"):
        return ui_xml(state).encode()
    files = state.setdefault("files", {})
    if cmd.startswith(HELPER_PREFIX):
        return helper(cmd, state).encode()
    if cmd.startswith("run-as "):
        return run_as(cmd, state)
    if cmd.startswith("dumpsys package "):
        return package_dump(shlex.split(cmd)[2], state).encode()
    if cmd.startswith("pm install -r "):
        return install(shlex.split(cmd)[3], state).encode()
    if cmd.startswith("logcat -d"):
        return logcat(cmd, state).encode()
    if cmd.startswith("settings get secure enabled_accessibility_services; "):
        return (":".join(state.get("a11y_enabled", [])) + "\n" + SPLIT_MARKER + "\n" + accessibility_dump(state)).encode()
    if cmd.startswith("settings get ") and SPLIT_MARKER in cmd:  # one setting, and dark mode
        ns, key = cmd.split()[2:4]
        value = state.get("settings", {}).get(f"{ns} {key.rstrip(';')}", "null")
        return f"{value}\n{SPLIT_MARKER}\nNight mode: {state.get('night_mode', 'no')}\n".encode()
    if cmd.startswith("tail -c +"):  # `tail -c +N PATH | head -c L`: part of a file
        m = re.match(r"tail -c \+(\d+) (.+) \| head -c (\d+)$", cmd)
        path = shlex.split(m.group(2))[0]
        if path not in files:
            return b""
        start = int(m.group(1)) - 1
        return base64.b64decode(files[path])[start:start + int(m.group(3))]
    if cmd.startswith("stat -c %s "):
        path = shlex.split(cmd)[3]
        return (f"{len(base64.b64decode(files[path]))}\n" if path in files else "").encode()
    if cmd == "cat /proc/net/unix":
        return ("Num       RefCount Protocol Flags    Type St Inode Path\n"
                "0000000000000000: 00000002 00000000 00010000 0001 01 123 @chrome_devtools_remote\n"
                "0000000000000000: 00000002 00000000 00010000 0001 01 124 @webview_devtools_remote_4321\n"
                "0000000000000000: 00000002 00000000 00010000 0001 01 125 /dev/socket/other\n").encode()
    if cmd.startswith("sha256sum "):  # `sha256sum PATH 2>/dev/null; true`
        path = cmd.split()[1]
        if path not in files:
            return b""
        return f"{hashlib.sha256(base64.b64decode(files[path])).hexdigest()}  {path}\n".encode()
    if "setsid nohup sh -c" in cmd:  # a background recording: its segments appear at once
        seconds = int(re.search(r"\+ (\d+) \)\)", cmd).group(1))
        for i in range(math.ceil(seconds / 180)):
            files[f"{RECORD_PREFIX}{i}.mp4"] = base64.b64encode(f"fake mp4 segment {i}".encode()).decode()
        save_state(state)
        return b""
    if cmd.startswith("touch /data/local/tmp/phone_relay_rec.stop"):  # the recording stopped: list its segments
        return "".join(f + "\n" for f in sorted(files) if f.startswith(RECORD_PREFIX)).encode()
    if cmd.startswith("rm -f "):
        for path in shlex.split(cmd)[2:]:
            files.pop(path, None)
        save_state(state)
        return b""
    if cmd.startswith("if [ -d "):
        return list_command(cmd, state).encode()
    if cmd.startswith("stat -c "):
        line = stat_line(shlex.split(cmd)[4], files)
        return ((line if line else MISSING_MARKER) + "\n").encode()
    if " && " in cmd:  # several `input` calls chained by type_commands, or settings
        return b"".join(run(part, state) for part in cmd.split(" && "))
    if cmd == "screencap -p":
        return png(WIDTH, HEIGHT, state.get("shade", 48))
    if cmd.startswith(("am start", "am broadcast")):
        return am(cmd, state).encode()
    if cmd.startswith("dumpsys notification"):
        return state.get("notification_dump", NOTIFICATION_DUMP).encode()
    if cmd.startswith("cmd statusbar "):
        state["statusbar"] = cmd.split()[2]
        save_state(state)
        return b""
    if cmd == "settings get system accelerometer_rotation; settings get system user_rotation":
        return "{}\n{}\n".format(*state.get("rotation", [1, 0])).encode()
    if cmd.startswith("settings put system "):
        name, value = cmd.split()[3], int(cmd.split()[4])
        rotation = list(state.get("rotation", [1, 0]))
        rotation[0 if name == "accelerometer_rotation" else 1] = value
        state["rotation"] = rotation
        save_state(state)
        return b""
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
    if state.get("hang"):
        time.sleep(state["hang"])
    if args[0] == "devices":
        sys.stdout.write("List of devices attached\nlocalhost:5555\tdevice\n")
        return 0
    if args[0] == "forward":
        code, out = forward(args, state)
        sys.stdout.write(out)
        return code
    if args[0] in ("pull", "push") and len(args) == 3:
        code, err = transfer(args, state)
        sys.stderr.write(err)
        return code
    if args[0] not in ("shell", "exec-out") or len(args) < 2:
        sys.stderr.write("fake adb: unsupported\n")
        return 1
    sys.stdout.buffer.write(run(" ".join(args[1:]), state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
