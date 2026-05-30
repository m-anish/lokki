# Lokki — Hardware Test Scenarios

Catalog of bench-test scenarios for verifying Lokki firmware on real Pico 2 W
hardware. There is no host-side test suite — everything in this file is run by
hand against a powered-up coord and/or leaf, watching the REPL, the status LED,
and the dashboard. The standalone scripts in `tests/*.py` cover the LoRa
link specifically; this file covers everything else.

## How to use this file

- **As a runbook**: pick a category, work down the scenarios, mark what
  passes/fails in your own notes (or file a GitHub issue if something is
  genuinely broken).
- **As a contract**: when you add a feature, add a scenario for it here.
  When a regression bites, codify the repro as a scenario so it doesn't
  recur silently.
- **Last-verified is best-effort.** Update it when you run a scenario
  end-to-end against current `main`. Stale dates aren't catastrophic but
  do indicate "nobody's looked at this in a while."

Each scenario has the same shape:

> **Setup** — starting hardware/config state.
> **Steps** — what the operator does, in order.
> **Expected** — what you should observe (LED, log lines, dashboard).
> **Last verified** — commit hash + date, or "not yet."

If a step needs a specific commit or feature flag, call it out.

---

## 1. Boot & Bring-up

### 1.1 Cold boot, sane config
**Setup**: A coord with a valid `config.json` that already has WiFi credentials
and a populated DS3231 battery.
**Steps**: Power on. Watch REPL + LED through boot.
**Expected**:
- LED: `booting` (white pulse) → `wifi_connecting` (blue blink) → `lora_init`
  (cyan solid) → `running_lora_ok`/`running_ok` (green solid).
- Logs: `[MAIN] WiFi connected`, `[NTP] Synced with ...`, no errors.
- Dashboard reachable at `<hostname>.local` within ~15 s of boot.
**Last verified**: not yet.

### 1.2 Boot with corrupt config.json
**Setup**: Flash a coord. SSH in via REPL and write garbage into `/config.json`,
then reboot.
**Steps**: Power-cycle, observe.
**Expected**:
- `config_manager._safe_mode_reason` is non-empty; `main.py` enters `safe_mode()`.
- All channels and relays held off. Status LED `error` (red blink).
- No crash loop; coord stays up so operator can re-flash config.
**Last verified**: not yet.

### 1.3 Time-sync gate (no clock available)
**Setup**: Coord with `timezone.ntp_enabled=false` and a fresh-from-factory
DS3231 (no time written). No prior config.
**Steps**: Power on. Don't manually set time.
**Expected**:
- LED stays on `time_waiting` (slow cyan pulse) indefinitely.
- `schedule_task` short-circuits; outputs sit at `default_state` for relays
  and `default_duty_percent` for channels.
- "Waiting for time sync" banner appears on the dashboard.
- Clicking "⏱ Set time from this browser" unblocks the schedule.
**Last verified**: not yet.

### 1.4 LoRa fails at boot, deferred retry succeeds
**Setup**: A coord where the E220's LM2596 supply is slow to stabilise (this
is the documented flake — see `main.py` boot comment).
**Steps**: Power on, watch REPL for ~120 s.
**Expected**:
- First LoRa init returns failure (`[LORA] init failed` or similar).
- Status LED shows `lora_recovering` (slow red pulse).
- Boot continues — `wifi_connecting`, web server, etc. all come up.
- Deferred retry fires at ~100 s; if it succeeds, LED transitions to
  `running_lora_ok` (green solid) and `lora_protocol.config_ok` becomes True.
- If three retries all fail: LED settles at `lora_disabled` (purple solid).
**Last verified**: not yet.

---

## 2. WiFi / AP Fallback

