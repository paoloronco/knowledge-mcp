# Troubleshooting

## Docker container exits immediately or the Web UI is unreachable

Use the complete `docker run` commands in the root README, including port, volume, and restart flags. Check `docker ps -a`, `docker logs knowledge-mcp`, and `docker logs knowledge-mcp-host-agent`. With Compose, check `docker compose ps` and `docker compose logs app host-agent`. Open `http://HOST_IP:8080` from the LAN. If it works at `http://127.0.0.1:8080` on the Docker host but not from another computer, check that Docker published port 8080 on `0.0.0.0` and that the host firewall allows it.

## Docker prints localized progress messages

The Docker client prints its own container creation and pull messages using its host settings. These messages are outside the application. On Linux, run a command with `LC_ALL=C` if you want to request English output from the Docker client, for example `LC_ALL=C docker compose up -d`.

## ImportError: MCPServer

Install the declared dependencies in the active virtual environment: python -m pip install -r knowledge-mcp/requirements.txt. The server uses the MCP Python SDK 2.x import path mcp.server.mcpserver.

## Missing policy or knowledge root

Create knowledge-mcp/.env from .env.example and set KNOWLEDGE_ROOT to an existing directory. Ingestion reads knowledge-mcp/mcp/index-policy.yaml by default; POLICY_FILE can override it.

## Qdrant unavailable

Check curl --fail http://127.0.0.1:6333/readyz and QDRANT_URL. `knowledge_status` reports `unavailable` (with a `reason` exception class) when the backend cannot be reached, and `empty` with `index_update_required` when the collection does not exist yet. Ingestion must create and populate the collection before the first search.

## Search misses newly indexed files

Current retrieval reads Qdrant directly. Check that indexing completed and that the MCP process uses the same collection and model as ingestion. After a code upgrade, restart MCP once to load the new code; subsequent indexing updates need no restart. If `index_update_required` is returned, run an incremental update to migrate the old collection.

## Documents are indexed but MCP returns no results

`knowledge_status` reporting `status: "ok"` with `index_update_required: true` means the collection still needs the upgrade to hybrid search and navigation. Legacy chunks lack the versioned payload fields required by the retrieval filters, so browse and read return nothing for them; search keeps working with dense-only retrieval when only the lexical vector is missing. All three tools include `index_update_required: true` with an actionable `message`. Run one incremental update per embedding model from the dashboard (or `python ingestion/ingest.py` for a source install), then wait for completion. Reconnect the MCP client afterwards to refresh its cached `tools/list`; the server registers `knowledge_status`, `search_private_knowledge`, `browse_private_knowledge` and `read_private_document` unconditionally. Verify with `knowledge_status`: `index_update_required` becomes `false`.

## Documents unavailable during host sync

After recreating the containers, the Documents badge reads `syncing…` (or `waiting for sync`) until the companion rescans the host folders, transfers changed files, and commits the first sync. The overview and Document folders page show the phase and counts; the badge turns `ready` when the sync completes. `docker logs knowledge-mcp-host-agent` records startup, connection, scan results, transfer progress, and completion. If the badge stays `unavailable`, check that the companion is running and paired with `docker ps` and `docker logs knowledge-mcp-host-agent`.

## Empty PDF results / "Documents were skipped"

The parser extracts embedded text only. It does not OCR scanned pages. A PDF made of scanned images, or a slide deck containing only pictures, is **skipped**, not treated as a failure: the run still succeeds, successfully indexed documents remain searchable, and the last usable indexed version of that source is preserved. Skipped files are recorded in the persistent ingestion state with a signature, so they are not re-parsed on later incremental runs (which keeps indexing fast). They appear in **Indexing → View details** under "Skipped: no extractable text", where each entry has an **Ignore this file** action and **Ignore all skipped documents** can exclude them all at once. To make them searchable, run OCR on the source files and save a text-bearing copy; the changed content is then picked up automatically. Short valid passages are still retained.

## Indexing reaches 100% but reports failures

The percentage counts documents examined, including any that fail parsing or indexing. Documents completed before the error remain in Qdrant: a run with a few genuine errors still activates the index and the selected model. The next run skips unchanged documents recorded in the persistent ingestion state. The Indexing page shows a compact note and opens per-document details in a separate dialog; downloads for the full output and timestamped status log remain available. You can fix a source file or choose **Ignore this file** to add it to the policy before retrying. Older runs may have only the full log; to inspect their per-file errors in a Docker installation, run:

```bash
docker exec knowledge-mcp sh -lc 'find /data/ingestion -name errors.log -type f -print -exec tail -n 20 {} \;'
```

Review file paths before sharing this output because it may contain private names. Microsoft Office lock files beginning with `~$` are excluded by current releases. After updating both app and host-agent containers while retaining their named volumes, run the incremental update from the dashboard. Documents are reprocessed when the index format, model or chunking settings have changed; otherwise unchanged documents are skipped.

## A file changed but incremental indexing skipped it

Incremental runs reuse SHA-256 checksums when file size, nanosecond timestamps and file identity agree. On filesystems or tools that preserve all of that metadata during edits, run `python ingestion/ingest.py --verify-hashes`. Manual host-agent resynchronization also clears its in-memory checksum cache. Use reliable local timestamps for automatic updates.

## SentenceTransformer import fails in torchvision

Check that torch and torchvision in the active environment are compatible. A mismatched pair can fail before the embedding model loads. Ingestion dry runs and knowledge_status do not require the model.

## Remote client receives 403

Check the proxy or Cloudflare Access policy. The MCP server does not issue 403 on its own. Test the endpoint with an MCP client, not a plain GET request. The dashboard (port 8080) does issue 403 for `Host` headers that are neither `localhost` nor an IP address; if it is served behind a reverse proxy under a hostname, list that hostname in `WEBUI_ALLOWED_HOSTS` (comma-separated). See the [security model](security-model.md).

## Service logs

On a systemd deployment, use sudo journalctl -u knowledge-mcp -f. Ingestion parse errors are written under INGESTION_BASE_DIR/logs/errors.log.
