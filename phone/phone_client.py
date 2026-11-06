#!/usr/bin/env python3
"""Phone client for the phone relay (phase 1: adb, from Termux on the phone or from a computer).

Dials out to the relay over WSS, receives commands, runs them through adb and
replies. There is no app allowlist: every app, key and setting is reachable.
A command given `if_foreground` (a package or a list) runs in the same shell
command as a focus check, so it only runs while one of those apps has focus.

Some commands go through a helper run as the shell user with app_process
(helper/phone-relay-helper.jar, pushed to the phone on first use): the UI tree,
multi-touch gestures, the clipboard, pasting text `input text` cannot type, and
audio. Its UI tree leaves the phone's accessibility services bound; only
`ui` with engine=uiautomator runs `uiautomator dump`, which unbinds them all
for a moment. Nothing else uses uiautomator, including --selftest.

Files and screen recordings cross the relay socket in acknowledged chunks
(see Channel), so their size is not limited by the socket's message size.

Usage:
    python phone_client.py --selftest      # check adb, foreground, input guard, text field, screenshot
    python phone_client.py                 # connect to the relay and serve commands
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import random
import re
import shlex
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

VERSION = "0.5.0"

# ---- key names and defaults (the relay has its own copy of the key names) ----

KEYCODES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "delete": "KEYCODE_DEL",
}
# Any other key may be sent by its Android name (KEYCODE_PAGE_DOWN, KEYCODE_POWER, ...) or number.
# These patterns only keep values shell-safe; they deny no key.
_RAW_KEY_RE = re.compile(r"^KEYCODE_[A-Z0-9_]{1,40}$")
_KEY_NUMBER_RE = re.compile(r"^\d{1,4}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
DEFAULT_STOP_FILE = os.path.expanduser("~/STOP_PHONE_AGENT")

JPEG_QUALITY = 60
# A command's deadline when the relay sends none; the agent can set any `timeout`.
DEFAULT_TIMEOUT_S = 30
# Characters per `input text` call, so a long text never becomes an over-long adb command.
TEXT_CHUNK = 1000
UI_DUMP_PATH = "/data/local/tmp/phone_relay_ui.xml"
# Printed by a guarded command when the focus check fails, instead of running the action.
REFUSED_MARKER = "PHONE_RELAY_REFUSED"
# Printed by a shell command when the path it was given does not exist or cannot be read.
MISSING_MARKER = "PHONE_RELAY_MISSING"
# Files and recordings cross the relay socket in chunks of this size, at most STREAM_WINDOW unacknowledged.
STREAM_CHUNK = 256 * 1024
STREAM_WINDOW = 8
# Default deadline for moving a file, and for stopping a recording and fetching it.
TRANSFER_TIMEOUT_S = 300
# screenrecord stops each file at 180 s; a longer recording is several files, sent back as a zip.
RECORD_SEGMENT_S = 180
RECORD_BIT_RATE = 2_000_000
RECORD_PREFIX = "/data/local/tmp/phone_relay_rec_"
RECORD_PID = "/data/local/tmp/phone_relay_rec.pid"  # the recording loop's shell, while it runs
RECORD_CHILD = "/data/local/tmp/phone_relay_rec.child"  # the screenrecord it is waiting on
RECORD_STOP = "/data/local/tmp/phone_relay_rec.stop"  # asks the loop not to start another segment
STATUSBAR_ACTIONS = {"notifications": "expand-notifications", "settings": "expand-settings", "collapse": "collapse"}
WAIT_KINDS = ("change", "stable", "foreground", "text")
# A batch may run any command that replies with JSON, plus sleep; not the streaming ones or batch itself.
BATCH_COMMANDS = ("state", "screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent",
                  "notifications", "statusbar", "rotation", "wait", "files", "record_start", "gesture",
                  "clipboard_get", "clipboard_set", "sleep")
# Commands that run their action inside the if_foreground guard themselves (in the same shell command).
# Any other command given if_foreground is checked against the foreground before it starts.
SELF_GUARDED = ("screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent", "statusbar",
                "rotation", "record_start", "record", "batch", "gesture", "clipboard_get", "clipboard_set", "audio")
# The app_process helper: built from helper/src by helper/build.py, pushed to the phone on first use.
HELPER_JAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper", "phone-relay-helper.jar")
HELPER_REMOTE = "/data/local/tmp/phone-relay-helper.jar"
HELPER_CLASS = "com.thomaswcode.phonerelay.Helper"
# The helper's last line: success, or failure and its reason. Only the last line counts, so content
# that happens to contain these words (clipboard text, a screen's text) is never mistaken for them.
HELPER_OK = "PHONE_RELAY_HELPER_OK"
HELPER_ERROR = "PHONE_RELAY_HELPER_ERROR"
# Text longer than this goes to the helper in a file, not on its command line.
HELPER_ARG_LIMIT = 4000
HELPER_TEXT_REMOTE = "/data/local/tmp/phone_relay_text.txt"
AUDIO_REMOTE = "/data/local/tmp/phone_relay_audio.wav"
AUDIO_SOURCES = ("output", "mic")
TYPE_METHODS = ("keys", "paste")
UI_ENGINES = ("helper", "uiautomator")
# Side messages of a running command, routed to its Channel rather than queued as commands.
SIDE_TYPES = ("chunk", "end", "ack", "cancel")

log = logging.getLogger("phone_client")


class CommandError(Exception):
    """A refusal or failure reported back to the relay as {ok: false, error: code}."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Channel:
    """One command's side messages on the relay socket: file chunks in or out, and their acks.

    A download (pull, record) sends a start message, then chunks, never more than
    STREAM_WINDOW ahead of the relay's acks. An upload (push) receives chunks, acks
    each and ends at the relay's end message. The relay sends cancel if the agent goes away.
    """

    def __init__(self, cid: str, send) -> None:
        self.id = cid
        self._send = send
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, msg: dict) -> None:
        await self._send(json.dumps({"id": self.id, **msg}))

    async def _next(self) -> dict:
        msg = await self.inbox.get()
        if msg.get("type") == "cancel":
            raise CommandError("cancelled", "the relay cancelled the transfer")
        return msg

    async def send_file(self, path: str, mime: str, headers: dict | None = None) -> int:
        size = os.path.getsize(path)
        await self.send({"type": "start", "mime": mime, "size": size, "headers": headers or {}})
        acked, seq = -1, 0
        with open(path, "rb") as f:
            while chunk := f.read(STREAM_CHUNK):
                # Read what has arrived (acks, a cancel); wait only when the window is full.
                while not self.inbox.empty() or seq - acked > STREAM_WINDOW:
                    msg = await self._next()
                    if msg.get("type") == "ack" and isinstance(msg.get("seq"), int):
                        acked = max(acked, msg["seq"])
                await self.send({"type": "chunk", "seq": seq, "data": base64.b64encode(chunk).decode("ascii")})
                seq += 1
        return size

    async def recv_file(self, path: str) -> int:
        total = 0
        with open(path, "wb") as f:
            while True:
                msg = await self._next()
                if msg.get("type") == "end":
                    return total
                if msg.get("type") == "chunk":
                    data = base64.b64decode(msg.get("data") or "")
                    f.write(data)
                    total += len(data)
                    await self.send({"type": "ack", "seq": msg.get("seq")})


# ---- adb wrapper ----


