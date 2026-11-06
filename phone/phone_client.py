#!/usr/bin/env python3
"""Phone client for the phone relay (phase 1: adb, from Termux on the phone or from a computer).

Dials out to the relay over WSS, receives commands, runs them through adb and
replies. There is no app allowlist: every app, key and setting is reachable.
A command given `if_foreground` (a package or a list) runs in the same shell
command as a focus check, so it only runs while one of those apps has focus.

`ui` (the UI tree) runs `uiautomator dump`, which unbinds every accessibility
service on the phone for a moment. Nothing else uses it, including --selftest.

Usage:
    python phone_client.py --selftest      # check adb, foreground, input guard, text field, screenshot
    python phone_client.py                 # connect to the relay and serve commands
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import re
import shlex
import stat
import sys
import xml.etree.ElementTree as ET

VERSION = "0.3.0"

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

log = logging.getLogger("phone_client")


class CommandError(Exception):
    """A refusal or failure reported back to the relay as {ok: false, error: code}."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


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
    """Any length of ASCII. Newlines and tabs are typed as Enter and Tab; other control characters cannot be."""
    if not isinstance(text, str) or not text:
        raise CommandError("invalid_args", "text must be a non-empty string")
    for c in text:
        if ord(c) > 0x7E:
            raise CommandError("invalid_args", "non-ASCII text is not supported yet")
        if ord(c) < 0x20 and c not in "\n\r\t":
            raise CommandError("invalid_args", "control characters other than newline and tab cannot be typed")
    return text


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


def default_timeout(cmd: str, args: dict) -> float:
    """Deadline when the agent sets none: 30 s, plus the time typing or a long swipe itself takes."""
    if cmd == "type" and isinstance(args.get("text"), str):
        return DEFAULT_TIMEOUT_S + len(args["text"]) / 10
    if cmd == "swipe" and _is_number(args.get("ms")):
        return DEFAULT_TIMEOUT_S + max(args["ms"], 0) / 1000
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
    ) -> None:
        self.adb = adb
        self.stop_file = stop_file
        self.focus_retries = focus_retries
        self.focus_retry_delay = focus_retry_delay
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
        if REFUSED_MARKER in out:
            raise await self._mismatch(packages)
        return out

    # ---- commands ----

    async def handle(self, cmd: str, args: dict) -> dict:
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
        }.get(cmd)
        if handler is None:
            raise CommandError("invalid_args", f"unknown command: {cmd}")
        _if_foreground(args)  # validates
        timeout = _timeout(args, default_timeout(cmd, args))
        try:
            # The deadline cancels the command; Adb.run kills the adb call it was waiting on.
            return await asyncio.wait_for(handler(args), timeout)
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

    async def cmd_ui(self, args: dict) -> dict:
        xml_bytes = await self._dump_ui(_if_foreground(args))
        fg, _ = await self.foreground_and_wake()
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
        commands = type_commands(args.get("text"))  # validates
        # Typing with no field focused is the agent's call: the reply says what was focused.
        field = await self.text_field()
        for command in commands:
            await self._guarded(command, args)
        return {
            "length": len(args["text"]),
            "field_package": field[0] if field else None,
            "field_input_type": field[1] if field else None,
            "password_field": is_password_type(field[1]) if field else False,
        }

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
            await self._guarded(f"am start -W -n {shlex.quote(component)}", args)
        else:
            out = await self._guarded(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", args)
            if "Events injected: 1" not in out:
                raise CommandError("launch_failed", out.strip()[-200:])
        return {"package": package}


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


def summarise(cmd: str, args: dict) -> str:
    """Log-safe summary of a command. Typed text is never logged."""
    if cmd == "type":
        text = args.get("text") if isinstance(args, dict) else None
        return f"type length={len(text) if isinstance(text, str) else '?'}"
    return f"{cmd} {json.dumps(args, sort_keys=True)}"


async def dispatch(controller: PhoneController, raw: str | bytes) -> dict | None:
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
        result = await controller.handle(str(cmd), args)
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
                    async for raw in ws:  # one command at a time: adb actions are serialised
                        reply = await dispatch(controller, raw)
                        if reply is not None:
                            await ws.send(json.dumps(reply))
                        if controller.stopped():
                            await ws.close(1000, "stop file")
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
