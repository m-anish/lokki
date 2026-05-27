#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/firmware/micropython/src"
WEB_DIR="$REPO_ROOT/web/app"
SAMPLE_CFG="$SRC_DIR/config/samples/config.json.sample"
SAMPLE_SUN="$REPO_ROOT/firmware/micropython/config/samples/sun_times.json.sample"
INBAND_TEST="$REPO_ROOT/tests/lora_e220_inband_test.py"
COLOR_TEST="$REPO_ROOT/firmware/micropython/tools/color_test.py"
I2C_HELPER="$REPO_ROOT/firmware/micropython/tools/i2c_helper.py"

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
LEAVES_COUNT=""
SETUP_WIFI=0
DEBUG=0
# 16-bit fleet-wide LoRa encryption key (CRYPT_H, CRYPT_L registers).
# Symmetric — every unit in the fleet must use the same pair. Defaults
# to the project-wide 0x07 / 0x93 so fresh boards out of the box already
# share keys with the rest of the fleet. Operators wanting a different
# shared secret pass --crypt-h/--crypt-l; set both to 0x00 to disable
# encryption entirely.
CRYPT_H="0x07"
CRYPT_L="0x93"

usage() {
    cat <<EOF
Usage: $0 [--fresh --role=coordinator|leaf [--id=N] [--leaves=N]
          [--wifi] [--debug] [--crypt-h=NN] [--crypt-l=NN]]

Without flags:
    Push code + web assets only. Preserves /config.json on the device.

With --fresh:
    --role=coordinator         Push the coordinator starter config from
                                $SAMPLE_CFG. WiFi credentials are placeholders
                                and MUST be edited before the unit comes up.
    --role=leaf --id=N         Push a minimal leaf config with unit_id=N (1-8).
                                The coordinator will overwrite it via LoRa once
                                you push a real config from the Config Builder.
    --leaves=N                 (coordinator only) Pre-cache N blank-slate leaf
                                configs to :/leaf-configs/{1..N}.json on the
                                coord, with lora/hardware/timezone copied from
                                the coord's just-pushed config. Prompts
                                interactively for each leaf's display name. When
                                each leaf later joins the fleet, the coord's
                                /api/units/N/config already returns a valid
                                cached config — Config Builder works without an
                                initial blank-slate push step.
    --debug                    Force system.log_level = "DEBUG" in the pushed
                                config (overrides the sample/stub default of INFO).
    --crypt-h=NN --crypt-l=NN  Override the fleet-wide LoRa encryption key bytes.
                                Default 0x07 / 0x93 (project-wide shared key).
                                Accept hex (0xNN) or decimal. Same value MUST be
                                used on every unit in the fleet, or modules will
                                silently fail to decode each other. Set both to
                                0x00 to disable encryption.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --fresh) FRESH=1 ;;
        --role=*) ROLE="${1#--role=}" ;;
        --role) shift; ROLE="${1:-}" ;;
        --id=*) LEAF_ID="${1#--id=}" ;;
        --id) shift; LEAF_ID="${1:-}" ;;
        --leaves=*) LEAVES_COUNT="${1#--leaves=}" ;;
        --leaves) shift; LEAVES_COUNT="${1:-}" ;;
        --wifi) SETUP_WIFI=1 ;;
        --debug) DEBUG=1 ;;
        --crypt-h=*) CRYPT_H="${1#--crypt-h=}" ;;
        --crypt-h) shift; CRYPT_H="${1:-}" ;;
        --crypt-l=*) CRYPT_L="${1#--crypt-l=}" ;;
        --crypt-l) shift; CRYPT_L="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) echo "[update] Unknown arg: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# Normalize crypt values to decimal so jq and the heredoc consume them
# uniformly. Accept either hex (0xNN) or plain decimal.
_to_dec() {
    local v="$1"
    if [[ "$v" =~ ^0[xX][0-9a-fA-F]+$ ]]; then
        printf '%d' "$v"
    elif [[ "$v" =~ ^[0-9]+$ ]]; then
        printf '%d' "$v"
    else
        echo "[update] ERROR: --crypt-h/--crypt-l must be 0..255 (got '$v')" >&2
        exit 2
    fi
}
CRYPT_H_DEC="$(_to_dec "$CRYPT_H")"
CRYPT_L_DEC="$(_to_dec "$CRYPT_L")"
if [ "$CRYPT_H_DEC" -lt 0 ] || [ "$CRYPT_H_DEC" -gt 255 ] \
   || [ "$CRYPT_L_DEC" -lt 0 ] || [ "$CRYPT_L_DEC" -gt 255 ]; then
    echo "[update] ERROR: crypt bytes must each be 0..255 (got h=$CRYPT_H_DEC l=$CRYPT_L_DEC)" >&2
    exit 2
