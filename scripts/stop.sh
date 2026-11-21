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
STOP_TIMEOUT="${SPARK_STOP_TIMEOUT:-10}"

if [[ ! "$STOP_TIMEOUT" =~ ^[0-9]+$ ]] || (( STOP_TIMEOUT < 1 )); then
  echo "SPARK_STOP_TIMEOUT must be a positive integer." >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  PID="$(cat "$PID_FILE")"
  kill "$PID"
  for ((attempt = 1; attempt <= STOP_TIMEOUT; attempt++)); do
    if ! kill -0 "$PID" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Spark Grid stopped"
      exit 0
    fi
    sleep 1
  done
  echo "Spark Grid is still running after ${STOP_TIMEOUT}s (pid $PID); PID file was kept." >&2
  exit 1
else
  rm -f "$PID_FILE"
  echo "Spark Grid is not running"
fi
