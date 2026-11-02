import ipaddress
import hashlib
import hmac
import json
import math
import ntpath
import os
import posixpath
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml
from host_sync import allowed_file, host_root, is_office_lock_file, relative_path, cached_file_hash
from embedding_models import DEFAULT_MODEL, MODELS
from gpu_probe import GPU_MONITOR
from policy_defaults import ensure_required_exclusions, remove_legacy_default_exclusions
from backup import MAX_UNPACKED_SIZE, create_backup, stage_restore, apply_restore


ROOT = Path(__file__).resolve().parents[1]
DATA = Path("/data")
MANAGED_SOURCE = DATA / "documents"
HOST_SOURCE = DATA / "host-documents"
SOURCE = Path(os.getenv("KNOWLEDGE_ROOT", str(MANAGED_SOURCE))).resolve()
CONFIG = DATA / "config.json"
AUTH = DATA / "auth.json"
AGENT_AUTH = DATA / "agent-auth.json"
AGENT_MANIFEST = DATA / "agent-manifest.json"
AUTO_AGENT_CONFIG = Path(os.environ["AUTO_HOST_AGENT_CONFIG"]) if os.getenv("AUTO_HOST_AGENT_CONFIG") else None
POLICY = DATA / "index-policy.yaml"
DEFAULT_POLICY = ROOT / "mcp" / "index-policy.yaml"
HTML = Path(__file__).with_name("webui.html")
SCRIPT = Path(__file__).with_name("app.js")
TOKEN = secrets.token_urlsafe(32)
SETUP_PATHS = ("/setup/login", "/setup/health", "/setup/folders", "/setup/policy", "/setup/eligible", "/setup/dry-run", "/setup/indexing")
DASHBOARD_PATHS = ("/dashboard", "/dashboard/indexing", "/dashboard/indexing/model", "/dashboard/folders", "/dashboard/policy", "/dashboard/settings")


