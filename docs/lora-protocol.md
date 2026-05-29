# Lokki LoRa Protocol

**Version:** 1.0-draft  
**Date:** 2026-04-18  
**Status:** Design — pending review

---

## 1. Hardware Layer (E220-900T22D)

### Operating Modes
The E220 is controlled via M0/M1 GPIO pins:

| M0 | M1 | Mode | Used for |
|----|-----|------|----------|
| 0 | 0 | Normal — fixed-point transmission | All runtime messaging |
| 1 | 1 | Sleep / register-mode config | Initial module configuration only |

Firmware configures the E220 on boot via the **register-mode binary protocol** (NOT AT commands — those are for Reyax RYLR modules; sending them to a real EBYTE E220 is a silent no-op that leaves the module in factory defaults). Once registers are written, the module is switched to normal mode for all runtime traffic.

### Register-Mode Configuration

In sleep mode (M0=1, M1=1), the module accepts these binary commands over the UART:

| Bytes | Meaning |
|---|---|
| `C0 <reg> <len> <values...>` | Write `len` registers starting at `<reg>`, persist to NVRAM |
| `C2 <reg> <len> <values...>` | Same, but volatile (lost on power cycle) |
| `C1 <reg> <len>` | Read `len` registers starting at `<reg>` |

The module replies with `C1 <reg> <len> <values>` (echo back the current contents).

**Register layout** (per E220-900T22D datasheet):

| Reg | Field | Bits | Meaning |
|---|---|---|---|
| 0x00 | ADDH | 7-0 | High byte of unit address |
| 0x01 | ADDL | 7-0 | Low byte of unit address |
| 0x02 | NETID | 7-0 | Network ID (peers must match; we use 0) |
| 0x03 | REG0 | 7-5 | UART baud rate (0b011 = 9600) |
| | | 4-3 | Parity (0b00 = 8N1) |
| | | 2-0 | Air data rate (0b010 = 2.4 kbps — longest range) |
| 0x04 | REG1 | 7-6 | Sub-packet size (0b00 = 200 B) |
| | | 5 | Ambient RSSI enable |
| | | 1-0 | TX power (0b00 = 22 dBm, 01 = 17 dBm, 10 = 13, 11 = 10) |
| 0x05 | REG2 | 7-0 | Channel — frequency = 850.125 + REG2 MHz, range 0..80 |
| 0x06 | REG3 | 7 | **RSSI byte append** (1 = trailing RSSI byte on every received packet) |
| | | 6 | **Transmission method** (1 = fixed-point — required for our addressing) |

The firmware writes registers 0x00..0x06 in a single `C0 00 07 <values>` command on boot. Frequency-to-channel conversion: `channel = round(frequency_mhz - 850)`. The project default is **channel 73 ≈ 923.125 MHz** with crypt key **0x0793**; every unit in the fleet must share both, or modules silently drop each other's frames.

### RSSI Byte Append

With REG3 bit 7 set, every received packet has a single trailing byte appended by the module before delivery to the MCU UART:

```
RSSI_dBm = -(256 - rssi_byte)
```

`lora_transport.recv()` strips this byte and stores the decoded value on `lora_transport.last_rssi_dbm`. The protocol layer surfaces it as `lora_protocol.last_rx_rssi`. The coordinator's fleet manager records the locally-measured value per-frame so the dashboard can show a per-leaf signal indicator.

### Fixed-Point Transmission
In normal mode, each transmitted packet is prefixed with a 3-byte routing header that the E220 hardware handles transparently:

```
[ ADDH ][ ADDL ][ CHAN ][ payload... ]
```

The receiving E220 strips this header before passing payload to the MCU UART. This gives us hardware-level addressing for free.

### Unit Addressing
| Unit | ADDH | ADDL | Notes |
|------|------|------|-------|
| Coordinator | 0x00 | 0x00 | Always unit_id 0 |
| Leaf 1 | 0x00 | 0x01 | |
| Leaf 2 | 0x00 | 0x02 | |
| ... | ... | ... | |
| Leaf 8 | 0x00 | 0x08 | |
| Broadcast | 0xFF | 0xFF | All units on channel receive |

### Transmit Discipline
Before transmitting, firmware checks the AUX pin (GP4):
- AUX HIGH → channel clear, safe to transmit
- AUX LOW → E220 busy (transmitting or receiving), wait and retry

Retry: poll AUX every 10ms, timeout after 2 seconds, log error if timeout.

