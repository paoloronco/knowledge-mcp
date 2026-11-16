"""Bounded, live Qdrant retrieval and indexed-document navigation."""

import os
import re
import threading
from pathlib import Path, PurePosixPath

import yaml
from qdrant_client import QdrantClient, models
from qdrant_client.common.client_exceptions import QdrantException
from qdrant_client.http import exceptions as qdrant_exceptions

from embedding_device import embedding_device
from embedding_models import query_text
from index_schema import (
    INDEX_VERSION,
    LEXICAL_VECTOR,
    source_metadata,
    sparse_vector,
    tokens,
)
from policy_defaults import (
    DEFAULT_POLICY,
    ensure_required_exclusions,
    remove_legacy_default_exclusions,
)
from redaction import redact_secrets

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
DENSE_COLLECTION = os.getenv("DENSE_COLLECTION", "documents")
MODEL_NAME = os.getenv("MODEL_NAME", "intfloat/multilingual-e5-small")
POLICY_FILE = Path(
    os.getenv("POLICY_FILE", str(Path(__file__).with_name("index-policy.yaml")))
)
DENSE_LIMIT = 80
LEXICAL_LIMIT = 80
FINAL_LIMIT = 8
MAX_CONTEXT_CHARS = 6000
BACKEND_MESSAGE = "Qdrant is unavailable. Check the service and retry."
MIGRATION_MESSAGE = (
    "This collection was created by an older release. Run an incremental update "
    "for each embedding model to enable hybrid search and document navigation."
)
BACKEND_ERRORS = (qdrant_exceptions.ApiException, QdrantException, OSError)
client = QdrantClient(url=QDRANT_URL, timeout=30)
model = None
_model_lock = threading.Lock()

_policy_cache = None
_policy_stamp = None
_policy_lock = threading.Lock()


def _policy_file_stamp():
    try:
        info = POLICY_FILE.stat()
    except OSError:
        return None
    # The Web UI replaces the policy file on every save, so the inode catches
    # edits even where st_mtime resolution is too coarse to change.
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def load_policy():
    global _policy_cache, _policy_stamp
    with _policy_lock:
        stamp = _policy_file_stamp()
        if _policy_cache is not None and stamp == _policy_stamp:
            return _policy_cache
        policy = (
            yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
            if POLICY_FILE.exists()
            else dict(DEFAULT_POLICY)
        )
        remove_legacy_default_exclusions(policy)
        ensure_required_exclusions(policy)
        _policy_cache = policy
        _policy_stamp = stamp
        return policy


def invalidate_policy_cache():
    global _policy_cache, _policy_stamp
    with _policy_lock:
        _policy_cache = None
        _policy_stamp = None


def classify_source(source):
    meta = source_metadata(source or "unknown")
    parts = set(meta["directories"])
    if parts & {"config", "configuration", "configurazioni"}:
        source_type, authority = "configuration", "high"
    elif parts & {"docs", "documentation", "guides"}:
        source_type, authority = "technical_documentation", "high"
    elif parts & {"marketing", "promo", "certificati", "certifications"}:
        source_type, authority = "marketing", "low"
    else:
        source_type, authority = "document", "medium"
    return {
        "lifecycle": meta["lifecycle"],
        "source_type": source_type,
        "authority": authority,
    }


