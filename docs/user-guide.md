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
| Green + blue flash | Green solid, brief blue flash every ~4 s | Running normally with LoRa active — healthy operating state |
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

### Updating a leaf unit's config

Leaf units don't have WiFi. Options:

- **USB** — connect with Thonny or `mpremote`, overwrite `config.json` on the device, reboot
- **Future: remote push** — OTA config push over LoRa is on the roadmap (Phase 2)

---

## Fleet Dashboard

The coordinator's web dashboard (served over WiFi) shows:

- All leaf units and their online/offline status
- Per-channel LED brightness and relay states
- PIR motion state per sensor
- LDR ambient light reading
- Uptime and error count per unit

Access it at `http://<coordinator-ip>/` — check your router's DHCP table for the IP, or use `http://lokki-<unitname>.local/` if mDNS is working on your network.

---

## Troubleshooting

### Coordinator won't connect to WiFi

- Confirm the SSID and password in `config.json` are correct (case-sensitive)
- Check the LED: blue blinking = trying to connect, red blinking = failed
- The coordinator falls back to RTC time if NTP fails — it will still run the schedule, but sunrise/sunset times may drift if the RTC battery is flat

### A leaf shows Offline in the dashboard

- Check the leaf's status LED — green means it's running; red blinking means it crashed
- Confirm the leaf's `unit_id` in its `config.json` matches what the coordinator lists in its `peers` list
- Check LoRa settings — both units must have the same `frequency_mhz` and `channel` in their configs
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
| `lora.frequency_mhz` | `lora` | Must match across all units on the network |

---

## Factory Reset

Hold the reset button (GPIO pin configured in `hardware.reset_btn_pin`) for 5 seconds. This clears `config.json` from the device and reboots into safe mode. Re-upload a fresh config to restore operation.

> If no reset button is wired, connect via USB and delete `config.json` manually using Thonny or `mpremote`.