fi

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

# Validate --leaves separately because it's only valid in combination with
# --fresh --role=coordinator. Doing it here instead of inline in the
# coordinator branch lets the operator catch a typo before any flashing
# happens.
if [ -n "$LEAVES_COUNT" ]; then
    if [ "$FRESH" != "1" ] || [ "$ROLE" != "coordinator" ]; then
        echo "[update] ERROR: --leaves=N requires --fresh --role=coordinator" >&2
        exit 2
    fi
    if ! [[ "$LEAVES_COUNT" =~ ^[1-8]$ ]]; then
        echo "[update] ERROR: --leaves must be 1..8 (got '$LEAVES_COUNT')" >&2
        exit 2
    fi
    if ! command -v jq >/dev/null 2>&1; then
        echo "[update] ERROR: jq is required for --leaves= pre-caching but not found on PATH" >&2
        exit 1
    fi
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
# Also flash the schema to /config.schema.json (filesystem root, not
# under /www/) so the firmware's config_manager._load_schema() can
# open it at runtime. Single source of truth: web/app/config.schema.json
# is the authoritative file; this copy and the /www/ copy must stay in
# sync — flashing always overwrites both with the repo version.
mpremote connect auto fs cp "$WEB_DIR/config.schema.json" :config.schema.json
# `fs cp -r` on mpremote creates the destination dir if needed.
mpremote connect auto fs cp -r "$WEB_DIR/vendor" :www/

# In-band E220 register tool — shipped alongside the firmware so a
# field unit can be recovered without bringing the bridge hardware.
# Idea: if runtime register config fails (e.g. corrupt config.json,
# leaf orphaned by a bad lora_config push), the operator can boot the
# Pico into this script by renaming it to main.py and re-pushing
# good defaults to the E220's NVRAM (PERSIST=True). After NVRAM is
# clean, restore the real main.py and reboot.
#
# Usage from a host shell with the device plugged in:
#   mpremote connect auto cp :/main.py  :/main.py.bak
#   mpremote connect auto cp :/tools/lora_inband_test.py :/main.py
#   mpremote connect auto reset
#   ... edit OP/WR_* via the running REPL or re-flash with new values ...
#   mpremote connect auto cp :/main.py.bak :/main.py
#   mpremote connect auto reset
if [ -f "$INBAND_TEST" ]; then
    mk_remote_dir "tools"
    echo "[update] Flashing in-band LoRa test tool -> :/tools/lora_inband_test.py"
    mpremote connect auto fs cp "$INBAND_TEST" ":tools/lora_inband_test.py"
fi

# Status-LED colour-cycle tool. Run on the device with:
#   mpremote exec "exec(open('/tools/color_test.py').read())"
# Useful for sanity-checking the WS2812 and previewing how named
# colour constants in src/hardware/status_led.py render.
if [ -f "$COLOR_TEST" ]; then
    mk_remote_dir "tools"
    echo "[update] Flashing status-LED color tool -> :/tools/color_test.py"
    mpremote connect auto fs cp "$COLOR_TEST" ":tools/color_test.py"
fi

# Interactive I2C / DS3231 helper. Standalone — doesn't import any
# Lokki module, so it works even on a half-bricked device. Run on
# the device with:
#   mpremote run firmware/micropython/tools/i2c_helper.py
# or:
#   mpremote exec "exec(open('/tools/i2c_helper.py').read())"
# Menu-driven: scan the bus, read/write DS3231 time, read DS3231
# temperature, run a soak loop to diagnose intermittent EIO.
if [ -f "$I2C_HELPER" ]; then
    mk_remote_dir "tools"
    echo "[update] Flashing I2C/RTC helper -> :/tools/i2c_helper.py"
    mpremote connect auto fs cp "$I2C_HELPER" ":tools/i2c_helper.py"
fi

