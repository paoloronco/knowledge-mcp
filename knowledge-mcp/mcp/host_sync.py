"""Shared path and policy checks for the host document companion."""

import ntpath
import posixpath
from pathlib import PurePosixPath


def file_stamp(path):
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "inode": info.st_ino, "device": info.st_dev}


def cached_file_hash(path, cache, hasher):
    """Reuse a checksum only while size, timestamps and file identity still agree."""
    stamp = file_stamp(path)
    previous = cache.get(str(path))
    if previous and previous["stamp"] == stamp:
        return previous["sha256"]
    digest = hasher(path)
    if stamp != file_stamp(path):
        raise RuntimeError(f"Document changed while hashing: {path.name}")
    cache[str(path)] = {"stamp": stamp, "sha256": digest}
    return digest


def host_root(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\x00\r\n"):
        raise ValueError("Enter an absolute host folder path")
    if ntpath.isabs(value) and ("\\" in value or ":" in value):
        normalized = ntpath.normpath(value)
        if normalized == ntpath.splitdrive(normalized)[0] + "\\":
            raise ValueError("Choose a folder, not a filesystem root")
    elif posixpath.isabs(value):
        normalized = posixpath.normpath(value)
        if normalized == "/":
            raise ValueError("Choose a folder, not a filesystem root")
    else:
        raise ValueError("Enter an absolute host folder path")
    return normalized


def root_allowed(value, allow_roots):
    """True when an absolute host/document root is inside the configured ACL allowlist.

    An empty allowlist means "no restriction" for backward compatibility. Both the
    candidate and the allowlist entries are normalized with ``host_root`` so a
    folder may only be selected under an explicitly permitted root.
    """
    if not allow_roots:
        return True
    try:
        candidate = host_root(value)
    except ValueError:
        return False
    for allowed in allow_roots:
        if not isinstance(allowed, str):
            continue
        try:
            base = host_root(allowed)
        except ValueError:
            continue
        module = ntpath if ("\\" in candidate or ":" in candidate) else posixpath
        sep = "\\" if module is ntpath else "/"
        normalize = str.casefold if module is ntpath else str
        if normalize(candidate) == normalize(base) or normalize(candidate).startswith(normalize(base.rstrip("\\/") + sep)):
            return True
    return False


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\\\x00\r\n:"):
        raise ValueError("Invalid document path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Invalid document path")
    return path


def is_office_lock_file(name):
    """Office's ~$ files are temporary locks, not readable documents."""
    return name.startswith("~$") and PurePosixPath(name).suffix.lower() in {".docx", ".pptx"}


def allowed_file(value, size, policy):
    path = relative_path(value)
    if is_office_lock_file(path.name):
        return False
    max_size = (policy.get("max_file_size_mb") or 50) * 1024 * 1024
    if type(size) is not int or size < 1 or size > max_size:
        return False
    parts = path.parts
    directories = {part.casefold() for part in parts[:-1]}
    excluded_top = {x.casefold() for x in (policy.get("exclude_top_level") or [])}
    excluded_dirs = {x.casefold() for x in (policy.get("exclude_directories") or [])}
    acl_deny = {x.casefold() for x in (policy.get("acl_deny_directories") or [])}
    if directories.intersection(excluded_top | excluded_dirs | acl_deny):
        return False
    excluded = {x.casefold() for x in (policy.get("exclude_files") or [])}
    if path.name.casefold() in excluded or value.casefold() in excluded:
        return False
    suffix = path.suffix.lower()
    included = {x.casefold() for x in (policy.get("include_extensions") or [])}
    excluded_ext = {x.casefold() for x in (policy.get("exclude_extensions") or [])}
    return suffix in included and suffix not in excluded_ext
