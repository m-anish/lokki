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

---

## Phase 2 — Remote Control & Live Dashboard

The coordinator already serves a web app. The next step is making it genuinely useful for daily operation without SSH or a USB cable.

- **Authenticated web dashboard** — token or password auth so the dashboard isn't open to anyone on the LAN
- **Real-time LED control** — brightness sliders and scene buttons that send live manual overrides to units without touching the config file
- **Live fleet status** — per-unit channel state, PIR state, LDR reading, uptime, error count, last-seen — auto-refreshing without a page reload
- **Remote reboot** — trigger a coordinator or leaf reboot from the dashboard
- **Config push from dashboard** — edit config in the browser, push directly to the running coordinator (already partially implemented via Save to device), with confirmation and reboot prompt

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
- **Secure remote access** — built-in support for a tunnelling solution (e.g., Cloudflare Tunnel) so the dashboard is reachable from outside the LAN without port forwarding

---

## What Won't Be in Lokki

To keep the system honest about its scope:

- **No cloud dependency** — Lokki is designed to run indefinitely without any external service. Notification integrations are optional add-ons, not core functionality.
- **No proprietary protocol** — LoRa messages are documented and open. The MQTT schema will be published.
- **No mandatory app** — the web dashboard works in any browser. A companion app is a convenience, not a requirement.
