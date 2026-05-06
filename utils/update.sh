#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/firmware/micropython/src"
WEB_DIR="$REPO_ROOT/web/app"
SAMPLE_CFG="$SRC_DIR/config/samples/config.json.sample"

# Network key cache. Lives in the repo root (gitignored) so subsequent leaf
# provisioning can pick it up without the user having to re-enter it. Treat
# this like an SSH private key — keep it off cloud storage.
NETKEY_FILE="$REPO_ROOT/.lokki-network-key"

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
SETUP_WIFI=0

usage() {
    cat <<EOF
Usage: $0 [--fresh --role=coordinator|leaf [--id=N] [--wifi]]

Without flags:
    Push code + web assets only. Preserves /config.json and /secrets.json
    on the device.

With --fresh:
    --role=coordinator         Push the coordinator starter config from
                                $SAMPLE_CFG. WiFi credentials are placeholders
                                and MUST be edited before the unit comes up.
                                Generates a network HMAC key (or reuses an
                                existing $NETKEY_FILE) and pushes secrets.json.
    --role=leaf --id=N         Push a minimal leaf config with unit_id=N (1-8)
                                AND the network HMAC key from $NETKEY_FILE.
                                Errors if no key file exists yet (provision a
                                coordinator first, or copy the key from the
                                machine that did).
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --fresh) FRESH=1 ;;
        --role=*) ROLE="${1#--role=}" ;;
        --role) shift; ROLE="${1:-}" ;;
        --id=*) LEAF_ID="${1#--id=}" ;;
        --id) shift; LEAF_ID="${1:-}" ;;
        --wifi) SETUP_WIFI=1 ;;
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

if [ "$FRESH" = "1" ]; then
    echo "[update] --fresh flag provided: Erasing existing files on the device..."
    mpremote connect auto exec "
import os
def r(d):
    for f in os.listdir(d):
        p = d + '/' + f
        if os.stat(p)[0] & 0x4000:
            r(p)
            os.rmdir(p)
        else:
            os.remove(p)
try:
    r('.')
except Exception as e:
    print('Clean failed:', e)
"
    sleep 0.5
fi

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
    TMP_SECRETS="$(mktemp -t lokki-secrets.XXXXXX)"
    trap 'rm -f "$TMP_CFG" "$TMP_SECRETS"' EXIT

    # ------------------------------------------------------------------
    # Network key handling
    # ------------------------------------------------------------------
    # Coordinator: if no key file exists, generate one and persist locally.
    # Leaf: require an existing key file (must have provisioned coord first).
    #       Refuse to push a leaf with no key — that would create a unit
    #       that silently runs unsigned and won't talk to the signed mesh.
    if [ "$ROLE" = "coordinator" ]; then
        if [ ! -f "$NETKEY_FILE" ]; then
            echo "[update] No network key found at $NETKEY_FILE — generating a new one."
            python3 -c "import secrets; print(secrets.token_hex(16))" > "$NETKEY_FILE"
            chmod 600 "$NETKEY_FILE"
            echo "[update] Wrote new key. Keep $NETKEY_FILE safe — it's the only copy."
        else
            echo "[update] Reusing network key from $NETKEY_FILE"
        fi
    else
        if [ ! -f "$NETKEY_FILE" ]; then
            echo "[update] ERROR: no network key at $NETKEY_FILE — provision a coordinator first" >&2
            echo "[update]        (or copy the key file from the machine that did)." >&2
            exit 3
        fi
    fi

    NETKEY="$(tr -d '[:space:]' < "$NETKEY_FILE")"
    if ! [[ "$NETKEY" =~ ^[0-9a-fA-F]{32}$ ]]; then
        echo "[update] ERROR: network key in $NETKEY_FILE is not 32 hex chars" >&2
        exit 3
    fi
    cat > "$TMP_SECRETS" <<EOF
{ "lora_key_hex": "$NETKEY" }
EOF

    if [ "$ROLE" = "coordinator" ]; then
        echo "[update] Pushing coordinator starter config from sample..."
        if [ "$SETUP_WIFI" = "1" ]; then
            read -p "Enter WiFi SSID: " WIFI_SSID
            while true; do
                read -s -p "Enter WiFi Password: " WIFI_PASS
                echo
                read -s -p "Confirm WiFi Password: " WIFI_PASS_CONFIRM
                echo
                if [ "$WIFI_PASS" = "$WIFI_PASS_CONFIRM" ]; then
                    break
                else
                    echo "[update] Passwords do not match. Please try again."
                fi
            done
            echo "[update] Applying WiFi credentials..."
            jq_cmd="."
            jq_args=()
            if [ -n "${WIFI_SSID:-}" ]; then
                jq_cmd+=" | .wifi.ssid=\$ssid"
                jq_args+=(--arg ssid "$WIFI_SSID")
            fi
            if [ -n "${WIFI_PASS:-}" ]; then
                jq_cmd+=" | .wifi.password=\$pass"
                jq_args+=(--arg pass "$WIFI_PASS")
            fi
            jq "${jq_args[@]}" "$jq_cmd" "$SAMPLE_CFG" > "$TMP_CFG"
        else
            cp "$SAMPLE_CFG" "$TMP_CFG"
        fi
        
        mpremote connect auto fs cp "$TMP_CFG" :config.json
        echo "[update] Pushing secrets.json (network HMAC key)..."
        mpremote connect auto fs cp "$TMP_SECRETS" :secrets.json
        echo
        if [ "$SETUP_WIFI" = "0" ]; then
            echo "[update] !! WiFi credentials in the pushed config are placeholders."
            echo "[update] !! Edit them via the Config Builder (USB) or with mpremote"
            echo "[update] !! before the coordinator can reach the network:"
            echo "[update] !!   mpremote connect auto edit :config.json"
            echo
        fi
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
    "led_color_order": "RGB",
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
        echo "[update] Pushing secrets.json (matching coordinator network key)..."
        mpremote connect auto fs cp "$TMP_SECRETS" :secrets.json
        echo
        echo "[update] Leaf $LEAF_ID stub config + network key pushed."
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
