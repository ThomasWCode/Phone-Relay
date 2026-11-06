// DeviceHub Durable Object: holds the phone's WebSocket (hibernation API),
// relays commands to it, enforces rate limits and the kill switch, and keeps
// an action log in SQLite storage.

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./index";
import { json } from "./http";
import { summariseArgs, type Command } from "./validate";

// Per-minute limits; override with the RATE_ACTIONS_PER_MIN / RATE_SCREENSHOTS_PER_MIN vars.
// They only catch runaway loops: an agent working normally stays far below them.
export const DEFAULT_RATE_LIMITS = { action: 300, screenshot: 120 } as const;
const RATE_WINDOW_MS = 60_000;
const LOG_KEEP = 1000;
// The phone gives each adb call up to 20 s (uiautomator and typing can be slow).
const DEFAULT_TIMEOUT_MS = 30_000;
const TYPE_TIMEOUT_MS = 60_000; // UI check plus up to 500 characters of input

// Device error code -> HTTP status. Unknown codes become 502.
const DEVICE_ERROR_STATUS: Record<string, number> = {
  invalid_args: 400,
  foreground_not_allowed: 409,
  password_field: 409,
  screen_off: 409,
  stopped: 503,
};

interface Pending {
  resolve: (msg: DeviceReply) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface DeviceReply {
  id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
  detail?: string;
}

interface DeviceInfo {
  connected_at: number;
  client?: string;
  version?: string;
  allowlist?: string[];
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export class DeviceHub extends DurableObject<Env> {
  private sql: SqlStorage;
  private pending = new Map<string, Pending>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS rate (kind TEXT NOT NULL, ts INTEGER NOT NULL)`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS rate_kind_ts ON rate (kind, ts)`);
    this.sql.exec(
      `CREATE TABLE IF NOT EXISTS log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT,
        ok INTEGER NOT NULL,
        error TEXT,
        ms INTEGER
      )`,
    );
  }

  // ---- storage helpers ----

  private isDisabled(): boolean {
    return this.sql.exec(`SELECT value FROM meta WHERE key = 'disabled'`).toArray().length > 0;
  }

  private log(actor: string, action: string, detail: unknown, ok: boolean, error?: string, ms?: number): void {
    this.sql.exec(
      `INSERT INTO log (ts, actor, action, detail, ok, error, ms) VALUES (?, ?, ?, ?, ?, ?, ?)`,
      Date.now(),
      actor,
      action,
      detail === undefined ? null : JSON.stringify(detail),
      ok ? 1 : 0,
      error ?? null,
      ms ?? null,
    );
    this.sql.exec(`DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - ?`, LOG_KEEP);
  }

  private rateLimits(): Record<keyof typeof DEFAULT_RATE_LIMITS, number> {
    const pick = (v: string | undefined, fallback: number) => {
      const n = Number(v);
      return Number.isInteger(n) && n > 0 ? n : fallback;
    };
    return {
      action: pick(this.env.RATE_ACTIONS_PER_MIN, DEFAULT_RATE_LIMITS.action),
      screenshot: pick(this.env.RATE_SCREENSHOTS_PER_MIN, DEFAULT_RATE_LIMITS.screenshot),
    };
  }

  // Sliding one-minute window, persisted so it survives hibernation.
  private takeRateToken(kind: keyof typeof DEFAULT_RATE_LIMITS): boolean {
    const now = Date.now();
    this.sql.exec(`DELETE FROM rate WHERE ts <= ?`, now - RATE_WINDOW_MS);
    const row = this.sql.exec(`SELECT COUNT(*) AS n FROM rate WHERE kind = ?`, kind).one();
    if (Number(row.n) >= this.rateLimits()[kind]) return false;
    this.sql.exec(`INSERT INTO rate (kind, ts) VALUES (?, ?)`, kind, now);
    return true;
  }

  private rateCounts(): Record<string, number> {
    const since = Date.now() - RATE_WINDOW_MS;
    const out: Record<string, number> = { action: 0, screenshot: 0 };
    for (const row of this.sql.exec(`SELECT kind, COUNT(*) AS n FROM rate WHERE ts > ? GROUP BY kind`, since)) {
      out[String(row.kind)] = Number(row.n);
    }
    return out;
  }

  // ---- socket helpers ----

  private openSockets(): WebSocket[] {
    return this.ctx.getWebSockets().filter((ws) => ws.readyState === WebSocket.OPEN);
  }

