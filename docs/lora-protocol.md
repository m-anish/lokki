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

The firmware writes registers 0x00..0x06 in a single `C0 00 07 <values>` command on boot. Frequency-to-channel conversion: `channel = round(frequency_mhz - 850)`. So 868 MHz → channel 18 → effective 868.125 MHz.

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
  "s":   1,                // source unit_id (0–8)
  "d":   0,                // destination unit_id (0–8, or 255 for broadcast)
  "t":   "HB",             // message type (see Section 3)
  "seq": 42,               // rolling sequence number 0–255, per source
  "p":   { ... },          // payload — type-specific, may be omitted
  "mac": "5d880ad7452b9a02" // 8-byte HMAC-SHA256 tag (see §2.1), 16 hex chars
}
```

**Sequence numbers** are per-source rolling 8-bit counters. The coordinator and every leaf track the last *authenticated* seq per source and reject anything ≤ last-seen (with a small rollover window — see §2.2). This is replay protection, not delivery accounting; ACK-required messages handle retransmit explicitly.

### 2.1 Authentication — network HMAC key

Lokki defends against a stray/malicious LoRa node in radio range using a **network-wide pre-shared key**. The threat is real: anyone with a Pico + E220 module on the same `frequency_mhz`/`channel` can otherwise sniff every frame and forge commands (EO blackouts, malicious CFG_* pushes, scene applies, etc.).

- The key is 16 random bytes, generated once during coordinator provisioning by `utils/update.sh --fresh --role=coordinator`.
- It lives in `/secrets.json` on every unit:
  ```json
  { "lora_key_hex": "<32 hex chars>" }
  ```
- `secrets.json` is **never** served by the web server (explicitly blocked in `_BLOCKED_PATHS`) and is **never** included in `/api/config` responses. There is no API to read it back. The only way to extract it is direct filesystem access via USB.
- Every outbound frame is signed: the sender JSON-serialises the envelope *without* the `mac` field, computes `HMAC-SHA256(key, body)`, takes the first 8 bytes, hex-encodes it, and inserts it as `mac`.
- Every inbound frame is verified: the receiver pops `mac`, re-serialises the rest the same way, recomputes the tag, and constant-time-compares. Mismatch → silent drop. No error reply (a probing attacker should learn nothing).
- Frames *without* a `mac` field are dropped if the receiver has a key configured. This means a network with a key won't accept legacy unsigned frames — every unit must have the same key.

**Why HMAC-SHA256 truncated to 8 bytes?**
- 8 bytes = 64 bits → ~10⁻¹⁹ forgery probability per attempt. Adequate against any realistic attacker on a slow LoRa link.
- Hex-encoded the field is 16 chars; with the JSON key, comma, and quotes, ~22 B of envelope overhead. Every other budget (200 B packet limit, SRP truncation thresholds) accounts for this.

**Failure mode if `secrets.json` is missing:**
- `LoRaProtocol.init()` logs a `WARN` and runs in unsigned mode.
- A unit in unsigned mode will accept ANY frame that parses, AND its outbound frames will be rejected by every signed peer.
- This is intentional — it lets you bring up a development board without secrets, but a production deployment without secrets is loudly visible in the logs.

**Key rotation:** out of scope for v1. The `CFG_*` chunked transfer can carry a new `secrets.json` to each leaf, but coordinated cut-over needs care. Captured as a follow-up in TODO.

### 2.2 Replay protection

The 8-bit `seq` counter rolls every 256 frames. To distinguish "fresh frame after rollover" from "real replay attack", receivers track per-source `last_seq` and accept new `seq` if either:

- `seq > last_seq` (normal forward progress), OR
- `last_seq ≥ 240` AND `seq < 16` (legitimate rollover window).

Anything else is dropped. The window is 16 frames: small enough that an attacker can't easily land a replay, big enough to tolerate occasional packet loss around the rollover boundary.

After a coord or leaf reboot, `last_seq` resets to "unknown" and the *first* frame from each source is accepted unconditionally. This is a documented gap — a captured frame replayed within a short window after a reboot can land. Mitigation: the attacker has to *know* the reboot happened, and the worst they can do is replay one specific MO/SC. Adding monotonic timestamps to defeat this is on the roadmap.

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
    "name":   "South Wing",   // leaf's unit_name (lets coordinator label fleet without a separate config push)
    "uptime": 3600,           // seconds since boot
    "ch":  [100,80,0,0,0,0,0,0],  // LED channels duty% — positional, sorted by channel id
    "rl":  [1, 0],            // relay states (1=on, 0=off) — positional, in config order
    "pir": [0, 0, 0, 0],      // PIR states (1=motion, 0=vacant) — positional, in config order
    "ldr": 42,                // LDR ambient reading 0–100%
    "err": 0,                 // error count since last heartbeat
    "rssi": -78               // dBm of the last LoRa packet THIS leaf received (or null until E220 RSSI append is enabled)
  }
}
```

**Note on positional lists:** `ch`, `rl`, `pir` are positional arrays, ordered by the unit's local config. The coordinator displays them by index — if a leaf's config has gaps (e.g. only `pir2` enabled), the array element is still at its config-order position, not at index `2`. Keep configs dense (no gaps) for predictable dashboard rendering.

---

### 3.2 `TS` — Time Sync
**Direction:** Coordinator → Broadcast  
**Frequency:** On boot, then every 24h after NTP sync  
**ACK required:** No

Coordinator broadcasts current epoch time. All leaves update their DS3231.

```json
{
  "s": 0, "d": 255, "t": "TS", "seq": 1,
  "p": {
    "epoch": 1745000000,      // Unix timestamp (UTC)
    "tz":    5.5              // UTC offset hours (matches config timezone)
  }
}
```

---

### 3.3 `PIR` — PIR Event
**Direction:** Leaf → Coordinator  
**Trigger:** On PIR state change (motion detected or vacancy)  
**ACK required:** No

```json
{
  "s": 2, "d": 0, "t": "PIR", "seq": 7,
  "p": {
    "id":    "pir1",          // PIR id from config
    "state": "motion"         // "motion" | "vacancy"
  }
}
```

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
    "ch":  [["ch1", 75], ["ch3", 0]],   // [channel_id, duty_percent] pairs
    "rl":  [["rly1", 1]],               // [relay_id, state] pairs (1=on, 0=off)
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

Same payload structure as `HB` plus a `sc` field listing the leaf's configured scene names. The coordinator caches `sc` in fleet state so the dashboard's per-unit Control modal can show scene buttons without round-tripping LoRa each time.

```json
{
  "s": 3, "d": 0, "t": "SRP", "seq": 44,
  "p": {
    "name":   "South Wing",
    "uptime": 7200,
    "ch":  [0,0,0,0,0,0,0,0],
    "rl":  [0, 0],
    "pir": [0, 0, 0, 0],
    "ldr": 88,
    "err": 0,
    "rssi": -78,
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

**`CFG_START`** — Coordinator → Leaf, initiates transfer
```json
{
  "s": 0, "d": 1, "t": "CFG_START", "seq": 20,
  "p": {
    "total_chunks": 12,
    "total_bytes": 1740,
    "transfer_id": "a3f2"     // random 4-char ID to match chunks to transfer
  }
}
```

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
