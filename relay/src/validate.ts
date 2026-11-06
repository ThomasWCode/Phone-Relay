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
// A command's deadline when the agent sets no `timeout` (seconds); typing, swipes, waits, recordings and
// batches get their own time on top. Moving a file, or stopping a recording and fetching it, gets five minutes.
export const DEFAULT_TIMEOUT_S = 30;
export const TRANSFER_TIMEOUT_S = 300;
const STATUSBAR_ACTIONS = ["notifications", "settings", "collapse"];
const WAIT_KINDS = ["change", "stable", "foreground"];
// Commands a batch may run (the JSON ones, plus sleep). Same list as the phone client.
export const BATCH_COMMANDS = [
  "state", "screenshot", "ui", "tap", "swipe", "type", "key", "launch", "open", "intent",
  "notifications", "statusbar", "rotation", "wait", "files", "record_start", "sleep",
];

export type CommandName =
  | "state" | "screenshot" | "ui" | "tap" | "swipe" | "type" | "key" | "launch"
  | "open" | "intent" | "notifications" | "statusbar" | "rotation" | "wait" | "batch"
  | "files" | "pull" | "push" | "record" | "record_start" | "record_stop";
// Commands whose reply is a file streamed from the phone, and the one that streams a file to it.
export const DOWNLOAD_COMMANDS: readonly CommandName[] = ["pull", "record", "record_stop"];
export const UPLOAD_COMMAND: CommandName = "push";

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
  // The request body is a file, not JSON: the command's fields come from the query string.
  raw?: boolean;
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
  "/open": { method: "POST", cmd: "open" },
  "/intent": { method: "POST", cmd: "intent" },
  "/notifications": { method: "GET", cmd: "notifications" },
  "/statusbar": { method: "POST", cmd: "statusbar" },
  "/rotation": { method: "POST", cmd: "rotation" },
  "/wait": { method: "POST", cmd: "wait" },
  "/batch": { method: "POST", cmd: "batch" },
  "/files": { method: "GET", cmd: "files" },
  "/files/pull": { method: "GET", cmd: "pull" },
  "/files/push": { method: "POST", cmd: "push", raw: true },
  "/record": { method: "GET", cmd: "record" },
  "/record/start": { method: "POST", cmd: "record_start" },
  "/record/stop": { method: "POST", cmd: "record_stop" },
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
  open: ["url", "package"],
  intent: ["action", "data", "type", "component", "package", "categories", "flags", "extras", "broadcast"],
  notifications: [],
  statusbar: ["action"],
  rotation: ["rotation"],
  wait: ["for", "seconds", "interval_ms", "threshold", "package"],
  batch: ["steps", "stop_on_error"],
  files: ["path"],
  pull: ["path"],
  push: ["path"],
  record: ["seconds", "size", "bit_rate"],
  record_start: ["seconds", "size", "bit_rate"],
  record_stop: [],
};
// Query-string fields read as numbers.
const NUMERIC_QUERY_FIELDS = ["scale", "timeout", "seconds", "bit_rate"];
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
    if (NUMERIC_QUERY_FIELDS.includes(k)) out[k] = v.trim() === "" ? NaN : Number(v);
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
  if (cmd === "wait") return DEFAULT_TIMEOUT_S + (isNumber(args.seconds) ? args.seconds : 10);
  if (cmd === "record") return 2 * DEFAULT_TIMEOUT_S + 2 * (isNumber(args.seconds) ? args.seconds : 10);
  if (cmd === "pull" || cmd === "push" || cmd === "record_stop") return TRANSFER_TIMEOUT_S;
  if (cmd === "batch" && Array.isArray(args.steps)) {
    let total = DEFAULT_TIMEOUT_S;
    for (const step of args.steps as { cmd: string; args: Fields }[]) {
      total += step.cmd === "sleep" ? Number(step.args.ms ?? 0) / 1000 : Number(step.args.timeout);
    }
    return total;
  }
  return DEFAULT_TIMEOUT_S;
}

function optionalString(src: Fields, name: string): string | null {
  const v = src[name];
  return v === undefined || (typeof v === "string" && v.length > 0) ? null : `${name} must be a non-empty string`;
}

