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

**Response `data`**
```json
{
  "uptime": 3742,
  "uptime_str": "1h 2m",
  "connections": { "wifi": true, "lora": true, "mqtt": false },
  "error_count": 0
}
```

---

#### `GET /api/config`
Returns the full `config.json` contents currently running on the coordinator.

Useful for the config builder "Load from Coordinator" flow, and as a config backup/export.

**Response `data`** — complete config object (see [config-schema.md](config-schema.md)).

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
Returns live status for all units — coordinator (id 0) and all leaf units that have been heard on LoRa.

**Response `data`** — object keyed by unit id (integer as string)
```json
{
  "0": {
    "online": true,
    "uptime": 3742,
    "ch": [100, 50, 0, 0, 0, 20, 20, 20],
    "rl": [false, false],
    "pir": [false, false, false, false],
    "ldr": 12,
    "err": 0
  },
  "1": { "online": true, "uptime": 2810, "ch": [...], ... }
}
```

- `ch` — array of 8 brightness values (0–100 %) in channel order
- `rl` — array of 2 relay states (boolean)
- `pir` — array of up to 4 motion-sensor states (boolean)
- `ldr` — ambient light level 0–100 %
- `err` — error counter since boot

---

#### `GET /api/units/{id}`
Returns status for a single unit. `id` = 0 for coordinator, 1–8 for leaf units.

**Response `data`** — same shape as a single entry in `/api/fleet`.

---

#### `POST /api/units/{id}/status`
Sends a LoRa status-request to a leaf unit (fire-and-forget). The leaf replies asynchronously; poll `/api/fleet` after ~2 s to see the updated state.

**Response `data`**
```json
{ "requested": 1 }
```

---

### Config push

#### `GET /api/units/{id}/config`
Returns a summary of the unit's channel and relay configuration (id names, enabled state, defaults). For the coordinator (id 0) this is drawn from live config. For leaf units the coordinator has no copy — the response notes that.

**Response `data` (coordinator)**
```json
{
  "version": "1.0",
  "role": "coordinator",
  "unit_id": 0,
  "unit_name": "Pagoda",
  "led_channels": [
    { "id": "ch1", "name": "Channel 1", "enabled": true, "default_duty_percent": 20 }
  ],
  "relays": [
    { "id": "rly1", "name": "Relay 1", "enabled": false, "default_state": "off" }
  ]
}
```

---

#### `POST /api/units/{id}/config`
Pushes a complete new `config.json` to a unit.

- **id = 0**: validates and applies the config locally on the coordinator, then writes `config.json`.
- **id = 1–8**: sends the config to the leaf unit over LoRa (chunked transfer).

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
Returns the list of named scenes currently configured on the coordinator.

**Response `data`**
```json
["evening", "meditation", "motion_bright"]
```

---

#### `POST /api/scenes/{name}/apply`
Activates a scene by name. Scene names are URL-encoded in the path (e.g. `scene%201`).

**Request body** (optional)
```json
{ "unit_ids": [0, 1, 2] }
```
Omit `unit_ids` to apply only to the coordinator (id 0). Leaf units receive the scene name over LoRa.

**Response `data`**
```json
{ "0": "applied_local", "1": "sent", "2": "sent" }
```

---

### Manual override

#### `POST /api/units/{id}/manual`
Sets LED channels and/or relays to specific values, optionally with a fade and auto-revert timer.

**Request body**
```json
{
  "ch": [["ch1", 75], ["ch3", 0]],
  "rl": [["rly1", 1]],
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
// Set channel ch1 to 80 % with a 1-second fade, revert after 5 minutes
await fetch('http://192.168.x.x/api/units/0/manual', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ch: [['ch1', 80]], fade_ms: 1000, revert_s: 300 })
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
