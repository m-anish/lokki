# Lokki REST API Reference

The coordinator unit exposes a small HTTP/1.1 REST API on port 80. All API responses use JSON with the envelope:

```json
{ "ok": true,  "data": <payload> }
{ "ok": false, "error": "reason" }
```

HTTP status codes mirror the outcome: 200 for success, 400/404/502 for errors.

---

## Base URL

```
http://<coordinator-ip>/
```

No authentication is required. The coordinator is intended for trusted local-network use only.

---

## Endpoints

### System

#### `GET /api/status`
Returns runtime status of the coordinator itself.

**Response `data`** — selected fields:
```json
{
  "unit_name": "Pagoda",
  "unit_id": 0,
  "role": "coordinator",
  "uptime_s": 3742,
  "uptime": "1h 2m",
  "connections": { "wifi": true, "lora": true, "web_server": true, "mqtt": false },
  "led_channels": [100, 50, 0, 0, 0, 20, 20, 20],
  "relays": { "1": false, "2": false },
  "pir": { "1": "vacant" },
  "ldr_ambient": 42,
  "ldr_cap": null,
  "sensors": { "temp_c": 28.4, "humidity_pct": 62.1 },
  "error_count": 0,
  "last_error": null
}
```

`unit_name`, `unit_id`, and `role` come from `system` config and let the static dashboard set its title and labels without server-side templating. `uptime` is the human string ("1h 2m"), `uptime_s` is the integer second count.

---

#### `GET /api/config`
Returns the full `config.json` contents currently running on the coordinator, with `wifi.password` masked as `********`.

Useful for the Config Builder's "Load from Coordinator" flow and as a config backup/export. The masking exists because the dashboard is unauthenticated — anything served here is readable by any client on the LAN. See the WiFi password handling note under `POST /api/units/0/config` for how round-tripping the masked value works.

**Response `data`** — complete config object (see [config-schema.md](config-schema.md)), with `wifi.password = "********"`.

---

#### `POST /api/reboot`
Schedules a soft reset of the coordinator (executes after ~1 s to allow the response to be sent).

**Response `data`**
```json
{ "rebooting": true }
```

---

### Fleet

#### `GET /api/fleet`
Returns live status for all units — coordinator (id 0) and all leaf units that have been heard on LoRa, plus any **unclaimed** (factory-reset) leaves waiting on the claim wizard.

**Response `data`** — `{fleet: {...}, unclaimed: {...}}`
```json
{
  "fleet": {
    "0": {
      "name": "Pagoda",
      "online": true,
      "last_seen": 1746521580,
      "uptime": 3742,
      "ch": [100, 50, 0, 0, 0, 20, 20, 20],
      "rl": [false, false],
      "pir": [false, false, false, false],
      "ldr": 12,
      "err": 0,
      "rssi": null
    },
    "1": {
      "name": "South Wing",
      "uid":  "A3F1C204",
      "online": true,
      "last_seen": 1746521552,
      "uptime": 2810,
      "ch": [100, 0, 0, 0, 0, 0, 0, 0],
      "rl": [false],
      "pir": [false],
      "ldr": 30,
      "err": 0,
      "rssi": -78
    }
  },
  "unclaimed": {
    "B2C4F1AA": {
      "unit_id": 99,
      "uid":     "B2C4F1AA",
      "online":  true,
      "last_seen_ago_s": 12,
      "rssi":    -72,
      "unclaimed": true
    }
  }
}
```

`unclaimed` is keyed by chip UID, not by `unit_id` (every factory-reset leaf shares `unit_id = 99` on the air). The dashboard renders these as "New device" cards above the regular fleet view.

