# Security model

## Boundaries

- The dashboard has no browser document upload. Compose starts and connects the Linux host agent automatically. It receives read-only access to the host filesystem, reads only the selected root, and transfers policy-eligible documents into the app's persistent volume. Its root filesystem is read-only; it drops capabilities except `DAC_READ_SEARCH`, masks host runtime/system directories, and has no Docker socket. Legacy uploaded documents may also remain in the app volume. Treat the volumes as private data.
- Dashboard login is optional, including for host folder selection. Without it, anyone who can reach the dashboard can change its settings and select host folders. Automatic agent connection credentials stay in a private Docker volume and are stored hashed in the app volume. The agent shares the app's network namespace and communicates over loopback. It publishes no ports. Review the host read access in Compose before deploying; on Linux older than 5.12, nested bind mounts may remain writable.
- Bind Qdrant to localhost. The application does not authenticate to Qdrant by default.
- Docker publishes the Web UI and MCP server on the host's LAN interfaces. The Web UI can use a password; the MCP server has no built-in login. Limit access with a firewall and add an authenticated HTTPS proxy or Cloudflare Access before Internet exposure. Use HTTPS when entering the Web UI password over a network.
- Review index-policy.yaml before ingestion. Directory and extension filters are the first boundary. Retrieval repeats restricted-path filtering, including Windows-style separators, as a second boundary.
- Bundled directory exclusions cover build artifacts and caches. They are mandatory, and older saved policies gain any missing exclusions at startup. The bundled policy does not include user-specific folder names. Select only document roots intended for indexing, and add any local exclusions through the dashboard. Search, browsing and document reading load the current policy and apply it before retrieval. Run **Run incremental update** to remove excluded documents from stored indexes as well. If no eligible documents remain, run `python ingestion/ingest.py --allow-empty` from `knowledge-mcp` to clear the index intentionally.
- Avoid putting secrets in the source corpus. Common credential patterns are redacted before chunking and again recursively at the MCP output boundary. This cannot guarantee removal of arbitrary credentials or sensitive prose. Existing indexes need an incremental update to receive ingestion-time redaction.
- MCP clients receive selected source text. Retrieved excerpts leave the host by design.

## Operational checks

1. If using a host source folder through the agent, check its connection and synchronization status before indexing. If using a bind mount, verify that it is read-only.
2. Inspect the ingestion policy and a dry-run sample before the first full index.
3. Keep .env, Cloudflare tokens, the Qdrant volume and ingestion state outside version control.
4. Test the intended access controls on the public hostname before giving it to a client.
5. Rotate any exposed credential; redaction does not undo exposure.

The [Cloudflare guide](cloudflare-access.md) covers one possible access layer. No specific homelab address, Google project, or Cloudflare token is required by this repository.
