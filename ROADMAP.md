# Lokki — Roadmap

Where Lokki is, where it's going, and what's deliberately out of scope. Ideas and priorities, not commitments — order shifts based on real-world feedback from deployments.

Have an idea or a use case not covered here? [Open an issue](https://github.com/m-anish/PagodaLightPico/issues).

---

## Currently Shipping (v1)

- 8-channel PWM LED control with gamma correction
- 2 relay outputs, scheduled or event-triggered
- 4 PIR motion sensors with configurable on-motion / on-vacancy actions
- LDR ambient light sensor with daylight capping
- Time-window scheduling with sunrise/sunset keywords
- Named scenes, priority arbiter (manual > PIR > schedule)
- LoRa mesh — coordinator + up to 8 leaf units, star topology, FIXED-mode addressing, per-packet RSSI byte
- Coordinator: WiFi, NTP time sync, time broadcast to leaves
- Web-based config builder (offline-capable, browser-only) with starter profiles for common deployment shapes
- Live web dashboard: real-time channel/relay/PIR state, manual override sliders, scene buttons, remote reboot, config push, claim wizard, "Set time now" override
- Optional HTTP Basic auth on the dashboard (LAN-only; relay-based public auth designed in `docs/relay-design.md`)
- Integrated sun times generator
- MQTT notification scaffolding (broker config, topic prefix; event publishing not yet wired — see Phase 3)
- **Claim wizard** — factory-reset leaves announce themselves at `unit_id=99` with their chip UID; operator picks a real unit_id and name from the dashboard, no USB-flashing per unit
- **Time-sync gate** — schedule is paused until the coord has a confirmed wall-clock (NTP / DS3231 / TS broadcast / operator override); LED shows `time_waiting` cyan pulse and the dashboard renders a yellow banner with a "Set time from this browser" button
- **RTC fault tolerance** — DS3231 read failures fall back to the MCU's internal clock with throttled warnings; logger timestamps stay readable through transient I²C glitches
- **DS3231 chip-temp sensor** — surfaced in dashboard Sensors row + sent over LoRa as a per-unit "is anything overheating" trend signal
- **mDNS hostname** — `lokki.local` via either lwIP's built-in responder or a Python-side fallback, depending on the firmware build's IGMP/mDNS compilation
- **Interactive I²C / DS3231 diagnostic tool** — `/tools/i2c_helper.py` for bus scan, time read/write, temp read, and a soak loop for chasing intermittent failures

---

## Active work — open items on `main`

Concrete next-up engineering items. Some are issue-track-grade, others are polish.

### Known gaps

- **DST handling for `timezone.utc_offset_hours`** — currently manual; needs either an in-config DST rule table or seasonal-flip documentation in the user guide.
- **AUX-disciplined transmit deadlock stress test** — no observed issue but never specifically driven hard (e.g. coordinator broadcasting TS while a leaf is mid-SRP under high LDR change rate).

### Deferred features (Phase-4-shaped)

- **Persistent error log to flash** — ring buffer for the event bus so post-mortem on a field unit doesn't depend on a serial console. Needs careful wear-levelling design.
- **LoRa frame authentication** — HMAC-SHA256 (truncated to 8 B) signing + per-source replay window on every frame, key in `/secrets.json` separate from `config.json`. Already implemented on branch [`feature/lora-shared-secret`](https://github.com/m-anish/lokki/tree/feature/lora-shared-secret) (commit `47f6d88`); +312 lines across 8 files. Not urgent for lab / friends-and-family use because the E220's `crypt_h`/`crypt_l` radio-level encryption already keeps the casually curious out — revisit before any deployment where a motivated attacker could plausibly be in radio range. **Merge effort ~2–3 h:** conflict-prone files are `lora_protocol.py`, `main.py`, `update.sh`, `docs/lora-protocol.md` (all touched heavily since the branch was cut); re-validate HB/SRP size budgets (HMAC adds 22 B per frame, but recent short-key + conditional-field work freed ~40 B so the math works out better than the branch author saw); confirm the claim-wizard flow still works under signing.
- **`docs/auth-design.md`** — sibling of `relay-design.md` covering a reusable auth library design once Lokki has more company. Defer until there's a second project that would consume it.

### Polish

- Reconcile `_dashboard_html`-era debug behaviour in any remaining scripts.
- Consolidate per-handler imports in `api_handlers.py` to top-of-file (only if it measurably helps boot RAM).

### Hardware-side open items (firmware can't fix)

- **LM2596 brownout** theory for the simultaneous-EIO pattern across I²C / UART / WiFi — add 100 µF + 100 nF decoupling at VSYS / E220 V+ on the next board rev.
- **DS3231 backup battery** health audit across deployed units (the recent EIO turned out to be a cold solder joint, but battery state across the fleet is unaudited).
- Next PCB rev: MOSFET-on-GPIO for forced E220 power-cycle as a hardware recovery path.

---

## Phase 3 — Notifications & Alerting

The config schema already has a `notifications` block with MQTT fields, and `comms/mqtt_notifier.py` connects to a broker. What's missing is the actual event-publishing layer and the cross-protocol bridges.

- **MQTT event publishing** — publish structured events on motion, vacancy, LDR threshold crossings, unit offline/online transitions, and errors.
- **Telegram bot bridge** — a lightweight companion service (or self-hosted script) that subscribes to the MQTT broker and forwards alerts to a Telegram chat or group. No cloud account required beyond a Telegram bot token.
- **WhatsApp notifications** — same bridge pattern via CallMeBot or Twilio. Useful for venues that don't use Telegram.
- **Alert types**:
  - Leaf unit went offline / came back online
  - PIR motion detected (with unit name and zone)
  - LDR ambient level crossed a threshold (e.g., daylight starting / ending)
  - Firmware error count spike
  - Coordinator lost WiFi / NTP sync failed
- **Daily digest** — optional end-of-day summary: uptime, total motion events, hours each channel was active.

---

## Phase 4 — OTA Firmware Updates

Once a venue has 8 units deployed across a building, updating firmware over USB is not realistic. (Config push over LoRa already works via the chunked transfer; this phase is the *firmware* counterpart.)

- **Coordinator OTA** — coordinator downloads a new firmware package from a GitHub release (or a self-hosted URL) over WiFi, writes it to flash, and reboots.
- **Leaf OTA over LoRa** — coordinator chunks a firmware image and pushes it to each leaf over LoRa using the existing chunked transfer protocol; leaf verifies checksum and reboots.
- **Version tracking** — current firmware version reported in fleet status and heartbeat payload.
- **Rollback** — if the new firmware fails to boot (watchdog timeout), automatically revert to the previous version stored in a backup partition.

---

## Phase 5 — Advanced Scheduling

- **Calendar overrides** — define special schedules for specific dates (public holidays, seasonal events, venue closures) that take precedence over the regular week schedule.
- **Scene sequencing** — chain multiple scenes with time delays (e.g., slow fade from "evening" to "night" over 30 minutes at 9 PM).
- **Circadian rhythm mode** — gradually shift LED channel balance through the day to support natural alertness cycles; useful for work and study spaces.
- **Occupancy-adaptive scheduling** — track PIR motion history and flag time windows where the configured schedule doesn't match actual occupancy; surface these as suggested adjustments in the dashboard.

---

## Phase 6 — Ecosystem & Integrations

- **Home Assistant integration** — MQTT discovery so Lokki channels and scenes appear automatically as HA entities; full control from HA dashboards and automations.
- **Voice assistant** — Alexa or Google Home skill via MQTT, for hands-free scene activation in venues.
- **Progressive Web App (PWA)** — installable mobile app wrapping the web dashboard; works on-site without an internet connection once the coordinator is reachable on the local network.
- **Multi-coordinator mesh** — allow two or more coordinators to share fleet state for larger campuses; one becomes primary for NTP/time sync, others relay.

---

## Phase 7 — Energy & Analytics

- **Channel on-time tracking** — log cumulative active hours per LED channel across each day.
- **Estimated energy consumption** — given known LED wattage per channel (configurable), estimate daily and monthly kWh.
- **Usage dashboard** — visualise per-channel activity as a heatmap or timeline in the web UI.
- **Anomaly detection** — flag channels that are on significantly more or less than their historical average (sensor failure, stuck relay, config error).

---

## Longer-Term Ideas

Less certain but worth tracking:

- **I²C sensor expansion** — temperature, humidity, CO₂, and occupancy count sensors feeding into schedule decisions and MQTT events. (BME280 / BH1750 / SCD40 already supported in `hardware/i2c_sensors.py`; just needs more drivers + UI surface.)
- **Physical scene cycling** — repurpose a spare GPIO button to step through scenes locally without any dashboard.
- **Config backup and restore** — export full fleet configuration (all units) as a single archive from the dashboard; restore to a replacement unit. (Per-unit backup via `GET /api/config` already works.)
- **Secure remote access via a self-hosted relay** — each coordinator gets a stable public URL (e.g. `abc1234.lokki.app`) by opening an outbound WebSocket to a Lokki-operated relay. Public traffic is proxied over that tunnel; auth and TLS are handled at the relay so the Pico stays simple, and the dashboard renders a QR code of its own public URL. No companion hardware, no port forwarding. **Design:** [docs/relay-design.md](docs/relay-design.md).

---

## What Won't Be in Lokki

To keep the system honest about its scope:

- **No cloud dependency** — Lokki is designed to run indefinitely without any external service. Notification integrations are optional add-ons, not core functionality.
- **No proprietary protocol** — LoRa messages are documented and open. The MQTT schema will be published.
- **No mandatory app** — the web dashboard works in any browser. A companion app is a convenience, not a requirement.
