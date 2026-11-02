import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from bs4 import BeautifulSoup, Tag, NavigableString
from docx import Document
from docx.table import Table
from pypdf import PdfReader
from pptx import Presentation
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)
from tqdm import tqdm
import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "mcp"))
from policy_defaults import (
    REQUIRED_DIRECTORY_NAMES,
    REQUIRED_TOP_LEVEL_NAMES,
    ensure_required_exclusions,
    remove_legacy_default_exclusions,
)
from embedding_models import document_text
from embedding_device import embedding_device
from host_sync import is_office_lock_file, file_stamp
from index_schema import (
    INDEX_VERSION,
    LEXICAL_VECTOR,
    ensure_search_schema,
    source_metadata,
    sparse_vector,
)
from redaction import redact_secrets

load_dotenv(PROJECT_DIR / ".env")

BASE_DIR = Path(os.getenv("INGESTION_BASE_DIR", str(PROJECT_DIR / ".state")))
POLICY_FILE = Path(
    os.getenv("POLICY_FILE", str(PROJECT_DIR / "mcp" / "index-policy.yaml"))
)
STATE_FILE = BASE_DIR / "state" / "index-state.json"
ERROR_LOG = BASE_DIR / "logs" / "errors.log"
PROGRESS_FILE = (
    Path(os.environ["INGEST_PROGRESS_FILE"])
    if os.getenv("INGEST_PROGRESS_FILE")
    else None
)
STAGES_FILE = (
    Path(os.environ["INGEST_STAGES_FILE"]) if os.getenv("INGEST_STAGES_FILE") else None
)
RUN_ERRORS_FILE = (
    Path(os.environ["INGEST_RUN_ERRORS_FILE"])
    if os.getenv("INGEST_RUN_ERRORS_FILE")
    else None
)
_progress_last_write = 0
_progress_last_stage = None


def write_progress(stage, completed=0, total=None, force=False):
    global _progress_last_write, _progress_last_stage
    if PROGRESS_FILE is None and STAGES_FILE is None:
        return
    now = time.monotonic()
    if (
        not force
        and stage == _progress_last_stage
        and now - _progress_last_write < 0.25
    ):
        return
    value = {
        "stage": stage,
        "completed": completed,
        "total": total,
        "updated_at": time.time(),
    }
    if PROGRESS_FILE is not None:
        temp = PROGRESS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(value), encoding="utf-8")
        temp.replace(PROGRESS_FILE)
    if STAGES_FILE is not None:
        timestamp = datetime.now(timezone.utc).isoformat()
        amount = f"{completed}/{total}" if total is not None else str(completed)
        with STAGES_FILE.open("a", encoding="utf-8") as output:
            output.write(f"{timestamp}\t{stage}\t{amount}\n")
    _progress_last_write, _progress_last_stage = now, stage


COLLECTION_NAME = os.getenv("DENSE_COLLECTION", "documents")

MODEL_NAME = os.getenv("MODEL_NAME", "intfloat/multilingual-e5-small")

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))


def load_policy():
    with POLICY_FILE.open("r", encoding="utf-8") as f:
        policy = yaml.safe_load(f)
    remove_legacy_default_exclusions(policy)
    ensure_required_exclusions(policy)
    root = os.getenv("KNOWLEDGE_ROOT") or policy.get("knowledge_root")
    if not root or root.startswith("${") or root == "/path/to/knowledge/root":
        raise ValueError(
            "Set KNOWLEDGE_ROOT in .env or knowledge_root in index-policy.yaml"
        )
    policy["knowledge_root"] = root
    return policy


def load_state():
    state = (
        json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if STATE_FILE.exists()
        else {"documents": {}}
    )
    journal = STATE_FILE.with_suffix(".journal")
    if journal.exists():
        lines = journal.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except ValueError:
                if index == len(lines) - 1:
                    break  # A process may have stopped while appending the final event.
                raise
            if event["entry"] is None:
                state["documents"].pop(event["document_id"], None)
            else:
                state["documents"][event["document_id"]] = event["entry"]
    return state