# ---------------------------------------------------------------------------
# Optional: starter config push (--fresh)
# ---------------------------------------------------------------------------
if [ "$FRESH" = "1" ]; then
    TMP_CFG="$(mktemp -t lokki-cfg.XXXXXX)"
    trap 'rm -f "$TMP_CFG"' EXIT

    if [ "$ROLE" = "coordinator" ]; then
        echo "[update] Pushing coordinator starter config from sample..."

        # Build jq edits incrementally. If neither --wifi nor --debug is set we
        # just copy the sample verbatim.
        jq_cmd="."
        jq_args=()

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
            if [ -n "${WIFI_SSID:-}" ]; then
                jq_cmd+=" | .wifi.ssid=\$ssid"
                jq_args+=(--arg ssid "$WIFI_SSID")
            fi
            if [ -n "${WIFI_PASS:-}" ]; then
                jq_cmd+=" | .wifi.password=\$pass"
                jq_args+=(--arg pass "$WIFI_PASS")
            fi
        fi

        if [ "$DEBUG" = "1" ]; then
            echo "[update] Forcing system.log_level = DEBUG..."
            jq_cmd+=' | .system.log_level="DEBUG"'
        fi

        # Always set crypt bytes — even when they match the sample
        # defaults, this keeps the push self-describing and surfaces
        # any --crypt-h/--crypt-l override without a second code path.
        echo "[update] Setting LoRa crypt key: h=$CRYPT_H_DEC l=$CRYPT_L_DEC"
        jq_cmd+=" | .lora.crypt_h=\$ch | .lora.crypt_l=\$cl"
        jq_args+=(--argjson ch "$CRYPT_H_DEC" --argjson cl "$CRYPT_L_DEC")

        if [ "$jq_cmd" = "." ]; then
            cp "$SAMPLE_CFG" "$TMP_CFG"
        else
            if ! command -v jq >/dev/null 2>&1; then
                echo "[update] ERROR: jq is required for --wifi or --debug edits but not found on PATH" >&2
                exit 1
            fi
            jq "${jq_args[@]}" "$jq_cmd" "$SAMPLE_CFG" > "$TMP_CFG"
        fi

        mpremote connect auto fs cp "$TMP_CFG" :config.json
        echo
        if [ "$SETUP_WIFI" = "0" ]; then
            echo "[update] !! WiFi credentials in the pushed config are placeholders."
            echo "[update] !! Edit them via the Config Builder (USB) or with mpremote"
            echo "[update] !! before the coordinator can reach the network:"
            echo "[update] !!   mpremote connect auto edit :config.json"
            echo
        fi

        # --- Optional: pre-cache blank-slate leaf configs (--leaves=N) ---
        # Mirrors what api_handlers._build_blank_slate_config() does at
        # claim-wizard time: copies lora/timezone/hardware from the coord
        # we just pushed, fills in role/unit_id/unit_name + standard
        # blank-slate everything else. The result lands at
        # /leaf-configs/N.json on the coord — exactly where
        # api_handlers._persist_leaf_cfg() writes after a real config
        # push, so /api/units/N/config will serve it as `source: cached`
        # the moment a leaf comes online at that unit_id.
        if [ -n "$LEAVES_COUNT" ]; then
            echo "[update] Pre-caching $LEAVES_COUNT blank-slate leaf config(s) on coord..."
            echo "[update] When each leaf later joins the fleet, the coord will already"
            echo "[update] have a usable config for it — Config Builder → Load from"
            echo "[update] device → 'Leaf N' will work without a first-time blank push."
            echo

            mk_remote_dir "leaf-configs"

            for i in $(seq 1 "$LEAVES_COUNT"); do
                DEFAULT_NAME="Leaf-$i"
                # `-r` keeps shell history off the prompt; `</dev/tty` so
                # the read works even if the script is piped or stdin is
                # otherwise redirected (some CI / wrapper scripts do this).
                read -r -p "  Leaf $i name [$DEFAULT_NAME]: " LEAF_NAME </dev/tty || LEAF_NAME=""
                LEAF_NAME="${LEAF_NAME:-$DEFAULT_NAME}"

                LEAF_TMP="$(mktemp -t "lokki-leaf-${i}.XXXXXX")"
                # jq -n with an explicit object template — single source of
                # truth for the blank-slate shape. Keep this in sync with
                # api_handlers._build_blank_slate_config(); the validator
                # in config_manager.py is what catches drift if either
                # one falls behind.
                jq --arg uid "$i" --arg uname "$LEAF_NAME" \
                   '{
                      version: (.version // "1.0"),
                      system: {
                        role: "leaf",
                        unit_id: ($uid | tonumber),
                        unit_name: $uname,
                        log_level: "INFO",
                        log_buffer_size: 100,
                        heartbeat_interval_s: (.system.heartbeat_interval_s // 30),
                        heartbeat_timeout_s:  (.system.heartbeat_timeout_s  // 120),
                        pwm_update_interval_ms: 500
                      },
                      wifi:     { ssid: "N/A", password: "" },
                      lora:     .lora,
                      timezone: .timezone,
                      hardware: .hardware,
                      ldr:      { enabled: false, smoothing_window_s: 60, cap_rules: [] },
                      pir:      [],
                      relays:   [],
                      led_channels: [
                        { id: 1, name: "Channel 1", gpio_pin: 16, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 2, name: "Channel 2", gpio_pin: 17, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 3, name: "Channel 3", gpio_pin: 18, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 4, name: "Channel 4", gpio_pin: 19, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 5, name: "Channel 5", gpio_pin: 22, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 6, name: "Channel 6", gpio_pin: 15, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 7, name: "Channel 7", gpio_pin: 14, enabled: false, default_duty_percent: 0, time_windows: [] },
                        { id: 8, name: "Channel 8", gpio_pin: 13, enabled: false, default_duty_percent: 0, time_windows: [] }
                      ],
                      scenes: [],
                      notifications: { mqtt_enabled: false }
                    }' "$TMP_CFG" > "$LEAF_TMP"

                echo "[update]   → :leaf-configs/$i.json   (unit_id=$i, name=\"$LEAF_NAME\")"
                mpremote connect auto fs cp "$LEAF_TMP" ":leaf-configs/$i.json"
                rm -f "$LEAF_TMP"
            done

            echo
            echo "[update] All $LEAVES_COUNT leaf config(s) cached."
            echo "[update] Next steps:"
            echo "[update]   1. Flash each leaf one at a time:"
            echo "[update]        $0 --fresh --role=leaf --id=1   # then 2, 3, ..."
            echo "[update]   2. Power on the leaf; the coord shows it within ~30 s."
            echo "[update]   3. Open the dashboard → Config Builder → Load from"
            echo "[update]      device → 'Leaf N' to see the cached starter config."
            echo
        fi

    else
        echo "[update] Pushing minimal leaf stub config (unit_id=$LEAF_ID)..."
        LEAF_LOG_LEVEL="INFO"
        if [ "$DEBUG" = "1" ]; then
            LEAF_LOG_LEVEL="DEBUG"
            echo "[update] Forcing system.log_level = DEBUG..."
        fi
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
    "log_level": "$LEAF_LOG_LEVEL",
    "log_buffer_size": 100,
    "heartbeat_interval_s": 30,
    "heartbeat_timeout_s": 120,
    "pwm_update_interval_ms": 500
  },
  "lora": {
    "enabled": true,
    "frequency_mhz": 868,
    "air_data_rate": 4800,
    "tx_power_dbm": 22,
    "channel": 73,
    "subpacket_size": 200,
    "lbt_enable": false,
    "ambient_rssi_enable": false,
    "crypt_h": $CRYPT_H_DEC,
    "crypt_l": $CRYPT_L_DEC
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
    { "id": 1, "name": "Channel 1", "gpio_pin": 16, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 2, "name": "Channel 2", "gpio_pin": 17, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 3, "name": "Channel 3", "gpio_pin": 18, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 4, "name": "Channel 4", "gpio_pin": 19, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 5, "name": "Channel 5", "gpio_pin": 22, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 6, "name": "Channel 6", "gpio_pin": 15, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 7, "name": "Channel 7", "gpio_pin": 14, "enabled": false, "default_duty_percent": 0, "time_windows": [] },
    { "id": 8, "name": "Channel 8", "gpio_pin": 13, "enabled": false, "default_duty_percent": 0, "time_windows": [] }
  ],
  "scenes": [],
  "notifications": { "mqtt_enabled": false }
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

# ---------------------------------------------------------------------------
# Push sample sun_times.json if not already on the device (both roles).
# The sample contains Dharamsala monthly data — replace it with your own
# location's data generated from the Config Builder's Sun Times section.
# ---------------------------------------------------------------------------
if [ "$FRESH" = "1" ] && [ -f "$SAMPLE_SUN" ]; then
    if ! mpremote connect auto fs ls : 2>/dev/null | grep -qE '(^|[[:space:]])sun_times\.json([[:space:]]|$)'; then
        echo "[update] Pushing sample sun_times.json (Dharamsala, monthly)..."
        mpremote connect auto fs cp "$SAMPLE_SUN" :sun_times.json
        echo "[update] !! sun_times.json contains Dharamsala sample data."
        echo "[update] !! Generate your own via Config Builder → Sun Times section"
        echo "[update] !! and copy it to the device if you use sunrise/sunset schedules."
        echo
    else
        echo "[update] sun_times.json already present on device — not overwriting."
    fi
fi

echo "[update] Resetting device..."
mpremote connect auto reset

echo "[update] Done."
