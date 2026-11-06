// Request validation for the agent-facing API.
// It checks only that each value has a shape the phone can run safely (numbers
// are numbers, names cannot carry shell syntax). There is no app allowlist, no
// key deny list and no range limit. The phone client checks the same shapes
// again (defence in depth).

export const KEY_NAMES = ["back", "home", "recents", "enter", "tab", "delete"] as const;
// Any other key by its Android name (KEYCODE_PAGE_DOWN, KEYCODE_POWER, ...) or number. Same patterns as the phone client.
const RAW_KEY_RE = /^KEYCODE_[A-Z0-9_]{1,40}$/;
const KEY_NUMBER_RE = /^\d{1,4}$/;
const PACKAGE_RE = /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$/;

export function isKey(key: unknown): boolean {
  if (typeof key === "number") return Number.isInteger(key) && key >= 0 && key <= 9999;
  if (typeof key !== "string") return false;
  return (KEY_NAMES as readonly string[]).includes(key) || RAW_KEY_RE.test(key) || KEY_NUMBER_RE.test(key);
}

export const DEFAULT_SCREENSHOT_SCALE = 0.5;
export const DEFAULT_SWIPE_MS = 300;
// A command's deadline when the agent sets no `timeout` (seconds); typing and long swipes get their own time on top.
export const DEFAULT_TIMEOUT_S = 30;

export type CommandName = "state" | "screenshot" | "ui" | "tap" | "swipe" | "type" | "key" | "launch";

export interface Command {
  cmd: CommandName;
  args: Record<string, unknown>;
  // Request fields this command does not take; they are dropped and named in the reply.
  ignored?: string[];
}

export type ValidationResult = { ok: true; command: Command } | { ok: false; error: string };

interface Route {
  method: "GET" | "POST";
  cmd: CommandName;
}

// Path -> route. Anything not listed here is not an agent endpoint.
export const AGENT_ROUTES: Record<string, Route> = {
  "/state": { method: "GET", cmd: "state" },
  "/screenshot": { method: "GET", cmd: "screenshot" },
  "/ui": { method: "GET", cmd: "ui" },
  "/tap": { method: "POST", cmd: "tap" },
  "/swipe": { method: "POST", cmd: "swipe" },
  "/type": { method: "POST", cmd: "type" },
  "/key": { method: "POST", cmd: "key" },
  "/launch": { method: "POST", cmd: "launch" },
};

// Each command's own fields; every command also takes COMMON_FIELDS.
const FIELDS: Record<CommandName, string[]> = {
  state: [],
  screenshot: ["scale"],
  ui: [],
  tap: ["x", "y", "scale"],
  swipe: ["x1", "y1", "x2", "y2", "ms", "scale"],
  type: ["text"],
  key: ["key", "longpress"],
  launch: ["package"],
};
const COMMON_FIELDS = ["timeout", "if_foreground"];

type Fields = Record<string, unknown>;

function fail(error: string): ValidationResult {
  return { ok: false, error };
}

function isNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function isPositive(v: unknown): v is number {
  return isNumber(v) && v > 0;
}

// Parse a JSON object body. Returns null for anything that is not a plain object.
export function parseBody(raw: string): Record<string, unknown> | null {
  try {
    const v: unknown = JSON.parse(raw);
    if (v === null || typeof v !== "object" || Array.isArray(v)) return null;
    return v as Record<string, unknown>;
  } catch {
    return null;
  }
}

// A GET's query string as fields: numbers for scale and timeout, a list for if_foreground.
function queryFields(query: URLSearchParams): Fields {
  const out: Fields = {};
  for (const [k, v] of query) {
    if (k === "scale" || k === "timeout") out[k] = v.trim() === "" ? NaN : Number(v);
    else if (k === "if_foreground") out[k] = v.split(",");
    else out[k] = v;
  }
  return out;
}

export function validateText(text: unknown): string | null {
  if (typeof text !== "string" || text.length === 0) return "text must be a non-empty string";
  for (const c of text) {
    const code = c.codePointAt(0) ?? 0;
    if (code > 0x7e) return "non-ASCII text is not supported yet";
    if (code < 0x20 && !"\n\r\t".includes(c)) return "control characters other than newline and tab cannot be typed";
  }
  return null;
}

