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

Chrome's pages are reached through Chrome DevTools: `adb forward` maps Chrome's
DevTools socket to a local port for the length of one command, then removes it.

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
import struct
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

VERSION = "0.7.0"

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
# Default deadline for installing an APK: Android compiles it before it replies.
INSTALL_TIMEOUT_S = 120
APK_MIME = "application/vnd.android.package-archive"
# Between two commands' output in one shell call.
SPLIT_MARKER = "PHONE_RELAY_SPLIT"
DEFAULT_LOG_LINES = 500
LOG_LEVELS = ("V", "D", "I", "W", "E", "F")
LOG_BUFFERS = ("main", "system", "crash", "events", "radio", "kernel", "default", "all")
_LOG_TAG_RE = re.compile(r"^[\w.$-]{1,64}$")
_LOG_TIME_RE = re.compile(r"^(\d{4}-)?\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,9})?$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*/[A-Za-z0-9_.$]+$")
# No part may start with "-", so none is read as an option of cat, rm or tar.
_DATA_PATH_RE = re.compile(r"^[\w.@+ ][\w.@+ -]*(/[\w.@+ ][\w.@+ -]*)*/?$")
_CONTENT_URI_RE = re.compile(r"^content://[\w.-]+(/[\w.%:@+-]*)*$")
_COLUMN_RE = re.compile(r"^\w{1,64}$")
_SORT_RE = re.compile(r"^[\w ,]{1,200}$")
SIGNALS = ("STOP", "CONT", "KILL")
NIGHT_MODES = ("yes", "no", "auto")
# Never force-stopped: the system's own, and what runs this client or the app.
STOP_REFUSED = ("android", "com.android.systemui", "com.android.phone", "com.termux", "com.thomaswcode.phonerelay")
_SETTING_KEY_RE = re.compile(r"^[\w.:-]{1,100}$")
SETTINGS_NAMESPACES = ("system", "secure", "global")
# PackageManager's COMPONENT_ENABLED_STATE_* by number, as `dumpsys package` prints it (enabled=N).
ENABLED_STATES = {0: "default", 1: "enabled", 2: "disabled", 3: "disabled_user", 4: "disabled_until_used"}
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
                  "clipboard_get", "clipboard_set", "chrome_tabs", "chrome_eval", "chrome_cdp", "chrome_new",
                  "chrome_close", "chrome_activate", "install", "accessibility", "logcat", "package", "settings",
                  "run_as_files", "content", "run_as_delete", "run_as_kill", "night_mode", "force_stop", "sleep")
# Commands that run their action inside the if_foreground guard themselves (in the same shell command).
# Any other command given if_foreground is checked against the foreground before it starts.
SELF_GUARDED = ("screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent", "statusbar",
                "rotation", "record_start", "record", "batch", "gesture", "clipboard_get", "clipboard_set", "audio",
                "install", "night_mode", "force_stop")
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
# Chrome DevTools: the abstract socket Chrome listens on (only the shell user, i.e. adb, can connect).
DEVTOOLS_SOCKET = "chrome_devtools_remote"
_DEVTOOLS_SOCKET_RE = re.compile(r"^[\w.-]{1,100}$")
_CDP_METHOD_RE = re.compile(r"^[A-Za-z]+\.[A-Za-z]+$")
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

    async def run(self, *args: str, timeout: float | None = None, stdin: str | None = None) -> bytes:
        """One adb call. No timeout by default: the command's deadline (PhoneController.handle) cancels it.
        `stdin` is a local file read as the command's standard input (`adb exec-in`), never whole into memory."""
        # A .py "adb" (the tests' fake) runs with this Python: no shell wrapper, so it works on Windows too.
        cmd = [sys.executable, self.adb_bin] if self.adb_bin.endswith(".py") else [self.adb_bin]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        source = open(stdin, "rb") if stdin is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=source,
            )
        except FileNotFoundError as e:
            raise CommandError("adb_failed", f"adb not found: {self.adb_bin}") from e
        finally:
            if source is not None:
                source.close()  # the child has its own copy of the file's descriptor
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

    async def exec_in(self, command: str, path: str, timeout: float | None = None) -> bytes:
        """`adb exec-in`: the local file at `path` is the command's standard input."""
        return await self.run("exec-in", command, timeout=timeout, stdin=path)


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


# ---- packages, accessibility, logcat and settings (pure, unit-tested) ----

_PACKAGE_FIELD_RES = {
    "version_code": re.compile(r"^\s*versionCode=(\d+)", re.MULTILINE),
    "version_name": re.compile(r"^\s*versionName=(.*)$", re.MULTILINE),
    "first_install_time": re.compile(r"^\s*firstInstallTime=(.+)$", re.MULTILINE),
    "last_update_time": re.compile(r"^\s*lastUpdateTime=(.+)$", re.MULTILINE),
    "code_path": re.compile(r"^\s*codePath=(\S+)", re.MULTILINE),
}
_PACKAGE_FLAGS_RE = re.compile(r"^\s*(?:pkg)?[Ff]lags=\[([^\]]*)\]", re.MULTILINE)
_USER0_ENABLED_RE = re.compile(r"^\s*User 0:.*?\benabled=(\d+)", re.MULTILINE)


