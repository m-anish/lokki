# Lokki Config Schema

**Version:** 1.0-draft  
**Date:** 2026-04-18  
**Status:** Design — pending review

The complete `config.json` structure for a Lokki unit. All rules and triggers are local to the unit — cross-unit coordination is v2.

---

## Top-Level Structure

```json
{
  "version": "1.0",
  "system": { ... },
  "wifi": { ... },
  "lora": { ... },
  "timezone": { ... },
  "hardware": { ... },
  "ldr": { ... },
  "pir": [ ... ],
  "relays": [ ... ],
  "led_channels": [ ... ],
  "scenes": [ ... ],
  "notifications": { ... }
}
```

---

## `version`
Semantic version string. Major version mismatch on load → safe mode boot.

```json
"version": "1.0"
```

---

## `system`

```json
"system": {
  "role": "coordinator",        // "coordinator" | "leaf"
  "unit_id": 0,                 // 0 = coordinator, 1–8 = leaf
  "unit_name": "Pagoda",        // human-readable, shown in web UI
  "peers": [1, 2, 3],           // leaf unit_ids (coordinator only, empty on leaf)
  "log_level": "INFO",          // FATAL | ERROR | WARN | INFO | DEBUG
  "heartbeat_interval_s": 30,   // how often leaf sends status to coordinator
  "heartbeat_timeout_s": 120,   // coordinator marks leaf offline after this
  "pwm_update_interval_ms": 500 // how often schedule engine re-evaluates outputs
}
```

---

## `wifi`
Coordinator only. Leaf ignores this block.

```json
"wifi": {
  "ssid": "MyNetwork",
  "password": "secret",
  "hostname": "lokki-pagoda"
}
```

---

## `lora`

```json
"lora": {
  "enabled": true,
  "frequency_mhz": 868,         // confirm region — 865-867 India, 868 EU
  "air_data_rate": 2400,        // bps — lower = longer range
  "tx_power_dbm": 22,           // max for E220-900T22D
  "channel": 0                  // E220 channel (0–83 depending on variant)
}
```

---

## `timezone`

```json
"timezone": {
  "name": "IST",
  "utc_offset_hours": 5.5
}
```

---

## `hardware`
Fixed GPIO assignments. Change only if PCB revision changes pin mapping.

```json
"hardware": {
  "i2c_sda_pin": 20,
  "i2c_scl_pin": 21,
  "i2c_freq_hz": 400000,
  "pwm_freq_hz": 1000,
  "ldr_adc_pin": 26,
  "status_led_pin": 5,
  "reset_btn_pin": 12,
  "lora_uart_id": 0,
  "lora_tx_pin": 0,
  "lora_rx_pin": 1,
  "lora_m0_pin": 2,
  "lora_m1_pin": 3,
  "lora_aux_pin": 4
}
```

---

## `ldr`
LDR acts as a brightness cap — never turns lights on or off.

```json
"ldr": {
  "enabled": true,
  "smoothing_window_s": 60,     // rolling average duration
  "cap_rules": [
    {
      "above_percent": 60,      // if ambient > 60% → cap outputs at 20%
      "cap_percent": 20
    },
    {
      "above_percent": 90,      // if ambient > 90% → cap outputs at 5%
      "cap_percent": 5
    }
  ]
}
```

Multiple cap rules evaluated highest `above_percent` first. If no rule matches, no cap applied.

---

## `pir`
One entry per physical PIR input. Up to 4.

```json
"pir": [
  {
    "id": "pir1",
    "name": "Main Entrance",
    "gpio_pin": 6,
    "enabled": true,
    "vacancy_timeout_s": 300,   // revert to schedule after this many seconds of no motion
    "on_motion": {
      "action": "set_scene",
      "scene_name": "motion_active"
    },
    "on_vacancy": {
      "action": "revert_to_schedule"  // always safe to revert
    }
  },
  {
    "id": "pir2",
    "name": "Side Door",
    "gpio_pin": 7,
    "enabled": true,
    "vacancy_timeout_s": 180,
    "on_motion": {
      "action": "set_led_channels",
      "channels": ["ch1", "ch2"],
      "duty_percent": 80,
      "fade_ms": 2000
    },
    "on_vacancy": {
      "action": "revert_to_schedule"
    }
  }
]
```