def validate_source_root(value):
    if not isinstance(value, str) or not value or (os.name != "nt" and "\\" in value):
        raise ValueError("Enter an absolute document path inside the container")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Enter an absolute document path inside the container")
    path = path.resolve()
    protected = [ROOT, DATA, *(Path(name) for name in ("/qdrant", "/proc", "/sys", "/dev", "/etc", "/run", "/root", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64", "/boot"))]
    if path == Path(path.anchor) or any(path == base or path.is_relative_to(base) for base in protected if base != DATA):
        raise ValueError("This container system path cannot be indexed")
    if path.is_relative_to(DATA) and not path.is_relative_to(MANAGED_SOURCE) and not path.is_relative_to(HOST_SOURCE):
        raise ValueError("Application data cannot be indexed")
    if not path.is_dir():
        raise ValueError(f"{path} is not visible inside the container. Mount the host folder read-only at this path and recreate the container.")
    default = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    restricted = {name.casefold() for name in default["exclude_directories"] + default["exclude_top_level"]}
    if any(part.casefold() in restricted for part in path.parts):
        raise ValueError("This path is excluded by the indexing policy")
    return path


def source_dir(subfolder, root=None):
    if not isinstance(subfolder, str) or (os.name != "nt" and "\\" in subfolder):
        raise ValueError("Invalid folder")
    root = Path(root or SOURCE).resolve()
    path = Path(subfolder)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError(f"Choose an existing folder inside {root}")
    return path


def normalize_folders(values, root=None):
    if not isinstance(values, list) or not values or len(values) > 32:
        raise ValueError("Choose between 1 and 32 folders")
    folders = []
    for value in values:
        root = Path(root or SOURCE).resolve()
        path = source_dir(value, root)
        relative = path.relative_to(root).as_posix()
        if relative == ".":
            relative = ""
        if relative not in folders:
            folders.append(relative)
    return folders


def selected_file(name, folders):
    return any(not folder or name.startswith(folder.rstrip("/") + "/") for folder in folders)


def document_paths(config):
    root = config["host_root"] if config["source_mode"] == "host_agent" else config["source_root"]
    join = ntpath.join if "\\" in root or ":" in root else posixpath.join
    return [join(root, folder) if folder else root for folder in config["folders"]] if root else []


def source_dir_or_false(value, root=None):
    try:
        source_dir(value, root)
        return True
    except ValueError:
        return False


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as document:
        for block in iter(lambda: document.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def password_record(password):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000)
    return {"salt": salt.hex(), "hash": digest.hex()}


def validate_policy(content):
    policy = yaml.safe_load(content)
    default = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("Policy must be a YAML object")
    for key in ("exclude_directories", "exclude_top_level", "exclude_extensions"):
        actual = policy.get(key)
        if not isinstance(actual, list) or not all(isinstance(x, str) for x in actual):
            raise ValueError(f"{key} must be a list")
        required = set(default[key]) if key == "exclude_extensions" else {x.casefold() for x in default[key]}
        present = set(actual) if key == "exclude_extensions" else {x.casefold() for x in actual}
        if not required <= present:
            raise ValueError(f"Do not remove the default exclusions from {key}")
    if not isinstance(policy.get("include_extensions"), list) or not all(isinstance(x, str) for x in policy["include_extensions"]):
        raise ValueError("include_extensions must be a list")
    if not isinstance(policy.get("exclude_files", []), list) or not all(isinstance(x, str) and x and "\\" not in x and ".." not in Path(x).parts and not x.startswith("/") for x in policy.get("exclude_files", [])):
        raise ValueError("exclude_files must contain basenames or relative paths")
    supported = {".pdf", ".docx", ".pptx", ".md", ".txt", ".html", ".htm"}
    if not policy["include_extensions"] or any(x not in supported for x in policy["include_extensions"]):
        raise ValueError("include_extensions must use supported extensions")
    if type(policy.get("max_file_size_mb")) not in (int, float) or not math.isfinite(policy["max_file_size_mb"]) or not 0 < policy["max_file_size_mb"] <= 512:
        raise ValueError("max_file_size_mb must be between 0 and 512")


class Controller:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        if SOURCE == MANAGED_SOURCE:
            SOURCE.mkdir(parents=True, exist_ok=True)
        if not POLICY.exists():
            shutil.copyfile(DEFAULT_POLICY, POLICY)
        saved_policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
        migrated_policy = remove_legacy_default_exclusions(saved_policy)
        if ensure_required_exclusions(saved_policy) or migrated_policy:
            tmp = POLICY.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(saved_policy, sort_keys=False, allow_unicode=True), encoding="utf-8")
            tmp.replace(POLICY)
        self.lock = threading.RLock()
        first_start = not CONFIG.exists()
        environment_model = next((key for key, spec in MODELS.items() if spec["model"] == os.getenv("MODEL_NAME", MODELS[DEFAULT_MODEL]["model"]) and spec["collection"] == os.getenv("DENSE_COLLECTION", "documents")), "environment")
        self.config = {"source_selection": "auto", "source_mode": "host_agent" if first_start else "container", "host_root": "", "sync_revision": 0, "sync_request": 0, "scan_requested_revision": -1, "source_root": str(HOST_SOURCE if first_start else SOURCE), "folders": [""], "interval_hours": 0, "mcp_enabled": False, "qdrant_enabled": True, "last_run_at": 0, "onboarding_complete": not first_start, "initial_index_skipped": False, "setup_step": 0 if first_start else 7, "setup_flow_version": 2, "embedding_model": environment_model, "active_embedding_model": None, "legacy_embedding_model": environment_model}
        migrated = False
        if CONFIG.exists():
            stored = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.config.update(stored)
            if "embedding_model" not in stored:
                self.config["embedding_model"] = environment_model
                self.config["active_embedding_model"] = environment_model if stored.get("setup_step", 7) >= 7 and not stored.get("initial_index_skipped", False) else None
                migrated = True
            if "legacy_embedding_model" not in stored:
                self.config["legacy_embedding_model"] = environment_model
                migrated = True
            if "setup_step" not in stored and not self.config["onboarding_complete"]:
                self.config["setup_step"] = 1 if AUTH.exists() else 0
            migrated = migrated or "folders" not in stored or "source_selection" not in stored or "setup_flow_version" not in stored
            if "setup_flow_version" not in stored:
                previous_step = self.config["setup_step"]
                if self.config["onboarding_complete"]:
                    self.config["setup_step"] = 7 if previous_step >= 6 else 6
                elif previous_step >= 4:
                    self.config["setup_step"] = 4
                self.config["setup_flow_version"] = 2
            if "folders" not in stored:
                self.config["folders"] = [stored.get("subfolder", "")]
        if self.config["source_mode"] == "host_agent":
            HOST_SOURCE.mkdir(parents=True, exist_ok=True)
        try:
            selected_root = validate_source_root(self.config["source_root"])
            self.config["folders"] = normalize_folders(self.config["folders"], selected_root)
        except ValueError:
            # Keep the configured source when its read-only mount is temporarily absent.
            if not isinstance(self.config.get("folders"), list):
                self.config["folders"] = [""]
        self.config.pop("subfolder", None)
        if migrated:
            self._save()
        self.sessions = {}
        self.login_attempts = {}
        self.agent_seen_at = 0
        self.agent_error_text = ""
        self.sync_in_progress = False
        self.agent_progress = None
        self.local_scan = {"revision": -1, "running": False, "checked": 0, "eligible": 0, "files": [], "error": ""}
        self.probe_condition = threading.Condition(self.lock)
        self.folder_probe = None
        self.folder_probe_result = None
        self.probe_serial = 0
        self.agent_manifest = json.loads(AGENT_MANIFEST.read_text(encoding="utf-8")) if AGENT_MANIFEST.exists() else {"revision": -1, "files": {}}
        self.sync_hash_cache = {}
        self.stopping = False
        self.restart_requested = False
        self.backup_download = None
        self.qdrant = None
        self.mcp = None
        self.ingest = None
        self.ingest_dry_run = False
        self.ingest_rebuild = False
        self.ingest_model = None
        self.ingest_finalized = False
        result_file = DATA / "last-result.json"
        self.last_result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else None
        if AUTO_AGENT_CONFIG is not None:
            self.setup_auto_agent()
        if Path("/qdrant/qdrant").exists() and self.config["qdrant_enabled"]:
            self._start_qdrant()
        if self.config["mcp_enabled"] and (not Path("/qdrant/qdrant").exists() or self.config["qdrant_enabled"]):
            self._start_mcp()
        threading.Thread(target=self._schedule, daemon=True).start()

    def _save(self):
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config), encoding="utf-8")
        tmp.replace(CONFIG)

    def backup_roots(self):
        if not Path("/qdrant/qdrant").is_file():
            raise ValueError("Backup requires the bundled Docker Qdrant service")
        if os.getenv("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/") != "http://127.0.0.1:6333":
            raise ValueError("Backup does not support external Qdrant")
        if os.getenv("QDRANT__STORAGE__STORAGE_PATH", "/qdrant/storage") != "/qdrant/storage":
            raise ValueError("Backup requires the standard Qdrant storage path")
        return {"data": DATA, "qdrant": Path("/qdrant/storage"), "agent": AUTO_AGENT_CONFIG.parent} if AUTO_AGENT_CONFIG else {"data": DATA, "qdrant": Path("/qdrant/storage")}

    def qdrant_backup_version(self):
        return subprocess.check_output(["/qdrant/qdrant", "--version"], text=True, timeout=5).strip()

    def discard_backup_download(self):
        if self.backup_download:
            self.backup_download[1].unlink(missing_ok=True)
            self.backup_download = None

    def validate_backup_settings(self, stages):
        data = stages["data"]
        if (data / "config.json").stat().st_size > 1024 * 1024 or (data / "index-policy.yaml").stat().st_size > 1024 * 1024:
            raise ValueError("Backup settings are too large")
        restored = json.loads((data / "config.json").read_text(encoding="utf-8"))
        if not isinstance(restored, dict):
            raise ValueError("Backup settings must be an object")
        for key, value in self.config.items():
            actual = restored.get(key)
            if key == "active_embedding_model":
                valid = actual is None or isinstance(actual, str)
            elif type(value) in (int, float):
                valid = type(actual) in (int, float) and math.isfinite(actual)
            else:
                valid = value is None or type(actual) is type(value)
            if key not in restored or not valid:
                raise ValueError(f"Incompatible backup setting: {key}")
        for key in ("embedding_model", "legacy_embedding_model", "active_embedding_model"):
            if restored.get(key) not in (*MODELS, "environment", None) or key != "active_embedding_model" and restored.get(key) is None:
                raise ValueError("Unsupported embedding model in backup")
        if restored["source_mode"] not in ("container", "host_agent") or restored["source_selection"] not in ("auto", "container", "host_agent") or not 0 <= restored["interval_hours"] <= 720:
            raise ValueError("Invalid source or schedule in backup")
        if not isinstance(restored["folders"], list) or len(restored["folders"]) > 32 or not all(isinstance(folder, str) and (folder == "" or relative_path(folder)) for folder in restored["folders"]):
            raise ValueError("Invalid folders in backup")
        if any(type(restored[key]) is not int for key in ("sync_revision", "sync_request", "scan_requested_revision", "setup_step", "setup_flow_version")) or not 0 <= restored["setup_step"] <= 7 or restored["setup_flow_version"] != 2 or restored["last_run_at"] < 0:
            raise ValueError("Invalid indexing state in backup")
        validate_policy((data / "index-policy.yaml").read_text(encoding="utf-8"))
        for filename in ("auth.json", "agent-auth.json", "agent-manifest.json", "last-result.json"):
            path = data / filename
            if not path.exists():
                continue
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Backup metadata is too large")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError(f"Invalid backup metadata: {filename}")
            if filename == "auth.json" and any(not isinstance(record.get(key), str) or len(bytes.fromhex(record[key])) != size for key, size in (("salt", 16), ("hash", 32))):
                raise ValueError("Invalid dashboard credentials in backup")
            if filename == "agent-manifest.json" and (type(record.get("revision")) is not int or not isinstance(record.get("files"), dict) or not all(isinstance(item, dict) and type(item.get("size")) is int and isinstance(item.get("sha256"), str) for item in record["files"].values())):
                raise ValueError("Invalid synchronized document inventory in backup")
            if filename == "last-result.json" and (type(record.get("exit_code")) is not int or type(record.get("dry_run")) is not bool or type(record.get("finished_at")) not in (int, float)):
                raise ValueError("Invalid indexing result in backup")
        if "agent" in stages:
            saved = stages["agent"] / AUTO_AGENT_CONFIG.name
            if not saved.is_file() or saved.stat().st_size > 4096:
                raise ValueError("Backup is missing the host agent connection")
            connection = json.loads(saved.read_text(encoding="utf-8"))
            if not isinstance(connection, dict) or connection.get("url") != "http://127.0.0.1:8080" or not isinstance(connection.get("token"), str):
                raise ValueError("Invalid host agent connection in backup")
            if not (data / "agent-auth.json").is_file():
                raise ValueError("Backup is missing host agent credentials")
            expected = json.loads((data / "agent-auth.json").read_text(encoding="utf-8"))
            if expected.get("sha256") != hashlib.sha256(connection["token"].encode()).hexdigest():
                raise ValueError("Host agent credentials do not match the backup")

    @contextmanager
    def backup_pause(self):
        with self.lock:
            if self.sync_in_progress or self.local_scan["running"] or self.folder_probe is not None or self.ingest and self.ingest.poll() is None:
                raise ValueError("Wait until indexing, scanning and folder synchronization finish")
            if self.ingest and not self.ingest_finalized:
                self._finish_index(self.ingest, self.ingest_dry_run, self.ingest_model)
            self._save()
            qdrant_running = bool(self.qdrant and self.qdrant.poll() is None)
            mcp_running = bool(self.mcp and self.mcp.poll() is None)
            try:
                self._stop_mcp()
                self._stop_qdrant()
                yield
            finally:
                if not self.stopping:
                    if qdrant_running:
                        self._start_qdrant()
                    if mcp_running:
                        self._start_mcp()

    def password_enabled(self):
        return AUTH.exists()

    def set_password(self, password):
        record = password_record(password)
        with self.lock:
            tmp = AUTH.with_suffix(".tmp")
            tmp.write_text(json.dumps(record), encoding="utf-8")
            tmp.replace(AUTH)
            if os.name == "posix":
                AUTH.chmod(0o600)
            self.sessions.clear()
            if not self.config["onboarding_complete"] and self.config["setup_step"] < 1:
                self.config["setup_step"] = 1
                self._save()

    def check_password(self, password):
        if not isinstance(password, str) or not AUTH.exists():
            return False
        record = json.loads(AUTH.read_text(encoding="utf-8"))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(record["salt"]), 600000)
        return hmac.compare_digest(digest, bytes.fromhex(record["hash"]))

    def complete_onboarding(self):
        with self.lock:
            if self.config["setup_step"] < 6:
                raise ValueError("Complete the dry-run test before indexing")
            self.config["onboarding_complete"] = True
            self.config["setup_step"] = max(self.config["setup_step"], 6)
            self._save()

    def skip_initial_index(self):
        with self.lock:
            if self.config["setup_step"] < 6:
                raise ValueError("Complete the dry-run test before skipping initial indexing")
            if self.config["onboarding_complete"] and not self.config["initial_index_skipped"]:
                raise ValueError("Initial indexing setup is already complete")
            if self.ingest and self.ingest.poll() is None:
                raise ValueError("Wait until indexing finishes")
            self.config["onboarding_complete"] = True
            self.config["initial_index_skipped"] = True
            self.config["setup_step"] = 7
            self._save()

    def advance_setup(self, step):
        if type(step) is not int or not 1 <= step <= 6:
            raise ValueError("Invalid setup step")
        with self.lock:
            if not self.config["onboarding_complete"] and step > self.config["setup_step"]:
                if self.ingest and self.ingest.poll() is not None and not self.ingest_finalized:
                    self._finish_index(self.ingest, self.ingest_dry_run)
                if step != self.config["setup_step"] + 1:
                    raise ValueError("Complete the previous setup step first")
                if step == 3 and (not self.config["folders"] or self.config["source_mode"] == "host_agent" and not self.config["host_root"]):
                    raise ValueError("Add a reachable document folder first")
                if step == 5 and not self.scan_ready():
                    raise ValueError("Scan the selected folder and find eligible documents before the dry run")
                if step == 6 and not (self.last_result and self.last_result["dry_run"] and self.last_result["exit_code"] == 0):
                    raise ValueError("Complete a successful dry-run test first")
                self.config["setup_step"] = step
                self._save()
            return {"setup_step": self.config["setup_step"]}

    def invalidate_result(self):
        with self.lock:
            self.last_result = None
            (DATA / "last-result.json").unlink(missing_ok=True)
            (DATA / "index-progress.json").unlink(missing_ok=True)

    def _env(self, model_id=None, indexing=False):
        env = {**os.environ, "KNOWLEDGE_ROOT": self.config["source_root"], "INDEX_SOURCE_PATHS": json.dumps(self.config["folders"]), "POLICY_FILE": str(POLICY), "INGEST_PROGRESS_FILE": str(DATA / "index-progress.json"), "INGEST_STAGES_FILE": str(DATA / "index-stages.log"), "INGEST_RUN_ERRORS_FILE": str(DATA / "index-errors.jsonl")}
        chosen = model_id or self.config["active_embedding_model"] or self.config["embedding_model"]
        if chosen in MODELS:
            spec = MODELS[chosen]
            env.update(MODEL_NAME=spec["model"], DENSE_COLLECTION=spec["collection"])
            if indexing:
                env.setdefault("EMBED_BATCH_SIZE", str(spec["batch_size"]))
            if indexing and chosen != self.config["legacy_embedding_model"]:
                base = Path(os.getenv("INGESTION_BASE_DIR", str(ROOT / ".state")))
                env["INGESTION_BASE_DIR"] = str(base / "models" / chosen)
        return env

    def select_embedding_model(self, model_id):
        if model_id not in MODELS:
            raise ValueError("Choose a supported embedding model")
        with self.lock:
            if self.ingest and self.ingest.poll() is None:
                raise ValueError("Wait until indexing finishes before changing the model")
            self.config["embedding_model"] = model_id
            self._save()
            return dict(self.config)

    def agent_pair(self):
        token = secrets.token_urlsafe(48)
        with self.lock:
            tmp = AGENT_AUTH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sha256": hashlib.sha256(token.encode()).hexdigest()}), encoding="utf-8")
            tmp.replace(AGENT_AUTH)
            if os.name == "posix":
                AGENT_AUTH.chmod(0o600)
            if AUTO_AGENT_CONFIG is not None:
                self.save_auto_agent(token)
            self.agent_seen_at = 0
            self.agent_progress = None
        return {"token": token}

    def save_auto_agent(self, token):
        AUTO_AGENT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = AUTO_AGENT_CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": "http://127.0.0.1:8080", "token": token}), encoding="utf-8")
        if os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(AUTO_AGENT_CONFIG)

    def setup_auto_agent(self):
        with self.lock:
            if AUTO_AGENT_CONFIG.exists():
                saved = json.loads(AUTO_AGENT_CONFIG.read_text(encoding="utf-8"))
                if self.agent_authenticated(saved.get("token")):
                    return
            token = secrets.token_urlsafe(48)
            tmp = AGENT_AUTH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sha256": hashlib.sha256(token.encode()).hexdigest()}), encoding="utf-8")
            if os.name == "posix":
                tmp.chmod(0o600)
            tmp.replace(AGENT_AUTH)
            self.save_auto_agent(token)

    def agent_authenticated(self, token):
        if not isinstance(token, str) or not AGENT_AUTH.exists():
            return False
        expected = json.loads(AGENT_AUTH.read_text(encoding="utf-8"))["sha256"]
        return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)

    def agent_task(self):
        with self.lock:
            self.agent_seen_at = time.time()
            return {"source_mode": self.config["source_mode"], "host_root": self.config["host_root"], "folders": self.config["folders"], "revision": self.config["sync_revision"], "sync_request": self.config["sync_request"], "index_running": bool(self.ingest and self.ingest.poll() is None), "scan_enabled": self.config["onboarding_complete"] or self.config["scan_requested_revision"] == self.config["sync_revision"], "folder_probe": self.folder_probe, "policy": yaml.safe_load(POLICY.read_text(encoding="utf-8"))}

    def check_folder(self, path, selection):
        if selection not in ("auto", "host_agent", "container"):
            raise ValueError("Invalid document location")
        candidate = host_root(path)
        mode = ("host_agent" if AUTO_AGENT_CONFIG is not None or not Path(candidate).is_dir() else "container") if selection == "auto" else selection
        if mode == "container":
            validate_source_root(candidate)
            return {"reachable": True, "mode": mode}
        with self.probe_condition:
            if time.time() - self.agent_seen_at >= 60:
                raise ValueError("Host agent unavailable. Check its Docker service")
            if self.folder_probe is not None:
                raise ValueError("Another folder check is in progress")
            self.probe_serial += 1
            probe_id = self.probe_serial
            self.folder_probe = {"id": probe_id, "path": candidate}
            self.folder_probe_result = None
            try:
                if not self.probe_condition.wait_for(lambda: self.folder_probe_result is not None, timeout=12):
                    raise ValueError("Host folder check timed out. Check the host agent")
                if not self.folder_probe_result.get("reachable"):
                    raise ValueError(self.folder_probe_result.get("error") or "Host folder unavailable")
                return {"reachable": True, "mode": mode}
            finally:
                self.folder_probe = None
                self.folder_probe_result = None

    def report_folder_probe(self, values):
        with self.probe_condition:
            if self.folder_probe and values.get("id") == self.folder_probe["id"]:
                reachable = values.get("reachable") is True
                self.folder_probe_result = {"reachable": reachable, "error": str(values.get("error", ""))[:512]}
                self.probe_condition.notify_all()
            return {"accepted": self.folder_probe_result is not None}

    def scan_ready(self):
        if self.config["source_mode"] == "host_agent":
            return self.host_source_ready() and bool(self.agent_manifest.get("files"))
        scan = self.local_scan
        return scan["revision"] == self.config["sync_revision"] and not scan["running"] and not scan["error"] and scan["eligible"] > 0

    def scan_files(self):
        with self.lock:
            if self.config["source_mode"] == "host_agent":
                if not self.host_source_ready():
                    raise ValueError("Finish the host folder scan first")
                return sorted(self.agent_manifest.get("files", {}))
            scan = self.local_scan
            if scan["revision"] != self.config["sync_revision"] or scan["running"] or scan["error"]:
                raise ValueError("Finish the document scan first")
            return list(scan["files"])

    def start_scan(self):
        with self.lock:
            if self.config["source_mode"] == "host_agent":
                return self.request_agent_sync()
            if self.local_scan["running"]:
                return {"requested": True}
            root = validate_source_root(self.config["source_root"])
            folders = normalize_folders(self.config["folders"], root)
            policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
            revision = self.config["sync_revision"]
            self.local_scan = {"revision": revision, "running": True, "checked": 0, "eligible": 0, "files": [], "error": ""}
            threading.Thread(target=self._scan_local, args=(root, folders, policy, revision), daemon=True).start()
            return {"requested": True}

    def _scan_local(self, root, folders, policy, revision):
        checked, files = 0, []
        excluded_dirs = {name.casefold() for name in policy["exclude_directories"]}
        excluded_top = {name.casefold() for name in policy["exclude_top_level"]}
        last_report = time.monotonic()
        try:
            def unreadable(error):
                raise RuntimeError(f"Could not read document folder {error.filename}: {error.strerror}") from error
            for folder in folders:
                selected = root / folder
                for base, dirs, names in os.walk(selected, followlinks=False, onerror=unreadable):
                    current = Path(base)
                    relative_dir = current.relative_to(root)
                    dirs[:] = sorted(name for name in dirs if not (current / name).is_symlink() and name.casefold() not in excluded_dirs and (relative_dir.parts or name.casefold() not in excluded_top))
                    for name in sorted(names):
                        document = current / name
                        checked += 1
                        if not document.is_symlink():
                            relative = document.relative_to(root).as_posix()
                            if allowed_file(relative, document.stat().st_size, policy):
                                files.append(relative)
                        if time.monotonic() - last_report >= 0.25:
                            with self.lock:
                                if revision != self.config["sync_revision"]:
                                    return
                                self.local_scan.update(checked=checked, eligible=len(files))
                            last_report = time.monotonic()
            with self.lock:
                if revision == self.config["sync_revision"]:
                    self.local_scan.update(running=False, checked=checked, eligible=len(files), files=sorted(set(files)))
        except (OSError, ValueError, RuntimeError) as error:
            with self.lock:
                if revision == self.config["sync_revision"]:
                    self.local_scan.update(running=False, checked=checked, eligible=len(files), files=sorted(set(files)), error=str(error)[:512])

    def request_agent_sync(self):
        with self.lock:
            if self.config["source_mode"] != "host_agent" or not self.config["host_root"]:
                raise ValueError("Select a host folder first")
            self.config["sync_request"] += 1
            self.config["scan_requested_revision"] = self.config["sync_revision"]
            self.agent_error_text = ""
            self.agent_progress = None
            self._save()
            return {"requested": True, "sync_request": self.config["sync_request"]}

    def report_agent_progress(self, values):
        phase = values.get("phase")
        checked = values.get("checked")
        completed = values.get("completed")
        total = values.get("total")
        eligible = values.get("eligible", 0)
        if phase not in ("scanning", "planning", "transferring", "finalizing") or any(type(value) is not int or value < 0 for value in (checked, completed, total, eligible)) or completed > total:
            raise ValueError("Invalid host sync progress")
        with self.lock:
            if values.get("revision") != self.config["sync_revision"] or values.get("sync_request") != self.config["sync_request"] or self.config["source_mode"] != "host_agent":
                return {"accepted": False}
            if phase == "scanning" and checked == 0:
                self.agent_error_text = ""
            self.agent_progress = {"phase": phase, "checked": checked, "eligible": eligible, "completed": completed, "total": total}
            self.agent_seen_at = time.time()
            self.sync_in_progress = True
            return {"accepted": True}

    def agent_error(self, values):
        with self.lock:
            if values.get("revision") == self.config["sync_revision"] and values.get("sync_request", self.config["sync_request"]) == self.config["sync_request"]:
                self.agent_error_text = str(values.get("error", "Host sync failed"))[:512]
                self.sync_in_progress = False
                self.agent_progress = None
            return {"recorded": True}

    def agent_plan(self, values):
        files = values.get("files")
        revision = values.get("revision")
        request_id = values.get("sync_request")
        if not isinstance(files, dict) or len(files) > 50000:
            raise ValueError("Invalid document inventory")
        # Accept inventories from an older companion while dropping Office lock
        # files. The newer companion excludes them before uploading.
        files = {name: item for name, item in files.items() if not is_office_lock_file(relative_path(name).name)}
        with self.lock:
            if self.config["source_mode"] != "host_agent" or not self.config["host_root"] or revision != self.config["sync_revision"]:
                raise ValueError("Host folder changed; retry the sync")
            if type(request_id) is not int or not 0 <= request_id <= self.config["sync_request"]:
                raise ValueError("Invalid host sync request")
            if self.ingest and self.ingest.poll() is None:
                raise ValueError("Wait until indexing finishes before syncing")
            policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
            for name, item in files.items():
                if not isinstance(item, dict) or not selected_file(name, self.config["folders"]) or not allowed_file(name, item.get("size"), policy) or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in item["sha256"]):
                    raise ValueError("Invalid or excluded document in inventory")
            self.sync_in_progress = True
            known = self.agent_manifest.get("files", {})
            missing = [name for name, item in files.items() if known.get(name) != item or not (HOST_SOURCE / name).is_file() or (HOST_SOURCE / name).is_symlink() or cached_file_hash(HOST_SOURCE / name, self.sync_hash_cache, file_sha256) != item["sha256"]]
            return {"missing": missing}

    def agent_file(self, name, revision, size, digest, stream):
        path = relative_path(name)
        with self.lock:
            policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
            if self.config["source_mode"] != "host_agent" or revision != self.config["sync_revision"] or not self.sync_in_progress:
                raise ValueError("No active host sync")
            if not selected_file(name, self.config["folders"]) or not allowed_file(name, size, policy) or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid or excluded document")
            destination = HOST_SOURCE.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != HOST_SOURCE and parent.is_relative_to(HOST_SOURCE)):
                raise ValueError("Symlink in document destination")
            temp = destination.with_name(destination.name + ".sync-" + secrets.token_hex(8))
            actual = hashlib.sha256()
            remaining = size
            try:
                with temp.open("xb") as output:
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Incomplete document upload")
                        output.write(chunk)
                        actual.update(chunk)
                        remaining -= len(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError("Document checksum mismatch")
                temp.replace(destination)
            finally:
                temp.unlink(missing_ok=True)

    def agent_commit(self, values):
        files = values.get("files")
        revision = values.get("revision")
        request_id = values.get("sync_request")
        if not isinstance(files, dict) or len(files) > 50000:
            raise ValueError("Invalid document inventory")
        files = {name: item for name, item in files.items() if not is_office_lock_file(relative_path(name).name)}
        with self.lock:
            if not self.sync_in_progress or revision != self.config["sync_revision"] or self.config["source_mode"] != "host_agent":
                raise ValueError("No active host sync")
            if type(request_id) is not int or not 0 <= request_id <= self.config["sync_request"]:
                raise ValueError("Invalid host sync request")
            policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
            for name, item in files.items():
                if not isinstance(item, dict) or not selected_file(name, self.config["folders"]) or not allowed_file(name, item.get("size"), policy) or not isinstance(item.get("sha256"), str):
                    raise ValueError("Invalid document inventory")
                path = HOST_SOURCE / name
                if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"] or cached_file_hash(path, self.sync_hash_cache, file_sha256) != item["sha256"]:
                    raise ValueError(f"Document sync incomplete: {name}")
            for base, dirs, names in os.walk(HOST_SOURCE, topdown=False, followlinks=False):
                folder = Path(base)
                for name in names:
                    path = folder / name
                    if not path.is_symlink() and path.relative_to(HOST_SOURCE).as_posix() not in files:
                        path.unlink()
                for name in dirs:
                    path = folder / name
                    if not path.is_symlink() and not any(path.iterdir()):
                        path.rmdir()
            for selected in self.config["folders"]:
                if selected:
                    destination = HOST_SOURCE.joinpath(*selected.split("/"))
                    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents if parent != HOST_SOURCE and parent.is_relative_to(HOST_SOURCE)):
                        raise ValueError("Symlink in selected document folder")
                    destination.mkdir(parents=True, exist_ok=True)
            tmp = AGENT_MANIFEST.with_suffix(".tmp")
            synced_at = time.time()
            checked = (self.agent_progress or {}).get("checked", 0)
            tmp.write_text(json.dumps({"revision": revision, "sync_request": request_id, "files": files, "checked": checked, "synced_at": synced_at}), encoding="utf-8")
            tmp.replace(AGENT_MANIFEST)
            self.agent_manifest = {"revision": revision, "sync_request": request_id, "files": files, "checked": checked, "synced_at": synced_at}
            retained = {str(HOST_SOURCE / name) for name in files}
            for name in set(self.sync_hash_cache) - retained:
                del self.sync_hash_cache[name]
            self.sync_in_progress = False
            self.agent_error_text = ""
            if request_id == self.config["sync_request"]:
                self.agent_progress = None
            return {"synced": len(files)}

    def _start_qdrant(self):
        if self.qdrant and self.qdrant.poll() is None:
            return
        self.qdrant = subprocess.Popen(["/qdrant/qdrant"], cwd="/qdrant", env=os.environ.copy())

    def _stop_qdrant(self):
        if self.qdrant and self.qdrant.poll() is None:
            self.qdrant.terminate()
            try:
                self.qdrant.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.qdrant.kill()
                self.qdrant.wait()
        self.qdrant = None

    def service(self, name, action):
        if name not in ("qdrant", "mcp") or action not in ("start", "stop", "restart"):
            raise ValueError("Invalid service action")
        with self.lock:
            if name == "qdrant":
                if not Path("/qdrant/qdrant").exists():
                    raise ValueError("Qdrant is managed externally in this installation")
                if self.ingest and self.ingest.poll() is None:
                    raise ValueError("Wait until indexing finishes")
                self.config["qdrant_enabled"] = action != "stop"
                self._save()
                if action in ("stop", "restart"):
                    self._stop_mcp()
                    self._stop_qdrant()
                if action in ("start", "restart"):
                    self._start_qdrant()
                    if self.config["mcp_enabled"]:
                        self._start_mcp()
            else:
                if action in ("start", "restart") and self.ingest and self.ingest.poll() is None:
                    raise ValueError("Wait until indexing finishes before starting MCP")
                if action in ("stop", "restart"):
                    self._stop_mcp()
                self.config["mcp_enabled"] = action != "stop"
                self._save()
                if action in ("start", "restart"):
                    self._start_mcp()
        return {"ok": True}

    def _start_mcp(self):
        if self.mcp and self.mcp.poll() is None:
            return
        with (DATA / "mcp.log").open("a", encoding="utf-8") as log:
            self.mcp = subprocess.Popen([sys.executable, "mcp/server.py"], cwd=ROOT, env=self._env(), stdout=log, stderr=subprocess.STDOUT)

    def _stop_mcp(self):
        if self.mcp and self.mcp.poll() is None:
            self.mcp.terminate()
            try:
                self.mcp.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mcp.kill()
                self.mcp.wait()
        self.mcp = None

    def update(self, values):
        selection = values.get("source_selection", self.config["source_selection"])
        requested_paths = values.get("document_paths")
        if requested_paths is not None:
            if not isinstance(requested_paths, list) or len(requested_paths) > 32 or any(not isinstance(path, str) for path in requested_paths):
                raise ValueError("Choose up to 32 document folders")
            if not requested_paths:
                if selection not in ("auto", "host_agent", "container"):
                    raise ValueError("Invalid document location")
                with self.lock:
                    if self.ingest and self.ingest.poll() is None:
                        raise ValueError("Wait until indexing finishes before changing folders")
                    self.config.update(source_selection=selection, host_root="", folders=[], sync_revision=self.config["sync_revision"] + 1, scan_requested_revision=-1)
                    self.sync_in_progress = False
                    self.agent_progress = None
                    self.agent_error_text = ""
                    self.local_scan = {"revision": -1, "running": False, "checked": 0, "eligible": 0, "files": [], "error": ""}
                    self.invalidate_result()
                    if not self.config["onboarding_complete"]:
                        self.config["setup_step"] = min(self.config["setup_step"], 2)
                    self._stop_mcp()
                    self._save()
                    return dict(self.config)
            clean_paths = list(dict.fromkeys(host_root(path) for path in requested_paths))
            paths = ntpath if "\\" in clean_paths[0] or ":" in clean_paths[0] else posixpath
            try:
                common = paths.commonpath(clean_paths)
            except ValueError as error:
                raise ValueError("Document folders must share a non-root parent directory") from error
            values = {**values, "document_root": common, "folders": ["" if path == common else paths.relpath(path, common).replace("\\", "/") for path in clean_paths]}
        if "document_root" in values:
            chosen = host_root(values["document_root"])
            visible = Path(chosen).is_absolute() and Path(chosen).exists()
            mode = ("host_agent" if AUTO_AGENT_CONFIG is not None or not visible else "container") if selection == "auto" else selection
            values = {**values, "host_root" if mode == "host_agent" else "source_root": chosen}
        else:
            # Retain compatibility with clients that explicitly select a source mode.
            mode = values.get("source_mode", self.config["source_mode"])
            if "source_mode" in values:
                selection = mode
        if selection not in ("auto", "host_agent", "container"):
            raise ValueError("Invalid document location")
        if mode not in ("host_agent", "container"):
            raise ValueError("Invalid document source mode")
        if mode == "host_agent":
            selected_host = host_root(values.get("host_root", self.config["host_root"]))
            selected_root = HOST_SOURCE
            selected_root.mkdir(parents=True, exist_ok=True)
            folders = values.get("folders", self.config["folders"])
            if not isinstance(folders, list) or not folders or len(folders) > 32 or any(not isinstance(folder, str) or "\\" in folder or folder.startswith("/") or any(part in ("", ".", "..") for part in folder.split("/")) and folder != "" for folder in folders):
                raise ValueError("Invalid selected host folders")
            folders = list(dict.fromkeys(folders))
        else:
            selected_host = ""
            selected_root = validate_source_root(values.get("source_root", self.config["source_root"]))
            if selected_root.is_relative_to(HOST_SOURCE):
                raise ValueError("The host sync copy cannot be selected as a mounted folder")
            folders = normalize_folders(values.get("folders", [values["subfolder"]] if "subfolder" in values else self.config["folders"]), selected_root)
        interval = values.get("interval_hours", self.config["interval_hours"])
        if type(interval) is not int or not 0 <= interval <= 720:
            raise ValueError("Interval must be between 0 and 720 hours")
        enabled = values.get("mcp_enabled", self.config["mcp_enabled"])
        if type(enabled) is not bool:
            raise ValueError("Invalid MCP state")
        with self.lock:
            root_changed = str(selected_root) != self.config["source_root"] or selected_host != self.config["host_root"] or mode != self.config["source_mode"] or folders != self.config["folders"]
            interval_changed = interval != self.config["interval_hours"]
            if root_changed and self.ingest and self.ingest.poll() is None:
                raise ValueError("Wait until indexing finishes before changing folders")
            self.config.update(source_selection=selection, source_mode=mode, host_root=selected_host, source_root=str(selected_root), folders=folders, interval_hours=interval, mcp_enabled=enabled)
            if root_changed:
                self.config["sync_revision"] += 1
                self.sync_in_progress = False
                self.agent_error_text = ""
                self.agent_progress = None
                self.local_scan = {"revision": -1, "running": False, "checked": 0, "eligible": 0, "files": [], "error": ""}
            if root_changed:
                self.invalidate_result()
                if not self.config["onboarding_complete"]:
                    self.config["setup_step"] = min(self.config["setup_step"], 2)
            if interval_changed:
                self.config["last_run_at"] = time.time()
            self._save()
            if not enabled or root_changed:
                self._stop_mcp()
            if enabled:
                self._start_mcp()
            return dict(self.config)

    def run_index(self, dry_run=False, rebuild=False):
        with self.lock:
            if dry_run and rebuild:
                raise ValueError("A rebuild cannot be a dry run")
            if self.stopping:
                raise ValueError("Application is restarting")
            if self.ingest and self.ingest.poll() is None:
                raise ValueError("Indexing is already running")
            if self.config["source_mode"] == "host_agent" and not self.host_source_ready():
                raise ValueError("Wait for the host agent to finish syncing documents")
            if not self.config["onboarding_complete"] and not self.scan_ready():
                raise ValueError("Complete the document scan and find eligible documents before indexing")
            if self.config["source_mode"] == "host_agent" and not self.agent_manifest.get("files"):
                raise ValueError("No eligible documents found. Review the document folder and indexing policy")
            selected_root = validate_source_root(self.config["source_root"])
            normalize_folders(self.config["folders"], selected_root)
            self.invalidate_result()
            (DATA / "index-stages.log").write_text("", encoding="utf-8")
            (DATA / "index-errors.jsonl").write_text("", encoding="utf-8")
            args = [sys.executable, "ingestion/ingest.py"]
            if dry_run:
                args += ["--dry-run", "--limit", "10"]
            if rebuild:
                args += ["--rebuild"]
            self.ingest_model = self.config["embedding_model"]
            if rebuild:
                self._stop_mcp()
            try:
                with (DATA / "ingest.log").open("w", encoding="utf-8") as log:
                    self.ingest = subprocess.Popen(args, cwd=ROOT, env=self._env(self.ingest_model, indexing=True), stdout=log, stderr=subprocess.STDOUT)
            except OSError:
                if rebuild and self.config["mcp_enabled"]:
                    self._start_mcp()
                raise
            self.ingest_dry_run = dry_run
            self.ingest_rebuild = rebuild
            self.ingest_finalized = False
            if not dry_run:
                self.config["last_run_at"] = time.time()
                self._save()
            threading.Thread(target=self._finish_index, args=(self.ingest, dry_run, self.ingest_model), daemon=True).start()

    def _finish_index(self, process, dry_run, model_id=None):
        code = process.wait()
        with self.lock:
            if self.ingest is not None and self.ingest is not process:
                return
            if self.ingest is process and self.ingest_finalized:
                return
            if self.ingest is process:
                self.ingest_finalized = True
            previous_model = self.config.get("active_embedding_model")
            self.last_result = {"exit_code": code, "dry_run": dry_run, "finished_at": time.time()}
            if code == 0 and not dry_run:
                self.config["active_embedding_model"] = model_id or self.ingest_model or self.config["embedding_model"]
                self._save()
            if code == 0 and not dry_run and self.config["setup_step"] >= 6:
                self.config["setup_step"] = 7
                self.config["initial_index_skipped"] = False
                self._save()
            result_file = DATA / "last-result.json"
            tmp = result_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.last_result), encoding="utf-8")
            tmp.replace(result_file)
            completed_model = model_id or self.ingest_model or self.config["embedding_model"]
            if not dry_run and self.config["mcp_enabled"] and not self.stopping and (self.ingest_rebuild or code == 0 and completed_model != previous_model):
                self._stop_mcp()
                self._start_mcp()

    def _restart_failed_services(self):
        bundled_qdrant = Path("/qdrant/qdrant").exists()
        if self.config["qdrant_enabled"] and bundled_qdrant and (self.qdrant is None or self.qdrant.poll() is not None):
            self._start_qdrant()
        if self.config["mcp_enabled"] and (not bundled_qdrant or self.config["qdrant_enabled"]) and (self.mcp is None or self.mcp.poll() is not None) and not (self.ingest and self.ingest.poll() is None):
            self._start_mcp()

    def _schedule(self):
        while not self.stopping:
            time.sleep(30)
            with self.lock:
                if self.stopping:
                    return
                if self.backup_download and self.backup_download[2] < time.time():
                    self.discard_backup_download()
                # Docker restarts this container when the Web UI exits. Keep the
                # separately managed child services running after their own crash.
                try:
                    self._restart_failed_services()
                except OSError as error:
                    print(f"Could not restart a service: {error}", file=sys.stderr, flush=True)
                hours = self.config["interval_hours"] if self.config["onboarding_complete"] else 0
                due_at = self.config["last_run_at"] + hours * 3600 if hours else 0
                due = hours and time.time() >= due_at
                if due and self.config["source_mode"] == "host_agent":
                    due = self.agent_manifest.get("synced_at", 0) >= due_at
                running = self.ingest and self.ingest.poll() is None
            if due and not running:
                try:
                    self.run_index()
                except (OSError, ValueError):
                    pass

    def index_errors(self):
        run_file = DATA / "index-errors.jsonl"
        if run_file.exists():
            entries = []
            for line in run_file.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and isinstance(event.get("source"), str):
                        entries.append(event)
                except ValueError:
                    continue
            origin = "current"
        elif (DATA / "ingest.log").exists():
            # An older image kept only a model-specific, append-only errors.log.
            base = Path(os.getenv("INGESTION_BASE_DIR", str(ROOT / ".state")))
            candidates = [base / "logs" / "errors.log", *(base / "models").glob("*/logs/errors.log")]
            existing = [path for path in candidates if path.is_file()]
            entries = []
            if existing:
                latest = max(existing, key=lambda path: path.stat().st_mtime)
                for line in latest.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]:
                    parts = line.split("\t", 2)
                    if len(parts) != 3:
                        continue
                    timestamp, path, detail = parts
                    try:
                        source = Path(path).resolve().relative_to(Path(self.config["source_root"]).resolve()).as_posix()
                    except ValueError:
                        source = path
                    entries.append({"at": timestamp, "stage": "indexing", "source": source, "error_type": detail.split("(", 1)[0], "message": detail})
            origin = "legacy"
        else:
            entries, origin = [], "none"
        excluded = {name.casefold() for name in yaml.safe_load(POLICY.read_text(encoding="utf-8")).get("exclude_files", [])}
        for entry in entries:
            entry["auto_excluded"] = is_office_lock_file(entry["source"].rsplit("/", 1)[-1])
            try:
                relative_path(entry["source"])
                entry["can_ignore"] = not entry["auto_excluded"]
            except (KeyError, ValueError):
                entry["can_ignore"] = False
            entry["ignored"] = entry["source"].casefold() in excluded
        return entries, origin

    def status(self):
        try:
            with urllib.request.urlopen(os.getenv("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/") + "/readyz", timeout=2) as response:
                qdrant = response.status == 200
        except Exception:
            qdrant = False
        try:
            with socket.create_connection(("127.0.0.1", int(os.getenv("MCP_PORT", "8000"))), timeout=1):
                mcp_reachable = True
        except OSError:
            mcp_reachable = False
        with self.lock:
            if self.ingest and self.ingest.poll() is not None and not self.ingest_finalized:
                self._finish_index(self.ingest, self.ingest_dry_run)
            try:
                source_root = validate_source_root(self.config["source_root"])
                source_ready = bool(self.config["folders"]) and all(source_dir_or_false(folder, source_root) for folder in self.config["folders"])
                if self.config["source_mode"] == "host_agent":
                    source_ready = source_ready and self.host_source_ready()
            except ValueError:
                source_ready = False
            host_mode = self.config["source_mode"] == "host_agent"
            if host_mode:
                scan_complete = self.host_source_ready()
                scan_running = self.sync_in_progress or (self.config["scan_requested_revision"] == self.config["sync_revision"] and self.config["sync_request"] > self.agent_manifest.get("sync_request", -1) and not self.agent_error_text)
                scan_checked = self.agent_manifest.get("checked", 0) if scan_complete else (self.agent_progress or {}).get("checked", 0)
                scan_eligible = len(self.agent_manifest.get("files", {})) if scan_complete else (self.agent_progress or {}).get("eligible", 0)
                scan_preview = sorted(self.agent_manifest.get("files", {}))[:20] if scan_complete else []
                scan_error = self.agent_error_text
            else:
                scan = self.local_scan
                scan_complete = scan["revision"] == self.config["sync_revision"] and not scan["running"] and not scan["error"]
                scan_running = scan["running"] and scan["revision"] == self.config["sync_revision"]
                scan_checked, scan_eligible, scan_preview, scan_error = scan["checked"], scan["eligible"], scan["files"][:20] if scan_complete else [], scan["error"]
            progress_file = DATA / "index-progress.json"
            try:
                index_progress = json.loads(progress_file.read_text(encoding="utf-8")) if progress_file.exists() else None
            except (OSError, ValueError):
                index_progress = None
            log = DATA / "ingest.log"
            tail = ""
            if log.exists():
                with log.open("rb") as output:
                    output.seek(0, 2)
                    output.seek(max(0, output.tell() - 12000))
                    tail = output.read().decode("utf-8", errors="replace")
            index_errors, error_origin = self.index_errors()
            return {
                "config": dict(self.config),
                "embedding_models": MODELS,
                "gpu": GPU_MONITOR.status(),
                "embedding_device_preference": os.getenv("EMBEDDING_DEVICE", "auto"),
                "document_paths": document_paths(self.config),
                "app_ready": True,
                "qdrant_ready": qdrant,
                "qdrant_managed": Path("/qdrant/qdrant").exists(),
                "qdrant_running": bool(self.qdrant and self.qdrant.poll() is None) if Path("/qdrant/qdrant").exists() else qdrant,
                "mcp_running": bool(self.mcp and self.mcp.poll() is None and mcp_reachable),
                "index_running": bool(self.ingest and self.ingest.poll() is None),
                "source_ready": source_ready,
                "scan_complete": scan_complete,
                "scan_ready": scan_complete and scan_eligible > 0,
                "scan_running": scan_running,
                "scan_checked": scan_checked,
                "scan_eligible_count": scan_eligible,
                "scan_eligible_preview": scan_preview,
                "scan_error": scan_error,
                "agent_connected": time.time() - self.agent_seen_at < 60,
                "agent_paired": AGENT_AUTH.exists(),
                "agent_managed": AUTO_AGENT_CONFIG is not None,
                "agent_synced": self.agent_manifest.get("revision") == self.config["sync_revision"] and self.agent_manifest.get("sync_request", -1) >= self.config["sync_request"] and not self.sync_in_progress,
                "agent_syncing": self.sync_in_progress,
                "agent_progress": self.agent_progress,
                "agent_error": self.agent_error_text,
                "agent_last_sync_at": self.agent_manifest.get("synced_at"),
                "agent_file_count": len(self.agent_manifest.get("files", {})) if self.agent_manifest.get("revision") == self.config["sync_revision"] else 0,
                "agent_sync_request_completed": self.agent_manifest.get("sync_request", -1),
                "source_root": self.config["source_root"],
                "last_result": self.last_result,
                "index_progress": index_progress,
                "index_errors": index_errors[:25],
                "index_error_count": len(index_errors),
                "index_error_origin": error_origin,
                "index_log_available": log.is_file(),
                "index_stages_available": (DATA / "index-stages.log").is_file(),
                "next_run_at": self.config["last_run_at"] + self.config["interval_hours"] * 3600 if self.config["interval_hours"] else None,
                "log": tail,
            }

    def host_source_ready(self):
        return bool(self.config["host_root"] and self.agent_manifest.get("revision") == self.config["sync_revision"] and self.agent_manifest.get("sync_request", -1) >= self.config["sync_request"] and not self.sync_in_progress and not self.agent_error_text and time.time() - self.agent_seen_at < 60)

    def close(self):
        with self.lock:
            self.stopping = True
            self.discard_backup_download()
            if self.ingest and self.ingest.poll() is None:
                self.ingest.terminate()
                try:
                    self.ingest.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.ingest.kill()
                    self.ingest.wait()
            self._stop_mcp()
            self._stop_qdrant()