def parse_package(dumpsys: str, name: str) -> dict | None:
    """What `dumpsys package NAME` says of an installed package, or None if it is not installed.

    Reads the first `Package [NAME]` block (an updated system app has a second, under "Hidden
    system packages"). Times are the phone's local time as it prints them ("2026-11-09 06:58:42"),
    which logcat's -t takes. The signing certificate is not in the dump (see apk_signers).
    """
    lines = dumpsys.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith(f"Package [{name}]")), None)
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = []
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        block.append(ln)
    text = "\n".join(block)
    found = {k: (m.group(1).strip() if (m := r.search(text)) else None) for k, r in _PACKAGE_FIELD_RES.items()}
    flags = _PACKAGE_FLAGS_RE.search(text)
    state = _USER0_ENABLED_RE.search(text)
    code_path = found.pop("code_path")
    return {
        "name": name,
        "version_name": found["version_name"],
        "version_code": int(found["version_code"]) if found["version_code"] else None,
        "first_install_time": found["first_install_time"],
        "last_update_time": found["last_update_time"],
        "debuggable": bool(flags) and "DEBUGGABLE" in flags.group(1).split(),
        "enabled_state": ENABLED_STATES.get(int(state.group(1)), state.group(1)) if state else None,
        "apk_path": f"{code_path}/base.apk" if code_path and not code_path.endswith(".apk") else code_path,
    }


def full_component(component: str) -> str:
    """package/.Class as package/package.Class: Android's lists name a component either way."""
    pkg, _, cls = component.partition("/")
    return f"{pkg}/{pkg}{cls}" if cls.startswith(".") else component


def parse_accessibility(enabled_setting: str, dumpsys: str) -> dict:
    """The accessibility services enabled (`settings get secure enabled_accessibility_services`) and,
    from `dumpsys accessibility`, those bound, as package/package.Class; `missing` is enabled but not bound.

    The bound list runs over several lines, from "Bound services:" to "Enabled services:", each entry
    Service[label=..., id=package/.Class, ...] (as Android 16 prints it). A dump that shows bound
    services without naming them gives bound and missing as None, not every service as unbound.
    """
    setting = enabled_setting.strip()
    enabled = [] if setting in ("", "null") else [full_component(c) for c in setting.split(":") if c]
    section, inside = [], False
    for line in dumpsys.splitlines():
        if not inside and "Bound services:" in line:
            inside = True
        if inside and "Enabled services:" in line:
            break
        if inside:
            section.append(line)
    text = "\n".join(section)
    bound = [full_component(c) for c in re.findall(r"\bid=([^,\s\]}]+)", text)]
    if not bound and "Service[" in text:
        return {"enabled": enabled, "bound": None, "missing": None}
    return {"enabled": enabled, "bound": bound, "missing": [c for c in enabled if c not in bound]}


def parse_install(out: str) -> str | None:
    """`pm install`'s error ("INSTALL_FAILED_...: its reason"), or None when it says Success."""
    if re.search(r"^Success\s*$", out, re.MULTILINE):
        return None
    m = re.search(r"Failure \[([A-Z_]+)(?::\s*(.*?))?\]\s*$", out, re.MULTILINE)
    if m:
        return m.group(1) + (f": {m.group(2)}" if m.group(2) else "")
    return out.strip()[-300:] or "pm install printed nothing"


def parse_settings(out: str) -> tuple[str | None, str | None]:
    """(the setting's value, None when unset; dark mode: yes, no, auto, ...) from
    `settings get NS KEY; echo SPLIT_MARKER; cmd uimode night`."""
    value, _, night = out.partition(SPLIT_MARKER)
    value = value.rstrip("\r\n")
    m = re.search(r"Night mode:\s*(\S+)", night)
    return (None if value == "null" else value), (m.group(1) if m else None)


def logcat_command(args: dict) -> tuple[str, int]:
    """The read-only `logcat -d` for /logcat's arguments, and its line cap. One line more than the cap
    is read, to tell whether the cap cut the log."""
    tags = args.get("tag")
    if tags is not None:
        tags = [tags] if isinstance(tags, str) else tags
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and _LOG_TAG_RE.match(t) for t in tags):
            raise CommandError("invalid_args", "tag must be a log tag or a list of them")
    level = args.get("level")
    if level is not None and level not in LOG_LEVELS:
        raise CommandError("invalid_args", f"level must be one of {', '.join(LOG_LEVELS)}")
    buffer = args.get("buffer")
    if buffer is not None and buffer not in LOG_BUFFERS:
        raise CommandError("invalid_args", f"buffer must be one of {', '.join(LOG_BUFFERS)}")
    lines = args.get("lines", DEFAULT_LOG_LINES)
    if not isinstance(lines, int) or isinstance(lines, bool) or lines <= 0:
        raise CommandError("invalid_args", "lines must be a whole number above 0")
    parts = ["logcat", "-d"]
    if buffer:
        parts += ["-b", buffer]
    since = args.get("since")
    if since is not None:
        if _is_number(since) and since > 0:
            # Seconds ago by the phone's own clock, in logcat's seconds.milliseconds form
            parts += ["-t", f"\"$(( $(date +%s) - {math.ceil(since)} )).000\""]
        elif isinstance(since, str) and _LOG_TIME_RE.match(since):
            parts += ["-t", shlex.quote(since if "." in since else since + ".000")]
        else:
            raise CommandError("invalid_args", 'since must be seconds ago or a time, e.g. "2026-11-09 06:58:42"')
    if tags:
        parts += ["-s"] + [shlex.quote(f"{t}:{level or 'V'}") for t in tags]  # a tag may hold $
    elif level:
        parts.append(f"'*:{level}'")
    return " ".join(parts) + f" 2>&1 | tail -n {lines + 1}", lines


def logcat_result(out: str, lines: int) -> dict:
    got = [ln.rstrip("\r") for ln in out.splitlines()]
    truncated = len(got) > lines
    got = got[-lines:] if truncated else got
    return {"count": len(got), "truncated": truncated, "lines": got}


def data_path(args: dict, required: bool = True, file: bool = False) -> str | None:
    """A path inside an app's own data folder (run-as's working folder): relative, with no "..";
    with `file`, a file's (no trailing /)."""
    path = args.get("path")
    if path is None and not required:
        return None
    if not isinstance(path, str) or not _DATA_PATH_RE.match(path) or ".." in path.split("/"):
        raise CommandError("invalid_args", "path must be relative to the app's data folder, e.g. files/plan.json, with no ..")
    if file and path.endswith("/"):
        raise CommandError("invalid_args", "path must name a file, not a folder")
    return path