class Adb:
    def __init__(self, adb_bin: str = "adb", serial: str | None = None) -> None:
        self.adb_bin = adb_bin
        self.serial = serial

    async def run(self, *args: str, timeout: float | None = None) -> bytes:
        """One adb call. No timeout by default: the command's deadline (PhoneController.handle) cancels it."""
        # A .py "adb" (the tests' fake) runs with this Python: no shell wrapper, so it works on Windows too.
        cmd = [sys.executable, self.adb_bin] if self.adb_bin.endswith(".py") else [self.adb_bin]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as e:
            raise CommandError("adb_failed", f"adb not found: {self.adb_bin}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as e:
            await _kill(proc)
            raise CommandError("timeout", " ".join(args[:2])) from e
        except asyncio.CancelledError:
            await _kill(proc)  # the command's deadline passed: never leave adb running behind it
            raise
        if proc.returncode != 0:
            raise CommandError("adb_failed", err.decode(errors="replace").strip()[:200])
        return out

    async def shell(self, command: str, timeout: float | None = None) -> str:
        return (await self.run("shell", command, timeout=timeout)).decode(errors="replace")

    async def exec_out(self, command: str, timeout: float | None = None) -> bytes:
        return await self.run("exec-out", command, timeout=timeout)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


# ---- parsing helpers (pure, unit-tested) ----

_FOCUS_RE = re.compile(r"mCurrentFocus=(null|Window\{[0-9a-fA-F]+ (?:u\d+ )?([^\s/}]+)[^}]*\})")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=.*?ActivityRecord\{[0-9a-fA-F]+ (?:u\d+ )?([^\s/}]+)/")
_WAKE_RE = re.compile(r"mWakefulness=(\w+)")
_SIZE_RE = re.compile(r"(Physical|Override) size: (\d+)x(\d+)")
_BATTERY_RE = re.compile(r"^\s*level: (\d+)", re.MULTILINE)
_INPUT_TYPE_RE = re.compile(r"\binputType=(0x[0-9a-fA-F]+|\d+)")
_EDITOR_PKG_RE = re.compile(r"\bpackageName=(\S+)")
_INPUT_SHOWN_RE = re.compile(r"\bmInputShown=(true|false)")
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_foreground(dumpsys: str) -> str | None:
    """Return the package (or window name) of the focused window, or None if unknown.

    Uses the first mCurrentFocus line. A lock screen shows up as NotificationShade
    or com.android.systemui. In-app popups (e.g. Chrome's overflow menu) are titled PopupWindow:<id> with no
    package; only for those, the owning app is taken from mFocusedApp.
    """
    m = _FOCUS_RE.search(dumpsys)
    if not m or m.group(1) == "null":
        return None
    name = m.group(2)
    if name.startswith("PopupWindow:"):
        app = _FOCUSED_APP_RE.search(dumpsys)
        return app.group(1) if app else None
    return name


def parse_wakefulness(dumpsys: str) -> str | None:
    m = _WAKE_RE.search(dumpsys)
    return m.group(1) if m else None


def parse_size(wm_size: str) -> tuple[int, int] | None:
    """Parse `wm size`. An override size wins over the physical size."""
    sizes = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in _SIZE_RE.finditer(wm_size)}
    return sizes.get("Override") or sizes.get("Physical")


def parse_battery(dumpsys: str) -> int | None:
    m = _BATTERY_RE.search(dumpsys)
    return int(m.group(1)) if m else None


def parse_bounds(bounds: str) -> list[int] | None:
    m = _BOUNDS_RE.fullmatch(bounds.strip())
    return [int(v) for v in m.groups()] if m else None


