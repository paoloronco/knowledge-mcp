"""Regression coverage for indexing issue classification and partial-success runs."""

import contextlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "mcp"))
import webui as app


class IndexIssuesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for item in [
            patch.object(app, "DATA", self.data),
            patch.object(app, "SOURCE", self.data / "documents"),
            patch.object(app, "MANAGED_SOURCE", self.data / "documents"),
            patch.object(app, "HOST_SOURCE", self.data / "host-documents"),
            patch.object(app, "CONFIG", self.data / "config.json"),
            patch.object(app, "POLICY", self.data / "index-policy.yaml"),
            patch.object(app, "AGENT_MANIFEST", self.data / "agent-manifest.json"),
            patch.object(app, "AUTO_AGENT_CONFIG", None),
            patch.dict(app.os.environ, {"INGESTION_BASE_DIR": str(self.data / "ingestion")}),
        ]:
            self.stack.enter_context(item)
        self.controller = app.Controller()
        self.addCleanup(self.controller.close)

    def test_legacy_no_text_errors_are_reclassified_as_skipped(self):
        events = [
            {
                "at": "2026-11-16T09:00:00+00:00",
                "stage": "indexing",
                "source": "Certificati/corso.pdf",
                "error_type": "ValueError",
                "message": "No extractable text; scanned PDFs require OCR before indexing",
            },
            {
                "at": "2026-11-16T09:00:01+00:00",
                "stage": "indexing",
                "source": "Documenti/broken.docx",
                "error_type": "PackageNotFoundError",
                "message": "Invalid package",
            },
        ]
        (self.data / "index-errors.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )
        errors, skipped, origin = self.controller.index_issues()
        self.assertEqual(origin, "current")
        self.assertEqual([item["source"] for item in errors], ["Documenti/broken.docx"])
        self.assertEqual([item["source"] for item in skipped], ["Certificati/corso.pdf"])
        self.assertEqual(skipped[0]["error_type"], "No extractable text")
        self.assertTrue(skipped[0]["can_ignore"])

    def test_skipped_file_entries_are_normalised(self):
        (self.data / "index-skipped.jsonl").write_text(
            json.dumps(
                {
                    "at": "2026-11-16T09:00:00+00:00",
                    "stage": "indexing",
                    "source": "Documenti/AIRE/scansione.pdf",
                    "reason": "No extractable text",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        skipped = self.controller.index_skipped()
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["source"], "Documenti/AIRE/scansione.pdf")
        self.assertEqual(skipped[0]["message"], "No extractable text")
        self.assertTrue(skipped[0]["can_ignore"])
        self.assertFalse(skipped[0]["ignored"])

    def test_partial_success_still_activates_the_model_and_completes_setup(self):
        (self.data / "index-summary.json").write_text(
            json.dumps(
                {
                    "dry_run": False,
                    "indexed": 3,
                    "already_indexed": 0,
                    "skipped": 0,
                    "failed": 1,
                    "chunks": 9,
                    "finished_at": 1,
                }
            ),
            encoding="utf-8",
        )
        self.controller.config["active_embedding_model"] = "e5-small"
        self.controller.config["embedding_model"] = "bge-m3"
        self.controller.config["mcp_enabled"] = False
        self.controller.config["setup_step"] = 6
        self.controller.lock = threading.RLock()
        self.controller._finish_index(Mock(wait=lambda: 1), False, "bge-m3")
        self.assertEqual(self.controller.config["active_embedding_model"], "bge-m3")
        self.assertEqual(self.controller.config["setup_step"], 7)
        self.assertEqual(self.controller.last_result["exit_code"], 1)

    def test_total_failure_does_not_activate_the_model(self):
        (self.data / "index-summary.json").write_text(
            json.dumps({"dry_run": False, "indexed": 0, "failed": 4}),
            encoding="utf-8",
        )
        self.controller.config["active_embedding_model"] = "e5-small"
        self.controller.config["embedding_model"] = "bge-m3"
        self.controller.config["mcp_enabled"] = False
        self.controller.lock = threading.RLock()
        self.controller._finish_index(Mock(wait=lambda: 1), False, "bge-m3")
        self.assertEqual(self.controller.config["active_embedding_model"], "e5-small")


if __name__ == "__main__":
    unittest.main()