### 2.1 Boot with no SSID configured (fresh install → AP mode)
**Setup**: Flash coord with `./utils/update.sh --fresh --role=coordinator` (no
`--wifi` flag). `config.json` has `wifi.ssid = ""` (placeholder).
**Steps**: Power on, watch.
**Expected**:
- Log: `[MAIN] No wifi.ssid configured — coming up in SoftAP mode`.
- AP comes up; log: `[AP] Up: SSID='Lokki-Setup' IP=192.168.4.1`.
- LED settles at `ap_mode` (magenta pulse) once time is sane.
- `Lokki-Setup` SSID visible from a phone. Joining with password
  `lokki-setup-1234` works.
- Dashboard reachable at `http://lokki.local/` and `http://192.168.4.1/`.
- Dashboard shows the **SoftAP fallback** banner with IP fallback link.
**Last verified**: not yet.

### 2.2 First-time setup wizard via AP
**Setup**: Continue from 2.1 — coord is in AP mode, phone joined.
**Steps**:
1. Open dashboard.
2. Click `⚙️ Configure` on the coord.
3. Open **Advanced** tab → **WiFi** section.
4. Enter real venue SSID + password, save.
5. Coord reboots.
**Expected**:
- Save fires a PATCH on `wifi`. Response indicates `rebooted=True` (wifi
  changes are not hot-applied).
- Coord reboots within ~3 s.
- On next boot, STA connects to the new network. AP comes down 60 s later
  (stability window).
- LED goes green (`running_lora_ok` if a leaf has been heard).
**Last verified**: not yet.

