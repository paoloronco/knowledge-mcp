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

Run an incremental update after installing this version. It adds the sparse vector name and payload indexes without deleting the collection, then reprocesses old documents using the new versioned representation. Other model collections are preserved. Search reports `index_update_required` for a collection without the lexical representation. Restart MCP once when installing new Python code; normal incremental updates are then visible without a restart.

The state records a pipeline signature, so changing the model or chunk size invalidates old chunk layouts. Embeddings are computed and uploaded in bounded batches. A document becomes searchable only after all its batches have been acknowledged and its source has remained stable. Previous layouts are removed after successful replacement. A failed replacement preserves the last usable version; deleted documents are still pruned when discovery and hashing completed, even if a different document failed parsing. A limited run never prunes.

State updates use an append-only recovery journal, compacted into the JSON checkpoint at the end of a run. A single-writer lock protects the state. Unchanged files reuse checksums when size, timestamps and identity agree; `--verify-hashes` forces a complete content verification. On filesystems that preserve all metadata during edits, use full verification. Source and destination host synchronization caches use the same metadata checks and are cleared on restart; a manually requested host sync forces source rehashing.

## Verification

From the repository root, run `python -m unittest discover -s tests -p 'test_*.py'` and `node --test tests/test_dashboard.js` with the service dependencies installed. `test_mcp_protocol.py` exercises Streamable HTTP initialization, tool schemas, search, browse, document pagination and invalid paths through an SDK client.

Set `KNOWLEDGE_TEST_REAL_MODEL=1` to run the protocol test with the locally cached E5 small model. Set `KNOWLEDGE_SCALE_DOCUMENTS=10000` when running `test_retrieval.py` to exercise bounded candidate retrieval on 10,000 synthetic documents. These tests use Qdrant's local engine; they verify correctness and request bounds, not production server latency, GPU throughput or recall on a private corpus.
