import hashlib
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "mcp"))
import webui as app
import policy_defaults
from embedding_models import MODELS, document_text, query_text
from embedding_device import embedding_device
from gpu_probe import parse_devices
sys.path.insert(0, str(ROOT))
import host_agent


class EmbeddingModelTest(unittest.TestCase):
    def test_embedding_device_uses_cuda_when_available_and_allows_cpu_override(self):
        fake_torch = Mock()
        fake_torch.cuda.is_available.return_value = True
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.dict(app.os.environ, {"EMBEDDING_DEVICE": "auto"}):
            self.assertEqual(embedding_device(), "cuda")
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.dict(app.os.environ, {"EMBEDDING_DEVICE": "cpu"}):
            self.assertEqual(embedding_device(), "cpu")
        fake_torch.cuda.is_available.return_value = False
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.dict(app.os.environ, {"EMBEDDING_DEVICE": "auto"}):
            self.assertEqual(embedding_device(), "cpu")
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.dict(app.os.environ, {"EMBEDDING_DEVICE": "cuda"}):
            with self.assertRaisesRegex(RuntimeError, "CUDA was requested"):
                embedding_device()

    def test_gpu_probe_parses_device_and_driver(self):
        self.assertEqual(parse_devices("0, NVIDIA GeForce RTX 3070, 617.14, 8192, 4720\n"), [{
            "index": 0, "name": "NVIDIA GeForce RTX 3070", "driver": "617.14",
            "memory_total_mb": 8192, "memory_free_mb": 4720,
        }])

    def test_profiles_keep_collections_distinct_and_format_text(self):
        self.assertEqual(len({item["collection"] for item in MODELS.values()}), len(MODELS))
        self.assertEqual(document_text(MODELS["e5-small"]["model"], "hello"), "passage: hello")
        self.assertEqual(query_text(MODELS["e5-base"]["model"], "hello"), "query: hello")
        self.assertEqual(document_text(MODELS["bge-m3"]["model"], "hello"), "hello")
        self.assertEqual(query_text(MODELS["bge-m3"]["model"], "hello"), "hello")

    def test_selection_preserves_active_index_until_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.dict(app.os.environ, {"INGESTION_BASE_DIR": str(data / "ingestion")}),
            ):
                controller = app.Controller()
                controller.config["active_embedding_model"] = "e5-small"
                controller._save()
                with self.assertRaisesRegex(ValueError, "supported embedding model"):
                    controller.select_embedding_model("unknown")
                controller.select_embedding_model("bge-m3")
                self.assertEqual(controller._env()["DENSE_COLLECTION"], "documents")
                candidate = controller._env("bge-m3", indexing=True)
                self.assertEqual(candidate["DENSE_COLLECTION"], "documents_bge_m3")
                self.assertEqual(candidate["EMBED_BATCH_SIZE"], "4")
                self.assertEqual(candidate["INGESTION_BASE_DIR"], str(data / "ingestion" / "models" / "bge-m3"))
                controller._finish_index(Mock(wait=lambda: 1), False, "bge-m3")
                self.assertEqual(controller.config["active_embedding_model"], "e5-small")
                controller._finish_index(Mock(wait=lambda: 0), False, "bge-m3")
                self.assertEqual(controller.config["active_embedding_model"], "bge-m3")
                self.assertEqual(controller._env()["DENSE_COLLECTION"], "documents_bge_m3")
                controller.close()
                restored = app.Controller()
                self.assertEqual(restored.config["active_embedding_model"], "bge-m3")
                restored.close()

    def test_legacy_custom_environment_index_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "config.json").write_text(json.dumps({"setup_step": 7, "onboarding_complete": True, "mcp_enabled": False}), encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.dict(app.os.environ, {"MODEL_NAME": "example/custom-model", "DENSE_COLLECTION": "custom_vectors", "INGESTION_BASE_DIR": str(data / "ingestion")}),
            ):
                controller = app.Controller()
                self.assertEqual(controller.config["active_embedding_model"], "environment")
                self.assertEqual(controller._env()["DENSE_COLLECTION"], "custom_vectors")
                controller.select_embedding_model("e5-small")
                self.assertEqual(controller._env()["DENSE_COLLECTION"], "custom_vectors")
                candidate = controller._env("e5-small", indexing=True)
                self.assertEqual(candidate["DENSE_COLLECTION"], "documents")
                self.assertEqual(candidate["INGESTION_BASE_DIR"], str(data / "ingestion" / "models" / "e5-small"))
                controller.close()


