// DeviceHub Durable Object: holds the phone's WebSocket (hibernation API),
// relays commands to it, enforces the kill switch, keeps an action log and the
// owner's private notes in SQLite storage.

import { DurableObject } from "cloudflare:workers";
import type { Env } from "./index";
import { json } from "./http";
import { evaluateCheck, type CheckRecord, type StepOutcome } from "./check";
import { DEFAULT_TIMEOUT_S, DOWNLOAD_COMMANDS, UPLOAD_COMMANDS, summariseArgs, type Command, type CommandName } from "./validate";

const LOG_KEEP = 1000;
// The phone enforces each command's `timeout` itself and replies "timeout". The relay waits this much
// longer before giving up on a reply that never comes (a lost message, a wedged client).
const REPLY_GRACE_MS = 10_000;
const MAX_TIMER_MS = 2 ** 31 - 1; // setTimeout's limit (about 24.8 days); longer waits are capped here
// Files cross the phone's socket in chunks of this size, at most STREAM_WINDOW unacknowledged (as the phone client).
const STREAM_CHUNK = 256 * 1024;
const STREAM_WINDOW = 8;
// The last request's outcome is kept whole up to this size (SQLite holds it in the meta table).
const REQUEST_KEEP_BYTES = 256 * 1024;
// /check reads this many of the crash buffer's last lines.
const CHECK_LOG_LINES = 2000;

// Device error code -> HTTP status. Unknown codes become 502.
const DEVICE_ERROR_STATUS: Record<string, number> = {
  invalid_args: 400,
  package_mismatch: 400, // /install: the APK is another package than the one named
  not_debuggable: 403, // /run-as: only a debuggable app's own files can be read or written
  not_found: 404,
  foreground_mismatch: 409,
  no_text_field: 409, // the Android app types into a focused field only
  gesture_cancelled: 409, // Android cancelled the app's gesture (a real touch, the screen changed)
  stop_refused: 409, // /force-stop: an app with an enabled accessibility service (Android would drop it), or the system's own
  launch_failed: 422,
  install_failed: 422, // Android refused the install: its own error (INSTALL_FAILED_...) is the detail
  unsupported: 501, // a command the connected client cannot run
  permission_missing: 503, // the app lacks a grant the owner gives on the phone
  shell_unavailable: 503, // the app's shell bridge: not paired, or wireless debugging is off or not reachable
  timeout: 504,
  stopped: 503,
};