**`on_motion` action types:**
- `set_scene` — apply a named scene
- `set_led_channels` — set specific channels to a duty level (with optional fade)
- `set_relay` — turn a relay on/off
- `revert_to_schedule` — hand back to schedule engine (useful for on_motion edge cases)

**`on_vacancy` action types:**
- `revert_to_schedule` — always the safe default
- `set_scene` — apply a specific "empty room" scene instead

---

## `relays`
One entry per relay. Up to 2.

```json
"relays": [
  {
    "id": "rly1",
    "name": "Main Power",
    "gpio_pin": 10,
    "enabled": true,
    "default_state": "off",     // "on" | "off" — state on boot before schedule runs
    "time_windows": [
      {
        "start": "06:00",
        "end": "22:00",
        "state": "on"
      }
    ]
  },
  {
    "id": "rly2",
    "name": "Emergency Light",
    "gpio_pin": 11,
    "enabled": false,
    "default_state": "off",
    "time_windows": []
  }
]
```

Relays follow the same time window model as LED channels but state is `"on"` / `"off"` instead of duty percent. PIR and manual overrides apply using the same priority stack.

---

## `led_channels`
One entry per LED driver channel. Up to 8.

```json
"led_channels": [
  {
    "id": "ch1",
    "name": "Altar Lights",
    "gpio_pin": 16,
    "enabled": true,
    "default_duty_percent": 0,
    "time_windows": [
      {
        "start": "sunrise",
        "end": "08:00",
        "duty_percent": 40,
        "fade_ms": 5000           // optional fade transition into this window
      },
      {
        "start": "08:00",
        "end": "sunset",
        "duty_percent": 0
      },
      {
        "start": "sunset",
        "end": "21:00",
        "duty_percent": 80,
        "fade_ms": 10000
      },
      {
        "start": "21:00",
        "end": "22:30",
        "duty_percent": 30
      }
    ]
  },
  {
    "id": "ch2",
    "name": "Corridor",
    "gpio_pin": 17,
    "enabled": true,
    "default_duty_percent": 0,
    "time_windows": [
      {
        "start": "05:30",
        "end": "sunrise",
        "duty_percent": 60
      },
      {
        "start": "21:30",
        "end": "23:00",
        "duty_percent": 20
      }
    ]
  }
]
```

**GPIO pin assignments (reference):**
| Channel | GPIO |
|---------|------|
| ch1 | GP16 |
| ch2 | GP17 |
| ch3 | GP18 |
| ch4 | GP19 |
| ch5 | GP22 |
| ch6 | GP15 |
| ch7 | GP14 |
| ch8 | GP13 |

**Time window notes:**
- `start` / `end`: `"HH:MM"`, `"sunrise"`, or `"sunset"`
- Windows are evaluated in order; first matching window wins
- Overnight windows (e.g. `"22:00"` → `"06:00"`) are supported
- `fade_ms`: optional, fade transition in milliseconds when entering window
- LDR cap applies on top of whatever duty this window sets

---

## `scenes`
Named output snapshots. Applied by PIR actions, manual API calls, or schedule rules.

```json
"scenes": [
  {
    "name": "motion_active",
    "led_channels": [
      { "id": "ch1", "duty_percent": 100, "fade_ms": 1000 },
      { "id": "ch2", "duty_percent": 80,  "fade_ms": 1000 }
    ],
    "relays": [
      { "id": "rly1", "state": "on" }
    ]
  },
  {
    "name": "night_minimal",
    "led_channels": [
      { "id": "ch1", "duty_percent": 5 },
      { "id": "ch2", "duty_percent": 10 }
    ],
    "relays": []
  },
  {
    "name": "all_off",
    "led_channels": [
      { "id": "ch1", "duty_percent": 0 },
      { "id": "ch2", "duty_percent": 0 },
      { "id": "ch3", "duty_percent": 0 },
      { "id": "ch4", "duty_percent": 0 },
      { "id": "ch5", "duty_percent": 0 },
      { "id": "ch6", "duty_percent": 0 },
      { "id": "ch7", "duty_percent": 0 },
      { "id": "ch8", "duty_percent": 0 }
    ],
    "relays": [
      { "id": "rly1", "state": "off" },
      { "id": "rly2", "state": "off" }
    ]
  }
]
```

