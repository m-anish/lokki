# Lokki Firmware Modules

**Version:** 1.0-draft  
**Date:** 2026-04-18  
**Status:** Design — pending review

Defines every firmware module: its responsibility, public interface, dependencies, and whether it is new, extended from existing code, or kept as-is.

---

## Module Map

```
firmware/micropython/src/
├── main.py                      [EXTEND]
├── core/
│   ├── config_manager.py        [EXTEND]
│   ├── schedule_engine.py       [NEW — replaces inline logic in main.py]
│   └── priority_arbiter.py      [NEW]
├── hardware/
│   ├── pwm_control.py           [EXTEND]
│   ├── relay_control.py         [NEW]
│   ├── pir_manager.py           [NEW]
│   ├── ldr_monitor.py           [NEW]
│   ├── rtc_module.py            [KEEP]
│   ├── rtc_shared.py            [KEEP]
│   ├── status_led.py            [REPLACE — WS2812 replaces simple RGB]
│   └── i2c_sensors.py           [NEW]
├── comms/
│   ├── lora_transport.py        [NEW]
│   ├── lora_protocol.py         [NEW]
│   ├── wifi_connect.py          [EXTEND]
│   └── mqtt_notifier.py         [KEEP — optional, unchanged]
├── coordinator/
│   ├── web_server.py            [EXTEND]
│   ├── fleet_manager.py         [NEW]
│   └── api_handlers.py          [NEW]
└── shared/
    ├── simple_logger.py         [KEEP]
    ├── system_status.py         [EXTEND]
    └── sun_times.py             [KEEP]
```

---

## `main.py` [EXTEND]

**Responsibility:** Boot sequence, async task orchestration. Knows the role (coordinator vs leaf) and launches the appropriate task set.

**Boot sequence:**
```
1. Load config (config_manager)
2. Init hardware (pwm, relay, pir, ldr, rtc, status_led, i2c_sensors)
3. Init LoRa (lora_transport — configure E220, switch to normal mode)
4. If coordinator:
     connect WiFi → NTP sync → update DS3231 → broadcast TIME_SYNC
     start: web_server, fleet_manager, heartbeat_collector_task
5. If leaf:
     listen for TIME_SYNC (with 60s timeout, continue on DS3231 if not received)
     start: heartbeat_broadcast_task, lora_listener_task
6. All units:
     start: schedule_engine_task, pir_monitor_task, ldr_monitor_task,
            ram_telemetry_task (if log_level DEBUG)
```

**Async tasks launched:**

| Task | Runs on | Interval |
|------|---------|----------|
| `schedule_engine_task` | All | `pwm_update_interval_ms` |
| `pir_monitor_task` | All | 100ms poll |
| `ldr_monitor_task` | All | 1s poll |
| `lora_listener_task` | All | event-driven (UART rx) |
| `heartbeat_broadcast_task` | Leaf | `heartbeat_interval_s` |
| `heartbeat_collector_task` | Coordinator | checks every 10s |
| `web_server_task` | Coordinator | event-driven |
| `ram_telemetry_task` | All (debug) | 60s |

---

## `core/config_manager.py` [EXTEND]

**Responsibility:** Load, validate, and hot-reload `config.json`. Provide typed access to all config sections.

**Changes from existing:**
- Schema validation updated for new config structure (relays, pir, ldr, scenes, lora)
- Version enforcement: major mismatch → safe mode
- New accessor methods for new config sections
- `save(section, data)` — write updated config back to flash (used by CFG push handler)

**Public interface:**
```python
config_manager.load()                    # load and validate config.json
config_manager.get(section)              # returns dict for named section
config_manager.update(section, data)     # validate + save + apply
config_manager.version                   # "major.minor" string
config_manager.role                      # "coordinator" | "leaf"
config_manager.unit_id                   # int 0–8
```

**Dependencies:** `shared/simple_logger`

---

## `core/schedule_engine.py` [NEW]

