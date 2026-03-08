#!/usr/bin/env bash
set -euo pipefail

DATA_DIR=/data
LIGHTS_FILE="${LIGHTS_FILE:-$DATA_DIR/lights.json}"
DISCOVERED_SENTINEL="$DATA_DIR/.discovery_done"

mkdir -p "$DATA_DIR"

# Read port from add-on options.json if present, otherwise default to 5050.
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

# Always run discovery on the very first boot (sentinel not present).
# On subsequent boots, only run if lights.json is missing or empty.
# The sentinel is written to /data so it persists across add-on updates.
if [ ! -f "$DISCOVERED_SENTINEL" ] || [ ! -s "$LIGHTS_FILE" ]; then
    echo "Running LIFX light discovery..."
    python3 /app/discover.py --output "$LIGHTS_FILE" && touch "$DISCOVERED_SENTINEL"
    echo "Discovery complete."
else
    echo "Using cached lights from $LIGHTS_FILE (use Re-discover in the web UI or integration to refresh)."
fi

export LIGHTS_FILE
export LIFX_SYNC_PORT="$PORT"

exec python3 /app/server.py
