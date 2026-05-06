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

echo "[update] Creating remote directory tree..."
# mpremote's `fs mkdir` does not support -p. We walk each path component
# ourselves and mkdir each level, deduped, shallowest-first. This makes
# the script work on a freshly-flashed Pico with no existing dirs, while
# still being a no-op on subsequent runs (the 2>/dev/null swallows EEXIST).
declare -A _made_dirs
mk_remote_dir() {
    local path="$1"
    local cur=""
    local IFS='/'
    local part
    for part in $path; do
        [ -z "$part" ] && continue
        cur="${cur:+$cur/}$part"
        if [ -z "${_made_dirs[$cur]:-}" ]; then
            mpremote connect auto fs mkdir ":$cur" 2>/dev/null || true
            _made_dirs[$cur]=1
        fi
    done
}

# Pre-create every dir under SRC_DIR (skipping __pycache__), shallowest first.
while IFS= read -r dir; do
    mk_remote_dir "$dir"
done < <(find "$SRC_DIR" -mindepth 1 -type d -not -path '*/__pycache__*' \
         | sed "s|^$SRC_DIR/||" \
         | sort)

echo "[update] Flashing firmware $SRC_DIR -> :/ ..."
find "$SRC_DIR" -type f -name "*.py" -not -path '*/__pycache__/*' | while read -r file; do
    rel_path="${file#$SRC_DIR/}"
    mpremote connect auto fs cp "$file" ":$rel_path"
done

echo "[update] Flashing web assets $WEB_DIR -> :/www ..."
mk_remote_dir "www"
mpremote connect auto fs cp "$WEB_DIR/index.html" :www/
mpremote connect auto fs cp "$WEB_DIR/dashboard.html" :www/
mpremote connect auto fs cp "$WEB_DIR/config-builder.html" :www/
mpremote connect auto fs cp "$WEB_DIR/config.schema.json" :www/
# `fs cp -r` on mpremote creates the destination dir if needed.
mpremote connect auto fs cp -r "$WEB_DIR/vendor" :www/

echo "[update] Resetting device..."
mpremote connect auto reset

echo "[update] Done."
