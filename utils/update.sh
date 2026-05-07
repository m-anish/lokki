#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/firmware/micropython/src"
WEB_DIR="$REPO_ROOT/web/app"
SAMPLE_CFG="$SRC_DIR/config/samples/config.json.sample"

# ---------------------------------------------------------------------------
# Argument parsing
#   no flags             → push code + web assets, preserve existing config.json
#   --fresh --role=coordinator
#                        → also push a starter coordinator config (placeholder
#                          wifi creds — user must edit before reboot)
#   --fresh --role=leaf --id=N
#                        → also push a minimal leaf stub config bound to unit N
# ---------------------------------------------------------------------------
FRESH=0
ROLE=""
LEAF_ID=""

usage() {
    cat <<EOF
Usage: $0 [--fresh --role=coordinator|leaf [--id=N]]

Without flags:
    Push code + web assets only. Preserves /config.json on the device.

With --fresh:
    --role=coordinator         Push the coordinator starter config from
                                $SAMPLE_CFG. WiFi credentials are placeholders
                                and MUST be edited before the unit comes up.
    --role=leaf --id=N         Push a minimal leaf config with unit_id=N (1-8).
                                The coordinator will overwrite it via LoRa once
                                you push a real config from the Config Builder.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --fresh) FRESH=1 ;;
        --role=*) ROLE="${1#--role=}" ;;
        --role) shift; ROLE="${1:-}" ;;
        --id=*) LEAF_ID="${1#--id=}" ;;
        --id) shift; LEAF_ID="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) echo "[update] Unknown arg: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

if [ "$FRESH" = "1" ]; then
    case "$ROLE" in
        coordinator)
            : # OK
            ;;
        leaf)
            if ! [[ "$LEAF_ID" =~ ^[1-8]$ ]]; then
                echo "[update] --role=leaf requires --id=1..8 (got '$LEAF_ID')" >&2
                exit 2
            fi
            ;;
        *)
            echo "[update] --fresh requires --role=coordinator or --role=leaf" >&2
            usage; exit 2
            ;;
    esac
fi

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

# ---------------------------------------------------------------------------
# Optional: starter config push (--fresh)
# ---------------------------------------------------------------------------
if [ "$FRESH" = "1" ]; then
    TMP_CFG="$(mktemp -t lokki-cfg.XXXXXX)"
    trap 'rm -f "$TMP_CFG"' EXIT

    if [ "$ROLE" = "coordinator" ]; then
        echo "[update] Pushing coordinator starter config from sample..."
        cp "$SAMPLE_CFG" "$TMP_CFG"
        mpremote connect auto fs cp "$TMP_CFG" :config.json
        echo
        echo "[update] !! WiFi credentials in the pushed config are placeholders."
        echo "[update] !! Edit them via the Config Builder (USB) or with mpremote"
        echo "[update] !! before the coordinator can reach the network:"
        echo "[update] !!   mpremote connect auto edit :config.json"
        echo
    else
        echo "[update] Pushing minimal leaf stub config (unit_id=$LEAF_ID)..."
        # Minimal leaf bootstrap — just enough to pass schema validation and
        # bring LoRa up. Once the leaf is online and visible on the coordinator,
        # the user fills in the real config in the Config Builder and pushes
        # over LoRa. Keep this in sync with the validator in config_manager.py.
        cat > "$TMP_CFG" <<EOF
{
  "version": "1.0",
  "system": {
    "role": "leaf",
    "unit_id": $LEAF_ID,
    "unit_name": "Leaf-$LEAF_ID",
    "log_level": "INFO",
    "heartbeat_interval_s": 30,
    "heartbeat_timeout_s": 120,
    "pwm_update_interval_ms": 500
  },
  "lora": {
    "enabled": true,
    "frequency_mhz": 868,
    "tx_power_dbm": 22,
    "channel": 0
  },
  "timezone": { "name": "UTC", "utc_offset_hours": 0 },
  "hardware": {
    "i2c_sda_pin": 20,
    "i2c_scl_pin": 21,
    "i2c_freq_hz": 400000,
    "pwm_freq_hz": 1000,
    "ldr_adc_pin": 26,
    "status_led_pin": 5,
    "reset_btn_pin": 12,
    "lora_uart_id": 0,
    "lora_tx_pin": 0,
    "lora_rx_pin": 1,
    "lora_m0_pin": 2,
    "lora_m1_pin": 3,
    "lora_aux_pin": 4
  },
  "ldr": { "enabled": false, "smoothing_window_s": 60, "cap_rules": [] },
  "pir": [],
  "relays": [],
  "led_channels": [
    { "id": "ch1", "name": "ch1", "gpio_pin": 16, "enabled": false, "default_duty_percent": 0, "time_windows": [] }
  ],
  "scenes": []
}
EOF
        mpremote connect auto fs cp "$TMP_CFG" :config.json
        echo
        echo "[update] Leaf $LEAF_ID stub config pushed."
        echo "[update] LoRa frequency/channel must match the coordinator before leaf will join."
        echo "[update] On the coordinator, open Config Builder → 'Load from device' → 'Leaf $LEAF_ID'"
        echo "[update] to fill in the full config."
        echo
    fi
else
    # No --fresh: check whether config.json is present on the device. We list
    # the root and grep, because `mpremote fs ls :config.json` behaves
    # inconsistently across mpremote versions — some treat a file argument as
    # "stat this", others as "list this directory" and error on regular files.
    # `fs ls :` always lists the root directory and is portable.
    if ! mpremote connect auto fs ls : 2>/dev/null | grep -qE '(^|[[:space:]])config\.json([[:space:]]|$)'; then
        echo
        echo "[update] WARNING: no config.json on the device — unit will boot into SAFE MODE."
        echo "[update] Re-run with --fresh to push a starter config. e.g.:"
        echo "[update]   $0 --fresh --role=coordinator"
        echo "[update]   $0 --fresh --role=leaf --id=1"
        echo
    fi
fi

echo "[update] Resetting device..."
mpremote connect auto reset

echo "[update] Done."