interface Pending {
  resolve: (msg: DeviceReply) => void;
  timer: ReturnType<typeof setTimeout>;
  // A streaming command's side messages from the phone (start, chunk, ack).
  side?: (msg: Record<string, unknown>) => void;
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
  // The commands the client runs (the Android app announces them; the adb client runs them all).
  capabilities?: string[];
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

// The body cut into STREAM_CHUNK pieces, whatever sizes the network delivers it in.
async function* chunks(body: ReadableStream<Uint8Array> | null): AsyncGenerator<Uint8Array> {
  if (!body) return;
  const reader = body.getReader();
  let buf = new Uint8Array(0);
  for (;;) {
    const { done, value } = await reader.read();
    if (value) {
      const joined = new Uint8Array(buf.length + value.length);
      joined.set(buf);
      joined.set(value, buf.length);
      buf = joined;
      while (buf.length >= STREAM_CHUNK) {
        yield buf.slice(0, STREAM_CHUNK);
        buf = buf.slice(STREAM_CHUNK);
      }
    }
    if (done) break;
  }
  if (buf.length) yield buf;
}

// Headers the phone may set on a download: its own X- headers only.
function phoneHeaders(h: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (h && typeof h === "object") {
    for (const [k, v] of Object.entries(h)) if (/^X-[A-Za-z-]{1,40}$/.test(k)) out[k] = String(v).slice(0, 512);
  }
  return out;
}

export class DeviceHub extends DurableObject<Env> {
  private sql: SqlStorage;
  private pending = new Map<string, Pending>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`);
    this.sql.exec(`DROP TABLE IF EXISTS rate`); // rate limits were removed
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

  private notes(): string {
    const row = this.sql.exec(`SELECT value FROM meta WHERE key = 'notes'`).toArray()[0];
    return row ? String(row.value) : "";
  }

  // The last request to the owner: pending while the phone shows it, then its answer (and the queued
  // steps' results), so an agent whose own call was cut off can still find out how it went.
  private recordRequest(outcome: Record<string, unknown>): void {
    let text = JSON.stringify(outcome);
    // A queued screenshot's image can be megabytes: past REQUEST_KEEP_BYTES, the steps' results are left out.
    if (text.length > REQUEST_KEEP_BYTES && outcome.batch && typeof outcome.batch === "object") {
      const { results: _results, ...batch } = outcome.batch as Record<string, unknown>;
      text = JSON.stringify({ ...outcome, batch: { ...batch, results_omitted: true } });
    }
    this.sql.exec(`INSERT OR REPLACE INTO meta (key, value) VALUES ('request', ?)`, text);
  }

  private lastRequest(): Record<string, unknown> | null {
    const row = this.sql.exec(`SELECT value FROM meta WHERE key = 'request'`).toArray()[0];
    return row ? (JSON.parse(String(row.value)) as Record<string, unknown>) : null;
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
      case "/internal/push":
        // An upload: the command rides in a header, the file is the body, streamed to the phone.
        return this.runCommand(JSON.parse(request.headers.get("X-Command") ?? "{}") as Command, request.body);
      case "/internal/notes":
        return new Response(this.notes(), { headers: { "Content-Type": "text/markdown; charset=utf-8" } });
      case "/admin/notes": {
        if (request.method === "GET") return json(200, { ok: true, notes: this.notes() });
        // PUT: the owner's private notes for the agent, appended to /docs/PHONE_RULES.md. An empty body clears them.
        const text = await request.text();
        if (text.trim()) this.sql.exec(`INSERT OR REPLACE INTO meta (key, value) VALUES ('notes', ?)`, text);
        else this.sql.exec(`DELETE FROM meta WHERE key = 'notes'`);
        this.log("admin", "notes", { length: text.length }, true);
        return json(200, { ok: true, length: text.trim() ? text.length : 0 });
      }
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
          notes_length: this.notes().length,
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

  // Send a command to the phone. The promise settles with its final reply, or with device_timeout /
  // device_offline; `side` receives a streaming command's other messages meanwhile.
  private dispatch(ws: WebSocket, command: Command, side?: Pending["side"]): { id: string; reply: Promise<DeviceReply> } {
    const id = crypto.randomUUID();
    const timeoutS = Number(command.args.timeout ?? DEFAULT_TIMEOUT_S);
    const timeoutMs = Math.min(timeoutS * 1000 + REPLY_GRACE_MS, MAX_TIMER_MS);
    const reply = new Promise<DeviceReply>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ id, ok: false, error: "device_timeout" });
      }, timeoutMs);
      this.pending.set(id, { resolve, timer, side });
      try {
        ws.send(JSON.stringify({ id, cmd: command.cmd, args: command.args }));
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ id, ok: false, error: "device_offline", detail: String(e) });
      }
    });
    return { id, reply };
  }

  // A side message to the phone for a running command; a closed socket is already failing it.
  private sendSide(ws: WebSocket, id: string, msg: Record<string, unknown>): void {
    try {
      ws.send(JSON.stringify({ id, ...msg }));
    } catch {
      // closed
    }
  }

  private failure(reply: DeviceReply): [number, string, Record<string, unknown>] {
    const error = reply.error ?? "device_error";
    const status = error === "device_timeout" ? 504 : error === "device_offline" ? 503 : (DEVICE_ERROR_STATUS[error] ?? 502);
    return [status, error, reply.detail ? { detail: reply.detail } : {}];
  }

  private async runCommand(command: Command, body: ReadableStream<Uint8Array> | null = null): Promise<Response> {
    const started = Date.now();
    const detail = summariseArgs(command);
    // Fields the command does not take are dropped; say which, so a typo does not pass unnoticed.
    const ignored = command.ignored?.length ? { ignored_fields: command.ignored } : {};
    const done = (status: number, error?: string, extra: Record<string, unknown> = {}) => {
      this.log("agent", command.cmd, detail, status < 400, error, Date.now() - started);
      return json(status, error ? { ok: false, error, ...extra, ...ignored } : { ok: true, ...extra, ...ignored });
    };

    // The last request's outcome, from the relay's own record: it answers while a request still holds the phone.
    if (command.cmd === "request_status") return json(200, { ok: true, request: this.lastRequest(), ...ignored });

    if (this.isDisabled()) return done(503, "disabled");
    const ws = this.openSockets()[0];
    if (!ws) return done(503, "device_offline");

    if (DOWNLOAD_COMMANDS.includes(command.cmd)) return this.download(ws, command, done);
    if (command.cmd === "check") return this.check(ws, command, done);
    const asking = command.cmd === "request";
    if (asking) this.recordRequest({ answer: "pending", asked_at: new Date(started).toISOString() });
    const reply = await (UPLOAD_COMMANDS.includes(command.cmd) ? this.upload(ws, command, body) : this.dispatch(ws, command)).reply;

    if (asking) {
      const asked_at = new Date(started).toISOString();
      if (reply.ok) this.recordRequest({ ...(reply.result ?? {}), asked_at, answered_at: new Date().toISOString() });
      else this.recordRequest({ answer: "failed", asked_at, error: reply.error ?? "device_error", ...(reply.detail ? { detail: reply.detail } : {}) });
    }
    if (!reply.ok) return done(...this.failure(reply));

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
          ...(command.ignored?.length ? { "X-Ignored-Fields": command.ignored.join(",") } : {}),
        },
      });
    }
    return done(200, undefined, result);
  }

  // GET /check: the phone commands one after another, each with the check's own timeout and
  // if_foreground, then the verdict against the last check's record (check.ts), which is kept here.
  private async check(ws: WebSocket, command: Command, done: (s: number, e?: string, x?: Record<string, unknown>) => Response): Promise<Response> {
    const args = command.args as { package: string; services?: string[]; accept_gone?: boolean; timeout?: number; if_foreground?: string[] };
    const common = { timeout: args.timeout ?? DEFAULT_TIMEOUT_S, ...(args.if_foreground ? { if_foreground: args.if_foreground } : {}) };
    const step = async (cmd: CommandName, stepArgs: Record<string, unknown>): Promise<StepOutcome> => {
      const r = await this.dispatch(ws, { cmd, args: { ...stepArgs, ...common } }).reply;
      return r.ok ? { ok: true, result: r.result ?? {} } : { ok: false, error: r.error ?? "device_error", ...(r.detail ? { detail: r.detail } : {}) };
    };
    const pkg = await step("package", { name: args.package });
    const state = await step("state", {});
    const accessibility = await step("accessibility", {});
    const installed = pkg.ok ? pkg.result : null;
    const since = typeof installed?.last_update_time === "string" ? { since: installed.last_update_time } : {};
    const crashLog = installed ? await step("logcat", { buffer: "crash", lines: CHECK_LOG_LINES, ...since }) : null;
    const files = installed?.debuggable === true ? await step("run_as_files", { package: args.package, path: "files" }) : null;

    const key = `check:${args.package}`;
    const row = this.sql.exec(`SELECT value FROM meta WHERE key = ?`, key).toArray()[0];
    const last = row ? (JSON.parse(String(row.value)) as CheckRecord) : null;
    const { report, record } = evaluateCheck(args, { package: pkg, state, accessibility, crashLog, files }, last);
    this.sql.exec(`INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)`, key, JSON.stringify(record));
    return done(200, undefined, report);
  }

  // A file from the phone (a pull, a recording), streamed to the agent as it arrives. The response
  // starts at the phone's start message; each chunk is acked once the agent's side has taken it, so
  // the phone never runs more than STREAM_WINDOW chunks ahead of a slow reader.
  private download(ws: WebSocket, command: Command, done: (s: number, e?: string, x?: Record<string, unknown>) => Response): Promise<Response> {
    const started = Date.now();
    let respond!: (r: Response) => void;
    const response = new Promise<Response>((resolve) => (respond = resolve));
    let writer: WritableStreamDefaultWriter<Uint8Array> | null = null;
    let bytes = 0;
    const { id, reply } = this.dispatch(ws, command, (msg) => {
      if (msg.type === "start" && !writer) {
        const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
        writer = writable.getWriter();
        respond(
          new Response(readable, {
            status: 200,
            headers: {
              "Content-Type": String(msg.mime ?? "application/octet-stream"),
              "Cache-Control": "no-store",
              "X-File-Size": String(msg.size ?? ""),
              ...phoneHeaders(msg.headers),
              ...(command.ignored?.length ? { "X-Ignored-Fields": command.ignored.join(",") } : {}),
            },
          }),
        );
      } else if (msg.type === "chunk" && writer && typeof msg.data === "string") {
        const data = base64ToBytes(msg.data);
        bytes += data.length;
        writer.write(data).then(
          () => this.sendSide(ws, id, { type: "ack", seq: msg.seq }),
          () => this.sendSide(ws, id, { type: "cancel" }), // the agent went away
        );
      }
    });
    void reply.then((r) => {
      // The relay gave up (the command sat queued behind a slow one, say): stop the phone sending.
      if (!r.ok && r.error === "device_timeout") this.sendSide(ws, id, { type: "cancel" });
      if (!writer) {
        // Nothing was streamed: an error before the start (or a reply with no file at all).
        respond(r.ok ? done(502, "device_error", { detail: "the phone sent no file" }) : done(...this.failure(r)));
        return;
      }
      this.log("agent", command.cmd, { ...summariseArgs(command), bytes }, r.ok, r.ok ? undefined : r.error, Date.now() - started);
      // A failure after the start cuts the body short, so the agent sees an incomplete transfer, not a whole file.
      if (r.ok) void writer.close().catch(() => {});
      else void writer.abort(r.error ?? "device_error").catch(() => {});
    });
    return response;
  }

  // A file from the agent streamed to the phone in chunks, at most STREAM_WINDOW ahead of its acks,
  // then an end message. Stops early if the phone replies first (an error) or the body breaks.
  private upload(ws: WebSocket, command: Command, body: ReadableStream<Uint8Array> | null): { id: string; reply: Promise<DeviceReply> } {
    let acked = -1;
    let finished = false;
    let ended = false;
    let wake: (() => void) | null = null;
    const sent = this.dispatch(ws, command, (msg) => {
      if (msg.type === "ack" && typeof msg.seq === "number") {
        acked = Math.max(acked, msg.seq);
        wake?.();
      }
    });
    void sent.reply.then(() => {
      finished = true;
      wake?.();
      // A reply before the end message (an error, or the relay's own deadline): the phone may still be
      // waiting on chunks, or have the upload queued, so tell it to stop.
      if (!ended) this.sendSide(ws, sent.id, { type: "cancel" });
    });
    const pump = async () => {
      let seq = 0;
      let total = 0;
      try {
        for await (const piece of chunks(body)) {
          while (seq - acked > STREAM_WINDOW && !finished) await new Promise<void>((r) => (wake = r));
          if (finished) return;
          ws.send(JSON.stringify({ id: sent.id, type: "chunk", seq, data: bytesToBase64(piece) }));
          seq++;
          total += piece.length;
        }
        if (!finished) {
          ws.send(JSON.stringify({ id: sent.id, type: "end", bytes: total }));
          ended = true;
        }
      } catch {
        this.sendSide(ws, sent.id, { type: "cancel" });
      }
    };
    void pump();
    return sent;
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
        ...(Array.isArray(msg.capabilities) ? { capabilities: msg.capabilities.slice(0, 100).map((c) => String(c).slice(0, 40)) } : {}),
      };
      ws.serializeAttachment(info);
      this.log("device", "hello", { client: info.client, version: info.version }, true);
      return;
    }
    if (typeof msg.id === "string") {
      const p = this.pending.get(msg.id);
      if (!p) return; // late reply after timeout
      if (typeof msg.type === "string") {
        p.side?.(msg); // a running command's start, chunk or ack
        return;
      }
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
