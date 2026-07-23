# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Lokki is a campus-scale LED lighting automation system for wellness/hospitality venues. It runs MicroPython on Raspberry Pi Pico 2 / Pico 2 W (RP2350) boards on a custom PCB, with up to 8 leaf units coordinated by a single coordinator via LoRa (E220-900T22D, ~868 MHz). No cloud dependency.

Same PCB, same firmware on every unit. Role (`coordinator` vs `leaf`) is determined by `config.json` at boot.

## Deploy / Flash Workflow

Firmware ships to a device via `mpremote`. Everything is wrapped in `utils/update.sh`:

```bash
# Push code + web assets only (preserves existing /config.json)
./utils/update.sh

# Wipe device, push code + starter coordinator config (placeholder WiFi creds)
./utils/update.sh --fresh --role=coordinator [--wifi] [--debug]

# Wipe device, push minimal leaf stub bound to unit_id N (1..8)
./utils/update.sh --fresh --role=leaf --id=N

# Coordinator + pre-cache blank-slate leaf configs on coord flash for N leaves
./utils/update.sh --fresh --role=coordinator --leaves=N

# Override fleet-wide LoRa encryption key (defaults to 0x07/0x93)
./utils/update.sh --fresh --role=... --crypt-h=NN --crypt-l=NN
```

Requires `mpremote` (and `jq` when `--leaves=` / `--wifi` / `--debug` are used). The script also flashes `/www/` (dashboard, config-builder) and `/config.schema.json` (firmware reads the schema at runtime from filesystem root — keep `web/app/config.schema.json` as the single source of truth; `update.sh` copies it to both `:/www/config.schema.json` and `:/config.schema.json`).

Manual REPL access:
```bash
mpremote connect auto repl       # or: screen /dev/ttyACM0 115200
mpremote connect auto reset
mpremote connect auto fs ls :    # list root, portable across mpremote versions
```

## No Host-Side Test Suite

There are no unit tests run on the host. Files in `tests/` are **standalone MicroPython scripts** flashed to a Pico 2 to bench-test the LoRa link (e.g. `lora_e220_test.py`, `pico_e220_bridge.py`). They are not pytest fixtures.

Verification happens by flashing to real hardware and watching the REPL / dashboard / status LED. Type-checking and host tests will not catch firmware bugs.

## Architecture (the parts that need cross-file reading)

### Boot, gating, and degraded-mode philosophy

Read `firmware/micropython/src/main.py` (long but well-commented) before changing anything in init or task wiring. Key invariants:

- **Config failure → safe mode.** `core/config_manager.py` captures `SafeModeError` in `safe_mode_reason` rather than raising at import time; `main.py` checks it and turns all outputs off.
- **Time-sync gate.** `schedule_task` only ticks when `system_status.time_synced` is true. Sources, in priority: DS3231-at-boot → NTP (coord) → LoRa TS broadcast (leaf) → operator override from dashboard. Until then, the priority arbiter falls back to per-channel `default_state` — that's the safe behaviour, do not "fix" it by ungating.
- **LoRa boot is best-effort.** The E220 on an LM2596-powered board is unreliable for ~30–60 s post-power. `main.py` does ONE attempt at boot then defers a silent retry every 100 s (×3). All LoRa-dependent async tasks (`listen_task`, `heartbeat_broadcast_task`, `time_sync_task`, `event_forward_task`) start unconditionally and no-op while `lora_transport.config_ok` is false. When the deferred retry succeeds, they pick up real traffic without re-registration. Do not add soft_reset loops or block boot on LoRa.
- **Priority arbiter** (`core/priority_arbiter.py`) is the only place that decides output state. Layers, highest first: manual override → PIR → schedule → default. LDR cap applies **only to the schedule layer** — explicit manual/PIR requests bypass the cap. Don't sprinkle output writes through other modules.

### LoRa protocol — wire-format and size discipline

`comms/lora_protocol.py` and `comms/lora_transport.py` implement a small message protocol over the E220 in FIXED-mode (`[ADDH][ADDL][CHAN][payload]`, hardware-routed). Packets are capped at 200 B subpacket size. Two consequences when adding/changing payloads:

1. **Short keys on the wire.** Heartbeat (`HB`) and status-response (`SRP`) payloads use 1–3 char keys (`n`, `up`, `ch`, `rl`, `pir`, `ldr`, `r`, `tc`, `uid`, `err`, `sc`). The internal/API keys read by the dashboard are different — `coordinator/fleet_manager.py` (`_fill`) maps wire → internal. If you add a HB field, add a wire key, an internal key, and the mapping. See the long comment in `heartbeat_broadcast_task` in `main.py`.
2. **Fitters.** When a payload might overflow 200 B, register a `_fit_*(payload, budget)` via `lora_protocol.fitter("TYPE", fn)` that sheds optional fields / truncates strings until it fits. See `_fit_hb`, `_fit_srp`, `_fit_err` in `_register_lora_handlers` in `main.py`. Don't drop the whole packet at the protocol layer.

