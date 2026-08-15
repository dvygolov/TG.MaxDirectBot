#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-maxdirectbot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
LOG_PREFIX="[TG.MaxDirectBot][service]"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload
  echo "$LOG_PREFIX Сервис удалён. Данные и .env сохранены."
  exit 0
fi

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  echo "$LOG_PREFIX ERROR: сначала создайте $PROJECT_ROOT/.env"
  exit 1
fi

if ! grep -Eq '^[[:space:]]*UPDATE_MODE[[:space:]]*=[[:space:]]*polling[[:space:]]*$' \
  "$PROJECT_ROOT/.env"; then
  echo "$LOG_PREFIX ERROR: для бездоменного сервиса задайте UPDATE_MODE=polling"
  exit 1
fi

if [[ ! -x "$PROJECT_ROOT/.venv/bin/tg-max-direct-bot" ]]; then
  "$PROJECT_ROOT/build.sh"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TG.MaxDirectBot Telegram Business to MAX bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PROJECT_ROOT/.venv/bin/tg-max-direct-bot run-polling
EnvironmentFile=$PROJECT_ROOT/.env
User=$SERVICE_USER
Group=$SERVICE_USER
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

chmod 600 "$PROJECT_ROOT/.env"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo "$LOG_PREFIX Сервис установлен: ${SERVICE_NAME}.service"
echo "$LOG_PREFIX Статус: systemctl status ${SERVICE_NAME}.service"
echo "$LOG_PREFIX Логи:  journalctl -u ${SERVICE_NAME}.service -f"
