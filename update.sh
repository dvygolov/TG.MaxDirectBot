#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-maxdirectbot}"
LOG_PREFIX="[TG.MaxDirectBot][update]"

cd "$PROJECT_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "$LOG_PREFIX ERROR: есть локальные изменения; обновление остановлено."
  exit 1
fi

git fetch --all --prune
git merge --ff-only '@{u}'
"$PROJECT_ROOT/build.sh"
systemctl restart "${SERVICE_NAME}.service"
systemctl is-active --quiet "${SERVICE_NAME}.service"

echo "$LOG_PREFIX Обновление завершено, сервис активен."
