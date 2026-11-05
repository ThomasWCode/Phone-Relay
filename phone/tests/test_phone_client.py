"""Unit tests for phone_client.py, using a fake adb (no phone needed).

Run from the repository root:
    python -m unittest discover -s phone/tests -v
"""

import base64
import json
import logging
import os
import shlex
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_adb  # noqa: E402
import phone_client as pc  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.WARNING)  # quiet asyncio debug mode in async tests


class InProcessAdb:
    """Calls fake_adb's handlers directly and records every command."""

    def __init__(self, state=None):
        self.state = {"foreground": "com.android.chrome", "awake": True, "password_focused": False}
        self.state.update(state or {})
        self.calls = []

    async def run(self, *args, timeout=None):
        self.calls.append(list(args))
        kind, cmd = args[0], " ".join(args[1:])
        if cmd == "screencap -p":
            return fake_adb.png(fake_adb.WIDTH, fake_adb.HEIGHT)
        if cmd.startswith("uiautomator dump"):
            return fake_adb.ui_xml(self.state).encode()
        # fake_adb.shell persists state through save_state(); keep it in memory instead
        out = fake_adb.shell(cmd, self.state)
        return out

    async def shell(self, command, timeout=None):
        return (await self.run("shell", command)).decode()

    async def exec_out(self, command, timeout=None):
        return await self.run("exec-out", command)

    def inputs(self):
        return [c[1] for c in self.calls if c[0] == "shell" and c[1].startswith("input ")]


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

    def test_focused_password_field(self):
        self.assertTrue(pc.focused_password_field(fake_adb.ui_xml(
            {"foreground": "com.android.chrome", "password_focused": True}).encode()))
        self.assertFalse(pc.focused_password_field(fake_adb.ui_xml(
            {"foreground": "com.android.chrome"}).encode()))


class TextEscapingTests(unittest.TestCase):
    def test_spaces_become_percent_s(self):
        self.assertEqual(pc.escape_text("hello world"), "hello%sworld")

    def test_shell_metacharacters_are_quoted(self):
        for text in ["a;rm -rf /", "$(reboot)", "`id`", "it's", 'say "hi"', "a|b&c>d<e", "back\\slash"]:
            quoted = pc.escape_text(text)
            # The device shell must see exactly one word equal to the input-escaped text.
            self.assertEqual(shlex.split(quoted), [text.replace(" ", "%s")])

    def test_rejects_bad_text(self):
        for bad in ["", "50%", "café", "line\nbreak", "tab\t", "x" * 501, None, 5]:
            with self.assertRaises(pc.CommandError, msg=repr(bad)) as cm:
                pc.escape_text(bad)
            self.assertEqual(cm.exception.code, "invalid_args")

    def test_accepts_max_length(self):
        self.assertEqual(pc.escape_text("x" * 500), "x" * 500)


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

    async def test_actions_refused_outside_allowlist(self):
        for fg in ["com.android.settings", "NotificationShade", "com.sec.android.app.launcher", None]:
            ctl, adb = controller({"foreground": fg})
            for cmd, args in [
                ("tap", {"x": 1, "y": 1}),
                ("swipe", {"x1": 1, "y1": 1, "x2": 5, "y2": 5}),
                ("type", {"text": "hi"}),
                ("key", {"key": "back"}),
                ("screenshot", {}),
                ("ui", {}),
            ]:
                with self.assertRaises(pc.CommandError, msg=f"{fg} {cmd}") as cm:
                    await ctl.handle(cmd, args)
                self.assertEqual(cm.exception.code, "foreground_not_allowed")
            self.assertEqual(adb.inputs(), [])
            self.assertFalse(any("screencap" in " ".join(c) for c in adb.calls))

    async def test_screen_off_refused(self):
        ctl, adb = controller({"awake": False})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("tap", {"x": 1, "y": 1})
        self.assertEqual(cm.exception.code, "screen_off")
        self.assertEqual(adb.inputs(), [])

    async def test_home_always_allowed(self):
        ctl, adb = controller({"foreground": "com.android.settings"})
        await ctl.handle("key", {"key": "home"})
        self.assertEqual(adb.inputs(), ["input keyevent KEYCODE_HOME"])

    async def test_keys(self):
        ctl, adb = controller()
        for key, code in pc.KEYCODES.items():
            if key == "home":
                continue
            await ctl.handle("key", {"key": key})
            self.assertEqual(adb.inputs()[-1], f"input keyevent {code}")
        with self.assertRaises(pc.CommandError):
            await ctl.handle("key", {"key": "power"})

    async def test_type(self):
        ctl, adb = controller()
        res = await ctl.handle("type", {"text": "hello world"})
        self.assertEqual(res, {"length": 11})
        self.assertEqual(adb.inputs(), ["input text hello%sworld"])

    async def test_type_refused_in_password_field(self):
        ctl, adb = controller({"password_focused": True})
        with self.assertRaises(pc.CommandError) as cm:
            await ctl.handle("type", {"text": "hunter2"})
        self.assertEqual(cm.exception.code, "password_field")
        self.assertEqual(adb.inputs(), [])

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

    async def test_launch(self):
        ctl, adb = controller({"foreground": "com.sec.android.app.launcher"})
        await ctl.handle("launch", {"package": "com.google.android.gm"})
        self.assertEqual(adb.state["foreground"], "com.google.android.gm")
        self.assertTrue(any(c[1].startswith("am start -W -n com.google.android.gm/") for c in adb.calls))

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
            if args[1] == "screencap -p":
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

    async def test_stop_file_refuses_everything(self):
        with tempfile.NamedTemporaryFile() as f:
            ctl, adb = controller(stop_file=f.name)
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
    """Exercises the real Adb wrapper against fake_adb.py as an executable."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.log = os.path.join(self.tmp.name, "log.jsonl")
        with open(self.state, "w") as f:
            json.dump({"foreground": "com.android.chrome"}, f)
        os.environ["FAKE_ADB_STATE"] = self.state
        os.environ["FAKE_ADB_LOG"] = self.log
        wrapper = os.path.join(self.tmp.name, "adb")
        with open(wrapper, "w") as f:
            f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(os.path.join(HERE, 'fake_adb.py'))} \"$@\"\n")
        os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)
        self.ctl = pc.PhoneController(pc.Adb(wrapper, "localhost:5555"),
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
        self.assertEqual(last[3], "input text 'it'\"'\"'s%sa%stest'")

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