def content_command(args: dict) -> tuple[str, list[str] | None]:
    """`content query` for /content's arguments (read-only), and its projection's columns."""
    uri = args.get("uri")
    if not isinstance(uri, str) or not _CONTENT_URI_RE.match(uri):
        raise CommandError("invalid_args", "uri must be a content:// URI, e.g. content://com.android.calendar/events")
    parts = ["content", "query", "--uri", shlex.quote(uri)]
    projection = args.get("projection")
    if projection is not None:
        if not isinstance(projection, list) or not projection or not all(isinstance(c, str) and _COLUMN_RE.match(c) for c in projection):
            raise CommandError("invalid_args", "projection must be a list of column names")
        parts += ["--projection", ":".join(projection)]
    where = args.get("where")
    if where is not None:
        if not isinstance(where, str) or not where:
            raise CommandError("invalid_args", "where must be a non-empty string")
        parts += ["--where", shlex.quote(where)]
    sort = args.get("sort")
    if sort is not None:
        if not isinstance(sort, str) or not _SORT_RE.match(sort):
            raise CommandError("invalid_args", "sort must be columns, e.g. dtstart DESC")
        parts += ["--sort", shlex.quote(sort)]
    return " ".join(parts) + " 2>&1", projection


def parse_content(out: str, projection: list[str] | None) -> list[dict]:
    """`content query`'s rows ("Row: 0 a=1, b=x, y"), each as {column: value}; NULL is None.

    A value may hold ", " and newlines: with a projection the columns are found in their order,
    so only a value holding ", <next column>=" is cut short; without one, at each ", name=".
    The provider's errors go to stderr while `content` still exits 0, so they are looked for here.
    """
    if re.search(r"^(Error while accessing provider|Exception|java\.|\[ERROR\])", out, re.MULTILINE):
        raise CommandError("content_failed", out.strip()[:300])
    if out.strip() == "No result found." or not out.strip():
        return []
    records, current = [], None
    for line in out.splitlines():
        m = re.match(r"^Row: \d+ (.*)$", line)
        if m:
            current = m.group(1)
            records.append(current)
        elif records:
            records[-1] += "\n" + line
    rows = []
    for text in records:
        if projection:
            row, rest = {}, text
            for i, col in enumerate(projection):
                if not rest.startswith(col + "="):
                    raise CommandError("content_failed", f"unexpected row: {text[:200]}")
                rest = rest[len(col) + 1:]
                if i + 1 < len(projection):
                    cut = rest.find(f", {projection[i + 1]}=")
                    value, rest = (rest, "") if cut < 0 else (rest[:cut], rest[cut + 2:])
                else:
                    value = rest
                row[col] = None if value == "NULL" else value
        else:
            row = {}
            for pair in re.split(r", (?=\w+=)", text):
                k, _, value = pair.partition("=")
                row[k] = None if value == "NULL" else value
        rows.append(row)
    return rows


def is_tar(data: bytes) -> bool:
    """A tar archive's first header (ustar), not an error printed in its place."""
    return len(data) >= 512 and data[257:262] == b"ustar"


def part_path(path: str) -> tuple[str, str]:
    """(the folder, the hidden .part file beside `path`) an upload is written to before it replaces the file."""
    folder, _, name = path.rpartition("/")
    folder = folder or "."
    return folder, f"{folder}/.{name}.part"


def _string_pool(data: bytes, off: int) -> list[str]:
    """A binary XML string pool (ResStringPool): its strings, UTF-8 or UTF-16."""
    header_size, count = struct.unpack_from("<H", data, off + 2)[0], struct.unpack_from("<I", data, off + 8)[0]
    flags, strings_start = struct.unpack_from("<II", data, off + 16)
    utf8 = bool(flags & 0x100)
    out = []
    for i in range(count):
        pos = off + strings_start + struct.unpack_from("<I", data, off + header_size + 4 * i)[0]
        if utf8:
            for _ in range(2):  # the length in characters, then in bytes: each 1 or 2 bytes
                n = data[pos]
                pos += 1
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[pos]
                    pos += 1
            out.append(data[pos:pos + n].decode("utf-8", errors="replace"))
        else:
            n = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos)[0]
                pos += 2
            out.append(data[pos:pos + 2 * n].decode("utf-16-le", errors="replace"))
    return out


# android:versionCode and android:versionName by their resource ids (attribute names can be stripped).
_VERSION_CODE_ATTR, _VERSION_NAME_ATTR = 0x0101021B, 0x0101021C