A scene only needs to specify the channels/relays it wants to change. Unmentioned outputs are left at their current state.

---

## `notifications`
Optional MQTT push. Carried over from existing firmware, unchanged.

```json
"notifications": {
  "mqtt_enabled": false,
  "broker": "192.168.1.100",
  "port": 1883,
  "topic_prefix": "lokki/pagoda",
  "client_id": "lokki-pagoda"
}
```

---

## Complete Minimal Example

A working single-unit config with 2 LED channels, 1 relay, 1 PIR, and no scenes:

```json
{
  "version": "1.0",
  "system": {
    "role": "leaf",
    "unit_id": 1,
    "unit_name": "Cell Block A",
    "peers": [],
    "log_level": "INFO",
    "heartbeat_interval_s": 30,
    "heartbeat_timeout_s": 120,
    "pwm_update_interval_ms": 500
  },
  "wifi": {},
  "lora": {
    "enabled": true,
    "frequency_mhz": 868,
    "air_data_rate": 2400,
    "tx_power_dbm": 22,
    "channel": 0
  },
  "timezone": {
    "name": "IST",
    "utc_offset_hours": 5.5
  },
  "hardware": {
    "i2c_sda_pin": 20,
    "i2c_scl_pin": 21,
    "i2c_freq_hz": 400000,
    "pwm_freq_hz": 1000,
    "ldr_adc_pin": 26,
    "status_led_pin": 5,
    "reset_btn_pin": 12,
    "lora_uart_id": 0,
    "lora_tx_pin": 0,
    "lora_rx_pin": 1,
    "lora_m0_pin": 2,
    "lora_m1_pin": 3,
    "lora_aux_pin": 4
  },
  "ldr": {
    "enabled": true,
    "smoothing_window_s": 60,
    "cap_rules": [
      { "above_percent": 70, "cap_percent": 10 }
    ]
  },
  "pir": [
    {
      "id": "pir1",
      "name": "Door Sensor",
      "gpio_pin": 6,
      "enabled": true,
      "vacancy_timeout_s": 300,
      "on_motion": { "action": "set_led_channels", "channels": ["ch1"], "duty_percent": 100, "fade_ms": 1000 },
      "on_vacancy": { "action": "revert_to_schedule" }
    }
  ],
  "relays": [
    {
      "id": "rly1",
      "name": "Main Power",
      "gpio_pin": 10,
      "enabled": true,
      "default_state": "off",
      "time_windows": [
        { "start": "05:00", "end": "23:00", "state": "on" }
      ]
    }
  ],
  "led_channels": [
    {
      "id": "ch1",
      "name": "Room Light",
      "gpio_pin": 16,
      "enabled": true,
      "default_duty_percent": 0,
      "time_windows": [
        { "start": "05:00", "end": "07:00", "duty_percent": 60 },
        { "start": "sunset", "end": "21:30", "duty_percent": 80 },
        { "start": "21:30", "end": "22:30", "duty_percent": 20 }
      ]
    }
  ],
  "scenes": [],
  "notifications": {
    "mqtt_enabled": false
  }
}
```

---

## Design Notes

- **Safe mode trigger:** missing `version` field, major version mismatch, or unparseable JSON → all outputs off, web UI accessible for re-upload
- **Unknown keys ignored:** firmware skips keys it doesn't recognise — forward compatible with future schema additions
- **`hardware` block is rarely edited** — pin assignments are fixed per PCB rev; config builder should hide this behind an "advanced" toggle
- **Scenes are optional** — a unit with no scenes is valid; PIR `on_motion` can use `set_led_channels` directly
- **`wifi` block empty on leaf** — coordinator reads it; leaf ignores it entirely
