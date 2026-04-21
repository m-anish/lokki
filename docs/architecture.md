# Lokki System Architecture

**Version:** 1.0-draft  
**Date:** 2026-04-18  
**Status:** Design — pending review

---

## 1. What Lokki Is

Lokki is a campus-scale lighting automation system for wellness and hospitality venues (meditation centers, retreats, resorts). Each Lokki unit is a self-contained lighting controller. Multiple units on the same campus coordinate via LoRa radio to create cohesive multi-zone lighting experiences.

**Design principles:**
- Every unit works standalone without network — schedules run locally
- Coordination is additive, not required — loss of LoRa degrades gracefully
- No cloud dependency — the system is fully self-hosted on-premises
- One PCB, one firmware, one codebase — role is determined by configuration

---

## 2. Hardware Platform

### MCU
| Role | MCU | WiFi | LoRa |
|------|-----|------|------|
| Coordinator | Raspberry Pi Pico 2 W (RP2350) | Yes | Yes |
| Leaf | Raspberry Pi Pico 2 (RP2350) | No | Yes |

Both use the same PCB. Role is set in `config.json`. RP2350 provides 520KB RAM — required headroom for LoRa protocol state, PIR state machines, and relay logic running concurrently.

### Per-Unit Hardware
| Peripheral | Quantity | Interface | Notes |
|------------|----------|-----------|-------|
| LED driver channels | 8 | PWM | PT4115 constant-current |
| Relay outputs | 2 | GPIO → IRLML2502 MOSFET | SPDT, 1N4001 flyback protection |
| PIR sensor inputs | 4 | GPIO (digital) via RJ45 | Zener + RC filtering on each |
| LDR ambient sensor | 1 | ADC (GP26) | Voltage divider, continuous read |
| RTC | 1 | I2C0 (GP20 SDA, GP21 SCL) | DS3231, battery-backed |
| LoRa radio | 1 | UART0 (GP0/GP1) | E220-900T22D, ~868MHz |
| Status LED | 1 | GPIO data (GP5) | WS2812 addressable LED |
| I2C expansion | 1 | I2C0 shared bus via RJ45 (RJ5) | Optional environmental sensors |
| Reset button | 1 | GPIO (GP12, pulled up 3V3) | Hardware reset |

### GPIO Map (complete)
| GPIO | Function | Direction | Notes |
|------|----------|-----------|-------|
| GP0 | UART0 TX → E220 RXD | OUT | LoRa transmit |
| GP1 | UART0 RX ← E220 TXD | IN | LoRa receive |
| GP2 | E220 M0 | OUT | LoRa mode select |
| GP3 | E220 M1 | OUT | LoRa mode select |
| GP4 | E220 AUX | IN | LoRa busy/ready indicator |
| GP5 | STATUS LED DIN | OUT | WS2812 addressable LED data |
| GP6 | PIR1 | IN | Digital, RC filtered |
| GP7 | PIR2 | IN | Digital, RC filtered |
| GP8 | PIR3 | IN | Digital, RC filtered |
| GP9 | PIR4 | IN | Digital, RC filtered |
| GP10 | RLY1 | OUT | MOSFET gate → relay coil |
| GP11 | RLY2 | OUT | MOSFET gate → relay coil |
| GP12 | RST_SW | IN | Pulled up to 3V3 |
| GP13 | PWM8 | OUT | PT4115 dim control |
| GP14 | PWM7 | OUT | PT4115 dim control |
| GP15 | PWM6 | OUT | PT4115 dim control |
| GP16 | PWM1 | OUT | PT4115 dim control |
| GP17 | PWM2 | OUT | PT4115 dim control |
| GP18 | PWM3 | OUT | PT4115 dim control |
| GP19 | PWM4 | OUT | PT4115 dim control |
| GP20 | I2C0 SDA | I/O | DS3231 RTC + I2C expansion shared |
| GP21 | I2C0 SCL | OUT | DS3231 RTC + I2C expansion shared |
| GP22 | PWM5 | OUT | PT4115 dim control |
| GP26 | LDR (ADC0) | IN | Analog ambient light level |
| GP27 | ADC1 | IN | Spare |
| GP28 | ADC2 | IN | Spare |

