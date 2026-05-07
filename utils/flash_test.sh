#!/usr/bin/env bash
# ----------------------------------------------------------------------
# Flash a Pico with JUST the standalone LoRa test script.
#
# Wipes the device's filesystem (everything under :/) then pushes
# tests/lora_e220_test.py as :main.py with the requested UNIT_ID
# patched in. After this, the unit boots straight into the test loop —
# no Lokki firmware involved.
#
# Usage:
#   utils/flash_test.sh --id=0    # coordinator-side
#   utils/flash_test.sh --id=1    # leaf-side
# ----------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_SRC="$REPO_ROOT/tests/lora_e220_test.py"

UNIT_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --id=*) UNIT_ID="${1#--id=}" ;;
        --id)   shift; UNIT_ID="${1:-}" ;;
        --help|-h)
            cat <<EOF
Usage: $0 --id=N

Wipes the connected Pico's filesystem and installs ONLY the LoRa test
loop as main.py. UNIT_ID (0 = coordinator, 1..8 = leaf) is patched into
the script's first lines before flashing.

Run on each Pico in turn (cable + USB). After both are flashed, both
should auto-boot into the test loop and start broadcasting PINGs.
EOF
            exit 0
            ;;
        *) echo "[flash_test] unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if ! [[ "$UNIT_ID" =~ ^[0-8]$ ]]; then
    echo "[flash_test] --id is required, must be 0..8 (got '${UNIT_ID:-}')" >&2
    exit 2
fi

if [ ! -f "$TEST_SRC" ]; then
    echo "[flash_test] test source not found at $TEST_SRC" >&2
    exit 1
fi

if ! command -v mpremote >/dev/null 2>&1; then
    echo "[flash_test] mpremote not found on PATH" >&2
    exit 1
fi

echo "[flash_test] Closing any running mpremote sessions..."
pkill -f mpremote 2>/dev/null || true
sleep 0.5

# ----------------------------------------------------------------------
# Wipe the device's filesystem.
# We use one mpremote exec to recursively delete everything under "/".
# ----------------------------------------------------------------------
echo "[flash_test] Wiping device filesystem..."
mpremote connect auto exec "$(cat <<'PYEOF'
import os
def _rm(p):
    try:
        for n in os.listdir(p):
            full = (p.rstrip('/') + '/' + n) if p else '/' + n
            try:
                _rm(full)
                os.rmdir(full)
            except OSError:
                try:
                    os.remove(full)
                except OSError:
                    pass
    except OSError:
        pass
_rm('/')
print('[wipe] done. Remaining at /:', os.listdir('/'))
PYEOF
)"

# ----------------------------------------------------------------------
# Patch UNIT_ID and push as main.py.
# ----------------------------------------------------------------------
TMP="$(mktemp -t lokki-test.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
sed "s/^UNIT_ID  = .*/UNIT_ID  = $UNIT_ID         # patched by flash_test.sh/" \
    "$TEST_SRC" > "$TMP"

# Sanity: verify the substitution actually happened
if ! grep -qE "^UNIT_ID  = $UNIT_ID\b" "$TMP"; then
    echo "[flash_test] sed substitution failed — UNIT_ID line not patched" >&2
    head -20 "$TMP" >&2
    exit 1
fi

echo "[flash_test] Pushing test script as main.py (UNIT_ID=$UNIT_ID)..."
mpremote connect auto fs cp "$TMP" :main.py

echo "[flash_test] Resetting device..."
mpremote connect auto reset

echo "[flash_test] Done. Open Thonny / serial console at 115200 baud to watch the boot."
echo
echo "Expected boot pattern (visually):"
echo "  - 3 fast white pulses          : Pico booted, MicroPython up"
echo "  - solid cyan (~1-3 s)          : configuring E220"
echo "  - 2 green pulses               : E220 config OK"
echo "  - then runtime LED reflects link quality:"
echo "      dim cyan   = configured but no PING received yet"
echo "      green      = healthy (RSSI better than -70 dBm)"
echo "      yellow     = weak (-70..-90 dBm)"
echo "      red        = barely receiving (worse than -90 dBm)"
echo "      amber      = peer went quiet for 30+ s"
echo "  - blue flash                   : transmitted a PING"
echo "  - bright green flash           : received a packet from peer"
echo
echo "If the LED stays solid red blinking, E220 config failed — boot log"
echo "has the failure attempts. Power-cycle (full power off) to retry."
