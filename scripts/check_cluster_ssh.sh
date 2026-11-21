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

if [[ -z "${SPARK_HOSTS:-}" ]]; then
  echo "No SSH targets configured. Set SPARK_HOSTS in $ENV_FILE." >&2
  exit 1
fi
read -r -a HOSTS <<<"${SPARK_HOSTS//,/ }"

SSH_ARGS=(
  -o BatchMode=yes
  -o ConnectTimeout="${SPARK_SSH_CONNECT_TIMEOUT:-${SPARK_SSH_TIMEOUT:-8}}"
  -o StrictHostKeyChecking="${SPARK_SSH_STRICT_HOST_KEY_CHECKING:-accept-new}"
)
if [[ -n "${SPARK_SSH_USER:-}" ]]; then
  SSH_ARGS+=(-l "$SPARK_SSH_USER")
fi
if [[ -n "${SPARK_SSH_PORT:-}" ]]; then
  SSH_ARGS+=(-p "$SPARK_SSH_PORT")
fi
if [[ -n "${SPARK_SSH_KEY_PATH:-}" ]]; then
  SSH_ARGS+=(-i "$SPARK_SSH_KEY_PATH" -o IdentitiesOnly=yes)
fi

GPU_QUERY_COMMAND="${SPARK_GPU_QUERY_COMMAND-nvidia-smi}"
REMOTE_GPU_QUERY="$(printf '%q' "$GPU_QUERY_COMMAND")"

for host in "${HOSTS[@]}"; do
  printf '%-15s ' "$host"
  ssh \
    "${SSH_ARGS[@]}" \
    "$host" \
    "bash -s -- $REMOTE_GPU_QUERY" <<'REMOTE'
set -euo pipefail
gpu_command="${1:-}"
printf "%s " "$(hostname)"
if [[ -z "$gpu_command" ]]; then
  echo "GPU query disabled"
  exit 0
fi
read -r -a GPU_ARGS <<<"$gpu_command"
if ! output="$("${GPU_ARGS[@]}" --query-gpu=name,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)"; then
  output=""
fi
echo "${output:-GPU unavailable}"
REMOTE
    2>/dev/null || echo "UNAVAILABLE"
done
