"""Portable backup of the three persistent volumes used by the Docker image."""

import gzip
import io
import json
import shutil
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath


FORMAT_VERSION = 2
MAX_UNPACKED_SIZE = 100 * 1024**3
MAX_MEMBERS = 250_000


def create_backup(destination, roots, qdrant_version=None):
    manifest = json.dumps({"format": "knowledge-mcp-backup", "version": FORMAT_VERSION, "roots": sorted(roots), "qdrant_version": qdrant_version}).encode()
    count, total = 1, 0
    with tarfile.open(destination, "w:gz", dereference=True) as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        for name, root in roots.items():
            if not root.is_dir() or root.is_symlink():
                raise ValueError(f"Backup volume is unavailable: {name}")
            def include(item):
                nonlocal count, total
                source = root.joinpath(*PurePosixPath(item.name).parts[1:])
                if source.is_symlink() or not (item.isfile() or item.isdir()) or any(part.startswith(".restore-") for part in PurePosixPath(item.name).parts):
                    return None
                count += 1
                total += item.size
                if count > MAX_MEMBERS or total > MAX_UNPACKED_SIZE:
                    raise ValueError("Backup exceeds the restore size or file count limit")
                return item

            archive.add(root, arcname=name, recursive=False, filter=include)
            for child in root.iterdir():
                if name == "data" and child.name == "huggingface":
                    continue  # Downloaded models can be fetched again; keep backups smaller.
                if child.name.startswith(".restore-"):
                    continue
                archive.add(child, arcname=f"{name}/{child.name}", filter=include)


def _is_safe_path(candidate: Path, parent: Path) -> bool:
    """Verify candidate resolves under parent, preventing symlink-based escape."""
    try:
        candidate.resolve(strict=False)
        parent.resolve(strict=False)
    except OSError:
        return False
    try:
        candidate.resolve().is_relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def stage_restore(source, roots, qdrant_version=None):
    """Validate and extract into hidden staging folders; leave live data untouched."""
    stages = {}
    try:
        for name, root in roots.items():
            root.mkdir(parents=True, exist_ok=True)
            stages[name] = Path(tempfile.mkdtemp(prefix=".restore-", dir=root))
        with gzip.open(source, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as archive:
            members = iter(archive)
            first = next(members, None)
            if first is None or first.name != "manifest.json" or not first.isfile() or first.size > 4096:
                raise ValueError("Invalid backup archive")
            manifest = json.load(archive.extractfile(first))
            if manifest != {"format": "knowledge-mcp-backup", "version": FORMAT_VERSION, "roots": sorted(roots), "qdrant_version": qdrant_version}:
                raise ValueError("Unsupported backup format, volume layout or Qdrant version")
            total = 0
            seen = set()
            found_roots = set()
            for count, member in enumerate(members, 2):
                path = PurePosixPath(member.name)
                is_unsafe_path = path.is_absolute() or not path.parts
                is_outside_roots = path.parts[0] not in roots
                has_traversal = any(part in ("", ".", "..") for part in path.parts)
                has_restore_prefix = any(part.startswith(".restore-") for part in path.parts)
                is_duplicate = member.name in seen
                is_bad_type = not (member.isfile() or member.isdir())
                is_negative_size = member.size < 0
                is_dir_with_size = member.isdir() and member.size != 0
                has_windows_chars = "\\" in member.name or ":" in member.name
                if (count > MAX_MEMBERS or is_unsafe_path or is_outside_roots or has_traversal
                        or has_restore_prefix or is_duplicate or is_bad_type or is_negative_size
                        or is_dir_with_size or has_windows_chars):
                    raise ValueError("Backup contains an unsafe path or file type")
                seen.add(member.name)
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise ValueError("Backup volume must be a directory")
                    found_roots.add(path.parts[0])
                    continue
                total += member.size
                if total > MAX_UNPACKED_SIZE:
                    raise ValueError("Backup is too large to restore")
                target = stages[path.parts[0]].joinpath(*path.parts[1:])
                if not _is_safe_path(target, stages[path.parts[0]]):
                    raise ValueError("Backup path escapes the staging directory")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as contents, target.open("wb") as output:
                        shutil.copyfileobj(contents, output)
                    target.chmod(0o600)
            # Read through the gzip trailer: tar parsing alone may miss CRC damage or truncation.
            trailing = 0
            for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                trailing += len(chunk)
                if trailing > 1024 * 1024:
                    raise ValueError("Backup contains excessive trailing data")
            if found_roots != set(roots):
                raise ValueError("Backup is missing a persistent volume")
            if not (stages["data"] / "config.json").is_file() or not (stages["data"] / "index-policy.yaml").is_file():
                raise ValueError("Backup is missing application settings")
        return stages
    except Exception as error:
        for stage in stages.values():
            shutil.rmtree(stage, ignore_errors=True)
        if isinstance(error, (gzip.BadGzipFile, EOFError, tarfile.TarError, zlib.error)):
            raise ValueError("Backup archive is corrupt or incomplete") from error
        raise


def apply_restore(stages, roots):
    """Keep the old files until all volumes have been replaced; roll back on error."""
    # ponytail: rollback covers I/O failures; add a journal for recovery across host crashes.
    old = {}
    moved_old, moved_new = [], []
    try:
        for name, root in roots.items():
            old[name] = Path(tempfile.mkdtemp(prefix=".restore-old-", dir=root))
            for child in root.iterdir():
                if child in (stages[name], old[name]):
                    continue
                saved = old[name] / child.name
                child.rename(saved)
                moved_old.append((saved, child))
            for child in stages[name].iterdir():
                destination = root / child.name
                child.rename(destination)
                moved_new.append((destination, child))
    except Exception:
        for destination, staged in reversed(moved_new):
            destination.rename(staged)
        for saved, original in reversed(moved_old):
            saved.rename(original)
        raise
    finally:
        for folder in old.values():
            # Never discard a saved file if rollback itself failed.
            if not any(folder.iterdir()):
                folder.rmdir()
    for folder in [*old.values(), *stages.values()]:
        shutil.rmtree(folder, ignore_errors=True)
