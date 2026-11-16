"""Tests for MCP auth, ACL settings, server hardening and access UI plumbing."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "mcp"))
import host_sync
import retrieval
import server
import webui as app
sys.path.insert(0, str(ROOT))
import host_agent


def _drive(guard, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    asyncio.run(guard(scope, receive, send))
    return messages


def _scope(path="/mcp", headers=None, query=b"", scope_type="http"):
    return {
        "type": scope_type,
        "path": path,
        "headers": headers or [],
        "query_string": query,
        "method": "POST",
    }


class _InnerApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


class AccessGuardTest(unittest.TestCase):
    def test_auth_disabled_passes_through_and_still_serves_health(self):
        inner = _InnerApp()
        guard = server.AccessGuard(inner, None)
        messages = _drive(guard, _scope())
        self.assertEqual(inner.calls, 1)
        self.assertEqual(messages[0]["status"], 200)
        health = _drive(guard, _scope(path="/healthz"))
        self.assertEqual(inner.calls, 1)
        self.assertEqual(health[0]["status"], 200)
        self.assertIn(b"knowledge-mcp", health[1]["body"])

    def test_missing_token_is_rejected_without_reaching_the_app(self):
        inner = _InnerApp()
        guard = server.AccessGuard(inner, "secret-token")
        messages = _drive(guard, _scope())
        self.assertEqual(inner.calls, 0)
        self.assertEqual(messages[0]["status"], 401)
        self.assertIn(
            (b"www-authenticate", b'Bearer realm="knowledge-mcp"'),
            messages[0]["headers"],
        )

    def test_bearer_api_key_and_query_token_are_accepted(self):
        for scope in (
            _scope(headers=[(b"authorization", b"Bearer secret-token")]),
            _scope(headers=[(b"x-api-key", b"secret-token")]),
            _scope(query=b"access_token=secret-token"),
        ):
            inner = _InnerApp()
            guard = server.AccessGuard(inner, "secret-token")
            messages = _drive(guard, scope)
            self.assertEqual(inner.calls, 1)
            self.assertEqual(messages[0]["status"], 200)

    def test_wrong_token_is_rejected(self):
        inner = _InnerApp()
        guard = server.AccessGuard(inner, "secret-token")
        self.assertEqual(
            _drive(guard, _scope(headers=[(b"authorization", b"Bearer wrong")]))[0][
                "status"
            ],
            401,
        )
        self.assertEqual(inner.calls, 0)

    def test_healthz_bypasses_auth_and_lifespan_is_forwarded(self):
        inner = _InnerApp()
        guard = server.AccessGuard(inner, "secret-token")
        self.assertEqual(_drive(guard, _scope(path="/healthz"))[0]["status"], 200)
        _drive(guard, _scope(scope_type="lifespan"))
        self.assertEqual(inner.calls, 1)


class RootAclTest(unittest.TestCase):
    def test_empty_allowlist_is_unrestricted(self):
        self.assertTrue(host_sync.root_allowed("/mnt/documents", []))
        self.assertTrue(host_sync.root_allowed("/mnt/documents", None))

    def test_root_must_be_inside_an_allowed_entry(self):
        self.assertTrue(host_sync.root_allowed("/mnt/documents", ["/mnt"]))
        self.assertTrue(host_sync.root_allowed("/mnt/documents", ["/mnt/documents"]))
        self.assertFalse(host_sync.root_allowed("/etc", ["/mnt"]))
        self.assertFalse(host_sync.root_allowed("/mnt/other", ["/mnt/documents"]))

    def test_relative_or_invalid_roots_are_rejected(self):
        self.assertFalse(host_sync.root_allowed("relative", ["/mnt"]))

    def test_windows_paths_are_case_insensitive(self):
        self.assertTrue(
            host_sync.root_allowed(r"D:\Docs\Reports", [r"D:\Docs"])
        )
        self.assertFalse(host_sync.root_allowed(r"E:\Docs", [r"D:\Docs"]))


class AllowedFileAclTest(unittest.TestCase):
    def policy(self, deny=()):
        return {
            "exclude_directories": [],
            "exclude_top_level": [],
            "exclude_files": [],
            "include_extensions": [".md"],
            "exclude_extensions": [],
            "max_file_size_mb": 50,
            "acl_deny_directories": list(deny),
        }

    def test_denied_directory_name_is_excluded_at_any_depth(self):
        policy = self.policy(["private", "hr"])
        self.assertTrue(host_sync.allowed_file("docs/guide.md", 100, policy))
        self.assertFalse(host_sync.allowed_file("docs/private/secret.md", 100, policy))
        self.assertFalse(host_sync.allowed_file("private/secret.md", 100, policy))
        self.assertFalse(host_sync.allowed_file("a/b/hr/note.md", 100, policy))

    def test_no_deny_list_keeps_default_behaviour(self):
        self.assertTrue(host_sync.allowed_file("private/note.md", 100, self.policy()))


class ReadPolicyAclTest(unittest.TestCase):
    def test_acl_is_overlaid_on_the_yaml_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_file = Path(tmp) / "index-policy.yaml"
            policy_file.write_text(
                yaml.safe_dump({"exclude_directories": [], "include_extensions": [".md"]}),
                encoding="utf-8",
            )
            with patch.object(app, "POLICY", policy_file):
                result = app.read_policy(
                    {"acl_deny_directories": ["hr"], "acl_allow_roots": ["/mnt/docs"]}
                )
            self.assertEqual(result["acl_deny_directories"], ["hr"])
            self.assertEqual(result["acl_allow_roots"], ["/mnt/docs"])
            self.assertEqual(result["include_extensions"], [".md"])


class RetrievalAclTest(unittest.TestCase):
    def policy(self, deny=()):
        return {
            "exclude_directories": [],
            "exclude_top_level": [],
            "exclude_files": [],
            "include_extensions": [".md"],
            "exclude_extensions": [],
            "acl_deny_directories": list(deny),
        }

    def test_source_allowed_honours_acl_deny(self):
        policy = self.policy(["private"])
        self.assertTrue(retrieval.source_allowed("docs/guide.md", policy))
        self.assertFalse(retrieval.source_allowed("docs/private/guide.md", policy))

    def test_query_filter_includes_acl_deny_in_directory_exclusions(self):
        filters = retrieval.query_filter(self.policy(["private"]))
        values = set()
        for condition in filters.must_not:
            match = getattr(condition, "match", None)
            if hasattr(match, "any"):
                values.update(match.any)
        self.assertIn("private", values)

    def test_env_deny_directories_are_parsed(self):
        with patch.dict(os.environ, {"ACL_DENY_DIRECTORIES": " HR , Private ,../bad "}):
            self.assertEqual(retrieval._env_deny_directories(), ["hr", "private"])


class SetAccessTest(unittest.TestCase):
    def _controller(self):
        controller = app.Controller.__new__(app.Controller)
        controller.config = {
            "mcp_auth_enabled": False,
            "acl_allow_roots": [],
            "acl_deny_directories": [],
            "lexical_bigrams": False,
            "sync_revision": 3,
            "mcp_enabled": True,
        }
        controller.lock = threading.RLock()
        controller.ingest = None
        controller._save = Mock()
        controller._stop_mcp = Mock()
        controller._start_mcp = Mock()
        return controller

    def test_enabling_auth_generates_a_token_and_restarts_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "mcp-auth.json"
            with patch.object(app, "MCP_AUTH", auth_file):
                controller = self._controller()
                result = controller.set_access({"mcp_auth_enabled": True})
            self.assertTrue(result["mcp_auth_enabled"])
            self.assertTrue(len(result["mcp_auth_token"]) >= 12)
            self.assertTrue(controller.config["mcp_auth_enabled"])
            controller._stop_mcp.assert_called_once_with()
            controller._start_mcp.assert_called_once_with()
            self.assertEqual(json.loads(auth_file.read_text())["token"], result["mcp_auth_token"])

    def test_default_is_disabled_and_no_token_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "MCP_AUTH", Path(tmp) / "mcp-auth.json"):
                controller = self._controller()
                result = controller.set_access({})
            self.assertFalse(result["mcp_auth_enabled"])
            self.assertIsNone(result["mcp_auth_token"])

    def test_acl_lists_are_normalized_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "MCP_AUTH", Path(tmp) / "mcp-auth.json"):
                controller = self._controller()
                result = controller.set_access(
                    {
                        "acl_allow_roots": ["/mnt/docs", "/mnt/docs"],
                        "acl_deny_directories": ["HR", "Private"],
                        "lexical_bigrams": True,
                    }
                )
        self.assertEqual(result["acl_allow_roots"], ["/mnt/docs"])
        self.assertEqual(result["acl_deny_directories"], ["hr", "private"])
        self.assertTrue(result["lexical_bigrams"])
        self.assertEqual(controller.config["sync_revision"], 4)

    def test_invalid_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "MCP_AUTH", Path(tmp) / "mcp-auth.json"):
                controller = self._controller()
                for values in (
                    {"mcp_auth_enabled": "yes"},
                    {"acl_allow_roots": "not-a-list"},
                    {"acl_deny_directories": ["../escape"]},
                    {"lexical_bigrams": 1},
                ):
                    with self.assertRaises(ValueError):
                        controller.set_access(values)

    def test_env_carries_auth_acl_and_lexical_flags(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            app, "MCP_AUTH", Path(tmp) / "mcp-auth.json"
        ):
            controller = self._controller()
            controller.config.update(
                mcp_auth_enabled=True,
                acl_deny_directories=["hr"],
                lexical_bigrams=True,
                active_embedding_model=None,
                embedding_model="e5-small",
                legacy_embedding_model="e5-small",
                source_root="/data/documents",
                folders=[""],
            )
            controller.set_access({"mcp_auth_token": "a" * 20})
            env = controller._env()
        self.assertEqual(env["MCP_AUTH_ENABLED"], "1")
        self.assertEqual(env["MCP_AUTH_TOKEN"], "a" * 20)
        self.assertEqual(env["ACL_DENY_DIRECTORIES"], "hr")
        self.assertEqual(env["LEXICAL_BIGRAMS"], "1")


class AccessRouteTest(unittest.TestCase):
    def test_api_access_is_auth_gated_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            with (
                patch.object(app, "DATA", data), patch.object(app, "SOURCE", data / "documents"),
                patch.object(app, "MANAGED_SOURCE", data / "documents"),
                patch.object(app, "HOST_SOURCE", data / "host-documents"),
                patch.object(app, "CONFIG", data / "config.json"),
                patch.object(app, "POLICY", data / "index-policy.yaml"),
                patch.object(app, "AUTH", data / "auth.json"),
                patch.object(app, "AGENT_AUTH", data / "agent-auth.json"),
                patch.object(app, "MCP_AUTH", data / "mcp-auth.json"),
                patch.object(app, "AGENT_MANIFEST", data / "agent-manifest.json"),
                patch.object(app, "AUTO_AGENT_CONFIG", None),
            ):
                controller = app.Controller()
                with patch.object(app, "controller", controller):
                    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                    thread.start()
                    try:
                        url = f"http://127.0.0.1:{httpd.server_port}"
                        headers = {
                            "Content-Type": "application/json",
                            "X-Control-Token": app.CONTROL_TOKEN,
                        }

                        def post(body, extra=None):
                            request = urllib.request.Request(
                                url + "/api/access",
                                data=json.dumps(body).encode(),
                                headers={**headers, **(extra or {})},
                            )
                            return urllib.request.urlopen(request)

                        with post({"mcp_auth_enabled": True}) as response:
                            result = json.load(response)
                        self.assertTrue(result["mcp_auth_enabled"])
                        self.assertTrue(result["mcp_auth_token"])
                        self.assertTrue(
                            json.loads(app.CONFIG.read_text())["mcp_auth_enabled"]
                        )
                    finally:
                        httpd.shutdown()
                        httpd.server_close()
                controller.close()


class ServerConfigTest(unittest.TestCase):
    def test_invalid_transport_and_port_fail_fast(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "bogus"}), self.assertRaises(
            SystemExit
        ):
            server.main()
        with patch.dict(os.environ, {"MCP_PORT": "not-a-number"}), self.assertRaises(
            SystemExit
        ):
            server.main()

    def test_auth_enabled_requires_a_token(self):
        env = {"MCP_AUTH_ENABLED": "1", "MCP_AUTH_TOKEN": "", "MCP_TRANSPORT": "stdio"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit):
            server.main()

    def test_env_flag_parses_truthy_values(self):
        with patch.dict(os.environ, {"FLAG": "YES"}):
            self.assertTrue(server._env_flag("FLAG"))
        with patch.dict(os.environ, {"FLAG": "0"}):
            self.assertFalse(server._env_flag("FLAG"))
        self.assertFalse(server._env_flag("MISSING_FLAG"))

    def test_build_http_app_wraps_in_access_guard(self):
        guard = server.build_http_app(
            "streamable-http", "127.0.0.1", "/mcp", False, False, "token"
        )
        self.assertIsInstance(guard, server.AccessGuard)
        self.assertEqual(guard.token, "token")


class HostAgentAclTest(unittest.TestCase):
    def test_allowlist_rejects_out_of_scope_host_root(self):
        with self.assertRaisesRegex(ValueError, "allowed-roots"):
            host_agent.resolve_host_folder("/mnt/docs", None, ["/allowed"])
        resolved = host_agent.resolve_host_folder("/mnt/docs", None, ["/mnt"])
        self.assertEqual(str(resolved), str(Path("/mnt/docs").resolve()))

    def test_inventory_denies_sensitive_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            (root / "private").mkdir(parents=True)
            (root / "public").mkdir()
            (root / "public" / "ok.md").write_text("hello", encoding="utf-8")
            (root / "private" / "secret.md").write_text("hello", encoding="utf-8")
            policy = {
                "exclude_directories": [],
                "exclude_top_level": [],
                "include_extensions": [".md"],
                "exclude_extensions": [],
                "exclude_files": [],
                "max_file_size_mb": 50,
                "acl_deny_directories": ["private"],
            }
            _, files = host_agent.inventory(str(root), policy)
            self.assertIn("public/ok.md", files)
            self.assertNotIn("private/secret.md", files)


if __name__ == "__main__":
    unittest.main()