def record_state(state, document_id, entry):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.with_suffix(".journal").open("a", encoding="utf-8") as output:
        output.write(
            json.dumps({"document_id": document_id, "entry": entry}, ensure_ascii=False)
            + "\n"
        )
        output.flush()
        os.fsync(output.fileno())
    if entry is None:
        state["documents"].pop(document_id, None)
    else:
        state["documents"][document_id] = entry


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    tmp.replace(STATE_FILE)
    STATE_FILE.with_suffix(".journal").unlink(missing_ok=True)


@contextmanager
def ingestion_lock():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                if lock.read(1) == b"":
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another ingestion is using this index state") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def log_error(path, error, stage="indexing"):
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)

    with ERROR_LOG.open("a", encoding="utf-8") as f:
        timestamp = datetime.now(timezone.utc).isoformat()
        f.write(f"{timestamp}\t{path}\t{repr(error)}\n")
    if RUN_ERRORS_FILE is not None:
        root = Path(os.getenv("KNOWLEDGE_ROOT", "")).resolve()
        try:
            source = path.resolve().relative_to(root).as_posix()
        except ValueError:
            source = path.name
        event = {
            "at": timestamp,
            "stage": stage,
            "source": source,
            "error_type": type(error).__name__,
            "message": str(error)[:1000],
        }
        with RUN_ERRORS_FILE.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


def is_candidate(path, root, policy):
    if path.is_symlink() or not path.is_file():
        return False

    if is_office_lock_file(path.name):
        return False

    if not path.resolve().is_relative_to(root.resolve()):
        return False

    relative = path.relative_to(root)

    # Top-level exclusions
    if relative.parts:
        excluded_top = {
            name.casefold() for name in policy.get("exclude_top_level", [])
        } | REQUIRED_TOP_LEVEL_NAMES

        if relative.parts[0].casefold() in excluded_top:
            return False

    # Directory exclusions
    excluded_dirs = {
        name.casefold() for name in policy.get("exclude_directories", [])
    } | REQUIRED_DIRECTORY_NAMES

    if any(part.casefold() in excluded_dirs for part in relative.parts[:-1]):
        return False

    excluded_files = {name.casefold() for name in policy.get("exclude_files", [])}
    if (
        path.name.casefold() in excluded_files
        or relative.as_posix().casefold() in excluded_files
    ):
        return False

    # Extension policy
    extension = path.suffix.lower()

    included = set(policy.get("include_extensions", []))
    excluded = set(policy.get("exclude_extensions", []))

    if extension not in included:
        return False

    if extension in excluded:
        return False

    # File size; unreadable files must abort discovery rather than look deleted.
    max_size = policy.get("max_file_size_mb", 50) * 1024 * 1024

    size = path.stat().st_size

    if size == 0 or size > max_size:
        return False

    return True


def discover_documents(policy):
    root = Path(policy["knowledge_root"]).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Knowledge root does not exist: {root}")

    candidates = []

    selected = json.loads(os.getenv("INDEX_SOURCE_PATHS", '[""]'))
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(item, str) for item in selected)
    ):
        raise ValueError("INDEX_SOURCE_PATHS must contain selected folders")
    excluded_dirs = {name.casefold() for name in policy["exclude_directories"]}
    excluded_top = {name.casefold() for name in policy["exclude_top_level"]}

    def unreadable(error):
        raise OSError(f"Document scan incomplete: {error}") from error

    scanned = []
    for item in sorted(selected):
        if "\\" in item:
            raise ValueError("Invalid selected folder")
        folder = (root / item).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError(f"Selected folder is outside the document library: {item}")
        relative = folder.relative_to(root)
        if any(part.casefold() in excluded_dirs for part in relative.parts) or (
            relative.parts and relative.parts[0].casefold() in excluded_top
        ):
            continue
        if any(folder.is_relative_to(previous) for previous in scanned):
            continue
        scanned.append(folder)
        for base, dirs, names in os.walk(folder, followlinks=False, onerror=unreadable):
            current = Path(base)
            dirs[:] = sorted(
                name
                for name in dirs
                if name.casefold() not in excluded_dirs
                and not (current / name).is_symlink()
                and (current != root or name.casefold() not in excluded_top)
            )
            for name in names:
                path = current / name
                if is_candidate(path, root, policy):
                    candidates.append(path)

    return sorted(set(candidates))