**Responsibility:** Evaluates time windows for all LED channels and relays, resolves sunrise/sunset, returns the desired output state for each channel at the current time. Does not drive hardware directly — outputs a desired-state dict that `priority_arbiter` consumes.

**Public interface:**
```python
schedule_engine.get_desired_state()
# returns:
# {
#   "ch1": {"duty_percent": 80, "fade_ms": 5000},
#   "ch2": {"duty_percent": 0},
#   ...
#   "rly1": {"state": "on"},
#   "rly2": {"state": "off"}
# }
```

**Logic:**
1. Get current time from RTC
2. For each LED channel: walk `time_windows` in order, find first matching window, return duty + fade
3. For each relay: same, return on/off state
4. If no window matches: return `default_duty_percent` / `default_state`
5. Resolve `"sunrise"` / `"sunset"` via `shared/sun_times`

**Dependencies:** `hardware/rtc_module`, `shared/sun_times`, `core/config_manager`

---

## `core/priority_arbiter.py` [NEW]

**Responsibility:** Single source of truth for what each output is *actually* doing. Holds current state per output, applies the priority stack, and drives hardware when state changes.

**Priority stack (highest to lowest):**
```
1. Manual override  (set via API / web UI)
2. PIR active state (set by pir_manager)
3. Schedule         (set by schedule_engine)
4. LDR cap          (modifier — applied on top of 1–3)
5. Default          (fallback if no other signal)
```

**State tracking per output:**
```python
{
  "ch1": {
    "manual":   {"duty_percent": 75, "fade_ms": 0, "revert_s": 0} | None,
    "pir":      {"duty_percent": 100, "fade_ms": 1000} | None,
    "schedule": {"duty_percent": 40, "fade_ms": 5000},
    "ldr_cap":  30,          # current cap from ldr_monitor (0–100 or None)
    "actual":   75           # what is currently being driven to hardware
  }
}
```

**Public interface:**
```python
arbiter.set_manual(output_id, duty_or_state, fade_ms, revert_s)
arbiter.clear_manual(output_id)
arbiter.set_pir(output_id, duty_or_state, fade_ms)
arbiter.clear_pir(output_id)           # called on vacancy timeout
arbiter.set_schedule(desired_state)    # called by schedule_engine each tick
arbiter.set_ldr_cap(cap_percent)       # called by ldr_monitor
arbiter.apply_scene(scene)             # applies scene as manual overrides
arbiter.get_actual_state()             # returns current driven state (for HB payload)
```

**On each call that changes state:** computes resolved output, applies LDR cap, calls `pwm_control` or `relay_control` to drive hardware, logs change.

**Dependencies:** `hardware/pwm_control`, `hardware/relay_control`, `core/config_manager`, `shared/simple_logger`

---

## `hardware/pwm_control.py` [EXTEND]

**Responsibility:** Drive 8 PWM channels. Support fade transitions (async, non-blocking).

**Changes from existing:**
- Expand from 5 to 8 channels
- Update default GPIO map: GP16,17,18,19,22,15,14,13
- Add `fade_to(channel_id, target_percent, fade_ms)` — steps duty in small increments over `fade_ms`, yields between steps (async-friendly)
- Remove hardcoded pin list — read from `hardware` config block

**Public interface:**
```python
pwm.set(channel_id, duty_percent)
pwm.fade_to(channel_id, target_percent, fade_ms)  # async
pwm.get(channel_id)                               # returns current duty%
pwm.deinit()
```

**Dependencies:** `machine.PWM`, `core/config_manager`

---

## `hardware/relay_control.py` [NEW]

**Responsibility:** Drive 2 relay channels via GPIO.

**Public interface:**
```python
relay.set(relay_id, state)    # state: True/False or "on"/"off"
relay.get(relay_id)           # returns bool
relay.deinit()
```

**GPIOs:** RLY1=GP10, RLY2=GP11 (from config hardware block)

**Dependencies:** `machine.Pin`, `core/config_manager`

---

## `hardware/pir_manager.py` [NEW]

