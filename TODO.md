# TODO

Active development is on `dev/lokki-v1`. See [docs/firmware-modules.md](docs/firmware-modules.md) for the full phased plan.

## Phase 1 — Core hardware (single unit, no comms)

- [ ] `core/config_manager.py` — extend schema for relays, PIR, LDR, scenes, LoRa
- [ ] `hardware/pwm_control.py` — extend to 8 channels, add fade support
- [ ] `hardware/relay_control.py` — new: 2-channel GPIO relay driver
- [ ] `hardware/status_led.py` — replace: WS2812 addressable LED (was simple RGB)
- [ ] `hardware/pir_manager.py` — new: 4× PIR state machines with debounce
- [ ] `hardware/ldr_monitor.py` — new: ADC read, rolling average, cap calculation
- [ ] `core/schedule_engine.py` — new: extract from main.py, extend for relays
- [ ] `core/priority_arbiter.py` — new: single source of truth for output state
- [ ] `main.py` — update boot sequence and async task list

## Phase 2 — LoRa comms

- [ ] `comms/lora_transport.py` — new: E220 UART driver
- [ ] `comms/lora_protocol.py` — new: message encode/decode/dispatch/ACK
- [ ] `comms/wifi_connect.py` — extend: broadcast TIME_SYNC after NTP sync

## Phase 3 — Coordinator layer

- [ ] `coordinator/fleet_manager.py` — new: heartbeat tracking, unit state
- [ ] `coordinator/api_handlers.py` — new: REST endpoint handlers
- [ ] `coordinator/web_server.py` — extend: fleet REST API endpoints
- [ ] `web/app/` — update helper app for fleet management UI

## Phase 4 — Optional / polish

- [ ] `hardware/i2c_sensors.py` — new: optional BME280/BH1750/SCD40 support
- [ ] Update `shared/json/config.schema.json` for JSON Schema validation
- [ ] Update `web/app/config-builder.html` for new schema
