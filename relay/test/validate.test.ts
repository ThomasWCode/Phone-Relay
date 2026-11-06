import { describe, expect, it } from "vitest";
import { parseBody, summariseArgs, validateCommand, validateText, type CommandName } from "../src/validate";

const q = (s = "") => new URLSearchParams(s);
const v = (cmd: CommandName, body: Record<string, unknown> | null = null, query = "") => validateCommand(cmd, q(query), body);

describe("validateCommand", () => {
  it("accepts reads without arguments", () => {
    expect(v("state")).toEqual({ ok: true, command: { cmd: "state", args: {} } });
    expect(v("ui")).toEqual({ ok: true, command: { cmd: "ui", args: {} } });
  });

  it("screenshot scale defaults to 0.5 and is bounded", () => {
    expect(v("screenshot")).toEqual({ ok: true, command: { cmd: "screenshot", args: { scale: 0.5 } } });
    expect(v("screenshot", null, "scale=1").ok).toBe(true);
    for (const s of ["0", "0.05", "1.5", "abc", "-1", "NaN"]) expect(v("screenshot", null, `scale=${s}`).ok).toBe(false);
  });

  it("tap needs integer coordinates in range", () => {
    expect(v("tap", { x: 10, y: 20 })).toEqual({ ok: true, command: { cmd: "tap", args: { x: 10, y: 20, scale: 1 } } });
    expect(v("tap", { x: 10, y: 20, scale: 0.5 }).ok).toBe(true);
    for (const body of [{ x: -1, y: 0 }, { x: 1.5, y: 0 }, { x: "1", y: 0 }, { x: 10001, y: 0 }, { x: 1 }, { x: 1, y: 1, z: 1 }, { x: 1, y: 1, scale: 0 }]) {
      expect(v("tap", body).ok, JSON.stringify(body)).toBe(false);
    }
    expect(v("tap", null).ok).toBe(false);
  });

  it("swipe duration is bounded", () => {
    const base = { x1: 1, y1: 2, x2: 3, y2: 4 };
    expect(v("swipe", base)).toEqual({ ok: true, command: { cmd: "swipe", args: { ...base, ms: 300, scale: 1 } } });
    expect(v("swipe", { ...base, ms: 50 }).ok).toBe(true);
    expect(v("swipe", { ...base, ms: 5000 }).ok).toBe(true);
    for (const ms of [49, 5001, 100.5, "300"]) expect(v("swipe", { ...base, ms }).ok).toBe(false);
  });

  it("type allows printable ASCII up to 500 characters without %", () => {
    expect(v("type", { text: "Hello, world! (test) $x `y`" }).ok).toBe(true);
    expect(v("type", { text: "x".repeat(500) }).ok).toBe(true);
    for (const text of ["", "x".repeat(501), "100%", "café", "a\nb", "tab\t", 5, null]) {
      expect(v("type", { text }).ok, JSON.stringify(text)).toBe(false);
    }
  });

  it("key must come from the fixed list", () => {
    for (const key of ["back", "home", "recents", "enter", "tab", "delete"]) expect(v("key", { key }).ok).toBe(true);
    for (const key of ["power", "KEYCODE_POWER", "26", ""]) expect(v("key", { key }).ok).toBe(false);
  });

  it("launch accepts only Gmail and Chrome", () => {
    expect(v("launch", { package: "com.google.android.gm" }).ok).toBe(true);
    expect(v("launch", { package: "com.android.chrome" }).ok).toBe(true);
    for (const p of ["com.android.settings", "com.android.chrome ", "com.android.chrome;reboot", 1]) {
      expect(v("launch", { package: p }).ok).toBe(false);
    }
  });
});

describe("parseBody", () => {
  it("accepts only JSON objects under the size limit", () => {
    expect(parseBody('{"a":1}')).toEqual({ a: 1 });
    expect(parseBody("[1,2]")).toBeNull();
    expect(parseBody("null")).toBeNull();
    expect(parseBody("nope")).toBeNull();
    expect(parseBody(JSON.stringify({ text: "x".repeat(5000) }))).toBeNull();
  });
});

describe("logging", () => {
  it("never includes typed text", () => {
    expect(summariseArgs({ cmd: "type", args: { text: "secret" } })).toEqual({ length: 6 });
    expect(summariseArgs({ cmd: "tap", args: { x: 1, y: 2, scale: 1 } })).toEqual({ x: 1, y: 2, scale: 1 });
  });

  it("validateText reports reasons", () => {
    expect(validateText("ok")).toBeNull();
    expect(validateText("50%")).toMatch(/%/);
  });
});
