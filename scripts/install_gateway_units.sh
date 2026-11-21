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

SERVICE_USER="${SPARK_GATEWAY_USER:-${SUDO_USER:-}}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  echo "Set SPARK_GATEWAY_USER to the account that should run the gateway." >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Gateway user does not exist: $SERVICE_USER" >&2
  exit 1
fi

SERVICE_GROUP="${SPARK_GATEWAY_GROUP:-$(id -gn "$SERVICE_USER")}"
SERVICE_HOME="${SPARK_GATEWAY_HOME:-$(getent passwd "$SERVICE_USER" | cut -d: -f6)}"
if [[ -z "$SERVICE_HOME" ]]; then
  echo "Could not determine the home directory for $SERVICE_USER." >&2
  exit 1
fi
CONFIG_DIR="${SPARK_GATEWAY_CONFIG_DIR:-/etc/spark-grid}"
UNIT_DIR="${SPARK_SYSTEM_UNIT_DIR:-/etc/systemd/system}"
TLS_ENV_FILE="$CONFIG_DIR/tls.env"
TUNNEL_ENV_FILE="$CONFIG_DIR/tunnel.env"
TLS_UNIT_NAME="${SPARK_TLS_UNIT_NAME:-spark-grid-tls}"
TUNNEL_UNIT_NAME="${SPARK_TUNNEL_UNIT_NAME:-spark-grid-tunnel}"
PYTHON_BIN="${SPARK_PYTHON:-python3}"
SSH_BIN="${SPARK_GATEWAY_SSH_BIN:-ssh}"

if [[ "$PYTHON_BIN" != */* ]]; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable is not available. Set SPARK_PYTHON." >&2
  exit 1
fi
if [[ "$SSH_BIN" != */* ]]; then
  SSH_BIN="$(command -v "$SSH_BIN" || true)"
fi
if [[ -z "$SSH_BIN" || ! -x "$SSH_BIN" ]]; then
  echo "SSH executable is not available. Set SPARK_GATEWAY_SSH_BIN." >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR" "$UNIT_DIR"

if [[ ! -f "$TLS_ENV_FILE" ]]; then
  cat >"$TLS_ENV_FILE" <<EOF
SPARK_TLS_LISTEN=${SPARK_TLS_LISTEN:-0.0.0.0}
SPARK_TLS_PORT=${SPARK_TLS_PORT:-30001}
SPARK_TLS_TARGET=${SPARK_TLS_TARGET:-127.0.0.1:16301}
SPARK_TLS_CERT=${SPARK_TLS_CERT:-/etc/spark-grid/tls.crt}
SPARK_TLS_KEY=${SPARK_TLS_KEY:-/etc/spark-grid/tls.key}
SPARK_TLS_CONNECT_TIMEOUT=${SPARK_TLS_CONNECT_TIMEOUT:-5}
SPARK_TLS_POLL_TIMEOUT=${SPARK_TLS_POLL_TIMEOUT:-30}
EOF
fi

if [[ ! -f "$TUNNEL_ENV_FILE" ]]; then
  cat >"$TUNNEL_ENV_FILE" <<EOF
SPARK_TUNNEL_KEY=${SPARK_TUNNEL_KEY:-$SERVICE_HOME/.ssh/id_ed25519}
SPARK_TUNNEL_PORT=${SPARK_TUNNEL_PORT:-22}
SPARK_TUNNEL_USER=${SPARK_TUNNEL_USER:-$SERVICE_USER}
SPARK_TUNNEL_HOST=${SPARK_TUNNEL_HOST:-gateway.example.com}
SPARK_TUNNEL_BIND=${SPARK_TUNNEL_BIND:-127.0.0.1}
SPARK_TUNNEL_LOCAL_PORT=${SPARK_TUNNEL_LOCAL_PORT:-16301}
SPARK_TUNNEL_TARGET_HOST=${SPARK_TUNNEL_TARGET_HOST:-127.0.0.1}
SPARK_TUNNEL_TARGET_PORT=${SPARK_TUNNEL_TARGET_PORT:-6301}
SPARK_TUNNEL_ALIVE_INTERVAL=${SPARK_TUNNEL_ALIVE_INTERVAL:-30}
SPARK_TUNNEL_ALIVE_COUNT=${SPARK_TUNNEL_ALIVE_COUNT:-3}
SPARK_TUNNEL_HOST_KEY_CHECKING=${SPARK_TUNNEL_HOST_KEY_CHECKING:-accept-new}
EOF
fi
chmod 0600 "$TLS_ENV_FILE" "$TUNNEL_ENV_FILE"
chown "$SERVICE_USER:$SERVICE_GROUP" "$TLS_ENV_FILE" "$TUNNEL_ENV_FILE"

render_unit() {
  local template="$1"
  local output="$2"
  local rendered
  rendered="$(<"$template")"
  rendered="${rendered//@SPARK_SERVICE_USER@/$SERVICE_USER}"
  rendered="${rendered//@SPARK_SERVICE_GROUP@/$SERVICE_GROUP}"
  rendered="${rendered//@SPARK_SERVICE_HOME@/$SERVICE_HOME}"
  rendered="${rendered//@SPARK_TLS_ENV_FILE@/$TLS_ENV_FILE}"
  rendered="${rendered//@SPARK_TUNNEL_ENV_FILE@/$TUNNEL_ENV_FILE}"
  rendered="${rendered//@SPARK_PYTHON@/$PYTHON_BIN}"
  rendered="${rendered//@SPARK_TLS_PROXY@/$ROOT/deploy/tls_proxy.py}"
  rendered="${rendered//@SPARK_SSH_BIN@/$SSH_BIN}"
  printf '%s\n' "$rendered" >"$output"
  chmod 0644 "$output"
}

render_unit \
  "$ROOT/deploy/spark-grid-tls.service" \
  "$UNIT_DIR/$TLS_UNIT_NAME.service"
render_unit \
  "$ROOT/deploy/spark-grid-tunnel.service" \
  "$UNIT_DIR/$TUNNEL_UNIT_NAME.service"

systemctl daemon-reload
if [[ "${SPARK_GATEWAY_ENABLE:-0}" == "1" ]]; then
  systemctl enable --now "$TUNNEL_UNIT_NAME.service" "$TLS_UNIT_NAME.service"
  systemctl --no-pager status \
    "$TUNNEL_UNIT_NAME.service" \
    "$TLS_UNIT_NAME.service"
else
  echo "Gateway units installed. Set SPARK_GATEWAY_ENABLE=1 or enable them explicitly."
fi