- `name` — unit's `system.unit_name`. For leaves, populated from the HB payload — empty until the first HB arrives after boot.
- `online` — heartbeat received within `heartbeat_timeout_s`.
- `last_seen` — Unix epoch seconds of the last HB (or, for unit 0, the time of this request). Dashboard renders as "12s ago" with a 1s ticker.
- `ch` — array of brightness values (0–100 %) in channel order. Coordinator entry is sorted by channel id.
- `rl` — array of relay states (boolean or 0/1) in config order.
- `pir` — array of motion-sensor states in config order.
- `ldr` — ambient light level 0–100 %.
- `err` — error counter since boot.
- `rssi` — dBm of the last LoRa packet THIS unit received from the coordinator (leaves only). `null` for the coordinator and until the E220 RSSI-byte append is wired up (see TODO).
- `uid` (leaves only) — last 4 bytes of `machine.unique_id()` as 8-char upper-hex. Stable per chip; useful for diagnostics ("Leaf 3 (chip ABCD1234)") and required for the claim wizard.

---

#### `GET /api/units/{id}`
Returns status for a single unit. `id` = 0 for coordinator, 1–8 for leaf units.

**Response `data`** — same shape as a single entry in `/api/fleet`.

---

#### `POST /api/units/{id}/status`
Sends a LoRa status-request to a leaf unit (fire-and-forget). The leaf replies asynchronously; poll `/api/fleet` after ~2 s to see the updated state.

**Throttled** to once per 5 seconds per unit, server-side. Repeat calls within the cooldown return `{"requested": id, "throttled": true}` with HTTP 200 and do **not** transmit anything. This keeps the dashboard from saturating the LoRa channel.

**Response `data`**
```json
{ "requested": 1 }
{ "requested": 1, "throttled": true }   // within cooldown
```

---

### Claim wizard (unclaimed leaves)

Endpoints for onboarding factory-reset leaves. Long-pressing the reset button on a leaf (5+ seconds) writes a default "unclaimed" config and reboots — the leaf comes up at `unit_id = 99` and starts broadcasting heartbeats. The coordinator surfaces these under the `unclaimed` key in `GET /api/fleet`, and the dashboard renders them as "New device" cards with a wizard.

The wizard does two things over LoRa, both addressed to `dest = 99` with a `target_uid` so only the matching chip responds:

1. **Identify**: flash that specific board's status LED (magenta, 3 s).
2. **Claim**: push a blank-slate config carrying a new `unit_id` and `unit_name`. The leaf applies it and reboots into the new identity.

The coordinator's `lora` and `hardware` config sections are copied into the blank-slate so the new leaf stays on the fleet's channel/crypt out of the box. All user-facing config (channels enabled, scenes, PIR, relays) is left empty — fill it in via the Config Builder after the claim succeeds.

#### `POST /api/unclaimed/{chip_uid}/blink`
Asks the leaf with the given chip UID to flash its status LED magenta so the operator can identify the physical board.

`chip_uid` is the 8-char upper-hex string returned in the `unclaimed` map.

**Response `data`**
```json
{ "blinked": "B2C4F1AA" }
```

Returns `404` if no unclaimed leaf with that UID is known.

#### `POST /api/unclaimed/{chip_uid}/claim`
Pushes a blank-slate config to the matching leaf so it reboots as a real claimed unit.

**Request body**
```json
{
  "unit_id": 2,                  // 1..8; rejected if already in use by an online leaf
  "name":    "Hallway Lights"    // optional; falls back to "Unit <id>"
}
```

**Response `data`**
```json
{ "claimed": "B2C4F1AA", "unit_id": 2, "name": "Hallway Lights" }
```

On success the coordinator caches the new config under `unit_id`, removes the entry from the `unclaimed` map, and the leaf reboots. The next HB from `unit_id = 2` repopulates the regular fleet slot.

Failure cases:
- `404` — unknown chip UID
- `400` — `unit_id` out of range (must be 1..8)
- `409` — `unit_id` is already in use by an online leaf
- `502` — the LoRa config push failed (`error` field has the reason from `lora_protocol.cfg_progress`)

---

### Config push

#### `GET /api/units/{id}/config`
Returns a summary of the unit's channel and relay configuration (ids, names, enabled state, defaults).

