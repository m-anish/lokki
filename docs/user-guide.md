# Lokki — User Guide

This guide is for venue managers and on-site staff who operate a deployed Lokki installation. It covers day-to-day use, reading the status LED, and what to do when something looks wrong.

For initial setup and config building, see the [web setup tools](../web/app/index.html) and the main [README](../README.md).

---

## Status LED

Each Lokki unit has a WS2812 RGB status LED on the front of the PCB. It tells you what the unit is currently doing at a glance.

| Colour | Pattern | Meaning |
|--------|---------|---------|
| White | Pulsing (breathing) | Booting up — normal on power-on, lasts a few seconds |
| Cyan | Solid | Initialising LoRa radio |
| Blue | Blinking | Connecting to WiFi *(coordinator only)* |
| Green | Solid (dim) | Running normally — LoRa not connected or disabled |
| Green + blue flash | Green solid, brief blue flash every ~4 s (0.5 s long) | Running normally with LoRa active — healthy operating state |
| Amber | Solid | One or more leaf units not responding *(coordinator only)* |
| Purple | Solid (dim) | Manual override active — someone has taken direct control via the dashboard |
| Red | Blinking | Error — check the serial console or web dashboard for details |
| Off | — | Unit is off, or status LED pin is not configured |

**Normal operating colour is dim green with a brief blue heartbeat flash every ~4 seconds.** The blue flash confirms the LoRa radio is initialised and the unit is in the mesh. If you walk past a unit and it's plain green (no blue flash), LoRa failed to initialise — check the serial console.

**Amber on the coordinator** means at least one leaf unit has missed its heartbeat. This could mean the leaf is powered off, out of LoRa range, or has crashed. Check the fleet dashboard.

**Purple** clears automatically when the manual override expires, or when someone cancels it from the dashboard.

---

## Day-to-Day Operation

### Lights not following the schedule?

1. Check the status LED — if it's green, the unit is running and following its config.
2. A **purple** LED means a manual override is active. It will expire on its own, or cancel it from the web dashboard.
3. Confirm the current time is correct. The coordinator syncs via NTP on boot. If NTP failed (WiFi issue), the RTC holds the last known time — check the dashboard for uptime and connection status.
4. If the schedule uses `sunrise`/`sunset` keywords, confirm `sun_times.json` is present on the device. Without it the firmware uses a default 6:30 AM / 6:30 PM fallback.

### Updating the config without a USB cable (coordinator)

Once the coordinator is running:

1. Open the web dashboard at `http://<coordinator-ip>/` or `http://lokki-<unitname>.local/`
2. Navigate to **Config Builder**
3. Make your changes
4. Click **Save to device** — the config uploads directly over WiFi
5. Reboot the coordinator (power cycle, or use the reboot button if available in the dashboard)

The new config takes effect after reboot.

### Provisioning a brand-new leaf

Leaf units don't have WiFi, so the very first config has to land on them via USB. After that, every subsequent edit flows through the coordinator over LoRa. Alternatively, skip USB entirely by USB-flashing the leaf-stub via `utils/update.sh --fresh --role=leaf --id=N` and then completing setup over LoRa using the **claim wizard** in the dashboard (see Factory Reset & Claiming a New Leaf below).

