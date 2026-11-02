"""Supported dense embedding profiles for both ingestion and retrieval."""

DEFAULT_MODEL = "e5-small"
MODELS = {
    "e5-small": {
        "name": "E5 multilingual small",
        "model": "intfloat/multilingual-e5-small",
        "collection": "documents",
        "dimensions": 384,
        "batch_size": 32,
        "resources": "Low",
        "speed": "Fast",
        "use_case": "A practical default for mixed-language documents on modest hardware.",
        "note": "Choose this when indexing speed and memory use matter most.",
        "url": "https://huggingface.co/intfloat/multilingual-e5-small",
    },
    "e5-base": {
        "name": "E5 multilingual base",
        "model": "intfloat/multilingual-e5-base",
        "collection": "documents_e5_base",
        "dimensions": 768,
        "batch_size": 16,
        "resources": "Medium",
        "speed": "Medium",
        "use_case": "A larger multilingual model for hosts with more RAM and CPU time.",
        "note": "Compare search results with E5 small on your own documents.",
        "url": "https://huggingface.co/intfloat/multilingual-e5-base",
    },
    "bge-m3": {
        "name": "BGE-M3",
        "model": "BAAI/bge-m3",
        "collection": "documents_bge_m3",
        "dimensions": 1024,
        "batch_size": 4,
        "resources": "High",
        "speed": "Slow",
        "use_case": "A large multilingual model for hosts with ample memory and CPU time.",
        "note": "Uses dense semantic vectors plus the shared lexical index. CPU indexing takes the most time.",
        "url": "https://huggingface.co/BAAI/bge-m3",
    },
}


def document_text(model_name, text):
    return text if model_name == "BAAI/bge-m3" else f"passage: {text}"


def query_text(model_name, text):
    return text if model_name == "BAAI/bge-m3" else f"query: {text}"
