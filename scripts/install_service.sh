#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR"
cp "$ROOT/scripts/spark-grid.service" "$UNIT_DIR/spark-grid.service"
systemctl --user daemon-reload
systemctl --user enable --now spark-grid.service
systemctl --user --no-pager status spark-grid.service
