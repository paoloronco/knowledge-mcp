"""Document companion, started automatically by Docker Compose on Linux."""

import argparse
import getpass
import hashlib
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent / "mcp"))
from host_sync import allowed_file, host_root, cached_file_hash


DEFAULT_CONFIG = Path.home() / ".config" / "knowledge-mcp" / "agent.json"
LOG = logging.getLogger("knowledge-host-agent")


def request(base, token, route, payload=None, headers=None, timeout=120):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"X-Agent-Token": token, **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + route, data=body, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", error.reason)
        except (ValueError, OSError):
            message = error.reason
        raise RuntimeError(f"Dashboard returned {error.code}: {message}") from error


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as document:
        for block in iter(lambda: document.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_host_folder(root, host_mount=None):
    value = host_root(root)
    if host_mount is None:
        return Path(value).expanduser().resolve()
    if not value.startswith("/") or "\\" in value or ":" in value:
        raise ValueError("Enter an absolute Linux host path, such as /mnt/documents")
    mount = host_mount.resolve()
    pending = deque(PurePosixPath(value).parts[1:])
    resolved = []
    links = 0
    # Absolute symlinks refer to the host filesystem, not the agent's image.
    while pending:
        part = pending.popleft()
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        if part in ("", ".", "/"):
            continue
        candidate = mount.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError("Too many symlinks in the host folder path")
            target = PurePosixPath(os.readlink(candidate))
            if target.is_absolute():
                resolved = []
            pending.extendleft(reversed(target.parts))
        else:
            resolved.append(part)
    if not resolved or resolved[0] in ("proc", "sys", "dev", "run"):
        raise ValueError("Choose a document folder, not a host system directory")
    return mount.joinpath(*resolved)


def inventory(root, policy, host_mount=None, progress=None, folders=None, hash_cache=None):
    folder = resolve_host_folder(root, host_mount)
    if not folder.is_dir():
        raise ValueError(f"Host folder is unavailable: {root}")
    folder = folder.resolve()
    if folder == Path(folder.anchor):
        raise ValueError("Choose a folder, not a filesystem root")
    restricted = {x.casefold() for x in policy["exclude_directories"] + policy["exclude_top_level"]}
    logical = folder.relative_to(host_mount.resolve()) if host_mount is not None else folder
    original_parts = PurePosixPath(host_root(root)).parts if host_mount is not None else folder.parts
    if any(part.casefold() in restricted for part in (*logical.parts, *original_parts)):
        raise ValueError("The selected host folder is excluded by the indexing policy")
    files = {}
    hash_cache = {} if hash_cache is None else hash_cache
    checked = 0

    def unreadable(error):
        raise RuntimeError(f"Could not read host folder {error.filename}: {error.strerror}") from error

    for selected_name in folders if folders is not None else [""]:
        if not isinstance(selected_name, str) or "\\" in selected_name or selected_name.startswith("/") or (selected_name != "" and any(part in ("", ".", "..") for part in selected_name.split("/"))):
            raise ValueError("Invalid selected host folder")
        if any(part.casefold() in restricted for part in selected_name.split("/") if part):
            raise ValueError("The selected host folder is excluded by the indexing policy")
        selected = folder.joinpath(*selected_name.split("/")) if selected_name else folder
        if not selected.is_dir() or selected.is_symlink() or not selected.resolve().is_relative_to(folder):
            raise ValueError(f"Host folder is unavailable: {selected_name}")
        for base, dirs, names in os.walk(selected, followlinks=False, onerror=unreadable):
            current = Path(base)
            relative_dir = current.relative_to(folder)
            dirs[:] = sorted(name for name in dirs if not (current / name).is_symlink() and name.casefold() not in restricted and (relative_dir.parts or name.casefold() not in {x.casefold() for x in policy["exclude_top_level"]}))
            for name in sorted(names):
                path = current / name
                relative = path.relative_to(folder).as_posix()
                if relative in files:
                    continue
                checked += 1
                if path.is_symlink():
                    if progress:
                        progress(checked, len(files))
                    continue
                try:
                    size = path.stat().st_size
                    if allowed_file(relative, size, policy):
                        files[relative] = {"size": size, "sha256": cached_file_hash(path, hash_cache, file_hash)}
                except (OSError, ValueError) as error:
                    raise RuntimeError(f"Could not read {path}: {error}") from error
                if progress:
                    progress(checked, len(files))
    if progress:
        progress(checked, len(files), force=True)
    retained = {str(folder.joinpath(*name.split("/"))) for name in files}
    for name in set(hash_cache) - retained:
        del hash_cache[name]
    return folder, files


def sync(base, token, task, host_mount=None, hash_cache=None):
    last_report = 0
    last_phase = None
    checked = 0
    eligible = 0

    def report(phase, completed=0, total=0, force=False):
        nonlocal last_report, last_phase
        now = time.monotonic()
        if force or phase != last_phase or now - last_report >= 1:
            request(base, token, "/api/agent/progress", {"revision": task["revision"], "sync_request": task["sync_request"], "phase": phase, "checked": checked, "eligible": eligible, "completed": completed, "total": total})
            last_report = now
            last_phase = phase

    def scanned(count, found, force=False):
        nonlocal checked, eligible
        checked, eligible = count, found
        report("scanning", force=force)

    report("scanning")
    folder, files = inventory(task["host_root"], task["policy"], host_mount, scanned, task.get("folders"), hash_cache)
    report("planning")
    body = {"revision": task["revision"], "sync_request": task["sync_request"], "files": files}
    missing = request(base, token, "/api/agent/plan", body, timeout=1800)["missing"]
    report("transferring", total=len(missing))
    for completed, name in enumerate(missing, 1):
        document = folder.joinpath(*name.split("/"))
        if document.is_symlink() or not document.resolve().is_relative_to(folder):
            raise ValueError("Document changed to a symlink during sync")
        send_file(base, token, task["revision"], name, document, files[name])
        report("transferring", completed=completed, total=len(missing), force=completed == len(missing))
    report("finalizing")
    result = request(base, token, "/api/agent/commit", body, timeout=1800)
    LOG.info("Synced %s documents (%s transferred)", result["synced"], len(missing))


def send_file(base, token, revision, name, document, info):
    parsed = urllib.parse.urlsplit(base)
    connection = (http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection)(parsed.hostname, parsed.port, timeout=120)
    remaining = info["size"]
    digest = hashlib.sha256()
    try:
        connection.putrequest("POST", "/api/agent/file?path=" + urllib.parse.quote(name, safe=""))
        connection.putheader("X-Agent-Token", token)
        connection.putheader("X-Sync-Revision", str(revision))
        connection.putheader("X-File-Sha256", info["sha256"])
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(remaining))
        connection.endheaders()
        with document.open("rb") as source:
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise RuntimeError(f"Document changed during sync: {name}")
                connection.send(block)
                digest.update(block)
                remaining -= len(block)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200 or digest.hexdigest() != info["sha256"]:
            raise RuntimeError(f"Document transfer failed for {name}: {body.decode(errors='replace')}")
    finally:
        connection.close()


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Use only the dashboard origin, such as http://127.0.0.1:8080")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Use HTTPS unless the dashboard is on this host's loopback address")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid dashboard URL")
    return value.rstrip("/")