**Responsibility:** Poll all 4 PIR GPIOs, debounce, manage motion/vacancy state, fire callbacks on state change.

**State machine per PIR:**
```
VACANT ──(motion detected)──► MOTION
MOTION ──(no motion for vacancy_timeout_s)──► VACANT
```

Debounce: require stable HIGH for 500ms before declaring motion.  
Vacancy: started from last motion pulse, reset on each new pulse.

**Public interface:**
```python
pir_manager.on_motion(pir_id, callback)    # register callback
pir_manager.on_vacancy(pir_id, callback)   # register callback
pir_manager.get_state(pir_id)             # "motion" | "vacant"
pir_manager.get_all_states()              # dict of all PIR states
```

Callbacks registered by `main.py` at boot — one for arbiter, one for lora_protocol (to send PIR_EVENT).

**Dependencies:** `machine.Pin`, `core/config_manager`, `shared/simple_logger`

---

## `hardware/ldr_monitor.py` [NEW]

**Responsibility:** Continuously sample LDR ADC, maintain rolling average, compute current brightness cap, notify arbiter when cap changes.

**Public interface:**
```python
ldr_monitor.get_ambient_percent()    # 0–100, rolling average
ldr_monitor.get_cap_percent()        # current cap (None if no rule matched)
ldr_monitor.on_cap_change(callback)  # fires when computed cap changes
```

**Logic:**
- Sample GP26 every 1s
- Maintain circular buffer of `smoothing_window_s` samples (default 60)
- Compute average → map ADC range to 0–100%
- Evaluate `ldr.cap_rules` highest-threshold-first → derive cap_percent
- If cap changes → call registered callback (arbiter updates its cap)

**Dependencies:** `machine.ADC`, `core/config_manager`

---

## `hardware/rtc_module.py` [KEEP]

No changes. Existing DS3231 interface is correct and tested.

---

## `hardware/rtc_shared.py` [KEEP]

No changes. Singleton I2C + DS3231 init, shared across modules.

---

## `hardware/status_led.py` [REPLACE]

**Responsibility:** Drive the WS2812 addressable status LED (GP5) to communicate unit state visually.

**Replaces:** existing simple RGB GPIO driver — completely different interface.

**States and colours:**

| State | Colour | Pattern |
|-------|--------|---------|
| Booting | White | Slow pulse |
| WiFi connecting (coordinator) | Blue | Fast blink |
| LoRa init | Cyan | Solid |
| Running — all OK | Green | Solid dim |
| Running — leaf offline detected | Yellow | Solid |
| Manual override active | Purple | Solid dim |
| Error / safe mode | Red | Fast blink |

**Public interface:**
```python
status_led.set_state(state_name)    # e.g. "running_ok", "error", "pir_active"
status_led.set_colour(r, g, b)      # direct colour override
status_led.off()
```

**Dependencies:** `machine.Pin`, `neopixel` (MicroPython built-in)

---

## `hardware/i2c_sensors.py` [NEW]

**Responsibility:** Detect and poll optional I2C expansion sensors. Non-blocking — if sensor absent, module is silent.

**Supported sensors (auto-detected by I2C address scan on boot):**

| Sensor | I2C Address | Readings |
|--------|-------------|---------|
| BME280 | 0x76 / 0x77 | temp_c, pressure_hpa, humidity_pct |
| BME680 | 0x76 / 0x77 | temp_c, pressure_hpa, humidity_pct, gas_ohm |
| SHT31 | 0x44 / 0x45 | temp_c, humidity_pct |
| BH1750 | 0x23 / 0x5C | lux |
| SCD40 | 0x62 | co2_ppm, temp_c, humidity_pct |

**Public interface:**
```python
i2c_sensors.get_readings()
# returns dict of whatever sensors are present, e.g.:
# {
#   "bme280": {"temp_c": 28.4, "pressure_hpa": 1012.3, "humidity_pct": 65.2},
#   "bh1750": {"lux": 430.0}
# }
# returns {} if no sensors found
```