  private closeAll(code: number, reason: string): void {
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.close(code, reason);
      } catch {
        // already closed
      }
    }
    this.failPending("device_offline", reason);
  }

  private failPending(error: string, detail: string): void {
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.resolve({ id, ok: false, error, detail });
    }
    this.pending.clear();
  }

  // ---- HTTP entry (only reachable through the Worker) ----

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    switch (url.pathname) {
      case "/device":
        return this.acceptDevice();
      case "/internal/command":
        return this.runCommand((await request.json()) as Command);
      case "/admin/disable":
        this.sql.exec(`INSERT OR REPLACE INTO meta (key, value) VALUES ('disabled', '1')`);
        this.closeAll(4001, "disabled");
        this.log("admin", "disable", undefined, true);
        return json(200, { ok: true, disabled: true });
      case "/admin/enable":
        this.sql.exec(`DELETE FROM meta WHERE key = 'disabled'`);
        this.log("admin", "enable", undefined, true);
        return json(200, { ok: true, disabled: false });
      case "/admin/status": {
        const ws = this.openSockets()[0];
        return json(200, {
          ok: true,
          disabled: this.isDisabled(),
          device_online: ws !== undefined,
          device: ws ? (ws.deserializeAttachment() as DeviceInfo | null) : null,
          pending: this.pending.size,
          rate_last_minute: this.rateCounts(),
          rate_limits: this.rateLimits(),
        });
      }
      case "/admin/log": {
        const n = Math.min(Math.max(parseInt(url.searchParams.get("n") ?? "50", 10) || 50, 1), 500);
        const rows = this.sql
          .exec(`SELECT id, ts, actor, action, detail, ok, error, ms FROM log ORDER BY id DESC LIMIT ?`, n)
          .toArray()
          .map((r) => ({
            ...r,
            time: new Date(Number(r.ts)).toISOString(),
            detail: r.detail ? JSON.parse(String(r.detail)) : null,
            ok: r.ok === 1,
          }));
        return json(200, { ok: true, entries: rows });
      }
      default:
        return json(404, { ok: false, error: "not_found" });
    }
  }

  private acceptDevice(): Response {
    if (this.isDisabled()) {
      this.log("device", "connect", undefined, false, "disabled");
      return json(403, { ok: false, error: "disabled" });
    }
    // Only one phone at a time: a new connection replaces the old one.
    this.closeAll(4000, "replaced");

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ connected_at: Date.now() } satisfies DeviceInfo);
    this.log("device", "connect", undefined, true);
    return new Response(null, { status: 101, webSocket: client });
  }

  private async runCommand(command: Command): Promise<Response> {
    const started = Date.now();
    const detail = summariseArgs(command);
    const done = (status: number, error?: string, extra: Record<string, unknown> = {}, headers = {}) => {
      this.log("agent", command.cmd, detail, status < 400, error, Date.now() - started);
      return json(status, error ? { ok: false, error, ...extra } : { ok: true, ...extra }, headers);
    };

    if (this.isDisabled()) return done(503, "disabled");
    const ws = this.openSockets()[0];
    if (!ws) return done(503, "device_offline");

    const kind = command.cmd === "screenshot" ? "screenshot" : "action";
    if (!this.takeRateToken(kind)) return done(429, "rate_limited", { limit_per_minute: this.rateLimits()[kind] }, { "Retry-After": "60" });

    const id = crypto.randomUUID();
    const timeoutMs = command.cmd === "type" ? TYPE_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
    const reply = await new Promise<DeviceReply>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ id, ok: false, error: "device_timeout" });
      }, timeoutMs);
      this.pending.set(id, { resolve, timer });
      try {
        ws.send(JSON.stringify({ id, cmd: command.cmd, args: command.args }));
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ id, ok: false, error: "device_offline", detail: String(e) });
      }
    });

    if (!reply.ok) {
      const error = reply.error ?? "device_error";
      const status = error === "device_timeout" ? 504 : error === "device_offline" ? 503 : (DEVICE_ERROR_STATUS[error] ?? 502);
      return done(status, error, reply.detail ? { detail: reply.detail } : {});
    }

    const result = reply.result ?? {};
    if (command.cmd === "screenshot") {
      if (typeof result.data !== "string") return done(502, "device_error", { detail: "screenshot missing data" });
      const bytes = base64ToBytes(result.data);
      this.log("agent", command.cmd, { ...detail, bytes: bytes.length }, true, undefined, Date.now() - started);
      return new Response(bytes, {
        status: 200,
        headers: {
          "Content-Type": String(result.mime ?? "image/jpeg"),
          "Cache-Control": "no-store",
          "X-Image-Width": String(result.width),
          "X-Image-Height": String(result.height),
          "X-Screen-Width": String(result.real_width),
          "X-Screen-Height": String(result.real_height),
          "X-Scale": String(result.scale),
          "X-Foreground": String(result.foreground ?? ""),
        },
      });
    }
    return done(200, undefined, result);
  }

  // ---- hibernation API handlers ----

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (typeof message !== "string") return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(message);
    } catch {
      return;
    }
    if (msg.type === "hello") {
      const info: DeviceInfo = {
        connected_at: (ws.deserializeAttachment() as DeviceInfo | null)?.connected_at ?? Date.now(),
        client: String(msg.client ?? "").slice(0, 64),
        version: String(msg.version ?? "").slice(0, 32),
        allowlist: Array.isArray(msg.allowlist) ? msg.allowlist.slice(0, 20).map((p) => String(p).slice(0, 128)) : [],
      };
      ws.serializeAttachment(info);
      this.log("device", "hello", { client: info.client, version: info.version }, true);
      return;
    }
    if (typeof msg.id === "string") {
      const p = this.pending.get(msg.id);
      if (!p) return; // late reply after timeout
      clearTimeout(p.timer);
      this.pending.delete(msg.id);
      p.resolve(msg as unknown as DeviceReply);
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    try {
      ws.close(code, reason);
    } catch {
      // already closed
    }
    if (this.openSockets().length === 0) this.failPending("device_offline", "connection closed");
    this.log("device", "disconnect", { code, reason: reason.slice(0, 64) }, true);
  }

  async webSocketError(ws: WebSocket, error: unknown): Promise<void> {
    this.log("device", "error", { error: String(error).slice(0, 128) }, false);
  }
}
