# Operations

## 1. Health and readiness

### 1.1 MCP `/healthz`

The MCP app serves `GET /healthz` (always `200 {"status":"ok","service":"knowledge-mcp"}`).
It is implemented as a thin ASGI wrapper (`server.AccessGuard`) around the SDK
app, so it works for both `streamable-http` and `sse` and bypasses bearer auth.

The dashboard now probes this endpoint over HTTP instead of a bare TCP connect,
so "MCP running" means the app is actually serving, not merely holding a port.

### 1.2 Dashboard `/api/health`

Returns app/Qdrant readiness and `mcp_running` (used by the Docker healthcheck).
`GET /api/status` additionally exposes:

| Field | Meaning |
| --- | --- |
| `mcp_auth` | `{enabled, token}` for the Settings page |
| `mcp_restarts` | Consecutive crash-restart attempts |
| `mcp_error_text` | Set when restarts are capped |
| `mcp_exit_code` | Last MCP process exit code |
| `mcp_log_tail` | Last 4 KB of `/data/mcp.log` |

### 1.3 `knowledge_status`

The MCP tool reports collection availability, chunk/document counts, active
model, index version and `index_update_required`. Prefer it from an AI client
before relying on search results.

## 2. Startup, shutdown and restarts

### 2.1 Fail-fast validation

`server.main()` validates `MCP_TRANSPORT`, `MCP_PORT`, and that
`MCP_AUTH_ENABLED=1` has a token. Bad values raise a clear `SystemExit` instead
of an unbounded crash loop.

### 2.2 Crash-restart backoff

`_restart_failed_services()` (every 30 s) now:

- waits with exponential backoff (30 → 60 → 120 → 240 → 300 s);
- caps at **8** consecutive restarts, then sets `mcp_error_text` and stops;
- resets the counter once `/healthz` reports healthy, or on a manual
  **Start/Restart** from the dashboard.

### 2.3 Model warm-up

With `MCP_PRELOAD_MODEL=1` a daemon thread loads the `SentenceTransformer` at
startup, so the first real query does not pay the cold-start cost. The single
global model is guarded by `_model_lock`; tool handlers remain synchronous and
run in the SDK's thread pool.

### 2.4 Graceful stop

`_stop_mcp()` sends `terminate()`, waits 5 s, then `kill()`. MCP stdout/stderr
go to `/data/mcp.log`; its tail is now visible in `/api/status`.

## 3. Logging

| File | Contents |
| --- | --- |
| `/data/mcp.log` | MCP process stdout/stderr |
| `/data/ingest.log` | Ingestion full log (downloadable in the dashboard) |
| `/data/index-stages.log` | Timestamped stage log |
| `/data/index-errors.jsonl`, `index-skipped.jsonl` | Per-document issues |

## 4. Backups

`Settings → Backup and restore` archives settings, policy, state, Qdrant
indexes, synchronized documents and credentials (including `mcp-auth.json`) —
**unencrypted**, keep it private. Restoring older backups is supported: the four
new access keys are optional and fall back to defaults. See
[Docker deployment](docker.md#downloading-and-importing-a-backup).

## 5. Updates

`bash scripts/self-update.sh` (see [Self-update](self-update.md)). After a code
upgrade, run one **incremental update** per model index so new payload fields,
the pipeline signature and any lexical changes are applied. Restart MCP once
after deploying new server code so clients refresh the tool list.

## 6. Routine checks

| Symptom | Check |
| --- | --- |
| MCP "ready" but tools fail | `mcp_log_tail`, `knowledge_status`, Qdrant `/readyz` |
| Repeated restarts | `mcp_restarts`, `mcp_error_text`, transport/port config |
| First query slow | `MCP_PRELOAD_MODEL=1` |
| Policy edit not applied | content-hash cache is automatic; restart MCP on code changes |
| Stale results after upgrade | run an incremental update (see [Changes](changes.md)) |
