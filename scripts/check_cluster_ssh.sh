#!/usr/bin/env bash
set -euo pipefail

DEFAULT_HOSTS="spark1 spark2 spark3 spark4 spark5 spark6 spark7 spark8"
read -r -a HOSTS <<<"${SPARK_HOSTS:-$DEFAULT_HOSTS}"

if [[ ${#HOSTS[@]} -eq 0 ]]; then
  echo "No SSH targets configured. Set SPARK_HOSTS or configure SSH aliases." >&2
  exit 1
fi

for host in "${HOSTS[@]}"; do
  printf '%-15s ' "$host"
  ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=4 \
    -o StrictHostKeyChecking=accept-new \
    "$host" \
    'printf "%s " "$(hostname)"; nvidia-smi --query-gpu=name,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1' \
    2>/dev/null || echo "UNAVAILABLE"
done
