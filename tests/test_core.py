import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from qdrant_client import QdrantClient
import yaml

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "ingestion"))
sys.path.insert(0, str(ROOT / "mcp"))
import ingest
import retrieval


class FakeVector:
    def tolist(self):
        return [1.0, 0.0, 0.0]


class FakeModel:
    def __init__(self, *args, **kwargs):
        pass

    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, inputs, **kwargs):
        return [FakeVector() for _ in inputs]


class CoreFlowTest(unittest.TestCase):
    def test_partial_index_can_retry_after_ignoring_bad_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "documents"
            root.mkdir()
            (root / "good.md").write_text("A valid synthetic document about the observatory. " * 4, encoding="utf-8")
            (root / "bad.docx").write_bytes(b"This is not a DOCX package")
            policy_file = base / "policy.yaml"
            policy_file.write_text((ROOT / "mcp" / "index-policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            state_file = base / "state.json"
            errors_file = base / "run-errors.jsonl"
            stages_file = base / "stages.log"
            client = QdrantClient(":memory:")
            fake_transformers = types.SimpleNamespace(SentenceTransformer=FakeModel)
            with (
                patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(root)}),
                patch.dict(sys.modules, {"sentence_transformers": fake_transformers}),
                patch.object(ingest, "QdrantClient", return_value=client),
                patch.object(ingest, "embedding_device", return_value="cpu"),
                patch.object(ingest, "POLICY_FILE", policy_file),
                patch.object(ingest, "STATE_FILE", state_file),
                patch.object(ingest, "ERROR_LOG", base / "errors.log"),
                patch.object(ingest, "RUN_ERRORS_FILE", errors_file),
                patch.object(ingest, "STAGES_FILE", stages_file),
                patch.object(ingest, "PROGRESS_FILE", base / "progress.json"),
                patch.object(sys, "argv", ["ingest.py"]),
            ):
                with self.assertRaises(SystemExit):
                    ingest.main()
                self.assertEqual(client.count(ingest.COLLECTION_NAME).count, 1)
                self.assertEqual(len(json.loads(state_file.read_text())["documents"]), 1)
                self.assertEqual(json.loads(errors_file.read_text())["source"], "bad.docx")
                self.assertIn("failed", stages_file.read_text())

                policy = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
                policy["exclude_files"] = ["bad.docx"]
                policy_file.write_text(yaml.safe_dump(policy), encoding="utf-8")
                errors_file.write_text("", encoding="utf-8")
                ingest.main()
                self.assertEqual(client.count(ingest.COLLECTION_NAME).count, 1)
                self.assertEqual(len(json.loads(state_file.read_text())["documents"]), 1)
                self.assertEqual(errors_file.read_text(), "")
                self.assertIn("complete", stages_file.read_text())

    def test_index_event_files_record_progress_and_relative_document_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "documents"
            root.mkdir()
            source = root / "~$draft.docx"
            with (
                patch.object(ingest, "PROGRESS_FILE", base / "progress.json"),
                patch.object(ingest, "STAGES_FILE", base / "stages.log"),
                patch.object(ingest, "RUN_ERRORS_FILE", base / "run-errors.jsonl"),
                patch.object(ingest, "ERROR_LOG", base / "errors.log"),
                patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(root)}),
            ):
                ingest.write_progress("indexing", 3, 10, force=True)
                ingest.log_error(source, ValueError("Not a valid document"), "indexing")
            self.assertEqual(json.loads((base / "progress.json").read_text())["completed"], 3)
            self.assertIn("indexing\t3/10", (base / "stages.log").read_text())
            event = json.loads((base / "run-errors.jsonl").read_text())
            self.assertEqual(event["source"], "~$draft.docx")
            self.assertEqual(event["error_type"], "ValueError")

    def test_default_directories_remain_excluded_with_an_older_policy(self):
        names = ("coverage", "cache", ".cache", "vendor", ".stversions")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "documents"
            root.mkdir()
            old_policy = (ROOT / "mcp" / "index-policy.yaml").read_text(encoding="utf-8")
            for name in names:
                old_policy = old_policy.replace(f"  - {name}\n", "")
            policy_file = Path(tmp) / "old-policy.yaml"
            policy_file.write_text(old_policy, encoding="utf-8")
            with patch.object(ingest, "POLICY_FILE", policy_file), patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(root)}):
                policy = ingest.load_policy()
            self.assertTrue({name.casefold() for name in names} <= {name.casefold() for name in policy["exclude_directories"]})
            for name in names:
                folder = root / "notes" / name.upper()
                folder.mkdir(parents=True, exist_ok=True)
                document = folder / "private.md"
                document.write_text("Sensitive", encoding="utf-8")
                self.assertFalse(ingest.is_candidate(document, root, policy), name)
                self.assertFalse(retrieval.eligible_for_query(document.relative_to(root).as_posix(), "current"), name)

    def test_selected_folders_and_file_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("team-a", "team-b", "cache"):
                (root / name).mkdir()
                (root / name / "note.md").write_text("Visible content", encoding="utf-8")
            (root / "team-a" / "skip.md").write_text("Excluded content", encoding="utf-8")
            (root / "team-a" / "~$draft.docx").write_bytes(b"Office lock, not a document")
            (root / "team-a" / "~$slides.pptx").write_bytes(b"Office lock, not slides")
            with patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(root)}):
                policy = ingest.load_policy()
            policy["exclude_files"] = ["skip.md"]
            with patch.dict(os.environ, {"INDEX_SOURCE_PATHS": json.dumps(["team-a", "team-b", "cache"])}):
                paths = ingest.discover_documents(policy)
            self.assertEqual({p.relative_to(root).as_posix() for p in paths}, {"team-a/note.md", "team-b/note.md"})

    def test_expansion_stays_in_section(self):
        selected = {"document_id": "a" * 64, "section_index": 0, "document_order": 0, "text": "relevant"}
        chunks = [{**selected, "page": 1, "section": "Relevant"},
                  {**selected, "section_index": 1, "document_order": 1, "text": "unrelated"}]
        with patch.object(retrieval, "read_document", return_value={"chunks": chunks}):
            expanded = retrieval.expand_result_context({"payload": selected})
        self.assertIn("relevant", expanded["text"])
        self.assertNotIn("unrelated", expanded["text"])

    def test_ingest_update_delete_and_retrieval_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            source = root / "note.md"
            source.write_text("Qdrant configuration. " * 10 + "\npassword: supersecret\n", encoding="utf-8")
            excluded = root / "CACHE"
            excluded.mkdir()
            (excluded / "private.md").write_text("Must stay out of Qdrant. " * 10, encoding="utf-8")
            state_file = Path(tmp) / "state.json"
            client = QdrantClient(":memory:")
            fake_transformers = types.SimpleNamespace(SentenceTransformer=FakeModel)
            with (
                patch.dict(os.environ, {"KNOWLEDGE_ROOT": str(root)}),
                patch.dict(sys.modules, {"sentence_transformers": fake_transformers}),
                patch.object(ingest, "QdrantClient", return_value=client),
                patch.object(ingest, "STATE_FILE", state_file),
                patch.object(ingest, "ERROR_LOG", Path(tmp) / "errors.log"),
                patch.object(sys, "argv", ["ingest.py"]),
            ):
                with patch.object(sys, "argv", ["ingest.py", "--dry-run", "--limit", "1"]):
                    ingest.main()
                self.assertFalse(client.get_collections().collections)
                ingest.main()
                self.assertGreater(client.count(ingest.COLLECTION_NAME).count, 0)
                self.assertEqual(len(json.loads(state_file.read_text(encoding="utf-8"))["documents"]), 1)
                unchanged_state = state_file.read_bytes()
                ingest.main()
                self.assertEqual(state_file.read_bytes(), unchanged_state)

                baseline = client.count(ingest.COLLECTION_NAME).count
                client.upsert(ingest.COLLECTION_NAME, [ingest.PointStruct(id=0, vector=[1.0, 0.0, 0.0], payload={"document_id": "stale"})])
                client.create_collection('other_model', vectors_config=ingest.VectorParams(size=3, distance=ingest.Distance.COSINE))
                client.upsert('other_model', [ingest.PointStruct(id=1, vector=[1.0, 0.0, 0.0])])
                with patch.object(sys, 'argv', ['ingest.py', '--rebuild', '--limit', '1']), self.assertRaises(SystemExit):
                    ingest.main()
                self.assertEqual(client.count(ingest.COLLECTION_NAME).count, baseline + 1)
                with patch.object(sys, 'argv', ['ingest.py', '--rebuild']), patch.object(FakeModel, 'encode', side_effect=FakeModel().encode) as encode:
                    ingest.main()
                    encode.assert_called_once()
                self.assertEqual(client.count(ingest.COLLECTION_NAME).count, baseline)
                self.assertEqual(client.count('other_model').count, 1)
                self.assertFalse(client.retrieve(ingest.COLLECTION_NAME, ids=[0]))

                retrieval.client = client
                retrieval.model = FakeModel()
                result = retrieval.search("Qdrant configuration")
                self.assertTrue(result["results"])
                self.assertIn("[REDACTED]", result["results"][0]["text"])
                self.assertFalse(retrieval.eligible_for_query(r"Projects\CACHE\secret.md", "current"))

                source.write_text("New Qdrant configuration. " * 10, encoding="utf-8")
                ingest.main()
                self.assertEqual(len(json.loads(state_file.read_text(encoding="utf-8"))["documents"]), 1)
                source.unlink()
                with patch.object(sys, 'argv', ['ingest.py', '--rebuild']), self.assertRaises(SystemExit):
                    ingest.main()
                self.assertGreater(client.count(ingest.COLLECTION_NAME).count, 0)
                with self.assertRaises(SystemExit):
                    ingest.main()
                self.assertGreater(client.count(ingest.COLLECTION_NAME).count, 0)
                with patch.object(sys, "argv", ["ingest.py", "--allow-empty"]):
                    ingest.main()
                self.assertEqual(client.count(ingest.COLLECTION_NAME).count, 0)


if __name__ == "__main__":
    unittest.main()