Polled every 60s. Readings exposed via `system_status` and REST API.  
Boot: scan I2C bus, init detected sensors, log found/not-found. Never raises exception for missing sensor.

**Dependencies:** `hardware/rtc_shared` (shares I2C bus), `shared/simple_logger`

---

## `comms/lora_transport.py` [NEW]

**Responsibility:** Low-level E220 UART driver. Configure module on boot, send/receive raw bytes, manage AUX pin discipline.

**E220 boot config sequence (AT commands in sleep mode):**
```
M0=1, M1=1  → sleep mode
Send: AT+ADDRESS=<unit_id>
Send: AT+NETWORKID=0
Send: AT+BAND=868000000   (or region-appropriate)
Send: AT+PARAMETER=9,7,1,<tx_power>   (SF9, BW125, CR4/5)
Send: AT+MODE=0           → fixed-point mode
M0=0, M1=0  → normal mode
```

**Public interface:**
```python
transport.send(dest_addr, payload_bytes)   # blocks until AUX clear, then transmits
transport.recv()                           # returns bytes or None (non-blocking)
transport.available()                      # True if data in UART buffer
```

**AUX discipline:** checks GP4 before every send. Polls every 10ms, 2s timeout, raises `LoRaTimeoutError` on timeout.

**Dependencies:** `machine.UART`, `machine.Pin`, `core/config_manager`

---

## `comms/lora_protocol.py` [NEW]

**Responsibility:** Encode/decode JSON message envelopes, dispatch received messages to handlers, manage sequence numbers, handle ACK tracking and retransmit for ACK-required messages.

**Public interface:**
```python
protocol.send(msg_type, dest, payload)          # encode + send via transport
protocol.on(msg_type, handler_fn)               # register handler for inbound type
protocol.handle_incoming()                      # call in lora_listener_task loop
```

**ACK tracking:**
- Maintains dict of `{seq: (msg, timestamp, retries)}`
- For ACK-required messages: waits up to 10s, retries once, then logs failure
- On ACK received: removes from tracking dict

**Sequence numbers:** per-unit rolling 8-bit counter, incremented per send.

**Chunked config transfer:** `send_config(dest, config_json_str)` — orchestrates CFG_START → CFG_CHUNK × N → CFG_END, waits for final ACK.

**Dependencies:** `comms/lora_transport`, `shared/simple_logger`, `ujson`

---

## `comms/wifi_connect.py` [EXTEND]

**Responsibility:** WiFi connection and NTP sync. Coordinator only — leaf never calls this.

**Changes from existing:**
- No changes to connection logic
- NTP sync result passed to `rtc_module` to update DS3231 (already exists)
- After successful sync, calls `lora_protocol.send(TS, broadcast, {epoch, tz})` to push time to leaves
- Periodic re-sync: schedule NTP re-attempt every 24h via async task

**Dependencies:** `network`, `ntptime`, `hardware/rtc_module`, `comms/lora_protocol`

---

## `comms/mqtt_notifier.py` [KEEP]

No changes. Optional, disabled by default. Existing implementation unchanged.

---

## `coordinator/web_server.py` [EXTEND]

**Responsibility:** Async HTTP server on port 80. Serves static UI files and REST API endpoints.

