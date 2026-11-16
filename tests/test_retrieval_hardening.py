"""Regression coverage for retrieval hardening and path-scope compatibility."""

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

import yaml
from qdrant_client import QdrantClient, models

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "mcp"))
import host_sync
import ingest
import retrieval
from index_schema import source_metadata
from test_core import FakeModel


class RetrievalHardeningTest(unittest.TestCase):
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
            patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(self.root), "INDEX_SOURCE_PATHS": '[""]'}),
            patch.dict(sys.modules, {"sentence_transformers": types.SimpleNamespace(SentenceTransformer=FakeModel)}),
            patch.object(ingest, "QdrantClient", return_value=self.client),
            patch.object(ingest, "embedding_device", return_value="cpu"),
            patch.object(ingest, "POLICY_FILE", self.policy_file),
            patch.object(retrieval, "POLICY_FILE", self.policy_file),
            patch.object(ingest, "STATE_FILE", self.base / "state.json"),
            patch.object(ingest, "ERROR_LOG", self.base / "errors.log"),
            patch.object(ingest, "RUN_ERRORS_FILE", None),
            patch.object(ingest, "SKIPPED_FILE", self.base / "skipped.jsonl"),
            patch.object(ingest, "SUMMARY_FILE", self.base / "summary.json"),
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

    def policy(self):
        return yaml.safe_load(self.policy_file.read_text(encoding="utf-8"))

    def test_nested_restricted_directories_are_excluded_everywhere(self):
        secret = self.write("alpha/secrets/db.md", "SECRETVALUE telescope credentials")
        self.write("alpha/docs/guide.md", "telescope handbook")
        policy = self.policy()
        self.assertFalse(ingest.is_candidate(secret, self.root, policy))
        self.assertFalse(retrieval.source_allowed("alpha/secrets/db.md", policy))
        self.assertFalse(retrieval.source_allowed("alpha/credentials/key.md", policy))
        self.assertFalse(retrieval.source_allowed("alpha/.env/creds.md", policy))
        self.assertFalse(host_sync.allowed_file("alpha/secrets/db.md", 100, policy))
        self.assertTrue(retrieval.source_allowed("alpha/docs/guide.md", policy))
        self.run_index()
        self.assertNotIn(
            "SECRETVALUE", str(retrieval.search("SECRETVALUE")["results"])
        )
        self.assertEqual(
            {r["source"] for r in retrieval.search("telescope")["results"]},
            {"alpha/docs/guide.md"},
        )
        self.assertEqual(
            [d["source"] for d in retrieval.browse_documents(limit=100)["documents"]],
            ["alpha/docs/guide.md"],
        )

    def test_path_scopes_and_exact_sources_are_case_insensitive(self):
        self.write("Docs/Guide.MD", "# Network\nVLAN 42 telescope configuration.")
        self.run_index()
        for kwargs in (
            {"path_prefix": "docs"},
            {"path_prefix": "DOCS"},
            {"source": "docs/guide.md"},
            {"source": "Docs/Guide.MD"},
        ):
            result = retrieval.search("telescope", **kwargs)
            self.assertEqual(
                [r["source"] for r in result["results"]],
                ["Docs/Guide.MD"],
                kwargs,
            )

    def test_legacy_payload_without_casefold_fields_keeps_exact_matching(self):
        self.client.create_collection(
            ingest.COLLECTION_NAME,
            models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        identity = "e" * 64
        source = "notes/network.md"
        payload = {
            **source_metadata(source),
            "document_id": identity,
            "source": source,
            "filename": "network.md",
            "extension": ".md",
            "section_index": 0,
            "chunk_index": 0,
            "document_order": 0,
            "total_chunks": 1,
            "ready": True,
            "embedding_model": ingest.MODEL_NAME,
            "text": "Il telescopio usa la VLAN 42.",
        }
        payload.pop("source_paths_casefold", None)
        payload.pop("source_prefixes_casefold", None)
        self.client.upsert(
            ingest.COLLECTION_NAME,
            [models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload=payload)],
        )
        result = retrieval.search("VLAN", source="notes/network.md")
        self.assertTrue(result["results"])
        self.assertEqual(result["results"][0]["source"], source)

    def test_embedding_failure_is_reported_instead_of_raising(self):
        self.write("note.md", "Qdrant telescope configuration.")
        self.run_index()

        class BrokenModel:
            def encode(self, *args, **kwargs):
                raise RuntimeError("model exploded")

        with patch.object(retrieval, "model", BrokenModel()):
            result = retrieval.search("telescope")
        self.assertEqual(result["backend"], "unavailable")
        self.assertEqual(result["reason"], "RuntimeError")
        self.assertEqual(result["results"], [])

    def test_scanned_pdfs_are_skipped_once_and_never_reparsed(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with (self.root / "scanned.pdf").open("wb") as output:
            writer.write(output)
        self.write("good.md", "Qdrant telescope configuration notes.")

        with patch.object(ingest, "parse_pdf", wraps=ingest.parse_pdf) as parse:
            self.run_index()
        self.assertEqual(parse.call_count, 1)
        state = ingest.load_state()["documents"]
        self.assertEqual(sum(1 for entry in state.values() if entry.get("empty")), 1)
        self.assertFalse(any(entry.get("pending") for entry in state.values()))
        self.assertEqual(
            len((self.base / "skipped.jsonl").read_text(encoding="utf-8").splitlines()),
            1,
        )
        summary = json.loads((self.base / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["indexed"], 1)

        with patch.object(ingest, "parse_pdf", wraps=ingest.parse_pdf) as reparsed:
            self.run_index()
        self.assertEqual(reparsed.call_count, 0)
        self.assertEqual(
            [r["source"] for r in retrieval.search("telescope")["results"]],
            ["good.md"],
        )

    def test_incremental_update_clears_legacy_flag_with_skipped_docs_and_orphan_points(
        self,
    ):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        scanned = self.root / "scanned.pdf"
        with scanned.open("wb") as output:
            writer.write(output)
        self.write("good.md", "Qdrant telescope VLAN 42 configuration.")

        self.client.create_collection(
            ingest.COLLECTION_NAME,
            models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        old_scanned_id = ingest.sha256_file(scanned)
        self.client.upsert(
            ingest.COLLECTION_NAME,
            [
                models.PointStruct(
                    id=101,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "document_id": old_scanned_id,
                        "source": "scanned.pdf",
                        "text": "legacy v1 scanned entry",
                    },
                ),
                models.PointStruct(
                    id=102,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "document_id": "f" * 64,
                        "source": "orphan-not-in-state.md",
                        "text": "orphan legacy v1 point",
                    },
                ),
            ],
        )
        ingest.save_state(
            {
                "documents": {
                    old_scanned_id: {
                        "source": "scanned.pdf",
                        "duplicate_sources": [],
                        "chunks": 1,
                    }
                }
            }
        )
        self.assertTrue(retrieval.search("VLAN")["index_update_required"])

        self.run_index()
        self.assertEqual(retrieval.legacy_chunk_count(), 0)
        result = retrieval.search("VLAN")
        self.assertNotIn("index_update_required", result)
        self.assertEqual([r["source"] for r in result["results"]], ["good.md"])
        self.assertNotIn(old_scanned_id, ingest.load_state()["documents"])

        calls = []
        real_count = self.client.count

        def capture_count(*args, **kwargs):
            calls.append(kwargs.get("exact"))
            return real_count(*args, **kwargs)

        with patch.object(self.client, "count", side_effect=capture_count):
            self.assertEqual(retrieval.legacy_chunk_count(), 0)
        self.assertEqual(calls, [True])

        with self.assertRaises(SystemExit):
            with patch.object(
                ingest, "parse_document", side_effect=RuntimeError("unreadable file")
            ):
                self.write("broken.md", "corrupt")
                self.client.upsert(
                    ingest.COLLECTION_NAME,
                    [
                        models.PointStruct(
                            id=103,
                            vector=[1.0, 0.0, 0.0],
                            payload={"document_id": "a" * 64, "source": "broken.md"},
                        )
                    ],
                )
                self.run_index()
        self.assertTrue(retrieval.search("VLAN")["index_update_required"])

    def test_ingest_and_retrieval_read_the_saved_policy_identically(self):
        custom = self.policy()
        custom["exclude_directories"] = custom["exclude_directories"] + ["team-private"]
        self.policy_file.write_text(yaml.safe_dump(custom), encoding="utf-8")
        retrieval.invalidate_policy_cache()
        self.assertEqual(
            ingest.load_policy()["exclude_directories"],
            retrieval.load_policy()["exclude_directories"],
        )
        self.assertIn("team-private", retrieval.load_policy()["exclude_directories"])


if __name__ == "__main__":
    unittest.main()
