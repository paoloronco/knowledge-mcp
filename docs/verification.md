# Verification

## 1. Required commands (from the repository root)

```bash
python -m unittest discover -s tests -q
node --test tests/test_dashboard.js
```

Both are required before pushing (see root `AGENTS.md`). No test needs a live
Qdrant: backend tests use `QdrantClient(":memory:")` with a `FakeModel` and a
patched `sentence_transformers`.

## 2. Current status

| Suite | Result |
| --- | --- |
| Python (`unittest discover -s tests -q`) | **139 tests, OK** (2 skipped) |
| JavaScript (`node --test tests/test_dashboard.js`) | **32 tests, pass** |

The 2 skips are opt-in integrations (real Qdrant binary, POSIX-only symlink
test).

## 3. Coverage map

| Source | Tests |
| --- | --- |
| `mcp/server.py` | `test_mcp_protocol.py` (initialize / list_tools / call_tool over Streamable HTTP), `test_access.py` (`AccessGuard`, `main()` validation, `build_http_app`) |
| `mcp/retrieval.py` | `test_retrieval.py`, `test_retrieval_hardening.py`, `test_core.py`, `test_access.py` (ACL) |
| `ingestion/ingest.py` | `test_core.py`, `test_retrieval.py`, `test_retrieval_hardening.py` |
| `mcp/webui.py` | `test_webui.py`, `test_index_issues.py`, `test_readiness.py`, `test_access.py` (`/api/access`) |
| `mcp/backup.py` | `test_backup.py` |
| `mcp/redaction.py` | `test_redaction_hardening.py` |
| `mcp/index_schema.py` | indirect via retrieval tests; `test_access.py` covers ACL helpers |
| `host_agent.py`, `mcp/host_sync.py` | `test_host_agent.py`, `test_access.py` |
| `mcp/app.js`, `mcp/webui.html` | `test_dashboard.js` |
| `scripts/self_update.py` | `test_self_update.py` |

## 4. What the new tests assert

`tests/test_access.py` (27 tests):

- **Auth**: disabled pass-through, `/healthz`, 401 on missing/wrong token,
  Bearer / `X-Api-Key` / query token acceptance, lifespan forwarding.
- **ACL**: `root_allowed` (empty list, nesting, Windows case-insensitivity),
  `allowed_file` deny at any depth, `read_policy` overlay, `source_allowed` and
  `query_filter` deny, env parsing.
- **Settings**: `set_access` normalization, auto-generated token, restart,
  invalid values, `_env()` propagation, `POST /api/access` over HTTP.
- **Server**: fail-fast transport/port/token validation, `_env_flag`,
  `build_http_app` wrapping.
- **Host agent**: allowlist rejection, denied-directory inventory filtering.

`tests/test_dashboard.js`: new Settings controls render and `saveAccess()`
posts the expected `/api/access` body.

## 5. Notes

- The MCP protocol test builds its own ASGI app from `mcp.streamable_http_app()`;
  it does not execute `server.py`'s `__main__`, which `test_access.py` covers.
- `test_setup_progress_is_persisted_and_requires_successful_initial_index`
  launches a real dry-run subprocess and is timing-sensitive; occasional flakes
  are unrelated to logic and pass on rerun.
- Optional scale test: `KNOWLEDGE_SCALE_DOCUMENTS=10000` on `test_retrieval.py`;
  real model: `KNOWLEDGE_TEST_REAL_MODEL=1` on `test_mcp_protocol.py`.
