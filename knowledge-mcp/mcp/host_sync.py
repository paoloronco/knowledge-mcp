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
    if type(size) is not int or size < 1 or size > policy["max_file_size_mb"] * 1024 * 1024:
        return False
    parts = path.parts
    if parts[0].casefold() in {x.casefold() for x in policy["exclude_top_level"]}:
        return False
    if any(x.casefold() in {y.casefold() for y in policy["exclude_directories"]} for x in parts[:-1]):
        return False
    excluded = {x.casefold() for x in policy.get("exclude_files", [])}
    if path.name.casefold() in excluded or value.casefold() in excluded:
        return False
    suffix = path.suffix.lower()
    return suffix in policy["include_extensions"] and suffix not in policy["exclude_extensions"]
