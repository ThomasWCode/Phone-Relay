import { describe, expect, it } from "vitest";
import { AGENT_ROUTES, parseBody, summariseArgs, validateCommand, validateText, type CommandName } from "../src/validate";

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

  it("type takes ASCII of any length, newlines and tabs included", () => {
    expect(v("type", { text: "Hello, world! (test) $x `y`" }).ok).toBe(true);
    expect(v("type", { text: "100% and %s" }).ok).toBe(true);
    expect(v("type", { text: "line one\nline two\ttabbed\r\n" }).ok).toBe(true);
    expect(args(v("type", { text: "x".repeat(10000) }))?.timeout).toBe(1030);
    for (const text of ["", "café", "bell\u0007", 5, null]) expect(v("type", { text }).ok, JSON.stringify(text)).toBe(false);
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
  });

  it("validateText reports reasons", () => {
    expect(validateText("ok")).toBeNull();
    expect(validateText("50%")).toBeNull();
    expect(validateText("café")).toMatch(/ASCII/);
  });
});
