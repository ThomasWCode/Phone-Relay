#!/usr/bin/env python3
"""Phone client for the phone relay (phase 1: adb, from Termux on the phone or from a computer).

Dials out to the relay over WSS, receives commands, runs them through adb and
replies. The app allowlist is enforced here, next to the phone, regardless of
what the relay sends. Every input is sent in the same shell command as a
focus check, so it only runs while an allowed app has focus.

It never runs `uiautomator dump`: that unbinds every accessibility service on
the phone for a moment. The screen is read from screenshots only.

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
import os
import random
import re
import shlex
import stat
import sys

VERSION = "0.2.0"

# ---- policy (edit deliberately; the relay has its own copy) ----

ALLOWED_PACKAGES = ("com.google.android.gm", "com.android.chrome")
# /launch may also start from the home screen (Samsung One UI Home).
HOME_PACKAGES = ("com.sec.android.app.launcher",)
KEYCODES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "delete": "KEYCODE_DEL",
}
DEFAULT_STOP_FILE = os.path.expanduser("~/STOP_PHONE_AGENT")

MAX_COORD = 10000
MIN_SCALE = 0.1
MAX_SCALE = 1.0
MIN_SWIPE_MS = 50
MAX_SWIPE_MS = 5000
MAX_TEXT_LENGTH = 500
JPEG_QUALITY = 60
ADB_TIMEOUT_S = 20
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

    async def run(self, *args: str, timeout: float = ADB_TIMEOUT_S) -> bytes:
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
            proc.kill()
            await proc.wait()
            raise CommandError("adb_timeout", " ".join(args[:2])) from e
        if proc.returncode != 0:
            raise CommandError("adb_failed", err.decode(errors="replace").strip()[:200])
        return out

    async def shell(self, command: str, timeout: float = ADB_TIMEOUT_S) -> str:
        return (await self.run("shell", command, timeout=timeout)).decode(errors="replace")

    async def exec_out(self, command: str, timeout: float = ADB_TIMEOUT_S) -> bytes:
        return await self.run("exec-out", command, timeout=timeout)


# ---- parsing helpers (pure, unit-tested) ----

_FOCUS_RE = re.compile(r"mCurrentFocus=(null|Window\{[0-9a-fA-F]+ (?:u\d+ )?([^\s/}]+)[^}]*\})")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=.*?ActivityRecord\{[0-9a-fA-F]+ (?:u\d+ )?([^\s/}]+)/")
_WAKE_RE = re.compile(r"mWakefulness=(\w+)")
_SIZE_RE = re.compile(r"(Physical|Override) size: (\d+)x(\d+)")
_BATTERY_RE = re.compile(r"^\s*level: (\d+)", re.MULTILINE)
_INPUT_TYPE_RE = re.compile(r"\binputType=(0x[0-9a-fA-F]+|\d+)")
_EDITOR_PKG_RE = re.compile(r"\bpackageName=(\S+)")


def parse_foreground(dumpsys: str) -> str | None:
    """Return the package (or window name) of the focused window, or None if unknown.

    Uses the first mCurrentFocus line. A lock screen shows up as NotificationShade
    or com.android.systemui, which is not on the allowlist, so it is refused.
    In-app popups (e.g. Chrome's overflow menu) are titled PopupWindow:<id> with no
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
    if not isinstance(text, str) or not text:
        raise CommandError("invalid_args", "text must be a non-empty string")
    if len(text) > MAX_TEXT_LENGTH:
        raise CommandError("invalid_args", f"text longer than {MAX_TEXT_LENGTH} characters")
    if any(not (0x20 <= ord(c) <= 0x7E) for c in text):
        raise CommandError("invalid_args", "text must be printable ASCII")
    if "%" in text:
        raise CommandError("invalid_args", "text must not contain %")
    return text


def escape_text(text: str) -> str:
    """Shell-quoted argument for `input text`. Spaces become %s (input's own escape)."""
    return shlex.quote(validate_text(text).replace(" ", "%s"))


def _coord(args: dict, name: str, scale: float) -> int:
    v = args.get(name)
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_COORD:
        raise CommandError("invalid_args", f"{name} must be an integer 0-{MAX_COORD}")
    return round(v / scale)


def _scale(args: dict, default: float = 1.0) -> float:
    v = args.get("scale", default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not MIN_SCALE <= v <= MAX_SCALE:
        raise CommandError("invalid_args", f"scale must be between {MIN_SCALE} and {MAX_SCALE}")
    return float(v)


# ---- controller ----


class PhoneController:
    def __init__(
        self,
        adb: Adb,
        allowlist: tuple[str, ...] = ALLOWED_PACKAGES,
        stop_file: str = DEFAULT_STOP_FILE,
        focus_retries: int = 3,
        focus_retry_delay: float = 0.3,
    ) -> None:
        self.adb = adb
        self.allowlist = allowlist
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

    async def require_allowed(self) -> str:
        fg, wake = await self.foreground_and_wake()
        if wake is not None and wake != "Awake":
            raise CommandError("screen_off", f"wakefulness={wake}")
        if fg not in self.allowlist:
            raise CommandError("foreground_not_allowed", f"foreground={fg}")
        return fg

    async def screen_size(self) -> tuple[int, int]:
        if self._screen_size is None:
            size = parse_size(await self.adb.shell("wm size"))
            if size is None:
                raise CommandError("device_error", "could not read screen size")
            self._screen_size = size
        return self._screen_size

    async def _check_point(self, x: int, y: int) -> None:
        # Accept either orientation; the device clips anything off-screen.
        w, h = await self.screen_size()
        limit = max(w, h)
        if not (0 <= x < limit and 0 <= y < limit):
            raise CommandError("invalid_args", f"point ({x},{y}) outside screen {w}x{h}")

    async def _guarded(self, action: str, packages: tuple[str, ...] | None = None, timeout: float = ADB_TIMEOUT_S) -> str:
        """Run `action` on the phone only while an allowed app has focus (checked in the same command)."""
        out = await self.adb.shell(guarded(action, packages or self.allowlist), timeout=timeout)
        if REFUSED_MARKER in out:
            raise CommandError("foreground_not_allowed", "focus changed before the action ran")
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
            "tap": self.cmd_tap,
            "swipe": self.cmd_swipe,
            "type": self.cmd_type,
            "key": self.cmd_key,
            "launch": self.cmd_launch,
        }.get(cmd)
        if handler is None:
            raise CommandError("invalid_args", f"unknown command: {cmd}")
        return await handler(args)

    async def cmd_state(self, args: dict) -> dict:
        fg, wake = await self.foreground_and_wake()
        battery = parse_battery(await self.adb.shell("dumpsys battery"))
        w, h = await self.screen_size()
        return {
            "foreground": fg,
            "foreground_allowed": fg in self.allowlist,
            "awake": wake == "Awake",
            "battery": battery,
            "screen": {"width": w, "height": h},
            "allowlist": list(self.allowlist),
            "client_version": VERSION,
        }

    async def cmd_screenshot(self, args: dict) -> dict:
        scale = _scale(args, 0.5)
        fg = await self.require_allowed()
        png = await self.adb.exec_out(guarded("screencap -p", self.allowlist), timeout=30)
        if png.startswith(REFUSED_MARKER.encode()):
            raise CommandError("foreground_not_allowed", "focus changed before the capture")
        if not png.startswith(b"\x89PNG"):
            raise CommandError("device_error", "screencap did not return a PNG")
        # Re-check: if the foreground changed while capturing, discard the image.
        await self.require_allowed()
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

    async def cmd_tap(self, args: dict) -> dict:
        scale = _scale(args)
        x, y = _coord(args, "x", scale), _coord(args, "y", scale)
        await self._check_point(x, y)
        await self.require_allowed()
        await self._guarded(f"input tap {x} {y}")
        return {"x": x, "y": y}

    async def cmd_swipe(self, args: dict) -> dict:
        scale = _scale(args)
        x1, y1 = _coord(args, "x1", scale), _coord(args, "y1", scale)
        x2, y2 = _coord(args, "x2", scale), _coord(args, "y2", scale)
        ms = args.get("ms", 300)
        if not isinstance(ms, int) or isinstance(ms, bool) or not MIN_SWIPE_MS <= ms <= MAX_SWIPE_MS:
            raise CommandError("invalid_args", f"ms must be an integer {MIN_SWIPE_MS}-{MAX_SWIPE_MS}")
        await self._check_point(x1, y1)
        await self._check_point(x2, y2)
        await self.require_allowed()
        await self._guarded(f"input swipe {x1} {y1} {x2} {y2} {ms}", timeout=ADB_TIMEOUT_S + ms / 1000)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}

    async def text_field(self) -> tuple[str, int] | None:
        """(package, inputType) of the focused text field, read without uiautomator."""
        return parse_editor_info(await self.adb.shell("dumpsys input_method | grep -A12 'curEditorInfo:'"))

    async def cmd_type(self, args: dict) -> dict:
        quoted = escape_text(args.get("text"))  # validates
        fg = await self.require_allowed()
        field = await self.text_field()
        if field is None or field[0] != fg or field[1] == 0:
            raise CommandError("no_text_field", f"no focused text field in {fg}")
        if is_password_type(field[1]):
            raise CommandError("password_field", "refusing to type into a password field")
        await self._guarded(f"input text {quoted}", timeout=45)
        return {"length": len(args["text"])}

    async def cmd_key(self, args: dict) -> dict:
        key = args.get("key")
        if key not in KEYCODES:
            raise CommandError("invalid_args", f"key must be one of: {', '.join(KEYCODES)}")
        # Home is guarded too: never press it over another app (e.g. a widget's automation in front).
        await self.require_allowed()
        await self._guarded(f"input keyevent {KEYCODES[key]}")
        return {"key": key}

    async def cmd_launch(self, args: dict) -> dict:
        package = args.get("package")
        if package not in self.allowlist:
            raise CommandError("invalid_args", f"package must be one of: {', '.join(self.allowlist)}")
        # Start only from an allowed app or the home screen, never over anything else.
        from_packages = self.allowlist + HOME_PACKAGES
        fg, wake = await self.foreground_and_wake()
        if wake is not None and wake != "Awake":
            raise CommandError("screen_off", f"wakefulness={wake}")
        if fg not in from_packages:
            raise CommandError("foreground_not_allowed", f"foreground={fg}")
        out = await self.adb.shell(
            f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {shlex.quote(package)}"
        )
        component = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith(package + "/")), None)
        if component:
            await self._guarded(f"am start -W -n {shlex.quote(component)}", from_packages, timeout=30)
        else:
            out = await self._guarded(
                f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", from_packages
            )
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

        return connect(url, extra_headers=headers, ping_interval=20, ping_timeout=20, open_timeout=15)
    return connect(url, additional_headers=headers, ping_interval=20, ping_timeout=20, open_timeout=15)


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
                            "allowlist": list(controller.allowlist),
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
    if not state["foreground_allowed"]:
        print("FAIL: open Chrome or Gmail on the phone, then re-run the self-test")
        return 1
    # The guard with a harmless action: proves the on-phone focus check works in this shell.
    probe = await controller.adb.shell(guarded("echo GUARD_OK", controller.allowlist))
    if "GUARD_OK" in probe:
        print("input guard: passes while", state["foreground"], "has focus")
    else:
        ok = False
        print("FAIL input guard: refused although an allowed app has focus:", probe.strip()[:200])
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
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
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
        return asyncio.run(selftest(controller))

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
