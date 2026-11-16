"""Regression coverage for the dashboard's plain-language service readiness."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "mcp"))
import webui

BASE = {
    "config": {
        "onboarding_complete": True,
        "source_mode": "container",
        "active_embedding_model": "e5-small",
    },
    "qdrant_ready": True,
    "mcp_running": True,
    "source_ready": True,
    "index_running": False,
    "agent_syncing": False,
    "scan_running": False,
    "maintenance": False,
    "uptime_seconds": 500,
    "last_result": None,
    "index_progress": None,
    "agent_progress": None,
    "qdrant_managed": True,
}


def readiness(**overrides):
    status = {**BASE, **overrides}
    return webui.Controller._readiness(None, status)


class ReadinessTest(unittest.TestCase):
    def test_ready_state(self):
        result = readiness()
        self.assertEqual(result["phase"], "ready")
        self.assertTrue(result["ready"])
        self.assertEqual(result["tone"], "ready")
        self.assertIsNone(result["action"])

    def test_startup_and_outage_are_distinguished_by_uptime(self):
        starting = readiness(qdrant_ready=False, uptime_seconds=10)
        self.assertEqual(starting["phase"], "starting")
        self.assertIn("deploy or restart", starting["detail"])
        self.assertEqual(starting["action"]["kind"], "start_qdrant")
        degraded = readiness(qdrant_ready=False, uptime_seconds=900)
        self.assertEqual(degraded["phase"], "degraded")
        self.assertEqual(degraded["tone"], "warning")

    def test_work_in_progress_phases(self):
        indexing = readiness(
            index_running=True,
            index_progress={"stage": "indexing", "completed": 10, "total": 100},
        )
        self.assertEqual(indexing["phase"], "indexing")
        self.assertIn("10 of 100", indexing["detail"])
        self.assertTrue(indexing["spinner"])
        syncing = readiness(
            agent_syncing=True,
            agent_progress={"phase": "transferring", "completed": 2, "total": 8, "checked": 20},
        )
        self.assertEqual(syncing["phase"], "syncing")
        self.assertIn("2 of 8", syncing["detail"])

    def test_setup_and_stopped_services_are_explained(self):
        setup = readiness(config={**BASE["config"], "onboarding_complete": False})
        self.assertEqual(setup["phase"], "setup")
        self.assertIn("MCP server is not available", setup["detail"])
        stopped = readiness(mcp_running=False)
        self.assertEqual(stopped["phase"], "mcp_stopped")
        self.assertEqual(stopped["action"]["kind"], "start_mcp")
        missing = readiness(source_ready=False)
        self.assertEqual(missing["phase"], "documents_missing")
        maintenance = readiness(maintenance=True)
        self.assertEqual(maintenance["phase"], "maintenance")

    def test_steps_reflect_each_service(self):
        result = readiness(qdrant_ready=False, source_ready=False, mcp_running=False)
        self.assertEqual(
            [(x["label"], x["state"]) for x in result["steps"]],
            [
                ("Database", "active"),
                ("Documents", "pending"),
                ("Search index", "done"),
                ("MCP server", "pending"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
