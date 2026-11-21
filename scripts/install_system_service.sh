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

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer with sudo: sudo $0" >&2
  exit 1
fi

resolve_path() {
  local value="$1"
  if [[ "$value" == "~" ]]; then
    value="${SERVICE_HOME:-$HOME}"
  elif [[ "$value" == "~/"* ]]; then
    value="${SERVICE_HOME:-$HOME}/${value#\~/}"
  fi
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s/%s\n' "$ROOT" "$value"
  fi
}

systemd_quote() {
  local value="$1"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "Service paths must not contain newlines." >&2
    exit 1
  fi
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

SERVICE_NAME="${SPARK_SYSTEM_SERVICE_NAME:-spark-grid}"
SERVICE_USER="${SPARK_SYSTEM_USER:-${SUDO_USER:-}}"
if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  echo "SPARK_SYSTEM_SERVICE_NAME contains unsupported characters." >&2
  exit 1
fi
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  echo "Set SPARK_SYSTEM_USER to the unprivileged account that owns this project." >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Service user does not exist: $SERVICE_USER" >&2
  exit 1
fi

SERVICE_GROUP="${SPARK_SYSTEM_GROUP:-$(id -gn "$SERVICE_USER")}"
SERVICE_HOME="${SPARK_SYSTEM_HOME:-$(getent passwd "$SERVICE_USER" | cut -d: -f6)}"
if [[ -z "$SERVICE_HOME" ]]; then
  echo "Could not determine the home directory for $SERVICE_USER." >&2
  exit 1
fi

PYTHON_BIN="${SPARK_PYTHON:-python3}"
if [[ "$PYTHON_BIN" != */* ]]; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable is not available. Set SPARK_PYTHON." >&2
  exit 1
fi

ENV_FILE="$(resolve_path "$ENV_FILE")"
DATA_DIR="$(resolve_path "${SPARK_MONITOR_DATA:-./data}")"
DATA_DIR="${DATA_DIR%/}"
if [[ -z "$DATA_DIR" ]]; then
  echo "SPARK_MONITOR_DATA must not resolve to the filesystem root." >&2
  exit 1
fi

READ_WRITE_PATHS=()
add_read_write_path() {
  local candidate
  candidate="$(resolve_path "$1")"
  candidate="${candidate%/}"
  if [[ -z "$candidate" || "$candidate" == "/" ]]; then
    echo "Refusing to grant a systemd service write access to /." >&2
    exit 1
  fi
  local existing
  for existing in "${READ_WRITE_PATHS[@]:-}"; do
    if [[ "$existing" == "$candidate" ]]; then
      return
    fi
  done
  READ_WRITE_PATHS+=("$candidate")
}

add_read_write_path "$DATA_DIR"

DATABASE_VALUE="${SPARK_DATABASE_PATH:-}"
if [[ -n "$DATABASE_VALUE" ]]; then
  add_read_write_path "$(dirname "$(resolve_path "$DATABASE_VALUE")")"
fi

CREDENTIAL_KEY_VALUE="${SPARK_CREDENTIAL_KEY_FILE:-${SPARK_SSH_SECRET_KEY_FILE:-}}"
if [[ -n "$CREDENTIAL_KEY_VALUE" ]]; then
  add_read_write_path "$(dirname "$(resolve_path "$CREDENTIAL_KEY_VALUE")")"
fi

UNIT_DIR="${SPARK_SYSTEM_UNIT_DIR:-/etc/systemd/system}"
UNIT_TEMPLATE="$ROOT/deploy/spark-grid.system.service"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"

mkdir -p "$UNIT_DIR" "${READ_WRITE_PATHS[@]}"
chown "$SERVICE_USER:$SERVICE_GROUP" "${READ_WRITE_PATHS[@]}"

QUOTED_READ_WRITE_PATHS=()
for read_write_path in "${READ_WRITE_PATHS[@]}"; do
  QUOTED_READ_WRITE_PATHS+=("$(systemd_quote "$read_write_path")")
done
READ_WRITE_VALUE="${QUOTED_READ_WRITE_PATHS[*]}"

rendered="$(<"$UNIT_TEMPLATE")"
rendered="${rendered//@SPARK_SERVICE_USER@/$SERVICE_USER}"
rendered="${rendered//@SPARK_SERVICE_GROUP@/$SERVICE_GROUP}"
rendered="${rendered//@SPARK_SERVICE_HOME@/$SERVICE_HOME}"
rendered="${rendered//@SPARK_ROOT@/$ROOT}"
rendered="${rendered//@SPARK_ENV_FILE@/$ENV_FILE}"
rendered="${rendered//@SPARK_PYTHON@/$PYTHON_BIN}"
rendered="${rendered//@SPARK_DATA_DIR@/$DATA_DIR}"
rendered="${rendered//@SPARK_READ_WRITE_PATHS@/$READ_WRITE_VALUE}"

printf '%s\n' "$rendered" >"$UNIT_FILE"
chmod 0644 "$UNIT_FILE"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"
systemctl --no-pager status "$SERVICE_NAME.service"
