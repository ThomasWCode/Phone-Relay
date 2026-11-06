"""Unit tests for phone_client.py, using a fake adb (no phone needed).

Run from the repository root:
    python -m unittest discover -s phone/tests -v
"""

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_adb  # noqa: E402
import phone_client as pc  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.WARNING)  # quiet asyncio debug mode in async tests

FAKE_ADB = os.path.join(HERE, "fake_adb.py")

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

    async def run(self, *args, timeout=None):
        self.calls.append(list(args))
        cmd = " ".join(args[1:])
        m = fake_adb.GUARD_RE.match(cmd)
        if m is None:
            self.executed.append(cmd)
        elif fake_adb.guard_passes(m.group("guard"), self.state):
            self.executed.append(m.group("action"))
        # fake_adb.save_state is a no-op without FAKE_ADB_STATE, so state stays in memory
        return fake_adb.run(cmd, self.state)

    async def shell(self, command, timeout=None):
        return (await self.run("shell", command)).decode()

    async def exec_out(self, command, timeout=None):
        return await self.run("exec-out", command)

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

    def test_ui_drops_other_packages_and_empty_nodes(self):
        els = pc.parse_ui(fake_adb.ui_xml({"foreground": "com.android.chrome"}).encode())
        self.assertTrue(all(e["pkg"] == "com.android.chrome" for e in els))
        self.assertEqual(len(els), 3)  # url bar, text, scroll view; empty View dropped
        url_bar = els[0]
        self.assertEqual(url_bar["center"], [500, 170])
        self.assertTrue(url_bar["editable"] and url_bar["focused"])
        self.assertEqual(els[1]["text"], "Hello & welcome")

    def test_ui_strips_trailing_status_line(self):
        xml = fake_adb.ui_xml({"foreground": "com.android.chrome"}).encode() + b"\nUI hierchary dumped to: /dev/tty"
        self.assertEqual(len(pc.parse_ui(xml)), 3)

    def test_ui_hides_password_text(self):
        xml = fake_adb.ui_xml({"foreground": "com.android.chrome", "password_focused": True})
        xml = xml.replace('text="" resource-id="com.android.chrome:id/url_bar"',
                          'text="secret" resource-id="com.android.chrome:id/url_bar"')
        els = pc.parse_ui(xml.encode())
        self.assertEqual(els[0]["text"], "")
        self.assertTrue(els[0]["password"])

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
    """The focus guard's own grep patterns, applied to realistic dumpsys output."""

    def passes(self, packages=pc.ALLOWED_PACKAGES, **state):
        full = fake_adb.default_state()
        full.update(state)
        return fake_adb.guard_passes(pc.focus_guard(packages), full)

    def test_allowed_app(self):
        self.assertTrue(self.passes(foreground="com.android.chrome"))
        self.assertTrue(self.passes(foreground="com.google.android.gm"))

    def test_other_windows(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher",
                   "com.microsoft.teams", "com.android.chromex", None]:
            self.assertFalse(self.passes(foreground=fg), fg)

    def test_popup_counts_for_its_app(self):
        self.assertTrue(self.passes(foreground="com.android.chrome", popup=True))
        self.assertFalse(self.passes(foreground="com.android.settings", popup=True))

    def test_screen_off(self):
        self.assertFalse(self.passes(foreground="com.android.chrome", awake=False))

    def test_package_dots_are_literal(self):
        # "com.android.chrome" must not match "comXandroidXchrome"
        self.assertFalse(self.passes(foreground="comXandroidXchrome"))

    def test_guarded_shape(self):
        cmd = pc.guarded("input tap 1 2", pc.ALLOWED_PACKAGES)
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
        script = self.STUBS + pc.guarded("echo RAN", pc.ALLOWED_PACKAGES)
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