### Packet Size Limit
Maximum payload: **200 bytes** per packet at default settings.  
At 2400 bps air data rate, a 200-byte packet takes ~700ms.  
Messages larger than 200 bytes use the chunked transfer protocol (see Section 5).

`lora_protocol.send()` enforces this limit: any non-`CFG_CHUNK` message whose serialized envelope exceeds 200 bytes is dropped with an error log rather than transmitted truncated. The receive UART buffer is sized at 256 bytes to give headroom for back-to-back frames in the read window.

**UART Race Condition and Truncation Avoidance:**
Because 9600 baud serial is slower than the Pico's processing loop, `lora_transport.recv()` explicitly waits for the `AUX` pin to go `HIGH` (indicating the E220 has finished its UART transmission) before reading from the buffer. This ensures the entire packet is read at once, preventing premature RSSI byte stripping on partial chunks.
Similarly, `lora_transport.send()` waits for `AUX` to go `LOW` after writing to the UART TX buffer. This guarantees that back-to-back `send()` calls do not concatenate packets in the Pico's UART buffer (which would violate the 200B limit and cause the E220 to truncate data).

---

## 2. Message Envelope

All messages use a compact JSON envelope. Short keys keep packets small.

```json
{
  "s":   1,          // source unit_id (0–8)
  "d":   0,          // destination unit_id (0–8, or 255 for broadcast)
  "t":   "HB",       // message type (see Section 3)
  "seq": 42,         // rolling sequence number 0–255, per source
  "p":   { ... }     // payload — type-specific, may be omitted
}
```

**Sequence numbers** are per-source rolling 8-bit counters. The coordinator tracks last-seen seq per leaf to detect dropped packets. No retransmit on drop for fire-and-forget messages; ACK-required messages handle retransmit explicitly.

---

## 3. Message Types

### 3.1 `HB` — Heartbeat
**Direction:** Leaf → Coordinator  
**Frequency:** Every `heartbeat_interval_s` (default 30s)  
**ACK required:** No

Leaf reports its current output states and basic health. Coordinator uses this to drive the web UI fleet view.

```json
{
  "s": 1, "d": 0, "t": "HB", "seq": 12,
  "p": {
    "n":   "South Wing",          // unit_name (was "name")
    "up":  3600,                  // uptime seconds (was "uptime")
    "ch":  [100,80,0,0,0,0,0,0],  // LED channels duty% — positional, sorted by channel id
    "rl":  [1, 0],                // relay states (1=on, 0=off) — positional, in config order
    "pir": [0, 0, 0, 0],          // PIR states (1=motion, 0=vacant) — positional, in config order
    "ldr": 42,                    // LDR ambient reading 0–100% (optional; first to be dropped by HB fitter)
    "r":   -78,                   // dBm of the last LoRa packet THIS leaf received (was "rssi"; optional)
    "tc":  31.5                   // DS3231 die temperature in °C (was "rtc_t"; optional, omitted if RTC absent)
  }
}
```

Two fields are sent **only when meaningful** so HB stays small under the 200 B packet limit:

  * `uid` — included only when `s == 99` (i.e. an unclaimed leaf). Claimed leaves are already disambiguated by the envelope's `s` (unit_id), so sending the chip UID every HB is wasted bytes. Operators who want the chip UID for a claimed leaf can request it via SRP.
  * `err` — included only when the leaf's local error count is non-zero. Absence ⇒ zero. Lets a freshly-rebooted leaf signal "no errors" without burning ~11 bytes per HB on the healthy-steady-state case.

**Note on positional lists:** `ch`, `rl`, `pir` are fixed-length positional arrays. Index `i` always corresponds to integer id `i+1` (channels: 8-slot `ch` for ids 1..8; relays: 2-slot `rl` for ids 1..2; pirs: 4-slot `pir` for ids 1..4). Disabled or unconfigured slots stay at 0. There are no gaps — the position alone identifies the output.

**HB fitter** (see `main._fit_hb`): when a long `n` plus all optional fields would exceed the budget, the fitter drops in this order — `tc` → `r` → `ldr` → truncate `n` (keeping ≥4 chars). Losing a diagnostic byte beats dropping the whole packet at the sender.

**Internal/API naming.** `fleet_manager._fill` maps the wire keys above onto longer, human-readable keys (`name`, `uptime`, `rssi`, `rtc_t`, etc.) for storage and the `/api/fleet` response. The dashboard reads those long names — only the LoRa wire format is short.

