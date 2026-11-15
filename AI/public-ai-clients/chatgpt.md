# ChatGPT

Expose the server through an authenticated HTTPS MCP endpoint. The [Cloudflare guide](../../docs/cloudflare-access.md) describes one way to do that. In ChatGPT, add the remote MCP URL using the current [OpenAI MCP app instructions](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt). Product menus and availability can change; follow the current product documentation.

Use the public portal URL if you created a Cloudflare MCP portal, or the protected origin URL if your client and authentication flow support it. Test the connection by calling `knowledge_status`. `search_private_knowledge` requires `query` and also accepts `limit` (1-20), `path_prefix`, `source` and `include_historical`; the server additionally offers `browse_private_knowledge` and `read_private_document`. Not every client UI exposes optional arguments.

Retrieved excerpts are sent to ChatGPT. Review the [security model](../../docs/security-model.md) and index policy before connecting a private corpus.
