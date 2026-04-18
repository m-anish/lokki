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
| 1 | 1 | Sleep / AT config | Initial module configuration only |

Firmware configures the E220 on boot via AT commands (sleep mode), then switches to normal mode for all runtime operation.

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
    "up":  3600,              // uptime seconds
    "ch":  [100,80,0,0,0,0,0,0],  // LED channels 1–8 duty% (current actual)
    "rl":  [1, 0],            // relays 1–2 state (1=on, 0=off)
    "pir": [0, 0, 0, 0],      // PIR states (1=motion, 0=vacant)
    "ldr": 42,                // LDR ambient reading 0–100%
    "err": 0                  // error count since last heartbeat
  }
}
```

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
    "ch":  [{"id": "ch1", "duty": 75, "fade_ms": 2000}],
    "rl":  [{"id": "rly1", "state": "on"}],
    "revert_s": 3600          // 0 = hold indefinitely until next manual or reboot
  }
}
```

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

Same payload structure as `HB` but sent on demand rather than on interval.

```json
{
  "s": 3, "d": 0, "t": "SRP", "seq": 44,
  "p": {
    "up":  7200,
    "ch":  [0,0,0,0,0,0,0,0],
    "rl":  [0, 0],
    "pir": [0, 0, 0, 0],
    "ldr": 88,
    "err": 0
  }
}
```

---

### 3.8 `ACK` — Acknowledgement
**Direction:** Any → Any  
**Trigger:** In response to any message that requires ACK (`SC`, `MO`, `CFG_*`)

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
