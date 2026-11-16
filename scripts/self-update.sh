#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_FILE="$SCRIPT_DIR/self_update.py"

if [[ ! -f "$PY_FILE" ]]; then
  PY_FILE="$(mktemp "${TMPDIR:-/tmp}/knowledge-self-update.XXXXXX.py")"
  trap 'rm -f "$PY_FILE"' EXIT
  echo "[self-update] Fetching the updater from the repository…" >&2
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "https://raw.githubusercontent.com/paoloronco/knowledge-mcp/main/scripts/self_update.py" -o "$PY_FILE"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$PY_FILE" "https://raw.githubusercontent.com/paoloronco/knowledge-mcp/main/scripts/self_update.py"
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
