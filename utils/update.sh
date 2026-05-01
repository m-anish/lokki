#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/firmware/micropython/src"
WEB_DIR="$REPO_ROOT/web/app"

if ! command -v mpremote >/dev/null 2>&1; then
    echo "[update] ERROR: mpremote not found on PATH" >&2
    exit 1
fi

if [ ! -d "$SRC_DIR" ]; then
    echo "[update] ERROR: firmware source not found: $SRC_DIR" >&2
    exit 1
fi

if [ ! -d "$WEB_DIR" ]; then
    echo "[update] ERROR: web asset source not found: $WEB_DIR" >&2
    exit 1
fi

echo "[update] Closing any running mpremote sessions..."
pkill -f mpremote 2>/dev/null || true
sleep 0.5

echo "[update] Flashing firmware $SRC_DIR -> :/ ..."
# Copy firmware files, excluding __pycache__ directories
find "$SRC_DIR" -type f -name "*.py" | while read -r file; do
    rel_path="${file#$SRC_DIR/}"
    dir_path="$(dirname "$rel_path")"
    if [ "$dir_path" != "." ]; then
        mpremote connect auto fs mkdir -p ":$dir_path" 2>/dev/null || true
    fi
    mpremote connect auto fs cp "$file" ":$rel_path"
done

echo "[update] Flashing web assets $WEB_DIR -> :/www ..."
mpremote connect auto fs mkdir www 2>/dev/null || true
mpremote connect auto fs cp "$WEB_DIR/index.html" :www/
mpremote connect auto fs cp "$WEB_DIR/config-builder.html" :www/
mpremote connect auto fs cp "$WEB_DIR/config.schema.json" :www/
mpremote connect auto fs cp -r "$WEB_DIR/vendor" :www/

echo "[update] Resetting device..."
mpremote connect auto reset

echo "[update] Done."
