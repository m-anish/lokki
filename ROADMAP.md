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
- Web-based config builder (browser-only) with starter profiles for common deployment shapes
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
- **`docs/auth-design.md`** — sibling of `relay-design.md` covering a reusable auth library design once Lokki has more company. Defer until there's a second project that would consume it.

### Polish

- Reconcile `_dashboard_html`-era debug behaviour in any remaining scripts.
- Consolidate per-handler imports in `api_handlers.py` to top-of-file (only if it measurably helps boot RAM).

### Hardware-side open items (firmware can't fix)

- **LM2596 brownout** theory for the simultaneous-EIO pattern across I²C / UART / WiFi — add 100 µF + 100 nF decoupling at VSYS / E220 V+ on the next board rev.
- **DS3231 backup battery** health audit across deployed units (the recent EIO turned out to be a cold solder joint, but battery state across the fleet is unaudited).
- Next PCB rev: MOSFET-on-GPIO for forced E220 power-cycle as a hardware recovery path.

---

## Phase 2.5 — Engineering & UX Modernisation

The current dashboard and Config Builder are two separate apps stitched together by a footer link, with very different mental models (cards-and-buttons vs forms-and-sections). The protocol is at the same time over-eager (full ~5 KB config push for any 1-field change → ~6 s wait) and under-formalised (validator lives in Python, schema doc is separate JSON, client mirrors rules informally). This phase tackles both as one coordinated overhaul. Roughly 10–12 weeks of focused work end-to-end; shippable in independent sub-phases.

### Engineering protocol upgrades (prerequisites for UX overhaul)

Order: provisioning → schema → patch protocol. Each is independently useful even if subsequent items slip.

- **Batch-provisioning interactive CLI** (~half a day). `./update.sh --fresh --role=coordinator --leaves=N` prompts interactively for each leaf's name (`Leaf 1 name: …`), generates the coord's config AND pre-caches blank-slate leaf configs (`/leaf-configs/N.json`) on the coord's flash. When each leaf later joins via claim wizard or USB stub, `/api/units/N/config` already returns a cached config — first-time-push step disappears.
- **JSON Schema as config validation source of truth** (~1 week). `web/app/config.schema.json` becomes the authoritative spec, copied to `firmware/.../src/config/config.schema.json` at flash time. A small purpose-built Lokki validator (~300 LOC, MicroPython-friendly subset: `type`, `required`, `enum`, `minimum/maximum`, `properties`, `items`, `additionalProperties`, `pattern`, `if/then/else`) replaces `core/config_manager._validate()`'s body. A thin Python wrapper preserves the SafeModeError special case for major-version mismatch. New `POST /api/config/validate` endpoint accepts `{config: {...}}` (full) or `{patch: [...], base_unit_id: N}` (incremental — coord applies patch to its cached config then validates the merged result). **Coord-only** — leaves stay slim; coord is now the single source of truth for any leaf config validation.
- **Incremental config protocol** (~3–4 days). Two new LoRa messages alongside existing CFG_START/CHUNK/END:
  - `CFG_PATCH` for single-value updates (`{"path": "led_channels/2/default_duty_percent", "value": 80}`) — ~80 B, one packet, ~300 ms total.
  - `CFG_SECTION` for replacing a top-level section (`{"section": "led_channels", "value": [...]}`) — chunked but still ~1/5 of full config.
  - Smart dispatch on the coord: one field changed → `CFG_PATCH`; 2-N fields in one section → `CFG_SECTION`; cross-section or >50% of fields → fall back to full transfer.
  - Leaf-side path walker (~80 LOC) supports `set` on `led_channels[N]/foo`, `scenes/$name`, etc. Validates merged result against the schema (now JSON-Schema-driven) before atomic flash write. Leaf reboots after a patch that affects boot-time wiring (hardware pins, system role) — same trigger logic as current CFG_END.
  - **Drops "save a channel default" round-trip from ~6 s to ~300 ms** — prerequisite for the inline-edit UX feel.

### UX overhaul

Five sub-phases. Each leaves the app *better-shaped* than before — never a "redesign halfway done and everything is worse" state.

- **UX-1 — Unification foundation** (~1 week).
  - Merge `config-builder.html` into `dashboard.html` as a hidden view, route the sidebar to it. Old `/config-builder.html` URL kept as a redirect for backward compat with bookmarks.
  - Sidebar gains Coordinator + Leaf N entries (one per claimed leaf, generated from `/api/fleet`).
  - Dark mode pass — honor `prefers-color-scheme` throughout.
  - **Drop offline-only Config Builder mode** — keep "Load from file" (lightweight, useful for review/backup); drop everything else that pretended the page worked without a coord. ~150 LOC of conditional logic gone.
  - Nothing functional changes for end users yet — pure organizational restructure so subsequent phases have a place to live.