- **id 0**: drawn live from the coordinator's `config_manager`. `source` = `"live"`.
- **id 1–8**: returned from the coordinator's leaf-config cache. The cache is populated whenever the user pushes a config to that leaf via `POST /api/units/{id}/config`, so the dashboard's Control modal works without round-tripping the leaf over LoRa each time. `source` = `"cached"`.
- **id 1–8 with no cache**: empty `led_channels`/`relays` arrays plus `source: "none"` and a `note` explaining what to do. The dashboard renders an inline hint pointing to the Config Builder.

**Response `data` (coordinator, live)**
```json
{
  "version": "1.0",
  "role": "coordinator",
  "unit_id": 0,
  "unit_name": "Pagoda",
  "led_channels": [
    { "id": 1, "name": "Channel 1", "enabled": true, "default_duty_percent": 20 }
  ],
  "relays": [
    { "id": 1, "name": "Relay 1", "enabled": false, "default_state": "off" }
  ],
  "source": "live"
}
```

**Response `data` (leaf, no cache)**
```json
{
  "unit_id": 1,
  "unit_name": "South Wing",
  "led_channels": [],
  "relays": [],
  "source": "none",
  "note": "No config metadata cached on coordinator. Push the leaf's config via the Config Builder once to populate."
}
```

The cache is RAM-only — it's lost on coordinator reboot and needs to be re-populated by re-pushing each leaf's config.

---

#### `POST /api/units/{id}/config`
Pushes a complete new `config.json` to a unit.

- **id = 0**: validates and applies the config locally on the coordinator, then writes `config.json` atomically (tmp + rename, survives power loss).
- **id = 1–8**: sends the config to the leaf unit over LoRa (chunked transfer).

**WiFi password handling.** `GET /api/config` masks `wifi.password` as `********` to keep credentials off the unauthenticated dashboard. When you push a config back via this endpoint, if `wifi.password` is exactly `********`, the coordinator restores the live password before applying — so round-tripping through the Config Builder doesn't break WiFi. To actually change the password, type the new value (anything other than `********`) into the Config Builder's WiFi field.

**Request body** — full config JSON (see [config-schema.md](config-schema.md)).

**Response `data`**
```json
{ "applied": "local" }     // coordinator
{ "sent_to": 1 }           // leaf unit
```

Returns HTTP 502 if the LoRa transfer fails. Returns HTTP 400 with a validation error message if the config is invalid.

---

### Scenes

#### `GET /api/scenes`
Returns the list of named scenes configured on the coordinator.

**Response `data`**
```json
["evening", "meditation", "motion_bright"]
```

---

#### `GET /api/units/{id}/scenes`
Returns the scene names available on a specific unit.

- **id = 0**: reads from the coordinator's live config immediately.
- **id = 1–8**: returns the scene list cached from the last SRP (status response) received from that leaf. Empty list `[]` if the leaf hasn't responded since boot — open its **Control** modal to trigger a status request, then open again.

**Response `data`**
```json
["evening", "security"]
```

---

#### `POST /api/scenes/{name}/apply`
Activates a scene by name. Scene names are URL-encoded in the path (e.g. `scene%201`).

**Request body** (optional)
```json
{ "unit_ids": [0, 1, 2] }
```
Omit `unit_ids` to apply only to the coordinator (id 0). Leaf units receive the scene name over LoRa (SC message). The scene must be defined in the leaf's own config — the coordinator sends only the name.

**Response `data`**
```json
{ "0": "applied_local", "1": "sent", "2": "sent" }
```

---

### Emergency Off

#### `POST /api/emergency-off`
Immediately zeroes all LED channels and relays on every unit.

- Coordinator: sets all outputs to 0 as a manual override (status LED goes purple).
- Each leaf: sends an `EO` (Emergency Off) LoRa message; the leaf applies 0% to all its own channels/relays.
- No request body needed.

