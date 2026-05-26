# Lokki — Roadmap

This document outlines the planned direction for Lokki beyond the current v1 release. These are ideas and priorities, not commitments — the order and scope will shift based on real-world feedback from deployments.

Have an idea or a use case not covered here? [Open an issue](https://github.com/m-anish/PagodaLightPico/issues) — that's where priorities get decided.

---

## Currently Shipping (v1)

- 8-channel PWM LED control with gamma correction
- 2 relay outputs, scheduled or event-triggered
- 4 PIR motion sensors with configurable on-motion / on-vacancy actions
- LDR ambient light sensor with daylight capping
- Time-window scheduling with sunrise/sunset keywords
- Named scenes, priority arbiter (manual > PIR > schedule)
- LoRa mesh — coordinator + up to 8 leaf units, star topology
- Coordinator: WiFi, NTP time sync, time broadcast to leaves
- Web-based config builder (offline-capable, browser-only)
- Integrated sun times generator
- MQTT notification scaffolding (broker config, topic prefix)
- **Claim wizard** — factory-reset leaves announce themselves; operator claims and names each one from the dashboard, no USB-flashing per unit
- **Time-sync gate** — schedule is paused until the coord has a confirmed wall-clock (NTP, DS3231, or operator override); LED shows `time_waiting` cyan pulse and a yellow banner appears on the dashboard
- **RTC fault tolerance** — DS3231 read failures fall back to the MCU's internal clock with throttled warnings; logger timestamps stay readable through transient I²C glitches
- **DS3231 chip-temp sensor** — surfaced in dashboard Sensors row + sent over LoRa as a per-unit "is anything overheating" trend signal
- **mDNS hostname** — `lokki.local` via either lwIP's built-in responder or a Python-side fallback, depending on the firmware build's IGMP/mDNS compilation

---

## Phase 2 — Remote Control & Live Dashboard ✅ (substantially complete)

The coordinator serves a web app at `http://<coord-ip>/` (or `http://lokki.local/` where mDNS works) with real-time control, live fleet status, and config-push flows. What was the original plan, and where it stands today:

- ✅ **Authenticated web dashboard** — HTTP Basic auth on the Pico for LAN-only deployments; relay-based public auth lives in [docs/relay-design.md](docs/relay-design.md)
- ✅ **Real-time LED control** — brightness sliders, scene buttons, manual overrides with revert timers
- ✅ **Live fleet status** — per-unit channel/relay/PIR state, LDR, uptime, error count, last-seen, RSSI; auto-refreshes every 15 s with a 1 s relative-time ticker
- ✅ **Remote reboot** — `POST /api/reboot` + dashboard button
- ✅ **Config push from dashboard** — Config Builder's "Save to device" pushes over LoRa for leaves, applies directly on the coordinator
- ✅ **Claim wizard** (bonus) — factory-reset leaves auto-surface as "New device" cards; operator picks unit_id and name from the browser instead of USB-flashing each one

---

## Phase 3 — Notifications & Alerting

The config schema already has an `notifications` block with MQTT fields. This phase makes it useful.

- **MQTT event publishing** — publish structured events on motion, vacancy, LDR threshold crossings, unit offline/online transitions, and errors
- **Telegram bot bridge** — a lightweight companion service (or self-hosted script) that subscribes to the MQTT broker and forwards alerts to a Telegram chat or group. No cloud account required beyond a Telegram bot token.
- **WhatsApp notifications** — same bridge pattern via CallMeBot or Twilio. Useful for venues that don't use Telegram.
- **Alert types**:
  - Leaf unit went offline / came back online
  - PIR motion detected (with unit name and zone)
  - LDR ambient level crossed a threshold (e.g., daylight starting / ending)
  - Firmware error count spike
  - Coordinator lost WiFi / NTP sync failed
- **Daily digest** — optional end-of-day summary: uptime, total motion events, hours each channel was active

---

## Phase 4 — OTA Firmware Updates

Once a venue has 8 units deployed across a building, updating firmware over USB is not realistic.

- **Coordinator OTA** — coordinator downloads a new firmware package from a GitHub release (or a self-hosted URL) over WiFi, writes it to flash, and reboots
- **Leaf OTA over LoRa** — coordinator chunks a firmware image and pushes it to each leaf over LoRa using the existing chunked transfer protocol; leaf verifies checksum and reboots
- **Version tracking** — current firmware version reported in fleet status and heartbeat payload
- **Rollback** — if the new firmware fails to boot (watchdog timeout), automatically revert to the previous version stored in a backup partition

---

## Phase 5 — Advanced Scheduling

- **Calendar overrides** — define special schedules for specific dates (public holidays, seasonal events, venue closures) that take precedence over the regular week schedule
- **Scene sequencing** — chain multiple scenes with time delays (e.g., slow fade from "evening" to "night" over 30 minutes at 9 PM)
- **Circadian rhythm mode** — gradually shift LED channel balance through the day to support natural alertness cycles; useful for work and study spaces
- **Occupancy-adaptive scheduling** — track PIR motion history and flag time windows where the configured schedule doesn't match actual occupancy; surface these as suggested adjustments in the dashboard

---

## Phase 6 — Ecosystem & Integrations

- **Home Assistant integration** — MQTT discovery so Lokki channels and scenes appear automatically as HA entities; full control from HA dashboards and automations
- **Voice assistant** — Alexa or Google Home skill via MQTT, for hands-free scene activation in venues
- **Progressive Web App (PWA)** — installable mobile app wrapping the web dashboard; works on-site without an internet connection once the coordinator is reachable on the local network
- **Multi-coordinator mesh** — allow two or more coordinators to share fleet state for larger campuses; one becomes primary for NTP/time sync, others relay

---

## Phase 7 — Energy & Analytics

- **Channel on-time tracking** — log cumulative active hours per LED channel across each day
- **Estimated energy consumption** — given known LED wattage per channel (configurable), estimate daily and monthly kWh
- **Usage dashboard** — visualise per-channel activity as a heatmap or timeline in the web UI
- **Anomaly detection** — flag channels that are on significantly more or less than their historical average (sensor failure, stuck relay, config error)

---

## Longer-Term Ideas

These are less certain but worth tracking:

- **I2C sensor expansion** — temperature, humidity, CO₂, and occupancy count sensors feeding into schedule decisions and MQTT events
- **Physical scene cycling** — repurpose a spare GPIO button to step through scenes locally without any dashboard
- **Config backup and restore** — export full fleet configuration (all units) as a single archive from the dashboard; restore to a replacement unit
- **Secure remote access via a self-hosted relay** — each coordinator gets a stable public URL (e.g. `abc1234.lokki.app`) by opening an outbound WebSocket to a Lokki-operated relay. Public traffic is proxied over that tunnel; auth and TLS are handled at the relay so the Pico stays simple, and the dashboard renders a QR code of its own public URL. No companion hardware, no port forwarding. **Design:** [docs/relay-design.md](docs/relay-design.md).

---

## What Won't Be in Lokki

To keep the system honest about its scope:

- **No cloud dependency** — Lokki is designed to run indefinitely without any external service. Notification integrations are optional add-ons, not core functionality.
- **No proprietary protocol** — LoRa messages are documented and open. The MQTT schema will be published.
- **No mandatory app** — the web dashboard works in any browser. A companion app is a convenience, not a requirement.