def normalize_text(text):
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def parse_txt_or_md(path):
    raw = path.read_bytes()
    encoding = (
        "utf-32"
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        else "utf-16"
        if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        else "utf-8-sig"
    )
    text = raw.decode(encoding)

    if path.suffix.lower() != ".md":
        return [{"text": normalize_text(text), "page": None, "section": None}]
    sections, buffer, heading, fence = [], [], None, None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            fence = None if fence == marker else marker if fence is None else fence
        match = (
            re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line) if fence is None else None
        )
        if match:
            if buffer:
                sections.append(
                    {
                        "text": normalize_text("\n".join(buffer)),
                        "page": None,
                        "section": heading,
                    }
                )
            heading, buffer = match.group(1), [line]
        else:
            buffer.append(line)
    if buffer:
        sections.append(
            {
                "text": normalize_text("\n".join(buffer)),
                "page": None,
                "section": heading,
            }
        )
    return sections


def parse_html(path):
    soup = BeautifulSoup(path.read_bytes(), "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    sections, buffer, heading = [], [], None
    for element in (soup.body or soup).descendants:
        if isinstance(element, Tag) and re.fullmatch(r"h[1-6]", element.name or ""):
            if buffer:
                sections.append(
                    {
                        "text": normalize_text("\n".join(buffer)),
                        "page": None,
                        "section": heading,
                    }
                )
            buffer = []
            heading = element.get_text(" ", strip=True)
        elif isinstance(element, NavigableString) and element.strip():
            buffer.append(str(element).strip())
    if buffer:
        sections.append(
            {
                "text": normalize_text("\n".join(buffer)),
                "page": None,
                "section": heading,
            }
        )
    return sections


def parse_pdf(path):
    reader = PdfReader(str(path))

    sections = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""

        text = normalize_text(text)

        if text:
            sections.append(
                {
                    "text": text,
                    "page": page_number,
                    "section": None,
                }
            )

    return sections


def parse_docx(path):
    document = Document(str(path))

    sections = []
    current_heading = None
    buffer = []

    def flush():
        nonlocal buffer

        text = normalize_text("\n".join(buffer))

        if text:
            sections.append(
                {
                    "text": text,
                    "page": None,
                    "section": current_heading,
                }
            )

        buffer = []

    for paragraph in document.iter_inner_content():
        if isinstance(paragraph, Table):
            buffer.extend(
                " | ".join(cell.text for cell in row.cells) for row in paragraph.rows
            )
            continue
        text = paragraph.text.strip()

        if not text:
            continue

        style = paragraph.style.name.lower() if paragraph.style else ""

        if style.startswith("heading"):
            flush()
            current_heading = text
            buffer.append(text)
        else:
            buffer.append(text)

    flush()

    return sections


def parse_pptx(path):
    presentation = Presentation(str(path))

    sections = []

    def collect(shapes):
        parts = []
        for shape in shapes:
            if hasattr(shape, "shapes"):
                parts.extend(collect(shape.shapes))
            elif getattr(shape, "has_table", False):
                parts.extend(
                    " | ".join(cell.text for cell in row.cells)
                    for row in shape.table.rows
                )
            elif getattr(shape, "has_text_frame", False):
                parts.append(shape.text.strip())
        return parts

    for slide_number, slide in enumerate(
        presentation.slides,
        start=1,
    ):
        text = normalize_text("\n".join(collect(slide.shapes)))

        if text:
            sections.append(
                {
                    "text": text,
                    "page": slide_number,
                    "section": f"Slide {slide_number}",
                }
            )

    return sections


def parse_document(path):
    extension = path.suffix.lower()

    if extension in {".txt", ".md"}:
        return parse_txt_or_md(path)

    if extension in {".html", ".htm"}:
        return parse_html(path)

    if extension == ".pdf":
        return parse_pdf(path)

    if extension == ".docx":
        return parse_docx(path)

    if extension == ".pptx":
        return parse_pptx(path)

    raise ValueError(f"Unsupported extension: {extension}")


def chunk_text(text):
    if len(text) <= CHUNK_SIZE:
        return [text] if text.strip() else []

    chunks = []

    start = 0

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))

        chunk = text[start:end]

        # Prefer a natural break close to the end.
        if end < len(text):
            search_start = max(0, len(chunk) - 300)

            break_positions = [
                chunk.rfind("\n\n", search_start),
                chunk.rfind(". ", search_start),
                chunk.rfind("\n", search_start),
            ]

            best_break = max(break_positions)

            if best_break > 0:
                chunk = chunk[: best_break + 1]
                end = start + len(chunk)

        chunk = normalize_text(chunk)

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        next_start = max(0, end - CHUNK_OVERLAP)

        # Safety against pathological loops
        if next_start <= start:
            next_start = end

        start = next_start

    return chunks


