# Agent Registry

Agent Registry is optional metadata for a Google ADK deployment. The active agent in adk/agent.py connects directly to KNOWLEDGE_MCP_URL; registration is not part of the MCP server setup.

The server advertises `knowledge_status` (no arguments), `search_private_knowledge` (required `query` plus optional scope and limit arguments), `browse_private_knowledge` and `read_private_document`. Discover tool schemas from the running MCP endpoint rather than maintaining a second manual JSON toolspec in this repository.
