# Lokki

A campus-scale LED lighting automation system for wellness and hospitality venues — meditation centers, retreats, and resorts.

Lokki units are self-contained lighting controllers that coordinate via LoRa radio. Each unit runs its schedule autonomously. Multiple units on the same campus share state and respond to each other's events without cloud dependency.

---

## What It Does

- Controls up to **8 independent LED channels** (PWM, 0–100%)
- Switches **2 relay outputs** (scheduled or event-triggered)
- Reads **4 PIR motion sensors** — motion overrides the schedule, lights up the space
- Reads an **LDR ambient light sensor** — caps brightness automatically in daylight
- Schedules lighting based on **time windows**, **sunrise/sunset**, or both
- Coordinates across **4–8 units per campus** via LoRa — no cloud, no internet required
- Exposes a **web dashboard and REST API** on the coordinator unit
- Optionally reports to an **MQTT broker**

---

## Hardware

Built on a custom PCB (Rev0) with:

| Component | Role |
|-----------|------|
| Raspberry Pi Pico 2 W (coordinator) | RP2350, WiFi + LoRa |
| Raspberry Pi Pico 2 (leaf units) | RP2350, LoRa only |
| PT4115 × 8 | Constant-current LED drivers |
| IRLML2502 + relay × 2 | Switched AC/DC loads |
| DS3231 | Battery-backed RTC |
| E220-900T22D | LoRa radio (~868MHz) |
| LM2596 module | +30V → +5V power supply |
| WS2812 | Addressable status LED |

Full GPIO map and hardware details: [docs/architecture.md](docs/architecture.md)

---

## Documentation

| Document | Contents |
|----------|----------|
| [docs/architecture.md](docs/architecture.md) | System overview, topology, GPIO map, failure modes |
| [docs/config-schema.md](docs/config-schema.md) | Complete `config.json` structure with examples |
| [docs/lora-protocol.md](docs/lora-protocol.md) | LoRa message types, packet format, chunked transfer |
| [docs/firmware-modules.md](docs/firmware-modules.md) | Module map, interfaces, implementation phases |

---

## Repository Structure

```
firmware/micropython/src/   # MicroPython firmware
  core/                     # Config, schedule engine, priority arbiter
  hardware/                 # PWM, relay, PIR, LDR, RTC, status LED, I2C sensors
  comms/                    # LoRa transport/protocol, WiFi, MQTT
  coordinator/              # Web server, fleet manager, REST API
  shared/                   # Logger, status, sun times

hardware/kicad/             # KiCad PCB project (Rev0, in progress)
web/app/                    # Static fleet management web UI (GitHub Pages)
shared/json/                # Shared JSON schemas
docs/                       # Architecture and design documentation
```

---

## Quick Start

### 1. Flash MicroPython (RP2350 build)
Download the Pico 2 / Pico 2 W MicroPython UF2 from [micropython.org](https://micropython.org/download/) and flash it.

### 2. Configure
Copy the sample config and edit for your deployment:
```bash
cp firmware/micropython/src/config/samples/config.json.sample config.json
```

See [docs/config-schema.md](docs/config-schema.md) for all options. The web helper app at [web/app/config-builder.html](web/app/config-builder.html) can generate a valid config interactively.

### 3. Deploy firmware
```bash
# rshell
rshell -p /dev/ttyACM0 cp -r firmware/micropython/src/* /pyboard/
rshell -p /dev/ttyACM0 cp config.json /pyboard/
rshell -p /dev/ttyACM0 cp sun_times.json /pyboard/

# or ampy
ampy -p /dev/ttyACM0 put firmware/micropython/src/
ampy -p /dev/ttyACM0 put config.json
ampy -p /dev/ttyACM0 put sun_times.json
```

### 4. Access the web UI (coordinator only)
```
http://<coordinator-ip>/
```

---

## Network Topology

```
[ Internet / NTP ]
        |
      WiFi
        |
 [ COORDINATOR ]  ←→  Web UI, REST API, fleet state
        |
      LoRa (~868MHz)
   ┌───┴───┐
[LEAF 1] [LEAF 2] ...    ← run schedules autonomously
```

Each leaf runs its own schedule locally. Coordinator failure does not stop leaf operation.

---

## Development

```bash
# REPL access
rshell -p /dev/ttyACM0 repl
screen /dev/ttyACM0 115200

# Soft reset in REPL
Ctrl+D

# Check free memory
import gc; gc.collect(); print(gc.mem_free())
```

Active development is on the `dev/lokki-v1` branch. See [docs/firmware-modules.md](docs/firmware-modules.md) for the phased implementation plan.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
