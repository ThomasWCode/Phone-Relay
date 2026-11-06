// The agent docs, bundled at deploy time (wrangler "Text" rule for *.md) and
// served to the agent, so a session only needs to be told the relay's URL.

import agentInstructions from "../../docs/AGENT_INSTRUCTIONS.md";
import capabilities from "../../docs/CAPABILITIES.md";
import phoneRules from "../../docs/PHONE_RULES.md";

// Path -> document. "/" is the entry point an agent is pointed at.
export const DOC_ROUTES: Record<string, string> = {
  "/": agentInstructions,
  "/docs/AGENT_INSTRUCTIONS.md": agentInstructions,
  "/docs/CAPABILITIES.md": capabilities,
  "/docs/PHONE_RULES.md": phoneRules,
};

// The document the owner's private notes (PUT /admin/notes) are appended to.
export const NOTES_DOC = phoneRules;

export function withNotes(doc: string, notes: string): string {
  if (!notes.trim()) return doc;
  return `${doc.trimEnd()}

## The owner's private notes

Kept in the relay, not in the public repository.

${notes.trim()}
`;
}

// Fill in the placeholder with the host the agent actually called.
export function renderDoc(doc: string, host: string): string {
  return doc.replaceAll("<WORKER_HOST>", host);
}
