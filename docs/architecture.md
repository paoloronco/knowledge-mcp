# Architecture

```text
Selected read-only documents
    -> host companion (optional sync with checksum cache)
    -> ingestion (policy, parsing, redaction, token-aware chunks, bounded embedding batches)
    -> per-model Qdrant collection: dense vectors + sparse lexical vectors + payload indexes
    -> live filtered retrieval / document navigation
    -> read-only MCP tools over Streamable HTTP
```

The dashboard passes `KNOWLEDGE_ROOT` and selected relative `INDEX_SOURCE_PATHS` to ingestion and supervises Qdrant and MCP. The host companion scans selected folders periodically after onboarding and synchronizes policy-eligible files. Unchanged file checksums are reused only while size, timestamps and file identity agree; source transfers still verify their SHA-256 digest. Excluded directories are pruned during discovery, and unreadable directories abort the scan rather than appearing deleted.

Ingestion keeps Markdown, HTML, Word heading and PDF page/PowerPoint slide references. Word and slide tables are included. It redacts recognized credential patterns before splitting, retains short text and splits further to respect the embedding model's tokenizer limit. Byte-identical files of the same format and lifecycle are grouped with source aliases; historical and current copies remain separate. Each model retains its own collection. New dense vectors and payloads use on-disk storage.

The state checkpoint records content hashes, sources, file metadata and a pipeline signature. An append-only journal records pending and completed documents without rewriting the entire checkpoint for every file. A single-writer lock prevents competing runs with the same state. Failed documents can be retried; successful documents remain recorded. Each document is published for search after all its batches complete. A successful replacement removes older chunk layouts. A complete scan removes missing documents, while retaining the last usable version of a failed replacement. `--limit` never purges missing documents. An empty source is rejected unless `--allow-empty` is explicit.

Retrieval sends dense and sparse queries to Qdrant with policy and scope filters applied before candidate limits. Grouping candidates by document prevents a large file from crowding out other sources. Reciprocal rank fusion combines the results; bounded neighbor reads enrich the strongest passages. Browse and read tools paginate indexed documents without loading the full corpus into Python memory. All output passes recursive redaction.

Normal updates are visible immediately without an MCP restart. The dashboard restarts MCP when switching the active embedding model or after a rebuild; it leaves the active model unchanged after failed indexing. Rebuilds still recreate the selected collection and can leave a partial index after an error. External Qdrant instances must be managed outside the dashboard.

The MCP server never modifies source documents or executes retrieved instructions. It does not provision Cloudflare, mount SMB, or enforce HTTP authentication. Those controls belong to the deployment. See [retrieval behavior](retrieval.md) and [security model](security-model.md).
