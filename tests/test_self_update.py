"""Regression coverage for the data-preserving container self-update."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import self_update


def app_info():
    return {
        "Id": "a" * 64,
        "Image": "sha256:oldapp",
        "Name": "/knowledge-mcp",
        "Config": {
            "Image": "paoloronco/knowledge-mcp:latest",
            "Env": [
                "AUTO_HOST_AGENT_CONFIG=/run/host-agent/agent.json",
                "MCP_HOST=0.0.0.0",
                "PATH=/usr/bin",
            ],
            "Cmd": ["python", "mcp/entrypoint.py"],
            "Entrypoint": None,
            "Labels": {},
            "Hostname": "a" * 12,
            "WorkingDir": "/app",
        },
        "HostConfig": {
            "Binds": [
                "knowledge_app:/data",
                "knowledge_qdrant:/qdrant/storage",
                "knowledge_agent:/run/host-agent",
            ],
            "PortBindings": {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
                "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}],
            },
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": "bridge",
            "Tmpfs": {},
            "DeviceRequests": [],
        },
        "Mounts": [],
    }


def helper_info():
    return {
        "Id": "b" * 64,
        "Image": "sha256:oldhelper",
        "Name": "/knowledge-mcp-host-agent",
        "Config": {
            "Image": "paoloronco/knowledge-mcp:latest",
            "Env": ["PATH=/usr/bin"],
            "Cmd": [
                "python", "host_agent.py", "run", "--config",
                "/run/host-agent/agent.json", "--host-root", "/host", "--log-stdout",
            ],
            "Entrypoint": None,
            "Labels": {},
            "Hostname": "a" * 12,
            "WorkingDir": "/app",
        },
        "HostConfig": {
            "Binds": ["knowledge_agent:/run/host-agent:ro", "/:/host:ro"],
            "Tmpfs": {"/host/proc": "ro,noexec,nosuid,size=1m"},
            "NetworkMode": "container:aaaaaaaaaaaa",
            "CapDrop": ["ALL"],
            "CapAdd": ["CAP_DAC_READ_SEARCH"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {},
            "DeviceRequests": [],
        },
        "Mounts": [],
    }


def completed(stdout=""):
    return subprocess.CompletedProcess(["docker"], 0, stdout=stdout, stderr="")


class RunArgsTest(unittest.TestCase):
    def test_app_command_preserves_volumes_ports_env_and_restart(self):
        args = self_update.build_run_args(app_info(), "paoloronco/knowledge-mcp:latest", "knowledge-mcp")
        self.assertEqual(args[:4], ["run", "-d", "--name", "knowledge-mcp"])
        self.assertIn("knowledge_app:/data", args)
        self.assertIn("knowledge_qdrant:/qdrant/storage", args)
        self.assertIn("8080:8080", args)
        self.assertIn("8000:8000", args)
        self.assertIn("unless-stopped", args)
        self.assertIn("AUTO_HOST_AGENT_CONFIG=/run/host-agent/agent.json", args)
        self.assertNotIn("--hostname", args)
        self.assertEqual(args[-3:], ["paoloronco/knowledge-mcp:latest", "python", "mcp/entrypoint.py"])

    def test_gpu_reservation_and_custom_tag_are_preserved(self):
        info = app_info()
        info["HostConfig"]["DeviceRequests"] = [
            {"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]], "Options": {}}
        ]
        args = self_update.build_run_args(info, "paoloronco/knowledge-mcp:cuda", "knowledge-mcp")
        self.assertIn("--gpus", args)
        self.assertIn("all", args)
        self.assertIn("paoloronco/knowledge-mcp:cuda", args)

    def test_shared_network_drops_incompatible_options(self):
        args = self_update.build_run_args(
            helper_info(),
            "paoloronco/knowledge-mcp:latest",
            "knowledge-mcp-host-agent",
            network_override="container:knowledge-mcp",
        )
        self.assertIn("container:knowledge-mcp", args)
        self.assertNotIn("--hostname", args)
        self.assertNotIn("-p", args)
        self.assertNotIn("--dns", args)
        self.assertIn("--read-only", args)
        self.assertIn("ALL", args)
        self.assertIn("CAP_DAC_READ_SEARCH", args)
        self.assertIn("/host/proc:ro,noexec,nosuid,size=1m", args)
        self.assertIn("knowledge_agent:/run/host-agent:ro", args)

    def test_compose_project_detection(self):
        info = app_info()
        info["Config"]["Labels"] = {
            "com.docker.compose.project": "knowledge",
            "com.docker.compose.service": "app",
        }
        with patch.object(self_update.os.path, "isfile", side_effect=lambda p: p in ("compose.yaml", "compose.gpu.yaml")):
            files = self_update.detect_compose_files(info)
        self.assertEqual(files, ["compose.yaml"])
        info["Config"]["Image"] = "paoloronco/knowledge-mcp:cuda"
        with patch.object(self_update.os.path, "isfile", side_effect=lambda p: p in ("compose.yaml", "compose.gpu.yaml")):
            files = self_update.detect_compose_files(info)
        self.assertEqual(files, ["compose.yaml", "compose.gpu.yaml"])
        self.assertEqual(
            self_update.compose_args(files, "knowledge"),
            ["compose", "-f", "compose.yaml", "-f", "compose.gpu.yaml", "-p", "knowledge",],
        )


class FlowTest(unittest.TestCase):
    def test_dry_run_prints_both_commands_without_changes(self):
        buffer = io.StringIO()
        with (
            patch.object(self_update, "docker", return_value=completed("29.0.0")) as docker_mock,
            patch.object(self_update, "inspect_container", side_effect=[app_info(), helper_info()]),
            contextlib.redirect_stdout(buffer),
        ):
            code = self_update.run(["--dry-run"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Would run:", output)
        self.assertIn("and companion knowledge-mcp-host-agent", output)
        self.assertIn("knowledge_app:/data", output)
        self.assertEqual(
            [call.args[0][0] for call in docker_mock.call_args_list],
            ["version"],
        )

    def test_unchanged_image_skips_restart(self):
        buffer = io.StringIO()
        with (
            patch.object(self_update, "docker", return_value=completed("29.0.0")) as docker_mock,
            patch.object(self_update, "inspect_container", side_effect=[app_info(), helper_info()]),
            patch.object(self_update, "image_id", return_value="sha256:same"),
            contextlib.redirect_stdout(buffer),
        ):
            code = self_update.run([])
        self.assertEqual(code, 0)
        self.assertIn("already up to date", buffer.getvalue())
        invoked = [call.args[0][0] for call in docker_mock.call_args_list]
        self.assertNotIn("stop", invoked)
        self.assertNotIn("rm", invoked)
        self.assertNotIn("run", invoked)

    def test_failed_health_triggers_rollback_to_old_image(self):
        calls = []

        def fake_docker(args, check=True, timeout=900):
            calls.append(list(args))
            if args[0] == "container" and args[1] == "inspect":
                name = args[-1]
                payload = app_info() if name == "knowledge-mcp" else helper_info()
                return completed(json.dumps([payload]))
            return completed("")

        buffer = io.StringIO()
        with (
            patch.object(self_update, "docker", side_effect=fake_docker),
            patch.object(self_update, "image_id", side_effect=lambda ref: "sha256:new" if "latest" in ref else "sha256:old"),
            patch.object(self_update, "wait_for", return_value=(False, "unhealthy")),
            contextlib.redirect_stdout(buffer),
        ):
            raised = None
            try:
                self_update.run(["--force"])
            except self_update.UpdateError as exc:
                raised = exc
        self.assertIsNotNone(raised)
        self.assertIn("rolled back", str(raised))
        self.assertIn("Rolling back", buffer.getvalue())
        self.assertTrue(any(call[0] == "rm" and "-f" in call for call in calls))
        self.assertTrue(any(call[0] == "run" and "sha256:oldapp" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