### Optional I2C Expansion Sensors (RJ5)
Firmware detects and reads these if present; boots normally if absent.

| Sensor | Measures |
|--------|----------|
| BME280 / BME680 | Temperature, pressure, humidity |
| SHT31 | Temperature, humidity (higher accuracy) |
| BH1750 | Lux (complements LDR) |
| SCD40 / SGP30 | CO2 / air quality |

CO2 monitoring is particularly relevant for enclosed meditation halls.

---

## 3. Network Topology

**Pattern: Star with Local Autonomy**

```
                    [ Internet / NTP ]
                            |
                          WiFi
                            |
                   ┌────────────────┐
                   │  COORDINATOR   │
                   │   Pico 2 W     │
                   │                │
                   │  Web UI        │
                   │  REST API      │
                   │  Fleet state   │
                   │  Time source   │
                   └───────┬────────┘
                           │
              LoRa (E220-900T22D, ~868MHz)
              ┌────────────┼────────────┐
              │            │            │
       ┌──────┴───┐  ┌─────┴────┐  ┌───┴──────┐
       │  LEAF 1  │  │  LEAF 2  │  │  LEAF N  │
       │ Pico 2   │  │ Pico 2   │  │ Pico 2   │
       │          │  │          │  │          │
       │ 8× LED   │  │ 8× LED   │  │ 8× LED   │
       │ 2× Relay │  │ 2× Relay │  │ 2× Relay │
       │ 4× PIR   │  │ 4× PIR   │  │ 4× PIR   │
       │ 1× LDR   │  │ 1× LDR   │  │ 1× LDR   │
       └──────────┘  └──────────┘  └──────────┘

Deployment scale: 4–8 units per campus
```

### Key Properties
- **Each unit runs its schedule locally.** Coordinator failure does not stop leaf operation.
- **Coordinator is elected by config**, not by protocol. Any unit can be coordinator.
- **LoRa range** (E220-900T22D): up to 5km open air — sufficient for any campus deployment without mesh routing.
- **No mesh routing required.** All units communicate directly with coordinator. If range is ever an issue, this is a future rev concern.

---

## 4. Unit Roles

### Coordinator
- Connects to WiFi on boot
- Syncs time via NTP → writes to local DS3231 → broadcasts to all leaves via LoRa
- Hosts web UI and REST API on port 80
- Aggregates status from all leaves (heartbeats)
- Pushes config changes and scenes to leaves on demand
- Runs its own local schedule, PIR, LDR, and relay logic (same as any leaf)
- Monitors leaf health — flags units that have not heartbeated within threshold

### Leaf
- Boots from local `config.json`
- Falls back to DS3231 RTC for time (no NTP)
- Listens for coordinator time sync and updates DS3231 accordingly
- Listens for config push and scene broadcasts from coordinator
- Sends status heartbeat to coordinator on interval
- Sends event notifications (PIR trigger, relay state change) to coordinator
- Runs schedule, PIR, LDR, and relay logic fully autonomously

### Role Assignment (config.json)
```json
"system": {
  "role": "coordinator",       // or "leaf"
  "unit_id": 1,                // unique per deployment, 1–8 (coordinator is always 0)
  "unit_name": "Pagoda",       // human-readable
  "peers": [2, 3, 4]           // unit_ids this coordinator manages (coordinator only)
}
```

---

## 5. Control Model

### Outputs
| Output | Type | Control Modes |
|--------|------|---------------|
| LED channel (×8) | PWM 0–100% | Schedule, Scene, Manual, PIR action, LoRa event |
| Relay (×2) | On/Off | Schedule, Scene, Manual, PIR action, LoRa event |

