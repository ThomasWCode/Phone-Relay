import { describe, expect, it } from "vitest";
import { AGENT_ROUTES, findRoute, parseBody, summariseArgs, validateCommand, validateText, type CommandName } from "../src/validate";

const q = (s = "") => new URLSearchParams(s);
const v = (cmd: CommandName, body: Record<string, unknown> | null = null, query = "") => validateCommand(cmd, q(query), body);
const args = (r: ReturnType<typeof v>) => (r.ok ? r.command.args : null);

describe("validateCommand", () => {
  it("accepts reads without arguments and always sends a timeout", () => {
    expect(v("state")).toEqual({ ok: true, command: { cmd: "state", args: { timeout: 30 } } });
    expect(v("ui")).toEqual({ ok: true, command: { cmd: "ui", args: { timeout: 30 } } });
    expect(AGENT_ROUTES["/ui"]).toEqual({ method: "GET", cmd: "ui" });
  });

  it("screenshot scale defaults to 0.5 and can be any number above 0", () => {
    expect(args(v("screenshot"))).toEqual({ scale: 0.5, timeout: 30 });
    for (const s of ["1", "0.05", "1.5", "0.01"]) expect(v("screenshot", null, `scale=${s}`).ok, s).toBe(true);
    for (const s of ["0", "abc", "-1", "NaN", ""]) expect(v("screenshot", null, `scale=${s}`).ok, s).toBe(false);
  });

  it("tap takes any numbers", () => {
    expect(args(v("tap", { x: 10, y: 20 }))).toEqual({ x: 10, y: 20, scale: 1, timeout: 30 });
    for (const body of [{ x: 10, y: 20, scale: 0.5 }, { x: -1, y: 0 }, { x: 1.5, y: 0 }, { x: 50000, y: 0 }]) {
      expect(v("tap", body).ok, JSON.stringify(body)).toBe(true);
    }
    for (const body of [{ x: "1", y: 0 }, { x: 1 }, { x: true, y: 1 }, { x: 1, y: 1, scale: 0 }, { x: 1, y: 1, scale: -2 }]) {
      expect(v("tap", body).ok, JSON.stringify(body)).toBe(false);
    }
  });

  it("swipe takes any duration from 0, and its default timeout grows with it", () => {
    const base = { x1: 1, y1: 2, x2: 3, y2: 4 };
    expect(args(v("swipe", base))).toEqual({ ...base, ms: 300, scale: 1, timeout: 30.3 });
    expect(v("swipe", { ...base, ms: 0 }).ok).toBe(true);
    expect(args(v("swipe", { ...base, ms: 60000 }))?.timeout).toBe(90);
    for (const ms of [-1, "300", true]) expect(v("swipe", { ...base, ms }).ok, String(ms)).toBe(false);
  });

  it("type takes any text: keys for ASCII, pasted otherwise", () => {
    expect(v("type", { text: "Hello, world! (test) $x `y`" }).ok).toBe(true);
    expect(v("type", { text: "100% and %s" }).ok).toBe(true);
    expect(v("type", { text: "line one\nline two\ttabbed\r\n" }).ok).toBe(true);
    expect(args(v("type", { text: "x".repeat(10000) }))?.timeout).toBe(1030);
    for (const text of ["café", "日本語 😀", "bell\u0007"]) expect(v("type", { text }).ok, text).toBe(true);
    expect(args(v("type", { text: "ascii", method: "paste" }))).toEqual({ text: "ascii", method: "paste", timeout: 30.5 });
    for (const body of [{ text: "café", method: "keys" }, { text: "x", method: "voice" }, { text: "" }, { text: 5 }, { text: null }]) {
      expect(v("type", body).ok, JSON.stringify(body)).toBe(false);
    }
  });

  it("key takes short names, any KEYCODE_* name and key numbers", () => {
    for (const key of ["back", "home", "recents", "enter", "tab", "delete"]) expect(v("key", { key }).ok).toBe(true);
    for (const key of ["KEYCODE_PAGE_DOWN", "KEYCODE_POWER", "KEYCODE_VOLUME_UP", "KEYCODE_MEDIA_PLAY", "KEYCODE_CALL", "26", 26]) {
      expect(v("key", { key }).ok, String(key)).toBe(true);
    }
    expect(args(v("key", { key: "KEYCODE_POWER", longpress: true }))).toEqual({ key: "KEYCODE_POWER", longpress: true, timeout: 30 });
    for (const key of ["power", "KEYCODE_back", "KEYCODE_", "KEYCODE_BACK;reboot", "26;x", "", -1, 1.5]) {
      expect(v("key", { key }).ok, String(key)).toBe(false);
    }
    expect(v("key", { key: "back", longpress: "yes" }).ok).toBe(false);
  });

  it("launch takes any package name", () => {
    for (const p of ["com.google.android.gm", "com.android.settings", "com.microsoft.teams"]) expect(v("launch", { package: p }).ok).toBe(true);
    for (const p of ["com.android.chrome ", "com.android.chrome;reboot", "a b", "", 1]) {
      expect(v("launch", { package: p }).ok, String(p)).toBe(false);
    }
  });

  it("every command takes timeout and if_foreground", () => {
    expect(args(v("tap", { x: 1, y: 1, timeout: 600, if_foreground: "com.android.chrome" }))).toEqual({
      x: 1, y: 1, scale: 1, timeout: 600, if_foreground: ["com.android.chrome"],
    });
    expect(args(v("screenshot", null, "timeout=5&if_foreground=com.a.b,com.c.d"))).toEqual({
      scale: 0.5, timeout: 5, if_foreground: ["com.a.b", "com.c.d"],
    });
    expect(args(v("state", { timeout: 1e9 }))?.timeout).toBe(1e9);
    for (const timeout of [0, -1, "5", null]) expect(v("tap", { x: 1, y: 1, timeout }).ok, String(timeout)).toBe(false);
    for (const f of ["", [], ["a;b"], [5], { a: 1 }]) expect(v("tap", { x: 1, y: 1, if_foreground: f }).ok, JSON.stringify(f)).toBe(false);
    expect(v("state", null, "timeout=").ok).toBe(false);
  });

  it("drops unknown fields and names them", () => {
    const r = v("tap", { x: 1, y: 1, scal: 0.5, z: 1 });
    expect(r.ok && r.command.ignored).toEqual(["scal", "z"]);
    expect(r.ok && r.command.args).toEqual({ x: 1, y: 1, scale: 1, timeout: 30 });
    const g = v("screenshot", null, "scale=0.4&foo=1");
    expect(g.ok && g.command.ignored).toEqual(["foo"]);
    const clean = v("tap", { x: 1, y: 1 });
    expect(clean.ok && clean.command.ignored).toBeUndefined();
  });
});

