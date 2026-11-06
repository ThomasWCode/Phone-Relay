// Markdown files are bundled as text by wrangler's "Text" rule (see wrangler.example.jsonc).
declare module "*.md" {
  const content: string;
  export default content;
}