def parse_apk_manifest(axml: bytes) -> dict:
    """package, version_code and version_name from an APK's AndroidManifest.xml (Android's binary XML):
    the attributes of its first element, <manifest>."""
    def u16(o):
        return struct.unpack_from("<H", axml, o)[0]

    def u32(o):
        return struct.unpack_from("<I", axml, o)[0]

    try:
        if u16(0) != 0x0003:
            raise ValueError("not binary XML")
        strings: list[str] = []
        res_ids: list[int] = []
        off = u16(2)
        while off + 8 <= len(axml):
            kind, header_size, size = u16(off), u16(off + 2), u32(off + 4)
            if size < 8:
                break
            if kind == 0x0001:
                strings = _string_pool(axml, off)
            elif kind == 0x0180:
                res_ids = [u32(off + header_size + 4 * i) for i in range((size - header_size) // 4)]
            elif kind == 0x0102:  # a start tag
                ext = off + header_size
                attr_start, attr_size, attr_count = u16(ext + 8), u16(ext + 10), u16(ext + 12)
                out = {"package": None, "version_code": None, "version_name": None}
                for i in range(attr_count):
                    a = ext + attr_start + i * attr_size
                    name_i, raw, kind_v, value = u32(a + 4), u32(a + 8), axml[a + 15], u32(a + 16)
                    name = strings[name_i] if name_i < len(strings) else ""
                    res = res_ids[name_i] if name_i < len(res_ids) else 0
                    text = strings[raw] if raw < len(strings) else (strings[value] if kind_v == 0x03 and value < len(strings) else None)
                    if name == "package" and not res:
                        out["package"] = text
                    elif res == _VERSION_CODE_ATTR or (not res and name == "versionCode"):
                        out["version_code"] = value if kind_v in (0x10, 0x11) else (int(text) if text and text.isdigit() else None)
                    elif res == _VERSION_NAME_ATTR or (not res and name == "versionName"):
                        out["version_name"] = text
                return out
            off += size
    except (struct.error, IndexError, ValueError) as e:
        raise CommandError("invalid_args", f"not an APK: its manifest could not be read ({e})") from e
    raise CommandError("invalid_args", "not an APK: its manifest has no <manifest>")


def apk_manifest(apk_path: str) -> dict:
    """parse_apk_manifest of a local APK file."""
    try:
        with zipfile.ZipFile(apk_path) as z:
            axml = z.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise CommandError("invalid_args", f"not an APK ({type(e).__name__}: {e})") from e
    return parse_apk_manifest(axml)


APK_SIG_MAGIC = b"APK Sig Block 42"
# APK Signature Scheme v3.1, v3 and v2 blocks, in the order Android prefers them.
_SIG_BLOCK_IDS = (0x1B93AD61, 0xF05368C0, 0x7109871A)


def central_directory_offset(tail: bytes) -> int | None:
    """Where a zip's central directory starts, from the end of the file (its end-of-central-directory record)."""
    pos = tail.rfind(b"PK\x05\x06")
    return None if pos < 0 or pos + 22 > len(tail) else struct.unpack_from("<I", tail, pos + 16)[0]


def _length_prefixed(data: bytes) -> list[bytes]:
    out, i = [], 0
    while i + 4 <= len(data):
        n = struct.unpack_from("<I", data, i)[0]
        out.append(data[i + 4:i + 4 + n])
        i += 4 + n
    return out


def apk_signers(block: bytes) -> list[str]:
    """The SHA-256 of each signer's certificate (hex), from an APK Signing Block (the v3.1, v3 or v2
    scheme's): what PackageManager reports as the app's signing certificate. [] for none."""
    pairs, values, i = block[8:-24], {}, 0
    while i + 12 <= len(pairs):
        n, kind = struct.unpack_from("<QI", pairs, i)
        values[kind] = pairs[i + 12:i + 8 + n]
        i += 8 + n
    scheme = next((values[k] for k in _SIG_BLOCK_IDS if k in values), None)
    if scheme is None:
        return []
    digests = []
    for signers in _length_prefixed(scheme)[:1]:
        for signer in _length_prefixed(signers):
            signed_data = _length_prefixed(signer)[0]
            certificates = _length_prefixed(signed_data)[1]
            first = _length_prefixed(certificates)[0]
            digest = hashlib.sha256(first).hexdigest()
            if digest not in digests:
                digests.append(digest)
    return digests


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
    if cmd in ("pull", "push", "record_stop", "package_apk", "run_as", "run_as_archive", "run_as_push"):
        return TRANSFER_TIMEOUT_S
    if cmd == "install":
        return INSTALL_TIMEOUT_S
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


def _list_command(path: str) -> str:
    """Shell command listing a directory's entries (or one file) as `stat -c '%F|%s|%Y|%n'` lines;
    MISSING_MARKER if the path does not exist or cannot be read."""
    q, fmt = shlex.quote(path), "'%F|%s|%Y|%n'"
    return (
        f"if [ -d {q} ]; then cd {q} 2>/dev/null || {{ echo {MISSING_MARKER}; exit 0; }}; "
        f"for f in * .*; do case \"$f\" in .|..) continue;; esac; "
        f"[ -e \"$f\" ] || [ -L \"$f\" ] || continue; stat -c {fmt} -- \"$f\"; done; "
        f"else stat -c {fmt} -- {q} 2>/dev/null || echo {MISSING_MARKER}; fi"
    )


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
        if cmd == "request":
            # Asking the owner needs something on the screen for them to answer; only the Android app has one.
            raise CommandError("unsupported", "only the Android app can ask the owner; ask them in your chat instead")
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
            "chrome_sockets": self.cmd_chrome_sockets,
            "chrome_tabs": self.cmd_chrome_tabs,
            "chrome_eval": self.cmd_chrome_eval,
            "chrome_cdp": self.cmd_chrome_cdp,
            "chrome_new": self.cmd_chrome_new,
            "chrome_close": self.cmd_chrome_close,
            "chrome_activate": self.cmd_chrome_activate,
            "install": self.cmd_install,
            "accessibility": self.cmd_accessibility,
            "logcat": self.cmd_logcat,
            "package": self.cmd_package,
            "run_as_files": self.cmd_run_as_files,
            "settings": self.cmd_settings,
            "content": self.cmd_content,
            "run_as_delete": self.cmd_run_as_delete,
            "run_as_kill": self.cmd_run_as_kill,
            "night_mode": self.cmd_night_mode,
            "force_stop": self.cmd_force_stop,
        }.get(cmd)
        # These stream bytes over the relay socket, so they need the command's channel.
        streaming = {
            "pull": self.cmd_pull,
            "push": self.cmd_push,
            "record": self.cmd_record,
            "record_stop": self.cmd_record_stop,
            "audio": self.cmd_audio,
            "package_apk": self.cmd_package_apk,
            "run_as": self.cmd_run_as,
            "run_as_archive": self.cmd_run_as_archive,
            "run_as_push": self.cmd_run_as_push,
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
            "client": "termux-adb",
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
        """Start an activity (or send a broadcast) from an intent's parts, through `am`.

        This client's `am` always runs as the shell user, so as_shell changes nothing here but is held
        to the same rule as in the app: only to a named component."""
        broadcast = args.get("broadcast", False)
        if not isinstance(broadcast, bool):
            raise CommandError("invalid_args", "broadcast must be true or false")
        as_shell = args.get("as_shell", False)
        if not isinstance(as_shell, bool):
            raise CommandError("invalid_args", "as_shell must be true or false")
        if as_shell and not (isinstance(args.get("component"), str) and _COMPONENT_RE.match(args["component"])):
            raise CommandError("invalid_args", "as_shell needs component, e.g. com.example.app/.debug.Command")
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
        out = await self.adb.shell(_list_command(path))
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


    # ---- releases: installing, and what is installed; accessibility, logs, settings; an app's own files ----

    async def _package(self, name: str) -> dict:
        """parse_package of the installed package; not_found if it is not installed."""
        info = parse_package(await self.adb.shell(f"dumpsys package {shlex.quote(name)}"), name)
        if info is None:
            raise CommandError("not_found", f"{name} is not installed")
        return info

    async def _read_range(self, path: str, offset: int, length: int) -> bytes:
        """`length` bytes of a file on the phone from `offset`, without reading the rest."""
        return await self.adb.exec_out(f"tail -c +{offset + 1} {shlex.quote(path)} | head -c {length}")

    async def _signers(self, apk: str) -> list[str] | None:
        """SHA-256 of the APK's signing certificates, read from its APK Signing Block (the end of the
        file only); None when it has none (a v1-only signature) or it cannot be read."""
        try:
            size = int((await self.adb.shell(f"stat -c %s {shlex.quote(apk)}")).strip())
            tail_len = min(size, 65557)  # the end-of-central-directory record, with the longest zip comment
            cd = central_directory_offset(await self._read_range(apk, size - tail_len, tail_len))
            if cd is None or cd < 32:
                return None
            footer = await self._read_range(apk, cd - 24, 24)
            if len(footer) != 24 or footer[8:] != APK_SIG_MAGIC:
                return None
            block_size = struct.unpack_from("<Q", footer)[0]
            block = await self._read_range(apk, cd - block_size - 8, block_size + 8)
            return apk_signers(block) or None
        except (ValueError, struct.error, IndexError) as e:
            log.warning("could not read %s's signing block: %s", apk, e)
            return None

    async def cmd_package(self, args: dict) -> dict:
        """What is installed of a package: its version, install and update times, signing certificate
        (SHA-256), whether it is debuggable, its enabled state and the installed APK's path."""
        name = _package_arg(args, "name")
        if name is None:
            raise CommandError("invalid_args", "name must be an Android package name")
        info = await self._package(name)
        info["signing_sha256"] = await self._signers(info["apk_path"]) if info["apk_path"] else None
        return info

    async def cmd_package_apk(self, args: dict, channel: Channel) -> dict:
        """The installed APK (its base.apk), streamed as a pull is."""
        name = _package_arg(args, "name")
        if name is None:
            raise CommandError("invalid_args", "name must be an Android package name")
        info = await self._package(name)
        local = _local_temp(".apk")
        try:
            await self.adb.run("pull", info["apk_path"], local)
            size = await channel.send_file(local, APK_MIME, {"X-Version-Code": str(info["version_code"]),
                                                             "X-File-Path": info["apk_path"]})
        finally:
            _remove(local)
        return {"name": name, "path": info["apk_path"], "version_code": info["version_code"], "bytes": size}

    async def cmd_install(self, args: dict) -> dict:
        """Install an APK already on the phone over the app that is there (`pm install -r`): never with
        -d (a downgrade), and nothing here uninstalls or clears data. The APK's own package must be the
        one named, read from its manifest first. Android's refusal (INSTALL_FAILED_...) is the error."""
        path = _remote_path(args)
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be the APK's Android package name")
        if (await self._stat(path))["type"] != "file":
            raise CommandError("invalid_args", f"{path} is not a file")
        local = _local_temp(".apk")
        try:
            await self.adb.run("pull", path, local)
            apk = apk_manifest(local)
        finally:
            _remove(local)
        if apk["package"] != package:
            raise CommandError("package_mismatch", f"{path} is {apk['package']}, not {package}")
        error = parse_install(await self._guarded(f"pm install -r {shlex.quote(path)} 2>&1 || true", args))
        if error:
            raise CommandError("install_failed", error)
        info = await self._package(package)
        return {"package": package, "version_name": info["version_name"], "version_code": info["version_code"]}

    async def cmd_accessibility(self, args: dict) -> dict:
        """The accessibility services enabled, those bound, and those enabled but not bound (missing)."""
        out = await self.adb.shell(
            f"settings get secure enabled_accessibility_services; echo {SPLIT_MARKER}; dumpsys accessibility")
        setting, _, dump = out.partition(SPLIT_MARKER)
        return parse_accessibility(setting, dump)

    async def cmd_logcat(self, args: dict) -> dict:
        """The log so far (`logcat -d`), read-only: by tag, from a time, from a level, at most `lines` (the last ones)."""
        command, lines = logcat_command(args)
        return logcat_result(await self.adb.shell(command), lines)

    async def cmd_settings(self, args: dict) -> dict:
        """One setting's value (null when unset), read-only, and dark mode (`cmd uimode night`)."""
        namespace, key = args.get("namespace"), args.get("key")
        if namespace not in SETTINGS_NAMESPACES:
            raise CommandError("invalid_args", f"namespace must be one of {', '.join(SETTINGS_NAMESPACES)}")
        if not isinstance(key, str) or not _SETTING_KEY_RE.match(key):
            raise CommandError("invalid_args", "key must be a setting's name, e.g. stay_on_while_plugged_in")
        value, night = parse_settings(await self.adb.shell(
            f"settings get {namespace} {key}; echo {SPLIT_MARKER}; cmd uimode night"))
        return {"namespace": namespace, "key": key, "value": value, "night_mode": night}

    async def _debuggable(self, package: str) -> None:
        """run-as reads only a debuggable app's files: anything else is refused here first, as not_debuggable."""
        if not (await self._package(package))["debuggable"]:
            raise CommandError("not_debuggable", f"{package} is not debuggable: run-as cannot read its files")

    async def cmd_run_as_files(self, args: dict) -> dict:
        """A folder in a debuggable app's own storage (`run-as`), as /files lists one."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        path = data_path(args, required=False) or "."
        await self._debuggable(package)
        out = await self.adb.shell(f"run-as {package} sh -c {shlex.quote(_list_command(path))} 2>&1")
        if MISSING_MARKER in out:
            raise CommandError("not_found", f"{path} does not exist in {package}'s data, or cannot be read")
        entries = [e for e in map(parse_stat, out.splitlines()) if e]
        return {"package": package, "path": path, "count": len(entries), "entries": entries}

    async def cmd_run_as(self, args: dict, channel: Channel) -> dict:
        """A file in a debuggable app's own storage (`run-as PACKAGE cat PATH`), read-only, streamed as a pull is."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        path = data_path(args)
        await self._debuggable(package)
        out = await self.adb.shell(
            f"run-as {package} stat -c '%F|%s|%Y|%n' -- {shlex.quote(path)} 2>/dev/null || echo {MISSING_MARKER}")
        entry = next((e for e in map(parse_stat, out.splitlines()) if e), None)
        if entry is None:
            raise CommandError("not_found", f"{path} does not exist in {package}'s data, or cannot be read")
        if entry["type"] != "file":
            raise CommandError("invalid_args", f"{path} is a {entry['type']}; list a folder with /run-as/files")
        data = await self.adb.exec_out(f"run-as {package} cat {shlex.quote(path)}")
        if len(data) != entry["size"]:  # exec-out passes run-as's own errors on as if they were the file
            raise CommandError("device_error", f"read {len(data)} bytes of {entry['size']}: {data[-200:].decode(errors='replace')}")
        local = _local_temp(os.path.splitext(path)[1])
        try:
            with open(local, "wb") as f:
                f.write(data)
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            size = await channel.send_file(local, mime, {"X-File-Path": path, "X-Package": package})
        finally:
            _remove(local)
        return {"package": package, "path": path, "bytes": size}

    async def cmd_run_as_archive(self, args: dict, channel: Channel) -> dict:
        """A folder of a debuggable app's own storage as a tar (`run-as PACKAGE tar -cf - PATH`), read-only:
        path=files gives the data.tar Decrastination's keep_data.sh makes."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        path = data_path(args, required=False) or "."
        await self._debuggable(package)
        data = await self.adb.exec_out(f"run-as {package} tar -cf - {shlex.quote(path)}")
        if not is_tar(data):  # exec-out passes run-as's or tar's error on as if it were the archive
            raise CommandError("not_found", f"{path} could not be archived: {data[-200:].decode(errors='replace').strip()}")
        local = _local_temp(".tar")
        try:
            with open(local, "wb") as f:
                f.write(data)
            size = await channel.send_file(local, "application/x-tar", {"X-Package": package, "X-File-Path": path})
        finally:
            _remove(local)
        return {"package": package, "path": path, "bytes": size}

    async def cmd_run_as_push(self, args: dict, channel: Channel) -> dict:
        """Write a file into a debuggable app's own storage: to a hidden .part beside it, its size checked,
        then moved over the file, so a failed upload leaves the old file as it was."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        path = data_path(args, file=True)
        await self._debuggable(package)
        # mv would put the file inside a folder of that name: refused before anything is sent
        out = await self.adb.shell(
            f"run-as {package} stat -c '%F|%s|%Y|%n' -- {shlex.quote(path)} 2>/dev/null || echo {MISSING_MARKER}")
        entry = next((e for e in map(parse_stat, out.splitlines()) if e), None)
        if entry is not None and entry["type"] == "dir":
            raise CommandError("invalid_args", f"{path} is a folder; name the file to write")
        folder, part = part_path(path)
        local = _local_temp(os.path.splitext(path)[1])
        try:
            size = await channel.recv_file(local)
            script = f"mkdir -p {shlex.quote(folder)} && cat > {shlex.quote(part)}"
            await self.adb.exec_in(f"run-as {package} sh -c {shlex.quote(script)}", local)
        finally:
            _remove(local)
        written = (await self.adb.shell(f"run-as {package} stat -c %s -- {shlex.quote(part)} 2>&1")).strip()
        if written != str(size):
            await self.adb.shell(f"run-as {package} rm -f -- {shlex.quote(part)}")
            raise CommandError("device_error", f"wrote {written[:100]!r} bytes of {size}")
        out = await self.adb.shell(f"run-as {package} mv -f -- {shlex.quote(part)} {shlex.quote(path)} 2>&1")
        if out.strip():
            raise CommandError("device_error", out.strip()[-300:])
        return {"package": package, "path": path, "bytes": size}

    async def cmd_run_as_delete(self, args: dict) -> dict:
        """Delete one file (never a folder) in a debuggable app's own storage."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        path = data_path(args, file=True)
        await self._debuggable(package)
        out = await self.adb.shell(
            f"run-as {package} stat -c '%F|%s|%Y|%n' -- {shlex.quote(path)} 2>/dev/null || echo {MISSING_MARKER}")
        entry = next((e for e in map(parse_stat, out.splitlines()) if e), None)
        if entry is None:
            raise CommandError("not_found", f"{path} does not exist in {package}'s data, or cannot be read")
        if entry["type"] != "file":
            raise CommandError("invalid_args", f"{path} is a {entry['type']}; only a file is deleted")
        out = await self.adb.shell(f"run-as {package} rm -f -- {shlex.quote(path)} 2>&1")
        if out.strip():
            raise CommandError("device_error", out.strip()[-300:])
        return {"package": package, "path": path, "deleted": True}

    async def cmd_run_as_kill(self, args: dict) -> dict:
        """Send STOP, CONT or KILL to a debuggable app's main process, as the app's own user (run-as):
        to pause it while a store is edited by hand, and end it after (an app with a bound accessibility
        service is started again by Android, still enabled, unlike a force-stop)."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        signal = args.get("signal")
        if signal not in SIGNALS:
            raise CommandError("invalid_args", f"signal must be one of {', '.join(SIGNALS)}")
        await self._debuggable(package)
        pids = (await self.adb.shell(f"pidof {package}; true")).split()
        if not pids or not pids[0].isdigit():
            raise CommandError("not_found", f"{package} is not running")
        out = await self.adb.shell(f"run-as {package} kill -{signal} {pids[0]} 2>&1")
        if out.strip():
            raise CommandError("device_error", out.strip()[-300:])
        return {"package": package, "pid": int(pids[0]), "signal": signal}

    async def cmd_content(self, args: dict) -> dict:
        """A provider's rows (`content query`, read-only): the calendar's events, say."""
        command, projection = content_command(args)
        rows = parse_content(await self.adb.shell(command), projection)
        return {"count": len(rows), "rows": rows}

    async def _night_mode(self) -> str | None:
        m = re.search(r"Night mode:\s*(\S+)", await self.adb.shell("cmd uimode night"))
        return m.group(1) if m else None

    async def cmd_night_mode(self, args: dict) -> dict:
        """Dark mode on (yes), off (no) or automatic; the reply's `previous` is the old setting, to put back."""
        mode = args.get("mode")
        if mode not in NIGHT_MODES:
            raise CommandError("invalid_args", f"mode must be one of {', '.join(NIGHT_MODES)}")
        previous = await self._night_mode()
        await self._guarded(f"cmd uimode night {mode}", args)
        return {"night_mode": await self._night_mode(), "previous": previous}

    async def cmd_force_stop(self, args: dict) -> dict:
        """Force-stop an app a task opened (`am force-stop`). Refused for an app with an enabled
        accessibility service, which Android would drop from the enabled list, and for the system's own."""
        package = _package_arg(args)
        if package is None:
            raise CommandError("invalid_args", "package must be an Android package name")
        if package in STOP_REFUSED:
            raise CommandError("stop_refused", f"{package} is never force-stopped")
        enabled = parse_accessibility(await self.adb.shell("settings get secure enabled_accessibility_services"), "")["enabled"]
        if any(c.split("/")[0] == package for c in enabled):
            raise CommandError("stop_refused", f"{package} has an enabled accessibility service, which a force-stop would turn off")
        await self._package(package)
        await self._guarded(f"am force-stop {package}", args)
        return {"package": package}

    # ---- Chrome DevTools ----

    async def _devtools(self, args: dict, work):
        """Run `work(port)` with the DevTools socket forwarded to a local port, then remove the forward.

        `socket` picks another DevTools socket (a debuggable WebView's, say; /chrome/sockets lists them).
        A page can only be reached while Chrome is running: in the background it may list no tabs.
        """
        socket = args.get("socket", DEVTOOLS_SOCKET)
        if not isinstance(socket, str) or not _DEVTOOLS_SOCKET_RE.match(socket):
            raise CommandError("invalid_args", "socket must be a DevTools socket name, e.g. chrome_devtools_remote")
        out = (await self.adb.run("forward", "tcp:0", f"localabstract:{socket}")).decode(errors="replace").strip()
        if not out.isdigit():
            raise CommandError("devtools_unavailable", f"adb forward gave {out[:100]!r}")
        port = int(out)
        try:
            return await work(port)
        finally:
            try:
                await asyncio.wait_for(self.adb.run("forward", "--remove", f"tcp:{port}"), 10)
            except (CommandError, asyncio.TimeoutError):
                log.warning("could not remove the DevTools forward on port %d", port)

    @staticmethod
    async def _devtools_http(port: int, path: str, method: str = "GET"):
        """One request to DevTools' HTTP endpoint (/json/list, /json/new, ...), as JSON or text."""
        def fetch():
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                         headers={"User-Agent": f"phone-relay-client/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                raise CommandError("devtools_failed", f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
            except (urllib.error.URLError, OSError) as e:
                raise CommandError("devtools_unavailable", f"is Chrome running? {type(e).__name__}: {e}") from e
            try:
                return json.loads(body)
            except ValueError:
                return body

        return await asyncio.to_thread(fetch)

    @staticmethod
    def _tab_list(raw: object) -> list[dict]:
        if not isinstance(raw, list):
            raise CommandError("devtools_failed", "DevTools did not list targets")
        return [{"id": t.get("id"), "type": t.get("type"), "title": t.get("title"), "url": t.get("url")}
                for t in raw if isinstance(t, dict)]

    async def _pick_tab(self, port: int, args: dict) -> str:
        """The WebSocket path of the target named by `tab`: a tab id, an index into /chrome/tabs' pages,
        or "browser" for the browser itself. By default the first page (Chrome lists the one in front first)."""
        tab = args.get("tab", 0)
        if tab == "browser":
            # Desktop Chrome gives /devtools/browser/<id>; Chrome on Android plain /devtools/browser.
            version = await self._devtools_http(port, "/json/version")
            url = version.get("webSocketDebuggerUrl", "") if isinstance(version, dict) else ""
            if "/devtools/browser" not in url:
                raise CommandError("devtools_failed", "no browser target")
            return url[url.index("/devtools/browser"):]
        pages = [t for t in self._tab_list(await self._devtools_http(port, "/json/list")) if t["type"] == "page"]
        if isinstance(tab, int) and not isinstance(tab, bool):
            if not 0 <= tab < len(pages):
                raise CommandError("not_found", f"no tab {tab}: {len(pages)} open (Chrome in the background lists none)")
            return f"/devtools/page/{pages[tab]['id']}"
        if isinstance(tab, str) and re.fullmatch(r"[\w-]{1,100}", tab):
            if not any(t["id"] == tab for t in pages):
                raise CommandError("not_found", f"no tab with id {tab}")
            return f"/devtools/page/{tab}"
        raise CommandError("invalid_args", 'tab must be a tab id, an index, or "browser"')

    @staticmethod
    async def _cdp(port: int, path: str, method: str, params: dict) -> dict:
        """One DevTools protocol call on a target's WebSocket; its result, or devtools_failed with its error."""
        try:
            from websockets.asyncio.client import connect  # websockets >= 13
        except ImportError:
            from websockets import connect  # legacy API
        reply = None
        try:
            async with connect(f"ws://127.0.0.1:{port}{path}", max_size=None, open_timeout=15) as ws:
                await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
                async for raw in ws:  # events may come first; the reply carries id 1
                    msg = json.loads(raw)
                    if msg.get("id") == 1:
                        reply = msg
                        break
            # (raised out here, so the socket closes normally rather than with an error code)
        except Exception as e:  # refused, closed, a bad handshake: DevTools is not there for this target
            raise CommandError("devtools_unavailable", f"{type(e).__name__}: {e}") from e
        if reply is None:
            raise CommandError("devtools_failed", "the target closed before replying")
        if "error" in reply:
            raise CommandError("devtools_failed", json.dumps(reply["error"])[:300])
        return reply.get("result", {})

    async def cmd_chrome_sockets(self, args: dict) -> dict:
        """DevTools sockets on the phone: Chrome's, and those of any debuggable WebView."""
        out = await self.adb.shell("cat /proc/net/unix")
        names = sorted({ln.split("@", 1)[1].strip() for ln in out.splitlines() if "@" in ln and "devtools_remote" in ln})
        return {"sockets": names}

    async def cmd_chrome_tabs(self, args: dict) -> dict:
        tabs = await self._devtools(args, lambda port: self._devtools_http(port, "/json/list"))
        tabs = self._tab_list(tabs)
        return {"count": len(tabs), "tabs": tabs}

    async def cmd_chrome_eval(self, args: dict) -> dict:
        """JavaScript in a page (Runtime.evaluate): its value as JSON, or the exception it threw."""
        expression = _string_arg(args, "expression")
        await_promise = args.get("await_promise", True)
        if not isinstance(await_promise, bool):
            raise CommandError("invalid_args", "await_promise must be true or false")

        async def work(port):
            return await self._cdp(port, await self._pick_tab(port, args), "Runtime.evaluate", {
                "expression": expression, "returnByValue": True, "awaitPromise": await_promise, "userGesture": True})

        result = await self._devtools(args, work)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or "exception"
            return {"threw": True, "exception": str(text)[:2000]}
        value = result.get("result", {})
        return {"threw": False, "type": value.get("type"), "value": value.get("value"),
                "description": value.get("description")}

    async def cmd_chrome_cdp(self, args: dict) -> dict:
        """Any DevTools protocol method on a tab (or the browser): its result as DevTools returns it."""
        method = _string_arg(args, "method")
        if not _CDP_METHOD_RE.match(method):
            raise CommandError("invalid_args", "method must look like Domain.method, e.g. Page.navigate")
        params = args.get("params", {})
        if not isinstance(params, dict):
            raise CommandError("invalid_args", "params must be an object")

        async def work(port):
            return await self._cdp(port, await self._pick_tab(port, args), method, params)

        return {"result": await self._devtools(args, work)}

    async def cmd_chrome_new(self, args: dict) -> dict:
        """Open a tab with Target.createTarget on the browser target: Chrome on Android refuses
        DevTools' /json/new ("Could not create new page")."""
        url = _string_arg(args, "url")

        async def work(port):
            created = await self._cdp(port, await self._pick_tab(port, {"tab": "browser"}), "Target.createTarget", {"url": url})
            tab_id = created.get("targetId")
            if not tab_id:
                raise CommandError("devtools_failed", f"no tab created: {json.dumps(created)[:200]}")
            listed = next((t for t in self._tab_list(await self._devtools_http(port, "/json/list")) if t["id"] == tab_id), None)
            return listed or {"id": tab_id, "type": "page", "title": "", "url": url}

        return {"tab": await self._devtools(args, work)}

    async def _tab_action(self, args: dict, action: str) -> dict:
        async def work(port):
            tab_id = (await self._pick_tab(port, args)).rsplit("/", 1)[1]
            return tab_id, await self._devtools_http(port, f"/json/{action}/{tab_id}")

        tab_id, out = await self._devtools(args, work)
        return {"tab": tab_id, "reply": out if isinstance(out, str) else json.dumps(out)[:200]}

    async def cmd_chrome_close(self, args: dict) -> dict:
        if args.get("tab") == "browser":
            raise CommandError("invalid_args", "close takes a tab, not the browser")
        return await self._tab_action(args, "close")

    async def cmd_chrome_activate(self, args: dict) -> dict:
        if args.get("tab") == "browser":
            raise CommandError("invalid_args", "activate takes a tab, not the browser")
        return await self._tab_action(args, "activate")


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
    if cmd == "chrome_eval":
        expression = args.get("expression")
        return {**{k: v for k, v in args.items() if k != "expression"},
                "expression_length": len(expression) if isinstance(expression, str) else "?"}
    if cmd == "chrome_cdp" and isinstance(args.get("params"), dict):
        return {**args, "params": sorted(args["params"])}
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
