#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
PY_FILE=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  PY_FILE="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd)/self_update.py"
fi

if [[ -z "$PY_FILE" || ! -f "$PY_FILE" ]]; then
  TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/knowledge-self-update.XXXXXX")"
  trap 'rm -rf "$TMP_DIR"' EXIT
  PY_FILE="$TMP_DIR/self_update.py"
  echo "[self-update] Fetching the updater from the repository…" >&2
  URL="https://raw.githubusercontent.com/paoloronco/knowledge-mcp/main/scripts/self_update.py"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$PY_FILE"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$PY_FILE" "$URL"
  else
    echo "[self-update] curl or wget is required to fetch the updater." >&2
    exit 1
  fi
fi

PY_BIN="${PYTHON:-}"
if [[ -z "$PY_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PY_BIN=python3
  elif command -v python >/dev/null 2>&1; then
    PY_BIN=python
  else
    echo "[self-update] Python 3 is required (set PYTHON=/path/to/python3)." >&2
    exit 1
  fi
fi

exec "$PY_BIN" "$PY_FILE" "$@"
