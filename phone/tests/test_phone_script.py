"""Unit tests for scripts/phone.py, against a local HTTP server (no relay, no phone needed).

Run from the repository root:
    python -m unittest discover -s phone/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("phone_script", os.path.join(ROOT, "scripts", "phone.py"))
phone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone)

TOKEN = "t0ken-" + "x" * 40


class Relay(BaseHTTPRequestHandler):
    """Answers like the relay: JSON for most paths, JPEG bytes for /shot, a 401 without the token."""
    seen: list = []

    def log_message(self, *args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length") or 0)
        Relay.seen.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            code, body, kind = 401, b'{"ok":false,"error":"unauthorized"}', "application/json"
        elif self.path == "/shot":
            code, body, kind = 200, b"\xff\xd8\xff\xe0jpeg", "image/jpeg"
        elif self.path == "/missing":
            code, body, kind = 404, b'{"ok":false,"error":"not_found"}', "application/json"
        else:
            code, body, kind = 200, b'{"ok":true}', "application/json"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("X-Foreground", "com.android.chrome")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = handle_request


class PhoneScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Relay)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Relay.seen = []
        self.env = {phone.TOKEN_VARIABLE: TOKEN, phone.URL_VARIABLE: self.url}

    def call(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = phone.main(list(argv), self.env if env is None else env, out, err)
        return code, out.getvalue(), err.getvalue()

    def test_a_get_sends_the_token_and_a_user_agent_and_prints_the_json(self):
        code, out, err = self.call("get", "/state")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True})
        method, path, headers, _ = Relay.seen[0]
        self.assertEqual((method, path), ("GET", "/state"))
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["User-Agent"], phone.USER_AGENT)
        self.assertIn("X-Foreground: com.android.chrome", err)  # the reply's X-* headers go to stderr

    def test_the_token_is_never_printed(self):
        for argv in (("GET", "/state"), ("GET", "/missing"), ("GET", "/shot", "-o", os.devnull)):
            _, out, err = self.call(*argv)
            self.assertNotIn(TOKEN, out + err, argv)
        # not even when the relay refuses it
        _, out, err = self.call("GET", "/state", env={**self.env, phone.TOKEN_VARIABLE: "wrong-" + TOKEN})
        self.assertNotIn(TOKEN, out + err)

    def test_a_post_sends_its_json_and_an_empty_object_when_given_none(self):
        self.call("POST", "/tap", '{"x": 1, "y": 2}')
        self.call("POST", "/key")
        self.assertEqual(Relay.seen[0][2]["Content-Type"], "application/json")
        self.assertEqual(json.loads(Relay.seen[0][3]), {"x": 1, "y": 2})
        self.assertEqual(Relay.seen[1][3], b"{}")

    def test_a_file_is_sent_as_raw_bytes(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x00raw bytes\xff")
        try:
            code, _, _ = self.call("POST", "/files/push?path=/sdcard/a.bin", "--data-file", f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(code, 0)
        self.assertEqual(Relay.seen[0][2]["Content-Type"], "application/octet-stream")
        self.assertEqual(Relay.seen[0][3], b"\x00raw bytes\xff")

    def test_a_reply_that_is_not_json_needs_an_output_file(self):
        code, out, err = self.call("GET", "/shot")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("-o FILE", err)
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "s.jpg")
            code, out, err = self.call("GET", "/shot", "-o", target)
            self.assertEqual((code, out), (0, ""))
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8\xff\xe0jpeg")

    def test_an_error_reply_is_printed_and_exits_1(self):
        code, out, _ = self.call("GET", "/missing")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "not_found")
        code, out, _ = self.call("GET", "/state", env={**self.env, phone.TOKEN_VARIABLE: "wrong"})
        self.assertEqual((code, json.loads(out)["error"]), (1, "unauthorized"))

    def test_the_token_goes_only_to_a_path_on_the_relay(self):
        for bad in ("state", "//evil.example/x", "https://evil.example/", "/a b", "/a\\b", "/a\nb"):
            code, _, err = self.call("GET", bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("path must start", err)
        self.assertEqual(Relay.seen, [])

    def test_setup_mistakes_exit_2_with_a_reason(self):
        code, _, err = self.call("GET", "/state", env={phone.URL_VARIABLE: self.url})
        self.assertEqual(code, 2)
        self.assertIn(phone.TOKEN_VARIABLE, err)
        self.assertIn("Git Bash", err)
        code, _, err = self.call("GET", "/state", env={phone.TOKEN_VARIABLE: TOKEN, phone.URL_VARIABLE: "http://example.com"})
        self.assertEqual(code, 2)
        self.assertIn("https://", err)
        code, _, err = self.call("GET", "/state", "{}")
        self.assertEqual(code, 2)
        self.assertIn("no body", err)
        code, _, err = self.call("POST", "/tap", "{not json")
        self.assertEqual(code, 2)
        self.assertIn("valid JSON", err)
        code, _, err = self.call("POST", "/tap", "{}", "--data-file", "x")
        self.assertEqual(code, 2)
        self.assertEqual(Relay.seen, [])

    def test_the_relay_address_comes_from_the_variable_or_phone_env(self):
        self.assertEqual(phone.relay_base({phone.URL_VARIABLE: "wss://relay.example.workers.dev/device"}), "https://relay.example.workers.dev")
        self.assertEqual(phone.relay_base({phone.URL_VARIABLE: "https://relay.example.workers.dev/"}), "https://relay.example.workers.dev")
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("OTHER=1\nRELAY_URL=wss://from-file.example.workers.dev/device\n")
        try:
            self.assertEqual(phone.relay_base({}, f.name), "https://from-file.example.workers.dev")
        finally:
            os.unlink(f.name)
        with self.assertRaises(phone.SetupError):
            phone.relay_base({}, os.path.join(tempfile.gettempdir(), "no-such-file.env"))


if __name__ == "__main__":
    unittest.main()
