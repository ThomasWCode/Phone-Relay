#!/usr/bin/env python3
"""Calls the relay's agent API from a session on your own computer.

A cloud session gets its agent token from its environment's network secret. A session on your own computer
has no such secret, so this sends the token from the environment variable PHONE_RELAY_AGENT_TOKEN. It never
prints the token, and sends it only to the relay. The relay's address is PHONE_RELAY_URL, else RELAY_URL in
phone/.env (https://HOST, or the phone client's wss://HOST/device).

    python scripts/phone.py GET /state
    python scripts/phone.py GET "/screenshot?scale=0.4" -o screen.jpg
    python scripts/phone.py POST /tap '{"x": 540, "y": 1170, "if_foreground": "com.android.chrome"}'
    python scripts/phone.py POST "/files/push?path=/sdcard/Download/notes.txt" --data-file notes.txt

JSON replies are printed; a reply that is not JSON (a screenshot, a recording, a file) needs -o FILE. The
status line and the reply's X-* headers go to stderr. Exit status: 0 for a 2xx reply, 1 for any other,
2 for a mistake in the call or the setup. Standard library only. On Windows, user environment variables are
visible in PowerShell but not in Git Bash.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_VARIABLE = "PHONE_RELAY_AGENT_TOKEN"
URL_VARIABLE = "PHONE_RELAY_URL"
# Cloudflare rejects some default User-Agents (Python's urllib among them) with 403 error code: 1010.
USER_AGENT = "phone-relay-script/1"
# A recording or a long wait can take minutes on the phone; the relay's own deadlines apply first.
DEFAULT_WAIT_S = 330


class SetupError(Exception):
    """Something wrong with the call or the setup, not with the relay's reply."""


def relay_base(env: dict, env_file: str | None = None) -> str:
    """The relay's https:// base address, from PHONE_RELAY_URL or RELAY_URL in phone/.env."""
    url = env.get(URL_VARIABLE, "").strip()
    if not url:
        path = env_file or os.path.join(ROOT, "phone", ".env")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("RELAY_URL="):
                        url = line.split("=", 1)[1].strip()
        except OSError:
            pass
    if not url:
        raise SetupError(f"no relay address: set {URL_VARIABLE} (https://HOST) or RELAY_URL in phone/.env")
    url = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    if url.endswith("/device"):
        url = url[: -len("/device")]
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in ("127.0.0.1", "localhost")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise SetupError("the relay address must be https:// (http:// only for this computer, to test)")
    return url.rstrip("/")


def agent_token(env: dict) -> str:
    token = env.get(TOKEN_VARIABLE, "").strip()
    if not token:
        raise SetupError(
            f"{TOKEN_VARIABLE} is not set. Keep the agent token in that environment variable "
            "(on Windows, a user variable: PowerShell sees it, Git Bash does not)"
        )
    return token


def build_request(method: str, path: str, body: bytes | None, base: str, token: str, raw_body: bool) -> urllib.request.Request:
    """The request, with the token. `path` must be a path on the relay: the token goes nowhere else."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(c in path for c in "\r\n "):
        raise SetupError("path must start with a single / and be a path on the relay, e.g. /state or \"/screenshot?scale=0.4\"")
    headers = {"Authorization": "Bearer " + token, "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream" if raw_body else "application/json"
    return urllib.request.Request(base + path, data=body, headers=headers, method=method)


def run(args: argparse.Namespace, env: dict, out, err) -> int:
    base = relay_base(env, args.env_file)
    token = agent_token(env)
    body: bytes | None = None
    if args.method == "GET" and (args.body or args.data_file):
        raise SetupError("a GET takes no body: put its fields in the path, e.g. /screenshot?scale=0.4")
    if args.data_file:
        if args.body:
            raise SetupError("give a JSON body or --data-file, not both")
        try:
            with open(args.data_file, "rb") as f:
                body = f.read()
        except OSError as e:
            raise SetupError(f"could not read {args.data_file}: {e.strerror}")
    elif args.body:
        try:
            json.loads(args.body)
        except ValueError as e:
            raise SetupError(f"the body is not valid JSON: {e}")
        body = args.body.encode("utf-8")
    elif args.method != "GET":
        body = b"{}"  # the relay takes a JSON object on every POST, even an empty one
    request = build_request(args.method, args.path, body, base, token, raw_body=bool(args.data_file))
    try:
        with urllib.request.urlopen(request, timeout=args.wait) as resp:
            status, headers, data = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        status, headers, data = e.code, e.headers, e.read()
        e.close()
    except urllib.error.URLError as e:
        raise SetupError(f"could not reach the relay: {e.reason}")
    kind = headers.get("Content-Type", "")
    err.write(f"HTTP {status} {kind} {len(data)} bytes\n")
    for name, value in headers.items():
        if name.lower().startswith("x-"):
            err.write(f"{name}: {value}\n")
    if args.output:
        try:
            with open(args.output, "wb") as f:
                f.write(data)
        except OSError as e:
            raise SetupError(f"could not write {args.output}: {e.strerror}")
        err.write(f"saved to {args.output}\n")
    elif "json" in kind or not data:
        out.write(data.decode("utf-8", errors="replace") + "\n")
    else:
        err.write("the reply is not JSON: use -o FILE to save it\n")
        return 2
    return 0 if 200 <= status < 300 else 1


def main(argv: list[str] | None = None, env: dict | None = None, out=None, err=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("method", type=str.upper, choices=["GET", "POST", "PUT"])
    parser.add_argument("path", help='a path on the relay, with its query: /state, "/screenshot?scale=0.4"')
    parser.add_argument("body", nargs="?", help="a JSON body (POST/PUT); an empty object if none")
    parser.add_argument("--data-file", help="send this file's bytes as the body (for /files/push)")
    parser.add_argument("-o", "--output", help="save the reply to this file (for screenshots, recordings, files)")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_S, help="seconds to wait for the reply (default %(default)s)")
    parser.add_argument("--env-file", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return run(args, os.environ if env is None else env, out or sys.stdout, err or sys.stderr)
    except SetupError as e:
        (err or sys.stderr).write(f"phone.py: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
