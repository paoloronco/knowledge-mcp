# Retrieval behavior

The active implementation is [retrieval.py](../knowledge-mcp/mcp/retrieval.py). It reads the live Qdrant collection rather than caching document text in the MCP process.

## Search

1. Validate the query, scope and result limit. Infer historical intent using whole words, or use the explicit `include_historical` argument.
2. Apply the current indexing policy, lifecycle and folder/source scope as Qdrant filters **before** retrieving candidates. Incomplete document uploads are excluded.
3. Run dense semantic search and sparse lexical search in the same model collection. Each channel requests up to 40 document groups with two passages each, so one long document cannot occupy all candidate positions.
4. Combine candidate ranks with reciprocal rank fusion (`k=60`). Matching source paths and headings receive a modest additional boost. No hardcoded list of technology names or corpus-wide anchor scan is required.
5. Keep one result per document identity, with eight results by default and a maximum of 20. Identically named files in separate projects remain independent.
6. Expand the first two results with nearby passages from the same section, capped at 6,000 characters per expansion. Context references retain their own chunk, page and heading information.
7. Recursively redact common credential patterns before returning any response.

The lexical representation uses Unicode terms and identifier components (for example, `nginx.conf` also matches `nginx`), BM25-style term-frequency saturation and a fixed reference passage length of 256 tokens. Path terms have weight 3 and heading terms weight 2. Qdrant maintains IDF through its [sparse-vector index](https://qdrant.tech/documentation/search/text-search/sparse-vector-search/). This is an approximation to corpus-average BM25, not Qdrant's learned sparse embedding model. Term IDs use a deterministic 32-bit hash; rare collisions are possible. Semantic retrieval handles paraphrases and cross-language questions.

Historical directory names (`old`, `archive`, `archivio`, `backup`, `backups`, `dismessi`) are excluded by default. An explicit historical query allows both current and archived evidence. Custom and mandatory exclusions still apply. Retrieval scores are ranking signals, not confidence probabilities or proof that a passage answers the question.

## MCP tools

| Tool | Purpose | Pagination / scope |
| --- | --- | --- |
| `knowledge_status` | Inspect collection availability, model and migration status | Reports chunk and document counts |
| `search_private_knowledge` | Find passages with source, section, page and document ID | `limit=1..20`, optional `path_prefix`, exact `source`, `include_historical` |
| `browse_private_knowledge` | Discover indexed documents with short previews | `limit=1..100`, folder `path_prefix`; continue with `next_cursor` |
| `read_private_document` | Read passages in document order | Use a returned `document_id`, `start_chunk=0`, `limit=1..20`; continue with `next_chunk` |

For example, search with `query="VLAN del telescopio", path_prefix="observatory"`, then read the returned `document_id`. Reading returns each passage's own page/slide and heading, so the client can cite an adjacent passage accurately. Duplicate source paths can also be used as an exact search scope. Browsing and reading use only indexed text and never open arbitrary server files. A policy change applies to all tools on the next request.

The server instructions tell clients to treat document contents as untrusted evidence, cite sources, and never infer or search for redacted values. Documents without extractable text need OCR or conversion before indexing; images and scanned PDF pages are not interpreted.

## Existing indexes and updates

Run an incremental update after installing this version. It adds the sparse vector name and payload indexes without deleting the collection, then reprocesses old documents using the new versioned representation. Other model collections are preserved. Until the migration finishes, every retrieval tool (`search_private_knowledge`, `browse_private_knowledge`, `read_private_document`) includes `index_update_required: true` with an actionable `message`; browse and read cannot return legacy chunks, while search falls back to dense-only retrieval when only the lexical vector is missing. `knowledge_status` reports the same flag and lists the server's `tools`. Restart MCP once when installing new Python code; normal incremental updates are then visible without a restart. MCP clients cache the tool list: reconnect them after an upgrade so `browse_private_knowledge` and `read_private_document` appear.

The state records a pipeline signature, so changing the model or chunk size invalidates old chunk layouts. Embeddings are computed and uploaded in bounded batches. A document becomes searchable only after all its batches have been acknowledged and its source has remained stable. Previous layouts are removed after successful replacement. A failed replacement preserves the last usable version; deleted documents are still pruned when discovery and hashing completed, even if a different document failed parsing. A limited run never prunes.

Files with no extractable text (scanned PDFs, image-only slides) raise a dedicated `EmptyDocumentError`. They are skipped rather than counted as failures: the run still succeeds, the source keeps its last usable indexed version, and the document is recorded in state with a pipeline signature so later runs do not parse it again. Skipped sources are reported separately from genuine errors in the dashboard's indexing details dialog. A run with genuine per-document errors still activates the successfully published index and the selected model; only a total failure leaves the previous model active.

State updates use an append-only recovery journal, compacted into the JSON checkpoint at the end of a run. A single-writer lock protects the state. Unchanged files reuse checksums when size, timestamps and identity agree; `--verify-hashes` forces a complete content verification. On filesystems that preserve all metadata during edits, use full verification. Source and destination host synchronization caches use the same metadata checks and are cleared on restart; a manually requested host sync forces source rehashing. A hashing failure keeps the last usable copy of only the affected sources; documents genuinely removed from the library are still pruned.

## MCP client compatibility

The server speaks MCP Streamable HTTP at `/mcp`, which all current MCP clients (Claude, ChatGPT, Cursor, VS Code, Gemini CLI, and SDK-based agents) support. It is exposed by the dashboard-managed MCP process on `MCP_PORT` (default `8000`). Optional environment variables adapt it to less common setups:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http`, `sse` (legacy clients at `/sse`), or `stdio` (local desktop clients running the image with `docker run -i`) |
| `MCP_PATH` | `/mcp` | Streamable HTTP endpoint path |
| `MCP_STATELESS` | `0` | Set to `1` for stateless requests behind load balancers or authenticated proxies that do not preserve session IDs |
| `MCP_JSON_RESPONSE` | `0` | Set to `1` to return plain JSON instead of an SSE response stream for simple clients |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Bind address (the image sets `0.0.0.0`) |
| `MCP_AUTH_ENABLED` / `MCP_AUTH_TOKEN` | `0` / — | Optional bearer-token auth (see [Access control](access-control.md)) |
| `MCP_PRELOAD_MODEL` | `0` | Warm the embedding model at startup |
| `ACL_DENY_DIRECTORIES` | empty | Comma-separated denied directory names |
| `LEXICAL_BIGRAMS` | `0` | Phrase-matching bigrams (re-index after changing) |

The endpoint has no built-in HTTP authentication by default; enable
`MCP_AUTH_ENABLED` in Settings or put an authenticated proxy / [Cloudflare Access](cloudflare-access.md)
in front of it before remote use. `GET /healthz` is an unauthenticated liveness
endpoint. `knowledge_status` lets a client confirm the index is available before
relying on search results.

## Recent additions (response metadata and navigation)

Chunks returned by `search`, `browse` and `read` now carry additive metadata:
`section_path` (heading breadcrumb for Markdown/HTML), `title`, `parent_dir`,
`language`, `char_count`. These are new keys; existing fields are unchanged.
`browse_private_knowledge` additionally returns a `folders` list of immediate
subfolders under `path_prefix`, so a client can walk the indexed library.

The per-request migration check is cached by collection fingerprint, so the
exact legacy count no longer runs on every query. See [Changes](changes.md) and
[Operations](operations.md) for details. An optional `LEXICAL_BIGRAMS` flag
adds phrase-matching terms to the sparse index (re-index after toggling).

## Verification

From the repository root, run `python -m unittest discover -s tests -q` and `node --test tests/test_dashboard.js` with the service dependencies installed. `test_mcp_protocol.py` exercises Streamable HTTP initialization, tool listing, search, browse, document pagination and invalid paths through an SDK client; `test_access.py` covers auth, ACL and server configuration.

Set `KNOWLEDGE_TEST_REAL_MODEL=1` to run the protocol test with the locally cached E5 small model. Set `KNOWLEDGE_SCALE_DOCUMENTS=10000` when running `test_retrieval.py` to exercise bounded candidate retrieval on 10,000 synthetic documents. These tests use Qdrant's local engine; they verify correctness and request bounds, not production server latency, GPU throughput or recall on a private corpus.