def _short(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[-1] if name else ""


def parse_ui(xml_bytes: bytes) -> list[dict]:
    """Compact list of useful elements from a uiautomator dump, from every package on screen.

    Nodes with no text, description or flag worth acting on are dropped; nothing else is.
    """
    start = xml_bytes.find(b"<?xml")
    if start < 0:
        start = xml_bytes.find(b"<hierarchy")
    end = xml_bytes.rfind(b"</hierarchy>")
    if start < 0 or end < 0:
        raise CommandError("ui_unavailable", "no hierarchy in uiautomator output")
    try:
        root = ET.fromstring(xml_bytes[start : end + len(b"</hierarchy>")])
    except ET.ParseError as e:
        raise CommandError("ui_unavailable", f"bad XML: {e}") from e

    elements: list[dict] = []
    for node in root.iter("node"):
        a = node.attrib
        bounds = parse_bounds(a.get("bounds", ""))
        if not bounds or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        cls = a.get("class", "")
        password = a.get("password") == "true"
        text = a.get("text", "")
        desc = a.get("content-desc", "")
        clickable = a.get("clickable") == "true"
        scrollable = a.get("scrollable") == "true"
        editable = "EditText" in cls
        if not (text or desc or clickable or scrollable or editable):
            continue
        el = {
            "i": len(elements),
            "text": text,
            "desc": desc,
            "id": _short(a.get("resource-id", "")),
            "cls": _short(cls),
            "pkg": a.get("package"),
            "bounds": bounds,
            "center": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
        }
        for flag, value in (
            ("clickable", clickable),
            ("scrollable", scrollable),
            ("editable", editable),
            ("focused", a.get("focused") == "true"),
            ("password", password),
            ("checked", a.get("checked") == "true"),
        ):
            if value:
                el[flag] = True
        elements.append(el)
    return elements


_NOTIFICATION_RE = re.compile(r"^\s*NotificationRecord\(0x[0-9a-fA-F]+: pkg=(\S+)(.*)$")
_NOTIFICATION_FIELD_RE = re.compile(r"\b(id|tag|importance|key)=(\S+)")
_EXTRA_RE = re.compile(r"^\s*android\.(\w+)=(\w+) \((.*)$")
# Notification extras returned, by their name after "android.".
NOTIFICATION_EXTRAS = {
    "title": "title", "text": "text", "bigText": "big_text", "subText": "sub_text", "infoText": "info_text",
    "summaryText": "summary", "conversationTitle": "conversation", "template": "template",
}


_DUMP_FIELD_RE = re.compile(r"^[\w.$:-]+[=(]")


def _structural(line: str, indent: int) -> bool:
    """Whether a dump line is the dump's own, not text carried on from a value: the next extra (a
    `key=` line at the extras' own indent), or the end of the extras (a `}` or another `key=` field
    further out). A value's own lines are printed as they are, so anything else belongs to it."""
    stripped = line.lstrip()
    depth = len(line) - len(stripped)
    if depth == indent:
        return bool(_DUMP_FIELD_RE.match(stripped))
    return 0 < depth < indent and (stripped == "}" or bool(_DUMP_FIELD_RE.match(stripped)))


def parse_notifications(dumpsys: str) -> list[dict]:
    """Notifications from `dumpsys notification --noredact`: key, package, id, tag, title, text, ...

    Each NotificationRecord block is read up to the next one. A value that runs over
    several lines (a long text) is joined back up: it ends where the dump's own indented
    lines resume, and its last ")" is the dump's. A key already seen is skipped, as the
    dump can list a record in more than one section.
    """
    out: list[dict] = []
    seen: set[str] = set()
    cur: dict | None = None
    lines = dumpsys.splitlines()
    i = 0
    while i < len(lines):
        m = _NOTIFICATION_RE.match(lines[i])
        if m:
            fields = dict(_NOTIFICATION_FIELD_RE.findall(m.group(2)))
            key = fields.get("key", "").rstrip(":")
            cur = None
            if key not in seen:
                seen.add(key)
                tag = fields.get("tag")
                cur = {"key": key, "pkg": m.group(1), "id": int(fields["id"]) if fields.get("id", "").lstrip("-").isdigit() else None,
                       "tag": None if tag in (None, "null") else tag}
                out.append(cur)
        elif cur is not None:
            e = _EXTRA_RE.match(lines[i])
            if e and e.group(1) in NOTIFICATION_EXTRAS:
                indent = len(lines[i]) - len(lines[i].lstrip())
                value = e.group(3)
                while i + 1 < len(lines) and not _structural(lines[i + 1], indent):
                    i += 1
                    value += "\n" + lines[i]
                value = value.rstrip()
                cur[NOTIFICATION_EXTRAS[e.group(1)]] = value[:-1] if value.endswith(")") else value
        i += 1
    return out


_STAT_TYPES = {"regular file": "file", "regular empty file": "file", "directory": "dir", "symbolic link": "link"}


def parse_stat(line: str) -> dict | None:
    """One `stat -c '%F|%s|%Y|%n'` line as {name, type, size, modified (Unix seconds)}."""
    parts = line.rstrip("\r").split("|", 3)
    if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    ftype, size, mtime, name = parts
    return {"name": name, "type": _STAT_TYPES.get(ftype, ftype), "size": int(size), "modified": int(mtime)}


def frame_signature(png: bytes):
    """A small greyscale copy of a screenshot for comparing frames; its hash if Pillow is missing."""
    try:
        from PIL import Image
    except ImportError:
        return hashlib.sha256(png).digest()
    img = Image.open(io.BytesIO(png)).convert("L")
    return img.resize((max(1, img.width // 8), max(1, img.height // 8)))


def frame_diff(a, b) -> float:
    """Fraction of pixels that changed noticeably between two frame signatures (0 or 1 for hashes)."""
    if isinstance(a, bytes) or isinstance(b, bytes):
        return 0.0 if a == b else 1.0
    if a.size != b.size:
        return 1.0
    from PIL import ImageChops

    changed = ImageChops.difference(a, b).point(lambda v: 255 if v > 24 else 0).histogram()[255]
    return changed / (a.width * a.height)


def parse_editor_info(dumpsys: str) -> tuple[str, int] | None:
    """(package, inputType) of the text field the keyboard is serving, from `dumpsys input_method`.

    Reads the indented block under the curEditorInfo: line. None if it is missing.
    """
    lines = dumpsys.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "curEditorInfo:":
            continue
        indent = len(line) - len(line.lstrip())
        block = []
        for nxt in lines[i + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            block.append(nxt)
        text = "\n".join(block)
        t, p = _INPUT_TYPE_RE.search(text), _EDITOR_PKG_RE.search(text)
        if not t or not p:
            return None
        return p.group(1), int(t.group(1), 0)
    return None


def parse_input_shown(dumpsys: str) -> bool | None:
    """Whether the on-screen keyboard is showing, from `dumpsys input_method` (None if unknown)."""
    m = _INPUT_SHOWN_RE.search(dumpsys)
    return None if m is None else m.group(1) == "true"


def is_password_type(input_type: int) -> bool:
    """True for Android's password inputTypes (EditorInfo class and variation bits)."""
    cls, variation = input_type & 0xF, input_type & 0xFF0
    # TYPE_CLASS_TEXT with PASSWORD, VISIBLE_PASSWORD or WEB_PASSWORD; TYPE_CLASS_NUMBER with PASSWORD
    return (cls == 0x1 and variation in (0x80, 0x90, 0xE0)) or (cls == 0x2 and variation == 0x10)


def focus_guard(packages: tuple[str, ...]) -> str:
    """POSIX sh condition: true only while the screen is awake and one of `packages` has focus.

    Mirrors parse_foreground: an in-app PopupWindow counts for the app in mFocusedApp.
    """
    alt = "|".join(re.escape(p) for p in packages)
    win = r"mCurrentFocus=Window\{[0-9a-fA-F]+ (u[0-9]+ )?"
    app = r"mFocusedApp=.*ActivityRecord\{[0-9a-fA-F]+ (u[0-9]+ )?"
    return (
        "d=$(dumpsys window | grep -E 'mCurrentFocus=|mFocusedApp=') && "
        "dumpsys power 2>/dev/null | grep -m1 'mWakefulness=' | grep -q Awake && "
        f"{{ echo \"$d\" | grep -qE '{win}({alt})/' || "
        f"{{ echo \"$d\" | grep -qE '{win}PopupWindow:' && echo \"$d\" | grep -qE '{app}({alt})/'; }}; }}"
    )


def guarded(action: str, packages: tuple[str, ...]) -> str:
    """`action` run only if focus_guard passes, in the same shell command; otherwise prints REFUSED_MARKER."""
    return f"if {focus_guard(packages)}; then {action}; else echo {REFUSED_MARKER}; fi"


def validate_text(text: object) -> str:
    """Text `input text` can type as keys: any length of ASCII. Newlines and tabs are typed as Enter
    and Tab; other control characters, and anything beyond ASCII, have to be pasted instead."""
    if not isinstance(text, str) or not text:
        raise CommandError("invalid_args", "text must be a non-empty string")
    for c in text:
        if ord(c) > 0x7E:
            raise CommandError("invalid_args", "non-ASCII text cannot be typed as keys; paste it (method paste)")
        if ord(c) < 0x20 and c not in "\n\r\t":
            raise CommandError("invalid_args", "control characters other than newline and tab cannot be typed as keys")
    return text


def keys_can_type(text: str) -> bool:
    try:
        validate_text(text)
        return True
    except CommandError:
        return False


def escape_text(text: str) -> str:
    """Shell-quoted argument for one `input text`. Spaces become %s (input's own escape).

    The text must be one line of printable ASCII without a literal "%s";
    input_text_command splits around those, and type_commands around newlines and tabs.
    """
    if any(not 0x20 <= ord(c) <= 0x7E for c in validate_text(text)):
        raise CommandError("invalid_args", "one `input text` call takes one line of printable ASCII")
    return shlex.quote(text.replace(" ", "%s"))


def input_text_command(text: str) -> str:
    """Shell command typing one line of `text` exactly, '%' included.

    `input text` turns every "%s" into a space, so a literal "%s" is sent as two
    calls ("...%" then "s..."). Spaces still go through as %s.
    """
    validate_text(text)
    parts = text.split("%s")
    segments = [parts[0]]
    for part in parts[1:]:
        segments[-1] += "%"
        segments.append("s" + part)
    return " && ".join(f"input text {escape_text(s)}" for s in segments if s)


def type_commands(text: object) -> list[str]:
    """Shell commands typing `text` exactly: `input text` per line, Enter for a newline, Tab for a tab.

    A line longer than TEXT_CHUNK characters is typed in several calls. Consecutive
    commands are joined with && into one adb call while that stays short.
    """
    text = validate_text(text).replace("\r\n", "\n").replace("\r", "\n")
    steps: list[str] = []
    for piece in re.split(r"([\n\t])", text):
        if piece == "\n":
            steps.append("input keyevent KEYCODE_ENTER")
        elif piece == "\t":
            steps.append("input keyevent KEYCODE_TAB")
        else:
            steps += [input_text_command(piece[i : i + TEXT_CHUNK]) for i in range(0, len(piece), TEXT_CHUNK)]
    commands: list[str] = []
    for step in steps:
        if commands and len(commands[-1]) + len(step) < 4 * TEXT_CHUNK:
            commands[-1] += " && " + step
        else:
            commands.append(step)
    return commands


def resolve_key(key: object) -> str:
    """Android keycode for a short name (back, home, ...), a KEYCODE_* name or a key number."""
    if isinstance(key, int) and not isinstance(key, bool) and 0 <= key <= 9999:
        return str(key)
    if isinstance(key, str) and key in KEYCODES:
        return KEYCODES[key]
    if isinstance(key, str) and (_RAW_KEY_RE.match(key) or _KEY_NUMBER_RE.match(key)):
        return key
    raise CommandError("invalid_args", f"key must be one of {', '.join(KEYCODES)}, a KEYCODE_* name or a key number")


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _coord(args: dict, name: str, scale: float) -> int:
    v = args.get(name)
    if not _is_number(v):
        raise CommandError("invalid_args", f"{name} must be a number")
    return round(v / scale)


def _scale(args: dict, default: float = 1.0) -> float:
    v = args.get("scale", default)
    if not _is_number(v) or v <= 0:
        raise CommandError("invalid_args", "scale must be a number above 0")
    return float(v)


def _timeout(args: dict, default: float) -> float:
    v = args.get("timeout", default)
    if not _is_number(v) or v <= 0:
        raise CommandError("invalid_args", "timeout must be a number of seconds above 0")
    return float(v)


def _number_arg(args: dict, name: str, default: float, minimum: float = 0) -> float:
    v = args.get(name, default)
    if not _is_number(v) or v < minimum:
        raise CommandError("invalid_args", f"{name} must be a number {minimum:g} or above")
    return v


def _string_arg(args: dict, name: str, required: bool = True) -> str | None:
    v = args.get(name)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v:
        raise CommandError("invalid_args", f"{name} must be a non-empty string")
    return v


def _package_arg(args: dict, name: str = "package") -> str | None:
    v = args.get(name)
    if v is not None and (not isinstance(v, str) or not _PACKAGE_RE.match(v)):
        raise CommandError("invalid_args", f"{name} must be an Android package name")
    return v


def _remote_path(args: dict) -> str:
    path = _string_arg(args, "path")
    if not path.startswith("/"):
        raise CommandError("invalid_args", "path must be absolute, e.g. /sdcard/Download/file.pdf")
    return path


def _extra_args(key: object, value: object) -> list[str]:
    """`am` arguments for one intent extra, typed by its JSON value."""
    if not isinstance(key, str) or not key:
        raise CommandError("invalid_args", "extras keys must be non-empty strings")
    k = shlex.quote(key)
    if value is None:
        return ["--esn", k]
    if isinstance(value, bool):
        return ["--ez", k, "true" if value else "false"]
    if isinstance(value, int):
        return ["--ei" if -(2**31) <= value < 2**31 else "--el", k, str(value)]
    if isinstance(value, float):
        return ["--ef", k, repr(value)]
    if isinstance(value, str):
        return ["--es", k, shlex.quote(value)]
    raise CommandError("invalid_args", "extras values must be strings, numbers, booleans or null")


def _check_am(out: str) -> None:
    """`am`'s own error line. Its commands are run with `|| true`, as it exits 1 on an unresolvable
    intent, which adb would report without the reason `am` printed."""
    if re.search(r"^(Error|Exception|java\.)", out, re.MULTILINE):
        raise CommandError("launch_failed", out.strip()[-300:])


def _record_args(args: dict) -> tuple[float, str | None, int]:
    seconds = _number_arg(args, "seconds", 10)
    if seconds <= 0:
        raise CommandError("invalid_args", "seconds must be above 0")
    size = args.get("size")
    if size is not None and (not isinstance(size, str) or not re.fullmatch(r"\d{2,5}x\d{2,5}", size)):
        raise CommandError("invalid_args", "size must look like 720x1560")
    bit_rate = args.get("bit_rate", RECORD_BIT_RATE)
    if not isinstance(bit_rate, int) or isinstance(bit_rate, bool) or bit_rate <= 0:
        raise CommandError("invalid_args", "bit_rate must be a whole number of bits per second")
    return seconds, size, bit_rate


def _local_temp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="phone_relay_", suffix=suffix)
    os.close(fd)
    return path


def _remove(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def default_timeout(cmd: str, args: dict) -> float:
    """Deadline when the agent sets none: 30 s, plus the time the work itself takes
    (typing, a long swipe, a wait, a recording, a batch's steps); five minutes for files."""
    if cmd == "type" and isinstance(args.get("text"), str):
        return DEFAULT_TIMEOUT_S + len(args["text"]) / 10
    if cmd == "swipe" and _is_number(args.get("ms")):
        return DEFAULT_TIMEOUT_S + max(args["ms"], 0) / 1000
    if cmd == "wait":
        return DEFAULT_TIMEOUT_S + (args["seconds"] if _is_number(args.get("seconds")) else 10)
    if cmd in ("record", "audio"):
        return 2 * DEFAULT_TIMEOUT_S + 2 * (args["seconds"] if _is_number(args.get("seconds")) else 10)
    if cmd == "gesture" and _is_number(args.get("ms")):
        return DEFAULT_TIMEOUT_S + max(args["ms"], 0) / 1000
    if cmd in ("pull", "push", "record_stop"):
        return TRANSFER_TIMEOUT_S
    if cmd == "batch" and isinstance(args.get("steps"), list):
        total = DEFAULT_TIMEOUT_S
        for step in args["steps"]:
            step_args = step.get("args") if isinstance(step, dict) and isinstance(step.get("args"), dict) else {}
            if isinstance(step, dict) and step.get("cmd") == "sleep":
                total += max(step_args.get("ms", 0), 0) / 1000 if _is_number(step_args.get("ms")) else 0
            elif isinstance(step, dict):
                t = step_args.get("timeout")
                total += t if _is_number(t) and t > 0 else default_timeout(str(step.get("cmd")), step_args)
        return total
    return DEFAULT_TIMEOUT_S


def _if_foreground(args: dict) -> tuple[str, ...] | None:
    """Packages named by the optional if_foreground argument (one name or a list), or None."""
    v = args.get("if_foreground")
    if v is None:
        return None
    names = [v] if isinstance(v, str) else v
    if not isinstance(names, list) or not names or not all(isinstance(p, str) and _PACKAGE_RE.match(p) for p in names):
        raise CommandError("invalid_args", "if_foreground must be a package name or a list of them")
    return tuple(names)


# ---- controller ----


class PhoneController:
    def __init__(
        self,
        adb: Adb,
        stop_file: str = DEFAULT_STOP_FILE,
        focus_retries: int = 3,
        focus_retry_delay: float = 0.3,
        helper_jar: str = HELPER_JAR,
    ) -> None:
        self.adb = adb
        self.stop_file = stop_file
        self.focus_retries = focus_retries
        self.focus_retry_delay = focus_retry_delay
        self.helper_jar = helper_jar
        self._helper_pushed = False
        self._screen_size: tuple[int, int] | None = None

    def stopped(self) -> bool:
        return os.path.exists(self.stop_file)

    async def foreground_and_wake(self) -> tuple[str | None, str | None]:
        """Foreground package and wakefulness. Retries briefly while focus is null (transitions)."""
        fg = wake = None
        for attempt in range(self.focus_retries):
            out = await self.adb.shell(
                "dumpsys window | grep -E 'mCurrentFocus=|mFocusedApp='; dumpsys power | grep -E 'mWakefulness='"
            )
            fg, wake = parse_foreground(out), parse_wakefulness(out)
            if fg is not None:
                break
            if attempt + 1 < self.focus_retries:
                await asyncio.sleep(self.focus_retry_delay)
        return fg, wake

    async def screen_size(self) -> tuple[int, int]:
        if self._screen_size is None:
            size = parse_size(await self.adb.shell("wm size"))
            if size is None:
                raise CommandError("device_error", "could not read screen size")
            self._screen_size = size
        return self._screen_size

    async def _helper_command(self, *args: str) -> str:
        """The shell command running the helper with `args`; pushes the jar first if the phone's copy differs."""
        if not self._helper_pushed:
            try:
                with open(self.helper_jar, "rb") as f:
                    local = hashlib.sha256(f.read()).hexdigest()
            except OSError as e:
                raise CommandError("helper_missing", f"{self.helper_jar}: build it with helper/build.py") from e
            # `; true`: adb passes on the shell's exit status, and sha256sum fails before the first push.
            remote = (await self.adb.shell(f"sha256sum {HELPER_REMOTE} 2>/dev/null; true")).split()[:1]
            if remote != [local]:
                await self.adb.run("push", self.helper_jar, HELPER_REMOTE)
            self._helper_pushed = True
        # `2>&1 || true`: app_process reports a missing class on stderr, and the helper exits 1 on failure,
        # which adb would report without the reason; the status line (see _helper) says how it went.
        return (f"CLASSPATH={HELPER_REMOTE} app_process / {HELPER_CLASS} " + " ".join(shlex.quote(a) for a in args)
                + " 2>&1 || true")

    async def _helper(self, args: dict, *helper_args: str) -> str:
        """Run the helper (inside the if_foreground guard if asked) and return its output without the status
        line. Its failure, or output without a status line (app_process never reached the helper: a missing
        or broken jar), becomes helper_failed."""
        out = await self._guarded(await self._helper_command(*helper_args), args)
        lines = out.splitlines()
        last = lines[-1].strip() if lines else ""
        if last == HELPER_OK:
            return "\n".join(lines[:-1]) + "\n"
        if last.startswith(HELPER_ERROR):
            raise CommandError("helper_failed", last[len(HELPER_ERROR):].strip()[:300])
        self._helper_pushed = False  # check the phone's copy (and push it again) on the next call
        raise CommandError("helper_failed", "the helper did not run: " + (out.strip()[-300:] or "no output"))

    async def _helper_text_arg(self, text: str) -> tuple[str, bool]:
        """The helper's argument for `text`: base64, or "@file" pushed to the phone when it is long.
        The bool says whether that file must be removed afterwards."""
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        if len(encoded) <= HELPER_ARG_LIMIT:
            return encoded, False
        local = _local_temp(".txt")
        try:
            with open(local, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            await self.adb.run("push", local, HELPER_TEXT_REMOTE)
        finally:
            _remove(local)
        return "@" + HELPER_TEXT_REMOTE, True

    async def _helper_with_text(self, args: dict, command: str, text: str) -> str:
        arg, is_file = await self._helper_text_arg(text)
        try:
            return await self._helper(args, command, arg)
        finally:
            if is_file:
                await self.adb.shell(f"rm -f {HELPER_TEXT_REMOTE}")

    async def _mismatch(self, packages: tuple[str, ...]) -> CommandError:
        fg, wake = await self.foreground_and_wake()
        return CommandError("foreground_mismatch", f"wanted {', '.join(packages)}; foreground={fg}, wakefulness={wake}")

    async def _dump_ui(self, packages: tuple[str, ...] | None) -> bytes:
        """uiautomator dump, guarded if asked. Unbinds all accessibility services for a moment."""
        dump = f"uiautomator dump {UI_DUMP_PATH} >/dev/null && cat {UI_DUMP_PATH}; rm -f {UI_DUMP_PATH}"
        command = dump if packages is None else guarded(dump, packages)
        last: CommandError | None = None
        for _ in range(2):  # uiautomator fails if the screen is animating; retry once
            try:
                out = await self.adb.exec_out(command)
                if out.startswith(REFUSED_MARKER.encode()):
                    raise await self._mismatch(packages or ())
                if b"</hierarchy>" in out:
                    return out
                last = CommandError("ui_unavailable", out.decode(errors="replace").strip()[:200])
            except CommandError as e:
                if e.code == "foreground_mismatch":
                    raise
                last = CommandError("ui_unavailable", e.detail)
            await asyncio.sleep(0.5)
        raise last or CommandError("ui_unavailable")

    async def _guarded(self, action: str, args: dict) -> str:
        """Run `action` on the phone. With if_foreground, only while one of those apps has focus,
        checked in the same shell command, so a focus change just before the action refuses it."""
        packages = _if_foreground(args)
        if packages is None:
            return await self.adb.shell(action)
        out = await self.adb.shell(guarded(action, packages))
        if out.strip() == REFUSED_MARKER:  # the refusal is all a refused command prints
            raise await self._mismatch(packages)
        return out

    # ---- commands ----

    async def handle(self, cmd: str, args: dict, channel: Channel | None = None) -> dict:
        if self.stopped():
            raise CommandError("stopped", "stop file present")
        if not isinstance(args, dict):
            raise CommandError("invalid_args", "args must be an object")
        handler = {
            "state": self.cmd_state,
            "screenshot": self.cmd_screenshot,
            "ui": self.cmd_ui,
            "tap": self.cmd_tap,
            "swipe": self.cmd_swipe,
            "type": self.cmd_type,
            "key": self.cmd_key,
            "launch": self.cmd_launch,
            "open": self.cmd_open,
            "intent": self.cmd_intent,
            "notifications": self.cmd_notifications,
            "statusbar": self.cmd_statusbar,
            "rotation": self.cmd_rotation,
            "wait": self.cmd_wait,
            "batch": self.cmd_batch,
            "files": self.cmd_files,
            "record_start": self.cmd_record_start,
            "gesture": self.cmd_gesture,
            "clipboard_get": self.cmd_clipboard_get,
            "clipboard_set": self.cmd_clipboard_set,
        }.get(cmd)
        # These stream bytes over the relay socket, so they need the command's channel.
        streaming = {
            "pull": self.cmd_pull,
            "push": self.cmd_push,
            "record": self.cmd_record,
            "record_stop": self.cmd_record_stop,
            "audio": self.cmd_audio,
        }.get(cmd)
        if handler is None and streaming is None:
            raise CommandError("invalid_args", f"unknown command: {cmd}")
        if streaming is not None and channel is None:
            raise CommandError("invalid_args", f"{cmd} streams over the relay; it cannot run here")
        packages = _if_foreground(args)  # validates
        timeout = _timeout(args, default_timeout(cmd, args))
        if packages is not None and cmd not in SELF_GUARDED:
            fg, wake = await self.foreground_and_wake()
            if fg not in packages or wake not in (None, "Awake"):
                raise CommandError("foreground_mismatch", f"wanted {', '.join(packages)}; foreground={fg}, wakefulness={wake}")
        try:
            # The deadline cancels the command; Adb.run kills the adb call it was waiting on.
            return await asyncio.wait_for(handler(args) if handler else streaming(args, channel), timeout)
        except asyncio.TimeoutError as e:
            raise CommandError("timeout", f"{cmd} took longer than {timeout:g} s; pass a larger timeout") from e

    async def cmd_state(self, args: dict) -> dict:
        fg, wake = await self.foreground_and_wake()
        battery = parse_battery(await self.adb.shell("dumpsys battery"))
        w, h = await self.screen_size()
        return {
            "foreground": fg,
            "awake": wake == "Awake",
            "battery": battery,
            "screen": {"width": w, "height": h},
            "rotation": await self._rotation(),
            "client_version": VERSION,
        }

    async def cmd_screenshot(self, args: dict) -> dict:
        scale = min(_scale(args, 0.5), 1.0)  # no upscaling: it adds no detail
        packages = _if_foreground(args)
        png = await self.adb.exec_out("screencap -p" if packages is None else guarded("screencap -p", packages))
        if png.startswith(REFUSED_MARKER.encode()):
            raise await self._mismatch(packages or ())
        if not png.startswith(b"\x89PNG"):
            raise CommandError("device_error", "screencap did not return a PNG")
        fg, _ = await self.foreground_and_wake()
        # With if_foreground: if the app in front changed while capturing, discard the image.
        if packages is not None and fg not in packages:
            raise CommandError("foreground_mismatch", f"foreground changed to {fg} during the capture")
        try:
            from PIL import Image
        except ImportError:
            w, h = _png_size(png)
            return _image_result(png, "image/png", w, h, w, h, 1.0, fg)
        img = Image.open(io.BytesIO(png))
        rw, rh = img.size
        if scale < 1:
            img = img.resize((max(1, round(rw * scale)), max(1, round(rh * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return _image_result(buf.getvalue(), "image/jpeg", img.size[0], img.size[1], rw, rh, scale, fg)

    async def _ui_xml(self, args: dict) -> bytes:
        """The UI tree as uiautomator XML: from the helper (accessibility services stay bound), or from
        `uiautomator dump` itself when engine is uiautomator (it unbinds them for a moment)."""
        engine = args.get("engine", "helper")
        if engine not in UI_ENGINES:
            raise CommandError("invalid_args", f"engine must be one of {', '.join(UI_ENGINES)}")
        if engine == "uiautomator":
            return await self._dump_ui(_if_foreground(args))
        return (await self._helper(args, "ui")).encode("utf-8")

    async def cmd_ui(self, args: dict) -> dict:
        packages = _if_foreground(args)
        xml_bytes = await self._ui_xml(args)
        fg, _ = await self.foreground_and_wake()
        # With if_foreground: if the app in front changed during the dump, discard it.
        if packages is not None and fg not in packages:
            raise CommandError("foreground_mismatch", f"foreground changed to {fg} during the UI dump")
        elements = parse_ui(xml_bytes)
        return {"foreground": fg, "count": len(elements), "elements": elements}

    async def cmd_tap(self, args: dict) -> dict:
        scale = _scale(args)
        x, y = _coord(args, "x", scale), _coord(args, "y", scale)
        await self._guarded(f"input tap {x} {y}", args)
        return {"x": x, "y": y}

    async def cmd_swipe(self, args: dict) -> dict:
        scale = _scale(args)
        x1, y1 = _coord(args, "x1", scale), _coord(args, "y1", scale)
        x2, y2 = _coord(args, "x2", scale), _coord(args, "y2", scale)
        ms = args.get("ms", 300)
        if not _is_number(ms) or ms < 0:
            raise CommandError("invalid_args", "ms must be a number 0 or above")
        ms = round(ms)
        await self._guarded(f"input swipe {x1} {y1} {x2} {y2} {ms}", args)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}

    async def input_state(self) -> tuple[tuple[str, int] | None, bool | None]:
        """((package, inputType) of the focused text field, keyboard showing), read without uiautomator."""
        out = await self.adb.shell("dumpsys input_method | grep -E -A12 'curEditorInfo:|mInputShown='")
        return parse_editor_info(out), parse_input_shown(out)

    async def text_field(self) -> tuple[str, int] | None:
        return (await self.input_state())[0]

    async def cmd_type(self, args: dict) -> dict:
        """Type text as keys (`input text`), or paste it through the clipboard: what keys cannot type
        (beyond ASCII, emoji, other control characters) is pasted unless the method says otherwise."""
        text, method = args.get("text"), args.get("method")
        if not isinstance(text, str) or not text:
            raise CommandError("invalid_args", "text must be a non-empty string")
        if method is not None and method not in TYPE_METHODS:
            raise CommandError("invalid_args", f"method must be one of {', '.join(TYPE_METHODS)}")
        method = method or ("keys" if keys_can_type(text) else "paste")
        commands = type_commands(text) if method == "keys" else []  # validates
        # Typing with no field focused is the agent's call: the reply says what was focused.
        field = await self.text_field()
        if method == "paste":
            await self._helper_with_text(args, "paste", text)
        for command in commands:
            if self.stopped():  # a long text: the owner's stop file halts it between chunks
                raise CommandError("stopped", "stop file present")
            await self._guarded(command, args)
        return {
            "length": len(text),
            "method": method,
            "field_package": field[0] if field else None,
            "field_input_type": field[1] if field else None,
            "password_field": is_password_type(field[1]) if field else False,
        }

    async def cmd_gesture(self, args: dict) -> dict:
        """Several fingers at once (pinch, rotate, two-finger swipe): each pointer a path of [x, y] points."""
        scale = _scale(args)
        pointers = args.get("pointers")
        if not isinstance(pointers, list) or not 1 <= len(pointers) <= 10:
            raise CommandError("invalid_args", "pointers must be a list of 1 to 10 paths")
        paths = []
        for i, path in enumerate(pointers):
            if not isinstance(path, list) or not path or not all(
                isinstance(pt, list) and len(pt) == 2 and all(_is_number(v) for v in pt) for pt in path
            ):
                raise CommandError("invalid_args", f"pointer {i} must be a list of [x, y] points")
            paths.append([[round(x / scale, 1), round(y / scale, 1)] for x, y in path])
        ms = _number_arg(args, "ms", 300)
        await self._helper_with_text(args, "gesture", json.dumps({"pointers": paths, "ms": round(ms)}))
        return {"pointers": len(paths), "ms": round(ms)}

    async def cmd_clipboard_get(self, args: dict) -> dict:
        out = await self._helper(args, "clip-get")
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), None)
        if line is None:
            raise CommandError("helper_failed", out.strip()[-300:])
        return {"text": json.loads(line).get("text")}

    async def cmd_clipboard_set(self, args: dict) -> dict:
        """Put text on the clipboard; null empties it."""
        text = args.get("text")
        if text is None:
            await self._helper(args, "clip-clear")
            return {"length": 0, "cleared": True}
        if not isinstance(text, str):
            raise CommandError("invalid_args", "text must be a string, or null to empty the clipboard")
        await self._helper_with_text(args, "clip-set", text)
        return {"length": len(text)}

    async def cmd_audio(self, args: dict, channel: Channel) -> dict:
        """Record the phone's audio output (it goes quiet on the speaker meanwhile) or its microphone, as WAV."""
        seconds = _number_arg(args, "seconds", 10)
        if seconds <= 0:
            raise CommandError("invalid_args", "seconds must be above 0")
        source = args.get("source", "output")
        if source not in AUDIO_SOURCES:
            raise CommandError("invalid_args", f"source must be one of {', '.join(AUDIO_SOURCES)}")
        local = _local_temp(".wav")
        try:
            await self._helper(args, "audio", f"{seconds:g}", source, AUDIO_REMOTE)
            await self.adb.run("pull", AUDIO_REMOTE, local)
            size = await channel.send_file(local, "audio/wav", {"X-Audio-Source": source})
        finally:
            _remove(local)
            await self.adb.shell(f"rm -f {AUDIO_REMOTE}")
        return {"seconds": seconds, "source": source, "bytes": size}

    async def cmd_key(self, args: dict) -> dict:
        keycode = resolve_key(args.get("key"))
        longpress = args.get("longpress", False)
        if not isinstance(longpress, bool):
            raise CommandError("invalid_args", "longpress must be true or false")
        await self._guarded(f"input keyevent {'--longpress ' if longpress else ''}{keycode}", args)
        return {"key": args.get("key")}

    async def cmd_launch(self, args: dict) -> dict:
        package = args.get("package")
        if not isinstance(package, str) or not _PACKAGE_RE.match(package):
            raise CommandError("invalid_args", "package must be an Android package name")
        out = await self.adb.shell(
            f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {shlex.quote(package)}"
        )
        component = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith(package + "/")), None)
        if component:
            _check_am(await self._guarded(f"am start -W -n {shlex.quote(component)} || true", args))
        else:
            out = await self._guarded(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", args)
            if "Events injected: 1" not in out:
                raise CommandError("launch_failed", out.strip()[-200:])
        return {"package": package}


    async def cmd_open(self, args: dict) -> dict:
        """Open a URL with whatever handles it (a browser, a mailto: link in the mail app, a deep link)."""
        url = _string_arg(args, "url")
        package = _package_arg(args)
        command = f"am start -W -a android.intent.action.VIEW -d {shlex.quote(url)}"
        if package:
            command += f" -p {shlex.quote(package)}"
        _check_am(await self._guarded(command + " || true", args))
        return {"url": url, "package": package}

    async def cmd_intent(self, args: dict) -> dict:
        """Start an activity (or send a broadcast) from an intent's parts, through `am`."""
        broadcast = args.get("broadcast", False)
        if not isinstance(broadcast, bool):
            raise CommandError("invalid_args", "broadcast must be true or false")
        parts = ["am", "broadcast"] if broadcast else ["am", "start", "-W"]
        given = False
        for field, flag in (("action", "-a"), ("data", "-d"), ("type", "-t"), ("component", "-n")):
            value = _string_arg(args, field, required=False)
            if value is not None:
                parts += [flag, shlex.quote(value)]
                given = True
        package = _package_arg(args)
        if package:
            parts += ["-p", package]
            given = True
        categories = args.get("categories", [])
        if not isinstance(categories, list) or not all(isinstance(c, str) and c for c in categories):
            raise CommandError("invalid_args", "categories must be a list of strings")
        for c in categories:
            parts += ["-c", shlex.quote(c)]
        flags = args.get("flags")
        if flags is not None:
            if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
                raise CommandError("invalid_args", "flags must be a whole number")
            parts += ["-f", str(flags)]
        extras = args.get("extras", {})
        if not isinstance(extras, dict):
            raise CommandError("invalid_args", "extras must be an object")
        for key, value in extras.items():
            parts += _extra_args(key, value)
        if not given:
            raise CommandError("invalid_args", "give at least one of action, data, component or package")
        out = await self._guarded(" ".join(parts) + " || true", args)
        if not broadcast:
            _check_am(out)
        return {"output": out.strip()[-500:]}

    async def cmd_notifications(self, args: dict) -> dict:
        items = parse_notifications(await self.adb.shell("dumpsys notification --noredact"))
        return {"count": len(items), "notifications": items}

    async def cmd_statusbar(self, args: dict) -> dict:
        action = args.get("action")
        if action not in STATUSBAR_ACTIONS:
            raise CommandError("invalid_args", f"action must be one of {', '.join(STATUSBAR_ACTIONS)}")
        await self._guarded(f"cmd statusbar {STATUSBAR_ACTIONS[action]}", args)
        return {"action": action}

    async def _rotation(self) -> dict:
        out = (await self.adb.shell("settings get system accelerometer_rotation; settings get system user_rotation")).split()
        return {
            "auto": out[0] == "1" if out else None,
            "user_rotation": int(out[1]) if len(out) > 1 and out[1].isdigit() else None,
        }

    async def cmd_rotation(self, args: dict) -> dict:
        """Lock the screen to a quarter turn (0-3) or let it rotate automatically; the reply has the old setting."""
        rotation = args.get("rotation")
        if rotation != "auto" and (isinstance(rotation, bool) or rotation not in (0, 1, 2, 3)):
            raise CommandError("invalid_args", 'rotation must be 0, 1, 2 or 3 (quarter turns) or "auto"')
        previous = await self._rotation()
        if rotation == "auto":
            await self._guarded("settings put system accelerometer_rotation 1", args)
        else:
            await self._guarded(f"settings put system accelerometer_rotation 0 && settings put system user_rotation {rotation}", args)
        return {"rotation": rotation, "previous": previous}

    async def _frame(self):
        return frame_signature(await self.adb.exec_out("screencap -p"))

    async def cmd_wait(self, args: dict) -> dict:
        """Wait until the screen changes, stops changing, or an app comes to the front; met=false at the end of `seconds`."""
        kind = args.get("for")
        if kind not in WAIT_KINDS:
            raise CommandError("invalid_args", f"for must be one of {', '.join(WAIT_KINDS)}")
        seconds = _number_arg(args, "seconds", 10)
        interval = _number_arg(args, "interval_ms", 500) / 1000
        threshold = _number_arg(args, "threshold", 0.005)
        package = _package_arg(args)
        wanted = args.get("text")
        if kind == "text" and (not isinstance(wanted, str) or not wanted):
            raise CommandError("invalid_args", "waiting for text needs text, a non-empty string")
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + seconds

        def result(met: bool, **extra) -> dict:
            return {"met": met, "elapsed_ms": round((loop.time() - started) * 1000), **extra}

        if kind == "text":
            # The helper's UI tree: no accessibility service is unbound, however often it polls.
            while True:
                found = next((e for e in parse_ui(await self._ui_xml({"engine": "helper"}))
                              if wanted in e["text"] or wanted in e["desc"]), None)
                if found is not None:
                    return result(True, element=found)
                if loop.time() >= deadline:
                    return result(False)
                await asyncio.sleep(interval)
        if kind == "foreground":
            first, _ = await self.foreground_and_wake()
            fg = first
            while True:
                if (fg == package) if package else (fg != first):
                    return result(True, foreground=fg)
                if loop.time() >= deadline:
                    return result(False, foreground=fg)
                await asyncio.sleep(interval)
                fg, _ = await self.foreground_and_wake()
        reference = await self._frame()
        diff = 0.0
        while True:
            if loop.time() >= deadline:
                return result(False, diff=round(diff, 4))
            await asyncio.sleep(interval)
            frame = await self._frame()
            diff = frame_diff(reference, frame)
            if (kind == "change" and diff > threshold) or (kind == "stable" and diff <= threshold):
                return result(True, diff=round(diff, 4))
            if kind == "stable":
                reference = frame

    async def cmd_batch(self, args: dict) -> dict:
        """Run steps in order in one request; each gets its own deadline and the batch's if_foreground unless it sets one."""
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            raise CommandError("invalid_args", "steps must be a non-empty list")
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("cmd") not in BATCH_COMMANDS:
                raise CommandError("invalid_args", f"step {i}: cmd must be one of {', '.join(BATCH_COMMANDS)}")
            if not isinstance(step.get("args", {}), dict):
                raise CommandError("invalid_args", f"step {i}: args must be an object")
        stop_on_error = args.get("stop_on_error", True)
        if not isinstance(stop_on_error, bool):
            raise CommandError("invalid_args", "stop_on_error must be true or false")
        inherited = {"if_foreground": args["if_foreground"]} if "if_foreground" in args else {}
        results: list[dict] = []
        for step in steps:
            cmd, step_args = step["cmd"], step.get("args") or {}
            try:
                if cmd == "sleep":
                    ms = _number_arg(step_args, "ms", 0)
                    await asyncio.sleep(ms / 1000)
                    result = {"ms": ms}
                else:
                    result = await self.handle(cmd, {**inherited, **step_args})
                results.append({"cmd": cmd, "ok": True, "result": result})
            except CommandError as e:
                if e.code == "stopped":
                    raise
                results.append({"cmd": cmd, "ok": False, "error": e.code, "detail": e.detail[:300]})
                if stop_on_error:
                    break
        return {"results": results, "completed": len(results),
                "all_ok": len(results) == len(steps) and all(r["ok"] for r in results)}

    async def _stat(self, path: str) -> dict:
        out = await self.adb.shell(f"stat -c '%F|%s|%Y|%n' -- {shlex.quote(path)} 2>/dev/null || echo {MISSING_MARKER}")
        entry = next((e for e in map(parse_stat, out.splitlines()) if e), None)
        if entry is None:
            raise CommandError("not_found", f"{path} does not exist or cannot be read")
        return entry

    async def cmd_files(self, args: dict) -> dict:
        """A directory's entries (name, type, size, modified), or one file's."""
        path = _remote_path(args)
        q, fmt = shlex.quote(path), "'%F|%s|%Y|%n'"
        out = await self.adb.shell(
            f"if [ -d {q} ]; then cd {q} 2>/dev/null || {{ echo {MISSING_MARKER}; exit 0; }}; "
            f"for f in * .*; do case \"$f\" in .|..) continue;; esac; "
            f"[ -e \"$f\" ] || [ -L \"$f\" ] || continue; stat -c {fmt} -- \"$f\"; done; "
            f"else stat -c {fmt} -- {q} 2>/dev/null || echo {MISSING_MARKER}; fi"
        )
        if MISSING_MARKER in out:
            raise CommandError("not_found", f"{path} does not exist or cannot be read")
        entries = [e for e in map(parse_stat, out.splitlines()) if e]
        return {"path": path, "count": len(entries), "entries": entries}

    async def cmd_pull(self, args: dict, channel: Channel) -> dict:
        path = _remote_path(args)
        if (await self._stat(path))["type"] == "dir":
            raise CommandError("invalid_args", f"{path} is a directory; list it with /files")
        local = _local_temp(os.path.splitext(path)[1])
        try:
            await self.adb.run("pull", path, local)
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            size = await channel.send_file(local, mime, {"X-File-Path": path})
        finally:
            _remove(local)
        return {"path": path, "bytes": size}

    async def cmd_push(self, args: dict, channel: Channel) -> dict:
        path = _remote_path(args)
        local = _local_temp(os.path.splitext(path)[1])
        try:
            size = await channel.recv_file(local)
            await self.adb.run("push", local, path)
        finally:
            _remove(local)
        return {"path": path, "bytes": size}

    async def _end_recording(self) -> list[str]:
        """Stop the recording loop (if it runs) and return its segment files on the phone, in order."""
        out = await self.adb.shell(
            f"touch {RECORD_STOP}; [ -e {RECORD_CHILD} ] && kill -INT $(cat {RECORD_CHILD}) 2>/dev/null; "
            f"n=0; while [ -e {RECORD_PID} ] && [ $n -lt 150 ]; do sleep 0.2; n=$((n+1)); done; "
            f"rm -f {RECORD_STOP}; ls {RECORD_PREFIX}*.mp4 2>/dev/null; true"
        )
        files = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(RECORD_PREFIX) and ln.strip().endswith(".mp4")]
        return sorted(files, key=lambda f: int(re.sub(r"\D", "", f[len(RECORD_PREFIX):]) or 0))

    async def end_recording(self) -> None:
        """Stop any recording and delete it: the owner's kill switches and a closed connection end capture."""
        remotes = await self._end_recording()
        if remotes:
            await self.adb.shell("rm -f " + " ".join(shlex.quote(r) for r in remotes))
            log.warning("stopped a screen recording (%d segment(s)) as the connection closed", len(remotes))

    async def cmd_record_start(self, args: dict) -> dict:
        """Start recording the screen in the background, for at most `seconds`; /record/stop returns the video."""
        seconds, size, bit_rate = _record_args(args)
        for f in await self._end_recording():  # a recording left running or unfetched is dropped
            await self.adb.shell(f"rm -f {shlex.quote(f)}")
        opts = f"--bit-rate {bit_rate}" + (f" --size {size}" if size else "")
        # One screenrecord per segment (each is capped at RECORD_SEGMENT_S), until the time is up or the stop file appears.
        loop = (
            f"echo $$ > {RECORD_PID}; end=$(( $(date +%s) + {math.ceil(seconds)} )); i=0; "
            f"while [ $(date +%s) -lt $end ] && [ ! -e {RECORD_STOP} ]; do left=$(( end - $(date +%s) )); "
            f"[ $left -gt {RECORD_SEGMENT_S} ] && left={RECORD_SEGMENT_S}; "
            f"screenrecord --time-limit $left {opts} {RECORD_PREFIX}$i.mp4 & echo $! > {RECORD_CHILD}; wait $!; "
            f"i=$((i+1)); done; rm -f {RECORD_CHILD} {RECORD_PID}"
        )
        # Detached from this adb shell (its own session), so it outlives the call.
        await self._guarded(f"{{ setsid nohup sh -c {shlex.quote(loop)} >/dev/null 2>&1 </dev/null & }}", args)
        return {"recording": True, "max_seconds": seconds}

    async def cmd_record_stop(self, args: dict, channel: Channel) -> dict:
        """Stop the recording and send it: one MP4, or a zip of MP4 segments past RECORD_SEGMENT_S."""
        remotes = await self._end_recording()
        if not remotes:
            raise CommandError("not_found", "no recording: start one with /record/start")
        locals_: list[str] = []
        try:
            for i, remote in enumerate(remotes):
                locals_.append(_local_temp(f"_{i}.mp4"))
                await self.adb.run("pull", remote, locals_[-1])
            if len(locals_) == 1:
                size = await channel.send_file(locals_[0], "video/mp4", {"X-Segments": "1"})
            else:
                bundle = _local_temp(".zip")
                locals_.append(bundle)
                with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as z:
                    for i, path in enumerate(locals_[:-1]):
                        z.write(path, f"segment_{i:03d}.mp4")
                size = await channel.send_file(bundle, "application/zip", {"X-Segments": str(len(remotes))})
        finally:
            _remove(*locals_)
            await self.adb.shell("rm -f " + " ".join(shlex.quote(r) for r in remotes))
        return {"segments": len(remotes), "bytes": size}

    async def cmd_record(self, args: dict, channel: Channel) -> dict:
        """Record for `seconds` and send the video: /record/start, a wait, /record/stop in one command."""
        started = await self.cmd_record_start(args)
        await asyncio.sleep(started["max_seconds"] + 1)
        return await self.cmd_record_stop(args, channel)


def _image_result(data: bytes, mime: str, w: int, h: int, rw: int, rh: int, scale: float, fg: str) -> dict:
    return {
        "mime": mime,
        "data": base64.b64encode(data).decode("ascii"),
        "width": w,
        "height": h,
        "real_width": rw,
        "real_height": rh,
        "scale": scale,
        "foreground": fg,
    }


def _png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _log_args(cmd: str, args: object) -> object:
    """Args with typed text replaced by its length, and an intent's extra values by their names."""
    if not isinstance(args, dict):
        return args
    if cmd in ("type", "clipboard_set"):
        text = args.get("text")
        return {"length": len(text) if isinstance(text, str) else "?"}
    if cmd == "batch" and isinstance(args.get("steps"), list):
        steps = [{"cmd": s.get("cmd"), "args": _log_args(str(s.get("cmd")), s.get("args"))} if isinstance(s, dict) else s
                 for s in args["steps"]]
        return {**args, "steps": steps}
    if cmd == "intent" and isinstance(args.get("extras"), dict):
        return {**args, "extras": sorted(args["extras"])}
    return args


def summarise(cmd: str, args: dict) -> str:
    """Log-safe summary of a command. Typed text is never logged."""
    if cmd == "type":
        text = args.get("text") if isinstance(args, dict) else None
        return f"type length={len(text) if isinstance(text, str) else '?'}"
    return f"{cmd} {json.dumps(_log_args(cmd, args), sort_keys=True, default=str)}"


async def dispatch(controller: PhoneController, raw: str | bytes, channel: Channel | None = None) -> dict | None:
    """Turn one relay message into a reply. Returns None for messages that need no reply."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("ignoring non-JSON message")
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("id"), str):
        return None
    cmd, args = msg.get("cmd"), msg.get("args") or {}
    log.info("command %s", summarise(str(cmd), args))
    try:
        result = await controller.handle(str(cmd), args, channel)
        return {"id": msg["id"], "ok": True, "result": result}
    except CommandError as e:
        log.info("refused/failed %s: %s", cmd, e.code)
        return {"id": msg["id"], "ok": False, "error": e.code, "detail": e.detail[:300]}
    except Exception as e:  # never let one bad command kill the connection
        log.exception("unexpected error in %s", cmd)
        return {"id": msg["id"], "ok": False, "error": "device_error", "detail": type(e).__name__}


# ---- relay connection ----


def _ws_connect(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"phone-relay-client/{VERSION}"}
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:
        from websockets import connect  # legacy API

        return connect(url, extra_headers=headers, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=None)
    # max_size=None: no cap on a command's size (the library's default is 1 MiB; the relay's own limit is 32 MiB)
    return connect(url, additional_headers=headers, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=None)


def _rejected_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        return response.status_code
    return getattr(exc, "status_code", None)


async def relay_loop(controller: PhoneController, url: str, token: str) -> int:
    backoff = 1.0
    while True:
        if controller.stopped():
            log.warning("stop file present; exiting")
            return 0
        try:
            log.info("connecting to relay")
            async with _ws_connect(url, token) as ws:
                log.info("connected")
                backoff = 1.0
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "client": "termux-adb",
                            "version": VERSION,
                        }
                    )
                )
                watcher = asyncio.create_task(_watch_stop(controller, ws))
                try:
                    await serve(controller, ws)
                finally:
                    watcher.cancel()
            log.info("connection closed")
        except Exception as e:
            status = _rejected_status(e)
            if status == 401:
                log.error("relay rejected DEVICE_TOKEN (401); fix .env and restart")
                return 2
            if status == 403:
                log.warning("relay is disabled by the owner (403); retrying in about a minute")
                backoff = 60.0
            else:
                log.warning("connection error: %s", type(e).__name__)
        if controller.stopped():
            log.warning("stop file present; exiting")
            return 0
        delay = backoff * (0.5 + random.random())
        log.info("reconnecting in %.1fs", delay)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, 60.0)


async def serve(controller: PhoneController, ws) -> None:
    """Run the relay's commands one at a time, in order, while reading the socket.

    Reading goes on while a command runs, so when the connection closes (the owner's
    disable, the stop file, a network drop) the running command is cancelled, and with
    it the adb call it was waiting on, instead of carrying on unseen. It also lets a
    running command receive its side messages (file chunks, acks) through its Channel.
    """
    commands: asyncio.Queue = asyncio.Queue()
    channels: dict[str, Channel] = {}

    async def worker() -> None:
        while True:
            raw, channel = await commands.get()  # one command at a time: adb actions are serialised
            try:
                reply = await dispatch(controller, raw, channel)
            finally:
                if channel is not None:
                    channels.pop(channel.id, None)
            if reply is not None:
                await ws.send(json.dumps(reply))
            if controller.stopped():
                await ws.close(1000, "stop file")

    task = asyncio.create_task(worker())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                msg = None
            if isinstance(msg, dict) and msg.get("type") in SIDE_TYPES:
                channel = channels.get(msg.get("id"))
                if channel is not None:  # side messages for a finished command are dropped
                    channel.inbox.put_nowait(msg)
                continue
            channel = None
            if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                # Registered now, before the command runs, so chunks sent right behind it are kept.
                channel = channels[msg["id"]] = Channel(msg["id"], ws.send)
            commands.put_nowait((raw, channel))
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # a reply that could not be sent: the connection is gone anyway
            log.warning("command worker stopped", exc_info=True)
        # A recording runs detached on the phone: the owner's disable or stop file (both close the
        # connection) must end it too, as must a drop, since nobody is left to fetch it.
        try:
            await asyncio.wait_for(controller.end_recording(), 45)
        except (CommandError, asyncio.TimeoutError):
            log.warning("could not check for a screen recording to stop")


async def _watch_stop(controller: PhoneController, ws) -> None:
    while True:
        await asyncio.sleep(2)
        if controller.stopped():
            log.warning("stop file detected; closing connection")
            await ws.close(1000, "stop file")
            return


# ---- self-test ----


async def selftest(controller: PhoneController) -> int:
    ok = True
    print(f"phone_client {VERSION} self-test")
    raw = await controller.adb.shell(
        "dumpsys window | grep -E 'mCurrentFocus=|mFocusedApp='; dumpsys power | grep -E 'mWakefulness='"
    )
    print("raw focus/wake lines (check these look right on your phone):")
    for line in raw.strip().splitlines():
        print("   ", line.strip())
    state = await controller.cmd_state({})
    print("state:", json.dumps(state))
    fg = state["foreground"]
    if not isinstance(fg, str) or not _PACKAGE_RE.match(fg) or "." not in fg:
        print("FAIL: no app has focus; unlock the phone and open an app, then re-run the self-test")
        return 1
    # The if_foreground guard with a harmless action: proves the on-phone focus check works in this shell.
    probe = await controller.adb.shell(guarded("echo GUARD_OK", (fg,)))
    refused = await controller.adb.shell(guarded("echo GUARD_OK", ("com.example.not.in.front",)))
    if "GUARD_OK" in probe and REFUSED_MARKER in refused:
        print("if_foreground guard: passes for", fg, "and refuses another package")
    else:
        ok = False
        print("FAIL if_foreground guard:", probe.strip()[:200], "/", refused.strip()[:200])
    try:
        version = (await controller._helper({}, "version")).strip()
        print("helper: version", version or "?", "(app_process works)")
    except CommandError as e:
        ok = False
        print("FAIL helper:", e)
    field = await controller.text_field()
    print("text field:", "none" if field is None else f"package={field[0]} inputType={field[1]:#x} password={is_password_type(field[1])}")
    try:
        shot = await controller.cmd_screenshot({"scale": 0.5})
        ext = "jpg" if shot["mime"] == "image/jpeg" else "png"
        path = f"selftest_screenshot.{ext}"
        with open(path, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(
            f"screenshot: {shot['width']}x{shot['height']} (screen {shot['real_width']}x{shot['real_height']}), "
            f"saved to {path}"
        )
    except CommandError as e:
        ok = False
        print("FAIL screenshot:", e)
    print("self-test", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# ---- config ----


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    mode = os.stat(path).st_mode
    if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):  # Windows has no POSIX modes
        log.warning("%s is readable by others; run: chmod 600 %s", path, path)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def main(argv: list[str] | None = None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=os.path.join(here, ".env"), help="path to .env (default: next to this script)")
    parser.add_argument("--selftest", action="store_true", help="check adb and the phone, do not connect")
    parser.add_argument("--stop-file", default=None, help=f"kill-switch file (default {DEFAULT_STOP_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true")
    opts = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)

    env = {**load_env(opts.env), **{k: v for k, v in os.environ.items() if k in ("RELAY_URL", "DEVICE_TOKEN", "ADB_SERIAL", "ADB_BIN", "STOP_FILE")}}
    adb = Adb(env.get("ADB_BIN") or "adb", env.get("ADB_SERIAL") or None)
    controller = PhoneController(adb, stop_file=opts.stop_file or env.get("STOP_FILE") or DEFAULT_STOP_FILE)

    if opts.selftest:
        # adb calls have no timeout of their own; a hung one stops the self-test after two minutes
        return asyncio.run(asyncio.wait_for(selftest(controller), 120))

    url, token = env.get("RELAY_URL"), env.get("DEVICE_TOKEN")
    if not url or not token:
        log.error("RELAY_URL and DEVICE_TOKEN must be set in %s", opts.env)
        return 2
    if not url.startswith("wss://") and not url.startswith("ws://127.0.0.1") and not url.startswith("ws://localhost"):
        log.error("RELAY_URL must use wss:// (plain ws:// is only allowed for localhost testing)")
        return 2
    try:
        return asyncio.run(relay_loop(controller, url, token))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