def heartbeat(base, token, done):
    while not done.wait(15):
        try:
            request(base, token, "/api/agent/task")
        except Exception:
            LOG.warning("Dashboard heartbeat failed")


def check_folder(base, token, probe, host_mount):
    try:
        folder = resolve_host_folder(probe["path"], host_mount)
        if not folder.is_dir():
            raise ValueError(f"Host folder is unavailable: {probe['path']}")
        with os.scandir(folder) as entries:
            next(entries, None)
        result = {"id": probe["id"], "reachable": True}
    except (OSError, ValueError) as error:
        result = {"id": probe["id"], "reachable": False, "error": str(error)}
    request(base, token, "/api/agent/folder-probe", result)


def run(config_path, once=False, scan_seconds=300, host_mount=None):
    last_signature = None
    last_scan = 0
    last_probe = None
    hash_cache = {}
    while True:
        task = None
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            base = validate_url(config["url"])
            token = config["token"]
            task = request(base, token, "/api/agent/task")
            probe = task.get("folder_probe")
            if probe and probe["id"] != last_probe:
                check_folder(base, token, probe, host_mount)
                last_probe = probe["id"]
            signature = (task["revision"], task["sync_request"], json.dumps(task["policy"], sort_keys=True))
            if signature != last_signature:
                hash_cache.clear()
            if task["source_mode"] == "host_agent" and task["host_root"] and task.get("scan_enabled", True) and not task.get("index_running") and (signature != last_signature or time.time() - last_scan >= scan_seconds):
                done = threading.Event()
                worker = threading.Thread(target=heartbeat, args=(base, token, done), daemon=True)
                worker.start()
                try:
                    sync(base, token, task, host_mount, hash_cache)
                finally:
                    done.set()
                    worker.join(timeout=1)
                last_signature = signature
                last_scan = time.time()
            if once:
                return
        except Exception as error:
            LOG.exception("Host sync failed; retrying")
            if task and task.get("source_mode") == "host_agent":
                try:
                    request(base, token, "/api/agent/error", {"revision": task["revision"], "sync_request": task["sync_request"], "error": str(error)})
                except Exception:
                    pass
            if once:
                raise
        time.sleep(3)


