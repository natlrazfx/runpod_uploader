#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
APP="$SCRIPT_DIR/runpod_uploader_gui.py"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Virtual environment not found. Create it with:"
  echo "  python3 -m venv \"$SCRIPT_DIR/.venv\""
  exit 1
fi

exec "$VENV_PY" "$APP"