### 2.3 Mid-runtime WiFi drop → recovery (the original bug)
**Setup**: Coord running normally on STA. Dashboard open.
**Steps**:
1. Power-cycle the venue AP (or block coord on the AP's MAC filter).
2. Watch LED + log + dashboard reachability.
3. Restore the AP after ~3 minutes.
**Expected**:
- Within ~5 s of disconnect: LED flips to `wifi_disconnected` (amber blink).
  Log: `[WIFI-MON] STA lost — entering reconnect loop`.
- After 3 failed reconnect attempts (~90 s): AP rises. LED `ap_mode`
  (magenta pulse). Log: `bringing up SoftAP fallback`.
- Once STA returns: log: `[WIFI-MON] STA recovered (IP …)`. MQTT and NTP
  resync. LED returns to green within ~10 s.
- 60 s after STA-stable: AP comes down. Log: `tearing down SoftAP fallback`.
**Last verified**: not yet.

### 2.4 Reset button responsive during WiFi outage
**Setup**: Coord in AP-fallback mode (continue from 2.3 mid-outage state).
**Steps**: Press the reset button (≥200 ms).
**Expected**:
- LED responds within ~250 ms with `reset_armed` (amber solid).
- Release within 2 s → soft_reset commits.
- **Regression guard**: prior to commit `31c8881` the reset button froze
  during WiFi outages because `ntptime.settime()` blocked the asyncio loop.
  This must not recur.
**Last verified**: not yet.

### 2.5 AP refusal on short password
**Setup**: Coord config with `wifi.ap_password = "short"` (<8 chars).
**Steps**: Trigger AP bring-up (boot with no STA, or fail STA 3×).
**Expected**:
- Log: `[AP] wifi.ap_password is N chars; need >=8 for WPA2. AP mode disabled`.
- AP does NOT come up. LED stays at `wifi_disconnected`.
- Dashboard remains unreachable until the operator fixes the config (via
  REPL — that's the only access path in this failure mode).
**Last verified**: not yet.

---

## 3. LoRa Link

### 3.1 Heartbeat round-trip
**Setup**: One coord + one claimed leaf, both healthy.
**Steps**: Watch REPL on both. Default HB interval is 30 s.
**Expected**:
- Leaf logs `[LORA] HB sent` every 30 s.
- Coord logs `[LORA] HB recv from unit N`.
- Coord LED briefly flashes blue on each HB (event flash via
  `flash_event`). Same on leaf when it receives downlink traffic.
- Dashboard's leaf card shows last-seen "now" or "Xs ago", uptime
  incrementing, RSSI populated.
**Last verified**: not yet.

### 3.2 Heartbeat timeout → leaf-offline
**Setup**: Coord + leaf running. Leaf is connected.
**Steps**: Power off the leaf. Wait `heartbeat_timeout_s` (default 120 s).
**Expected**:
- Coord logs `[FLEET] Unit N marked offline` (or equivalent) at the timeout.
- Coord LED switches to `leaf_offline` (orange solid).
- Dashboard fleet card for that leaf shows red dot / "Offline".
- Power leaf back on: within one HB cycle, it shows online again and LED
  returns to `running_lora_ok`.
**Last verified**: not yet.

### 3.3 Config push, single packet (CFG_PATCH)
**Setup**: Coord + leaf online. Dashboard open on the leaf detail page.
**Steps**:
1. Channels tab → edit one channel's name (small change, fits in 200 B).
2. Click Save. Watch dashboard status + leaf REPL.
**Expected**:
- Status flips to "Saving…" then `Saved ✓ (applied instantly)` within
  ~3 s (no reboot needed; hot-applied via `hot_apply.py`).
- Leaf logs `[LORA] CFG_PATCH applied` and `[ARBITER] schedule re-initialised`.
- Dashboard channel name updates on the fleet card within ~1 s.
**Last verified**: not yet.

### 3.4 Config push, chunked (CFG_SECTION)
**Setup**: Coord + leaf online.
**Steps**:
1. Channels tab → edit a channel that has many `time_windows` (or the
   `system` section with multiple fields), exceeding the 140 B single-packet
   budget.
2. Save. Watch progress.
**Expected**:
- Dashboard shows chunked-transfer progress bar (`Patching… ~N%`).
- Leaf logs CFG_START, multiple CFG_CHUNK, then CFG_END with checksum match.
- If a chunk is dropped on the wire: leaf NAKs with `missing` indices; coord
  retransmits only those; progress bar holds at peak (doesn't regress).
- On success: leaf hot-applies if path allows, else reboots.
- Dashboard reports correct `rebooted: True/False` (see commit `c3a0cb7` —
  prior to that, chunked transfers always reported `rebooted=True`).
**Last verified**: not yet.

### 3.5 Claim wizard (unclaimed leaf → bound)
**Setup**: A factory-reset leaf (`./utils/update.sh --fresh --role=leaf --id=99`).
Coord already running.
**Steps**:
1. Power leaf. Wait ~30 s for it to HB the coord.
2. Dashboard shows leaf under "New Devices" section.
3. Click **Claim…**, optionally click **Blink** to verify which leaf it is,
   pick a unit_id (1..8), give a name, submit.
**Expected**:
- "Blink" briefly flashes leaf's LED white. Operator can identify which
  physical unit on the bench.
- Submit pushes a full config via chunked transfer with `target_uid` set to
  the chip UID — only that specific leaf accepts the config (other 99-unit
  leaves nearby ignore it).
- Leaf reboots into the new unit_id.
- Dashboard fleet view shows it under the chosen unit_id within ~60 s.
**Last verified**: not yet.

---

## 4. Schedule & Priority Arbiter

### 4.1 Time-window activation
**Setup**: Leaf with one LED channel + a window `05:00–07:00 @ 60% fade
5000ms`. Clock is synced.
**Steps**: Cross the 05:00 boundary (or operator-override the clock).
**Expected**:
- At 04:59:59 → channel at `default_duty_percent`.
- At 05:00:00 → channel ramps to 60% over 5 s.
- Schedule strip on the Channels tab shows the active window highlighted.
**Last verified**: not yet.

### 4.2 Cross-midnight window
**Setup**: Channel with `{start: "22:00", end: "05:00", duty: 30}`.
**Steps**: Observe behavior at 21:59 / 22:01 / 04:59 / 05:01.
**Expected**:
- Window is active for `current_minutes >= 22:00` OR `current_minutes < 05:00`
  (see `_window_active` in `schedule_engine.py`).
- Schedule strip renders as two visual segments on either side of midnight.
**Last verified**: not yet.

### 4.3 Sunrise/sunset edges
**Setup**: Channel with `{start: "sunset", end: "21:30"}`.
**Steps**: Let the day pass; observe activation at actual sunset
(per `sun_times.get_sunrise_sunset`).
**Expected**:
- Window activates at the per-date sunset minute (varies by month).
- Schedule strip renders the segment with a dashed border (approximate
  placement — visualizer uses fixed 18:00 as default position).
**Last verified**: not yet.

### 4.4 First-match-wins on overlap
**Setup**: Channel with two windows in this order:
1. `{start: "18:00", end: "22:00", duty: 50}`
2. `{start: "20:00", end: "21:00", duty: 100}`
Both cover 20:00–21:00.
**Steps**: At 20:30, observe the channel.
**Expected**:
- Channel sits at **50%** (window 1 wins; first match in array order).
- Reorder via dashboard's ↑/↓ buttons so window 2 is first → channel jumps
  to 100% within one schedule tick. **No auto-sort** ever.
**Last verified**: not yet.

### 4.5 LDR cap on schedule layer only
**Setup**: Channel scheduled at 80%. LDR cap rule `{above_percent: 70,
cap_percent: 20}`. Direct sun on the LDR (>70% ambient).
**Steps**: Observe. Then manually override the channel to 90% via the
Channels tab Manual Override slider.
**Expected**:
- With only schedule active: channel is capped at 20% (LDR cap applies).
- With manual override: channel is at 90% — LDR cap bypassed (manual >
  schedule, cap only modifies schedule layer per `priority_arbiter._resolve_channel`).
**Last verified**: not yet.

### 4.6 Priority manual > pir > schedule > default
**Setup**: Channel enabled, schedule at 50%, PIR action `set_led_channels:
[{id: N, duty: 100}]`.
**Steps**:
1. Observe default state.
2. Trigger PIR motion.
3. While PIR active, set manual override to 30%.
4. Clear manual.
5. Wait for PIR vacancy_timeout.
**Expected**:
1. Channel at 50% (schedule).
2. Channel jumps to 100% (PIR wins).
3. Channel drops to 30% (manual wins).
4. Channel returns to 100% (PIR still active).
5. Channel returns to 50% (PIR cleared, schedule resumes).
**Last verified**: not yet.

---

## 5. PIR & LDR

### 5.1 PIR motion → set_scene action
**Setup**: PIR sensor with `on_motion: {action: "set_scene", scene_name:
"motion_active"}`. Scene "motion_active" defined in config.
**Steps**: Wave hand in front of the PIR.
**Expected**:
- Log: `[PIR] pir1 on_motion → set_scene('motion_active')`.
- Channels/relays listed in the scene jump to their scene values.
- Status LED shows `manual_override` (magenta) on the leaf (scene apply
  uses the manual layer).
**Last verified**: not yet.

### 5.2 PIR vacancy → revert
**Setup**: Continue from 5.1. PIR `vacancy_timeout_s: 60`,
`on_vacancy: {action: "revert_to_schedule"}`.
**Steps**: Stop triggering motion. Wait 60 s.
**Expected**:
- Log: `[PIR] pir1 on_vacancy → revert_to_schedule`.
- Channels return to their schedule layer values within one tick.
- LED returns to running_*.
**Last verified**: not yet.

### 5.3 Sunrise/sunset compute from lat/lon
**Setup**: Coord with `config.location.lat = 32.219, lon = 76.323` (Dharamsala
defaults from the sample config). Schedule has a window `{start: "sunset",
end: "21:30"}` on at least one channel. Clock is synced.
**Steps**:
1. `GET /api/sun-times`. Note the response.
2. Open dashboard Channels tab on the coord, expand the channel with the
   sunset-start window.
3. Edit lat/lon via **Advanced → Location** to something dramatically
   different (e.g. Tromsø `69.65, 18.96`). Save.
**Expected**:
- (1) Response has `source: "compute"`, sunrise/sunset values within a
  minute of an external sunrise/sunset reference for today's date.
- (2) Schedule strip's sunset segment edge sits at the real sunset minute,
  without the dashed-approximate border.
- (3) After save, the strip re-renders. Edge moves to the new location's
  sunset time. In Arctic-circle summer the compute returns None — strip
  falls back to dashed default placement.
**Last verified**: not yet.

### 5.4 LDR cap rule activates
**Setup**: LDR enabled, `cap_rules: [{above_percent: 70, cap_percent: 20}]`.
Channel scheduled at 100%.
**Steps**: Shine a bright light at the LDR.
**Expected**:
- Within `smoothing_window_s` (default 60), `ambient_percent` rises above 70.
- Log: `[LDR] cap updated to 20%` (or similar).
- Channel duty drops from 100% to 20% (cap on the schedule layer).
- Dashboard status header shows the LDR reading + active cap.
**Last verified**: not yet.

---

## 6. Dashboard & Config Editor

### 6.1 Inline schedule editor — add window
**Setup**: Leaf detail page, Channels tab.
**Steps**:
1. Expand a channel's row.
2. Click **+ Add window** under the schedule strip.
3. Edit start/end/duty/fade.
4. Save.
**Expected**:
- Schedule strip above the editor updates live as you type.
- Save validates (HH:MM regex or `sunrise`/`sunset`; duty 0–100; fade
  ≤60000) before sending; bad input shows red highlight + status banner.
- Save succeeds, status `Saved ✓ (applied instantly)`. Channel value
  reflects the new schedule on the next tick.
**Last verified**: not yet.

### 6.2 Schedule editor — reorder for overlap
**Setup**: Channel with two overlapping windows (see 4.4).
**Steps**: Click ↑/↓ on a window row.
**Expected**:
- Window swaps with its neighbour in the editor.
- Schedule strip re-renders to reflect new order.
- After save, the active window at the overlap minute changes accordingly
  (verified by observing the channel).
**Last verified**: not yet.

### 6.3 Scene editor — apply, edit, delete
**Setup**: Coord detail page, Scenes tab. Sample scenes "motion_active" and
"all_off" present.
**Steps**:
1. Click **Apply to unit** on `motion_active`. Watch the coord's channels.
2. Edit the scene — toggle a channel's "include" checkbox.
3. Save.
4. Add a new scene via **+ Add scene**.
5. Delete it via the Delete button.
**Expected**:
- Apply broadcasts via LoRa, channels reflect scene values within ~2 s.
- Save validates name (no spaces, no duplicates) before submitting.
- Whole `scenes` array round-trips via PATCH; coord cache updates.
- Delete confirms first, then drops the card.
**Last verified**: not yet.

### 6.4 Hot-apply vs reboot decision
**Setup**: Any leaf, dashboard open.
**Steps**: Make these edits, one at a time:
1. Change a channel's `name`.
2. Change a channel's `enabled` flag.
3. Change `system.unit_name`.
4. Change `system.heartbeat_interval_s`.
5. Change `lora.channel`.
**Expected**:
- (1) Hot-applied. Status `Saved ✓ (applied instantly)`.
- (2) Reboot. Status `Saved ✓ (leaf rebooting…)`. Leaf disappears for ~30 s.
- (3) Hot-applied (chunked transfer, but no reboot — see commit `c3a0cb7`).
- (4) Hot-applied. The `heartbeat_broadcast_task` re-reads the interval
  dynamically each iteration.
- (5) Reboot. LoRa registers are programmed at boot only.
**Last verified**: not yet.

### 6.5 XSS sanitisation
**Setup**: Dashboard open.
**Steps**:
1. Rename a channel to `<script>alert('x')</script>`. Save.
2. Reload dashboard.
**Expected**:
- The literal string `<script>alert('x')</script>` is shown as text in the
  channel name field everywhere it appears (fleet card, sidebar tooltip,
  override label, edit row, schedule strip tooltip).
- No alert fires. No console error.
- Regression guard: prior to commit `c3a0cb7` this would execute.
**Last verified**: not yet.

---

## 7. Status LED

### 7.1 Every base state renders correctly
**Steps**: From the REPL on a powered unit, run:
```python
from hardware.status_led import status_led, _STATES
import time
for s in _STATES:
    print("→", s); status_led.set_state(s, force=True); time.sleep(3)
```
**Expected**:
- Each state animates with its documented colour + pattern (see `_STATES`
  in `status_led.py`).
- `wifi_disconnected` is visually distinct from `wifi_connecting` (amber
  blink vs blue blink).
- `ap_mode` is visually distinct from `lora_disabled` (magenta pulse vs
  purple solid).
**Last verified**: not yet.

### 7.2 Reset-button hold-time feedback
**Setup**: A leaf (not coord — coord refuses long-press).
**Steps**: Press and hold the reset button while watching LED.
**Expected**:
- 0–200 ms: no change.
- 200 ms: LED locks to `reset_armed` (amber solid).
- 2 s: LED escalates to `reset_warning` (red fast blink).
- 5 s: factory_reset commits regardless of release.
- Release between 200 ms–2 s: soft_reset commits.
- Release between 2 s–5 s: soft_reset commits (factory aborted).
- HB-flash / fleet-timeout-task / leaf-status-task do not override the
  hold-time feedback (LED is locked).
**Last verified**: not yet.

### 7.3 Coord refuses factory reset
**Setup**: Coord.
**Steps**: Hold reset button > 5 s.
**Expected**:
- At 5 s threshold: log `[RESET_BTN] Long-press detected on coordinator — refusing`.
- LED restores to previous state; no machine.reset() called.
**Last verified**: not yet.

---

## 8. Resilience

### 8.1 DS3231 battery dead, NTP unreachable
**Setup**: Coord with DS3231 battery removed (or not yet installed). No
internet (block at AP).
**Steps**: Power on.
**Expected**:
- `_try_seed_time_from_rtc` finds garbage time, doesn't mark synced.
- NTP fails (no internet). Doesn't mark synced.
- LED stays at `time_waiting`. Schedule paused.
- Operator can unblock via dashboard's "Set time from this browser" button.
**Last verified**: not yet.

### 8.2 MQTT broker unreachable
**Setup**: Coord config has `notifications.mqtt_enabled = true` and
`broker = "10.99.99.99"` (unreachable).
**Steps**: Power on.
**Expected**:
- `mqtt_notifier.connect()` returns False quickly (does not hang).
- Boot continues; LoRa, WiFi, web server all come up normally.
- Dashboard shows MQTT absent from connection chip; doesn't error.
- WiFi recovery does NOT re-spam the broker faster than the normal retry
  cadence.
**Last verified**: not yet.

### 8.3 LoRa AUX pin stuck high or low
**Setup**: Disconnect the E220's AUX line at boot (or short to GND/VCC).
**Steps**: Power on, observe.
**Expected**:
- LoRa init detects the bad AUX state, fails gracefully, logs an error.
- LED settles at `lora_recovering` then `lora_disabled` after retries.
- Boot continues; coord's web server is still reachable; dashboard shows
  the coord with no LoRa connectivity rather than refusing to load.
**Last verified**: not yet.

---

## 9. Adding a new scenario

When you ship a feature that has a non-trivial behaviour (a state machine,
a recovery path, an arbitration rule, a UI flow that touches multiple
endpoints), add a scenario here under the right category. Copy the
template:

```markdown
### X.Y Short title
**Setup**: starting state, hardware/config required.
**Steps**: numbered, operator-runnable.
**Expected**: what to observe (LED, log, dashboard).
**Last verified**: not yet.
```

If a scenario's expected behaviour was wrong and you fixed it — add a
**Regression guard** line citing the commit that fixed it, so future
versions don't silently re-break.

If a scenario is genuinely broken (firmware doesn't do what it should),
file a GitHub issue and link it from the `Last verified` line.