class AdminBoundaryTest(unittest.TestCase):
    def test_index_error_history_and_authenticated_log_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            root = base / "documents"
            root.mkdir()
            policy_file = data / "index-policy.yaml"
            policy_file.write_text(app.DEFAULT_POLICY.read_text(encoding="utf-8"), encoding="utf-8")
            state = data / "ingestion" / "logs"
            state.mkdir(parents=True)
            (state / "errors.log").write_text(f"2026-10-04T14:19:12+00:00\t{root / '~$draft.docx'}\tPackageNotFoundError('invalid file')\n", encoding="utf-8")
            (data / "ingest.log").write_text("Full indexing output\n", encoding="utf-8")
            (data / "index-stages.log").write_text("2026-10-04T14:00:00+00:00\tindexing\t2/3\n", encoding="utf-8")
            with patch.object(app, "DATA", data), patch.object(app, "POLICY", policy_file), patch.dict(app.os.environ, {"INGESTION_BASE_DIR": str(data / "ingestion")}):
                controller = app.Controller.__new__(app.Controller)
                controller.config = {"source_root": str(root)}
                controller.last_result = {"exit_code": 1}
                errors, origin = controller.index_errors()
                self.assertEqual(origin, "legacy")
                self.assertEqual(errors[0]["source"], "~$draft.docx")
                self.assertTrue(errors[0]["auto_excluded"])
                self.assertFalse(errors[0]["can_ignore"])
                (data / "index-errors.jsonl").write_text(json.dumps({"source": "notes/bad.pdf", "stage": "indexing", "error_type": "ValueError", "message": "Cannot parse"}) + "\n", encoding="utf-8")
                errors, origin = controller.index_errors()
                self.assertEqual(origin, "current")
                self.assertEqual([item["source"] for item in errors], ["notes/bad.pdf"])

                fake = types.SimpleNamespace(password_enabled=lambda: True, sessions={}, lock=threading.RLock(), index_errors=lambda: (errors, origin))
                with patch.object(app, "controller", fake):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}/api/index/log/full"
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(url)
                        self.assertEqual(denied.exception.code, 401)
                        fake.password_enabled = lambda: False
                        with urllib.request.urlopen(url) as response:
                            self.assertIn(b"Full indexing output", response.read())
                            self.assertIn("attachment", response.headers["Content-Disposition"])
                        with urllib.request.urlopen(url.replace("/full", "/stages")) as response:
                            self.assertIn(b"indexing\t2/3", response.read())
                        with urllib.request.urlopen(url.replace("/log/full", "/errors")) as response:
                            self.assertEqual(json.load(response)["errors"][0]["source"], "notes/bad.pdf")
                    finally:
                        server.shutdown()
                        server.server_close()

    def test_host_folder_and_agent_pairing_work_without_dashboard_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "AGENT_AUTH", data / "agent-auth.json"),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.object(app, "AUTO_AGENT_CONFIG", Path(tmp) / "agent" / "agent.json"),
            ):
                controller = app.Controller()
                with patch.object(app, "controller", controller):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}"
                        for route in (*app.SETUP_PATHS, *app.DASHBOARD_PATHS):
                            with urllib.request.urlopen(url + route) as response:
                                self.assertEqual(response.status, 200)
                                self.assertIn(b"Knowledge MCP", response.read())
                        with urllib.request.urlopen(url + "/dashboard/services") as response:
                            self.assertEqual(response.geturl(), url + "/dashboard")
                        with urllib.request.urlopen(url + "/dashboard/access") as response:
                            self.assertEqual(response.geturl(), url + "/dashboard/settings")

                        def post(route, body, cookie=""):
                            headers = {"Content-Type": "application/json", "X-Control-Token": app.TOKEN}
                            if cookie:
                                headers["Cookie"] = cookie
                            request = urllib.request.Request(url + route, data=json.dumps(body).encode(), headers=headers)
                            return urllib.request.urlopen(request)

                        with post("/api/security", {"enabled": False}) as response:
                            self.assertFalse(json.load(response)["enabled"])
                        self.assertEqual(controller.config["setup_step"], 1)
                        self.assertEqual(json.loads(app.CONFIG.read_text())["setup_step"], 1)
                        with post("/api/config", {"document_root": "/mnt/documents", "source_selection": "auto", "folders": [""]}) as response:
                            self.assertEqual(json.load(response)["host_root"], "/mnt/documents")
                        with post("/api/agent/pair", {}) as response:
                            self.assertTrue(controller.agent_authenticated(json.load(response)["token"]))
                        self.assertFalse(controller.password_enabled())
                        with post("/api/security", {"enabled": True, "password": "long-test-password"}):
                            pass
                        request = urllib.request.Request(url + "/api/login", data=json.dumps({"password": "long-test-password"}).encode(), headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request) as response:
                            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                        with post("/api/security", {"enabled": False}, cookie):
                            pass
                        self.assertFalse(controller.password_enabled())
                        self.assertEqual(controller.config["host_root"], "/mnt/documents")
                        controller.config.update(onboarding_complete=True, setup_step=7)
                        with post("/api/security", {"enabled": False}):
                            pass
                        self.assertEqual(controller.config["setup_step"], 7)
                    finally:
                        server.shutdown()
                        server.server_close()
                controller.close()

    def test_index_completion_ignores_old_processes_and_restarts_only_for_model_changes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'DATA', Path(tmp)):
            controller = app.Controller.__new__(app.Controller)
            controller.lock = threading.RLock()
            controller.config = {'active_embedding_model': 'e5-small', 'embedding_model': 'bge-m3', 'setup_step': 7, 'mcp_enabled': True}
            controller.ingest_model = 'bge-m3'
            controller.ingest_rebuild = False
            controller.ingest_finalized = False
            controller.stopping = False
            controller._save = Mock()
            controller._stop_mcp = Mock()
            controller._start_mcp = Mock()
            controller.ingest = Mock(wait=lambda: 0)
            controller._finish_index(Mock(wait=lambda: 0), False, 'e5-small')
            self.assertEqual(controller.config['active_embedding_model'], 'e5-small')
            controller._save.assert_not_called()
            controller._finish_index(controller.ingest, False, 'bge-m3')
            self.assertEqual(controller.config['active_embedding_model'], 'bge-m3')
            controller._start_mcp.assert_called_once()
            controller._start_mcp.reset_mock()
            controller.ingest = Mock(wait=lambda: 0)
            controller.ingest_finalized = False
            controller._finish_index(controller.ingest, False, 'bge-m3')
            controller._start_mcp.assert_not_called()

    def test_failed_services_restart_only_while_enabled(self):
        controller = object.__new__(app.Controller)
        controller.ingest = None
        controller.config = {"qdrant_enabled": True, "mcp_enabled": True}
        controller.qdrant = Mock()
        controller.qdrant.poll.return_value = 1
        controller.mcp = Mock()
        controller.mcp.poll.return_value = 1
        with (
            patch.object(app.Path, "exists", return_value=True),
            patch.object(controller, "_start_qdrant") as start_qdrant,
            patch.object(controller, "_start_mcp") as start_mcp,
        ):
            controller._restart_failed_services()
            start_qdrant.assert_called_once_with()
            start_mcp.assert_called_once_with()
            start_qdrant.reset_mock()
            start_mcp.reset_mock()
            controller.ingest = Mock()
            controller.ingest.poll.return_value = None
            controller._restart_failed_services()
            start_mcp.assert_not_called()
            start_qdrant.reset_mock()
            controller.ingest = None
            controller.config["qdrant_enabled"] = False
            controller._restart_failed_services()
            start_qdrant.assert_not_called()
            start_mcp.assert_not_called()

    def test_rebuild_pauses_search_and_resumes_it_after_an_indexing_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            documents = data / 'documents'
            with (
                patch.object(app, 'DATA', data), patch.object(app, 'SOURCE', documents),
                patch.object(app, 'MANAGED_SOURCE', documents), patch.object(app, 'HOST_SOURCE', data / 'host-documents'),
                patch.object(app, 'CONFIG', data / 'config.json'), patch.object(app, 'POLICY', data / 'index-policy.yaml'),
                patch.object(app, 'AGENT_MANIFEST', data / 'agent-manifest.json'), patch.object(app, 'AUTO_AGENT_CONFIG', None),
                patch.object(app.Controller, '_schedule', lambda self: None),
            ):
                controller = app.Controller()
                controller.config.update(source_mode='container', source_root=str(documents), folders=[''], onboarding_complete=True, setup_step=7, mcp_enabled=True, active_embedding_model='e5-small', embedding_model='bge-m3')
                process = Mock()
                process.poll.return_value = None
                process.wait.return_value = 1
                with patch.object(app.subprocess, 'Popen', return_value=process) as spawn, patch.object(app.threading, 'Thread'), patch.object(controller, '_stop_mcp') as stop, patch.object(controller, '_start_mcp') as start:
                    with self.assertRaisesRegex(ValueError, 'cannot be a dry run'):
                        controller.run_index(dry_run=True, rebuild=True)
                    spawn.assert_not_called()
                    controller.run_index(rebuild=True)
                    self.assertIn('--rebuild', spawn.call_args.args[0])
                    self.assertEqual(spawn.call_args.kwargs['env']['DENSE_COLLECTION'], MODELS['bge-m3']['collection'])
                    stop.assert_called_once()
                    controller._restart_failed_services()
                    start.assert_not_called()
                    process.poll.return_value = 1
                    controller._finish_index(process, False)
                    start.assert_called_once()
                    self.assertEqual(controller.config['active_embedding_model'], 'e5-small')
                controller.config['mcp_enabled'] = False
                controller.close()

    def test_saved_policy_recovers_required_directory_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            policy_file = data / "index-policy.yaml"
            old_policy = app.DEFAULT_POLICY.read_text(encoding="utf-8")
            for name in ("coverage", "cache", ".cache", "vendor", ".stversions"):
                old_policy = old_policy.replace(f"  - {name}\n", "")
            policy_file.write_text(old_policy, encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", policy_file),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                app.validate_policy(policy_file.read_text(encoding="utf-8"))
                controller.close()

    def test_legacy_bundled_exclusions_are_removed_from_saved_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            policy_file = data / "index-policy.yaml"
            policy = yaml.safe_load(app.DEFAULT_POLICY.read_text(encoding="utf-8"))
            old_entries = ["former-one", "former-two", "former-three", "former-four"]
            legacy_list = policy["exclude_directories"] + old_entries
            policy["exclude_directories"] = legacy_list + ["custom-folder"]
            policy_file.write_text(yaml.safe_dump(policy), encoding="utf-8")
            fingerprint = hashlib.sha256("\0".join(legacy_list).encode()).hexdigest()
            with (
                patch.object(policy_defaults, "LEGACY_DIRECTORY_LIST_SHA256", fingerprint),
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", policy_file),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                controller.close()
            saved = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["exclude_directories"], policy_defaults.DEFAULT_POLICY["exclude_directories"] + ["custom-folder"])

    def test_setup_progress_is_persisted_and_requires_successful_initial_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            source = data / "documents"
            mounted = Path(tmp) / "mounted"
            mounted.mkdir()
            (mounted / "sample.md").write_text("An eligible document", encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", source),
                patch.object(app, "MANAGED_SOURCE", source), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                self.assertEqual(controller.config["setup_step"], 0)
                controller.set_password("long-test-password")
                self.assertEqual(controller.config["setup_step"], 1)
                with self.assertRaisesRegex(ValueError, "previous setup step"):
                    controller.advance_setup(3)
                controller.advance_setup(2)
                controller.update({"source_mode": "container", "source_root": str(mounted), "folders": [""]})
                self.assertEqual(controller.config["setup_step"], 2)
                for step in (3, 4):
                    controller.advance_setup(step)
                with self.assertRaisesRegex(ValueError, "Scan the selected folder"):
                    controller.advance_setup(5)
                controller.start_scan()
                for _ in range(100):
                    if controller.status()["scan_complete"]:
                        break
                    time.sleep(0.01)
                self.assertTrue(controller.status()["scan_ready"])
                self.assertEqual(controller.status()["scan_eligible_preview"], ["sample.md"])
                controller.advance_setup(5)
                with self.assertRaisesRegex(ValueError, "successful dry-run"):
                    controller.advance_setup(6)
                controller.run_index(dry_run=True)
                controller.ingest.wait(timeout=30)
                for _ in range(100):
                    if controller.last_result:
                        break
                    time.sleep(0.01)
                self.assertEqual(controller.last_result["exit_code"], 0)
                self.assertIn("Documents parsed: 1", (data / "ingest.log").read_text(encoding="utf-8"))
                self.assertEqual(controller.status()["index_progress"]["stage"], "complete")
                controller.advance_setup(6)
                controller.complete_onboarding()
                controller.ingest = None
                controller._finish_index(Mock(wait=lambda: 1), False)
                self.assertEqual(controller.config["setup_step"], 6)
                controller._finish_index(Mock(wait=lambda: 0), True)
                self.assertEqual(controller.config["setup_step"], 6)
                controller._finish_index(Mock(wait=lambda: 0), False)
                self.assertEqual(controller.config["setup_step"], 7)
                controller.close()
                restored = app.Controller()
                self.assertEqual(restored.config["setup_step"], 7)
                restored.close()

    def test_initial_index_can_be_postponed_and_started_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            source = data / "documents"
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", source),
                patch.object(app, "MANAGED_SOURCE", source), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                with self.assertRaisesRegex(ValueError, "dry-run test"):
                    controller.skip_initial_index()
                controller.config["setup_step"] = 6
                controller._save()
                controller.skip_initial_index()
                self.assertTrue(controller.config["onboarding_complete"])
                self.assertTrue(controller.config["initial_index_skipped"])
                self.assertEqual(controller.config["setup_step"], 7)
                controller.close()
                restored = app.Controller()
                self.assertTrue(restored.config["initial_index_skipped"])
                restored._finish_index(Mock(wait=lambda: 0), False)
                self.assertFalse(restored.config["initial_index_skipped"])
                restored.close()

    def test_managed_source_is_created_on_first_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            source = data / "documents"
            (data / "config.json").write_text(json.dumps({"mcp_enabled": False}), encoding="utf-8")
            with (
                patch.object(app, "DATA", data),
                patch.object(app, "MANAGED_SOURCE", source),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "SOURCE", source),
                patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                self.assertTrue(source.is_dir())
                self.assertEqual(app.source_dir(""), source)
                controller.close()

    def test_legacy_subfolder_is_migrated_and_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            source = data / "documents"
            (source / "notes").mkdir(parents=True)
            (data / "config.json").write_text(json.dumps({"subfolder": "notes", "mcp_enabled": False}), encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "MANAGED_SOURCE", source),
                patch.object(app, "SOURCE", source), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                self.assertEqual(controller.config["folders"], ["notes"])
                self.assertEqual(controller.config["source_root"], str(source))
                self.assertEqual(json.loads((data / "config.json").read_text())["folders"], ["notes"])
                controller.close()

    def test_document_root_can_change_to_another_visible_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            original = data / "documents"
            mounted = Path(tmp) / "mounted" / "knowledge"
            (mounted / "notes").mkdir(parents=True)
            with (
                patch.object(app, "DATA", data), patch.object(app, "MANAGED_SOURCE", original),
                patch.object(app, "SOURCE", original), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                result = controller.update({"source_mode": "container", "source_root": str(mounted), "folders": ["notes"]})
                self.assertEqual(result["source_root"], str(mounted))
                self.assertEqual(result["folders"], ["notes"])
                self.assertEqual(controller._env()["KNOWLEDGE_ROOT"], str(mounted))
                self.assertEqual(json.loads(controller._env()["INDEX_SOURCE_PATHS"]), ["notes"])
                with self.assertRaisesRegex(ValueError, "Mount the host folder"):
                    controller.update({"source_mode": "container", "source_root": str(Path(tmp) / "not-mounted"), "folders": [""]})
                with self.assertRaisesRegex(ValueError, "Application data cannot be indexed"):
                    controller.update({"source_mode": "container", "source_root": str(data), "folders": [""]})
                controller.close()
                restored = app.Controller()
                self.assertEqual(restored.config["source_root"], str(mounted))
                restored.close()

    def test_automatic_path_replaces_legacy_container_location_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            source = data / "documents"
            (data / "config.json").write_text(json.dumps({"source_mode": "container", "source_root": str(source), "folders": [""]}), encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "MANAGED_SOURCE", source),
                patch.object(app, "SOURCE", source), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"), patch.object(app, "AUTH", data / "auth.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                self.assertEqual(controller.config["source_selection"], "auto")
                self.assertEqual(controller.config["source_root"], str(source))
                controller.set_password("strong-test-password")
                result = controller.update({"document_root": "/mnt/documents", "source_selection": "auto", "folders": [""]})
                self.assertEqual(result["source_mode"], "host_agent")
                self.assertEqual(result["host_root"], "/mnt/documents")
                self.assertEqual(controller._env()["KNOWLEDGE_ROOT"], str(data / "host-documents"))
                self.assertFalse(controller.status()["source_ready"])
                with self.assertRaisesRegex(ValueError, "host agent"):
                    controller.run_index(dry_run=True)
                controller.update({"document_root": r"D:\User folders\Documents", "source_selection": "host_agent", "folders": [""]})
                controller.close()
                restored = app.Controller()
                self.assertEqual(restored.config["host_root"], r"D:\User folders\Documents")
                self.assertEqual(restored.config["source_mode"], "host_agent")
                restored.close()

    def test_automatic_path_reads_visible_roots_and_rejects_application_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            mounted = Path(tmp) / "document-mount"
            mounted.mkdir()
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                result = controller.update({"document_root": str(mounted), "source_selection": "auto", "folders": [""]})
                self.assertEqual(result["source_selection"], "auto")
                self.assertEqual(result["source_mode"], "container")
                self.assertTrue(controller.status()["source_ready"])
                with self.assertRaisesRegex(ValueError, "Application data"):
                    controller.update({"document_root": str(data), "source_selection": "auto"})
                controller.close()

    def test_source_and_policy_cannot_escape_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "notes").mkdir()
            with patch.object(app, "SOURCE", source):
                self.assertEqual(app.source_dir("notes"), source / "notes")
                self.assertEqual(app.source_dir(str(source)), source)
                self.assertEqual(app.normalize_folders([str(source), "notes"]), ["", "notes"])
                with self.assertRaises(ValueError):
                    app.source_dir("../elsewhere")
                with self.assertRaises(ValueError):
                    app.source_dir(str(source.parent))

        original = app.DEFAULT_POLICY.read_text(encoding="utf-8")
        app.validate_policy(original)
        with self.assertRaises(ValueError):
            app.validate_policy(original.replace("  - vendor\n", ""))
        with self.assertRaises(ValueError):
            app.validate_policy(original.replace("  - .pem\n", "  - .PEM\n"))

    def test_local_ui_requires_token_for_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            source = data / "source"
            source.mkdir()
            (data / "config.json").write_text(json.dumps({"mcp_enabled": False}), encoding="utf-8")
            with (
                patch.object(app, "DATA", data),
                patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "SOURCE", source),
                patch.object(app, "MANAGED_SOURCE", source),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "AUTH", data / "auth.json"),
            ):
                controller = app.Controller()
                with patch.object(app, "controller", controller):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}"
                        with urllib.request.urlopen(url) as response:
                            page = response.read().decode()
                            self.assertIn(app.TOKEN, page)
                            self.assertIn('lang="en"', page)
                            self.assertEqual(page.count('type="file"'), 1)
                            self.assertIn('id="backup-file" type="file" accept=".tar.gz,application/gzip"', page)
                        lan = urllib.request.Request(url, headers={"Host": "10.10.10.80:8080"})
                        with urllib.request.urlopen(lan) as response:
                            self.assertEqual(response.status, 200)
                        external = urllib.request.Request(url, headers={"Host": "untrusted.example:8080"})
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(external)
                        self.assertEqual(denied.exception.code, 403)
                        request = urllib.request.Request(url + "/api/config", data=b"{}", headers={"Content-Type": "application/json"})
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(request)
                        self.assertEqual(denied.exception.code, 403)
                        request.add_header("X-Control-Token", app.TOKEN)
                        request.add_header("Host", "10.10.10.80:8080")
                        with urllib.request.urlopen(request) as response:
                            self.assertEqual(response.status, 200)
                        upload = urllib.request.Request(url + "/api/upload?path=notes%2Fexample.md", data=b"Example document", headers={"X-Control-Token": app.TOKEN})
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(upload)
                        self.assertEqual(denied.exception.code, 404)
                        self.assertFalse((source / "notes" / "example.md").exists())
                        self.assertNotIn("host_agent.py install", page)
                        self.assertNotIn("Generate pairing key", page)
                        (source / "notes").mkdir()
                        config = {"folders": [str(source / "notes")], "interval_hours": 2}
                        request = urllib.request.Request(url + "/api/config", data=json.dumps(config).encode(), headers={"Content-Type": "application/json", "X-Control-Token": app.TOKEN})
                        with urllib.request.urlopen(request) as response:
                            self.assertEqual(json.load(response)["folders"], ["notes"])
                        self.assertEqual(json.loads((data / "config.json").read_text())["folders"], ["notes"])
                        controller.config.update(onboarding_complete=False, setup_step=5)
                        controller._save()
                        policy_request = urllib.request.Request(
                            url + "/api/policy",
                            data=json.dumps({"content": app.DEFAULT_POLICY.read_text(encoding="utf-8")}).encode(),
                            headers={"Content-Type": "application/json", "X-Control-Token": app.TOKEN},
                        )
                        with urllib.request.urlopen(policy_request) as response:
                            self.assertEqual(response.status, 200)
                        self.assertEqual(controller.config["setup_step"], 3)
                        self.assertEqual(json.loads((data / "config.json").read_text())["setup_step"], 3)
                    finally:
                        server.shutdown()
                        server.server_close()
                        controller.close()

    def test_password_blocks_status_and_changes_until_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            source = data / "documents"
            source.mkdir()
            with (
                patch.object(app, "DATA", data), patch.object(app, "MANAGED_SOURCE", source),
                patch.object(app, "SOURCE", source), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                controller.set_password("long-test-password")
                with patch.object(app, "controller", controller):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}"
                        with urllib.request.urlopen(url) as response:
                            self.assertNotIn(app.TOKEN, response.read().decode())
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(url + "/api/status")
                        self.assertEqual(denied.exception.code, 401)
                        try:
                            with urllib.request.urlopen(url + "/api/health") as health:
                                self.assertEqual(health.status, 200)
                        except urllib.error.HTTPError as unhealthy:
                            self.assertEqual(unhealthy.code, 503)
                        request = urllib.request.Request(url + "/api/login", data=json.dumps({"password": "long-test-password"}).encode(), headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request) as response:
                            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                        request = urllib.request.Request(url + "/api/status", headers={"Cookie": cookie})
                        with urllib.request.urlopen(request) as response:
                            self.assertEqual(response.status, 200)
                    finally:
                        server.shutdown()
                        server.server_close()
                        controller.close()

    def test_host_agent_syncs_changed_files_and_removes_deleted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            mirror = data / "documents"
            host_mirror = data / "host-documents"
            host = base / "host"
            (host / "notes").mkdir(parents=True)
            (host / "notes" / "first.md").write_text("first version", encoding="utf-8")
            (host / "secrets").mkdir()
            (host / "secrets" / "private.md").write_text("private", encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", mirror),
                patch.object(app, "MANAGED_SOURCE", mirror), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", host_mirror),
                patch.object(app, "AUTH", data / "auth.json"), patch.object(app, "AGENT_AUTH", data / "agent-auth.json"),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                controller.set_password("strong-test-password")
                token = controller.agent_pair()["token"]
                # The test host and container share a process; hide the host path only
                # while resolving the dashboard selection, as Docker would do.
                exists = Path.exists
                with patch.object(Path, "exists", lambda path: False if path == host else exists(path)):
                    controller.update({"document_root": str(host), "source_selection": "auto", "folders": [""]})
                with patch.object(app, "controller", controller):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}"
                        task = host_agent.request(url, token, "/api/agent/task")
                        with patch.object(controller, "report_agent_progress", wraps=controller.report_agent_progress) as progress:
                            host_agent.sync(url, token, task)
                        self.assertEqual([call.args[0]["phase"] for call in progress.call_args_list], ["scanning", "scanning", "planning", "transferring", "transferring", "finalizing"])
                        self.assertEqual((host_mirror / "notes" / "first.md").read_text(), "first version")
                        self.assertFalse((host_mirror / "secrets" / "private.md").exists())
                        self.assertTrue(controller.status()["source_ready"])
                        self.assertEqual(controller.status()["agent_file_count"], 1)
                        self.assertEqual(controller._env()["KNOWLEDGE_ROOT"], str(host_mirror))
                        # An older companion can still report a Word lock file.
                        lock_name = "notes/~$draft.docx"
                        lock_file = host_mirror / lock_name
                        lock_file.write_bytes(b"stale lock")
                        old_inventory = dict(controller.agent_manifest["files"])
                        old_inventory[lock_name] = {"size": lock_file.stat().st_size, "sha256": hashlib.sha256(lock_file.read_bytes()).hexdigest()}
                        old_payload = {"revision": controller.config["sync_revision"], "sync_request": controller.config["sync_request"], "files": old_inventory}
                        self.assertNotIn(lock_name, controller.agent_plan(old_payload)["missing"])
                        self.assertEqual(controller.agent_commit(old_payload)["synced"], 1)
                        self.assertFalse(lock_file.exists())
                        (host / "notes" / "first.md").write_text("second version", encoding="utf-8")
                        host_agent.sync(url, token, task)
                        self.assertEqual((host_mirror / "notes" / "first.md").read_text(), "second version")
                        (host / "notes" / "first.md").unlink()
                        host_agent.sync(url, token, task)
                        self.assertFalse((host_mirror / "notes" / "first.md").exists())
                        (host / "old.md").write_text("old folder", encoding="utf-8")
                        host_agent.sync(url, token, task)
                        self.assertTrue((host_mirror / "old.md").exists())
                        another_host = base / "another-host"
                        another_host.mkdir()
                        (another_host / "second.md").write_text("another folder", encoding="utf-8")
                        controller.update({"source_mode": "host_agent", "host_root": str(another_host), "folders": [""]})
                        self.assertFalse(controller.status()["source_ready"])
                        host_agent.sync(url, token, host_agent.request(url, token, "/api/agent/task"))
                        self.assertEqual((host_mirror / "second.md").read_text(), "another folder")
                        self.assertFalse((host_mirror / "old.md").exists())
                        self.assertEqual(json.loads((data / "config.json").read_text())["host_root"], str(another_host))
                        host_agent.request(url, token, "/api/agent/error", {"revision": controller.config["sync_revision"], "error": "Folder unavailable"})
                        self.assertFalse(controller.status()["source_ready"])
                        self.assertIn("Folder unavailable", controller.status()["agent_error"])
                        host_agent.sync(url, token, host_agent.request(url, token, "/api/agent/task"))
                        self.assertTrue(controller.status()["source_ready"])
                        refresh_id = controller.request_agent_sync()["sync_request"]
                        self.assertLess(controller.status()["agent_sync_request_completed"], refresh_id)
                        self.assertFalse(controller.status()["source_ready"])
                        self.assertFalse(controller.report_agent_progress({"revision": controller.config["sync_revision"], "sync_request": refresh_id - 1, "phase": "scanning", "checked": 1, "completed": 0, "total": 0})["accepted"])
                        host_agent.sync(url, token, host_agent.request(url, token, "/api/agent/task"))
                        self.assertEqual(controller.status()["agent_sync_request_completed"], refresh_id)
                        self.assertTrue(controller.status()["source_ready"])
                        bad = urllib.request.Request(url + "/api/agent/plan", data=b"{}", headers={"X-Agent-Token": "wrong"})
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(bad)
                        self.assertEqual(denied.exception.code, 403)
                    finally:
                        server.shutdown()
                        server.server_close()
                        controller.close()

    def test_selected_host_folders_are_checked_and_only_they_are_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            host = base / "host"
            for name in ("first", "second", "unselected"):
                (host / name).mkdir(parents=True)
                (host / name / "note.md").write_text(name, encoding="utf-8")
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"), patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"), patch.object(app, "AUTH", data / "auth.json"),
                patch.object(app, "AGENT_AUTH", data / "agent-auth.json"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
            ):
                controller = app.Controller()
                token = controller.agent_pair()["token"]
                with patch.object(app, "controller", controller):
                    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{server.server_port}"
                        controller.agent_task()
                        checked = []
                        probe = threading.Thread(target=lambda: checked.append(controller.check_folder(str(host / "first"), "host_agent")))
                        probe.start()
                        for _ in range(100):
                            if controller.folder_probe:
                                break
                            time.sleep(0.01)
                        host_agent.check_folder(url, token, controller.folder_probe, None)
                        probe.join(timeout=2)
                        self.assertEqual(checked, [{"reachable": True, "mode": "host_agent"}])
                        selected = [str(host / "first"), str(host / "second")]
                        result = controller.update({"document_paths": selected, "source_selection": "host_agent"})
                        self.assertEqual(result["folders"], ["first", "second"])
                        self.assertEqual(result["host_root"], str(host))
                        self.assertFalse(controller.agent_task()["scan_enabled"])
                        controller.request_agent_sync()
                        task = host_agent.request(url, token, "/api/agent/task")
                        self.assertTrue(task["scan_enabled"])
                        host_agent.sync(url, token, task)
                        self.assertEqual(controller.scan_files(), ["first/note.md", "second/note.md"])
                        self.assertFalse((data / "host-documents" / "unselected").exists())
                        self.assertEqual(controller.status()["document_paths"], selected)
                        controller.update({"document_paths": [], "source_selection": "host_agent"})
                        self.assertEqual(controller.status()["document_paths"], [])
                    finally:
                        server.shutdown()
                        server.server_close()
                        controller.close()

    def test_unreadable_host_directory_is_reported_instead_of_syncing_empty_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            def denied_walk(folder, **options):
                options["onerror"](PermissionError(13, "Permission denied", str(folder / "private")))
                return iter(())

            policy = app.yaml.safe_load(app.DEFAULT_POLICY.read_text(encoding="utf-8"))
            with patch.object(host_agent.os, "walk", denied_walk):
                with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                    host_agent.inventory(tmp, policy)

    def test_host_service_install_keeps_its_runtime_independent_of_downloaded_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            config = data / "agent.json"
            with (
                patch.object(host_agent.getpass, "getpass", return_value="test-pairing-key"),
                patch.object(host_agent, "request", return_value={}),
                patch.object(Path, "home", return_value=data),
                patch.object(host_agent.subprocess, "run", return_value=Mock(returncode=0)) as run,
            ):
                host_agent.install("http://127.0.0.1:8080", config)
            script = data / "runtime" / "host_agent.py"
            self.assertTrue(script.is_file())
            self.assertTrue((data / "runtime" / "mcp" / "host_sync.py").is_file())
            unit = data / ".config" / "systemd" / "user" / "knowledge-mcp-agent.service"
            if host_agent.os.name == "posix":
                self.assertIn(str(script), unit.read_text(encoding="utf-8"))
            else:
                self.assertIn(str(script), run.call_args_list[0].args[0][run.call_args_list[0].args[0].index("/TR") + 1])

    def test_docker_agent_pairs_automatically_and_reuses_credentials_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            credentials = Path(tmp) / "agent" / "agent.json"
            with (
                patch.object(app, "DATA", data), patch.object(app, "MANAGED_SOURCE", data / "documents"),
                patch.object(app, "SOURCE", data / "documents"), patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"), patch.object(app, "AUTH", data / "auth.json"),
                patch.object(app, "AGENT_AUTH", data / "agent-auth.json"), patch.object(app, "AUTO_AGENT_CONFIG", credentials),
                patch.object(app, "POLICY", data / "index-policy.yaml"), patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
            ):
                controller = app.Controller()
                saved = json.loads(credentials.read_text(encoding="utf-8"))
                self.assertTrue(controller.agent_authenticated(saved["token"]))
                self.assertTrue(controller.status()["agent_managed"])
                self.assertNotIn("token", controller.status()["config"])
                controller.set_password("strong-test-password")
                # A managed agent must interpret paths on the host, even if the app
                # happens to have a directory with the same name.
                visible = Path(tmp) / "chosen-host-folder"
                visible.mkdir()
                controller.update({"document_root": str(visible), "source_selection": "auto", "folders": [""]})
                self.assertEqual(controller.config["source_mode"], "host_agent")
                self.assertEqual(controller.config["host_root"], str(visible))
                controller.close()
                restored = app.Controller()
                self.assertEqual(json.loads(credentials.read_text(encoding="utf-8")), saved)
                self.assertTrue(restored.agent_authenticated(saved["token"]))
                rotated = restored.agent_pair()["token"]
                self.assertEqual(json.loads(credentials.read_text(encoding="utf-8"))["token"], rotated)
                self.assertFalse(restored.agent_authenticated(saved["token"]))
                restored.close()


if __name__ == "__main__":
    unittest.main()
