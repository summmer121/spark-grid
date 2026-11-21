#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SPARK_ENV_FILE:-$ROOT/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="${SPARK_UNIT_NAME:-spark-grid}"
PYTHON_BIN="${SPARK_PYTHON:-python3}"

if [[ ! "$UNIT_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  echo "SPARK_UNIT_NAME contains unsupported characters." >&2
  exit 1
fi

if [[ "$PYTHON_BIN" != */* ]]; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable is not available. Set SPARK_PYTHON." >&2
  exit 1
fi

quote_unit() {
  printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

mkdir -p "$UNIT_DIR"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME.service"
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Spark Grid monitoring center
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(quote_unit "$ROOT")
Environment=$(quote_unit "HOME=$HOME")
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$(quote_unit "$ENV_FILE")
ExecStart=$(quote_unit "$PYTHON_BIN") -m app.main
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
EOF

echo "Installed $UNIT_FILE"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME.service"
systemctl --user --no-pager status "$UNIT_NAME.service"
