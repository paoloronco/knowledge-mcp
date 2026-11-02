"""Shared, versioned payload and lexical representation for indexing and search."""

import hashlib
import re
from collections import Counter
from pathlib import PurePosixPath

from qdrant_client import models

INDEX_VERSION = 2
LEXICAL_VECTOR = "lexical"
HISTORICAL_DIRECTORIES = {"dismessi", "old", "backup", "backups", "archive", "archivio"}
STOPWORDS = set(
    "a an and are as at be by for from in is it of on or that the this to was with "
    "a al alla anche che con da dal dalla degli dei del della delle di e è gli i il "
    "in la le lo nel nella o per quale quali sono su un una come cosa dove quando".split()
)


def tokens(text):
    # Preserve identifiers, but also match their components (e.g. nginx.conf / nginx).
    result = []
    for token in re.findall(r"[^\W_]+(?:[_.+-][^\W_]+)*", text.casefold()):
        parts = re.split(r"[_.+-]", token)
        result.extend(
            part for part in dict.fromkeys([token, *parts]) if part not in STOPWORDS
        )
    return result


def sparse_vector(text, source="", section="", query=False):
    counts = Counter(tokens(text))
    length = max(1, sum(counts.values()))
    if not query:
        counts.update(
            {term: count * 3 for term, count in Counter(tokens(source)).items()}
        )
        counts.update(
            {term: count * 2 for term, count in Counter(tokens(section or "")).items()}
        )
    weights = {}
    for term, frequency in counts.items():
        index = int.from_bytes(
            hashlib.blake2s(term.encode(), digest_size=4).digest(), "little"
        )
        # BM25-style TF with a fixed reference passage length; Qdrant maintains IDF.
        weight = (
            1.0
            if query
            else frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / 256))
        )
        weights[index] = weights.get(index, 0.0) + weight
    indices = sorted(weights)
    return models.SparseVector(
        indices=indices, values=[weights[index] for index in indices]
    )


def source_metadata(source, duplicate_sources=()):
    path = PurePosixPath(source.replace("\\", "/"))
    directories = [part.casefold() for part in path.parts[:-1]]
    prefixes = set()
    for name in (source, *duplicate_sources):
        parts = PurePosixPath(name).parts
        prefixes.update(
            PurePosixPath(*parts[:end]).as_posix() for end in range(1, len(parts))
        )
    return {
        "index_version": INDEX_VERSION,
        "directories": directories,
        "source_top_level": path.parts[0].casefold(),
        "source_prefixes": sorted(prefixes),
        "source_paths": [source, *duplicate_sources],
        "lifecycle": "historical"
        if HISTORICAL_DIRECTORIES.intersection(directories)
        else "current_or_unknown",
    }


def ensure_search_schema(client, collection):
    info = client.get_collection(collection)
    if LEXICAL_VECTOR not in (info.config.params.sparse_vectors or {}):
        client.create_vector_name(
            collection,
            LEXICAL_VECTOR,
            models.SparseVectorNameConfig(
                sparse=models.SparseVectorConfig(modifier=models.Modifier.IDF)
            ),
        )
    fields = {
        "document_id": models.PayloadSchemaType.KEYWORD,
        "embedding_model": models.PayloadSchemaType.KEYWORD,
        "source": models.PayloadSchemaType.KEYWORD,
        "filename": models.PayloadSchemaType.KEYWORD,
        "source_casefold": models.PayloadSchemaType.KEYWORD,
        "filename_casefold": models.PayloadSchemaType.KEYWORD,
        "extension": models.PayloadSchemaType.KEYWORD,
        "source_prefixes": models.PayloadSchemaType.KEYWORD,
        "source_paths": models.PayloadSchemaType.KEYWORD,
        "source_top_level": models.PayloadSchemaType.KEYWORD,
        "directories": models.PayloadSchemaType.KEYWORD,
        "lifecycle": models.PayloadSchemaType.KEYWORD,
        "index_version": models.PayloadSchemaType.INTEGER,
        "ready": models.PayloadSchemaType.BOOL,
        "document_order": models.PayloadSchemaType.INTEGER,
    }
    for field, schema in fields.items():
        if field not in info.payload_schema:
            client.create_payload_index(collection, field, schema, wait=True)