def make_point_id(document_hash, section_index, chunk_index):
    value = f"{document_hash}:{section_index}:{chunk_index}"

    # Qdrant accepts UUID or unsigned integer IDs.
    return int(
        hashlib.sha256(value.encode()).hexdigest()[:16],
        16,
    )


def ensure_collection(client, vector_size, rebuild=False):
    existing = {collection.name for collection in client.get_collections().collections}

    if rebuild and COLLECTION_NAME in existing:
        client.delete_collection(COLLECTION_NAME)
        existing.remove(COLLECTION_NAME)

    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=vector_size,
                distance=Distance.COSINE,
                on_disk=True,
            ),
            on_disk_payload=True,
        )
        ensure_search_schema(client, COLLECTION_NAME)
        return True
    actual_size = client.get_collection(COLLECTION_NAME).config.params.vectors.size
    if actual_size != vector_size:
        raise ValueError(
            f"Collection vector size {actual_size} differs from model size {vector_size}"
        )
    ensure_search_schema(client, COLLECTION_NAME)
    return False


def embedding_input(record):
    context = " / ".join(filter(None, [record.get("filename"), record.get("section")]))[
        :200
    ]
    return document_text(
        MODEL_NAME, f"{context}\n{record['text']}" if context else record["text"]
    )


def fit_model_chunks(record, model):
    tokenizer = getattr(model, "tokenizer", None)
    maximum = getattr(model, "max_seq_length", None)
    if tokenizer is None or not isinstance(maximum, int):
        yield record
        return
    if (
        len(tokenizer.encode(embedding_input(record), add_special_tokens=True))
        <= maximum
    ):
        yield record
        return
    text = record["text"]
    if len(text) < 2:
        raise ValueError(
            "The model token limit is too small for a passage and its metadata"
        )
    middle = len(text) // 2
    split = text.rfind(" ", max(0, middle - 100), middle + 1)
    split = split if split > 0 else middle
    for part in (text[:split], text[split:]):
        if part.strip():
            yield from fit_model_chunks({**record, "text": part.strip()}, model)


def document_records(path, model=None):
    records = []
    for section_index, section in enumerate(parse_document(path)):
        # Redact before splitting so a credential cannot straddle chunk boundaries.
        text = redact_secrets(section["text"])
        chunk_index = 0
        for chunk in chunk_text(text):
            record = {
                "section_index": section_index,
                "chunk_index": chunk_index,
                "text": chunk,
                "page": section["page"],
                "section": redact_secrets(section["section"]),
                "filename": path.name,
            }
            fitted = fit_model_chunks(record, model) if model is not None else [record]
            for part in fitted:
                records.append(
                    {**part, "chunk_index": chunk_index, "document_order": len(records)}
                )
                chunk_index += 1
    if not records:
        raise ValueError(
            "No extractable text; scanned PDFs require OCR before indexing"
        )
    return records