- **UX-2 — Per-unit detail pages** (~2 weeks).
  - Build tabbed per-leaf and per-coord detail pages: live status header + tabbed config sections inline (Overview / Channels / Relays / PIRs / Schedule / Scenes / Advanced). Tabbed layout chosen specifically to support a wizard-style flow in later phases.
  - Inline manual override sliders (replace the Control modal entirely).
  - **Per-leaf reboot button** (`POST /api/units/{id}/reboot` + new LoRa `RB` message handler) — this folds in the standalone "Leaf reboot" gap.
  - "Configure" buttons on the fleet overview cards link directly to the relevant detail page.

- **UX-3 — Schedule visualizer + scene editor** (~2–3 weeks).
  - 24h timeline as static SVG, showing schedule windows across channels for the currently-selected unit. Click-to-edit (a window opens an inline editor); no drag. Phase 4/5/6 can add drag later if it becomes worth the work.
  - Timeline data model uses a typed event enum (`regular | override | sequence | …`) from day one — Phase 5 calendar overrides + scene sequencing layer on top without rewriting the visualizer.
  - **Pluggable data source** — visualization layer separated from data so Phase 7's energy/usage heatmap can reuse the same chart component with a different feed.
  - Scene editor v2: "snapshot current state of these channels/relays into a scene" replaces the manual form. Hover-preview shows what each scene would look like without applying.

- **UX-4 — Multi-unit operations + activity history + config backup** (~1–2 weeks).
  - Checkbox-select N units on the fleet view, toolbar with bulk operations (Apply scene to selected / Push config to selected / Reboot selected).
  - **Config backup & restore** (folded in from Longer-Term Ideas): export full fleet configuration (all units) as a single archive; restore to a replacement unit.
  - Activity history view: recent manual overrides, config pushes, reboots, claim events. Answers "what changed since yesterday?"
  - Cross-linking from logs to leaves / scenes / config fields (clicking a log line about Leaf 2 navigates to Leaf 2's detail page with the relevant tab open).

- **UX-5 — Mobile-first polish + PWA** (~1–2 weeks).
  - Mobile-first redesign of the unified app (the unmodified Config Builder is currently unusable on a phone).
  - Inline help: hover popovers replacing `.field-hint` text where it's longer than one line.
  - Global search across leaves, channels, scenes — useful at 8-leaf scale with 64 channels worth of names.
  - **PWA basics — manifest.json + minimal service worker for offline-loading the dashboard shell** (folds in Phase 6's "PWA" item; cheap if done now alongside the mobile pass, expensive to retrofit). Once installed on a phone's home screen, the dashboard is a click away.

### Coordinated edits to existing entries

- Phase 6's "Progressive Web App" line moved into UX-5 (already done above; note left as a pointer).
- Phase 7's "Usage dashboard — heatmap or timeline" item should reuse UX-3's chart layer when it gets built — flagged here so future-us doesn't write a parallel one.
- Phase 5's calendar overrides + scene sequencing + circadian mode layer onto UX-3's typed timeline model — no rewrites required.
- Phase 4's firmware version tracking adds a new HB field (`v` or `fw`) when it lands — the HB fitter's drop-order list already has room near the bottom.

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
- **Multi-coordinator mesh** — allow two or more coordinators to share fleet state for larger campuses; one becomes primary for NTP/time sync, others relay.

> PWA support (manifest + service worker for installable on-device dashboard) moved into Phase 2.5 / UX-5 — see below.

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
- **Secure remote access via a self-hosted relay** — each coordinator gets a stable public URL (e.g. `abc1234.lokki.app`) by opening an outbound WebSocket to a Lokki-operated relay. Public traffic is proxied over that tunnel; auth and TLS are handled at the relay so the Pico stays simple, and the dashboard renders a QR code of its own public URL. No companion hardware, no port forwarding. **Design:** [docs/relay-design.md](docs/relay-design.md).
- **I²C provisioning between coord and leaf** — coord ↔ leaf wired through the PCB expansion port; a leaf gesture (hold reset 10 s, or held-from-boot) puts the leaf into I²C-slave mode at a fixed address, and the coord pushes a full config over the wire — no LoRa, no USB to the leaf. Useful for bench provisioning at scale, recovery on a leaf whose LoRa is broken inside a sealed enclosure, and RF-free provisioning at security-sensitive sites. **Effort blocker:** MicroPython's `machine.I2C` is master-only; would need a custom firmware build with a C wrapper around the RP2040/RP2350 I²C peripheral, or a PIO state machine implementing slave mode. ~3–7 days for the slave-mode plumbing; everything else is small. Parked — claim wizard + USB-flash cover the common cases today.

---

## What Won't Be in Lokki

To keep the system honest about its scope:

- **No cloud dependency** — Lokki is designed to run indefinitely without any external service. Notification integrations are optional add-ons, not core functionality.
- **No proprietary protocol** — LoRa messages are documented and open. The MQTT schema will be published.
- **No mandatory app** — the web dashboard works in any browser. A companion app is a convenience, not a requirement.
