# Configuration

## 1. Where settings live

| Layer | File / source | Written by | Read by |
| --- | --- | --- | --- |
| Dashboard config | `/data/config.json` | `webui.py` (`_save`) | dashboard |
| Indexing policy | `/data/index-policy.yaml` | `webui.py` | ingestion, retrieval, host agent |
| Dashboard password | `/data/auth.json` (PBKDF2 hash) | `webui.py` | dashboard |
| MCP access token | `/data/mcp-auth.json` | `webui.py` (`set_access`) | dashboard → MCP env |
| Agent pairing | `/data/agent-auth.json` (SHA-256 hash) | `webui.py` | dashboard |
| Environment | `.env` / container env | operator / dashboard `_env()` | every process |

Config keys missing from an older `config.json` are filled from defaults on load,
so upgrades are backwards compatible. The four new access keys are **optional**
when restoring a backup created by an older release.

## 2. Config keys (`config.json`)

### 2.1 Original keys

`source_selection`, `source_mode`, `host_root`, `source_root`, `folders`,
`interval_hours`, `mcp_enabled`, `qdrant_enabled`, `last_run_at`,
`onboarding_complete`, `initial_index_skipped`, `setup_step`,
`setup_flow_version`, `sync_revision`, `sync_request`,
`scan_requested_revision`, `embedding_model`, `active_embedding_model`,
`legacy_embedding_model`.

### 2.2 Access-control keys (new)

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mcp_auth_enabled` | bool | `false` | Require a bearer token on the MCP endpoint |
| `acl_allow_roots` | list[str] | `[]` | Permitted absolute document roots; empty = unrestricted |
| `acl_deny_directories` | list[str] | `[]` | Directory names denied at any depth (case-insensitive) |
| `lexical_bigrams` | bool | `false` | Add bigram terms to the lexical index (phrase matching) |

All four are edited together in **Settings → Access control**, which posts to
`POST /api/access`. The token itself is stored in `mcp-auth.json`, never in
`config.json`, so it is not served as part of `status().config`.

## 3. Environment variables

### 3.1 Existing

| Variable | Default | Purpose |
| --- | --- | --- |
| `QDRANT_URL` | `http://127.0.0.1:6333` | Vector database |
| `MODEL_NAME` / `DENSE_COLLECTION` | E5 small / `documents` | Active model + collection |
| `KNOWLEDGE_ROOT` | — | Document root for source installs |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` (image: `0.0.0.0`) | Bind address |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http`, `sse`, `stdio` |
| `MCP_PATH` | `/mcp` | Streamable-HTTP path |
| `MCP_STATELESS` / `MCP_JSON_RESPONSE` | `0` | Proxy-friendly modes |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` / `EMBED_BATCH_SIZE` | `1200` / `200` / `32` | Chunking and batching |

### 3.2 New

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_AUTH_ENABLED` | `0` | Enable bearer-token auth in `server.py` |
| `MCP_AUTH_TOKEN` | — | The token (required when auth is enabled) |
| `MCP_PRELOAD_MODEL` | `0` | Warm the embedding model at startup |
| `ACL_DENY_DIRECTORIES` | empty | Comma-separated denied directory names |
| `LEXICAL_BIGRAMS` | `0` | Bigram lexical terms |

The dashboard sets the last four for its child processes automatically. Source
installs set them in `.env` (see `.env.example`).

## 4. How settings travel

Ingestion, retrieval (MCP) and the host agent are **separate processes**, so a
setting must be delivered explicitly:

- **Deny list** → `ACL_DENY_DIRECTORIES` env (ingestion + retrieval) and overlaid
  into the policy dict sent to the host agent (`agent_task().policy`).
- **Allowlist** → validated in `validate_source_root` / `check_folder`
  (dashboard) and passed to the agent through `agent_task().folder_probe` and the
  policy (`acl_allow_roots`), so `resolve_host_folder` enforces it too.
- **Auth** → `MCP_AUTH_ENABLED` + `MCP_AUTH_TOKEN` env on the MCP child; MCP is
  restarted when the setting changes, because env is read once at startup.
- **Policy** → the YAML file is read on every request; its cache is keyed by a
  **content hash**, so edits take effect reliably even with inode reuse.

## 5. Validation rules

- `acl_allow_roots`: up to 64 absolute paths; each normalized with `host_root`.
- `acl_deny_directories`: up to 128 bare names (no `/`, no `..`), casefolded.
- `mcp_auth_token`: 12–1024 characters; a random 32-byte token is generated if
  left blank. Invalid `MCP_TRANSPORT` / `MCP_PORT` / missing token cause a clear
  startup error instead of a silent crash loop.

See [Access control](access-control.md) for usage and examples.