function absolutePath(src: Fields): Fields | string {
  if (typeof src.path !== "string" || !src.path.startsWith("/")) return "path must be absolute, e.g. /sdcard/Download/file.pdf";
  return { path: src.path };
}

function recordArgs(src: Fields): Fields | string {
  const out: Fields = {};
  if (src.seconds !== undefined) {
    if (!isPositive(src.seconds)) return "seconds must be a number above 0";
    out.seconds = src.seconds;
  }
  if (src.size !== undefined) {
    if (typeof src.size !== "string" || !/^\d{2,5}x\d{2,5}$/.test(src.size)) return "size must look like 720x1560";
    out.size = src.size;
  }
  if (src.bit_rate !== undefined) {
    if (!Number.isInteger(src.bit_rate) || (src.bit_rate as number) <= 0) return "bit_rate must be a whole number of bits per second";
    out.bit_rate = src.bit_rate;
  }
  return out;
}

// A batch's steps, each validated as its own command; their unknown fields are reported as steps[i].field.
function batchArgs(src: Fields, ignored: string[]): Fields | string {
  if (!Array.isArray(src.steps) || src.steps.length === 0) return "steps must be a non-empty list";
  const steps: Fields[] = [];
  for (const [i, raw] of (src.steps as unknown[]).entries()) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return `step ${i} must be an object`;
    const step = raw as Fields;
    if (typeof step.cmd !== "string" || !BATCH_COMMANDS.includes(step.cmd)) {
      return `step ${i}: cmd must be one of ${BATCH_COMMANDS.join(", ")}`;
    }
    const stepSrc = step.args ?? {};
    if (stepSrc === null || typeof stepSrc !== "object" || Array.isArray(stepSrc)) return `step ${i}: args must be an object`;
    for (const k of Object.keys(step)) if (k !== "cmd" && k !== "args") ignored.push(`steps[${i}].${k}`);
    if (step.cmd === "sleep") {
      const ms = (stepSrc as Fields).ms;
      if (!isNumber(ms) || ms < 0) return `step ${i}: sleep needs ms, a number 0 or above`;
      steps.push({ cmd: "sleep", args: { ms } });
      continue;
    }
    const cmd = step.cmd as CommandName;
    const common = commonArgs(stepSrc as Fields);
    if (typeof common === "string") return `step ${i}: ${common}`;
    const args = commandArgs(cmd, stepSrc as Fields, ignored);
    if (typeof args === "string") return `step ${i}: ${args}`;
    for (const k of Object.keys(stepSrc)) {
      if (!FIELDS[cmd].includes(k) && !COMMON_FIELDS.includes(k)) ignored.push(`steps[${i}].args.${k}`);
    }
    steps.push({ cmd, args: { ...args, ...common, timeout: common.timeout ?? defaultTimeout(cmd, args) } });
  }
  if (src.stop_on_error !== undefined && typeof src.stop_on_error !== "boolean") return "stop_on_error must be true or false";
  return src.stop_on_error === undefined ? { steps } : { steps, stop_on_error: src.stop_on_error };
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