def install(url, config_path):
    base = validate_url(url)
    token = getpass.getpass("Pairing key from the dashboard: ").strip()
    if not token:
        raise ValueError("Pairing key is required")
    request(base, token, "/api/agent/task")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"url": base, "token": token}), encoding="utf-8")
    # Keep the service independent of a downloaded archive or repository checkout.
    runtime = config_path.resolve().parent / "runtime"
    (runtime / "mcp").mkdir(parents=True, exist_ok=True)
    for source, destination in ((Path(__file__).resolve(), runtime / "host_agent.py"), (Path(__file__).resolve().parent / "mcp" / "host_sync.py", runtime / "mcp" / "host_sync.py")):
        if source != destination:
            shutil.copyfile(source, destination)
    agent_script = runtime / "host_agent.py"
    if os.name == "posix":
        config_path.chmod(0o600)
        unit = Path.home() / ".config" / "systemd" / "user" / "knowledge-mcp-agent.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\nDescription=Knowledge MCP host document agent\nAfter=network-online.target\n\n[Service]\nExecStart=\"" + sys.executable + "\" \"" + str(agent_script) + "\" run --config \"" + str(config_path.resolve()) + "\"\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "knowledge-mcp-agent.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "knowledge-mcp-agent.service"], check=True)
        linger = subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True, text=True)
        if linger.returncode:
            print(f"To keep the agent running after logout and start it at boot, run: sudo loginctl enable-linger {getpass.getuser()}")
        print("Host agent installed and started. It runs under your user account.")
    elif os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else Path(sys.executable)
        command = f'"{executable}" "{agent_script}" run --config "{config_path.resolve()}"'
        subprocess.run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", "KnowledgeMCPHostAgent", "/TR", command, "/F"], check=True)
        subprocess.run(["schtasks", "/Run", "/TN", "KnowledgeMCPHostAgent"], check=True)
        print("Host agent installed and started. It restarts when you sign in.")
    else:
        print(f"Config saved at {config_path}. Run: {sys.executable} {agent_script} run --config {config_path.resolve()}")


def main():
    parser = argparse.ArgumentParser(description="Sync a host document folder selected in Knowledge MCP")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="Pair and install a per-user background service")
    setup.add_argument("--url", default="http://127.0.0.1:8080")
    setup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve = commands.add_parser("run", help="Run the background agent")
    serve.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--scan-seconds", type=int, default=300)
    serve.add_argument("--host-root", type=Path, help="Read-only Docker mount of the Linux host filesystem")
    serve.add_argument("--log-stdout", action="store_true", help="Write logs to Docker logs")
    args = parser.parse_args()
    if getattr(args, "log_stdout", False):
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    else:
        DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=DEFAULT_CONFIG.parent / "agent.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "install":
        install(args.url, args.config)
    else:
        run(args.config, args.once, args.scan_seconds, args.host_root)


if __name__ == "__main__":
    main()
