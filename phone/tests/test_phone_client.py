"""Unit tests for phone_client.py, using a fake adb (no phone needed).

Run from the repository root:
    python -m unittest discover -s phone/tests -v
"""

import asyncio
import base64
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_adb  # noqa: E402
import fake_devtools  # noqa: E402
import phone_client as pc  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.WARNING)  # quiet asyncio debug mode in async tests

FAKE_ADB = os.path.join(HERE, "fake_adb.py")
# Packages the guard tests name with if_foreground.
PACKAGES = ("com.google.android.gm", "com.android.chrome")

# The curEditorInfo block as Android 17 on a Galaxy S24 prints it (from `dumpsys input_method`).
EDITOR_INFO_S24 = """      curEditorInfo:
        inputType=0x0 imeOptions=0x0 privateImeOptions=null
        actionLabel=null actionId=0
        initialSelStart=-1 initialSelEnd=-1 initialCapsMode=0x0
        hintText=null label=null
        packageName=com.sec.android.app.launcher autofillId=0 fieldId=0 fieldName=null
      mCurClient=ClientState{1}
"""


class InProcessAdb:
    """Calls fake_adb.run directly, records every command and the actions that actually ran."""

    def __init__(self, state=None):
        self.state = fake_adb.default_state()
        self.state.update(state or {})
        self.calls = []
        self.executed = []

    async def run(self, *args, timeout=None, stdin=None):
        self.calls.append(list(args))
        if stdin is not None:  # a local file's path, as Adb.run takes it
            with open(stdin, "rb") as f:
                stdin = f.read()
        if self.state.get("hang"):
            await asyncio.sleep(self.state["hang"])
        if args[0] in ("pull", "push"):
            code, err = fake_adb.transfer(list(args), self.state)
            if code:
                raise pc.CommandError("adb_failed", err)
            return b""
        if args[0] == "forward":
            code, out = fake_adb.forward(list(args), self.state)
            if code:
                raise pc.CommandError("adb_failed", "forward failed")
            return out.encode()
        cmd = " ".join(args[1:])
        m = fake_adb.GUARD_RE.match(cmd)
        if m is None:
            self.executed.append(cmd)
        elif fake_adb.guard_passes(m.group("guard"), self.state):
            self.executed.append(m.group("action"))
        # fake_adb.save_state is a no-op without FAKE_ADB_STATE, so state stays in memory
        return fake_adb.run(cmd, self.state, stdin)

    async def shell(self, command, timeout=None):
        return (await self.run("shell", command)).decode()

    async def exec_out(self, command, timeout=None):
        return await self.run("exec-out", command)

    async def exec_in(self, command, path, timeout=None):
        return await self.run("exec-in", command, stdin=path)

    def inputs(self):
        return [c for c in self.executed if c.startswith("input ")]


def controller(state=None, stop_file=None):
    adb = InProcessAdb(state)
    ctl = pc.PhoneController(adb, stop_file=stop_file or os.path.join(tempfile.gettempdir(), "no-such-stop-file"),
                             focus_retry_delay=0)
    return ctl, adb


class ParsingTests(unittest.TestCase):
    def test_foreground_app(self):
        out = "  mCurrentFocus=Window{9f1c2d u0 com.android.chrome/com.google.android.apps.chrome.Main}\n"
        self.assertEqual(pc.parse_foreground(out), "com.android.chrome")

    def test_foreground_without_user(self):
        self.assertEqual(pc.parse_foreground("mCurrentFocus=Window{abc com.google.android.gm/.Foo}"),
                         "com.google.android.gm")

    def test_foreground_lock_screen(self):
        self.assertEqual(pc.parse_foreground("mCurrentFocus=Window{abc u0 NotificationShade}"), "NotificationShade")

    def test_foreground_popup_uses_focused_app(self):
        out = ("  mCurrentFocus=Window{4d3c2b1 u0 PopupWindow:9a8b7c6}\n"
               "  mFocusedApp=ActivityRecord{1f2e3d4 u0 com.android.chrome/org.chromium.chrome.browser.ChromeTabbedActivity t412}\n")
        self.assertEqual(pc.parse_foreground(out), "com.android.chrome")
        self.assertIsNone(pc.parse_foreground("  mCurrentFocus=Window{4d3c2b1 u0 PopupWindow:9a8b7c6}\n"))

    def test_foreground_shade_ignores_focused_app(self):
        out = ("  mCurrentFocus=Window{abc u0 NotificationShade}\n"
               "  mFocusedApp=ActivityRecord{1f2e3d4 u0 com.android.chrome/.Main t1}\n")
        self.assertEqual(pc.parse_foreground(out), "NotificationShade")

    def test_foreground_null_or_missing(self):
        self.assertIsNone(pc.parse_foreground("mCurrentFocus=null"))
        self.assertIsNone(pc.parse_foreground("nothing here"))

    def test_wakefulness(self):
        self.assertEqual(pc.parse_wakefulness("  mWakefulness=Asleep\n"), "Asleep")
        # Android 17 prints a second, numeric line; the first one wins
        self.assertEqual(pc.parse_wakefulness("  mWakefulness=Awake\nmWakefulness=1\n"), "Awake")

    def test_size_override_wins(self):
        self.assertEqual(pc.parse_size("Physical size: 1080x2340\nOverride size: 720x1560\n"), (720, 1560))
        self.assertEqual(pc.parse_size("Physical size: 1080x2340\n"), (1080, 2340))
        self.assertIsNone(pc.parse_size("error"))

    def test_battery(self):
        self.assertEqual(pc.parse_battery("  AC powered: false\n  level: 42\n  scale: 100\n"), 42)

    def test_ui_keeps_every_package_and_drops_empty_nodes(self):
        els = pc.parse_ui(fake_adb.ui_xml({"foreground": "com.android.chrome"}).encode())
        # status bar clock, url bar, text, scroll view; the empty layouts and View dropped
        self.assertEqual([e["pkg"] for e in els], ["com.android.systemui"] + ["com.android.chrome"] * 3)
        self.assertEqual(els[0]["text"], "12:00")
        url_bar = els[1]
        self.assertEqual(url_bar["center"], [500, 170])
        self.assertTrue(url_bar["editable"] and url_bar["focused"])
        self.assertEqual(els[2]["text"], "Hello & welcome")

    def test_ui_strips_trailing_status_line(self):
        xml = fake_adb.ui_xml({"foreground": "com.android.chrome"}).encode() + b"\nUI hierchary dumped to: /dev/tty"
        self.assertEqual(len(pc.parse_ui(xml)), 4)

    def test_ui_keeps_password_text_and_flags_it(self):
        xml = fake_adb.ui_xml({"foreground": "com.android.chrome", "password_focused": True})
        xml = xml.replace('text="" resource-id="com.android.chrome:id/url_bar"',
                          'text="secret" resource-id="com.android.chrome:id/url_bar"')
        els = pc.parse_ui(xml.encode())
        self.assertEqual(els[1]["text"], "secret")
        self.assertTrue(els[1]["password"])

    def test_ui_keeps_long_text(self):
        xml = fake_adb.ui_xml({"foreground": "com.android.chrome"}).replace("Hello &amp; welcome", "x" * 5000)
        self.assertEqual(pc.parse_ui(xml.encode())[2]["text"], "x" * 5000)

    def test_ui_bad_output(self):
        with self.assertRaises(pc.CommandError) as cm:
            pc.parse_ui(b"ERROR: could not get idle state.")
        self.assertEqual(cm.exception.code, "ui_unavailable")

    def test_input_shown(self):
        self.assertTrue(pc.parse_input_shown("      mInputShown=true\n"))
        self.assertFalse(pc.parse_input_shown("      mInputShown=false\n"))
        self.assertIsNone(pc.parse_input_shown("nothing"))

    def test_editor_info_s24(self):
        self.assertEqual(pc.parse_editor_info(EDITOR_INFO_S24), ("com.sec.android.app.launcher", 0))

    def test_editor_info_stops_at_block_end(self):
        # inputType after the block (another client's) must not be picked up
        dump = "      curEditorInfo:\n        packageName=com.android.chrome\n      inputType=0x81\n"
        self.assertIsNone(pc.parse_editor_info(dump))
        self.assertIsNone(pc.parse_editor_info("nothing"))

    def test_editor_info_fake_matches_parser(self):
        state = {"foreground": "com.android.chrome", "password_focused": True}
        self.assertEqual(pc.parse_editor_info(fake_adb.editor_info(state)), ("com.android.chrome", 0x81))

    def test_password_types(self):
        for t in [0x81, 0x91, 0xE1, 0x12, 0x4081]:  # text password, visible, web, number password, + flags
            self.assertTrue(pc.is_password_type(t), hex(t))
        for t in [0x0, 0x1, 0x21, 0x2, 0x3, 0x22C001, 0x11, 0xA1]:  # null, text, email, number, phone, ...
            self.assertFalse(pc.is_password_type(t), hex(t))


class GuardTests(unittest.TestCase):
    """The if_foreground guard's own grep patterns, applied to realistic dumpsys output."""

    def passes(self, packages=PACKAGES, **state):
        full = fake_adb.default_state()
        full.update(state)
        return fake_adb.guard_passes(pc.focus_guard(packages), full)

    def test_named_app(self):
        self.assertTrue(self.passes(foreground="com.android.chrome"))
        self.assertTrue(self.passes(foreground="com.google.android.gm"))

    def test_other_windows(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher",
                   "com.microsoft.teams", "com.android.chromex", None]:
            self.assertFalse(self.passes(foreground=fg), fg)

    def test_any_package_can_be_named(self):
        self.assertTrue(self.passes(("com.android.settings",), foreground="com.android.settings"))
        self.assertTrue(self.passes(("com.sec.android.app.launcher",), foreground="com.sec.android.app.launcher"))

    def test_popup_counts_for_its_app(self):
        self.assertTrue(self.passes(foreground="com.android.chrome", popup=True))
        self.assertFalse(self.passes(foreground="com.android.settings", popup=True))

    def test_screen_off(self):
        self.assertFalse(self.passes(foreground="com.android.chrome", awake=False))

    def test_package_dots_are_literal(self):
        # "com.android.chrome" must not match "comXandroidXchrome"
        self.assertFalse(self.passes(foreground="comXandroidXchrome"))

    def test_guarded_shape(self):
        cmd = pc.guarded("input tap 1 2", PACKAGES)
        m = fake_adb.GUARD_RE.match(cmd)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("action"), "input tap 1 2")
        self.assertNotIn("uiautomator", cmd)


def find_sh():
    """A POSIX sh: on PATH, or the one Git for Windows ships next to git.exe."""
    sh = shutil.which("sh")
    if sh:
        return sh
    git = shutil.which("git")
    if git:
        candidate = os.path.join(os.path.dirname(os.path.dirname(git)), "usr", "bin", "sh.exe")
        if os.path.exists(candidate):
            return candidate
    return None


SH = find_sh()