controller = None


class Handler(BaseHTTPRequestHandler):
    def _authenticated(self):
        if not controller.password_enabled():
            return True
        cookie = self.headers.get("Cookie", "")
        session = next((part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("knowledge_session=")), "")
        with controller.lock:
            expiry = controller.sessions.get(session, 0)
            if expiry > time.time():
                return True
            controller.sessions.pop(session, None)
        return False

    def _allowed_host(self):
        try:
            host = urlsplit("//" + self.headers.get("Host", "")).hostname
            if host == "localhost":
                return True
            ipaddress.ip_address(host)
            return True
        except (ValueError, TypeError):
            return False

    def send(self, code, data, kind="application/json; charset=utf-8", cookie=None):
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def send_download(self, path, filename, kind="text/plain; charset=utf-8"):
        if not path.is_file() or path.is_symlink():
            self.send(404, json.dumps({"error": "This log is not available for the selected run"}))
            return
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            remaining = size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        if not self._allowed_host():
            self.send(403, json.dumps({"error": "Host not allowed"}))
            return
        if self.path in ("/dashboard/services", "/dashboard/access"):
            self.send_response(302)
            self.send_header("Location", "/dashboard/settings" if self.path == "/dashboard/access" else "/dashboard")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/api/auth":
            self.send(200, json.dumps({"required": controller.password_enabled(), "authenticated": self._authenticated()}))
            return
        if self.path == "/api/agent/task":
            if not controller.agent_authenticated(self.headers.get("X-Agent-Token")):
                self.send(403, json.dumps({"error": "Host agent not paired"}))
                return
            self.send(200, json.dumps(controller.agent_task()))
            return
        if self.path == "/app.js":
            self.send(200, SCRIPT.read_text(encoding="utf-8"), "text/javascript; charset=utf-8")
            return
        if self.path == "/api/health":
            status = controller.status()
            self.send(200 if status["app_ready"] and status["qdrant_ready"] else 503, json.dumps({"app_ready": status["app_ready"], "qdrant_ready": status["qdrant_ready"], "mcp_running": status["mcp_running"]}))
            return
        if self.path == "/" or self.path in SETUP_PATHS or self.path in DASHBOARD_PATHS:
            token = TOKEN if self._authenticated() else ""
            self.send(200, HTML.read_text(encoding="utf-8").replace("__TOKEN__", token), "text/html; charset=utf-8")
            return
        if not self._authenticated():
            self.send(401, json.dumps({"error": "Login required"}))
            return
        if urlsplit(self.path).path == "/api/backup/download":
            key = parse_qs(urlsplit(self.path).query).get("id", [""])[0]
            with controller.lock:
                pending = controller.backup_download
                if not pending or not hmac.compare_digest(pending[0], key) or pending[2] < time.time():
                    self.send(404, json.dumps({"error": "Backup download has expired; create a new backup"}))
                    return
                controller.backup_download = None
            try:
                self.send_download(pending[1], "knowledge-mcp-backup.tar.gz", "application/gzip")
            finally:
                pending[1].unlink(missing_ok=True)
        elif self.path == "/api/status":
            self.send(200, json.dumps(controller.status()))
        elif self.path == "/api/index/errors":
            entries, origin = controller.index_errors()
            self.send(200, json.dumps({"errors": entries, "origin": origin}))
        elif self.path == "/api/index/log/full":
            self.send_download(DATA / "ingest.log", "indexing-full.log")
        elif self.path == "/api/index/log/stages":
            self.send_download(DATA / "index-stages.log", "indexing-stages.log")
        elif self.path == "/api/scan/files":
            try:
                self.send(200, json.dumps({"files": controller.scan_files()}))
            except ValueError as error:
                self.send(409, json.dumps({"error": str(error)}))
        elif self.path == "/api/policy":
            content = POLICY.read_text(encoding="utf-8")
            self.send(200, json.dumps({"content": content, "policy": yaml.safe_load(content)}))
        elif self.path == "/api/folders":
            root = Path(controller.config["source_root"])
            if controller.config["source_mode"] == "host_agent" and not controller.host_source_ready():
                self.send(200, json.dumps({"folders": [""], "root": controller.config["host_root"]}))
                return
            policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
            excluded = {x.casefold() for x in policy["exclude_directories"]}
            excluded_top = {x.casefold() for x in policy["exclude_top_level"]}
            folders = [""]
            for base, dirs, _ in os.walk(root):
                relative = Path(base).relative_to(root)
                dirs[:] = sorted(d for d in dirs if d.casefold() not in excluded and (relative.parts or d.casefold() not in excluded_top) and not (Path(base) / d).is_symlink())
                for directory in dirs:
                    folders.append((relative / directory).as_posix())
                    if len(folders) >= 500:
                        break
                if len(folders) >= 500:
                    break
            self.send(200, json.dumps({"folders": folders, "root": str(root)}))
        else:
            self.send(404, json.dumps({"error": "Not found"}))

    def do_POST(self):
        if not self._allowed_host():
            self.send(403, json.dumps({"error": "Unauthorized request"}))
            return
        route = urlsplit(self.path).path
        agent = route.startswith("/api/agent/") and route not in ("/api/agent/pair", "/api/agent/refresh")
        if route != "/api/login" and (not controller.agent_authenticated(self.headers.get("X-Agent-Token")) if agent else not self._authenticated() or self.headers.get("X-Control-Token") != TOKEN):
            self.send(403, json.dumps({"error": "Unauthorized request"}))
            return
        self.connection.settimeout(30)
        # ponytail: serialize mutations during backup; use a maintenance gate if uploads need concurrency.
        with controller.lock:
            if controller.stopping:
                self.send(503, json.dumps({"error": "Application is restarting"}))
                return
            self._post()

    def _post(self):
        if not self._allowed_host():
            self.send(403, json.dumps({"error": "Unauthorized request"}))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            route = urlsplit(self.path)
            if route.path == "/api/login":
                if not 0 < length <= 4096 or not controller.password_enabled():
                    raise ValueError("Invalid login request")
                values = json.loads(self.rfile.read(length))
                address = self.client_address[0]
                with controller.lock:
                    attempts = [t for t in controller.login_attempts.get(address, []) if time.time() - t < 60]
                    if len(attempts) >= 5:
                        self.send(429, json.dumps({"error": "Try again in a minute"}))
                        return
                if not controller.check_password(values.get("password")):
                    with controller.lock:
                        controller.login_attempts[address] = attempts + [time.time()]
                    self.send(401, json.dumps({"error": "Invalid password"}))
                    return
                session = secrets.token_urlsafe(32)
                with controller.lock:
                    controller.sessions[session] = time.time() + 86400
                    controller.login_attempts.pop(address, None)
                self.send(200, json.dumps({"ok": True}), cookie=f"knowledge_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400")
                return
            if route.path.startswith("/api/agent/") and route.path not in ("/api/agent/pair", "/api/agent/refresh"):
                if not controller.agent_authenticated(self.headers.get("X-Agent-Token")):
                    self.send(403, json.dumps({"error": "Host agent not paired"}))
                    return
                if route.path == "/api/agent/file":
                    query = parse_qs(route.query)
                    name = query.get("path", [""])[0]
                    revision = int(self.headers.get("X-Sync-Revision", "-1"))
                    controller.agent_file(name, revision, length, self.headers.get("X-File-Sha256"), self.rfile)
                    self.send(200, json.dumps({"saved": True}))
                    return
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("Document inventory is too large or empty")
                values = json.loads(self.rfile.read(length))
                if not isinstance(values, dict):
                    raise ValueError("Invalid document inventory")
                if route.path == "/api/agent/plan":
                    result = controller.agent_plan(values)
                elif route.path == "/api/agent/commit":
                    result = controller.agent_commit(values)
                elif route.path == "/api/agent/progress":
                    result = controller.report_agent_progress(values)
                elif route.path == "/api/agent/error":
                    result = controller.agent_error(values)
                elif route.path == "/api/agent/folder-probe":
                    result = controller.report_folder_probe(values)
                else:
                    self.send(404, json.dumps({"error": "Not found"}))
                    return
                self.send(200, json.dumps(result))
                return
            if not self._authenticated() or self.headers.get("X-Control-Token") != TOKEN:
                self.send(403, json.dumps({"error": "Unauthorized request"}))
                return
            if route.path in ("/api/backup", "/api/restore"):
                roots = controller.backup_roots()
                version = controller.qdrant_backup_version()
                if route.path == "/api/backup":
                    descriptor, filename = tempfile.mkstemp(prefix="knowledge-backup-", suffix=".tar.gz")
                    os.close(descriptor)
                    archive = Path(filename)
                    try:
                        with controller.backup_pause():
                            create_backup(archive, roots, version)
                        controller.discard_backup_download()
                        key = secrets.token_urlsafe(32)
                        controller.backup_download = (key, archive, time.time() + 600)
                        self.send(200, json.dumps({"url": "/api/backup/download?id=" + key}))
                    except Exception:
                        if controller.backup_download and controller.backup_download[1] == archive:
                            controller.backup_download = None
                        archive.unlink(missing_ok=True)
                        raise
                    return
                with tempfile.TemporaryDirectory(prefix="knowledge-backup-") as folder:
                    archive = Path(folder) / "backup.tar.gz"
                    if route.path == "/api/restore":
                        if not 0 < length <= MAX_UNPACKED_SIZE:
                            raise ValueError("Backup upload is too large or empty")
                        remaining = length
                        with archive.open("wb") as output:
                            while remaining:
                                chunk = self.rfile.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("Incomplete backup upload")
                                output.write(chunk)
                                remaining -= len(chunk)
                        stages = stage_restore(archive, roots, version)
                        try:
                            controller.validate_backup_settings(stages)
                            with controller.backup_pause():
                                apply_restore(stages, roots)
                                controller.stopping = True
                                controller.restart_requested = True
                        finally:
                            for stage in stages.values():
                                shutil.rmtree(stage, ignore_errors=True)
                        try:
                            self.send(200, json.dumps({"restored": True, "restarting": True}))
                        finally:
                            threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route.path == "/api/logout":
                cookie = self.headers.get("Cookie", "")
                session = next((part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("knowledge_session=")), "")
                with controller.lock:
                    controller.sessions.pop(session, None)
                self.send(200, json.dumps({"ok": True}), cookie="knowledge_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if route.path == "/api/upload":
                self.send(404, json.dumps({"error": "Document uploads are unavailable"}))
                return
            if not 0 < length <= 65536:
                raise ValueError("Request is too large or empty")
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("Invalid data")
            if self.path == "/api/config":
                result = controller.update(values)
            elif self.path == "/api/embedding-model":
                result = controller.select_embedding_model(values.get("model"))
            elif self.path == "/api/agent/pair":
                result = controller.agent_pair()
            elif self.path == "/api/agent/refresh":
                result = controller.request_agent_sync()
            elif self.path == "/api/scan":
                result = controller.start_scan()
            elif self.path == "/api/folder/check":
                result = controller.check_folder(values.get("path"), values.get("source_selection", controller.config["source_selection"]))
            elif self.path == "/api/security":
                if values.get("enabled") is True:
                    controller.set_password(values.get("password"))
                elif values.get("enabled") is False:
                    AUTH.unlink(missing_ok=True)
                    with controller.lock:
                        controller.sessions.clear()
                        controller.advance_setup(1)
                else:
                    raise ValueError("Invalid security setting")
                result = {"enabled": controller.password_enabled()}
            elif self.path == "/api/onboarding":
                controller.complete_onboarding()
                result = {"complete": True}
            elif self.path == "/api/onboarding/skip-index":
                controller.skip_initial_index()
                result = {"complete": True, "initial_index_skipped": True}
            elif self.path == "/api/onboarding/progress":
                result = controller.advance_setup(values.get("step"))
            elif self.path == "/api/service":
                result = controller.service(values.get("name"), values.get("action"))
            elif self.path == "/api/index":
                controller.run_index(values.get("dry_run") is True, rebuild=values.get("rebuild") is True)
                result = {"started": True}
            elif self.path == "/api/policy":
                content = values.get("content")
                if isinstance(values.get("policy"), dict):
                    content = yaml.safe_dump(values["policy"], sort_keys=False, allow_unicode=True)
                if not isinstance(content, str):
                    raise ValueError("Invalid policy")
                validate_policy(content)
                with controller.lock:
                    if controller.ingest and controller.ingest.poll() is None:
                        raise ValueError("Wait until indexing finishes before changing policy")
                    tmp = POLICY.with_suffix(".tmp")
                    tmp.write_text(content, encoding="utf-8")
                    tmp.replace(POLICY)
                    controller.invalidate_result()
                    if not controller.config["onboarding_complete"]:
                        controller.config["setup_step"] = min(controller.config["setup_step"], 3)
                        controller._save()
                    controller.config["sync_revision"] += 1
                    controller.sync_in_progress = False
                    controller.agent_progress = None
                    controller.local_scan = {"revision": -1, "running": False, "checked": 0, "eligible": 0, "files": [], "error": ""}
                    controller._save()
                result = {"saved": True}
            else:
                self.send(404, json.dumps({"error": "Not found"}))
                return
            self.send(200, json.dumps(result))
        except (ValueError, yaml.YAMLError, tarfile.TarError, EOFError) as exc:
            self.send(400, json.dumps({"error": str(exc)}))
        except Exception:
            self.send(500, json.dumps({"error": "Internal error; check the container logs"}))
            raise


def main():
    global controller
    controller = Controller()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        controller.close()
        server.server_close()
    if controller.restart_requested:
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


if __name__ == "__main__":
    main()