### Inputs
| Input | Type | Behaviour |
|-------|------|-----------|
| PIR (×4) | Digital trigger | Fires action on motion detect and on vacancy timeout |
| LDR | Continuous ADC | Rolling 60s average → brightness cap modifier |
| I2C sensors | Continuous poll | Environmental telemetry, logged and exposed via API |
| LoRa events | Message-driven | Peer unit events can trigger local actions |
| Schedule | Time-driven | Time windows with absolute or sunrise/sunset anchors |
| Manual | API / web UI | Immediate override, optional auto-revert timeout |

### Priority Stack
When multiple control signals are active simultaneously, higher priority wins:

```
1. Manual override (web UI / API command)
2. PIR motion trigger
3. Scheduled time window
4. LDR brightness cap        ← modifier on top of 1–3, not a replacement
5. Default / standby state
```

**LDR is a cap, not a trigger.** It never turns lights on or off. It only limits the ceiling of whatever the higher-priority signal requests. Example: schedule says 80%, LDR cap is 30% (bright daylight) → output is 30%.

**PIR motion always wins over schedule.** A dim scheduled period should still light up when someone enters the room. PIR vacancy timeout determines how long the motion state holds before reverting to schedule.

### Trigger → Action Rules
Each unit's config contains a list of rules:

```
trigger:  { type, source, condition }
action:   { type, target, value, transition_ms }
```

**Trigger types:** `time_window`, `sunrise_offset`, `sunset_offset`, `pir_motion`, `pir_vacancy`, `ldr_threshold`, `lora_event`, `manual`

**Action types:** `set_led`, `set_relay`, `set_scene`, `broadcast_lora_event`, `fade_led`

### Scenes
A scene is a named snapshot of desired output states across any combination of channels and relays. Scenes can be:
- Applied locally (one unit)
- Broadcast to all peers via LoRa (coordinator only)
- Triggered by any trigger type

---

## 6. Communication

### LoRa (E220-900T22D)
- **Mode:** UART AT command interface, transparent/fixed-point modes
- **Addressing:** Unit IDs 1–8, coordinator is always ID 0
- **Frequency:** ~868MHz (confirm region before production — 865–867MHz for India)
- **GPIOs:** TX=GP0, RX=GP1, M0=GP2, M1=GP3, AUX=GP4
- **Message types:** defined in `lora-protocol.md`

### WiFi (coordinator only)
- Connects to local network on boot
- Used for: NTP time sync, web UI serving, optional future remote access
- SSID/password in `config.json`

### Web / REST API (coordinator only)
- Port 80, HTTP
- Serves static UI files
- JSON API endpoints for fleet status, config, scenes, manual control
- Defined in full in `api-spec.md` (forthcoming)

---

## 7. Data Flow Diagrams

### Boot Sequence
```
All units:
  Boot → load config.json → init hardware → start schedule engine
  
Coordinator additionally:
  → connect WiFi → NTP sync → update DS3231
  → broadcast TIME_SYNC to all leaves via LoRa
  → start web server
  → start heartbeat collector

Leaf additionally:
  → listen for TIME_SYNC from coordinator
  → update DS3231 if received
  → start heartbeat broadcaster
```

### PIR Motion Event Flow
```
Leaf unit:
  PIR fires → evaluate trigger/action rules
            → apply action locally (e.g. set LED ch1 to 100%)
            → send PIR_EVENT to coordinator via LoRa

Coordinator:
  receives PIR_EVENT → update fleet state
                     → evaluate any cross-unit rules
                       (e.g. motion in Cell 3 → dim Pagoda corridor)
                     → apply cross-unit action if rule matches
                     → update web UI state
```

### Config Push Flow
```
User edits config in web UI → POST to coordinator REST API
Coordinator validates config
→ saves locally if for self
→ sends CONFIG_PUSH via LoRa to target leaf(s)
→ leaf receives, validates, saves config.json, applies
→ leaf sends ACK
→ coordinator updates fleet state
```