@unittest.skipUnless(SH, "needs a POSIX sh (Git for Windows provides one)")
class GuardShellTests(unittest.TestCase):
    """Runs the guard in a real POSIX shell with dumpsys stubbed, to check its shell syntax."""

    STUBS = ("dumpsys() { case \"$1\" in window) printf '%s\\n' \"$WIN\";; "
             "power) printf '  mWakefulness=%s\\n' \"$WAKE\";; esac; }\n")

    def run_guard(self, win, wake="Awake"):
        # sh's own folder first, so it finds its grep (Git for Windows' PATH does not include usr\bin)
        path = os.path.dirname(SH) + os.pathsep + os.environ.get("PATH", "")
        env = {**os.environ, "PATH": path, "WIN": win, "WAKE": wake}
        script = self.STUBS + pc.guarded("echo RAN", PACKAGES)
        out = subprocess.run([SH, "-c", script], env=env, capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    def test_allowed(self):
        self.assertEqual(self.run_guard("  mCurrentFocus=Window{1a2b u0 com.android.chrome/x.Main}"), "RAN")

    def test_refused(self):
        self.assertEqual(self.run_guard("  mCurrentFocus=Window{1a2b u0 com.android.settings/x.Main}"),
                         pc.REFUSED_MARKER)
        self.assertEqual(self.run_guard("  mCurrentFocus=Window{1a2b u0 com.android.chrome/x.Main}", "Asleep"),
                         pc.REFUSED_MARKER)

    def test_popup(self):
        win = ("  mCurrentFocus=Window{4d3c u0 PopupWindow:9a8b}\n"
               "  mFocusedApp=ActivityRecord{1f2e u0 com.google.android.gm/.Main t4}")
        self.assertEqual(self.run_guard(win), "RAN")


def typed(commands):
    """What a list of type_commands (or one input_text_command) would type."""
    if isinstance(commands, str):
        commands = [commands]
    out = ""
    for command in commands:
        for call in command.split(" && "):
            words = shlex.split(call)
            if words[:2] == ["input", "keyevent"]:
                out += {"KEYCODE_ENTER": "\n", "KEYCODE_TAB": "\t"}[words[2]]
            else:
                out += words[2].replace("%s", " ")
    return out


class TextEscapingTests(unittest.TestCase):
    def test_spaces_become_percent_s(self):
        self.assertEqual(pc.escape_text("hello world"), "hello%sworld")

    def test_shell_metacharacters_are_quoted(self):
        for text in ["a;rm -rf /", "$(reboot)", "`id`", "it's", 'say "hi"', "a|b&c>d<e", "back\\slash"]:
            quoted = pc.escape_text(text)
            # The device shell must see exactly one word equal to the input-escaped text.
            self.assertEqual(shlex.split(quoted), [text.replace(" ", "%s")])

    def test_rejects_bad_text(self):
        for bad in ["", "café", "line\nbreak", "tab\t", None, 5]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                pc.input_text_command(bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    def test_type_rejects_what_cannot_be_typed(self):
        for bad in ["", "café", "emoji \U0001F600", "bell\x07", "nul\x00", None, 5]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                pc.type_commands(bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    def test_percent_is_typed_exactly(self):
        for text in ["50% off", "100%", "%", "a%sb", "%s", "%%s", "x%s y%sz", "% %s %", "it's 5% $(x)"]:
            self.assertEqual(typed(pc.input_text_command(text)), text, text)
            self.assertEqual(typed(pc.type_commands(text)), text, text)

    def test_literal_percent_s_splits_into_two_calls(self):
        self.assertEqual(pc.input_text_command("a%sb"), "input text a% && input text sb")
        self.assertEqual(pc.input_text_command("plain text"), "input text plain%stext")

    def test_newlines_and_tabs_become_keys(self):
        commands = pc.type_commands("Dear X,\n\nline\ttwo\r\nend\rlast")
        self.assertEqual(typed(commands), "Dear X,\n\nline\ttwo\nend\nlast")
        self.assertIn("input keyevent KEYCODE_ENTER", commands[0])
        self.assertIn("input keyevent KEYCODE_TAB", commands[0])

    def test_long_text_is_split_over_calls(self):
        text = ("word " * 2000).strip()  # 9999 characters, no length limit
        commands = pc.type_commands(text)
        self.assertEqual(typed(commands), text)
        self.assertGreater(len(commands), 1)
        self.assertTrue(all(len(c) < 5 * pc.TEXT_CHUNK for c in commands))


class KeyTests(unittest.TestCase):
    def test_short_names(self):
        self.assertEqual(pc.resolve_key("back"), "KEYCODE_BACK")
        self.assertEqual(pc.resolve_key("recents"), "KEYCODE_APP_SWITCH")

    def test_raw_keycodes(self):
        for key in ["KEYCODE_PAGE_DOWN", "KEYCODE_ESCAPE", "KEYCODE_DPAD_LEFT", "KEYCODE_MOVE_END", "KEYCODE_A"]:
            self.assertEqual(pc.resolve_key(key), key)

    def test_every_key_is_accepted(self):
        for key in ["KEYCODE_POWER", "KEYCODE_SLEEP", "KEYCODE_CALL", "KEYCODE_VOLUME_UP", "KEYCODE_MEDIA_PLAY",
                    "KEYCODE_CAMERA", "KEYCODE_ASSIST", "KEYCODE_BRIGHTNESS_DOWN", "KEYCODE_WAKEUP", "26"]:
            self.assertEqual(pc.resolve_key(key), key)
        self.assertEqual(pc.resolve_key(26), "26")

    def test_malformed(self):
        for key in ["power", "keycode_back", "KEYCODE_BACK; reboot", "26;x", "", None, "KEYCODE_", True, -1, 1.5]:
            with self.assertRaises(pc.CommandError, msg=repr(key)):
                pc.resolve_key(key)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_state(self):
        ctl, _ = controller()
        st = await ctl.handle("state", {})
        self.assertEqual(st["foreground"], "com.android.chrome")
        self.assertTrue(st["awake"])
        self.assertEqual(st["battery"], 87)
        self.assertEqual(st["screen"], {"width": 1080, "height": 2340})
        self.assertEqual(st["client"], "termux-adb")
        self.assertNotIn("allowlist", st)

    async def test_state_in_any_app(self):
        ctl, _ = controller({"foreground": "com.android.settings"})
        st = await ctl.handle("state", {})
        self.assertEqual(st["foreground"], "com.android.settings")

    async def test_tap(self):
        ctl, adb = controller()
        await ctl.handle("tap", {"x": 100, "y": 200})
        self.assertEqual(adb.inputs(), ["input tap 100 200"])

    async def test_tap_scaled(self):
        ctl, adb = controller()
        res = await ctl.handle("tap", {"x": 270, "y": 585, "scale": 0.5})
        self.assertEqual(res, {"x": 540, "y": 1170})
        self.assertEqual(adb.inputs(), ["input tap 540 1170"])

    async def test_tap_in_popup(self):
        ctl, adb = controller({"popup": True})
        await ctl.handle("tap", {"x": 100, "y": 200, "if_foreground": "com.android.chrome"})
        self.assertEqual(adb.inputs(), ["input tap 100 200"])

    async def test_actions_work_in_any_app(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher",
                   "com.microsoft.teams", None]:
            ctl, adb = controller({"foreground": fg})
            for cmd, args in [
                ("tap", {"x": 1, "y": 1}),
                ("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5}),
                ("type", {"text": "hi"}),
                ("key", {"key": "back"}),
                ("screenshot", {}),
                ("ui", {}),
                ("key", {"key": "home"}),
            ]:
                await ctl.handle(cmd, args)
            self.assertEqual(len(adb.inputs()), 5, fg)
            self.assertTrue(any("screencap" in c for c in adb.executed))

    async def test_no_guard_without_if_foreground(self):
        ctl, adb = controller()
        await ctl.handle("tap", {"x": 1, "y": 1})
        self.assertFalse(any(fake_adb.GUARD_RE.match(c[1]) for c in adb.calls))

    async def test_if_foreground_guards_every_command(self):
        ctl, adb = controller()
        guard = {"if_foreground": "com.android.chrome"}
        await ctl.handle("tap", {"x": 1, "y": 1, **guard})
        await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5, **guard})
        await ctl.handle("type", {"text": "hi\nthere", **guard})
        await ctl.handle("key", {"key": "back", **guard})
        await ctl.handle("screenshot", guard)
        await ctl.handle("ui", guard)
        await ctl.handle("launch", {"package": "com.google.android.gm", **guard})
        for call in adb.calls:
            cmd = call[1]
            if cmd.startswith(("input ", "screencap", "am start", "monkey", "uiautomator")):
                self.fail(f"unguarded action: {cmd}")

    async def test_if_foreground_refuses_other_apps(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher", None]:
            ctl, adb = controller({"foreground": fg})
            for cmd, args in [
                ("tap", {"x": 1, "y": 1}),
                ("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5}),
                ("type", {"text": "hi"}),
                ("key", {"key": "home"}),
                ("screenshot", {}),
                ("ui", {}),
                ("launch", {"package": "com.android.chrome"}),
            ]:
                with self.assertRaises(pc.CommandError, msg=f"{fg} {cmd}") as cm:
                    await ctl.handle(cmd, {**args, "if_foreground": ["com.android.chrome", "com.google.android.gm"]})
                self.assertEqual(cm.exception.code, "foreground_mismatch")
            self.assertEqual(adb.inputs(), [])
            self.assertFalse(any(c.startswith(("screencap", "uiautomator", "am start", "monkey")) for c in adb.executed))

    async def test_if_foreground_any_package(self):
        ctl, adb = controller({"foreground": "com.android.settings"})
        await ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": "com.android.settings"})
        self.assertEqual(adb.inputs(), ["input tap 1 1"])

    async def test_if_foreground_is_checked_in_the_same_command(self):
        """The focus changes just before the tap's adb call: the guard inside that call refuses it."""
        ctl, adb = controller()
        original = adb.run

        async def run(*args, timeout=None):
            if "input tap" in args[1]:
                adb.state["foreground"] = "com.android.settings"
            return await original(*args, timeout=timeout)

        adb.run = run
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": "com.android.chrome"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        self.assertIn("com.android.settings", cm.exception.detail)
        self.assertEqual(adb.inputs(), [])

    async def test_bad_if_foreground(self):
        ctl, adb = controller()
        for bad in ["", [], ["ok.pkg", 5], "a;b", {"x": 1}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": bad})
            self.assertEqual(cm.exception.code, "invalid_args")
        self.assertEqual(adb.calls, [])

    async def test_screen_off_does_not_refuse(self):
        ctl, adb = controller({"awake": False})
        await ctl.handle("tap", {"x": 1, "y": 1})
        self.assertEqual(adb.inputs(), ["input tap 1 1"])
        with self.assertRaises(pc.CommandError) as cm:  # the guard does want the screen on
            await ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": "com.android.chrome"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")

    async def test_home(self):
        ctl, adb = controller()
        await ctl.handle("key", {"key": "home"})
        self.assertEqual(adb.inputs(), ["input keyevent KEYCODE_HOME"])
        self.assertEqual(adb.state["foreground"], fake_adb.LAUNCHER)

    async def test_keys(self):
        ctl, adb = controller()
        for key, code in pc.KEYCODES.items():
            if key == "home":
                continue
            await ctl.handle("key", {"key": key})
            self.assertEqual(adb.inputs()[-1], f"input keyevent {code}")
        for key in ["KEYCODE_PAGE_DOWN", "KEYCODE_POWER", "KEYCODE_VOLUME_DOWN", "KEYCODE_MEDIA_PAUSE", 26]:
            await ctl.handle("key", {"key": key})
            self.assertEqual(adb.inputs()[-1], f"input keyevent {key}")
        await ctl.handle("key", {"key": "KEYCODE_POWER", "longpress": True})
        self.assertEqual(adb.inputs()[-1], "input keyevent --longpress KEYCODE_POWER")
        for args in [{"key": "power"}, {"key": "back", "longpress": "yes"}]:
            with self.assertRaises(pc.CommandError, msg=repr(args)):
                await ctl.handle("key", args)

    async def test_type(self):
        ctl, adb = controller()
        res = await ctl.handle("type", {"text": "hello world"})
        self.assertEqual(res, {"length": 11, "method": "keys", "field_package": "com.android.chrome",
                               "field_input_type": 0x1, "password_field": False})
        self.assertEqual(adb.inputs(), ["input text hello%sworld"])

    async def test_type_percent(self):
        ctl, adb = controller()
        await ctl.handle("type", {"text": "a%sb"})
        self.assertEqual(adb.inputs(), ["input text a% && input text sb"])

    async def test_type_lines(self):
        ctl, adb = controller()
        await ctl.handle("type", {"text": "one\ntwo"})
        self.assertEqual(adb.inputs(), ["input text one && input keyevent KEYCODE_ENTER && input text two"])

    async def test_type_into_password_field_says_so(self):
        ctl, adb = controller({"password_focused": True})
        res = await ctl.handle("type", {"text": "hunter2"})
        self.assertTrue(res["password_field"])
        self.assertEqual(res["field_input_type"], 0x81)
        self.assertEqual(adb.inputs(), ["input text hunter2"])

    async def test_type_without_text_field(self):
        """No field focused: the agent's call; it gets field_input_type 0 back and checks the screen."""
        ctl, adb = controller({"text_field": False})
        res = await ctl.handle("type", {"text": "hi"})
        self.assertEqual(res["field_input_type"], 0)
        self.assertEqual(adb.inputs(), ["input text hi"])

    async def test_taps_on_password_keyboard_run(self):
        ctl, adb = controller({"password_focused": True, "keyboard": True})
        await ctl.handle("tap", {"x": 100, "y": 2000})
        await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5})
        self.assertEqual(adb.inputs(), ["input tap 100 2000", "input swipe 1 1 5 5 300"])

    async def test_swipe(self):
        ctl, adb = controller()
        await ctl.handle("swipe", {"x1": 500, "y1": 1800, "x2": 500, "y2": 600, "ms": 400})
        self.assertEqual(adb.inputs(), ["input swipe 500 1800 500 600 400"])

    async def test_any_coordinates_and_durations(self):
        ctl, adb = controller()
        await ctl.handle("tap", {"x": 1.6, "y": -5})
        await ctl.handle("tap", {"x": 20000, "y": 3000})
        await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 2, "y2": 2, "ms": 0})
        await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 2, "y2": 2, "ms": 12000.4})
        self.assertEqual(adb.inputs(), ["input tap 2 -5", "input tap 20000 3000", "input swipe 1 1 2 2 0",
                                        "input swipe 1 1 2 2 12000"])

    async def test_invalid_values(self):
        ctl, adb = controller()
        for args in [{"x": "1", "y": 1}, {"x": True, "y": 1}, {"y": 1}, {"x": float("nan"), "y": 1},
                     {"x": 1, "y": 1, "scale": 0}, {"x": 1, "y": 1, "scale": -1}, {"x": 1, "y": 1, "timeout": 0}]:
            with self.assertRaises(pc.CommandError, msg=repr(args)) as cm:
                await ctl.handle("tap", args)
            self.assertEqual(cm.exception.code, "invalid_args")
        for ms in [-1, "300", True]:
            with self.assertRaises(pc.CommandError):
                await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 2, "y2": 2, "ms": ms})
        self.assertEqual(adb.inputs(), [])

    async def test_launch_from_home_screen(self):
        ctl, adb = controller({"foreground": fake_adb.LAUNCHER})
        await ctl.handle("launch", {"package": "com.google.android.gm"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")
        self.assertTrue(any(c.startswith("am start -W -n com.google.android.gm/") for c in adb.executed))

    async def test_launch_from_an_app(self):
        ctl, adb = controller({"foreground": "com.android.chrome"})
        await ctl.handle("launch", {"package": "com.google.android.gm"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")

    async def test_launch_any_app_over_any_app(self):
        for fg in ["com.android.settings", "com.microsoft.teams", "NotificationShade", None]:
            ctl, adb = controller({"foreground": fg})
            await ctl.handle("launch", {"package": "com.android.settings"})  # no launcher entry known: monkey
            self.assertEqual(adb.state["foreground"], "com.android.settings")

    async def test_launch_rejects_bad_package_names(self):
        ctl, adb = controller()
        for pkg in ["com.android.chrome; reboot", "a b", "", None, 5]:
            with self.assertRaises(pc.CommandError) as cm:
                await ctl.handle("launch", {"package": pkg})
            self.assertEqual(cm.exception.code, "invalid_args")
        self.assertEqual(adb.calls, [])

    async def test_screenshot_jpeg_scaled(self):
        ctl, _ = controller()
        shot = await ctl.handle("screenshot", {"scale": 0.5})
        self.assertEqual(shot["mime"], "image/jpeg")
        self.assertEqual((shot["width"], shot["height"]), (540, 1170))
        self.assertEqual((shot["real_width"], shot["real_height"]), (1080, 2340))
        self.assertEqual(shot["foreground"], "com.android.chrome")
        self.assertTrue(base64.b64decode(shot["data"]).startswith(b"\xff\xd8"))

    async def test_screenshot_does_not_upscale(self):
        ctl, _ = controller()
        shot = await ctl.handle("screenshot", {"scale": 2})
        self.assertEqual((shot["width"], shot["scale"]), (1080, 1.0))

    async def test_screenshot_discarded_if_foreground_changes_with_if_foreground(self):
        for guard, expect_error in [({"if_foreground": "com.android.chrome"}, True), ({}, False)]:
            ctl, adb = controller()
            original = adb.run

            async def run(*args, timeout=None, adb=adb, original=original):
                out = await original(*args, timeout=timeout)
                if "screencap -p" in args[1]:
                    adb.state["foreground"] = "com.android.settings"
                return out

            adb.run = run
            if expect_error:
                with self.assertRaises(pc.CommandError) as cm:
                    await ctl.handle("screenshot", guard)
                self.assertEqual(cm.exception.code, "foreground_mismatch")
            else:
                shot = await ctl.handle("screenshot", guard)
                self.assertEqual(shot["foreground"], "com.android.settings")

    async def test_ui(self):
        ctl, _ = controller()
        ui = await ctl.handle("ui", {})
        self.assertEqual(ui["count"], 4)

    async def test_ui_discarded_if_foreground_changes_with_if_foreground(self):
        for engine, marker in [("helper", "Helper ui"), ("uiautomator", "uiautomator dump")]:
            ctl, adb = controller()
            original = adb.run

            async def run(*args, timeout=None, adb=adb, original=original, marker=marker):
                out = await original(*args, timeout=timeout)
                if len(args) > 1 and marker in args[1]:
                    adb.state["foreground"] = "com.android.settings"
                return out

            adb.run = run
            with self.assertRaises(pc.CommandError, msg=engine) as cm:
                await ctl.handle("ui", {"if_foreground": "com.android.chrome", "engine": engine})
            self.assertEqual(cm.exception.code, "foreground_mismatch")

    async def test_stop_file_halts_long_typing_between_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = os.path.join(tmp, "STOP_PHONE_AGENT")
            ctl, adb = controller(stop_file=stop)
            original = adb.run

            async def run(*args, timeout=None):
                out = await original(*args, timeout=timeout)
                if "input text" in args[1]:
                    open(stop, "w").close()  # the owner stops the agent after the first chunk
                return out

            adb.run = run
            with self.assertRaises(pc.CommandError) as cm:
                await ctl.handle("type", {"text": "x" * (5 * pc.TEXT_CHUNK)})
            self.assertEqual(cm.exception.code, "stopped")
            self.assertEqual(len(adb.inputs()), 1)

    async def test_only_ui_calls_uiautomator(self):
        """uiautomator unbinds accessibility services, so nothing but an explicit /ui may run it."""
        ctl, adb = controller()
        await ctl.handle("state", {})
        await ctl.handle("screenshot", {})
        await ctl.handle("tap", {"x": 1, "y": 1})
        await ctl.handle("type", {"text": "hi"})
        await ctl.handle("key", {"key": "home"})
        await ctl.handle("launch", {"package": "com.android.chrome"})
        self.assertFalse(any("uiautomator" in c[1] for c in adb.calls))

    async def test_timeout_stops_the_command(self):
        ctl, adb = controller({"hang": 5})
        started = time.monotonic()
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("tap", {"x": 1, "y": 1, "timeout": 0.2})
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(adb.inputs(), [])
        adb.state["hang"] = 0
        await ctl.handle("tap", {"x": 1, "y": 1})  # the next command still runs
        self.assertEqual(adb.inputs(), ["input tap 1 1"])

    async def test_long_timeout_is_allowed(self):
        ctl, adb = controller({"hang": 0.3})
        await ctl.handle("tap", {"x": 1, "y": 1, "timeout": 86400})
        self.assertEqual(adb.inputs(), ["input tap 1 1"])

    def test_default_timeout_grows_with_the_work(self):
        self.assertEqual(pc.default_timeout("tap", {}), 30)
        self.assertEqual(pc.default_timeout("type", {"text": "x" * 1000}), 130)
        self.assertEqual(pc.default_timeout("swipe", {"ms": 20000}), 50)

    async def test_stop_file_refuses_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = os.path.join(tmp, "STOP_PHONE_AGENT")
            open(stop, "w").close()
            ctl, adb = controller(stop_file=stop)
            for cmd in ["state", "tap", "key"]:
                with self.assertRaises(pc.CommandError) as cm:
                    await ctl.handle(cmd, {"x": 1, "y": 1, "key": "home"})
                self.assertEqual(cm.exception.code, "stopped")
            self.assertEqual(adb.calls, [])

    async def test_unknown_command(self):
        ctl, _ = controller()
        with self.assertRaises(pc.CommandError):
            await ctl.handle("shell", {"cmd": "reboot"})


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_shapes(self):
        ctl, _ = controller()
        ok = await pc.dispatch(ctl, json.dumps({"id": "a", "cmd": "state", "args": {}}))
        self.assertEqual(ok["id"], "a")
        self.assertTrue(ok["ok"])
        bad = await pc.dispatch(ctl, json.dumps({"id": "b", "cmd": "launch", "args": {"package": "x;y"}}))
        self.assertEqual((bad["ok"], bad["error"]), (False, "invalid_args"))
        self.assertIsNone(await pc.dispatch(ctl, "not json"))
        self.assertIsNone(await pc.dispatch(ctl, json.dumps({"no": "id"})))

    async def test_typed_text_not_logged(self):
        ctl, _ = controller()
        with self.assertLogs("phone_client", level="INFO") as logs:
            await pc.dispatch(ctl, json.dumps({"id": "c", "cmd": "type", "args": {"text": "topsecretvalue"}}))
        self.assertFalse(any("topsecretvalue" in line for line in logs.output))


class FakeRelay(pc.Channel):
    """A command's channel with the relay side faked: chunks sent are recorded and acked at once,
    and `incoming` (chunks, end, cancel) is what the relay sends."""

    def __init__(self, incoming=(), ack=True):
        super().__init__("t", self._record)
        self.sent = []
        self.ack = ack
        for msg in incoming:
            self.inbox.put_nowait(msg)

    async def _record(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.ack and msg.get("type") == "chunk":
            self.inbox.put_nowait({"type": "ack", "seq": msg["seq"]})

    def received(self):
        """The bytes of the chunks sent to the relay, joined."""
        return b"".join(base64.b64decode(m["data"]) for m in self.sent if m["type"] == "chunk")

    def start(self):
        return next(m for m in self.sent if m["type"] == "start")


def upload(data, chunk=5):
    """The messages the relay sends for an upload of `data`."""
    msgs = [{"type": "chunk", "seq": i, "data": base64.b64encode(data[o:o + chunk]).decode()}
            for i, o in enumerate(range(0, len(data), chunk))]
    return msgs + [{"type": "end", "bytes": len(data)}]


TINY = os.path.join(HERE, "data", "tiny.apk")


class ReleaseParsingTests(unittest.TestCase):
    def test_apk_manifest_and_signer_of_a_real_apk(self):
        """tiny.apk is built by aapt2 and signed by apksigner (v2 and v3); apksigner's own digest is TINY_SHA256."""
        self.assertEqual(pc.apk_manifest(TINY), {"package": "com.example.tiny", "version_code": 42, "version_name": "1.2.3-test"})
        with open(TINY, "rb") as f:
            data = f.read()
        cd = pc.central_directory_offset(data[-65557:])
        self.assertEqual(data[cd - 16:cd], pc.APK_SIG_MAGIC)
        size = int.from_bytes(data[cd - 24:cd - 16], "little")
        self.assertEqual(pc.apk_signers(data[cd - size - 8:cd]), [fake_adb.TINY_SHA256])

    def test_apk_manifest_refuses_what_is_not_an_apk(self):
        for bad in [b"", b"\x03\x00\x08\x00", b"<manifest/>"]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                pc.parse_apk_manifest(bad)
        local = os.path.join(tempfile.mkdtemp(), "x.apk")
        with open(local, "wb") as f:
            f.write(b"not a zip")
        with self.assertRaises(pc.CommandError):
            pc.apk_manifest(local)

    def test_package(self):
        dump = fake_adb.package_dump("com.example.tiny", fake_adb.default_state())
        self.assertEqual(pc.parse_package(dump, "com.example.tiny"), {
            "name": "com.example.tiny", "version_name": "1.2.2", "version_code": 41,
            "first_install_time": "2026-10-01 10:00:00", "last_update_time": "2026-11-09 06:58:42",
            "debuggable": True, "enabled_state": "default", "apk_path": fake_adb.TINY_INSTALLED})
        self.assertIsNone(pc.parse_package("Unable to find package: com.x\n", "com.x"))
        # An updated system app is listed twice: the installed one first, the hidden system copy after.
        twice = dump + "\nHidden system packages:\n  Package [com.example.tiny] (1):\n    versionCode=1 minSdk=1\n    versionName=0.1\n"
        self.assertEqual(pc.parse_package(twice, "com.example.tiny")["version_code"], 41)
        chrome = pc.parse_package(fake_adb.package_dump("com.android.chrome", fake_adb.default_state()), "com.android.chrome")
        self.assertFalse(chrome["debuggable"])

    def test_accessibility(self):
        state = fake_adb.default_state()
        res = pc.parse_accessibility(":".join(state["a11y_enabled"]), fake_adb.accessibility_dump(state))
        self.assertEqual(res, {"enabled": [fake_adb.FOCUS, "com.b/com.b.S"], "bound": [fake_adb.FOCUS, "com.b/com.b.S"], "missing": []})
        res = pc.parse_accessibility(":".join(state["a11y_enabled"]), fake_adb.accessibility_dump({**state, "a11y_bound": ["com.b/com.b.S"]}))
        self.assertEqual(res["missing"], [fake_adb.FOCUS])
        unnamed = pc.parse_accessibility("com.b/.S", fake_adb.accessibility_dump({**state, "a11y_unnamed": True}))
        self.assertEqual(unnamed, {"enabled": ["com.b/com.b.S"], "bound": None, "missing": None})
        self.assertEqual(pc.parse_accessibility("null", "     Bound services:{}\n     Enabled services:{}\n"),
                         {"enabled": [], "bound": [], "missing": []})

    def test_install_output(self):
        self.assertIsNone(pc.parse_install("Performing Streamed Install\nSuccess\n"))
        self.assertEqual(pc.parse_install("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package com.a signatures do not match newer version; ignoring!]\n"),
                         "INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package com.a signatures do not match newer version; ignoring!")
        self.assertEqual(pc.parse_install("Failure [INSTALL_FAILED_INVALID_APK]\n"), "INSTALL_FAILED_INVALID_APK")
        self.assertEqual(pc.parse_install("Error: Unable to open file: /x.apk\n"), "Error: Unable to open file: /x.apk")

    def test_settings_output(self):
        self.assertEqual(pc.parse_settings(f"0\n{pc.SPLIT_MARKER}\nNight mode: yes\n"), ("0", "yes"))
        self.assertEqual(pc.parse_settings(f"null\n{pc.SPLIT_MARKER}\nNight mode: auto\n"), (None, "auto"))

    def test_logcat_command(self):
        self.assertEqual(pc.logcat_command({}), ("logcat -d 2>&1 | tail -n 501", 500))
        command, _ = pc.logcat_command({"tag": ["Decrastination", "AndroidRuntime"], "level": "W", "since": 60, "lines": 10, "buffer": "crash"})
        self.assertEqual(command, 'logcat -d -b crash -t "$(( $(date +%s) - 60 )).000" -s Decrastination:W AndroidRuntime:W 2>&1 | tail -n 11')
        self.assertEqual(pc.logcat_command({"since": "2026-11-09 06:58:42", "level": "E"})[0],
                         "logcat -d -t '2026-11-09 06:58:42.000' '*:E' 2>&1 | tail -n 501")
        self.assertEqual(pc.logcat_command({"tag": ["My$Tag"]})[0], "logcat -d -s 'My$Tag:V' 2>&1 | tail -n 501")
        for bad in [{"tag": "a;b"}, {"level": "X"}, {"since": "now"}, {"since": 0}, {"lines": 0}, {"buffer": "x"}, {"tag": []}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                pc.logcat_command(bad)
        self.assertEqual(pc.logcat_result("a\nb\nc\n", 2), {"count": 2, "truncated": True, "lines": ["b", "c"]})
        self.assertEqual(pc.logcat_result("a\n", 2), {"count": 1, "truncated": False, "lines": ["a"]})

    def test_logcat_since_seconds_runs_in_a_real_shell(self):
        """The seconds-ago form is shell arithmetic on the phone: it must give logcat's seconds.milliseconds."""
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no sh")
        command, _ = pc.logcat_command({"since": 90})
        out = subprocess.run([sh, "-c", "echo " + command.split(" -t ")[1].split(" 2>&1")[0]], capture_output=True, text=True).stdout
        self.assertAlmostEqual(float(out), time.time() - 90, delta=5)

    def test_data_path(self):
        for ok in ["files/plan.json", "files", "files/", "shared_prefs/a b.xml", "."]:
            self.assertEqual(pc.data_path({"path": ok}), ok)
        for bad in ["/data/x", "../x", "files/../../x", "a//b", "a;b", "", 5]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                pc.data_path({"path": bad})
        self.assertIsNone(pc.data_path({}, required=False))


class ReleaseCommandTests(unittest.IsolatedAsyncioTestCase):
    async def push_tiny(self, ctl, path="/sdcard/Download/tiny.apk"):
        with open(TINY, "rb") as f:
            await ctl.handle("push", {"path": path}, FakeRelay(upload(f.read(), 4096)))

    async def test_install_over_the_installed_app(self):
        ctl, adb = controller()
        await self.push_tiny(ctl)
        res = await ctl.handle("install", {"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"})
        self.assertEqual(res, {"package": "com.example.tiny", "version_name": "1.2.3-test", "version_code": 42})
        # Streamed in by the shell: the system server cannot open a file in shared storage itself
        self.assertIn("cat /sdcard/Download/tiny.apk | pm install -r -S $(stat -c %s /sdcard/Download/tiny.apk) 2>&1 || true",
                      adb.executed)
        self.assertFalse(any(" -d" in c for c in adb.executed if c.startswith("pm ")))

    async def test_install_refuses_another_package_and_reports_androids_error(self):
        ctl, adb = controller()
        await self.push_tiny(ctl)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("install", {"path": "/sdcard/Download/tiny.apk", "package": "com.android.chrome"})
        self.assertEqual(cm.exception.code, "package_mismatch")
        self.assertFalse(any(c.startswith("pm install") for c in adb.executed))
        adb.state["packages"]["com.example.tiny"]["version_code"] = 50
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("install", {"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"})
        self.assertEqual(cm.exception.code, "install_failed")
        self.assertTrue(cm.exception.detail.startswith("INSTALL_FAILED_VERSION_DOWNGRADE"), cm.exception.detail)
        adb.state["install_error"] = "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match"
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("install", {"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny"})
        self.assertEqual(cm.exception.detail, "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match")
        for bad, code in [({"path": "/sdcard/Download/nope.apk", "package": "com.example.tiny"}, "not_found"),
                          ({"path": "/sdcard/Download/a.txt", "package": "com.example.tiny"}, "invalid_args"),
                          ({"path": "/sdcard/Download/tiny.apk"}, "invalid_args")]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("install", bad)
            self.assertEqual(cm.exception.code, code)

    async def test_install_guard_is_in_the_same_command(self):
        ctl, adb = controller()
        await self.push_tiny(ctl)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("install", {"path": "/sdcard/Download/tiny.apk", "package": "com.example.tiny",
                                         "if_foreground": "com.android.settings"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        self.assertEqual(adb.state["packages"]["com.example.tiny"]["version_code"], 41)
        guarded = [c for c in adb.calls if "pm install" in c[-1]]
        self.assertTrue(guarded[0][-1].startswith("if "), guarded)

    async def test_package_with_its_signing_certificate(self):
        ctl, _ = controller()
        res = await ctl.handle("package", {"name": "com.example.tiny"})
        self.assertEqual(res["signing_sha256"], [fake_adb.TINY_SHA256])
        self.assertEqual(res["version_code"], 41)
        self.assertTrue(res["debuggable"])
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("package", {"name": "com.example.missing"})
        self.assertEqual(cm.exception.code, "not_found")

    async def test_package_of_a_preinstalled_app_names_its_own_apk(self):
        # A preinstalled app's code folder holds Settings.apk, not base.apk: pm path names it
        ctl, adb = controller()
        apk = "/system/priv-app/Settings/Settings.apk"
        adb.state["files"][apk] = adb.state["files"][fake_adb.TINY_INSTALLED]
        adb.state["packages"]["com.android.settings"] = {"version_code": 36, "version_name": "16", "debuggable": False,
                                                         "enabled": 0, "apk": apk}
        res = await ctl.handle("package", {"name": "com.android.settings"})
        self.assertEqual(res["apk_path"], apk)
        self.assertEqual(res["signing_sha256"], [fake_adb.TINY_SHA256])

    async def test_package_apk_streams_the_installed_apk(self):
        ctl, _ = controller()
        relay = FakeRelay()
        res = await ctl.handle("package_apk", {"name": "com.example.tiny"}, relay)
        with open(TINY, "rb") as f:
            self.assertEqual(relay.received(), f.read())
        self.assertEqual(relay.start()["mime"], pc.APK_MIME)
        self.assertEqual(relay.start()["headers"]["X-Version-Code"], "41")
        self.assertEqual(res["path"], fake_adb.TINY_INSTALLED)

    async def test_accessibility_logcat_and_settings(self):
        ctl, adb = controller()
        res = await ctl.handle("accessibility", {})
        self.assertEqual(res["missing"], [])
        res = await ctl.handle("logcat", {"tag": ["Decrastination"], "lines": 1})
        self.assertEqual(res, {"count": 1, "truncated": True, "lines": [fake_adb.LOG_LINES[2]]})
        res = await ctl.handle("settings", {"namespace": "global", "key": "stay_on_while_plugged_in"})
        self.assertEqual(res, {"namespace": "global", "key": "stay_on_while_plugged_in", "value": "0", "night_mode": "yes"})
        res = await ctl.handle("settings", {"namespace": "secure", "key": "unset_key"})
        self.assertIsNone(res["value"])
        for bad in [{"namespace": "other", "key": "a"}, {"namespace": "global", "key": "a;reboot"}]:
            with self.assertRaises(pc.CommandError):
                await ctl.handle("settings", bad)

    async def test_run_as_reads_a_debuggable_apps_files_only(self):
        ctl, _ = controller()
        relay = FakeRelay()
        res = await ctl.handle("run_as", {"package": "com.example.tiny", "path": "files/plan.json"}, relay)
        self.assertEqual(relay.received(), b'{"plan": []}')
        self.assertEqual(res["bytes"], 12)
        self.assertEqual(relay.start()["mime"], "application/json")
        listing = await ctl.handle("run_as_files", {"package": "com.example.tiny", "path": "files"})
        self.assertEqual([(e["name"], e["type"], e["size"]) for e in listing["entries"]], [("plan.json", "file", 12)])
        top = await ctl.handle("run_as_files", {"package": "com.example.tiny"})
        self.assertEqual([e["name"] for e in top["entries"]], ["files"])
        for args, code in [({"package": "com.android.chrome", "path": "files/x"}, "not_debuggable"),
                           ({"package": "com.example.missing", "path": "files/x"}, "not_found"),
                           ({"package": "com.example.tiny", "path": "files/nope.json"}, "not_found"),
                           ({"package": "com.example.tiny", "path": "files"}, "invalid_args"),
                           ({"package": "com.example.tiny", "path": "../../system/x"}, "invalid_args")]:
            with self.assertRaises(pc.CommandError, msg=repr(args)) as cm:
                await ctl.handle("run_as", args, FakeRelay())
            self.assertEqual(cm.exception.code, code, args)

    async def test_intent_as_shell_needs_a_component(self):
        ctl, adb = controller()
        await ctl.handle("intent", {"component": "com.example.tiny/.debug.Command", "extras": {"cmd": "backup-now"}, "as_shell": True})
        self.assertEqual(adb.state["last_intent"][:5], ["am", "start", "-W", "-n", "com.example.tiny/.debug.Command"])
        for bad in [{"action": "a", "as_shell": True}, {"component": "com.a/.B", "as_shell": "yes"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("intent", bad)
            self.assertEqual(cm.exception.code, "invalid_args")


class DecrastinationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """What Decrastination's development.md does over adb, through the relay's commands."""

    async def test_content_query(self):
        ctl, adb = controller()
        res = await ctl.handle("content", {"uri": "content://com.android.calendar/events", "projection": ["_id", "dtstart", "description"],
                                           "where": "calendar_id=3 AND deleted=0", "sort": "dtstart"})
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["rows"][0], {"_id": "1", "dtstart": "1700000000000", "description": "Made up, with a comma, and a\nsecond line"})
        self.assertIsNone(res["rows"][1]["description"])
        self.assertEqual(adb.state["last_content"]["--where"], "calendar_id=3 AND deleted=0")
        without = await ctl.handle("content", {"uri": "content://com.android.calendar/events"})
        self.assertEqual(without["rows"][1]["dtend"], "1700010800000")
        adb.state["calendar"] = []
        self.assertEqual((await ctl.handle("content", {"uri": "content://com.android.calendar/events"}))["rows"], [])
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("content", {"uri": "content://nowhere/x"})
        self.assertEqual(cm.exception.code, "content_failed")
        self.assertTrue(cm.exception.detail.startswith("Error while accessing provider:content://nowhere/x"))
        # A value's later line that starts like an error is still the value
        self.assertEqual(pc.parse_content("Row: 0 description=Notes\nException handling workshop\n", ["description"]),
                         [{"description": "Notes\nException handling workshop"}])
        for bad in [{"uri": "https://x"}, {"uri": "content://a", "projection": ["a;b"]}, {"uri": "content://a", "sort": "a;b"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("content", bad)

    async def test_run_as_archive_is_a_tar(self):
        ctl, _ = controller()
        relay = FakeRelay()
        await ctl.handle("run_as_archive", {"package": "com.example.tiny", "path": "files"}, relay)
        with tarfile.open(fileobj=io.BytesIO(relay.received())) as tar:
            self.assertEqual(tar.getnames(), ["files/plan.json"])
            self.assertEqual(tar.extractfile("files/plan.json").read(), b'{"plan": []}')
        self.assertEqual(relay.start()["mime"], "application/x-tar")
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("run_as_archive", {"package": "com.example.tiny", "path": "nothing"}, FakeRelay())
        self.assertEqual(cm.exception.code, "not_found")

    async def test_run_as_push_then_delete(self):
        ctl, adb = controller()
        root = fake_adb.DATA_ROOT + "com.example.tiny/"
        data = b'{"PowerPlannerUsername": "made-up"}'
        res = await ctl.handle("run_as_push", {"package": "com.example.tiny", "path": "files/credentials.import"}, FakeRelay(upload(data, 7)))
        self.assertEqual(res["bytes"], len(data))
        self.assertEqual(base64.b64decode(adb.state["files"][root + "files/credentials.import"]), data)
        self.assertNotIn(root + "files/.credentials.import.part", adb.state["files"])
        await ctl.handle("run_as_delete", {"package": "com.example.tiny", "path": "files/credentials.import"})
        self.assertNotIn(root + "files/credentials.import", adb.state["files"])
        for cmd, args, code in [("run_as_delete", {"package": "com.example.tiny", "path": "files/credentials.import"}, "not_found"),
                                ("run_as_delete", {"package": "com.example.tiny", "path": "files"}, "invalid_args"),
                                ("run_as_delete", {"package": "com.android.chrome", "path": "files/x"}, "not_debuggable"),
                                ("run_as_delete", {"package": "com.example.tiny", "path": "-rf"}, "invalid_args"),
                                ("run_as_delete", {"package": "com.example.tiny", "path": "files/"}, "invalid_args")]:
            with self.assertRaises(pc.CommandError, msg=repr(args)) as cm:
                await ctl.handle(cmd, args)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("run_as_push", {"package": "com.android.chrome", "path": "files/x"}, FakeRelay(upload(b"x")))
        self.assertEqual(cm.exception.code, "not_debuggable")
        # An existing folder is refused, not written into
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("run_as_push", {"package": "com.example.tiny", "path": "files"}, FakeRelay(upload(b"x")))
        self.assertEqual(cm.exception.code, "invalid_args")
        self.assertNotIn(root + "files/.files.part", adb.state["files"])

    async def test_run_as_push_rechecks_the_foreground_before_replacing(self):
        ctl, adb = controller()
        root = fake_adb.DATA_ROOT + "com.example.tiny/"
        before = adb.state["files"][root + "files/plan.json"]

        class Switching(FakeRelay):
            async def recv_file(self, path):
                n = await super().recv_file(path)
                adb.state["foreground"] = "com.android.settings"  # the owner switched apps during the upload
                return n

        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("run_as_push", {"package": "com.example.tiny", "path": "files/plan.json",
                                             "if_foreground": "com.android.chrome"}, Switching(upload(b"new")))
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        self.assertEqual(adb.state["files"][root + "files/plan.json"], before)
        self.assertNotIn(root + "files/.plan.json.part", adb.state["files"])

    async def test_run_as_kill(self):
        ctl, adb = controller()
        res = await ctl.handle("run_as_kill", {"package": "com.example.tiny", "signal": "STOP"})
        self.assertEqual(res, {"package": "com.example.tiny", "pid": 4321, "signal": "STOP"})
        self.assertEqual(adb.state["signals"], [["com.example.tiny", "-STOP", 4321]])
        adb.state["running"] = {}
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("run_as_kill", {"package": "com.example.tiny", "signal": "KILL"})
        self.assertEqual(cm.exception.code, "not_found")
        for bad in [{"package": "com.example.tiny", "signal": "TERM"}, {"package": "com.android.chrome", "signal": "STOP"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("run_as_kill", bad)

    async def test_night_mode_and_force_stop(self):
        ctl, adb = controller()
        res = await ctl.handle("night_mode", {"mode": "no", "if_foreground": "com.android.chrome"})
        self.assertEqual(res, {"night_mode": "no", "previous": "yes"})
        await ctl.handle("night_mode", {"mode": "yes"})
        self.assertEqual(adb.state["night_mode"], "yes")
        await ctl.handle("force_stop", {"package": "com.android.chrome"})
        self.assertEqual(adb.state["stopped"], ["com.android.chrome"])
        for package in ["com.example.tiny", "com.b", "com.android.systemui", "com.thomaswcode.phonerelay"]:
            with self.assertRaises(pc.CommandError, msg=package) as cm:
                await ctl.handle("force_stop", {"package": package})
            self.assertEqual(cm.exception.code, "stop_refused", package)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("force_stop", {"package": "com.example.missing"})
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(adb.state["stopped"], ["com.android.chrome"])


class NewParsingTests(unittest.TestCase):
    def test_notifications(self):
        items = pc.parse_notifications(fake_adb.NOTIFICATION_DUMP)
        self.assertEqual(len(items), 2)  # the snoozed copy of the first record is skipped
        gm, chrome = items
        self.assertEqual(gm["key"], "0|com.google.android.gm|4|gig:12345|10234")
        self.assertEqual((gm["pkg"], gm["id"], gm["tag"]), ("com.google.android.gm", 4, "gig:12345"))
        self.assertEqual((gm["title"], gm["text"]), ("A sender", "Meeting moved"))
        self.assertEqual(gm["big_text"], "Meeting moved to Thursday.\nBring the slides (final version).")
        self.assertEqual(gm["sub_text"], "someone@example.com")
        self.assertNotIn("showWhen", gm)
        self.assertEqual((chrome["tag"], chrome["title"]), (None, "report.pdf"))

    def test_notification_text_with_parentheses_over_several_lines(self):
        dump = fake_adb.NOTIFICATION_DUMP.replace(
            "Meeting moved to Thursday.\nBring the slides (final version).)",
            "Meeting moved (Thursday)\nBring the slides (final version).\n\n  indented (a quote))")
        gm = pc.parse_notifications(dump)[0]
        self.assertEqual(gm["big_text"], "Meeting moved (Thursday)\nBring the slides (final version).\n\n  indented (a quote)")
        self.assertEqual(gm["sub_text"], "someone@example.com")  # the next extra is still read
        last = pc.parse_notifications(dump)[1]
        self.assertEqual(last["text"], "Download complete")  # the last extra ends at the closing brace

    def test_stat(self):
        self.assertEqual(pc.parse_stat("regular file|12|1700000000|a b|c.txt"),
                         {"name": "a b|c.txt", "type": "file", "size": 12, "modified": 1700000000})
        self.assertEqual(pc.parse_stat("directory|4096|1|Download")["type"], "dir")
        self.assertEqual(pc.parse_stat("regular empty file|0|1|e")["type"], "file")
        self.assertIsNone(pc.parse_stat(pc.MISSING_MARKER))
        self.assertIsNone(pc.parse_stat("x|y|z|w"))

    def test_frame_diff(self):
        a = pc.frame_signature(fake_adb.png(80, 160, 40))
        self.assertEqual(pc.frame_diff(a, pc.frame_signature(fake_adb.png(80, 160, 40))), 0)
        self.assertEqual(pc.frame_diff(a, pc.frame_signature(fake_adb.png(80, 160, 200))), 1)
        self.assertEqual(pc.frame_diff(b"x", b"x"), 0)
        self.assertEqual(pc.frame_diff(b"x", b"y"), 1)

    def test_default_timeouts(self):
        self.assertEqual(pc.default_timeout("wait", {"seconds": 100}), 130)
        self.assertEqual(pc.default_timeout("record", {"seconds": 10}), 80)
        self.assertEqual(pc.default_timeout("pull", {}), pc.TRANSFER_TIMEOUT_S)
        steps = [{"cmd": "tap", "args": {}}, {"cmd": "sleep", "args": {"ms": 2000}}, {"cmd": "key", "args": {"timeout": 5}}]
        self.assertEqual(pc.default_timeout("batch", {"steps": steps}), 30 + 30 + 2 + 5)


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_file_keeps_to_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.bin")
            data = os.urandom(pc.STREAM_CHUNK * 20 + 7)
            with open(path, "wb") as f:
                f.write(data)
            relay = FakeRelay(ack=False)
            task = asyncio.create_task(relay.send_file(path, "application/octet-stream"))
            await asyncio.sleep(0.05)
            chunks = [m for m in relay.sent if m["type"] == "chunk"]
            self.assertEqual(len(chunks), pc.STREAM_WINDOW)  # no more until the relay acks
            relay.ack = True
            for m in chunks:
                relay.inbox.put_nowait({"type": "ack", "seq": m["seq"]})
            self.assertEqual(await task, len(data))
            self.assertEqual(relay.received(), data)
            self.assertEqual(relay.start()["size"], len(data))

    async def test_cancel_stops_a_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.bin")
            with open(path, "wb") as f:
                f.write(b"x" * pc.STREAM_CHUNK * 30)
            relay = FakeRelay(ack=False)
            task = asyncio.create_task(relay.send_file(path, "application/octet-stream"))
            await asyncio.sleep(0.05)
            relay.inbox.put_nowait({"type": "cancel"})
            with self.assertRaises(pc.CommandError) as cm:
                await task
            self.assertEqual(cm.exception.code, "cancelled")

    async def test_recv_file_acks_each_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.bin")
            relay = FakeRelay(upload(b"hello there, phone"))
            self.assertEqual(await relay.recv_file(path), 18)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"hello there, phone")
            self.assertEqual([m["seq"] for m in relay.sent if m["type"] == "ack"], [0, 1, 2, 3])


class AppCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_url(self):
        ctl, adb = controller({"foreground": fake_adb.LAUNCHER})
        await ctl.handle("open", {"url": "https://example.com/a?b=1&c=2"})
        self.assertEqual(adb.state["foreground"], "com.android.chrome")
        self.assertEqual(adb.state["last_intent"][-1], "https://example.com/a?b=1&c=2")

    async def test_open_mailto(self):
        ctl, adb = controller()
        await ctl.handle("open", {"url": "mailto:someone@example.com?subject=Hi%20there&body=Line"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")

    async def test_open_with_package_and_guard(self):
        ctl, adb = controller()
        await ctl.handle("open", {"url": "https://example.com", "package": "com.android.chrome",
                                  "if_foreground": "com.android.chrome"})
        self.assertIn("-p", adb.state["last_intent"])
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("open", {"url": "https://example.com", "if_foreground": "com.android.settings"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")

    async def test_open_unresolvable(self):
        ctl, _ = controller()
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("open", {"url": "nosuchscheme:x"})
        self.assertEqual(cm.exception.code, "launch_failed")
        for bad in [{"url": ""}, {}, {"url": "https://x", "package": "a b"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("open", bad)

    async def test_intent(self):
        ctl, adb = controller()
        await ctl.handle("intent", {"action": "android.intent.action.SEND", "type": "text/plain",
                                    "package": "com.google.android.gm", "categories": ["android.intent.category.DEFAULT"],
                                    "flags": 268435456, "extras": {"android.intent.extra.TEXT": "it's here", "n": 5,
                                                                   "big": 2**40, "f": 1.5, "b": True, "none": None}})
        words = adb.state["last_intent"]
        self.assertEqual(words[:3], ["am", "start", "-W"])
        for seq in (["-a", "android.intent.action.SEND"], ["-t", "text/plain"], ["-f", "268435456"],
                    ["--es", "android.intent.extra.TEXT", "it's here"], ["--ei", "n", "5"], ["--el", "big", str(2**40)],
                    ["--ef", "f", "1.5"], ["--ez", "b", "true"], ["--esn", "none"]):
            i = words.index(seq[0], words.index(seq[0]) if seq[0] not in ("--es", "--ei", "--el", "--ef", "--ez", "--esn") else 0)
            self.assertEqual(words[i:i + len(seq)], seq)

    async def test_intent_broadcast_and_bad_values(self):
        ctl, adb = controller()
        res = await ctl.handle("intent", {"action": "com.example.PING", "broadcast": True})
        self.assertIn("Broadcast completed", res["output"])
        for bad in [{}, {"action": ""}, {"action": "x", "extras": {"k": [1]}}, {"action": "x", "flags": -1},
                    {"action": "x", "categories": "c"}, {"action": "x", "broadcast": "yes"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("intent", bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    async def test_notifications(self):
        ctl, _ = controller()
        res = await ctl.handle("notifications", {})
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["notifications"][1]["text"], "Download complete")

    async def test_statusbar(self):
        ctl, adb = controller()
        for action, verb in pc.STATUSBAR_ACTIONS.items():
            await ctl.handle("statusbar", {"action": action})
            self.assertEqual(adb.state["statusbar"], verb)
        with self.assertRaises(pc.CommandError):
            await ctl.handle("statusbar", {"action": "open"})

    async def test_rotation(self):
        ctl, adb = controller({"rotation": [1, 0]})
        res = await ctl.handle("rotation", {"rotation": 1})
        self.assertEqual(res["previous"], {"auto": True, "user_rotation": 0})
        self.assertEqual(adb.state["rotation"], [0, 1])
        self.assertEqual((await ctl.handle("state", {}))["rotation"], {"auto": False, "user_rotation": 1})
        await ctl.handle("rotation", {"rotation": "auto"})
        self.assertEqual(adb.state["rotation"][0], 1)
        for bad in [4, -1, True, "left", None]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("rotation", {"rotation": bad})

    async def test_every_command_honours_if_foreground(self):
        """Commands without a guarded action of their own are checked before they start."""
        ctl, adb = controller({"foreground": "com.android.settings", "rotation": [1, 0]})
        guard = {"if_foreground": "com.android.chrome"}
        for cmd, args in [("rotation", {"rotation": 1}), ("state", {}), ("notifications", {}),
                          ("files", {"path": "/sdcard"}), ("wait", {"for": "change", "seconds": 0})]:
            with self.assertRaises(pc.CommandError, msg=cmd) as cm:
                await ctl.handle(cmd, {**args, **guard})
            self.assertEqual(cm.exception.code, "foreground_mismatch", cmd)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("push", {"path": "/sdcard/Download/x", **guard}, FakeRelay(upload(b"data")))
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        self.assertEqual(adb.state["rotation"], [1, 0])
        self.assertNotIn("/sdcard/Download/x", adb.state["files"])
        await ctl.handle("rotation", {"rotation": 1, "if_foreground": "com.android.settings"})
        self.assertEqual(adb.state["rotation"], [0, 1])

    async def test_rotation_guard_is_in_the_same_command(self):
        ctl, adb = controller({"rotation": [1, 0]})
        original = adb.run

        async def run(*args, timeout=None):
            if "settings put" in args[1]:
                adb.state["foreground"] = "com.android.settings"
            return await original(*args, timeout=timeout)

        adb.run = run
        with self.assertRaises(pc.CommandError):
            await ctl.handle("rotation", {"rotation": 2, "if_foreground": "com.android.chrome"})
        self.assertEqual(adb.state["rotation"], [1, 0])


class WaitBatchTests(unittest.IsolatedAsyncioTestCase):
    def after_calls(self, adb, n, **change):
        """Change the fake phone's state once `n` more adb calls have run."""
        original, count = adb.run, [0]

        async def run(*args, timeout=None):
            count[0] += 1
            if count[0] == n:
                adb.state.update(change)
            return await original(*args, timeout=timeout)

        adb.run = run

    async def test_wait_for_foreground(self):
        ctl, adb = controller()
        self.after_calls(adb, 3, foreground="com.google.android.gm")
        res = await ctl.handle("wait", {"for": "foreground", "package": "com.google.android.gm", "interval_ms": 0})
        self.assertTrue(res["met"])
        self.assertEqual(res["foreground"], "com.google.android.gm")

    async def test_wait_for_any_foreground_change(self):
        ctl, adb = controller()
        self.after_calls(adb, 2, foreground="com.android.settings")
        res = await ctl.handle("wait", {"for": "foreground", "interval_ms": 0})
        self.assertEqual((res["met"], res["foreground"]), (True, "com.android.settings"))

    async def test_wait_for_change_and_stable(self):
        ctl, adb = controller()
        self.after_calls(adb, 3, shade=220)
        res = await ctl.handle("wait", {"for": "change", "interval_ms": 0})
        self.assertTrue(res["met"])
        self.assertGreater(res["diff"], 0.5)
        res = await ctl.handle("wait", {"for": "stable", "interval_ms": 0})
        self.assertEqual((res["met"], res["diff"]), (True, 0))

    async def test_wait_ends_unmet(self):
        ctl, _ = controller()
        res = await ctl.handle("wait", {"for": "change", "seconds": 0.3, "interval_ms": 50})
        self.assertFalse(res["met"])
        self.assertGreaterEqual(res["elapsed_ms"], 300)
        for bad in [{"for": "text"}, {"for": "change", "seconds": -1}, {"for": "foreground", "package": "x y"}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("wait", bad)

    async def test_batch(self):
        ctl, adb = controller()
        res = await ctl.handle("batch", {"steps": [
            {"cmd": "tap", "args": {"x": 1, "y": 2}},
            {"cmd": "sleep", "args": {"ms": 10}},
            {"cmd": "type", "args": {"text": "hi"}},
            {"cmd": "state"},
        ]})
        self.assertTrue(res["all_ok"])
        self.assertEqual([r["cmd"] for r in res["results"]], ["tap", "sleep", "type", "state"])
        self.assertEqual(adb.inputs(), ["input tap 1 2", "input text hi"])

    async def test_request_is_unsupported(self):
        ctl, adb = controller({"foreground": "com.android.settings"})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("request", {"reason": "x", "if_foreground": "com.android.chrome",
                                         "steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}]})
        self.assertEqual(cm.exception.code, "unsupported")  # not foreground_mismatch: it never gets that far
        self.assertEqual(adb.inputs(), [])

    async def test_batch_stops_on_error_unless_told_not_to(self):
        steps = [{"cmd": "tap", "args": {"x": 1, "y": 1}}, {"cmd": "launch", "args": {"package": "x y"}},
                 {"cmd": "tap", "args": {"x": 2, "y": 2}}]
        ctl, adb = controller()
        res = await ctl.handle("batch", {"steps": steps})
        self.assertEqual((res["completed"], res["all_ok"]), (2, False))
        self.assertEqual(res["results"][1]["error"], "invalid_args")
        ctl, adb = controller()
        res = await ctl.handle("batch", {"steps": steps, "stop_on_error": False})
        self.assertEqual(res["completed"], 3)
        self.assertEqual(adb.inputs(), ["input tap 1 1", "input tap 2 2"])

    async def test_batch_passes_if_foreground_to_its_steps(self):
        ctl, adb = controller({"foreground": "com.android.settings"})
        res = await ctl.handle("batch", {"if_foreground": "com.android.chrome", "steps": [
            {"cmd": "tap", "args": {"x": 1, "y": 1}},
            {"cmd": "tap", "args": {"x": 2, "y": 2, "if_foreground": "com.android.settings"}},
        ], "stop_on_error": False})
        self.assertEqual([r["ok"] for r in res["results"]], [False, True])
        self.assertEqual(adb.inputs(), ["input tap 2 2"])

    async def test_batch_rejects_streaming_steps_and_halts_on_stop(self):
        ctl, _ = controller()
        for bad in [[{"cmd": "pull", "args": {"path": "/x"}}], [{"cmd": "batch"}], [], [{"cmd": "tap", "args": 5}]]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("batch", {"steps": bad})
        with tempfile.TemporaryDirectory() as tmp:
            stop = os.path.join(tmp, "STOP")
            ctl, adb = controller(stop_file=stop)
            original = adb.run

            async def run(*args, timeout=None):
                out = await original(*args, timeout=timeout)
                open(stop, "w").close()
                return out

            adb.run = run
            with self.assertRaises(pc.CommandError) as cm:
                await ctl.handle("batch", {"steps": [{"cmd": "tap", "args": {"x": 1, "y": 1}}] * 3})
            self.assertEqual(cm.exception.code, "stopped")
            self.assertEqual(len(adb.inputs()), 1)


class FileAndRecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_folder_and_file(self):
        ctl, _ = controller()
        res = await ctl.handle("files", {"path": "/sdcard/Download"})
        self.assertEqual(res["entries"], [{"name": "a.txt", "type": "file", "size": 10, "modified": fake_adb.MTIME}])
        res = await ctl.handle("files", {"path": "/sdcard"})
        self.assertEqual([(e["name"], e["type"]) for e in res["entries"]], [("Download", "dir")])
        res = await ctl.handle("files", {"path": "/sdcard/Download/a.txt"})
        self.assertEqual(res["count"], 1)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("files", {"path": "/sdcard/nope"})
        self.assertEqual(cm.exception.code, "not_found")
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("files", {"path": "sdcard"})
        self.assertEqual(cm.exception.code, "invalid_args")

    async def test_pull(self):
        ctl, _ = controller()
        relay = FakeRelay()
        res = await ctl.handle("pull", {"path": "/sdcard/Download/a.txt"}, relay)
        self.assertEqual(res, {"path": "/sdcard/Download/a.txt", "bytes": 10})
        self.assertEqual(relay.received(), b"hello file")
        self.assertEqual(relay.start()["mime"], "text/plain")
        self.assertEqual(relay.start()["headers"], {"X-File-Path": "/sdcard/Download/a.txt"})

    async def test_pull_refusals(self):
        ctl, _ = controller()
        for path, code in [("/sdcard/missing.pdf", "not_found"), ("/sdcard/Download", "invalid_args")]:
            with self.assertRaises(pc.CommandError, msg=path) as cm:
                await ctl.handle("pull", {"path": path}, FakeRelay())
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("pull", {"path": "/sdcard/Download/a.txt"})  # no relay channel
        self.assertEqual(cm.exception.code, "invalid_args")

    async def test_push(self):
        ctl, adb = controller()
        data = bytes(range(256)) * 3
        res = await ctl.handle("push", {"path": "/sdcard/Download/new file.bin"}, FakeRelay(upload(data, 100)))
        self.assertEqual(res["bytes"], len(data))
        self.assertEqual(base64.b64decode(adb.state["files"]["/sdcard/Download/new file.bin"]), data)

    async def test_push_empty_file(self):
        ctl, adb = controller()
        await ctl.handle("push", {"path": "/sdcard/Download/empty"}, FakeRelay(upload(b"")))
        self.assertEqual(adb.state["files"]["/sdcard/Download/empty"], "")

    async def test_record_start_and_stop(self):
        ctl, adb = controller()
        res = await ctl.handle("record_start", {"seconds": 30, "size": "720x1560", "bit_rate": 4000000})
        self.assertEqual(res, {"recording": True, "max_seconds": 30})
        started = next(c for c in adb.executed if "setsid nohup sh -c" in c)
        self.assertIn("--size 720x1560", started)
        self.assertIn("--bit-rate 4000000", started)
        self.assertTrue(started.startswith("{ ") and started.endswith("& }"))
        relay = FakeRelay()
        res = await ctl.handle("record_stop", {}, relay)
        self.assertEqual(res["segments"], 1)
        self.assertEqual(relay.start()["mime"], "video/mp4")
        self.assertEqual(relay.received(), b"fake mp4 segment 0")
        self.assertFalse(any(p.startswith(fake_adb.RECORD_PREFIX) for p in adb.state["files"]))  # cleaned up

    async def test_long_recording_comes_back_as_a_zip(self):
        ctl, _ = controller()
        await ctl.handle("record_start", {"seconds": 400})
        relay = FakeRelay()
        res = await ctl.handle("record_stop", {}, relay)
        self.assertEqual(res["segments"], 3)
        self.assertEqual(relay.start()["mime"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(relay.received())) as z:
            self.assertEqual(z.namelist(), ["segment_000.mp4", "segment_001.mp4", "segment_002.mp4"])
            self.assertEqual(z.read("segment_002.mp4"), b"fake mp4 segment 2")

    async def test_record_in_one_command(self):
        ctl, _ = controller()
        relay = FakeRelay()
        res = await ctl.handle("record", {"seconds": 0.1}, relay)
        self.assertEqual(res["segments"], 1)

    async def test_record_stop_without_a_recording(self):
        ctl, _ = controller()
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("record_stop", {}, FakeRelay())
        self.assertEqual(cm.exception.code, "not_found")
        for bad in [{"seconds": 0}, {"size": "big"}, {"bit_rate": 1.5}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("record_start", bad)


class HelperTests(unittest.IsolatedAsyncioTestCase):
    def helper_calls(self, adb):
        return [c[1] for c in adb.calls if len(c) > 1 and fake_adb.HELPER_PREFIX in c[1]]

    async def test_jar_is_pushed_once_and_again_only_if_it_changed(self):
        ctl, adb = controller()
        await ctl.handle("ui", {})
        await ctl.handle("ui", {})
        self.assertEqual([c[0] for c in adb.calls].count("push"), 1)
        ctl2 = pc.PhoneController(adb, stop_file=ctl.stop_file, focus_retry_delay=0)  # a new client run, same phone
        await ctl2.handle("ui", {})
        self.assertEqual([c[0] for c in adb.calls].count("push"), 1)  # the phone's copy matches
        with tempfile.TemporaryDirectory() as tmp:
            changed = os.path.join(tmp, "helper.jar")
            with open(pc.HELPER_JAR, "rb") as f, open(changed, "wb") as g:
                g.write(f.read() + b"changed")
            ctl3 = pc.PhoneController(adb, stop_file=ctl.stop_file, focus_retry_delay=0, helper_jar=changed)
            await ctl3.handle("ui", {})
        self.assertEqual([c[0] for c in adb.calls].count("push"), 2)

    async def test_missing_jar(self):
        ctl, _ = controller()
        ctl.helper_jar = os.path.join(tempfile.gettempdir(), "no-such-helper.jar")
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("ui", {})
        self.assertEqual(cm.exception.code, "helper_missing")

    async def test_ui_uses_the_helper_not_uiautomator(self):
        ctl, adb = controller()
        ui = await ctl.handle("ui", {})
        self.assertEqual(ui["count"], 4)
        self.assertFalse(any("uiautomator" in c[1] for c in adb.calls if len(c) > 1))
        ui = await ctl.handle("ui", {"engine": "uiautomator"})
        self.assertEqual(ui["count"], 4)
        self.assertTrue(any("uiautomator dump" in c[1] for c in adb.calls if len(c) > 1))
        with self.assertRaises(pc.CommandError):
            await ctl.handle("ui", {"engine": "other"})

    async def test_helper_failure_is_reported_never_replaced_by_uiautomator(self):
        ctl, adb = controller({"helper_error": "NoSuchMethodException: connect"})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("ui", {})
        self.assertEqual(cm.exception.code, "helper_failed")
        self.assertIn("NoSuchMethodException", cm.exception.detail)
        self.assertFalse(any("uiautomator" in c[1] for c in adb.calls if len(c) > 1))

    async def test_a_helper_that_never_started_is_a_failure_not_a_success(self):
        """app_process failing before the helper's main (a deleted or broken jar) prints no status line."""
        ctl, adb = controller()
        await ctl.handle("ui", {})  # pushes the jar
        for cmd, args in [("gesture", {"pointers": [[[1, 1]]]}), ("clipboard_set", {"text": "x"}),
                          ("type", {"text": "café"}), ("ui", {})]:
            adb.state["files"].pop(fake_adb.HELPER_REMOTE, None)  # deleted on the phone behind the client's back
            ctl._helper_pushed = True  # while the client believes it is still there
            with self.assertRaises(pc.CommandError, msg=cmd) as cm:
                await ctl.handle(cmd, args)
            self.assertEqual(cm.exception.code, "helper_failed", cmd)
            self.assertIn("did not run", cm.exception.detail)
        self.assertNotIn("gestures", adb.state)
        self.assertEqual((await ctl.handle("ui", {}))["count"], 4)  # the failure made it push the jar again

    async def test_content_that_looks_like_a_marker_is_just_content(self):
        ctl, adb = controller()
        for text in ["PHONE_RELAY_HELPER_ERROR boom", "PHONE_RELAY_HELPER_OK", "x\nPHONE_RELAY_REFUSED"]:
            await ctl.handle("clipboard_set", {"text": text})
            self.assertEqual((await ctl.handle("clipboard_get", {}))["text"], text)
            res = await ctl.handle("clipboard_get", {"if_foreground": "com.android.chrome"})
            self.assertEqual(res["text"], text)

    async def test_helper_runs_inside_the_guard(self):
        ctl, adb = controller({"foreground": "com.android.settings"})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("gesture", {"pointers": [[[1, 1]]], "if_foreground": "com.android.chrome"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        self.assertNotIn("gestures", adb.state)

    async def test_gesture(self):
        ctl, adb = controller()
        pinch = {"pointers": [[[500, 1000], [300, 800]], [[580, 1080], [780, 1280]]], "ms": 400}
        res = await ctl.handle("gesture", pinch)
        self.assertEqual(res, {"pointers": 2, "ms": 400})
        self.assertEqual(adb.state["gestures"], [pinch])
        await ctl.handle("gesture", {"pointers": [[[50, 100], [60, 110], [70, 120]]], "scale": 0.5})
        self.assertEqual(adb.state["gestures"][1], {"pointers": [[[100, 200], [120, 220], [140, 240]]], "ms": 300})
        for bad in [{}, {"pointers": []}, {"pointers": [[]]}, {"pointers": [[[1]]]}, {"pointers": [[["1", 2]]]},
                    {"pointers": [[[1, 1]]] * 11}, {"pointers": [[[1, 1]]], "ms": -1}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("gesture", bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    async def test_clipboard(self):
        ctl, adb = controller()
        self.assertEqual(await ctl.handle("clipboard_get", {}), {"text": None})
        await ctl.handle("clipboard_set", {"text": "Grüße 👋 \"quoted\" $(x)"})
        self.assertEqual(adb.state["clipboard"], "Grüße 👋 \"quoted\" $(x)")
        self.assertEqual((await ctl.handle("clipboard_get", {}))["text"], "Grüße 👋 \"quoted\" $(x)")
        await ctl.handle("clipboard_set", {"text": ""})  # an empty clip is allowed
        res = await ctl.handle("clipboard_set", {"text": None})  # null empties the clipboard
        self.assertEqual((res, adb.state["clipboard"]), ({"length": 0, "cleared": True}, None))
        with self.assertRaises(pc.CommandError):
            await ctl.handle("clipboard_set", {"text": 5})

    async def test_long_text_goes_in_a_file_that_is_removed(self):
        ctl, adb = controller()
        text = "Ünïcödé " * 2000
        await ctl.handle("clipboard_set", {"text": text})
        self.assertEqual(adb.state["clipboard"], text)
        self.assertNotIn(pc.HELPER_TEXT_REMOTE, adb.state["files"])
        self.assertTrue(any(c[0] == "push" and c[2] == pc.HELPER_TEXT_REMOTE for c in adb.calls))

    async def test_type_pastes_what_keys_cannot_type(self):
        ctl, adb = controller()
        res = await ctl.handle("type", {"text": "Café — 日本語 😀\nnew line"})
        self.assertEqual(res["method"], "paste")
        self.assertEqual(adb.state["pasted"], ["Café — 日本語 😀\nnew line"])
        self.assertEqual(adb.inputs(), [])
        res = await ctl.handle("type", {"text": "plain ascii", "method": "paste"})
        self.assertEqual((res["method"], adb.state["pasted"][-1]), ("paste", "plain ascii"))
        res = await ctl.handle("type", {"text": "plain ascii"})
        self.assertEqual(res["method"], "keys")
        for bad in [{"text": "café", "method": "keys"}, {"text": "x", "method": "voice"}, {"text": ""}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                await ctl.handle("type", bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    async def test_audio(self):
        ctl, adb = controller()
        relay = FakeRelay()
        res = await ctl.handle("audio", {"seconds": 2.5, "source": "mic"}, relay)
        self.assertEqual((res["seconds"], res["source"]), (2.5, "mic"))
        self.assertEqual(relay.start()["mime"], "audio/wav")
        self.assertTrue(relay.received().startswith(b"RIFF"))
        self.assertIn(b"2.5s mic", relay.received())
        self.assertNotIn(pc.AUDIO_REMOTE, adb.state["files"])
        await ctl.handle("audio", {}, FakeRelay())  # default: 10 s of the output
        for bad in [{"source": "speaker"}, {"seconds": 0}]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)):
                await ctl.handle("audio", bad, FakeRelay())

    async def test_wait_for_text(self):
        ctl, adb = controller()
        res = await ctl.handle("wait", {"for": "text", "text": "welcome", "interval_ms": 0})
        self.assertTrue(res["met"])
        self.assertEqual(res["element"]["text"], "Hello & welcome")
        res = await ctl.handle("wait", {"for": "text", "text": "Search or type", "interval_ms": 0})
        self.assertEqual(res["element"]["desc"], "Search or type URL")
        res = await ctl.handle("wait", {"for": "text", "text": "never there", "seconds": 0.2, "interval_ms": 50})
        self.assertFalse(res["met"])
        self.assertFalse(any("uiautomator" in c[1] for c in adb.calls if len(c) > 1))
        with self.assertRaises(pc.CommandError):
            await ctl.handle("wait", {"for": "text"})

    async def test_batch_with_helper_steps(self):
        ctl, adb = controller()
        res = await ctl.handle("batch", {"steps": [
            {"cmd": "clipboard_set", "args": {"text": "from a batch"}},
            {"cmd": "gesture", "args": {"pointers": [[[1, 1], [2, 2]]]}},
            {"cmd": "clipboard_get"},
        ]})
        self.assertTrue(res["all_ok"])
        self.assertEqual(res["results"][2]["result"], {"text": "from a batch"})

    def test_helper_jar_matches_its_source(self):
        """The committed jar was built from the committed source (rebuild with helper/build.py)."""
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "helper"))
        import build
        self.assertEqual(build.jar_hash(pc.HELPER_JAR), build.source_hash(),
                         "phone/helper/phone-relay-helper.jar is stale: run python phone/helper/build.py")


class DevToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devtools = fake_devtools.FakeDevTools()
        await self.devtools.start()
        self.ctl, self.adb = controller({"devtools_port": self.devtools.port})

    async def asyncTearDown(self):
        await self.devtools.stop()

    def no_forward_left(self):
        self.assertEqual(self.adb.state.get("forwards"), [])

    async def test_sockets(self):
        res = await self.ctl.handle("chrome_sockets", {})
        self.assertEqual(res["sockets"], ["chrome_devtools_remote", "webview_devtools_remote_4321"])

    async def test_tabs(self):
        res = await self.ctl.handle("chrome_tabs", {})
        self.assertEqual(res["count"], 3)
        self.assertEqual(res["tabs"][0], {"id": "T1", "type": "page", "title": "Example", "url": "https://example.com/"})
        self.no_forward_left()

    async def test_eval_in_the_front_tab_and_by_id_or_index(self):
        res = await self.ctl.handle("chrome_eval", {"expression": "1+1"})
        self.assertEqual(res, {"threw": False, "type": "number", "value": 2, "description": "2"})
        path, method, params = self.devtools.calls[-1]
        self.assertEqual((path, method), ("/devtools/page/T1", "Runtime.evaluate"))
        self.assertTrue(params["returnByValue"] and params["awaitPromise"])
        self.assertEqual((await self.ctl.handle("chrome_eval", {"expression": "document.title", "tab": "T2"}))["value"], "Other")
        self.assertEqual((await self.ctl.handle("chrome_eval", {"expression": "document.title", "tab": 1}))["value"], "Other")
        self.no_forward_left()

    async def test_eval_exception(self):
        res = await self.ctl.handle("chrome_eval", {"expression": "throw boom"})
        self.assertEqual(res, {"threw": True, "exception": "Error: boom"})

    async def test_cdp_any_method_on_a_tab_or_the_browser(self):
        res = await self.ctl.handle("chrome_cdp", {"method": "Page.navigate", "params": {"url": "https://example.net/"}})
        self.assertEqual(res["result"], {"echo": {"url": "https://example.net/"}, "target": "/devtools/page/T1"})
        res = await self.ctl.handle("chrome_cdp", {"method": "Target.getTargets", "tab": "browser"})
        self.assertEqual(res["result"]["target"], "/devtools/browser")
        with self.assertRaises(pc.CommandError) as cm:
            await self.ctl.handle("chrome_cdp", {"method": "Nope.missing"})
        self.assertEqual(cm.exception.code, "devtools_failed")
        self.assertIn("wasn't found", cm.exception.detail)
        self.no_forward_left()

    async def test_new_activate_close(self):
        res = await self.ctl.handle("chrome_new", {"url": "https://example.net/a b?x=1&y=2"})
        self.assertEqual(res["tab"]["url"], "https://example.net/a b?x=1&y=2")
        new_id = res["tab"]["id"]
        self.assertIn(("/devtools/browser", "Target.createTarget", {"url": "https://example.net/a b?x=1&y=2"}),
                      self.devtools.calls)
        res = await self.ctl.handle("chrome_new", {"url": "https://example.net/#/settings?tab=2"})  # a hash route
        self.assertEqual(res["tab"]["url"], "https://example.net/#/settings?tab=2")
        await self.ctl.handle("chrome_close", {"tab": res["tab"]["id"]})
        self.assertIn("activated", (await self.ctl.handle("chrome_activate", {"tab": new_id}))["reply"])
        res = await self.ctl.handle("chrome_close", {"tab": new_id})
        self.assertEqual(res["tab"], new_id)
        self.assertNotIn(new_id, [t["id"] for t in self.devtools.tabs])
        self.no_forward_left()

    async def test_refusals(self):
        for cmd, args, code in [
            ("chrome_eval", {"expression": "1", "tab": 9}, "not_found"),
            ("chrome_eval", {"expression": "1", "tab": "T9"}, "not_found"),
            ("chrome_eval", {"expression": "1", "tab": "a b"}, "invalid_args"),
            ("chrome_eval", {}, "invalid_args"),
            ("chrome_eval", {"expression": "1", "await_promise": "no"}, "invalid_args"),
            ("chrome_cdp", {"method": "navigate"}, "invalid_args"),
            ("chrome_cdp", {"method": "Page.navigate", "params": []}, "invalid_args"),
            ("chrome_close", {"tab": "browser"}, "invalid_args"),
            ("chrome_tabs", {"socket": "bad name!"}, "invalid_args"),
        ]:
            with self.assertRaises(pc.CommandError, msg=f"{cmd} {args}") as cm:
                await self.ctl.handle(cmd, args)
            self.assertEqual(cm.exception.code, code, f"{cmd} {args}")
        self.no_forward_left()

    async def test_chrome_not_running(self):
        await self.devtools.stop()
        with self.assertRaises(pc.CommandError) as cm:
            await self.ctl.handle("chrome_tabs", {})
        self.assertEqual(cm.exception.code, "devtools_unavailable")
        self.no_forward_left()
        await self.devtools.start()  # for asyncTearDown

    async def test_guarded_and_in_a_batch(self):
        with self.assertRaises(pc.CommandError) as cm:
            await self.ctl.handle("chrome_eval", {"expression": "1", "if_foreground": "com.android.settings"})
        self.assertEqual(cm.exception.code, "foreground_mismatch")
        res = await self.ctl.handle("batch", {"steps": [{"cmd": "chrome_tabs"}, {"cmd": "chrome_eval", "args": {"expression": "1+1"}}]})
        self.assertTrue(res["all_ok"])
        self.assertEqual(res["results"][1]["result"]["value"], 2)


class FakeSocket:
    """The relay side of the connection: messages queued for the client, replies recorded."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self.closed = True
        self.incoming.put_nowait(None)


class ServeTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_run_in_order(self):
        ctl, adb = controller()
        ws = FakeSocket()
        for i in range(3):
            ws.incoming.put_nowait(json.dumps({"id": str(i), "cmd": "tap", "args": {"x": i, "y": i}}))
        task = asyncio.create_task(pc.serve(ctl, ws))
        while len(ws.sent) < 3:
            await asyncio.sleep(0.01)
        ws.incoming.put_nowait(None)
        await task
        self.assertEqual([r["id"] for r in ws.sent], ["0", "1", "2"])
        self.assertEqual(adb.inputs(), ["input tap 0 0", "input tap 1 1", "input tap 2 2"])

    async def test_closing_the_connection_cancels_the_running_command(self):
        """The owner's disable closes the socket: a hung or long command must not carry on."""
        ctl, adb = controller({"hang": 30})
        ws = FakeSocket()
        ws.incoming.put_nowait(json.dumps({"id": "a", "cmd": "tap", "args": {"x": 1, "y": 1, "timeout": 600}}))
        task = asyncio.create_task(pc.serve(ctl, ws))
        await asyncio.sleep(0.2)  # the tap is now waiting on adb
        adb.state["hang"] = 0  # (only that call hangs; the cleanup after it does not)
        started = time.monotonic()
        ws.incoming.put_nowait(None)  # the relay closes the connection
        await asyncio.wait_for(task, 5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(adb.inputs(), [])
        self.assertEqual(ws.sent, [])

    async def test_closing_the_connection_ends_a_background_recording(self):
        """The owner's disable or stop file closes the socket; a detached recording must stop with it."""
        ctl, adb = controller()
        ws = FakeSocket()
        ws.incoming.put_nowait(json.dumps({"id": "r", "cmd": "record_start", "args": {"seconds": 600}}))
        task = asyncio.create_task(pc.serve(ctl, ws))
        while not ws.sent:
            await asyncio.sleep(0.01)
        self.assertTrue(any(p.startswith(fake_adb.RECORD_PREFIX) for p in adb.state["files"]))
        ws.incoming.put_nowait(None)
        await task
        self.assertTrue(any(c[1].startswith("touch /data/local/tmp/phone_relay_rec.stop") for c in adb.calls))
        self.assertFalse(any(p.startswith(fake_adb.RECORD_PREFIX) for p in adb.state["files"]))

    async def test_a_cancel_sent_while_a_transfer_waits_in_the_queue_ends_it_at_once(self):
        """The relay gave up on a queued upload: it must not hold the queue waiting for chunks."""
        ctl, adb = controller()
        ws = FakeSocket()
        ws.incoming.put_nowait(json.dumps({"id": "slow", "cmd": "wait", "args": {"for": "change", "seconds": 0.5}}))
        ws.incoming.put_nowait(json.dumps({"id": "up", "cmd": "push", "args": {"path": "/sdcard/Download/u", "timeout": 300}}))
        ws.incoming.put_nowait(json.dumps({"id": "up", "type": "chunk", "seq": 0, "data": base64.b64encode(b"part").decode()}))
        ws.incoming.put_nowait(json.dumps({"id": "up", "type": "cancel"}))
        task = asyncio.create_task(pc.serve(ctl, ws))
        started = time.monotonic()
        while not any(m.get("id") == "up" and "ok" in m for m in ws.sent):
            await asyncio.sleep(0.01)
        self.assertLess(time.monotonic() - started, 5)
        reply = next(m for m in ws.sent if m.get("id") == "up" and "ok" in m)
        self.assertEqual(reply["error"], "cancelled")
        ws.incoming.put_nowait(None)
        await task

    async def test_upload_chunks_sent_right_behind_the_command_reach_it(self):
        ctl, adb = controller()
        ws = FakeSocket()
        ws.incoming.put_nowait(json.dumps({"id": "u", "cmd": "push", "args": {"path": "/sdcard/Download/up.txt"}}))
        for msg in upload(b"uploaded through the socket", 4):
            ws.incoming.put_nowait(json.dumps({"id": "u", **msg}))
        ws.incoming.put_nowait(json.dumps({"id": "other", "type": "chunk", "seq": 0, "data": ""}))  # no such command
        task = asyncio.create_task(pc.serve(ctl, ws))
        while not any(m.get("ok") is not None for m in ws.sent):
            await asyncio.sleep(0.01)
        ws.incoming.put_nowait(None)
        await task
        reply = next(m for m in ws.sent if "ok" in m)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(base64.b64decode(adb.state["files"]["/sdcard/Download/up.txt"]), b"uploaded through the socket")
        self.assertEqual(len([m for m in ws.sent if m.get("type") == "ack"]), 7)


class SubprocessAdbTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the real Adb wrapper against fake_adb.py run as a separate process."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.log = os.path.join(self.tmp.name, "log.jsonl")
        with open(self.state, "w") as f:
            json.dump({"foreground": "com.android.chrome"}, f)
        os.environ["FAKE_ADB_STATE"] = self.state
        os.environ["FAKE_ADB_LOG"] = self.log
        self.ctl = pc.PhoneController(pc.Adb(FAKE_ADB, "localhost:5555"),
                                      stop_file=os.path.join(self.tmp.name, "STOP"), focus_retry_delay=0)

    async def asyncTearDown(self):
        os.environ.pop("FAKE_ADB_STATE", None)
        os.environ.pop("FAKE_ADB_LOG", None)
        self.tmp.cleanup()

    def logged(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    async def test_serial_and_argument_passing(self):
        await self.ctl.handle("type", {"text": "it's a test"})
        last = self.logged()[-1]
        self.assertEqual(last, ["-s", "localhost:5555", "shell", "input text 'it'\"'\"'s%sa%stest'"])
        await self.ctl.handle("type", {"text": "it's a test", "if_foreground": "com.android.chrome"})
        last = self.logged()[-1]
        self.assertTrue(last[3].endswith("then input text 'it'\"'\"'s%sa%stest'; else echo PHONE_RELAY_REFUSED; fi"),
                        last[3])

    async def test_screenshot_and_launch(self):
        shot = await self.ctl.handle("screenshot", {"scale": 0.25})
        self.assertEqual(shot["width"], 270)
        await self.ctl.handle("key", {"key": "home"})
        with self.assertRaises(pc.CommandError):
            await self.ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": "com.android.chrome"})
        await self.ctl.handle("launch", {"package": "com.android.chrome"})
        await self.ctl.handle("tap", {"x": 1, "y": 1, "if_foreground": "com.android.chrome"})

    async def test_timeout_kills_adb(self):
        with open(self.state, "w") as f:
            json.dump({"foreground": "com.android.chrome", "hang": 20}, f)
        started = time.monotonic()
        with self.assertRaises(pc.CommandError) as cm:
            await self.ctl.handle("state", {"timeout": 1})
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 10)

    async def test_adb_missing(self):
        ctl = pc.PhoneController(pc.Adb(os.path.join(self.tmp.name, "nope")), focus_retry_delay=0)
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("state", {})
        self.assertEqual(cm.exception.code, "adb_failed")


class EnvTests(unittest.TestCase):
    def test_load_env(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# comment\nRELAY_URL=wss://x.workers.dev/device\nDEVICE_TOKEN='abc'\n\nbad line\n")
        try:
            os.chmod(f.name, 0o600)
            self.assertEqual(pc.load_env(f.name), {"RELAY_URL": "wss://x.workers.dev/device", "DEVICE_TOKEN": "abc"})
        finally:
            os.unlink(f.name)

    def test_refuses_plain_ws_to_remote_host(self):
        env = {"RELAY_URL": "ws://example.com/device", "DEVICE_TOKEN": "x"}
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("".join(f"{k}={v}\n" for k, v in env.items()))
        try:
            os.chmod(f.name, 0o600)
            self.assertEqual(pc.main(["--env", f.name]), 2)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
