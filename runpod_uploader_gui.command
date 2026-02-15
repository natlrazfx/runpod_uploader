#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
APP="$SCRIPT_DIR/runpod_uploader_gui.py"

if [[ -x "$VENV_PY" ]]; then
  exec "$VENV_PY" "$APP"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$APP"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$APP"
fi

echo "Python not found. Install Python 3 or create .venv:"
echo "  python3 -m venv \"$SCRIPT_DIR/.venv\""
exit 1
