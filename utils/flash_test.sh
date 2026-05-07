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

UNIT_ID=""
SCRIPT_NAME="baseline"
NO_WRITES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --id=*) UNIT_ID="${1#--id=}" ;;
        --id)   shift; UNIT_ID="${1:-}" ;;
        --script=*) SCRIPT_NAME="${1#--script=}" ;;
        --script)   shift; SCRIPT_NAME="${1:-}" ;;
        --no-writes) NO_WRITES=1 ;;
        --help|-h)
            cat <<EOF
Usage: $0 --id=N [--script=baseline|step1] [--no-writes]

  --id=N            UNIT_ID to patch into the script (0=coord, 1..8=leaf).
  --script=NAME     Which test script to flash (default: baseline). Choices:
                      baseline  — tests/lora_e220_test.py
                                  (zero config, factory defaults, transparent)
                      step1     — tests/lora_e220_step1_config.py
                                  (baseline + minimal register-mode write)
                      xreef     — tests/lora_e220_xreef.py
                                  (uses xreef's E220 library directly;
                                   pushes tests/xreef/ alongside as :/xreef/)
  --no-writes       (step1 only) Patches DO_REGISTER_WRITES=False — mode-
                    bounce through CONFIG and back without any UART config
                    commands. Used to isolate whether mode bouncing itself
                    or the writes break RX.
EOF
            exit 0
            ;;
        *) echo "[flash_test] unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$SCRIPT_NAME" in
    baseline) TEST_SRC="$REPO_ROOT/tests/lora_e220_test.py"; PUSH_XREEF=0 ;;
    step1)    TEST_SRC="$REPO_ROOT/tests/lora_e220_step1_config.py"; PUSH_XREEF=0 ;;
    xreef)    TEST_SRC="$REPO_ROOT/tests/lora_e220_xreef.py"; PUSH_XREEF=1 ;;
    *) echo "[flash_test] --script must be 'baseline', 'step1', or 'xreef' (got '$SCRIPT_NAME')" >&2; exit 2 ;;
esac

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

# Optional: also patch DO_REGISTER_WRITES=False (only meaningful in step1).
if [ "$NO_WRITES" = "1" ]; then
    sed -i.bak "s/^DO_REGISTER_WRITES = .*/DO_REGISTER_WRITES = False  # patched by flash_test.sh --no-writes/" "$TMP"
    rm -f "$TMP.bak"
    if ! grep -qE "^DO_REGISTER_WRITES = False" "$TMP"; then
        echo "[flash_test] --no-writes requested but DO_REGISTER_WRITES line not found in $TEST_SRC" >&2
        exit 1
    fi
    echo "[flash_test] patched DO_REGISTER_WRITES=False"
fi

# If the test depends on the xreef library, push the package directory first.
if [ "$PUSH_XREEF" = "1" ]; then
    echo "[flash_test] Pushing xreef library to /xreef/ ..."
    mpremote connect auto fs mkdir :xreef 2>/dev/null || true
    for f in lora_e220.py lora_e220_constants.py lora_e220_operation_constant.py; do
        mpremote connect auto fs cp "$REPO_ROOT/tests/xreef/$f" ":xreef/$f"
    done
    # An empty __init__.py to make /xreef/ a package
    EMPTY="$(mktemp -t lokki-empty.XXXXXX)"
    : > "$EMPTY"
    mpremote connect auto fs cp "$EMPTY" :xreef/__init__.py
    rm -f "$EMPTY"
fi

echo "[flash_test] Pushing test script as main.py (UNIT_ID=$UNIT_ID)..."
mpremote connect auto fs cp "$TMP" :main.py

echo "[flash_test] Resetting device..."
mpremote connect auto reset

echo "[flash_test] Done. Open Thonny / serial console at 115200 baud to watch the boot."
echo
echo "This is the BASELINE test — no module configuration, factory defaults,"
echo "transparent mode. Both units will hear each other only if they both"
echo "have factory-default address/channel/NETID."
echo
echo "Expected behaviour:"
echo "  - solid blue at startup, then dim yellow (idle)"
echo "  - green flash every ~2.5 s when transmitting a Hello message"
echo "  - red flash whenever a message arrives from the peer"
echo "  - serial log shows '[TX]' and '[RX]' lines"
echo
echo "If TX flashes happen but no RX ever — peer not heard. Possible causes:"
echo "  - one or both modules previously had non-default config written"
echo "    (most likely; we wrote 0xC2 volatile from earlier tests but if a"
echo "     previous run wrote 0xC0 NVRAM the change persisted across power cycles)"
echo "  - antenna missing or wrong frequency"
echo "  - distance / line-of-sight / interference"