Config push uses chunked transfer (`CFG_START` / `CFG_CHUNK` / `CFG_END`) with CRC32 and per-leaf chip-UID targeting (so multiple unclaimed `unit_id=99` leaves don't all swallow the same config). Acks include `missing` chunk indices for smart retry — checksum mismatch and missing chunks are recoverable, not errors. The leaf applies a received config FIRST, then ACKs `ok:True` only after a durable flash write, so the coord's cache cannot diverge from what the leaf actually runs.

### Coordinator role

`coordinator/fleet_manager.py` aggregates per-leaf state from incoming HBs/SRPs. `coordinator/web_server.py` is a hand-rolled non-blocking HTTP server (port 80). `coordinator/api_handlers.py` implements the REST endpoints and persists pushed leaf configs to `/leaf-configs/N.json` (so `/api/units/N/config` can return cached configs as `source: cached` even before the leaf is online — this is how the claim wizard works).

The schema-driven config validator (`core/schema_validator.py` + `core/semantic_checks.py`) is **coord-only logic in intent**, but the schema file lives in `web/app/config.schema.json` and is mirrored to the device root by `update.sh` so the leaf can also self-validate on `replace()`.

### Event bus

`shared/event_bus.py` is an in-memory ring of recent log events stamped with `src=unit_id` and a sequence number. `Logger` (`shared/simple_logger.py`) pushes into it. On leaves, `event_forward_task` drains WARN+ entries and forwards them to the coord as `ERR` packets (rate-limited, dedup window). On the coord, the `ERR` handler in `_register_lora_handlers` rewrites them into the local event bus tagged with `tag="leaf"` so the dashboard's Logs view shows fleet-wide activity in one place. Per-leaf `src_seq` dedup avoids re-pushing retried frames.

### Status LED (WS2812)

`hardware/status_led.py` is a state machine — `set_state("running_ok" | "lora_init" | "lora_recovering" | "lora_disabled" | "leaf_offline" | "manual_override" | "time_waiting" | "booting" | "error")` selects a base pattern; `flash_event(r, g, b, ms=...)` overlays a brief flash without disturbing the base state. Heartbeat sends/receives trigger a per-event flash (blue if LoRa config came up clean at boot, red if not). The base state during normal operation is computed by `_ok_led_state()` in `main.py`.

## Key Files / Directories

```
firmware/micropython/src/    # device firmware
  main.py                    # boot, async task orchestration, LoRa handler registration
  core/                      # config_manager, schedule_engine, priority_arbiter, schema_validator
  hardware/                  # pwm, relay, pir, ldr, rtc (DS3231), status_led, i2c_sensors
  comms/                     # lora_transport, lora_protocol, wifi_connect, mqtt_notifier, mdns_responder
  coordinator/               # web_server, fleet_manager, api_handlers
  shared/                    # event_bus, simple_logger, system_status, sun_times
  config/samples/            # config.json.sample shipped by update.sh --fresh

web/app/                     # index.html (dashboard), config-builder.html, config.schema.json (SoT for schema)
site/                        # marketing site for lokki.starstucklab.com (Cloudflare Pages)
docs/                        # architecture.md, lora-protocol.md, config-schema.md, firmware-modules.md, api-reference.md
hardware/                    # KiCad project (Rev0 PCB)
utils/                       # update.sh + E220 provisioning helpers
tests/                       # standalone on-device LoRa link tests (NOT host pytest)
```

Important docs to consult before structural changes:
- `docs/architecture.md` — GPIO map, topology, failure modes, priority stack
- `docs/lora-protocol.md` — register layout, fixed-point format, message types
- `docs/firmware-modules.md` — per-module responsibilities and interfaces
- `docs/config-schema.md` — `config.json` structure
- `ROADMAP.md` — what's deliberately scoped out, what's planned

## Conventions Worth Knowing

- **RP2350 GPIO reservations** (enforced by `config_manager._RESERVED_PINS = {0,1,2,3,4,5,20,21}`): GP0/1 = LoRa UART, GP2/3/4 = LoRa M0/M1/AUX, GP5 = WS2812 status LED, GP20/21 = I2C0. These cannot be used as LED-channel or relay outputs.
- **`unit_id=99`** is the "unclaimed leaf" sentinel — a factory-reset leaf announces itself at 99 with its chip UID. The claim wizard on the dashboard binds it to a real unit_id (1..8). Many `_register_lora_handlers` code paths special-case this.
- **Schema is authoritative.** When adding a config field, edit `web/app/config.schema.json` first. The firmware validator reads it at runtime from `/config.schema.json` (flashed by `update.sh`); the dashboard reads it from `/www/config.schema.json`. Both copies must come from the same source file — `update.sh` already enforces this.
- **Active development branch:** `dev/lokki-v1` (per README), though the current working branch may be `main`.

## License

GPL-3.0 — see `LICENSE`.