// Deadline (seconds) when the agent gives none. Mirrors the phone client's default_timeout.
export function defaultTimeout(cmd: CommandName, args: Fields): number {
  if (cmd === "type" && typeof args.text === "string") return DEFAULT_TIMEOUT_S + args.text.length / 10;
  if (cmd === "swipe" && isNumber(args.ms)) return DEFAULT_TIMEOUT_S + Math.max(args.ms, 0) / 1000;
  return DEFAULT_TIMEOUT_S;
}

// timeout (seconds above 0) and if_foreground (a package name or a list), taken by every command.
function commonArgs(src: Fields): Fields | string {
  const out: Fields = {};
  if (src.timeout !== undefined) {
    if (!isPositive(src.timeout)) return "timeout must be a number of seconds above 0";
    out.timeout = src.timeout;
  }
  if (src.if_foreground !== undefined) {
    const v = src.if_foreground;
    const list = typeof v === "string" ? [v] : v;
    if (!Array.isArray(list) || list.length === 0 || !list.every((p) => typeof p === "string" && PACKAGE_RE.test(p))) {
      return "if_foreground must be a package name or a list of them";
    }
    out.if_foreground = list;
  }
  return out;
}

function commandArgs(cmd: CommandName, src: Fields): Fields | string {
  switch (cmd) {
    case "state":
    case "ui":
      return {};

    case "screenshot": {
      const scale = src.scale ?? DEFAULT_SCREENSHOT_SCALE;
      if (!isPositive(scale)) return "scale must be a number above 0";
      return { scale };
    }

    case "tap": {
      if (!isNumber(src.x) || !isNumber(src.y)) return "x and y must be numbers";
      const scale = src.scale ?? 1;
      if (!isPositive(scale)) return "scale must be a number above 0";
      return { x: src.x, y: src.y, scale };
    }

    case "swipe": {
      const { x1, y1, x2, y2 } = src;
      if (!isNumber(x1) || !isNumber(y1) || !isNumber(x2) || !isNumber(y2)) return "x1, y1, x2, y2 must be numbers";
      const ms = src.ms ?? DEFAULT_SWIPE_MS;
      if (!isNumber(ms) || ms < 0) return "ms must be a number 0 or above";
      const scale = src.scale ?? 1;
      if (!isPositive(scale)) return "scale must be a number above 0";
      return { x1, y1, x2, y2, ms, scale };
    }

    case "type": {
      const err = validateText(src.text);
      if (err) return err;
      return { text: src.text };
    }

    case "key": {
      if (!isKey(src.key)) return `key must be one of ${KEY_NAMES.join(", ")}, a KEYCODE_* name or a key number`;
      if (src.longpress !== undefined && typeof src.longpress !== "boolean") return "longpress must be true or false";
      return src.longpress === undefined ? { key: src.key } : { key: src.key, longpress: src.longpress };
    }

    case "launch": {
      if (typeof src.package !== "string" || !PACKAGE_RE.test(src.package)) return "package must be an Android package name";
      return { package: src.package };
    }
  }
}

// A GET takes its fields from the query string, a POST from its JSON body.
export function validateCommand(
  cmd: CommandName,
  query: URLSearchParams,
  body: Record<string, unknown> | null,
): ValidationResult {
  const src = body ?? queryFields(query);
  const common = commonArgs(src);
  if (typeof common === "string") return fail(common);
  const args = commandArgs(cmd, src);
  if (typeof args === "string") return fail(args);
  // Always send the deadline, so the phone and the relay use the same one.
  const timeout = common.timeout ?? defaultTimeout(cmd, args);
  const command: Command = { cmd, args: { ...args, ...common, timeout } };
  const ignored = Object.keys(src).filter((k) => !FIELDS[cmd].includes(k) && !COMMON_FIELDS.includes(k));
  if (ignored.length) command.ignored = ignored;
  return { ok: true, command };
}

// Summary of a command for the action log. Typed text is never logged, only its length.
export function summariseArgs(command: Command): Record<string, unknown> {
  if (command.cmd === "type") return { length: String(command.args.text ?? "").length };
  return { ...command.args };
}
