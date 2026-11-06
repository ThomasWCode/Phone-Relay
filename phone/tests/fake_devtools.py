"""A fake Chrome DevTools endpoint for tests: the HTTP /json/* calls and a page's WebSocket.

    devtools = FakeDevTools()
    await devtools.start()          # devtools.port is where `adb forward` would point
    ...
    await devtools.stop()

It sends an event before each reply, as Chrome does, so clients must match replies by id.
Run on its own (python fake_devtools.py PORT) it serves until killed, for the end-to-end test.
"""

import asyncio
import json
import sys
import urllib.parse

from websockets.asyncio.server import serve


class FakeDevTools:
    def __init__(self):
        self.tabs = [
            {"id": "T1", "type": "page", "title": "Example", "url": "https://example.com/"},
            {"id": "T2", "type": "page", "title": "Other", "url": "https://example.org/"},
            {"id": "SW1", "type": "service_worker", "title": "sw", "url": "https://example.com/sw.js"},
        ]
        self.calls = []  # (target path, method, params)
        self.port = None
        self._server = None

    async def start(self, port=0):
        self._server = await serve(self._ws, "127.0.0.1", port, process_request=self._http, max_size=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    def _http(self, connection, request):
        path = request.path
        if path.startswith("/devtools/"):
            return None  # a WebSocket handshake
        if path == "/json/list":
            return connection.respond(200, json.dumps(self.tabs))
        if path == "/json/version":
            return connection.respond(200, json.dumps({
                "Browser": "Chrome/150.0", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/B1"}))
        if path.startswith("/json/new?"):
            tab = {"id": f"T{len(self.tabs) + 1}", "type": "page", "title": "",
                   "url": urllib.parse.unquote(path.split("?", 1)[1])}
            self.tabs.insert(0, tab)
            return connection.respond(200, json.dumps(tab))
        if path.startswith("/json/close/"):
            tab_id = path.rsplit("/", 1)[1]
            self.tabs = [t for t in self.tabs if t["id"] != tab_id]
            return connection.respond(200, "Target is closing")
        if path.startswith("/json/activate/"):
            return connection.respond(200, "Target activated")
        return connection.respond(404, "Unknown command")

    async def _ws(self, ws):
        path = ws.request.path
        async for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.calls.append((path, method, params))
            await ws.send(json.dumps({"method": "Page.frameStoppedLoading", "params": {}}))  # an event first
            if method == "Runtime.evaluate":
                expr = params.get("expression")
                if expr.startswith("throw"):
                    reply = {"result": {"result": {"type": "object"}, "exceptionDetails": {
                        "text": "Uncaught", "exception": {"description": "Error: " + expr[6:]}}}}
                elif expr == "document.title":
                    title = next((t["title"] for t in self.tabs if path.endswith("/" + t["id"])), "")
                    reply = {"result": {"result": {"type": "string", "value": title}}}
                else:
                    reply = {"result": {"result": {"type": "number", "value": 2, "description": "2"}}}
            elif method == "Nope.missing":
                reply = {"error": {"code": -32601, "message": "'Nope.missing' wasn't found"}}
            else:
                reply = {"result": {"echo": params, "target": path}}
            await ws.send(json.dumps({"id": msg["id"], **reply}))


async def _main(port):
    devtools = FakeDevTools()
    await devtools.start(port)
    print(devtools.port, flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