**New endpoints added:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/fleet` | All units' status (coordinator aggregated state) |
| GET | `/api/units/<id>` | Single unit status |
| GET | `/api/units/<id>/config` | Unit config JSON |
| POST | `/api/units/<id>/config` | Push new config to unit (triggers CFG transfer) |
| GET | `/api/scenes` | List scenes (coordinator's own) |
| POST | `/api/scenes/<name>/apply` | Apply scene to unit(s) |
| POST | `/api/units/<id>/manual` | Manual override on unit |
| DELETE | `/api/units/<id>/manual` | Clear manual override |
| GET | `/api/sensors` | Environmental sensor readings (all units) |

**Existing endpoints kept:**
- `GET /` — dashboard (updated for fleet view)
- `GET /api/config` — coordinator's own config
- `GET /api/status` — coordinator's own status
- `POST /upload-config-*` — chunked direct upload (keep for direct device flashing)

**Dependencies:** `coordinator/api_handlers`, `coordinator/fleet_manager`, `shared/simple_logger`

---

## `coordinator/fleet_manager.py` [NEW]

**Responsibility:** Tracks state of all leaf units. Updated by incoming heartbeats and status responses. Source of truth for the web UI fleet view.

**State per unit:**
```python
{
  1: {
    "online":    True,
    "last_seen": 1745003600,   # epoch of last heartbeat
    "uptime":    3600,
    "ch":        [100,80,0,0,0,0,0,0],
    "rl":        [1,0],
    "pir":       [0,0,0,0],
    "ldr":       42,
    "sensors":   {},           # i2c expansion readings if reported
    "err":       0
  }
}
```

**Public interface:**
```python
fleet.update(unit_id, heartbeat_payload)    # called by lora_protocol handler
fleet.get(unit_id)                          # returns unit state dict
fleet.get_all()                             # returns all units
fleet.mark_offline(unit_id)                 # called after heartbeat timeout
fleet.is_online(unit_id)                    # bool
```

**Heartbeat timeout task:** runs every 10s, marks any unit offline whose `last_seen` exceeds `heartbeat_timeout_s`.

**Dependencies:** `core/config_manager`, `shared/simple_logger`

---

## `coordinator/api_handlers.py` [NEW]

**Responsibility:** Handler functions for REST API endpoints. Thin layer between web_server routing and the modules that do actual work.

Each handler receives the parsed request, calls the appropriate module, and returns a JSON-serialisable response dict.

**Key handlers:**
```python
handle_fleet_status()          → fleet_manager.get_all()
handle_unit_config_push(id)    → lora_protocol.send_config(id, config_str)
handle_scene_apply(name, ids)  → lora_protocol.send(SC, id, {"scene": name})
handle_manual_override(id)     → lora_protocol.send(MO, id, payload)
handle_manual_clear(id)        → lora_protocol.send(MO, id, {"revert_s": -1})
```

**Dependencies:** `coordinator/fleet_manager`, `comms/lora_protocol`, `core/config_manager`

---

## `shared/simple_logger.py` [KEEP]

No changes.

---

## `shared/system_status.py` [EXTEND]

**Responsibility:** Runtime status for this unit. Extended to include new peripheral states.

**Changes from existing:**
- Add relay states
- Add PIR states
- Add LDR reading and current cap
- Add I2C sensor readings
- Add LoRa link status (last tx/rx timestamps, error count)

**Dependencies:** None (written to by other modules, read by web_server and heartbeat)

---

## `shared/sun_times.py` [KEEP]

No changes. Existing sunrise/sunset lookup is correct.

---

## Implementation Order

Work in this sequence — each step is independently testable:

```
Phase 1 — Core hardware (no comms)
  1. config_manager   (extend schema)
  2. pwm_control      (extend to 8 channels + fade)
  3. relay_control    (new, simple)
  4. status_led       (replace with WS2812)
  5. pir_manager      (new)
  6. ldr_monitor      (new)
  7. schedule_engine  (extract from main.py + extend for relays)
  8. priority_arbiter (new — wire everything together)
  9. main.py          (update boot + tasks for above)
  → At this point: single unit, all hardware working, schedules running

Phase 2 — LoRa comms
  10. lora_transport  (E220 driver)
  11. lora_protocol   (message encode/decode/dispatch)
  12. wifi_connect    (extend for post-NTP TS broadcast)
  → At this point: multi-unit coordination working

Phase 3 — Coordinator layer
  13. fleet_manager
  14. api_handlers
  15. web_server      (extend for fleet REST API)
  → At this point: full system working, web UI can be updated

Phase 4 — Optional / polish
  16. i2c_sensors
  17. mqtt_notifier   (already exists, no changes)
  18. ram_telemetry   (already exists, minor updates)
```