---

### 3.2 `TS` — Time Sync
**Direction:** Coordinator → Broadcast
**Frequency:** On boot (if NTP/RTC succeeded before LoRa came up), again immediately when LoRa-deferred-retry brings the radio online with valid wall-clock, then every 1 h. Also fired on-demand in response to a `TS_REQ`.
**ACK required:** No

Coordinator broadcasts current epoch time. All leaves update their DS3231 AND their MCU's internal clock.

```json
{
  "s": 0, "d": 255, "t": "TS", "seq": 1,
  "p": {
    "epoch": 1745000000,      // Unix timestamp (UTC)
    "tz":    5.5              // UTC offset hours (matches config timezone)
  }
}
```

The coord refuses to broadcast TS while its own `system_status.time_synced` is False — that would push a bogus boot-uptime epoch out and corrupt every leaf that was actually OK. While unsynced, `time_sync_task` retries NTP every 60 s and broadcasts nothing.

---

### 3.2b `TS_REQ` — Time Sync Request
**Direction:** Leaf → Coordinator
**Trigger:** Leaf has been up ~90 s without a usable wall-clock (typically: dead DS3231 backup battery + missed the coord's boot-time TS).
**ACK required:** No

```json
{
  "s": 2, "d": 0, "t": "TS_REQ", "seq": 1
}
```

No payload — the request itself is just "please broadcast a TS now". Coord responds by firing a normal `TS` broadcast if and only if its own time is synced; otherwise silently drops the request. Leaf retries every 60 s up to 5 attempts before giving up and falling back to the 1 h periodic broadcast.

This closes the worst-case race where the coord's LoRa init fails at boot, NTP succeeds (so the coord IS synced), the coord broadcasts TS (suppressed by the LoRa-not-configured gate), then LoRa-deferred-retry succeeds, and any leaves that booted in the same window are stuck in `time_waiting` until the next periodic broadcast — up to 1 h away.

---

### 3.3 `PIR` — PIR Event
**Direction:** Leaf → Coordinator
**Trigger:** PIR state transitions (vacant→motion at the moment of motion, motion→vacant after `vacancy_timeout_s` of no motion). Fired by `pir_manager._broadcast_event` alongside the local action handler — same trigger point so dashboard sees the transition at the same instant the leaf acts on it.
**ACK required:** No

```json
{
  "s": 2, "d": 0, "t": "PIR", "seq": 7,
  "p": {
    "id": 1,                  // PIR id from config (1..4)
    "state": "motion"         // "motion" | "vacant"
  }
}
```

Coord-side handler (in `_register_lora_handlers`): updates `fleet_manager.get(leaf_id)["pir"][id-1]` so the dashboard's Motion (PIR) column reflects the transition on the next `/api/fleet` poll (within 15 s), AND pushes an INFO event tagged `pir` to the event bus so the Logs view shows the motion-activity stream across the whole fleet.

Without this real-time event, the dashboard would only see PIR state via the periodic HB (every ~30 s) — meaning a motion event in the first 29 s of an HB cycle would be invisible until the next HB.

---

### 3.4 `SC` — Scene Apply
**Direction:** Coordinator → Leaf (or broadcast)  
**Trigger:** Manual from web UI, or coordinator-initiated  
**ACK required:** Yes

```json
{
  "s": 0, "d": 2, "t": "SC", "seq": 5,
  "p": {
    "scene": "night_minimal"  // scene name — must exist in leaf's config
  }
}
```

If scene name not found on target leaf, leaf replies with `ERR`.

---

### 3.5 `MO` — Manual Override
**Direction:** Coordinator → Leaf  
**Trigger:** Direct control from web UI  
**ACK required:** Yes

Sets specific outputs immediately, bypassing schedule. Optional `revert_s` auto-reverts to schedule after N seconds.

```json
{
  "s": 0, "d": 1, "t": "MO", "seq": 8,
  "p": {
    "ch":  [[1, 75], [3, 0]],   // [channel_id, duty_percent] pairs
    "rl":  [[1, 1]],               // [relay_id, state] pairs (1=on, 0=off)
    "revert_s": 3600,                   // 0 = hold indefinitely; -1 = clear all manual
    "fade_ms": 2000                     // single global fade applied to all channels
  }
}
```

**Special values for `revert_s`:**
- `0` → hold the override indefinitely (until cleared or replaced)
- `-1` → clear all manual overrides on the leaf (revert to schedule)
- `>0` → auto-revert after N seconds

---

### 3.5b `EO` — Emergency Off
**Direction:** Coordinator → Leaf (per-unit, not broadcast)  
**Trigger:** Dashboard "Emergency Off" button  
**ACK required:** Yes

Forces all of the leaf's configured LED channels and relays to 0/off via manual override. Distinct from `MO` because the coordinator doesn't know the leaf's channel/relay IDs — the leaf iterates its own config and zeroes everything.

```json
{
  "s": 0, "d": 1, "t": "EO", "seq": 14
}
```

No payload. Leaf applies `priority_arbiter.set_manual(id, 0, 0, 0)` for every output and sets the status LED to `manual_override` (purple). Use a subsequent `MO` with `revert_s = -1` to clear and resume schedule.

---

### 3.5d `RB` — Reboot
**Direction:** Coordinator → Leaf
**Trigger:** Operator clicks the reboot button on a leaf's detail page in the dashboard (`POST /api/units/{id}/reboot`).
**ACK required:** Yes

```json
{
  "s": 0, "d": 3, "t": "RB", "seq": 26
}
```

No payload. Leaf ACKs immediately (via the dispatcher's `_ACK_REQUIRED` auto-ACK path), then schedules `machine.reset()` after a 1 s grace period so the ACK reaches the coord before the radio goes silent. The coord-side endpoint waits for the ACK before returning success to the dashboard, so the operator gets immediate feedback that the reset was received.

For coordinator reboot (id=0), the dashboard hits `POST /api/reboot` directly — no LoRa involved.

---

### 3.5c `BLINK` — Flash to Identify
**Direction:** Coordinator → Leaf (typically `d = 99`, the unclaimed unit_id)
**Trigger:** Operator clicks "Flash to identify" in the claim wizard
**ACK required:** No

```json
{
  "s": 0, "d": 99, "t": "BLINK", "seq": 17,
  "p": {
    "target_uid": "A3F1C204"   // chip UID — only the matching leaf flashes
  }
}
```

Sent to `dest = 99` so every unclaimed leaf on the air receives it; only the leaf whose `_chip_uid_hex()` matches `target_uid` flashes its status LED magenta for ~3 s. Non-matching leaves silently drop the frame. Without `target_uid`, all listening leaves flash.

---

### 3.6 `SR` — Status Request
**Direction:** Coordinator → Leaf  
**Trigger:** Web UI refresh, or coordinator detects stale heartbeat  
**ACK required:** No (leaf responds with `SRP`)

```json
{
  "s": 0, "d": 3, "t": "SR", "seq": 9
}
```

---

### 3.7 `SRP` — Status Response
**Direction:** Leaf → Coordinator  
**Trigger:** In response to `SR`  
**ACK required:** No

Same payload structure as `HB` (same short wire keys, same conditional-`uid`/`err` rules) plus a `sc` field listing the leaf's configured scene names. The coordinator caches `sc` in fleet state so the dashboard's per-unit Control modal can show scene buttons without round-tripping LoRa each time.

```json
{
  "s": 3, "d": 0, "t": "SRP", "seq": 44,
  "p": {
    "n":   "South Wing",
    "up":  7200,
    "ch":  [0,0,0,0,0,0,0,0],
    "rl":  [0, 0],
    "pir": [0, 0, 0, 0],
    "ldr": 88,
    "r":   -78,
    "tc":  31.5,
    "sc":  ["evening", "security", "demo"]
  }
}
```

If the scene list would push the envelope past 200 bytes, the leaf truncates entries from the end until it fits. The dashboard sees fewer scenes; user gets all of them after the next SR with whatever scenes survived (deterministic by config order).

---

### 3.8 `ACK` — Acknowledgement
**Direction:** Any → Any  
**Trigger:** In response to any message that requires ACK (`SC`, `MO`, `EO`, `CFG_END`)

```json
{
  "s": 2, "d": 0, "t": "ACK", "seq": 5,
  "p": {
    "ack_seq": 5,             // seq of the message being acknowledged
    "ok": true,               // false if message was rejected
    "reason": ""              // human-readable error if ok=false
  }
}
```

---

### 3.9 `ERR` — Error Notification
**Direction:** Any → Coordinator  
**Trigger:** Any runtime error worth surfacing  
**ACK required:** No

```json
{
  "s": 2, "d": 0, "t": "ERR", "seq": 13,
  "p": {
    "code":  "CONFIG_LOAD",   // short error code
    "msg":   "Invalid JSON"   // human-readable detail
  }
}
```

**Standard error codes:**
| Code | Meaning |
|------|---------|
| `CONFIG_LOAD` | Failed to load or parse config.json |
| `CONFIG_INVALID` | Config loaded but failed schema validation |
| `SCENE_NOT_FOUND` | Scene name in SC message not in local config |
| `LORA_TIMEOUT` | AUX pin did not clear within timeout |
| `RTC_FAIL` | DS3231 not responding on I2C |
| `HARDWARE_FAIL` | Generic hardware initialisation failure |

---

## 4. Chunked Config Transfer

Config files may exceed the 200-byte packet limit. Chunked transfer breaks them into 150-byte chunks (leaving 50 bytes for envelope overhead).

### Message Types

### Incremental updates (`CFG_PATCH`, `CFG_START` with `target_path`)

Editing a single field on a leaf used to require pushing the entire ~3-5 KB config via the chunked CFG_START/CHUNK/END flow (~6 s on the wire). The incremental config protocol gives that path a fast lane:

  * **`CFG_PATCH`** — Coordinator → Leaf, single packet, fits in ~100 B. Payload:
    ```json
    { "path": "led_channels/2/default_duty_percent", "value": 80 }
    ```
    Path syntax is slash-separated, numeric segments index lists. Leaf walks its in-memory config to that path, sets the value, validates the merged result, atomically writes flash, ACKs, and reboots. **ACK required.** On failure the ACK carries `ok: false, reason: "APPLY_FAILED" | "BAD_PATCH", err: "..."`.

  * **`CFG_START` with `target_path`** — chunked transfer where the assembled blob is **set at a path** instead of replacing the whole config. Used by the coord's smart-dispatch when the patch payload exceeds `CFG_PATCH`'s ~140 B budget (e.g. replacing an entire `led_channels` array). The path goes in the `CFG_START` payload alongside the existing `transfer_id`, `total_chunks`, `total_bytes`, `target_uid` fields.

The coord chooses which path to take based on the encoded payload size of `{path, value}` — single packet under 140 B, chunked otherwise. Falls back to a full-config push (no `target_path`) only when explicitly told to via `POST /api/units/{id}/config`.

**Hot-apply vs reboot:** as of UX-2c, most patches hot-apply without rebooting — the leaf re-runs the relevant subsystem's `init_from_config` in place. The ACK payload carries `rebooted: true | false` so the coord (and through it the dashboard) can show "applied instantly" or "rebooting" accordingly. Boot-wired fields (`lora.*`, `hardware.*`, `system.role`/`unit_id`/`log_level`/`log_buffer_size`/`heartbeat_interval_s`/`pwm_update_interval_ms`, `wifi.*`, `notifications.*`, and `enabled`/`gpio_pin` on channels/relays/PIRs) still reboot. See `firmware/.../core/hot_apply.py` for the authoritative rules.

### `CFG_START`, `CFG_CHUNK`, `CFG_END` — full or path-targeted chunked transfer

**`CFG_START`** — Coordinator → Leaf, initiates transfer
```json
{
  "s": 0, "d": 1, "t": "CFG_START", "seq": 20,
  "p": {
    "total_chunks": 12,
    "total_bytes":  1740,
    "transfer_id":  "a3f2",          // random 4-char ID to match chunks to transfer
    "target_uid":   "A3F1C204",      // OPTIONAL — only the leaf whose chip UID matches accepts the transfer
    "target_path":  "led_channels"   // OPTIONAL — assembled blob is set at this path instead of replacing the whole config
  }
}
```

**`target_uid` behaviour (claim wizard only):** When set, leaves whose `_chip_uid_hex()` doesn't match silently drop the `CFG_START` (no transfer state created, no auto-ACK from the dispatcher because the handler returns before that point). Subsequent `CFG_CHUNK`s are dropped because they reference an unknown `transfer_id`. The `CFG_END` from non-target leaves is suppressed when `unit_id == 99` so the coord doesn't see racing ACKs from multiple unclaimed boards on the bench. Without `target_uid`, the transfer behaves exactly as before.

**`target_path` behaviour (incremental config protocol):** When set, the assembled UTF-8 string is parsed as JSON and SET at that path in the leaf's current in-memory config (rather than replacing the whole config). Used for section-level updates that don't fit `CFG_PATCH`'s single-packet budget — e.g. replacing the entire `led_channels` array. Same path syntax as `CFG_PATCH`. Without `target_path`, the assembled string IS the new full config (existing behaviour).

**`CFG_CHUNK`** — Coordinator → Leaf, one chunk
```json
{
  "s": 0, "d": 1, "t": "CFG_CHUNK", "seq": 21,
  "p": {
    "transfer_id": "a3f2",
    "chunk_index": 0,         // 0-based
    "data": "{ \"version\": \"1.0\", ..."   // raw config text slice
  }
}
```

**`CFG_END`** — Coordinator → Leaf, signals transfer complete
```json
{
  "s": 0, "d": 1, "t": "CFG_END", "seq": 33,
  "p": {
    "transfer_id": "a3f2",
    "checksum": "d4e1f2a3"    // CRC32 hex of full config string
  }
}
```

Leaf validates checksum. On match → saves config.json, applies, sends ACK ok=true.  
On mismatch → discards, sends ACK ok=false reason="CHECKSUM_FAIL".  
Coordinator retries full transfer on failure.

### Transfer Timing
- Inter-chunk delay: 200ms (allow leaf UART buffer to clear)
- Leaf timeout: if no chunk received for 30s during transfer → abandon, send ERR
- Coordinator timeout: if no ACK within 60s of CFG_END → retry (max 3 attempts)

---

## 5. Timing and Cadence Summary

| Message | Interval / Trigger | Direction | ACK |
|---------|-------------------|-----------|-----|
| `HB` | Every 30s | Leaf → Coordinator | No |
| `TS` | Boot + every 24h | Coordinator → Broadcast | No |
| `PIR` | On state change | Leaf → Coordinator | No |
| `SC` | On demand | Coordinator → Leaf | Yes |
| `MO` | On demand | Coordinator → Leaf | Yes |
| `EO` | Emergency Off button | Coordinator → Leaf | Yes |
| `BLINK` | Claim-wizard "Flash to identify" | Coordinator → Leaf(99) | No |
| `CFG_PATCH` | Incremental single-field config update | Coordinator → Leaf | Yes |
| `TS_REQ` | Leaf asking coord for an immediate TS broadcast | Leaf → Coord | No |
| `RB` | Reboot — coord asks leaf to machine.reset() | Coordinator → Leaf | Yes |
| `SR` | On demand | Coordinator → Leaf | No |
| `SRP` | Response to SR | Leaf → Coordinator | No |
| `ACK` | Response to SC/MO/CFG | Any | — |
| `ERR` | On error | Any → Coordinator | No |
| `CFG_*` | On demand | Coordinator → Leaf | Yes (CFG_END) |

---

## 6. Collision Avoidance

The E220 AUX pin indicates channel busy state. Firmware must:

1. Check AUX HIGH before any transmit
2. If AUX LOW: wait 10ms, retry up to 200 times (2s total)
3. If still LOW after timeout: log `LORA_TIMEOUT`, skip transmit for this cycle
4. For heartbeats: add `unit_id × 500ms` jitter on top of interval to spread leaf traffic

Example for 4 leaves with 30s interval and jitter:
```
Leaf 1: transmits at 30s, 60s, 90s ...        (offset 0.5s)
Leaf 2: transmits at 31s, 61s, 91s ...        (offset 1.0s)
Leaf 3: transmits at 31.5s, 61.5s, 91.5s ... (offset 1.5s)
Leaf 4: transmits at 32s, 62s, 92s ...        (offset 2.0s)
```

---

## 7. Failure Handling

| Scenario | Behaviour |
|----------|-----------|
| Leaf misses coordinator TS | Continues on local DS3231. Resync on next TS received. |
| Coordinator misses leaf HB | Marks leaf `offline` after `heartbeat_timeout_s`. Shown in web UI. |
| SC/MO not ACKed in 10s | Coordinator retries once. On second failure logs error, marks command failed in UI. |
| CFG transfer checksum fail | Coordinator retries full transfer up to 3 times. Reports failure in UI. |
| LoRa AUX timeout | Logs error, skips this transmit cycle. Does not halt other firmware tasks. |
| E220 unresponsive on boot | Logs `HARDWARE_FAIL`, unit continues operating on local schedule with no LoRa. |