### Time Sync Flow
```
Coordinator NTP sync (on boot + every 24h)
→ write to coordinator DS3231
→ broadcast TIME_SYNC via LoRa (unit_id, epoch, timezone_offset)
→ each leaf receives → updates local DS3231
→ leaves use DS3231 for all schedule operations
```

---

## 8. Failure Modes and Degradation

| Failure | Behaviour |
|---------|-----------|
| Coordinator WiFi loss | Leaves unaffected. Coordinator continues on DS3231. Web UI still accessible on LAN. NTP re-attempted periodically. |
| Coordinator LoRa loss | Leaves unaffected — run local schedules. Coordinator loses fleet visibility. |
| Leaf LoRa loss | Leaf runs local schedule autonomously. Coordinator marks unit offline after heartbeat timeout. Other leaves unaffected. |
| Coordinator full failure | All leaves continue running local schedules indefinitely via DS3231. No fleet coordination until coordinator restored. |
| DS3231 battery dead | Unit falls back to last-known time on boot. Corrected on next LoRa TIME_SYNC or (coordinator) NTP sync. |
| I2C sensor missing | Firmware detects absence on boot, disables sensor polling, continues normally. |
| Config corruption | Firmware detects invalid JSON or schema mismatch on load, boots into safe mode (all outputs off, web UI accessible for re-upload). |

---

## 9. Firmware Module Map

Detailed in `firmware-modules.md`. Summary:

```
core/
  main.py              — async task orchestration, boot sequence
  config_manager.py    — load, validate, hot-reload config (extend existing)
  schedule_engine.py   — time windows, sunrise/sunset, trigger evaluation
  priority_arbiter.py  — resolves competing control signals per output

hardware/
  pwm_control.py       — 8-channel PWM (extend existing)
  relay_control.py     — 2-channel relay driver (new)
  pir_manager.py       — 4× PIR state machines (new)
  ldr_monitor.py       — ADC read, rolling average, cap calculation (new)
  rtc_module.py        — DS3231 interface (extend existing)
  status_led.py        — WS2812 addressable LED driver (replace existing)
  i2c_sensors.py       — optional expansion sensors, non-blocking (new)

comms/
  lora_transport.py    — E220 UART driver, send/receive (new)
  lora_protocol.py     — message types, encoding, routing (new)
  wifi_connect.py      — WiFi + NTP (extend existing)
  mqtt_notifier.py     — MQTT push (keep existing, optional)

coordinator/
  web_server.py        — HTTP server, extend for REST API (extend existing)
  fleet_manager.py     — heartbeat tracking, unit state aggregation (new)
  api_handlers.py      — REST endpoint handlers (new)

shared/
  simple_logger.py     — timestamped logging (keep existing)
  system_status.py     — runtime status (extend existing)
  sun_times.py         — sunrise/sunset lookup (keep existing)
```

---

## 10. File Structure (target)

```
firmware/micropython/src/
  main.py
  core/
  hardware/
  comms/
  coordinator/
  shared/
  config.json          (device-local, not in repo)
  sun_times.json       (device-local, not in repo)

docs/
  architecture.md      (this file)
  config-schema.md
  lora-protocol.md
  firmware-modules.md
  api-spec.md

web/app/               (helper app, extended for fleet management)
  index.html
  config-builder.html  (includes integrated sun times generator)
  scene-editor.html    (new)
  network-view.html    (new)

shared/json/
  config.schema.json   (extended)
```

---

## Open Questions

- **LoRa frequency** — ~868MHz assumed. Confirm exact sub-band before production flash of E220 config (865–867MHz for India, 868MHz for EU). Non-blocking for firmware development.
- **Cross-unit trigger rules** — deferred to v2. All trigger/action rules in v1 are local to each unit only.

Proceed to:
1. `config-schema.md` — define the new config.json structure
2. `lora-protocol.md` — define message types, packet format, addressing
3. `firmware-modules.md` — define each module's interface and responsibilities