class TextEscapingTests(unittest.TestCase):
    def test_spaces_become_percent_s(self):
        self.assertEqual(pc.escape_text("hello world"), "hello%sworld")

    def test_shell_metacharacters_are_quoted(self):
        for text in ["a;rm -rf /", "$(reboot)", "`id`", "it's", 'say "hi"', "a|b&c>d<e", "back\\slash"]:
            quoted = pc.escape_text(text)
            # The device shell must see exactly one word equal to the input-escaped text.
            self.assertEqual(shlex.split(quoted), [text.replace(" ", "%s")])

    def test_rejects_bad_text(self):
        for bad in ["", "café", "line\nbreak", "tab\t", "x" * 501, None, 5]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                pc.input_text_command(bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    def test_accepts_max_length(self):
        self.assertEqual(pc.escape_text("x" * 500), "x" * 500)

    @staticmethod
    def typed(command):
        """What `input text` would type for a command from input_text_command."""
        out = ""
        for call in command.split(" && "):
            arg = shlex.split(call)[2]
            out += arg.replace("%s", " ")
        return out

    def test_percent_is_typed_exactly(self):
        for text in ["50% off", "100%", "%", "a%sb", "%s", "%%s", "x%s y%sz", "% %s %", "it's 5% $(x)"]:
            self.assertEqual(self.typed(pc.input_text_command(text)), text, text)

    def test_literal_percent_s_splits_into_two_calls(self):
        self.assertEqual(pc.input_text_command("a%sb"), "input text a% && input text sb")
        self.assertEqual(pc.input_text_command("plain text"), "input text plain%stext")


class KeyTests(unittest.TestCase):
    def test_short_names(self):
        self.assertEqual(pc.resolve_key("back"), "KEYCODE_BACK")
        self.assertEqual(pc.resolve_key("recents"), "KEYCODE_APP_SWITCH")

    def test_raw_keycodes(self):
        for key in ["KEYCODE_PAGE_DOWN", "KEYCODE_ESCAPE", "KEYCODE_DPAD_LEFT", "KEYCODE_MOVE_END", "KEYCODE_A"]:
            self.assertEqual(pc.resolve_key(key), key)

    def test_denied_keycodes(self):
        for key in ["KEYCODE_POWER", "KEYCODE_SLEEP", "KEYCODE_CALL", "KEYCODE_VOLUME_UP", "KEYCODE_MEDIA_PLAY",
                    "KEYCODE_CAMERA", "KEYCODE_ASSIST", "KEYCODE_BRIGHTNESS_DOWN", "KEYCODE_WAKEUP"]:
            with self.assertRaises(pc.CommandError, msg=key):
                pc.resolve_key(key)

    def test_malformed(self):
        for key in ["power", "keycode_back", "KEYCODE_BACK; reboot", "26", "", None, "KEYCODE_"]:
            with self.assertRaises(pc.CommandError, msg=repr(key)):
                pc.resolve_key(key)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_state(self):
        ctl, _ = controller()
        st = await ctl.handle("state", {})
        self.assertEqual(st["foreground"], "com.android.chrome")
        self.assertTrue(st["foreground_allowed"] and st["awake"])
        self.assertEqual(st["battery"], 87)
        self.assertEqual(st["screen"], {"width": 1080, "height": 2340})

    async def test_state_reports_disallowed_foreground_without_refusing(self):
        ctl, _ = controller({"foreground": "com.android.settings"})
        st = await ctl.handle("state", {})
        self.assertFalse(st["foreground_allowed"])

    async def test_tap_allowed(self):
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
        await ctl.handle("tap", {"x": 100, "y": 200})
        self.assertEqual(adb.inputs(), ["input tap 100 200"])

    async def test_every_input_is_guarded(self):
        ctl, adb = controller()
        await ctl.handle("tap", {"x": 1, "y": 1})
        await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5})
        await ctl.handle("type", {"text": "hi"})
        await ctl.handle("key", {"key": "back"})
        await ctl.handle("screenshot", {})
        await ctl.handle("ui", {})
        for call in adb.calls:
            cmd = call[1]
            if cmd.startswith(("input ", "screencap", "am start", "monkey", "uiautomator")):
                self.fail(f"unguarded action: {cmd}")

    async def test_actions_refused_outside_allowlist(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher",
                   "com.microsoft.teams", None]:
            ctl, adb = controller({"foreground": fg})
            for cmd, args in [
                ("tap", {"x": 1, "y": 1}),
                ("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5}),
                ("type", {"text": "hi"}),
                ("key", {"key": "back"}),
                ("key", {"key": "home"}),
                ("screenshot", {}),
                ("ui", {}),
            ]:
                with self.assertRaises(pc.CommandError, msg=f"{fg} {cmd}") as cm:
                    await ctl.handle(cmd, args)
                self.assertEqual(cm.exception.code, "foreground_not_allowed")
            self.assertEqual(adb.inputs(), [])
            self.assertFalse(any("screencap" in c or "uiautomator" in c for c in adb.executed))

    async def test_focus_change_after_check_is_refused_by_guard(self):
        """The race the plan accepted: the app changes between the check and the tap."""
        ctl, adb = controller()
        original = adb.run

        async def run(*args, timeout=None):
            out = await original(*args, timeout=timeout)
            if args[1].startswith("dumpsys window"):  # the Python-side pre-check just passed
                adb.state["foreground"] = "com.android.settings"
            return out

        adb.run = run
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("tap", {"x": 1, "y": 1})
        self.assertEqual(cm.exception.code, "foreground_not_allowed")
        self.assertEqual(adb.inputs(), [])

    async def test_screen_off_refused(self):
        ctl, adb = controller({"awake": False})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("tap", {"x": 1, "y": 1})
        self.assertEqual(cm.exception.code, "screen_off")
        self.assertEqual(adb.inputs(), [])

    async def test_home_from_allowed_app(self):
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
        await ctl.handle("key", {"key": "KEYCODE_PAGE_DOWN"})
        self.assertEqual(adb.inputs()[-1], "input keyevent KEYCODE_PAGE_DOWN")
        for key in ["power", "KEYCODE_POWER", "KEYCODE_VOLUME_DOWN"]:
            with self.assertRaises(pc.CommandError, msg=key):
                await ctl.handle("key", {"key": key})

    async def test_type(self):
        ctl, adb = controller()
        res = await ctl.handle("type", {"text": "hello world"})
        self.assertEqual(res, {"length": 11, "field_input_type": 0x1})
        self.assertEqual(adb.inputs(), ["input text hello%sworld"])

    async def test_type_percent(self):
        ctl, adb = controller()
        await ctl.handle("type", {"text": "a%sb"})
        self.assertEqual(adb.inputs(), ["input text a% && input text sb"])

    async def test_type_refused_in_password_field(self):
        ctl, adb = controller({"password_focused": True})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("type", {"text": "hunter2"})
        self.assertEqual(cm.exception.code, "password_field")
        self.assertEqual(adb.inputs(), [])

    async def test_type_without_text_field_is_allowed(self):
        """No field focused: the agent's call; it gets field_input_type 0 back and checks the screen."""
        ctl, adb = controller({"text_field": False})
        res = await ctl.handle("type", {"text": "hi"})
        self.assertEqual(res["field_input_type"], 0)
        self.assertEqual(adb.inputs(), ["input text hi"])

    async def test_keyboard_taps_refused_on_password_field(self):
        """The keyboard never takes focus, so its keys would pass the focus guard."""
        ctl, adb = controller({"password_focused": True, "keyboard": True})
        for cmd, args in [("tap", {"x": 100, "y": 2000}), ("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5})]:
            with self.assertRaises(pc.CommandError, msg=cmd) as cm:
                await ctl.handle(cmd, args)
            self.assertEqual(cm.exception.code, "password_field")
        self.assertEqual(adb.inputs(), [])
        await ctl.handle("key", {"key": "back"})  # back is how the agent closes the keyboard
        adb.state["keyboard"] = False
        await ctl.handle("tap", {"x": 100, "y": 2000})
        self.assertEqual(adb.inputs()[-1], "input tap 100 2000")

    async def test_swipe(self):
        ctl, adb = controller()
        await ctl.handle("swipe", {"x1": 500, "y1": 1800, "x2": 500, "y2": 600, "ms": 400})
        self.assertEqual(adb.inputs(), ["input swipe 500 1800 500 600 400"])

    async def test_invalid_coordinates(self):
        ctl, adb = controller()
        for args in [{"x": -1, "y": 1}, {"x": 1.5, "y": 1}, {"x": "1", "y": 1}, {"x": True, "y": 1},
                     {"x": 3000, "y": 1}, {"x": 1, "y": 1, "scale": 0}, {"x": 1, "y": 1, "scale": 2}]:
            with self.assertRaises(pc.CommandError, msg=repr(args)) as cm:
                await ctl.handle("tap", args)
            self.assertEqual(cm.exception.code, "invalid_args")
        for ms in [10, 6000, 1.5, "300"]:
            with self.assertRaises(pc.CommandError):
                await ctl.handle("swipe", {"x1": 1, "y1": 1, "x2": 2, "y2": 2, "ms": ms})
        self.assertEqual(adb.inputs(), [])

    async def test_launch_from_home_screen(self):
        ctl, adb = controller({"foreground": fake_adb.LAUNCHER})
        await ctl.handle("launch", {"package": "com.google.android.gm"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")
        self.assertTrue(any(c.startswith("am start -W -n com.google.android.gm/") for c in adb.executed))

    async def test_launch_from_allowed_app(self):
        ctl, adb = controller({"foreground": "com.android.chrome"})
        await ctl.handle("launch", {"package": "com.google.android.gm"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")

    async def test_launch_refused_over_other_apps(self):
        for fg in ["com.android.settings", "com.microsoft.teams", "NotificationShade", None]:
            ctl, adb = controller({"foreground": fg})
            with self.assertRaises(pc.CommandError, msg=str(fg)) as cm:
                await ctl.handle("launch", {"package": "com.android.chrome"})
            self.assertEqual(cm.exception.code, "foreground_not_allowed")
            self.assertFalse(any(c.startswith(("am start", "monkey")) for c in adb.executed))

    async def test_launch_refuses_other_packages(self):
        ctl, adb = controller()
        for pkg in ["com.android.settings", "com.android.chrome; reboot", None]:
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
        self.assertTrue(base64.b64decode(shot["data"]).startswith(b"\xff\xd8"))

    async def test_screenshot_discarded_if_foreground_changes(self):
        ctl, adb = controller()
        original = adb.run

        async def run(*args, timeout=None):
            out = await original(*args, timeout=timeout)
            if "screencap -p" in args[1]:
                adb.state["foreground"] = "com.android.settings"
            return out

        adb.run = run
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("screenshot", {})
        self.assertEqual(cm.exception.code, "foreground_not_allowed")

    async def test_ui(self):
        ctl, _ = controller()
        ui = await ctl.handle("ui", {})
        self.assertEqual(ui["count"], 3)

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
        bad = await pc.dispatch(ctl, json.dumps({"id": "b", "cmd": "launch", "args": {"package": "x"}}))
        self.assertEqual((bad["ok"], bad["error"]), (False, "invalid_args"))
        self.assertIsNone(await pc.dispatch(ctl, "not json"))
        self.assertIsNone(await pc.dispatch(ctl, json.dumps({"no": "id"})))

    async def test_typed_text_not_logged(self):
        ctl, _ = controller()
        with self.assertLogs("phone_client", level="INFO") as logs:
            await pc.dispatch(ctl, json.dumps({"id": "c", "cmd": "type", "args": {"text": "topsecretvalue"}}))
        self.assertFalse(any("topsecretvalue" in line for line in logs.output))


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
        self.assertEqual(last[:3], ["-s", "localhost:5555", "shell"])
        self.assertTrue(last[3].endswith("then input text 'it'\"'\"'s%sa%stest'; else echo PHONE_RELAY_REFUSED; fi"),
                        last[3])

    async def test_screenshot_and_launch(self):
        shot = await self.ctl.handle("screenshot", {"scale": 0.25})
        self.assertEqual(shot["width"], 270)
        await self.ctl.handle("key", {"key": "home"})
        with self.assertRaises(pc.CommandError):
            await self.ctl.handle("tap", {"x": 1, "y": 1})
        await self.ctl.handle("launch", {"package": "com.android.chrome"})
        await self.ctl.handle("tap", {"x": 1, "y": 1})

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