describe("app, file and control commands", () => {
  it("open takes a URL and an optional package", () => {
    expect(args(v("open", { url: "mailto:a@example.com?subject=Hi" }))).toEqual({ url: "mailto:a@example.com?subject=Hi", timeout: 30 });
    expect(v("open", { url: "https://x", package: "com.android.chrome" }).ok).toBe(true);
    for (const body of [{}, { url: "" }, { url: 5 }, { url: "https://x", package: "a b" }]) expect(v("open", body).ok, JSON.stringify(body)).toBe(false);
  });

  it("intent takes an intent's parts and typed extras", () => {
    const body = { action: "android.intent.action.SEND", type: "text/plain", categories: ["c"], flags: 1, extras: { t: "x", n: 1, b: true, z: null }, broadcast: false };
    expect(args(v("intent", body))).toEqual({ ...body, timeout: 30 });
    for (const bad of [{}, { type: "text/plain" }, { action: "" }, { action: "a", extras: { k: [1] } }, { action: "a", extras: [] },
      { action: "a", flags: -1 }, { action: "a", flags: 1.5 }, { action: "a", categories: [""] }, { action: "a", broadcast: 1 }]) {
      expect(v("intent", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("statusbar, rotation and notifications", () => {
    for (const action of ["notifications", "settings", "collapse"]) expect(v("statusbar", { action }).ok).toBe(true);
    expect(v("statusbar", { action: "open" }).ok).toBe(false);
    for (const rotation of [0, 1, 2, 3, "auto"]) expect(v("rotation", { rotation }).ok).toBe(true);
    for (const rotation of [4, -1, 1.5, "left", null, true]) expect(v("rotation", { rotation }).ok, String(rotation)).toBe(false);
    expect(args(v("notifications"))).toEqual({ timeout: 30 });
  });

  it("wait gets its own seconds on top of the default deadline", () => {
    expect(args(v("wait", { for: "change" }))).toEqual({ for: "change", timeout: 40 });
    expect(args(v("wait", { for: "foreground", package: "com.a.b", seconds: 120, interval_ms: 0, threshold: 0.1 }))?.timeout).toBe(150);
    for (const bad of [{}, { for: "text" }, { for: "change", seconds: -1 }, { for: "change", interval_ms: "1" }, { for: "foreground", package: "a b" }]) {
      expect(v("wait", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("batch validates each step and sums their deadlines", () => {
    const r = v("batch", { steps: [{ cmd: "tap", args: { x: 1, y: 2 } }, { cmd: "sleep", args: { ms: 1500 } }, { cmd: "type", args: { text: "x".repeat(100), timeout: 5 } }, { cmd: "state" }] });
    expect(r.ok).toBe(true);
    expect(args(r)).toEqual({
      steps: [
        { cmd: "tap", args: { x: 1, y: 2, scale: 1, timeout: 30 } },
        { cmd: "sleep", args: { ms: 1500 } },
        { cmd: "type", args: { text: "x".repeat(100), timeout: 5 } },
        { cmd: "state", args: { timeout: 30 } },
      ],
      timeout: 30 + 30 + 1.5 + 5 + 30,
    });
    const typo = v("batch", { steps: [{ cmd: "tap", args: { x: 1, y: 1, scal: 2 }, delay: 1 }], stop_on_error: false });
    expect(typo.ok && typo.command.ignored).toEqual(["steps[0].delay", "steps[0].args.scal"]);
    for (const bad of [{}, { steps: [] }, { steps: [{ cmd: "pull", args: { path: "/x" } }] }, { steps: [{ cmd: "batch" }] },
      { steps: [{ cmd: "tap", args: { x: "1", y: 1 } }] }, { steps: [{ cmd: "sleep" }] }, { steps: [5] }, { steps: [{ cmd: "tap", args: [] }] },
      { steps: [{ cmd: "state" }], stop_on_error: "no" }]) {
      expect(v("batch", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("files, pull and push need an absolute path; transfers get five minutes", () => {
    expect(args(v("files", null, "path=/sdcard/Download"))).toEqual({ path: "/sdcard/Download", timeout: 30 });
    expect(args(v("pull", null, "path=/sdcard/a%20b.pdf"))).toEqual({ path: "/sdcard/a b.pdf", timeout: 300 });
    expect(args(v("push", null, "path=/sdcard/x&timeout=900"))?.timeout).toBe(900);
    for (const q of ["", "path=", "path=sdcard/x"]) expect(v("pull", null, q).ok, q).toBe(false);
    expect(AGENT_ROUTES["/files/push"]).toEqual({ method: "POST", cmd: "push", raw: true });
  });

  it("record takes seconds, size and bit rate", () => {
    expect(args(v("record", null, "seconds=20&size=720x1560&bit_rate=4000000"))).toEqual({
      seconds: 20, size: "720x1560", bit_rate: 4000000, timeout: 100,
    });
    expect(args(v("record_start", { seconds: 600 }))?.timeout).toBe(30);
    expect(args(v("record_stop"))?.timeout).toBe(300);
    for (const q of ["seconds=0", "seconds=-1", "size=big", "bit_rate=1.5"]) expect(v("record", null, q).ok, q).toBe(false);
  });
});

describe("helper commands", () => {
  it("routes by path and method", () => {
    expect(findRoute("/clipboard", "GET")).toEqual({ method: "GET", cmd: "clipboard_get" });
    expect(findRoute("/clipboard", "POST")).toEqual({ method: "POST", cmd: "clipboard_set" });
    expect(findRoute("/clipboard", "PUT")).toBe("method");
    expect(findRoute("/tap", "GET")).toBe("method");
    expect(findRoute("/nope", "GET")).toBeNull();
  });

  it("gesture takes 1 to 10 paths of [x, y] points", () => {
    const pinch = { pointers: [[[500, 1000], [300, 800]], [[580, 1080], [780, 1280]]], ms: 400 };
    expect(args(v("gesture", pinch))).toEqual({ ...pinch, scale: 1, timeout: 30.4 });
    for (const bad of [{}, { pointers: [] }, { pointers: [[]] }, { pointers: [[[1]]] }, { pointers: [[["1", 2]]] },
      { pointers: Array(11).fill([[1, 1]]) }, { pointers: [[[1, 1]]], ms: -1 }, { pointers: [[[1, 1]]], scale: 0 }]) {
      expect(v("gesture", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("clipboard, audio, ui engine and waiting for text", () => {
    expect(args(v("clipboard_get"))).toEqual({ timeout: 30 });
    expect(v("clipboard_set", { text: "" }).ok).toBe(true);
    expect(v("clipboard_set", { text: 5 }).ok).toBe(false);
    expect(v("clipboard_set", { text: null }).ok).toBe(true);
    expect(v("clipboard_set", {}).ok).toBe(false);
    expect(args(v("audio", null, "seconds=5&source=mic"))).toEqual({ seconds: 5, source: "mic", timeout: 70 });
    for (const q of ["source=speaker", "seconds=0"]) expect(v("audio", null, q).ok, q).toBe(false);
    expect(args(v("ui", null, "engine=uiautomator"))).toEqual({ engine: "uiautomator", timeout: 30 });
    expect(v("ui", null, "engine=x").ok).toBe(false);
    expect(args(v("wait", { for: "text", text: "Sign in" }))).toEqual({ for: "text", text: "Sign in", timeout: 40 });
    expect(v("wait", { for: "text" }).ok).toBe(false);
    expect(v("batch", { steps: [{ cmd: "gesture", args: { pointers: [[[1, 1]]] } }, { cmd: "clipboard_get" }] }).ok).toBe(true);
    expect(v("batch", { steps: [{ cmd: "audio" }] }).ok).toBe(false);
  });

  it("keeps clipboard text out of the log", () => {
    expect(summariseArgs({ cmd: "clipboard_set", args: { text: "secret", timeout: 30 } })).toEqual({ length: 6 });
  });
});

describe("Chrome DevTools commands", () => {
  it("take tabs by id, index or the browser, and keep scripts out of the log", () => {
    expect(args(v("chrome_tabs"))).toEqual({ timeout: 30 });
    expect(args(v("chrome_tabs", null, "socket=webview_devtools_remote_123"))).toEqual({ socket: "webview_devtools_remote_123", timeout: 30 });
    expect(args(v("chrome_eval", { expression: "document.title", tab: 1, await_promise: false }))).toEqual({
      expression: "document.title", tab: 1, await_promise: false, timeout: 30,
    });
    expect(v("chrome_eval", { expression: "1", tab: "browser" }).ok).toBe(true);
    expect(args(v("chrome_cdp", { method: "Page.navigate", params: { url: "https://example.com/" }, tab: "ABC-123" }))).toEqual({
      method: "Page.navigate", params: { url: "https://example.com/" }, tab: "ABC-123", timeout: 30,
    });
    expect(v("chrome_new", { url: "https://example.com/" }).ok).toBe(true);
    expect(v("chrome_close", { tab: "T1" }).ok).toBe(true);
    for (const [cmd, body] of [
      ["chrome_eval", {}], ["chrome_eval", { expression: "1", tab: -1 }], ["chrome_eval", { expression: "1", tab: "a b" }],
      ["chrome_eval", { expression: "1", await_promise: "no" }], ["chrome_cdp", { method: "navigate" }],
      ["chrome_cdp", { method: "Page.navigate", params: [] }], ["chrome_new", {}], ["chrome_close", { tab: "browser" }],
      ["chrome_tabs", { socket: "bad name!" }],
    ] as [CommandName, Record<string, unknown>][]) {
      expect(v(cmd, body).ok, `${cmd} ${JSON.stringify(body)}`).toBe(false);
    }
    expect(summariseArgs({ cmd: "chrome_eval", args: { expression: "secret()", tab: 0 } })).toEqual({ tab: 0, expression_length: 8 });
    expect(summariseArgs({ cmd: "chrome_cdp", args: { method: "Input.insertText", params: { text: "secret" } } }))
      .toEqual({ method: "Input.insertText", params: ["text"] });
    expect(v("batch", { steps: [{ cmd: "chrome_eval", args: { expression: "1" } }] }).ok).toBe(true);
    expect(v("batch", { steps: [{ cmd: "chrome_sockets" }] }).ok).toBe(false);
  });
});

describe("requests to the owner", () => {
  it("POST asks the phone, GET reads the relay's record", () => {
    expect(findRoute("/request", "POST")).toEqual({ method: "POST", cmd: "request" });
    expect(findRoute("/request", "GET")).toEqual({ method: "GET", cmd: "request_status" });
    expect(findRoute("/request", "PUT")).toBe("method");
  });

  it("needs a reason, and waits five minutes for the answer by default", () => {
    expect(args(v("request", { reason: "Check the bus times" }))).toEqual({ reason: "Check the bus times", timeout: 330 });
    expect(args(v("request", { reason: "x", seconds: 60 }))?.timeout).toBe(90);
    for (const bad of [{}, { reason: "" }, { reason: "  " }, { reason: 1 }, { reason: "x", seconds: 0 }, { reason: "x", seconds: "60" }]) {
      expect(v("request", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("queues a batch's steps, checked as a batch's, with their time on top of the wait", () => {
    const r = v("request", { reason: "x", seconds: 60, steps: [{ cmd: "launch", args: { package: "com.android.chrome" } }, { cmd: "sleep", args: { ms: 2000 } }], stop_on_error: false, if_foreground: "com.android.chrome" });
    expect(r.ok).toBe(true);
    // 60 s for the answer, then the batch: 30 s, the launch's 30 s and the 2 s sleep.
    expect(args(r)).toEqual({
      reason: "x", seconds: 60, stop_on_error: false, if_foreground: ["com.android.chrome"], timeout: 122,
      steps: [{ cmd: "launch", args: { package: "com.android.chrome", timeout: 30 } }, { cmd: "sleep", args: { ms: 2000 } }],
    });
    for (const bad of [{ reason: "x", steps: [] }, { reason: "x", steps: [{ cmd: "request" }] }, { reason: "x", steps: [{ cmd: "pull", args: { path: "/x" } }] }]) {
      expect(v("request", bad).ok, JSON.stringify(bad)).toBe(false);
    }
    // stop_on_error with nothing queued has nothing to apply to.
    expect(v("request", { reason: "x", stop_on_error: true })).toMatchObject({ ok: true, command: { ignored: ["stop_on_error"] } });
  });

  it("logs the reason's length and the steps' shapes, never their text", () => {
    expect(summariseArgs({ cmd: "request", args: { reason: "secret plan", seconds: 60, steps: [{ cmd: "type", args: { text: "secret" } }], timeout: 120 } }))
      .toEqual({ seconds: 60, steps: [{ cmd: "type", args: { length: 6 } }], timeout: 120, reason_length: 11 });
  });
});

describe("parseBody", () => {
  it("accepts only JSON objects, of any size", () => {
    expect(parseBody('{"a":1}')).toEqual({ a: 1 });
    expect(parseBody("[1,2]")).toBeNull();
    expect(parseBody("null")).toBeNull();
    expect(parseBody("nope")).toBeNull();
    expect(parseBody(JSON.stringify({ text: "x".repeat(100000) }))).toEqual({ text: "x".repeat(100000) });
  });
});

describe("building, installing and checking a release", () => {
  it("install takes an APK's path on the phone and the package it must be", () => {
    expect(args(v("install", { path: "/sdcard/Download/app.apk", package: "com.example.app" })))
      .toEqual({ path: "/sdcard/Download/app.apk", package: "com.example.app", timeout: 120 });
    expect(findRoute("/install", "POST")).toEqual({ method: "POST", cmd: "install" });
    for (const bad of [{ path: "app.apk", package: "com.a" }, { path: "/sdcard/a.apk" }, { path: "/sdcard/a.apk", package: "a b" }, {}]) {
      expect(v("install", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });

  it("accessibility, package and package/apk", () => {
    expect(args(v("accessibility"))).toEqual({ timeout: 30 });
    expect(args(v("package", null, "name=com.example.app"))).toEqual({ name: "com.example.app", timeout: 30 });
    expect(args(v("package_apk", null, "name=com.example.app"))).toEqual({ name: "com.example.app", timeout: 300 });
    expect(findRoute("/package/apk", "GET")).toEqual({ method: "GET", cmd: "package_apk" });
    for (const q of ["", "name=", "name=a%20b", "name=com.a;reboot"]) expect(v("package", null, q).ok, q).toBe(false);
  });

  it("logcat: tags, a level, since (seconds ago or a time), a line cap and a buffer", () => {
    expect(args(v("logcat"))).toEqual({ lines: 500, timeout: 30 });
    expect(args(v("logcat", null, "tag=Decrastination,AndroidRuntime&level=w&since=60&lines=50&buffer=crash"))).toEqual({
      tag: ["Decrastination", "AndroidRuntime"], level: "W", since: 60, lines: 50, buffer: "crash", timeout: 30,
    });
    expect(args(v("logcat", null, "since=2026-11-09T06:58:42"))?.since).toBe("2026-11-09 06:58:42");
    expect(args(v("logcat", null, "since=11-09 06:58:42.123"))?.since).toBe("11-09 06:58:42.123");
    for (const q of ["tag=a;b", "tag=", "level=X", "since=0", "since=yesterday", "since=2026-11-09", "lines=0", "lines=1.5", "lines=x", "buffer=other"]) {
      expect(v("logcat", null, q).ok, q).toBe(false);
    }
  });

  it("run-as takes a package and a path inside its data folder", () => {
    expect(args(v("run_as", null, "package=com.example.app&path=files/plan.json")))
      .toEqual({ package: "com.example.app", path: "files/plan.json", timeout: 300 });
    expect(args(v("run_as_files", null, "package=com.example.app"))).toEqual({ package: "com.example.app", timeout: 30 });
    expect(args(v("run_as_files", null, "package=com.example.app&path=files/"))?.path).toBe("files/");
    for (const q of ["package=com.a", "package=com.a&path=/data/x", "package=com.a&path=../b", "package=com.a&path=files/../../x",
      "package=com.a&path=a//b", "package=com.a&path=a;b", "path=files/x"]) {
      expect(v("run_as", null, q).ok, q).toBe(false);
    }
  });

  it("settings reads one setting by namespace and name", () => {
    expect(args(v("settings", null, "namespace=global&key=stay_on_while_plugged_in")))
      .toEqual({ namespace: "global", key: "stay_on_while_plugged_in", timeout: 30 });
    for (const q of ["namespace=global", "namespace=other&key=a", "namespace=secure&key=a;b", "key=a"]) expect(v("settings", null, q).ok, q).toBe(false);
  });

  it("check takes a package, services that must be bound, and accept_gone", () => {
    expect(args(v("check", null, "package=com.example.app&services=com.example.app/.FocusService,com.b/com.b.S&accept_gone=true"))).toEqual({
      package: "com.example.app", services: ["com.example.app/.FocusService", "com.b/com.b.S"], accept_gone: true, timeout: 30,
    });
    for (const q of ["", "package=com.a&services=com.a", "package=com.a&accept_gone=yes"]) expect(v("check", null, q).ok, q).toBe(false);
  });

  it("the new reads may be batch steps, and the files they stream may not", () => {
    const r = v("batch", { steps: [{ cmd: "install", args: { path: "/sdcard/a.apk", package: "com.a" } }, { cmd: "accessibility" }] });
    expect(r.ok && r.command.args.timeout).toBe(30 + 120 + 30);
    for (const cmd of ["package_apk", "run_as", "check"]) expect(v("batch", { steps: [{ cmd, args: {} }] }).ok, cmd).toBe(false);
  });
});

describe("what Decrastination's workflow does over adb", () => {
  it("content: a read-only query of a provider", () => {
    expect(args(v("content", null, "uri=content://com.android.calendar/events&projection=dtstart,dtend&where=calendar_id=3 AND deleted=0&sort=dtstart DESC")))
      .toEqual({ uri: "content://com.android.calendar/events", projection: ["dtstart", "dtend"], where: "calendar_id=3 AND deleted=0", sort: "dtstart DESC", timeout: 30 });
    for (const q of ["", "uri=https://x", "uri=content://a b", "uri=content://a&projection=a;b", "uri=content://a&sort=a;b", "uri=content://a&where="]) {
      expect(v("content", null, q).ok, q).toBe(false);
    }
    expect(summariseArgs({ cmd: "content", args: { uri: "content://a", where: "title='secret'" } })).toEqual({ uri: "content://a", where_length: 14 });
  });

  it("run-as: an archive, a push, a delete and a signal", () => {
    expect(args(v("run_as_archive", null, "package=com.a&path=files"))).toEqual({ package: "com.a", path: "files", timeout: 300 });
    expect(findRoute("/run-as/push", "POST")).toEqual({ method: "POST", cmd: "run_as_push", raw: true });
    expect(args(v("run_as_push", null, "package=com.a&path=files/credentials.import"))).toEqual({ package: "com.a", path: "files/credentials.import", timeout: 300 });
    expect(v("run_as_push", null, "package=com.a").ok).toBe(false);
    expect(v("run_as_push", null, "package=com.a&path=../x").ok).toBe(false);
    for (const path of ["files/", "-rf", "files/-x", "files/--to-command=x"]) expect(v("run_as_push", null, `package=com.a&path=${path}`).ok, path).toBe(false);
    expect(v("run_as_files", null, "package=com.a&path=files/").ok).toBe(true);
    expect(args(v("run_as_delete", { package: "com.a", path: "files/ai-prompts.json" }))).toEqual({ package: "com.a", path: "files/ai-prompts.json", timeout: 30 });
    expect(args(v("run_as_kill", { package: "com.a", signal: "STOP" }))).toEqual({ package: "com.a", signal: "STOP", timeout: 30 });
    for (const signal of ["TERM", "stop", 9, undefined]) expect(v("run_as_kill", { package: "com.a", signal }).ok, String(signal)).toBe(false);
  });

  it("night mode and force-stop", () => {
    for (const mode of ["yes", "no", "auto"]) expect(v("night_mode", { mode }).ok).toBe(true);
    expect(v("night_mode", { mode: "dark" }).ok).toBe(false);
    expect(args(v("force_stop", { package: "com.google.android.youtube" }))).toEqual({ package: "com.google.android.youtube", timeout: 30 });
    expect(v("force_stop", { package: "a b" }).ok).toBe(false);
  });
});

describe("intent flags by name, and as the shell", () => {
  it("flags take a number, or names (and numbers) combined", () => {
    expect(args(v("intent", { action: "a", flags: ["FLAG_ACTIVITY_NEW_TASK"] }))?.flags).toBe(268435456);
    expect(args(v("intent", { action: "a", flags: ["FLAG_ACTIVITY_NEW_TASK", "FLAG_ACTIVITY_CLEAR_TASK", 1] }))?.flags).toBe(0x10008001);
    expect(args(v("intent", { action: "a", flags: ["FLAG_ACTIVITY_NO_HISTORY", "FLAG_ACTIVITY_SINGLE_TOP"] }))?.flags).toBe(0x60000000);
    for (const flags of [[], ["NEW_TASK"], ["FLAG_ACTIVITY_NEW_TASK", -1], ["toString"], "FLAG_ACTIVITY_NEW_TASK"]) {
      expect(v("intent", { action: "a", flags }).ok, JSON.stringify(flags)).toBe(false);
    }
  });

  it("as_shell needs an explicit component", () => {
    const body = { component: "com.example.app/.debug.Command", extras: { cmd: "backup-now" }, as_shell: true };
    expect(args(v("intent", body))).toEqual({ ...body, timeout: 30 });
    expect(v("intent", { action: "a", as_shell: false }).ok).toBe(true);
    for (const bad of [{ action: "a", as_shell: true }, { package: "com.a", as_shell: true }, { component: "com.a", as_shell: true },
      { component: "com.a/.B", as_shell: "yes" }]) {
      expect(v("intent", bad).ok, JSON.stringify(bad)).toBe(false);
    }
  });
});

describe("logging", () => {
  it("never includes typed text", () => {
    expect(summariseArgs({ cmd: "type", args: { text: "secret" } })).toEqual({ length: 6 });
    expect(summariseArgs({ cmd: "tap", args: { x: 1, y: 2, scale: 1 } })).toEqual({ x: 1, y: 2, scale: 1 });
    expect(summariseArgs({ cmd: "batch", args: { steps: [{ cmd: "type", args: { text: "secret" } }, { cmd: "tap", args: { x: 1 } }] } }))
      .toEqual({ steps: [{ cmd: "type", args: { length: 6 } }, { cmd: "tap", args: { x: 1 } }] });
    expect(summariseArgs({ cmd: "intent", args: { action: "a", extras: { "android.intent.extra.TEXT": "secret" } } }))
      .toEqual({ action: "a", extras: ["android.intent.extra.TEXT"] });
  });

  it("validateText reports reasons", () => {
    expect(validateText("ok")).toBeNull();
    expect(validateText("50%")).toBeNull();
    expect(validateText("café")).toMatch(/ASCII/);
  });
});
