# Changes in this iteration

A summary of the implemented improvements, grouped by area. All changes are
backwards compatible: new settings default to **off**, the tool set is unchanged
(4 tools), and existing tests remain green.

## 1. New features

### 1.1 MCP authentication (opt-in)
- `mcp_auth_enabled` config key + `mcp-auth.json` token file.
- `server.AccessGuard` ASGI wrapper: Bearer / `X-Api-Key` / `?access_token=`.
- `GET /healthz` liveness endpoint (unauthenticated).
- Settings UI section + `POST /api/access`; MCP restarts on change.

### 1.2 ACL (opt-in)
- `acl_allow_roots`: enforced in `validate_source_root`, `update`, `check_folder`,
  `host_agent.resolve_host_folder`, `inventory` and folder probes.
- `acl_deny_directories`: enforced in ingestion (`is_candidate`,
  `discover_documents`), retrieval (`source_allowed`, `query_filter`), the host
  agent (`inventory`, `allowed_file`) and the dashboard scan.
- `host_sync.root_allowed()` helper; ACL delivered across processes via env and
  the policy overlay in `read_policy()`.

### 1.3 Lexical bigrams (opt-in)
- `LEXICAL_BIGRAMS` adds adjacent token pairs to `sparse_vector` (32-bit term
  IDs kept, because Qdrant sparse indices are uint32).
- Included in the ingestion signature so toggling triggers reprocessing.

## 2. MCP server optimizations

| Change | Effect |
| --- | --- |
| Cached migration check (`collection_needs_update`) | The exact legacy `count` runs only when the point fingerprint or schema changes (30 s TTL), not on every search/browse/read |
| Distinct error messages | Model-load/encode failures report `MODEL_MESSAGE` instead of the misleading "Qdrant is unavailable" |
| `ensure_model()` / `warm_model()` | Single locked lazy load + optional startup preload |
| `/healthz` + HTTP readiness probe | "Running" means the app serves, not just a TCP port |
| Fail-fast config validation | Clear `SystemExit` for bad transport/port/token |
| Restart backoff + cap | No silent unbounded crash loop |
| `mcp.log` tail / exit code / restart count in `status()` | Observable MCP health |
| Content-hash policy cache | Policy edits take effect even with inode reuse |
| Fewer `collection_exists` calls | `expand_result_context` skips a redundant check |

## 3. Ingestion correctness

- `PIPELINE_VERSION` added to the index signature → parser/chunking/redaction/
  lexical changes now force reprocessing on incremental updates.
- Path terms fed to the sparse vector are redacted (`safe_source`).
- `ACL_DENY_DIRECTORIES` and `LEXICAL_BIGRAMS` included in effective policy /
  signature.

### 3.1 Deliberately deferred
Two findings from the review were **not** changed because they would alter
chunk counts and a specifically-covered prune contract, risking regressions:
a full token/sentence-boundary chunker rewrite and the empty-replacement prune
guard. Both are documented in [Retrieval](retrieval.md) and can be scheduled as
a versioned migration. The over-fetch reduction (`group_size`, two-phase payload
fetch) was also kept as-is to preserve ranking-quality test contracts; the
dominant per-request cost (the exact count) is already removed.

## 4. Functionality / richer results

- Additive payload fields: `section_path` (heading breadcrumb for MD/HTML),
  `title`, `parent_dir`, `language`, `char_count` (no dict-equality contracts
  broken). New payload indexes in `ensure_search_schema`.
- `browse_private_knowledge` now returns a `folders` list of immediate
  subfolders for library navigation.
- `public_chunk` exposes the new metadata; `classify_source` unchanged.

## 5. Correctness of error reporting

`backend_failure()` accepts a `message` override; `backend`/`reason` keys are
unchanged, so existing diagnostics keep working.

## 6. Tests and docs

- `tests/test_access.py`: 27 tests (auth guard, ACL, `/api/access`, env,
  server validation, host-agent ACL).
- `tests/test_dashboard.js`: new Settings test and `/api/access` mock route.
- Full suite: `139` Python tests + `32` JS tests, all green.
- New docs: this page, [Access control](access-control.md),
  [Configuration](configuration.md), [Operations](operations.md),
  [Verification](verification.md); index rewritten in [README](README.md).
