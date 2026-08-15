#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_PREFIX="[TG.MaxDirectBot][build]"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "$LOG_PREFIX ERROR: $PYTHON_BIN не найден."
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  echo "$LOG_PREFIX ERROR: требуется Python 3.9 или новее."
  exit 1
fi

echo "$LOG_PREFIX Подготовка $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
python -m pip install --no-cache-dir --upgrade pip wheel >/dev/null
python -m pip install --no-cache-dir "$PROJECT_ROOT"
deactivate

echo "$LOG_PREFIX Готово."