1. Connect the leaf via USB. Use Thonny or `mpremote` to push a *minimal* `config.json` containing at least:
   - `system.role = "leaf"`
   - `system.unit_id = N` (matching one of the coordinator's `system.peers`)
   - `lora.channel` matching the coordinator (project default: **73**)
   - `lora.crypt_h` / `lora.crypt_l` matching the coordinator (project default: **0x07 / 0x93**)
   - `wifi` may be omitted (leaves don't use it)
2. Reboot the leaf. It'll come up, initialise LoRa, and start sending heartbeats.
3. On the coordinator, open **Config Builder**.
4. From the **Load from device** dropdown next to the button, pick **Leaf N**, click the button.
   - On a fresh setup the coordinator has no cached config for that leaf yet, so you'll see a hint to that effect.
5. Fill in the full config in the form — make sure `system.unit_id` is set to N and `system.role` is `leaf`.
6. Click **Save to device**. The coordinator does two things atomically:
   - Pushes the config to the leaf over LoRa (chunked).
   - Caches a copy on its own flash at `/leaf-configs/N.json`.
7. From now on, **Load from device → Leaf N** brings back the cached copy for editing. Save again to push updates.

### Editing a leaf's config later

Same as steps 3–6 above. The coordinator keeps the most recent pushed config on flash (in `/leaf-configs/`), so it survives coord reboots. If the cache is ever lost, the leaf is still running its own copy — just rebuild the config in the Builder and push again.

### Updating the coordinator's own config

1. Open the dashboard at `http://<coordinator-ip>/`
2. Open **Config Builder**, click **Load from device** (Coordinator selected by default)
3. Make changes
4. Click **Save to device**
5. Reboot the coordinator for the new config to take effect (power cycle, or use the dashboard's Reboot button)

---

## Fleet Dashboard

The coordinator's web dashboard (served over WiFi) shows:

- All leaf units and their online/offline status
- Per-channel LED brightness and relay states
- PIR motion state per sensor
- LDR ambient light reading
- Uptime and error count per unit

Access it at `http://<coordinator-ip>/` — check your router's DHCP table for the IP, or try `http://lokki.local/` (the hostname is set from `wifi.hostname` in `config.json`, defaulting to `lokki`). Whether `.local` resolves depends on whether the MicroPython build has the lwIP mDNS responder compiled in *and* whether your router forwards mDNS — if it doesn't resolve, fall back to the raw IP.

---

## Troubleshooting

### Coordinator won't connect to WiFi

- Confirm the SSID and password in `config.json` are correct (case-sensitive)
- Check the LED: blue blinking = trying to connect, red blinking = failed
- The coordinator falls back to RTC time if NTP fails — it will still run the schedule, but sunrise/sunset times may drift if the RTC battery is flat

### A leaf shows Offline in the dashboard

- Check the leaf's status LED — green means it's running; red blinking means it crashed
- Confirm the leaf's `unit_id` in its `config.json` matches what the coordinator lists in its `peers` list
- Check LoRa settings — both units must share the same `channel` **and** the same `crypt_h` / `crypt_l` pair. A mismatch in either silently drops every frame at the radio with no error indication on the dashboard. Project defaults: channel **73**, key **0x07 / 0x93**.
- Distance / obstruction — the E220 LoRa module has a long range in open air but reinforced concrete walls significantly reduce it

### All lights stuck on or stuck off after reboot

- The unit may have entered **safe mode** — status LED will be red blinking
- Safe mode triggers when `config.json` is missing, unreadable, or fails schema validation
- Connect via USB serial (115200 baud) to see the boot log
- Re-upload a valid `config.json` and reboot

### Time is wrong / sunrise-sunset schedule is offset

- The coordinator syncs time via NTP on boot. If WiFi failed at boot, the RTC holds the last saved time.
- Check whether the `utc_offset_hours` in the config matches the venue's current offset (including daylight saving if applicable)
- If `sun_times.json` was generated for the wrong timezone or location, regenerate it using the Config Builder's Sun Times section and re-upload

### PIR motion not triggering lights

- Confirm `enabled: true` in the PIR config for that sensor
- Check the `gpio_pin` matches the physical wiring
- The `on_motion` action must be set — `revert_to_schedule` is the default (does nothing if already on schedule)
- `vacancy_timeout_s` — confirm it's not set to 0, which would immediately revert back

---

## Config Quick Reference

The full config reference is in [docs/config-schema.md](config-schema.md). Common fields you might want to change in the field:

| Field | Location | What it does |
|-------|----------|-------------|
| `system.unit_name` | `system` | Display name in dashboard and mDNS hostname |
| `wifi.ssid` / `wifi.password` | `wifi` | WiFi credentials (coordinator only) |
| `timezone.utc_offset_hours` | `timezone` | UTC offset for time-based schedules |
| `led_channels[].time_windows` | per channel | Schedule windows with brightness and fade |
| `hardware.gamma` | `hardware` | Gamma correction factor (default 2.2) |
| `ldr.enabled` | `ldr` | Enable/disable daylight capping |
| `lora.channel` | `lora` | E220 channel (0–80, frequency = 850 + channel MHz). Must match across all units. Project default: **73**. |
| `lora.crypt_h` / `lora.crypt_l` | `lora` | 16-bit symmetric key — must match across all units. Project default: **0x07 / 0x93**. Set both to 0 to disable encryption. |

---

## Factory Reset & Claiming a New Leaf

The reset button (GPIO `hardware.reset_btn_pin`) has two gestures:

- **Short press (0.2–2 s)** — `machine.soft_reset()`. Useful for kicking a stuck unit or driving the LoRa-retry recovery loop manually. LED goes solid yellow at 0.2 s ("armed") as visual confirmation.
- **Long press (5 s+)** — **factory reset to "unclaimed" leaf**. The LED goes from yellow → red-blink ("warning") → red-blink-then-reset at 5 s. The leaf writes a default config with `unit_id = 99` (preserving the existing `lora`, `hardware`, `timezone`, and `wifi` sections so the board can still reach the fleet) and reboots.

After a long-press, the leaf comes up at `unit_id = 99` and starts heartbeating. The coordinator catches the HB (with the leaf's chip UID) and surfaces it as a **"New device"** card in the dashboard, above the Fleet Status. From there:

1. Click **Flash to identify** — the matching board flashes its LED magenta for 3 s so you can spot which physical unit you're about to claim.
2. Pick a `Unit ID` (1–8; in-use IDs are greyed out) and optional name.
3. Click **Claim**. The coordinator pushes a blank-slate config over LoRa with `target_uid` set so only the matching board accepts it. The leaf applies the config and reboots. ~30 seconds later it shows up in the normal Fleet view at its new `unit_id`.
4. Open the **Config Builder** to fill in channels, scenes, PIRs, etc.

Coordinator units **refuse** the long-press (it would orphan the fleet). Hold time on a coord shows the same LED feedback but exits without resetting at the 5 s mark.

> If no reset button is wired, connect via USB and delete `config.json` manually using Thonny or `mpremote` — the board then boots in safe mode and you re-upload a complete config.