def source_allowed(source, policy):
    if not source:
        return False
    path = PurePosixPath(source.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return False
    parts = [part.casefold() for part in path.parts]
    directories = set(parts[:-1])
    excluded = {name.casefold() for name in (policy.get("exclude_files") or [])}
    excluded_directories = {
        name.casefold() for name in (policy.get("exclude_directories") or [])
    }
    excluded_top_level = {
        name.casefold() for name in (policy.get("exclude_top_level") or [])
    }
    included = {name.casefold() for name in (policy.get("include_extensions") or [])}
    excluded_extensions = {
        name.casefold() for name in (policy.get("exclude_extensions") or [])
    }
    return (
        not directories.intersection(excluded_top_level)
        and not directories.intersection(excluded_directories)
        and path.name.casefold() not in excluded
        and path.as_posix().casefold() not in excluded
        and path.suffix.lower() in included
        and path.suffix.lower() not in excluded_extensions
    )


def eligible_for_query(source, temporal_intent):
    return source_allowed(source, load_policy()) and (
        temporal_intent != "current"
        or classify_source(source)["lifecycle"] != "historical"
    )


def detect_temporal_intent(query):
    return (
        "historical"
        if re.search(
            r"\b(prima|vecchi[oa]|precedent\w*|storic[oa]|dismess[oa]|passato|avevo|old|previous|historical|formerly)\b",
            query,
            re.I,
        )
        else "current"
    )


def validate_path(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(c in value for c in "\\\x00\r\n:")
    ):
        raise ValueError("Use a relative indexed path with forward slashes")
    if value.startswith("/"):
        raise ValueError("Use a relative indexed path without traversal")
    value = value.rstrip("/")
    if value and (
        value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("Use a relative indexed path without traversal")
    return value


def condition(key, value):
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def query_filter(policy, include_historical=False, path_prefix="", source=""):
    if type(include_historical) is not bool:
        raise ValueError("include_historical must be a boolean")
    must = [
        condition("index_version", INDEX_VERSION),
        condition("ready", True),
        condition("embedding_model", MODEL_NAME),
    ]
    must_not = []
    excluded_directories = [v.casefold() for v in (policy.get("exclude_directories") or [])]
    excluded_top_level = [v.casefold() for v in (policy.get("exclude_top_level") or [])]
    directory_exclusions = sorted(set(excluded_directories) | set(excluded_top_level))
    if directory_exclusions:
        must_not.append(
            models.FieldCondition(
                key="directories", match=models.MatchAny(any=directory_exclusions)
            )
        )
    if excluded_top_level:
        must_not.append(
            models.FieldCondition(
                key="source_top_level", match=models.MatchAny(any=excluded_top_level)
            )
        )
    if not include_historical:
        must_not.append(condition("lifecycle", "historical"))
    if path_prefix:
        prefix = validate_path(path_prefix)
        must.append(
            models.Filter(
                should=[
                    condition("source_prefixes", prefix),
                    condition("source_prefixes_casefold", prefix.casefold()),
                ]
            )
        )
        folder = PurePosixPath(path_prefix)
        if set(part.casefold() for part in folder.parts).intersection(
            set(excluded_directories) | set(excluded_top_level)
        ) or folder.parts[0].casefold() in set(excluded_top_level):
            must.append(condition("document_id", "excluded"))
    if source:
        exact = validate_path(source)
        must.append(
            models.Filter(
                should=[
                    condition("source_paths", exact),
                    condition("source_paths_casefold", exact.casefold()),
                ]
            )
        )
        if not source_allowed(source, policy):
            must.append(condition("document_id", "excluded"))
    included = policy.get("include_extensions") or []
    if included:
        must.append(
            models.FieldCondition(
                key="extension", match=models.MatchAny(any=included)
            )
        )
    else:
        must.append(condition("document_id", "excluded"))
    excluded_extensions = policy.get("exclude_extensions") or []
    if excluded_extensions:
        must_not.append(
            models.FieldCondition(
                key="extension", match=models.MatchAny(any=excluded_extensions)
            )
        )
    for field, paths in (("source_casefold", True), ("filename_casefold", False)):
        values = [
            value.casefold()
            for value in (policy.get("exclude_files") or [])
            if ("/" in value) == paths
        ]
        if values:
            must_not.append(
                models.FieldCondition(key=field, match=models.MatchAny(any=values))
            )
    return models.Filter(must=must, must_not=must_not)


def index_available():
    return client.collection_exists(DENSE_COLLECTION)


def backend_failure(exc, **fields):
    return sanitize(
        {
            **fields,
            "backend": "unavailable",
            "reason": type(exc).__name__,
            "message": BACKEND_MESSAGE,
        }
    )


def legacy_chunk_count():
    return client.count(
        DENSE_COLLECTION,
        count_filter=models.Filter(
            must_not=[
                models.Filter(
                    must=[
                        condition("index_version", INDEX_VERSION),
                        condition("embedding_model", MODEL_NAME),
                    ]
                )
            ]
        ),
        exact=False,
    ).count


def collection_needs_update(info=None):
    try:
        info = info if info is not None else client.get_collection(DENSE_COLLECTION)
        return (
            LEXICAL_VECTOR not in (info.config.params.sparse_vectors or {})
            or legacy_chunk_count() > 0
        )
    except Exception:
        return False


def sanitize(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def public_chunk(payload, policy):
    return {
        "document_id": payload.get("document_id"),
        "source": payload.get("source"),
        "duplicate_sources": [
            s for s in payload.get("duplicate_sources", []) if source_allowed(s, policy)
        ],
        "page": payload.get("page"),
        "section": payload.get("section"),
        "section_index": payload.get("section_index"),
        "chunk_index": payload.get("chunk_index"),
        "document_order": payload.get("document_order"),
        "total_chunks": payload.get("total_chunks"),
        "modified_at": payload.get("modified_at"),
        "text": payload.get("text", ""),
        **classify_source(payload.get("source")),
    }


def read_document(document_id, start_chunk=0, limit=8, include_historical=False):
    if not isinstance(document_id, str) or not re.fullmatch(
        r"[a-f0-9]{64}", document_id
    ):
        raise ValueError("Use a document_id returned by search or browse")
    if (
        type(start_chunk) is not int
        or start_chunk < 0
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValueError(
            "start_chunk must be nonnegative and limit must be between 1 and 20"
        )
    try:
        available = index_available()
    except Exception as exc:
        return backend_failure(
            exc, document_id=document_id, chunks=[], next_chunk=None
        )
    if not available:
        return sanitize(
            {"document_id": document_id, "chunks": [], "next_chunk": None}
        )
    policy = load_policy()
    filters = query_filter(policy, include_historical)
    filters.must.extend(
        [
            condition("document_id", document_id),
            models.FieldCondition(
                key="document_order", range=models.Range(gte=start_chunk)
            ),
        ]
    )
    try:
        points, _ = client.scroll(
            DENSE_COLLECTION,
            scroll_filter=filters,
            limit=limit + 1,
            order_by="document_order",
            with_payload=True,
            with_vectors=False,
        )
    except BACKEND_ERRORS as exc:
        return backend_failure(
            exc, document_id=document_id, chunks=[], next_chunk=None
        )
    visible = [
        point.payload
        for point in points
        if source_allowed((point.payload or {}).get("source"), policy)
    ]
    page = visible[:limit]
    result = {
        "document_id": document_id,
        "chunks": [public_chunk(p, policy) for p in page],
        "next_chunk": visible[limit]["document_order"]
        if len(visible) > limit
        else None,
    }
    if not visible and collection_needs_update():
        result.update(index_update_required=True, message=MIGRATION_MESSAGE)
    return sanitize(result)


def browse_documents(path_prefix="", limit=30, cursor=None, include_historical=False):
    path_prefix = validate_path(path_prefix)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not cursor.isdecimal()
        or not 0 <= int(cursor) < 2**64
    ):
        raise ValueError("Use next_cursor from the previous browse response")
    try:
        available = index_available()
    except Exception as exc:
        return backend_failure(
            exc, path_prefix=path_prefix, documents=[], next_cursor=None
        )
    if not available:
        return sanitize(
            {"path_prefix": path_prefix, "documents": [], "next_cursor": None}
        )
    policy = load_policy()
    filters = query_filter(policy, include_historical, path_prefix)
    filters.must.append(condition("document_order", 0))
    try:
        points, offset = client.scroll(
            DENSE_COLLECTION,
            scroll_filter=filters,
            limit=limit,
            offset=int(cursor) if cursor is not None else None,
            with_payload=True,
            with_vectors=False,
        )
    except BACKEND_ERRORS as exc:
        return backend_failure(
            exc, path_prefix=path_prefix, documents=[], next_cursor=None
        )
    documents = []
    for point in points:
        payload = point.payload or {}
        if source_allowed(payload.get("source"), policy):
            item = public_chunk(payload, policy)
            item["text"] = item["text"][:300]
            documents.append(item)
    result = {
        "path_prefix": path_prefix,
        "documents": documents,
        "next_cursor": str(offset) if offset is not None else None,
    }
    if not documents and collection_needs_update():
        result.update(index_update_required=True, message=MIGRATION_MESSAGE)
    return sanitize(result)


def expand_result_context(item, include_historical=False):
    payload = item.get("payload") or {}
    unchanged = {
        "text": payload.get("text", ""),
        "expanded": False,
        "expansion_mode": "none",
        "expanded_chunks": 1,
    }
    document_id = payload.get("document_id")
    if not document_id:
        return unchanged
    start = max(0, payload.get("document_order", 0) - 1)
    result = read_document(document_id, start, 3, include_historical)
    chunks = [
        chunk
        for chunk in result.get("chunks", [])
        if chunk["section_index"] == payload.get("section_index")
    ]
    if not chunks:
        return unchanged
    text = "\n\n".join(chunk["text"] for chunk in chunks)
    return {
        "text": text[:MAX_CONTEXT_CHARS],
        "expanded": len(chunks) > 1,
        "expansion_mode": "neighbors",
        "expanded_chunks": len(chunks),
        "context_chunks": [
            {k: chunk[k] for k in ("document_order", "page", "section")}
            for chunk in chunks
        ],
    }


def search(
    query, limit=FINAL_LIMIT, path_prefix="", source="", include_historical=None
):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("query must be a non-empty string of at most 1000 characters")
    if "[redacted" in query.casefold():
        raise ValueError("Redacted values cannot be searched")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    path_prefix, source = validate_path(path_prefix), validate_path(source)
    historical = (
        detect_temporal_intent(query) == "historical"
        if include_historical is None
        else include_historical
    )
    policy = load_policy()
    filters = query_filter(policy, historical, path_prefix, source)
    try:
        available = index_available()
    except Exception as exc:
        return backend_failure(exc, query=query, results=[], result_count=0)
    if not available:
        return sanitize(
            {
                "query": query,
                "results": [],
                "result_count": 0,
                "index_update_required": True,
                "message": "No index is available yet. Run an initial indexing job before searching.",
            }
        )
    try:
        info = client.get_collection(DENSE_COLLECTION)
    except BACKEND_ERRORS as exc:
        return backend_failure(exc, query=query, results=[], result_count=0)
    needs_update = collection_needs_update(info)
    lexical_available = LEXICAL_VECTOR in (info.config.params.sparse_vectors or {})
    global model
    current_model = model
    if current_model is None:
        with _model_lock:
            if model is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    model = SentenceTransformer(MODEL_NAME, device=embedding_device())
                except Exception as exc:
                    return backend_failure(
                        exc, query=query, results=[], result_count=0
                    )
            current_model = model
    try:
        vector = current_model.encode(
            [query_text(MODEL_NAME, query)], normalize_embeddings=True
        )[0].tolist()
    except Exception as exc:
        return backend_failure(exc, query=query, results=[], result_count=0)
    try:
        dense_groups = client.query_points_groups(
            DENSE_COLLECTION,
            group_by="document_id",
            group_size=2,
            query=vector,
            query_filter=filters,
            limit=DENSE_LIMIT // 2,
            with_payload=True,
        )
    except BACKEND_ERRORS as exc:
        return backend_failure(exc, query=query, results=[], result_count=0)
    dense = sorted(
        (point for group in dense_groups.groups for point in group.hits),
        key=lambda p: p.score,
        reverse=True,
    )
    lexical = []
    if lexical_available:
        lexical_vector = sparse_vector(query, query=True)
        if lexical_vector.indices:
            try:
                lexical_groups = client.query_points_groups(
                    DENSE_COLLECTION,
                    group_by="document_id",
                    group_size=2,
                    query=lexical_vector,
                    using=LEXICAL_VECTOR,
                    query_filter=filters,
                    limit=LEXICAL_LIMIT // 2,
                    with_payload=True,
                )
            except BACKEND_ERRORS as exc:
                return backend_failure(exc, query=query, results=[], result_count=0)
            lexical = sorted(
                (point for group in lexical_groups.groups for point in group.hits),
                key=lambda p: p.score,
                reverse=True,
            )
    fused = {}
    for channel, points in (("dense", dense), ("lexical", lexical)):
        for rank, point in enumerate(points, 1):
            payload = point.payload or {}
            if not source_allowed(payload.get("source"), policy):
                continue
            entry = fused.setdefault(
                point.id,
                {
                    "payload": payload,
                    "score": 0.0,
                    "dense_rank": None,
                    "lexical_rank": None,
                },
            )
            entry["score"] += 1 / (60 + rank)
            entry[channel + "_rank"] = rank
    terms = set(tokens(query))
    for item in fused.values():
        payload = item["payload"]
        affinity = terms.intersection(
            tokens((payload.get("source") or "") + " " + (payload.get("section") or ""))
        )
        item["score"] *= 1 + min(0.3, len(affinity) * 0.1)
    ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)
    selected, seen = [], set()
    for item in ranked:
        identity = item["payload"].get("document_id") or item["payload"].get("source")
        if identity in seen:
            continue
        seen.add(identity)
        result = {
            **public_chunk(item["payload"], policy),
            "score": round(item["score"], 6),
            "dense_rank": item["dense_rank"],
            "lexical_rank": item["lexical_rank"],
            "expanded": False,
            "expansion_mode": "none",
            "expanded_chunks": 1,
        }
        if len(selected) < 2:
            result.update(expand_result_context(item, historical))
        selected.append(result)
        if len(selected) == limit:
            break
    result = {
        "query": query,
        "temporal_intent": "historical" if historical else "current",
        "candidate_chunks": len(fused),
        "result_count": len(selected),
        "results": selected,
        "retrieval_policy": {
            "historical_sources_allowed": historical,
            "restricted_sources_excluded": True,
        },
        "secret_guard": {"enabled": True, "mode": "ingestion-and-output-redaction"},
    }
    if needs_update:
        result.update(index_update_required=True, message=MIGRATION_MESSAGE)
    return sanitize(result)