def run_ingestion(args):
    start_time = time.time()
    policy = load_policy()
    root = Path(policy["knowledge_root"]).resolve()
    recover_state = (
        not STATE_FILE.exists() and not STATE_FILE.with_suffix(".journal").exists()
    )
    state = load_state()
    if state.get("collection") not in (None, COLLECTION_NAME):
        state = {"documents": {}}
        recover_state = True
    recover_state = recover_state or state.get("reconcile_required", False)
    if recover_state:
        state["reconcile_required"] = True
    if state.get("root") != str(root) or state.get("collection") != COLLECTION_NAME:
        # Keep document entries for cleanup, but never reuse hashes across libraries.
        state["files"] = {}
    state.update(root=str(root), collection=COLLECTION_NAME)
    previous_files = state.get("files", {})
    current_files = {}
    print("Discovering candidate documents...", flush=True)
    write_progress("discovering", force=True)
    candidates = discover_documents(policy)
    print(f"Candidates found: {len(candidates)}", flush=True)
    if not candidates and not args.dry_run and not args.allow_empty:
        raise SystemExit(
            "No eligible documents found; the index was not changed. Use --allow-empty to clear it intentionally."
        )
    hash_to_paths, file_hashes = {}, {}
    hash_failures = 0
    for count, path in enumerate(tqdm(candidates), 1):
        try:
            relative = path.relative_to(root).as_posix()
            stamp = file_stamp(path)
            previous = previous_files.get(relative, {})
            # shortcut: metadata-preserving edits require --verify-hashes; use it for unreliable filesystem timestamps.
            if not args.verify_hashes and previous.get("stamp") == stamp:
                digest = previous["sha256"]
            else:
                digest = sha256_file(path)
                if stamp != file_stamp(path):
                    raise RuntimeError("Document changed while hashing; retry indexing")
            current_files[relative] = {"stamp": stamp, "sha256": digest}
            lifecycle = source_metadata(relative)["lifecycle"]
            document_id = hashlib.sha256(
                f"{digest}:{path.suffix.lower()}:{lifecycle}".encode()
            ).hexdigest()
            hash_to_paths.setdefault(document_id, []).append(path)
            file_hashes[document_id] = digest
        except Exception as exc:
            hash_failures += 1
            log_error(path, exc, "hashing")
        write_progress("hashing", count, len(candidates))
    write_progress("hashing", len(candidates), len(candidates), force=True)
    if args.rebuild and hash_failures:
        raise SystemExit("Some documents could not be read; the index was not rebuilt.")
    documents = list(hash_to_paths.items())
    if args.limit is not None:
        documents = documents[: args.limit]
    print(f"Unique documents: {len(documents)}", flush=True)
    if args.dry_run:
        failures, chunks = hash_failures, 0
        for count, (_, paths) in enumerate(tqdm(documents), 1):
            try:
                chunks += len(document_records(paths[0]))
            except Exception as exc:
                failures += 1
                log_error(paths[0], exc, "dry_run")
            write_progress("dry_run", count, len(documents))
        print(
            f"Documents parsed: {len(documents) - failures}\nFailed: {failures}\nChunks produced: {chunks}",
            flush=True,
        )
        write_progress(
            "failed" if failures else "complete",
            len(documents),
            len(documents),
            force=True,
        )
        if failures or not documents:
            raise SystemExit(1)
        return

    # Compact a recovered journal (including a truncated final event) before appending.
    save_state(state)
    model = None

    def get_model():
        nonlocal model
        if model is None:
            write_progress("loading_model", force=True)
            from sentence_transformers import SentenceTransformer

            device = embedding_device()
            print(f"Loading {MODEL_NAME} on {device}", flush=True)
            model = SentenceTransformer(MODEL_NAME, device=device)
        return model

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    exists = client.collection_exists(COLLECTION_NAME)
    if exists and recover_state and not args.rebuild:
        # Recover only identifiers and sources, never load document text or vectors.
        offset = None
        while True:
            points, offset = client.scroll(
                COLLECTION_NAME,
                offset=offset,
                limit=512,
                with_payload=["document_id", "source", "duplicate_sources"],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                if payload.get("document_id"):
                    state["documents"].setdefault(payload["document_id"], payload)
            if offset is None:
                break
        state.pop("reconcile_required", None)
        save_state(state)
    dimension = (
        client.get_collection(COLLECTION_NAME).config.params.vectors.size
        if exists and not args.rebuild
        else get_model().get_sentence_embedding_dimension()
    )
    created = ensure_collection(client, dimension, rebuild=args.rebuild)
    if created or client.count(COLLECTION_NAME, exact=True).count == 0:
        state["documents"] = {}
        state.pop("reconcile_required", None)
        save_state(state)
    signature = hashlib.sha256(
        json.dumps(
            [INDEX_VERSION, MODEL_NAME, CHUNK_SIZE, CHUNK_OVERLAP], sort_keys=True
        ).encode()
    ).hexdigest()
    indexed, skipped, failed, indexed_chunks = 0, 0, 0, 0
    failed_sources = set()
    try:
        for count, (document_id, paths) in enumerate(tqdm(documents), 1):
            write_progress("indexing", count - 1, len(documents))
            primary = paths[0]
            source = primary.relative_to(root).as_posix()
            duplicates = [path.relative_to(root).as_posix() for path in paths[1:]]
            previous = state["documents"].get(document_id, {})
            if previous.get("signature") == signature and not previous.get("pending"):
                if previous.get("source") == source:
                    if previous.get("duplicate_sources") != duplicates:
                        client.set_payload(
                            COLLECTION_NAME,
                            {
                                **source_metadata(source, duplicates),
                                "duplicate_sources": duplicates,
                            },
                            points=Filter(
                                must=[
                                    FieldCondition(
                                        key="document_id",
                                        match=MatchValue(value=document_id),
                                    )
                                ]
                            ),
                            wait=True,
                        )
                        record_state(
                            state,
                            document_id,
                            {**previous, "duplicate_sources": duplicates},
                        )
                    if (
                        previous_files.get(source, {}).get("stamp")
                        != current_files[source]["stamp"]
                    ):
                        client.set_payload(
                            COLLECTION_NAME,
                            {
                                "modified_at": current_files[source]["stamp"][
                                    "mtime_ns"
                                ]
                                / 1e9
                            },
                            points=Filter(
                                must=[
                                    FieldCondition(
                                        key="document_id",
                                        match=MatchValue(value=document_id),
                                    )
                                ]
                            ),
                            wait=True,
                        )
                    skipped += 1
                    continue
            try:
                embedding_model = get_model()
                if embedding_model.get_sentence_embedding_dimension() != dimension:
                    raise ValueError(
                        "Embedding model dimension differs from this collection; use the matching model or rebuild"
                    )
                before = file_stamp(primary)
                records = document_records(primary, embedding_model)
                generation = hashlib.sha256((signature + source).encode()).hexdigest()
                metadata = source_metadata(source, duplicates)
                record_state(
                    state,
                    document_id,
                    {
                        "source": source,
                        "duplicate_sources": duplicates,
                        "pending": True,
                    },
                )
                for offset in range(0, len(records), EMBED_BATCH_SIZE):
                    batch = records[offset : offset + EMBED_BATCH_SIZE]
                    embeddings = embedding_model.encode(
                        [embedding_input(record) for record in batch],
                        batch_size=EMBED_BATCH_SIZE,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )
                    if len(embeddings) != len(batch):
                        raise RuntimeError(
                            "Embedding model returned the wrong number of vectors"
                        )
                    points = []
                    for record, vector in zip(batch, embeddings):
                        payload = {
                            **record,
                            **metadata,
                            "document_id": document_id,
                            "file_hash": file_hashes[document_id],
                            "embedding_model": MODEL_NAME,
                            "source": source,
                            "duplicate_sources": duplicates,
                            "extension": primary.suffix.lower(),
                            "source_casefold": source.casefold(),
                            "filename_casefold": primary.name.casefold(),
                            "modified_at": before["mtime_ns"] / 1e9,
                            "file_size": before["size"],
                            "total_chunks": len(records),
                            "index_signature": generation,
                            "ready": False,
                        }
                        points.append(
                            PointStruct(
                                id=make_point_id(
                                    document_id + generation,
                                    record["section_index"],
                                    record["chunk_index"],
                                ),
                                vector={
                                    "": vector.tolist(),
                                    LEXICAL_VECTOR: sparse_vector(
                                        record["text"], source, record["section"]
                                    ),
                                },
                                payload=payload,
                            )
                        )
                    client.upsert(COLLECTION_NAME, points=points, wait=True)
                if (
                    before != file_stamp(primary)
                    or before != current_files[source]["stamp"]
                ):
                    raise RuntimeError(
                        "Document changed while indexing; retry indexing"
                    )
                client.set_payload(
                    COLLECTION_NAME,
                    {"ready": True},
                    points=Filter(
                        must=[
                            FieldCondition(
                                key="document_id", match=MatchValue(value=document_id)
                            ),
                            FieldCondition(
                                key="index_signature",
                                match=MatchValue(value=generation),
                            ),
                        ]
                    ),
                    wait=True,
                )
                # Remove previous chunk layouts only after the replacement has been saved.
                client.delete(
                    COLLECTION_NAME,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="document_id", match=MatchValue(value=document_id)
                            )
                        ],
                        must_not=[
                            FieldCondition(
                                key="index_signature",
                                match=MatchValue(value=generation),
                            )
                        ],
                    ),
                    wait=True,
                )
                record_state(
                    state,
                    document_id,
                    {
                        "source": source,
                        "duplicate_sources": duplicates,
                        "chunks": len(records),
                        "signature": signature,
                        "indexed_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                indexed += 1
                indexed_chunks += len(records)
            except Exception as exc:
                failed += 1
                failed_sources.update(
                    path.relative_to(root).as_posix() for path in paths
                )
                log_error(primary, exc)
        if args.limit is None and not hash_failures:
            for document_id in set(state["documents"]) - set(hash_to_paths):
                entry = state["documents"][document_id]
                if failed_sources.intersection(
                    [entry.get("source"), *entry.get("duplicate_sources", [])]
                ):
                    continue  # Keep the last usable version of a document that failed replacement.
                client.delete(
                    COLLECTION_NAME,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="document_id", match=MatchValue(value=document_id)
                            )
                        ]
                    ),
                    wait=True,
                )
                record_state(state, document_id, None)
        state["files"] = current_files
    finally:
        save_state(state)
    print(
        f"Indexed documents: {indexed}\nAlready indexed: {skipped}\nFailed: {failed + hash_failures}\nChunks indexed: {indexed_chunks}\nElapsed: {(time.time() - start_time):.1f}s",
        flush=True,
    )
    write_progress(
        "failed" if failed or hash_failures else "complete",
        len(documents),
        len(documents),
        force=True,
    )
    if failed or hash_failures:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and chunk without embeddings or Qdrant writes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process the first N unique documents; never prune the index",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow an empty source to clear the index",
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="Recreate only the selected collection"
    )
    parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Rehash all files instead of reusing unchanged file metadata",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be greater than zero")
    if args.rebuild and (args.dry_run or args.limit is not None):
        parser.error("--rebuild cannot be combined with --dry-run or --limit")
    if not 0 <= CHUNK_OVERLAP < CHUNK_SIZE or EMBED_BATCH_SIZE < 1:
        parser.error(
            "Require 0 <= CHUNK_OVERLAP < CHUNK_SIZE and EMBED_BATCH_SIZE >= 1"
        )
    with ingestion_lock():
        run_ingestion(args)


if __name__ == "__main__":
    main()
