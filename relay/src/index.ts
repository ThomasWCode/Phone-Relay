// Worker entry point: authenticates every request, validates agent commands and
// forwards them to the single DeviceHub Durable Object that holds the phone's
// WebSocket.

import { isAuthorised } from "./auth";
import { DOC_ROUTES, NOTES_DOC, renderDoc, withNotes } from "./docs";
import { json } from "./http";
import { AGENT_ROUTES, parseBody, validateCommand } from "./validate";
import type { DeviceHub } from "./device";

export { DeviceHub } from "./device";

export interface Env {
  DEVICE_HUB: DurableObjectNamespace<DeviceHub>;
  AGENT_TOKEN: string;
  DEVICE_TOKEN: string;
  ADMIN_TOKEN: string;
}

const ADMIN_ROUTES: Record<string, string[]> = {
  "/admin/disable": ["POST"],
  "/admin/enable": ["POST"],
  "/admin/status": ["GET"],
  "/admin/log": ["GET"],
  "/admin/notes": ["GET", "PUT"],
};

const unauthorised = () => json(401, { ok: false, error: "unauthorized" });

function hub(env: Env): DurableObjectStub<DeviceHub> {
  return env.DEVICE_HUB.get(env.DEVICE_HUB.idFromName("primary"));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // Phone connection (outbound WSS from the phone).
    if (path === "/device") {
      if (!(await isAuthorised(request, env.DEVICE_TOKEN))) return unauthorised();
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return json(426, { ok: false, error: "websocket_required" });
      }
      return hub(env).fetch(new Request("https://hub/device", { headers: request.headers }));
    }

    // Owner-only endpoints. The agent token never grants these.
    if (path in ADMIN_ROUTES) {
      if (!(await isAuthorised(request, env.ADMIN_TOKEN))) return unauthorised();
      if (!ADMIN_ROUTES[path].includes(request.method)) return json(405, { ok: false, error: "method_not_allowed" });
      const body = request.method === "PUT" ? await request.text() : undefined;
      return hub(env).fetch(new Request(`https://hub${path}${url.search}`, { method: request.method, body }));
    }

    // Everything else needs the agent token, including unknown paths.
    if (!(await isAuthorised(request, env.AGENT_TOKEN))) return unauthorised();

    // Agent docs: served by the Worker itself, not the phone.
    if (path in DOC_ROUTES) {
      if (request.method !== "GET") return json(405, { ok: false, error: "method_not_allowed" });
      let doc = DOC_ROUTES[path];
      // The phone rules end with the owner's private notes, kept in the Durable Object, not the repository.
      if (doc === NOTES_DOC) doc = withNotes(doc, await (await hub(env).fetch("https://hub/internal/notes")).text());
      return new Response(renderDoc(doc, url.host), {
        headers: { "Content-Type": "text/markdown; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    const route = AGENT_ROUTES[path];
    if (!route) return json(404, { ok: false, error: "not_found" });
    if (request.method !== route.method) return json(405, { ok: false, error: "method_not_allowed" });

    let body: Record<string, unknown> | null = null;
    if (route.method === "POST") {
      body = parseBody(await request.text());
      if (!body) return json(400, { ok: false, error: "invalid_args", detail: "JSON object body required" });
    }

    const result = validateCommand(route.cmd, url.searchParams, body);
    if (!result.ok) return json(400, { ok: false, error: "invalid_args", detail: result.error });

    return hub(env).fetch(
      new Request("https://hub/internal/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(result.command),
      }),
    );
  },
} satisfies ExportedHandler<Env>;