**Response `data`**
```json
{ "0": "applied_local", "1": "sent", "2": "sent" }
```

To restore normal operation, use `DELETE /api/units/{id}/manual` (or **Clear All** in the dashboard) for each unit.

---

### Manual override

#### `POST /api/units/{id}/manual`
Sets LED channels and/or relays to specific values, optionally with a fade and auto-revert timer.

**Request body**
```json
{
  "ch": [[1, 75], [3, 0]],
  "rl": [[1, 1]],
  "fade_ms": 2000,
  "revert_s": 60
}
```

- `ch` — array of `[channel_id, brightness_percent]` pairs (0–100)
- `rl` — array of `[relay_id, state]` pairs (1 = on, 0 = off)
- `fade_ms` — transition time in milliseconds (0 = instant)
- `revert_s` — seconds before reverting to schedule (0 = stay indefinitely)

**Response `data`**
```json
{ "applied": "local" }   // coordinator
{ "sent_to": 1 }         // leaf unit
```

---

#### `DELETE /api/units/{id}/manual`
Clears all manual overrides on a unit, reverting to the scheduled state immediately.

**Response `data`**
```json
{ "ok": true }
```

---

### Sensors

#### `GET /api/sensors`
Returns the latest environmental sensor readings from all units.

**Response `data`**
```json
{
  "coordinator": { "temp_c": 28.4, "humidity_pct": 62.1 },
  "1": {}
}
```
Leaf unit readings are forwarded in their heartbeat packets; the coordinator caches the last known value.

---

## Building a PWA or third-party client

### CORS

Since firmware version 1.x, all API responses include:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

OPTIONS preflight requests return `204 No Content`. This means any web page — including one served from a different origin — can call the API directly from the browser.

### HTTP vs HTTPS

The coordinator runs plain HTTP. Modern browsers block **mixed content**: a page loaded over HTTPS cannot call an HTTP endpoint. The practical options are:

| Scenario | Works? |
|----------|--------|
| App served from the coordinator (`http://192.168.x.x/`) | ✓ Same origin, no CORS needed |
| App on another HTTP local server | ✓ CORS headers allow it |
| App on Cloudflare Pages (HTTPS) calling coordinator (HTTP) | ✗ Mixed content blocked by browser |
| App on Cloudflare Pages — user adds a TLS-terminating reverse proxy in front of coordinator | ✓ With extra infra |

The simplest path for a custom app: drop your HTML/JS files into `/www/` on the coordinator's filesystem. The web server will serve them at `http://<coordinator-ip>/your-file.html`. Your code then calls the API on the same origin with no CORS or mixed-content issues.

### Polling for live state

The API has no WebSocket or server-sent events. Poll `/api/fleet` on a timer for live channel and sensor values. A 2–5 second interval is reasonable and light enough for the Pico 2.

```js
async function poll() {
  const { data } = await fetch('http://192.168.x.x/api/fleet').then(r => r.json());
  // data["0"].ch  → brightness array for coordinator
  // data["1"].ch  → brightness array for leaf 1
}
setInterval(poll, 3000);
```

### Controlling LEDs

```js
// Set channel 1 to 80 % with a 1-second fade, revert after 5 minutes
await fetch('http://192.168.x.x/api/units/0/manual', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ch: [[1, 80]], fade_ms: 1000, revert_s: 300 })
});

// Apply a named scene to all units
await fetch('http://192.168.x.x/api/scenes/evening/apply', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ unit_ids: [0, 1, 2] })
});

// Clear all overrides
await fetch('http://192.168.x.x/api/units/0/manual', { method: 'DELETE' });
```

### Service workers

Service workers require a **secure context** (HTTPS or `localhost`). If your PWA is served directly from the coordinator over HTTP, service worker registration will fail in most browsers. The existing web app does not use a service worker for this reason. If you need offline capability, serve the app from a local HTTPS server or from Cloudflare Pages (accepting the mixed-content constraint above, resolved by the reverse-proxy option).
