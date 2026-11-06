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

describe("parseBody", () => {
  it("accepts only JSON objects, of any size", () => {
    expect(parseBody('{"a":1}')).toEqual({ a: 1 });
    expect(parseBody("[1,2]")).toBeNull();
    expect(parseBody("null")).toBeNull();
    expect(parseBody("nope")).toBeNull();
    expect(parseBody(JSON.stringify({ text: "x".repeat(100000) }))).toEqual({ text: "x".repeat(100000) });
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
