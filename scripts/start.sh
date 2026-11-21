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

resolve_path() {
  local value="${1:-$2}"
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s/%s\n' "$ROOT" "$value"
  fi
}

PID_FILE="$(resolve_path "${SPARK_PID_FILE:-}" ".server.pid")"
LOG_FILE="$(resolve_path "${SPARK_LOG_FILE:-}" ".server.log")"
DATA_DIR="$(resolve_path "${SPARK_MONITOR_DATA:-}" "data")"
PYTHON_BIN="${SPARK_PYTHON:-python3}"
STARTUP_TIMEOUT="${SPARK_STARTUP_TIMEOUT:-15}"
HEALTH_TIMEOUT="${SPARK_HEALTH_TIMEOUT:-2}"

if [[ "$PYTHON_BIN" == */* ]]; then
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python executable is not available: $PYTHON_BIN" >&2
    exit 1
  fi
else
  PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python executable was not found. Set SPARK_PYTHON." >&2
    exit 1
  fi
fi

if [[ ! "$STARTUP_TIMEOUT" =~ ^[0-9]+$ ]] || (( STARTUP_TIMEOUT < 1 )); then
  echo "SPARK_STARTUP_TIMEOUT must be a positive integer." >&2
  exit 1
fi
if [[ ! "$HEALTH_TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] || \
  ! awk "BEGIN { exit !($HEALTH_TIMEOUT > 0) }"; then
  echo "SPARK_HEALTH_TIMEOUT must be a positive number." >&2
  exit 1
fi

cd "$ROOT"
mkdir -p "$DATA_DIR" "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Spark Grid already running: $(cat "$PID_FILE")"
  exit 0
fi

nohup "$PYTHON_BIN" -m app.main >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

HOST="${SPARK_MONITOR_HOST:-0.0.0.0}"
PORT="${SPARK_MONITOR_PORT:-6301}"
HEALTH_HOST="${SPARK_HEALTH_HOST:-}"
if [[ -z "$HEALTH_HOST" ]]; then
  case "$HOST" in
    0.0.0.0) HEALTH_HOST="127.0.0.1" ;;
    ::) HEALTH_HOST="[::1]" ;;
    *:*) HEALTH_HOST="[$HOST]" ;;
    *) HEALTH_HOST="$HOST" ;;
  esac
fi
HEALTH_PATH="${SPARK_HEALTH_PATH:-/api/health}"
if [[ "$HEALTH_PATH" != /* ]]; then
  HEALTH_PATH="/$HEALTH_PATH"
fi
HEALTH_URL="${SPARK_HEALTH_URL:-http://$HEALTH_HOST:$PORT$HEALTH_PATH}"

for ((attempt = 1; attempt <= STARTUP_TIMEOUT; attempt++)); do
  if "$PYTHON_BIN" - "$HEALTH_URL" "$HEALTH_TIMEOUT" <<'PY'
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2])) as response:
        raise SystemExit(0 if 200 <= response.status < 400 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    echo "Spark Grid started: http://$HOST:$PORT (pid $(cat "$PID_FILE"))"
    exit 0
  fi
  if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "Spark Grid failed to start; inspect $LOG_FILE" >&2
exit 1
