# AI clients

The MCP endpoint is http://127.0.0.1:8000/mcp by default. Remote clients need an authenticated HTTPS route to it. The server itself does not implement OAuth or enforce access tokens.

- [Cloudflare Access and MCP portal](../docs/cloudflare-access.md)
- [ChatGPT](public-ai-clients/chatgpt.md)
- [Claude](public-ai-clients/claude.md)
- [Google ADK example](googlecloud-adk-agent/README.md)

The server exposes four tools: `knowledge_status()` (no arguments), `search_private_knowledge(query, limit, path_prefix, source, include_historical)`, `browse_private_knowledge(path_prefix, limit, cursor, include_historical)` and `read_private_document(document_id, start_chunk, limit, include_historical)`. Discover the current schemas from the running endpoint. A client should normally make one comprehensive search per question and use a second only when the first misses essential evidence; browse and read then let it inspect an indexed document in order.
