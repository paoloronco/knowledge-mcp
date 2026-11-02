"""Best-effort credential redaction shared by ingestion and MCP output."""

import re


def redact_secrets(text):
    """Secret Guard v2.1: redact credentials before retrieval output leaves the MCP boundary."""

    if not text:
        return text

    value = str(text)

    # Private keys.
    value = re.sub(
        r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
    )

    # Authorization headers.
    value = re.sub(
        r"(?im)^(\s*(?:authorization|proxy-authorization)\s*:\s*)[^\r\n]+",
        r"\1[REDACTED]",
        value,
    )

    # Assignment-style credentials.
    # Examples: QDRANT_API_KEY=x, password: x, export TOKEN=x.
    assignment_pattern = (
        r"(?im)^(\s*(?:export\s+)?[`\"\']?"
        r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|psw|api[_ -]?key|apikey|"
        r"client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
        r"auth[_ -]?token|bearer[_ -]?token|private[_ -]?key|secret|token)"
        r"[A-Za-z0-9_.-]*[`\"\']?\s*[:=]\s*)[^\r\n]+"
    )
    value = re.sub(
        assignment_pattern,
        r"\1[REDACTED]",
        value,
    )

    # Label-style credentials.
    value = re.sub(
        r"(?im)^(\s*(?:password|passwd|pwd|psw|api[ _-]?key|client[ _-]?secret|access[ _-]?token|refresh[ _-]?token)\s+)\S[^\r\n]*",
        r"\1[REDACTED]",
        value,
    )

    # Bearer credentials anywhere in a line/prose.
    # Handles Bearer TOKEN, Bearer: TOKEN, Bearer=`TOKEN`, etc.
    value = re.sub(
        r"(?i)\bBearer\b[\s:`\'\"=-]+[A-Za-z0-9._~+/=-]{8,}",
        "Bearer [REDACTED]",
        value,
    )

    # Well-known credential formats.
    value = re.sub(
        r"\bsk-[A-Za-z0-9_-]{16,}\b",
        "[REDACTED API KEY]",
        value,
    )
    value = re.sub(
        r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
        "[REDACTED TOKEN]",
        value,
    )
    value = re.sub(
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        "[REDACTED TOKEN]",
        value,
    )
    value = re.sub(
        r"\bAIza[0-9A-Za-z_-]{30,}\b",
        "[REDACTED API KEY]",
        value,
    )
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED TOKEN]",
        value,
    )

    # Credentials embedded in URLs.
    value = re.sub(
        r"(?i)\b(https?|mongodb(?:\+srv)?|postgres(?:ql)?|mysql)://([^/\s:@]+):([^@\s/]+)@",
        r"\1://\2:[REDACTED]@",
        value,
    )

    return value
