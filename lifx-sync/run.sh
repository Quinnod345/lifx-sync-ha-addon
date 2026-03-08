#!/usr/bin/env bash
set -euo pipefail

DATA_DIR=/data
LIGHTS_FILE="${LIGHTS_FILE:-$DATA_DIR/lights.json}"

if [ -z "${PORT:-}" ] && [ -f "$DATA_DIR/options.json" ]; then
    PORT="$(python3 - <<'PY'
import json
from pathlib import Path

options = Path("/data/options.json")
if options.exists():
    data = json.loads(options.read_text(encoding="utf-8"))
    print(data.get("port", 5050))
else:
    print(5050)
PY
)"
else
    PORT="${PORT:-5050}"
fi

mkdir -p "$DATA_DIR"

if [ ! -s "$LIGHTS_FILE" ]; then
    echo "Running initial LIFX discovery..."
    python3 /app/discover.py --output "$LIGHTS_FILE"
fi

export LIGHTS_FILE
export LIFX_SYNC_PORT="$PORT"

exec python3 /app/server.py
