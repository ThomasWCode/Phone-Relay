// The agent docs, bundled at deploy time (wrangler "Text" rule for *.md) and
// served to the agent, so a session only needs to be told the relay's URL.

import agentInstructions from "../../docs/AGENT_INSTRUCTIONS.md";
import capabilities from "../../docs/CAPABILITIES.md";

// Path -> document. "/" is the entry point an agent is pointed at.
export const DOC_ROUTES: Record<string, string> = {
  "/": agentInstructions,
  "/docs/AGENT_INSTRUCTIONS.md": agentInstructions,
  "/docs/CAPABILITIES.md": capabilities,
};

// Fill in the placeholder with the host the agent actually called.
export function renderDoc(doc: string, host: string): string {
  return doc.replaceAll("<WORKER_HOST>", host);
}
