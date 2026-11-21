#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/.server.pid"
LOG_FILE="$ROOT/.server.log"

cd "$ROOT"
mkdir -p data

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Spark Grid already running: $(cat "$PID_FILE")"
  exit 0
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

nohup python3 -m app.main >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
sleep 1

PORT="${SPARK_MONITOR_PORT:-6301}"
if curl --fail --silent --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  echo "Spark Grid started: http://0.0.0.0:$PORT (pid $(cat "$PID_FILE"))"
else
  echo "Spark Grid failed to start; inspect $LOG_FILE" >&2
  exit 1
fi
