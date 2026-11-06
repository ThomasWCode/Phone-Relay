// Request validation for the agent-facing API.
// Everything the phone receives passes through here first; the phone client
// validates again (defence in depth).

export const ALLOWED_PACKAGES = ["com.google.android.gm", "com.android.chrome"] as const;
export const ALLOWED_KEYS = ["back", "home", "recents", "enter", "tab", "delete"] as const;

export const MAX_COORD = 10000;
export const MIN_SCALE = 0.1;
export const MAX_SCALE = 1;
export const DEFAULT_SCREENSHOT_SCALE = 0.5;
export const MIN_SWIPE_MS = 50;
export const MAX_SWIPE_MS = 5000;
export const DEFAULT_SWIPE_MS = 300;
export const MAX_TEXT_LENGTH = 500;
export const MAX_BODY_BYTES = 4096;

export type CommandName = "state" | "screenshot" | "tap" | "swipe" | "type" | "key" | "launch";

export interface Command {
  cmd: CommandName;
  args: Record<string, string | number>;
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
  "/tap": { method: "POST", cmd: "tap" },
  "/swipe": { method: "POST", cmd: "swipe" },
  "/type": { method: "POST", cmd: "type" },
  "/key": { method: "POST", cmd: "key" },
  "/launch": { method: "POST", cmd: "launch" },
};

const TEXT_RE = /^[\x20-\x7e]+$/;

function fail(error: string): ValidationResult {
  return { ok: false, error };
}

function isCoord(v: unknown): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= MAX_COORD;
}

function isScale(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= MIN_SCALE && v <= MAX_SCALE;
}

function checkKeys(body: Record<string, unknown>, allowed: string[]): string | null {
  for (const k of Object.keys(body)) {
    if (!allowed.includes(k)) return `unknown field: ${k}`;
  }
  return null;
}

// Parse a JSON object body. Returns null for anything that is not a plain object.
export function parseBody(raw: string): Record<string, unknown> | null {
  if (raw.length > MAX_BODY_BYTES) return null;
  try {
    const v: unknown = JSON.parse(raw);
    if (v === null || typeof v !== "object" || Array.isArray(v)) return null;
    return v as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function validateText(text: unknown): string | null {
  if (typeof text !== "string" || text.length === 0) return "text must be a non-empty string";
  if (text.length > MAX_TEXT_LENGTH) return `text longer than ${MAX_TEXT_LENGTH} characters`;
  if (!TEXT_RE.test(text)) return "text must be printable ASCII";
  if (text.includes("%")) return "text must not contain %";
  return null;
}

export function validateCommand(
  cmd: CommandName,
  query: URLSearchParams,
  body: Record<string, unknown> | null,
): ValidationResult {
  switch (cmd) {
    case "state":
      return { ok: true, command: { cmd, args: {} } };

    case "screenshot": {
      const raw = query.get("scale");
      const scale = raw === null ? DEFAULT_SCREENSHOT_SCALE : Number(raw);
      if (!isScale(scale)) return fail(`scale must be between ${MIN_SCALE} and ${MAX_SCALE}`);
      return { ok: true, command: { cmd, args: { scale } } };
    }

    case "tap": {
      if (!body) return fail("JSON object body required");
      const bad = checkKeys(body, ["x", "y", "scale"]);
      if (bad) return fail(bad);
      if (!isCoord(body.x) || !isCoord(body.y)) return fail(`x and y must be integers 0-${MAX_COORD}`);
      const scale = body.scale ?? 1;
      if (!isScale(scale)) return fail(`scale must be between ${MIN_SCALE} and ${MAX_SCALE}`);
      return { ok: true, command: { cmd, args: { x: body.x, y: body.y, scale } } };
    }

    case "swipe": {
      if (!body) return fail("JSON object body required");
      const bad = checkKeys(body, ["x1", "y1", "x2", "y2", "ms", "scale"]);
      if (bad) return fail(bad);
      const { x1, y1, x2, y2 } = body;
      if (!isCoord(x1) || !isCoord(y1) || !isCoord(x2) || !isCoord(y2)) {
        return fail(`x1, y1, x2, y2 must be integers 0-${MAX_COORD}`);
      }
      const ms = body.ms ?? DEFAULT_SWIPE_MS;
      if (typeof ms !== "number" || !Number.isInteger(ms) || ms < MIN_SWIPE_MS || ms > MAX_SWIPE_MS) {
        return fail(`ms must be an integer ${MIN_SWIPE_MS}-${MAX_SWIPE_MS}`);
      }
      const scale = body.scale ?? 1;
      if (!isScale(scale)) return fail(`scale must be between ${MIN_SCALE} and ${MAX_SCALE}`);
      return { ok: true, command: { cmd, args: { x1, y1, x2, y2, ms, scale } } };
    }

    case "type": {
      if (!body) return fail("JSON object body required");
      const bad = checkKeys(body, ["text"]);
      if (bad) return fail(bad);
      const err = validateText(body.text);
      if (err) return fail(err);
      return { ok: true, command: { cmd, args: { text: body.text as string } } };
    }

    case "key": {
      if (!body) return fail("JSON object body required");
      const bad = checkKeys(body, ["key"]);
      if (bad) return fail(bad);
      if (typeof body.key !== "string" || !(ALLOWED_KEYS as readonly string[]).includes(body.key)) {
        return fail(`key must be one of: ${ALLOWED_KEYS.join(", ")}`);
      }
      return { ok: true, command: { cmd, args: { key: body.key } } };
    }

    case "launch": {
      if (!body) return fail("JSON object body required");
      const bad = checkKeys(body, ["package"]);
      if (bad) return fail(bad);
      if (typeof body.package !== "string" || !(ALLOWED_PACKAGES as readonly string[]).includes(body.package)) {
        return fail(`package must be one of: ${ALLOWED_PACKAGES.join(", ")}`);
      }
      return { ok: true, command: { cmd, args: { package: body.package } } };
    }
  }
}

// Summary of a command for the action log. Typed text is never logged, only its length.
export function summariseArgs(command: Command): Record<string, string | number> {
  if (command.cmd === "type") return { length: String(command.args.text ?? "").length };
  return { ...command.args };
}