function commandArgs(cmd: CommandName, src: Fields, ignored: string[]): Fields | string {
  switch (cmd) {
    case "state":
    case "ui":
    case "notifications":
    case "record_stop":
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

    case "open": {
      if (typeof src.url !== "string" || src.url.length === 0) return "url must be a non-empty string";
      if (src.package !== undefined && (typeof src.package !== "string" || !PACKAGE_RE.test(src.package))) {
        return "package must be an Android package name";
      }
      return src.package === undefined ? { url: src.url } : { url: src.url, package: src.package };
    }

    case "intent": {
      const out: Fields = {};
      for (const name of ["action", "data", "type", "component"]) {
        const err = optionalString(src, name);
        if (err) return err;
        if (src[name] !== undefined) out[name] = src[name];
      }
      if (src.package !== undefined) {
        if (typeof src.package !== "string" || !PACKAGE_RE.test(src.package)) return "package must be an Android package name";
        out.package = src.package;
      }
      if (!["action", "data", "component", "package"].some((k) => out[k] !== undefined)) {
        return "give at least one of action, data, component or package";
      }
      if (src.categories !== undefined) {
        if (!Array.isArray(src.categories) || !src.categories.every((c) => typeof c === "string" && c.length > 0)) {
          return "categories must be a list of strings";
        }
        out.categories = src.categories;
      }
      if (src.flags !== undefined) {
        if (!Number.isInteger(src.flags) || (src.flags as number) < 0) return "flags must be a whole number";
        out.flags = src.flags;
      }
      if (src.extras !== undefined) {
        const e = src.extras;
        if (e === null || typeof e !== "object" || Array.isArray(e)) return "extras must be an object";
        for (const v of Object.values(e)) {
          if (v !== null && !["string", "number", "boolean"].includes(typeof v)) {
            return "extras values must be strings, numbers, booleans or null";
          }
        }
        out.extras = e;
      }
      if (src.broadcast !== undefined) {
        if (typeof src.broadcast !== "boolean") return "broadcast must be true or false";
        out.broadcast = src.broadcast;
      }
      return out;
    }

    case "statusbar": {
      if (typeof src.action !== "string" || !STATUSBAR_ACTIONS.includes(src.action)) {
        return `action must be one of ${STATUSBAR_ACTIONS.join(", ")}`;
      }
      return { action: src.action };
    }

    case "rotation": {
      if (src.rotation !== "auto" && ![0, 1, 2, 3].includes(src.rotation as number)) {
        return 'rotation must be 0, 1, 2 or 3 (quarter turns) or "auto"';
      }
      return { rotation: src.rotation };
    }

    case "wait": {
      if (typeof src.for !== "string" || !WAIT_KINDS.includes(src.for)) return `for must be one of ${WAIT_KINDS.join(", ")}`;
      const out: Fields = { for: src.for };
      if (src.seconds !== undefined) {
        if (!isNumber(src.seconds) || src.seconds < 0) return "seconds must be a number 0 or above";
        out.seconds = src.seconds;
      }
      if (src.interval_ms !== undefined) {
        if (!isNumber(src.interval_ms) || src.interval_ms < 0) return "interval_ms must be a number 0 or above";
        out.interval_ms = src.interval_ms;
      }
      if (src.threshold !== undefined) {
        if (!isNumber(src.threshold) || src.threshold < 0) return "threshold must be a number 0 or above";
        out.threshold = src.threshold;
      }
      if (src.package !== undefined) {
        if (typeof src.package !== "string" || !PACKAGE_RE.test(src.package)) return "package must be an Android package name";
        out.package = src.package;
      }
      return out;
    }

    case "batch":
      return batchArgs(src, ignored);

    case "files":
    case "pull":
    case "push":
      return absolutePath(src);

    case "record":
    case "record_start":
      return recordArgs(src);
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
  const ignored = Object.keys(src).filter((k) => !FIELDS[cmd].includes(k) && !COMMON_FIELDS.includes(k));
  const args = commandArgs(cmd, src, ignored);
  if (typeof args === "string") return fail(args);
  // Always send the deadline, so the phone and the relay use the same one.
  const timeout = common.timeout ?? defaultTimeout(cmd, args);
  const command: Command = { cmd, args: { ...args, ...common, timeout } };
  if (ignored.length) command.ignored = ignored;
  return { ok: true, command };
}

// Summary of a command for the action log. Typed text is never logged, only its length; nor are a
// batch's typed steps or an intent's extra values (a share intent's text), only their names.
export function summariseArgs(command: Command): Record<string, unknown> {
  return summarise(command.cmd, command.args);
}

function summarise(cmd: string, args: Fields): Fields {
  if (cmd === "type") return { length: String(args.text ?? "").length };
  if (cmd === "batch" && Array.isArray(args.steps)) {
    return { ...args, steps: (args.steps as Fields[]).map((s) => ({ cmd: s.cmd, args: summarise(String(s.cmd), (s.args ?? {}) as Fields) })) };
  }
  if (cmd === "intent" && args.extras && typeof args.extras === "object") return { ...args, extras: Object.keys(args.extras) };
  return { ...args };
}
