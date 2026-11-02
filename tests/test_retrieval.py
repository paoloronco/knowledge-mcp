"""Regression coverage for index migration, bounded retrieval and document navigation."""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from qdrant_client import QdrantClient, models
from docx import Document
from pptx import Presentation
from pptx.util import Inches
import yaml

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "mcp"))
import ingest
import retrieval
from index_schema import sparse_vector, tokens, INDEX_VERSION
from test_core import FakeModel


class IndexingAndNavigationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "documents"
        self.root.mkdir()
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.policy_file = self.base / "policy.yaml"
        self.policy_file.write_text(
            (ROOT / "mcp" / "index-policy.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for p in [
            patch.dict(
                os.environ,
                {"KNOWLEDGE_ROOT": str(self.root), "INDEX_SOURCE_PATHS": '[""]'},
            ),
            patch.dict(
                sys.modules,
                {
                    "sentence_transformers": types.SimpleNamespace(
                        SentenceTransformer=FakeModel
                    )
                },
            ),
            patch.object(ingest, "QdrantClient", return_value=self.client),
            patch.object(ingest, "embedding_device", return_value="cpu"),
            patch.object(ingest, "POLICY_FILE", self.policy_file),
            patch.object(retrieval, "POLICY_FILE", self.policy_file),
            patch.object(ingest, "STATE_FILE", self.base / "state.json"),
            patch.object(ingest, "ERROR_LOG", self.base / "errors.log"),
            patch.object(ingest, "RUN_ERRORS_FILE", None),
            patch.object(ingest, "STAGES_FILE", None),
            patch.object(ingest, "PROGRESS_FILE", None),
            patch.object(retrieval, "client", self.client),
            patch.object(retrieval, "model", FakeModel()),
        ]:
            self.stack.enter_context(p)
        self.stack.enter_context(warnings.catch_warnings())
        warnings.filterwarnings("ignore", message="Payload indexes have no effect")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_index(self, *args):
        with (
            patch.object(sys, "argv", ["ingest.py", *args]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            ingest.main()

    def points(self):
        return self.client.scroll(
            ingest.COLLECTION_NAME, limit=1000, with_vectors=False
        )[0]

    def test_live_update_browse_and_read_without_corpus_cache(self):
        self.write(
            "alpha/README.md",
            "# Network\nVLAN 42 connects the observatory.\n# Storage\nBackups use snapshots.",
        )
        self.write(
            "beta/README.md", "# Database\nQdrant retains the telescope telemetry."
        )
        self.run_index()
        # The two READMEs must remain independently discoverable.
        result = retrieval.search("observatory telescope", limit=10)
        self.assertEqual(
            {r["source"] for r in result["results"]},
            {"alpha/README.md", "beta/README.md"},
        )
        first = retrieval.browse_documents(limit=1)
        second = retrieval.browse_documents(limit=1, cursor=first["next_cursor"])
        self.assertNotEqual(
            first["documents"][0]["document_id"], second["documents"][0]["document_id"]
        )
        self.assertIsNone(second["next_cursor"])
        scoped = retrieval.search("Qdrant", path_prefix="beta")
        self.assertEqual([r["source"] for r in scoped["results"]], ["beta/README.md"])
        alpha = retrieval.browse_documents("alpha")["documents"][0]
        beta = retrieval.browse_documents("beta")["documents"][0]
        chunk = retrieval.read_document(alpha["document_id"], limit=1)
        self.assertEqual(chunk["chunks"][0]["section"], "Network")
        next_page = retrieval.read_document(
            alpha["document_id"], chunk["next_chunk"], limit=1
        )
        self.assertEqual(next_page["chunks"][0]["section"], "Storage")
        self.assertIsNone(next_page["next_chunk"])
        self.write(
            "beta/README.md",
            "# Database\nA replacement describes astrometry and nebula calibration.",
        )
        self.run_index()
        new = retrieval.search("astrometry", source="beta/README.md")["results"][0]
        self.assertIn("astrometry", new["text"])
        self.assertNotIn("telemetry", new["text"])
        self.assertFalse(retrieval.read_document(beta["document_id"])["chunks"])

    def test_policy_and_history_are_filtered_before_limits_and_at_every_tool(self):
        for number in range(85):
            self.write(f"old/history-{number}.md", f"Qdrant archival material {number}")
        self.write("active.md", "Qdrant runs on the current database node.")
        self.run_index()
        self.assertEqual(
            [r["source"] for r in retrieval.search("Qdrant")["results"]], ["active.md"]
        )
        self.assertEqual(
            len(
                retrieval.browse_documents(include_historical=True, limit=100)[
                    "documents"
                ]
            ),
            86,
        )
        self.assertEqual(
            retrieval.detect_temporal_intent("folder naming for uploads"), "current"
        )
        document_id = retrieval.browse_documents()["documents"][0]["document_id"]
        policy = yaml.safe_load(self.policy_file.read_text())
        policy["exclude_files"] = ["ACTIVE.MD"]
        self.policy_file.write_text(yaml.safe_dump(policy), encoding="utf-8")
        self.assertFalse(retrieval.search("Qdrant")["results"])
        self.assertFalse(retrieval.read_document(document_id)["chunks"])
        self.assertFalse(retrieval.browse_documents()["documents"])

    def test_configuration_change_replaces_chunks_and_upgrade_preserves_other_collections(
        self,
    ):
        self.write("note.md", "Qdrant configuration and observatory notes. " * 60)
        self.client.create_collection(
            ingest.COLLECTION_NAME,
            models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        self.client.upsert(
            ingest.COLLECTION_NAME,
            [
                models.PointStruct(
                    id=1, vector=[1.0, 0.0, 0.0], payload={"document_id": "legacy"}
                )
            ],
        )
        ingest.save_state({"documents": {"legacy": {"source": "note.md"}}})
        self.client.create_collection(
            "other_model", models.VectorParams(size=3, distance=models.Distance.COSINE)
        )
        self.run_index()
        self.assertNotIn(1, [point.id for point in self.points()])
        old_count = len(self.points())
        with patch.object(ingest, "CHUNK_SIZE", 5000):
            self.run_index()
        self.assertLess(len(self.points()), old_count)
        self.assertEqual({p.payload["total_chunks"] for p in self.points()}, {1})
        self.assertTrue(self.client.collection_exists("other_model"))
        with patch.object(
            retrieval, "MODEL_NAME", "a-different-model-with-the-same-dimension"
        ):
            self.assertFalse(retrieval.search("Qdrant")["results"])
        self.assertTrue(
            all(p.payload["index_version"] == INDEX_VERSION for p in self.points())
        )

    def test_unchanged_run_avoids_model_hashing_and_upserts_but_full_verification_rehashes(
        self,
    ):
        self.write("note.md", "A short document with a useful port: 8443.")
        self.run_index()
        with (
            patch.object(ingest, "sha256_file", wraps=ingest.sha256_file) as hashes,
            patch.object(FakeModel, "encode") as encode,
            patch.object(self.client, "upsert") as upsert,
        ):
            self.run_index()
            hashes.assert_not_called()
            encode.assert_not_called()
            upsert.assert_not_called()
            self.run_index("--verify-hashes")
            self.assertEqual(hashes.call_count, 1)

    def test_partial_failure_never_exposes_staged_chunks_and_retry_cleans_up(self):
        self.write("note.md", "Original telescope settings. " * 50)
        self.run_index()
        old_text = [p.payload["text"] for p in self.points()]
        upsert = self.client.upsert
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return upsert(*args, **kwargs)

        with (
            patch.object(ingest, "CHUNK_SIZE", 500),
            patch.object(ingest, "EMBED_BATCH_SIZE", 1),
            patch.object(self.client, "upsert", side_effect=interrupted),
        ):
            with self.assertRaises(SystemExit):
                self.run_index()
        self.assertEqual(
            [p.payload["text"] for p in self.points() if p.payload["ready"]], old_text
        )
        self.assertTrue(any(not p.payload["ready"] for p in self.points()))
        self.assertIn("Original", retrieval.search("telescope")["results"][0]["text"])
        with patch.object(ingest, "CHUNK_SIZE", 500):
            self.run_index()
        points = self.points()
        self.assertEqual(len({p.payload["index_signature"] for p in points}), 1)
        self.assertTrue(all(p.payload["ready"] for p in points))

    def test_corrupt_journal_tail_recovers_successes_and_pending_removed_file(self):
        self.write("good.md", "Valid nebula documentation.")
        self.run_index()
        state = ingest.load_state()
        ingest.record_state(state, "b" * 64, {"source": "removed.md", "pending": True})
        with ingest.STATE_FILE.with_suffix(".journal").open(
            "a", encoding="utf-8"
        ) as output:
            output.write('{"document_id":')
        self.run_index()
        self.assertEqual(len(ingest.load_state()["documents"]), 1)
        self.assertFalse(ingest.STATE_FILE.with_suffix(".journal").exists())

    def test_duplicate_aliases_and_current_historical_copies_remain_accessible(self):
        text = "# Telescope\nQdrant telescope monitoring configuration."
        first = self.write("a.md", text)
        self.write("b.md", text)
        self.write("old/a.md", text)
        self.run_index()
        self.assertEqual(len(ingest.load_state()["documents"]), 2)
        item = retrieval.search("telescope")["results"][0]
        self.assertEqual(item["source"], "a.md")
        self.assertEqual(item["duplicate_sources"], ["b.md"])
        first.unlink()
        self.run_index()
        self.assertEqual(retrieval.search("telescope")["results"][0]["source"], "b.md")

    def test_parsers_preserve_headings_tables_short_content_and_reject_empty_documents(
        self,
    ):
        markdown = self.write(
            "short.md", "# Port\n443\n```sh\n# a comment, not a heading\n```"
        )
        records = ingest.document_records(markdown)
        self.assertEqual(len(records), 1)
        self.assertIn("443", records[0]["text"])
        self.assertEqual(records[0]["section"], "Port")
        encoded = self.root / "utf16.txt"
        encoded.write_text(
            "Configurazione: porta 8443, rete osservatorio.", encoding="utf-16"
        )
        self.assertIn("osservatorio", ingest.document_records(encoded)[0]["text"])
        html = self.write(
            "page.html",
            "<html><body><h1>Network</h1><p>VLAN 42</p><h2>Database</h2><p>Qdrant</p><script>ignored()</script></body></html>",
        )
        sections = ingest.parse_document(html)
        self.assertEqual([s["section"] for s in sections], ["Network", "Database"])
        self.assertNotIn("ignored", str(sections))
        word = Document()
        word.add_heading("Network ports", 1)
        word.add_table(rows=1, cols=2).rows[0].cells[0].text = "HTTPS 8443"
        path = self.root / "table.docx"
        word.save(path)
        self.assertIn("8443", ingest.document_records(path)[0]["text"])
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_table(
            1, 2, Inches(1), Inches(1), Inches(5), Inches(1)
        ).table.cell(0, 0).text = "VLAN 42"
        slide_path = self.root / "table.pptx"
        deck.save(slide_path)
        self.assertIn("VLAN 42", ingest.document_records(slide_path)[0]["text"])
        self.write("empty.md", "   ")
        with self.assertRaisesRegex(ValueError, "No extractable text"):
            ingest.document_records(self.root / "empty.md")

    def test_redaction_precedes_chunking_and_output_recurses(self):
        secret = "synthetic-value-" * 30
        self.write(
            "note.md", "Service configuration\npassword: " + secret + "\nport: 8443"
        )
        with (
            patch.object(ingest, "CHUNK_SIZE", 100),
            patch.object(ingest, "CHUNK_OVERLAP", 20),
        ):
            self.run_index()
        self.assertNotIn(secret[:30], json.dumps([p.payload for p in self.points()]))
        self.assertNotIn(
            "synthetic-value", json.dumps(retrieval.search("configuration"))
        )
        with self.assertRaisesRegex(ValueError, "Redacted"):
            retrieval.search("[REDACTED]")
        self.assertIn(
            "[REDACTED]",
            retrieval.sanitize({"nested": [{"section": "password: abc"}]})["nested"][0][
                "section"
            ],
        )

    def test_batches_and_token_limits_bound_embedding_work(self):
        path = self.write("note.md", "界" * 3000)
        tokenizer = types.SimpleNamespace(encode=lambda text, **kwargs: list(text))
        model = FakeModel()
        model.tokenizer, model.max_seq_length = tokenizer, 150
        records = ingest.document_records(path, model)
        self.assertTrue(all(len(ingest.embedding_input(r)) <= 150 for r in records))
        self.assertGreater(len(records), 20)
        with (
            patch.object(ingest, "CHUNK_SIZE", 100),
            patch.object(ingest, "CHUNK_OVERLAP", 20),
            patch.object(ingest, "EMBED_BATCH_SIZE", 3),
            patch.object(FakeModel, "encode", wraps=FakeModel().encode) as encode,
        ):
            self.run_index()
        self.assertGreater(encode.call_count, 1)
        self.assertTrue(all(len(call.args[0]) <= 3 for call in encode.call_args_list))

    def test_large_document_does_not_crowd_other_sources_out_of_candidates(self):
        self.write(
            "large.md",
            "\n".join(
                f"# Part {n}\nQdrant telescope configuration {n}." for n in range(100)
            ),
        )
        self.write(
            "small.md", "Qdrant telescope configuration for a second independent site."
        )
        self.run_index()
        self.assertEqual(
            {r["source"] for r in retrieval.search("Qdrant telescope")["results"]},
            {"large.md", "small.md"},
        )

    def test_deleted_documents_are_pruned_even_if_another_replacement_fails(self):
        removed = self.write("removed.md", "A document that will be removed.")
        broken = self.write("broken.md", "Last usable telescope configuration.")
        self.run_index()
        removed.unlink()
        broken.write_text("   ", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_index()
        self.assertEqual(
            [r["source"] for r in retrieval.search("telescope")["results"]],
            ["broken.md"],
        )

    def test_duplicate_sources_can_be_used_as_exact_paths_and_folder_scopes(self):
        text = "Qdrant telemetry settings for telescope control."
        self.write("alpha/note.md", text)
        self.write("beta/note.md", text)
        self.run_index()
        self.assertTrue(retrieval.search("telemetry", source="beta/note.md")["results"])
        self.assertTrue(retrieval.browse_documents("beta")["documents"])
        (self.root / "beta/note.md").unlink()
        with patch.object(FakeModel, "encode") as encode:
            self.run_index()
            encode.assert_not_called()
        self.assertFalse(
            retrieval.search("telemetry", source="beta/note.md")["results"]
        )

    def test_scan_errors_abort_before_pruning_and_simultaneous_writers_are_rejected(
        self,
    ):
        self.write("good.md", "Usable telescope configuration.")
        self.run_index()
        original_ids = {p.id for p in self.points()}
        with patch.object(
            ingest.os, "walk", side_effect=PermissionError("unreadable directory")
        ):
            with self.assertRaises(PermissionError):
                self.run_index()
        self.assertEqual({p.id for p in self.points()}, original_ids)
        with ingest.ingestion_lock():
            with self.assertRaisesRegex(RuntimeError, "Another ingestion"):
                self.run_index()

    def test_missing_checkpoint_recovers_remote_identifiers_and_removes_old_sources(
        self,
    ):
        removed = self.write(
            "removed.md", "A document that will be removed after checkpoint loss."
        )
        self.write("retained.md", "A retained telescope configuration.")
        self.run_index()
        removed.unlink()
        ingest.STATE_FILE.unlink()
        self.run_index()
        self.assertEqual({p.payload["source"] for p in self.points()}, {"retained.md"})

    def test_query_fetches_bounded_candidates_from_a_synthetic_corpus(self):
        from index_schema import ensure_search_schema, source_metadata

        size = int(os.getenv("KNOWLEDGE_SCALE_DOCUMENTS", "500"))
        self.client.create_collection(
            ingest.COLLECTION_NAME,
            models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        ensure_search_schema(self.client, ingest.COLLECTION_NAME)
        for offset in range(0, size, 200):
            points = []
            for number in range(offset, min(size, offset + 200)):
                source = f"docs/service-{number:06}.md"
                text = f"Telemetry service {number:06} uses port {10000 + number}."
                payload = {
                    **source_metadata(source),
                    "document_id": f"{number:064x}",
                    "source": source,
                    "filename": source.split("/")[-1],
                    "extension": ".md",
                    "section_index": 0,
                    "chunk_index": 0,
                    "document_order": 0,
                    "total_chunks": 1,
                    "ready": True,
                    "embedding_model": ingest.MODEL_NAME,
                    "text": text,
                }
                points.append(
                    models.PointStruct(
                        id=number,
                        vector={
                            "": [1.0, 0.0, 0.0],
                            "lexical": sparse_vector(text, source),
                        },
                        payload=payload,
                    )
                )
            self.client.upsert(ingest.COLLECTION_NAME, points)
        with (
            patch.object(self.client, "scroll", wraps=self.client.scroll) as scroll,
            patch.object(
                self.client,
                "query_points_groups",
                wraps=self.client.query_points_groups,
            ) as query,
        ):
            result = retrieval.search(f"service-{size - 1:06}", limit=8)
            self.assertEqual(
                result["results"][0]["source"], f"docs/service-{size - 1:06}.md"
            )
            self.assertLessEqual(result["candidate_chunks"], 160)
            self.assertEqual(query.call_count, 2)
            self.assertLessEqual(scroll.call_count, 2)
            self.assertTrue(
                all(call.kwargs["limit"] <= 4 for call in scroll.call_args_list)
            )
            self.assertTrue(
                all(call.kwargs["group_size"] == 2 for call in query.call_args_list)
            )

    def test_sparse_identifiers_unicode_and_path_validation(self):
        self.assertIn("vm", tokens("VM IP VLAN nginx.conf 測定"))
        self.assertIn("nginx", tokens("nginx.conf"))
        self.assertTrue(sparse_vector("測定").indices)
        for path in ("../secret", "/etc", "a/../b", "a\\b"):
            with self.assertRaises(ValueError):
                retrieval.browse_documents(path)
        self.assertNotEqual(
            sparse_vector("port 443").indices, sparse_vector("port 8443").indices
        )


if __name__ == "__main__":
    unittest.main()
