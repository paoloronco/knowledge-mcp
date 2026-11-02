import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT))
import host_agent


class DockerHostAccessTest(unittest.TestCase):
    def setUp(self):
        self.policy = yaml.safe_load((ROOT / "mcp" / "index-policy.yaml").read_text(encoding="utf-8"))

    def test_selects_any_host_folder_and_applies_policy_before_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            folder = mount / "mnt" / "my-documents"
            folder.mkdir(parents=True)
            (folder / "example.md").write_text("Example document", encoding="utf-8")
            (folder / "~$draft.docx").write_bytes(b"Office lock")
            (folder / "~$slides.pptx").write_bytes(b"Office lock")
            (folder / "cache").mkdir()
            (folder / "cache" / "excluded.md").write_text("Excluded fixture", encoding="utf-8")
            resolved, files = host_agent.inventory("/mnt/my-documents", self.policy, mount)
            self.assertEqual(resolved, folder.resolve())
            self.assertEqual(list(files), ["example.md"])
            with self.assertRaisesRegex(ValueError, "/mnt/not-present"):
                host_agent.inventory("/mnt/not-present", self.policy, mount)
            with self.assertRaisesRegex(ValueError, "excluded"):
                host_agent.inventory("/mnt/my-documents/cache", self.policy, mount)
            with self.assertRaisesRegex(ValueError, "host system directory"):
                host_agent.resolve_host_folder("/proc/1/fd", mount)

    @unittest.skipUnless(os.name == "posix", "Requires Linux symlinks")
    def test_absolute_and_relative_host_symlinks_stay_inside_host_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            target = mount / "srv" / "documents"
            target.mkdir(parents=True)
            (mount / "mnt").mkdir()
            (mount / "mnt" / "absolute").symlink_to("/srv/documents")
            (mount / "mnt" / "relative").symlink_to("../srv/documents")
            (target / "note.md").write_text("Example", encoding="utf-8")
            for value in ("/mnt/absolute", "/mnt/relative"):
                resolved, files = host_agent.inventory(value, self.policy, mount)
                self.assertEqual(resolved, target)
                self.assertEqual(list(files), ["note.md"])
            restricted = mount / "srv" / "cache"
            restricted.mkdir()
            (mount / "mnt" / "private-alias").symlink_to("/srv/cache")
            with self.assertRaisesRegex(ValueError, "excluded"):
                host_agent.inventory("/mnt/private-alias", self.policy, mount)
            (mount / "mnt" / "loop").symlink_to("/mnt/loop")
            with self.assertRaisesRegex(ValueError, "Too many symlinks"):
                host_agent.resolve_host_folder("/mnt/loop", mount)

    def test_agent_does_not_sync_while_indexing_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text('{"url":"http://127.0.0.1:8080","token":"test-token"}', encoding="utf-8")
            task = {"revision": 1, "sync_request": 0, "policy": self.policy, "source_mode": "host_agent", "host_root": "/mnt/documents", "index_running": True}
            with patch.object(host_agent, "request", return_value=task), patch.object(host_agent, "sync") as sync:
                host_agent.run(config, once=True, host_mount=Path("/host"))
            sync.assert_not_called()

    def test_heartbeat_keeps_host_access_connected_during_a_long_scan(self):
        done = Mock()
        done.wait.side_effect = [False, False, True]
        with patch.object(host_agent, "request") as request:
            host_agent.heartbeat("http://127.0.0.1:8080", "test-token", done)
        self.assertEqual(request.call_count, 2)

    def test_inventory_reuses_only_unchanged_checksums_and_drops_deleted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = root / "note.md"
            document.write_text("First telemetry settings", encoding="utf-8")
            cache = {}
            with patch.object(host_agent, "file_hash", wraps=host_agent.file_hash) as hashes:
                first = host_agent.inventory(str(root), self.policy, hash_cache=cache)[1]
                second = host_agent.inventory(str(root), self.policy, hash_cache=cache)[1]
                self.assertEqual(first, second)
                self.assertEqual(hashes.call_count, 1)
                document.write_text("Changed telemetry settings", encoding="utf-8")
                third = host_agent.inventory(str(root), self.policy, hash_cache=cache)[1]
                self.assertNotEqual(third, first)
                self.assertEqual(hashes.call_count, 2)
                document.unlink()
                host_agent.inventory(str(root), self.policy, hash_cache=cache)
                self.assertEqual(cache, {})


if __name__ == "__main__":
    unittest.main()
