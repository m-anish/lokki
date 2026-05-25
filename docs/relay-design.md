# Lokki Cloud Relay — Design Document

**Status:** Not implemented. Forward-looking design for future work.
**Owner:** TBD
**Last updated:** 2026-05-26

This document describes the planned architecture for giving every Lokki coordinator a stable public URL — like `<chip_uid>.lokki.app` — without any on-site companion hardware, port forwarding, or per-install configuration. The coordinator itself initiates a single outbound connection to a hosted relay; the relay assigns a hostname and forwards public HTTP requests to the coord over that connection.

This is "**Option C**" from the remote-access comparison: roll-your-own ngrok-shaped service tailored to Lokki.

---

## 1. Goals and Non-Goals

### Goals

- **Per-coordinator stable public URL.** Each Lokki gets a hostname that doesn't change across reboots, WiFi changes, or ISP-imposed NAT shuffles.
- **No on-site hardware beyond the Pico itself.** The coordinator is the only box. No Pi Zero, no router config, no DDNS.
- **No inbound exposure from the public internet to the Pico.** The Pico only ever opens outbound connections.
- **Pico knows its own URL.** So the dashboard can render a QR code pointing at itself, and operators can scan it to access the dashboard from anywhere.
- **Auth handled by the relay**, not by the Pico. MicroPython's HTTP stack is not hardened; we don't want to write authn/authz on the device.
- **TLS terminated at the relay.** The Pico ↔ relay link uses outbound TLS via MicroPython's `ssl` (well-supported as a client). Public clients ↔ relay use Let's Encrypt.
- **Pico stays usable on the LAN even if the relay is down.** Local access via `http://<lan-ip>/` must keep working with the relay unreachable.

### Non-Goals

- A general-purpose tunnel service (this is Lokki-specific framing).
- Multi-region failover or HA for v1. A single $5/mo VPS is fine to start.
- Hiding the dashboard from the LAN. Local access stays open; auth is a remote-access concern.
- Cellular fallback when WiFi is down. The relay assumes a working internet path.

---

## 2. High-Level Architecture

```
                                                                                                    
   ┌─────────────────────────┐                          ┌──────────────────────────────────────┐
   │  Operator's browser     │   HTTPS                  │   Relay (one VPS)                    │
   │  abc1234.lokki.app/…    │ ───────────────────────▶ │   - TLS termination                  │
   └─────────────────────────┘   GET /api/fleet         │   - Auth gate (Cloudflare Access /   │
                                                        │     magic-link / OAuth)              │
                                                        │   - Subdomain → tunnel routing       │
                                                        │   - Request/response framing on WS   │
                                                        └────────────┬─────────────────────────┘
                                                                     │  outbound WSS, persistent
                                                                     │  framed { req_id, … }
                                                                     ▼
                                                        ┌──────────────────────────────────────┐
                                                        │  Lokki coordinator (Pico 2 W)        │
                                                        │  - Outbound TLS WebSocket client     │
                                                        │  - Dispatches inbound frames to the  │
                                                        │    existing /api routes              │
                                                        │  - Continues to serve LAN clients on │
                                                        │    plain HTTP                        │
                                                        └──────────────────────────────────────┘
```

**Key shape:** the relay is a stateless reverse proxy for HTTP, multiplexed over one persistent outbound WebSocket per coord. The Pico never accepts a public inbound connection.

---

## 3. The Pico Side

### 3.1 New module: `comms/relay_client.py`

A single asyncio task that:

1. Reads relay config from `config.json` → `relay` section (see §6).
2. Opens an outbound WebSocket Secure (WSS) connection to `wss://relay.lokki.app/coord` with an auth header containing the coord's enrolment token.
3. On connect, sends a `HELLO` frame with `{chip_uid, fw_version, public_key_fingerprint}`.
4. Receives back an `ASSIGNED` frame with `{public_url}`, which it stashes on a module-level variable for `/api/public-url` (see §3.4).
5. Listens for `REQUEST` frames carrying serialized HTTP requests; dispatches each to the existing `web_server` request handler in a way that produces a `RESPONSE` frame on the same `req_id`.
6. On disconnect or error, reconnects with exponential backoff (clamped to 5 min, same shape as the LoRa deferred-retry pattern at [main.py](../firmware/micropython/src/main.py) lines 164–199).

Why outbound WebSocket and not HTTP long-poll: a single TCP connection, mature framing, minimal head-of-line blocking. MicroPython has working WS client implementations (`uwebsockets`, or hand-rolled — `< 200 lines`).

### 3.2 Request/response framing on the wire

Lokki control frames are JSON envelopes over WS, mirroring the LoRa protocol envelope shape so the Pico-side code feels familiar:

```jsonc
// Relay → Pico (HTTP request to forward)
{
  "t":   "REQ",
  "id":  "r-9f3a",         // opaque, used to correlate the RESPONSE
  "m":   "GET",
  "p":   "/api/fleet",
  "h":   { "x-lokki-user": "anish@..." },   // hop-by-hop auth headers
  "b":   ""                 // body, base64 if non-utf8; usually empty for GET
}

// Pico → Relay (response)
{
  "t":  "RES",
  "id": "r-9f3a",
  "st": 200,
  "h":  { "content-type": "application/json" },
  "b":  "{\"ok\":true,...}"
}

// Bidirectional control frames
{ "t": "PING" }                       // 30s heartbeat from Pico
{ "t": "PONG" }                       // echoed by relay
{ "t": "HELLO", "uid":"…", "fw":"…" } // Pico → Relay on connect
{ "t": "ASSIGNED", "url":"https://abc1234.lokki.app" }  // Relay → Pico
{ "t": "ERR", "code":"...", "msg":"..." }
```

**Single-flight per req_id**: the relay only forwards one outstanding REQ per public client at a time; the Pico can stream RES frames in any order and the relay re-orders by `id`. v1 keeps it simple — at most ~4 in-flight per coord, since the operator's browser polls every few seconds and there's only one or two of them.

**Size cap.** Frames are capped at 4 KB (same order as LoRa's 200 B cap is for radio; here it's about Pico heap fragmentation). Anything larger gets chunked just like CFG_*. In practice, `/api/fleet` responses are 1–3 KB and the rest are smaller.

**Streaming responses.** Not needed in v1. The dashboard polls; it doesn't use SSE or WebSockets.

### 3.3 Integration point: dispatching to existing routes

The existing `web_server` already parses HTTP and dispatches to handlers via `_route_unit`, `_route_unclaimed`, and the `/api/...` table at [web_server.py:243-282](../firmware/micropython/src/coordinator/web_server.py#L243-L282). We refactor that dispatch out of the request-parsing path into a `web_server.dispatch(method, path, headers, body)` function returning `(status, content_type, body)`. The relay client calls this directly with the REQ frame contents. No HTTP parsing on the WS path.

This keeps the LAN HTTP server unchanged and reuses every route — including `/api/fleet`, `/api/units/N/manual`, `/api/unclaimed/<uid>/claim`, etc. — for free.

### 3.4 New endpoint: `/api/public-url`

Trivial handler returning whatever `relay_client` last received in an `ASSIGNED` frame:

```jsonc
GET /api/public-url
{
  "ok": true,
  "data": {
    "url": "https://abc1234.lokki.app",
    "connected": true,
    "since_s": 4213
  }
}
```

When the relay isn't connected (boot, network down, relay outage), returns `{url: null, connected: false, ...}`. The dashboard hides the QR in that case and shows a small "Remote access offline" indicator.

### 3.5 RAM and CPU budget on the Pico

Order-of-magnitude estimate:

- TLS WS client: ~30 KB RAM steady-state (mbedTLS heap during handshake is the worst moment — historically ~50 KB peak; the LoRa deferred-init pattern teaches us to do this **after** the rest of boot stabilizes).
- Frame buffer (single 4 KB inbound + 4 KB outbound): ~8 KB.
- One asyncio task with a per-loop allocation budget similar to `listen_task`.

Total: ~40 KB steady, ~60 KB peak during handshake. Pico 2 W has ~520 KB SRAM; we use ~80 KB at idle today. Plenty of headroom, but we should benchmark before claiming it's free.

CPU: WebSocket frame parse + a JSON decode per inbound request. The bottleneck is the existing route handlers, not the relay client.

---

## 4. The Relay Side

A small Go or Rust service running on a single VPS. ~500–1000 LOC.

### 4.1 Responsibilities

- Terminate TLS for `*.lokki.app` (wildcard cert via Let's Encrypt DNS-01 with Cloudflare API).
- Accept inbound WSS at `wss://relay.lokki.app/coord` with token auth.
- Maintain an in-memory map `chip_uid → live WS connection`.
- Assign each coord a subdomain on first connect: `{slug}.lokki.app`, where `slug` is either:
  - The chip UID lowercased (default), or
  - A human-friendly name registered via a separate admin endpoint (`POST /admin/aliases`).
- For each inbound HTTPS request to `<slug>.lokki.app/...`:
  1. Look up the coord by slug → WS connection.
  2. Pass the request through the **auth gate** (see §5).
  3. Wrap as a `REQ` frame and forward to the Pico.
  4. Await the matching `RES` frame (with a 30 s deadline; on timeout, return 504 to the client).
  5. Unwrap and respond to the public HTTP client.
- Re-issue the same slug to the same chip UID across reconnects (subdomains are sticky, persisted in a small SQLite or BoltDB file).

### 4.2 What it is NOT

- It is **not** a generic reverse proxy. It only understands the Lokki framing.
- It is **not** a stateful caching layer. Every request round-trips to the Pico.
- It does **not** terminate or inspect Lokki API semantics. Auth headers are added; the body is opaque.

### 4.3 Failure modes

| Scenario | Relay behaviour | Pico behaviour |
|---|---|---|
| Pico disconnects (WiFi blip) | Drop in-flight `req_id`s with 502 to the public client. Hold the slug for 24h. | Reconnect with backoff; re-claim the slug on `HELLO`. |
| Pico OOM mid-frame | Same as above. | Best-effort `ERR` frame, then close + reconnect. |
| Relay crashes / restarts | Public clients see TLS errors briefly. | Reconnects on backoff; receives a fresh `ASSIGNED` (same slug). |
| Pico boots with no internet | LAN access still works. | `/api/public-url` returns `connected: false`. Relay client task sits in backoff loop, no LED noise. |
| Operator's network blocks outbound 443 | Same as above. | Eventually we add a `relay.fallback_port` config (e.g. 8443) for sites with strict egress filtering. |

---

## 5. Auth

The dashboard is unauthenticated on the LAN by design (anyone on your WiFi can already control your lights — same threat model as any home automation hub). On the public internet, that won't fly. The relay enforces auth so the Pico stays simple.

Three plausible auth models, in increasing order of "do this for v1":

1. **Cloudflare Access in front of the relay.** Easiest: bolt CF Access onto the relay's public endpoints. CF handles email-OTP, Google SSO, etc.; we get a signed `Cf-Access-Jwt-Assertion` header on every forwarded request and can pass identity through to the Pico for audit logging. Free for ≤50 users.

2. **Magic-link auth built into the relay.** Sign in once per browser with an emailed link; cookie holds a signed JWT thereafter. ~200 LOC.

3. **HTTP Basic + per-coord password.** Set in the relay's `aliases.json`, displayed on the LAN dashboard. Crude but fits inside a single screen.

The auth model is decoupled from the relay's tunnel mechanics — you can swap (1) ↔ (2) ↔ (3) without changing any Pico-side code.

---

## 6. Configuration

New `relay` section in `config.json`:

```jsonc
"relay": {
  "enabled":  true,
  "endpoint": "wss://relay.lokki.app/coord",
  "token":    "lokki-prov-AAAA-BBBB-CCCC",   // enrolment token, minted per coord at provisioning
  "alias":    null,                          // optional human-friendly slug request; null = use chip UID
  "fallback_port": null                      // optional, for restricted egress
}
```

Validator rules (in `config_manager._validate`):
- `enabled: false` skips the whole feature; identical to not having the section at all.
- `token` is required when `enabled: true`. No default — must be provisioned.
- `endpoint` defaults to `wss://relay.lokki.app/coord` if omitted.

The schema needs a corresponding entry in [config-schema.md](config-schema.md) when implemented.

---

## 7. Provisioning Flow

For each coordinator we ship:

1. **Mint a token.** Admin runs `relayctl mint-token --chip-uid <uid>` on the relay; gets back `lokki-prov-XXXX-YYYY-ZZZZ`. This token is bound to the chip UID — the relay refuses to accept it from a different chip.
2. **Bake into config.** `config.relay.token = <token>`. Either via the Config Builder UI (new section) or via a CLI tool.
3. **First boot.** Pico connects, sends `HELLO {chip_uid}`, relay verifies token + chip UID, assigns `<chip_uid_lower>.lokki.app`, persists the binding.
4. **QR appears on dashboard.** Operator scans, accesses dashboard remotely after going through the auth gate.

Renaming to a human-friendly slug is a separate admin action (`relayctl alias <chip_uid> hallway-pagoda`).

---

## 8. Dashboard UX

A new section on the Fleet page, below the hero:

- "**Remote access**" panel.
- If `/api/public-url` returns `connected: true`: show URL, a QR code (rendered client-side with a small JS lib like `qrcode-svg`, ~3 KB), and a copy-to-clipboard button.
- If `connected: false`: show "Remote access offline" with the last-known URL faded out and a hint that LAN access is unaffected.
- A small expandable details row: relay endpoint, last connect time, last error if any. Useful for triage without SSH.

No UI changes anywhere else — the relay is transparent to the rest of the dashboard.

---

## 9. Open Questions

1. **Where does the relay live?** Hetzner CX11 ($5/mo) for v1 is plenty (estimated <100 KB/s aggregate per coord; can host hundreds on one box). Geographic latency? `eu-central` covers most of where Lokki gets deployed; add `us-east` if needed.
2. **Who pays?** Free for owned fleet. If we ever sell Lokki, the relay cost rolls into the unit price (~$2/year of VPS per device at scale; trivial).
3. **What about the Config Builder?** It currently runs as a static page (`/config-builder.html`) served by the coord. Through the relay it works unchanged — every API call is just a `REQ`/`RES` round-trip. Slow but functional.
4. **Mqtt notifier vs relay.** The existing MQTT scaffolding could route through the same relay infrastructure. Out of scope for v1.
5. **Multi-coordinator mesh** (ROADMAP Phase 6). If two coords share fleet state, do they share a slug too? Probably not — each gets its own URL, but the relay could route `<slug>/peer/...` to the right one. Defer.
6. **Audit log.** Should the relay log every forwarded request? Yes for debugging. Privacy: the request body can carry sensitive state. Log line metadata only, not bodies, in v1.

---

## 10. Implementation Order

When this gets picked up, suggested order — each step independently testable:

1. **Relay skeleton.** WS server, in-memory map, hard-coded single coord, plain HTTP not HTTPS yet. Push the existing LAN dashboard through it end-to-end on `localhost`.
2. **Pico-side `relay_client`.** Outbound WS client + frame dispatch into a stubbed `web_server.dispatch()`. Verify on a bench with the relay on a laptop.
3. **Refactor `web_server` to expose `dispatch()`.** This is the only change to existing code paths; do it carefully and verify all LAN behaviour is unchanged.
4. **TLS + Let's Encrypt + wildcard DNS** on the relay.
5. **Token-based enrolment**, slug persistence (SQLite), reconnect logic.
6. **Auth gate** (start with Cloudflare Access for the easy ride).
7. **Dashboard QR panel.**
8. **Config Builder section** for `relay.*`.
9. **Soak test** — leave a coord connected for a week, see what breaks.

Each step is a small PR. Total estimated effort: 2–3 focused weekends for v1, plus another for hardening.

---

## 11. What This Replaces

- Cloudflare Tunnel + companion Pi (Option A from the comparison): not needed, no companion required.
- Tailscale Funnel: not needed.
- Port forwarding + DDNS + reverse proxy: not needed.

The relay is the one mechanism every coord uses, regardless of where it's deployed.

---

## 12. Status & References

- **Not implemented.** This document is the design; code is future work.
- Related: [ROADMAP.md](../ROADMAP.md) → Phase 6 / Longer-Term Ideas ("Secure remote access").
- Related: [api-reference.md](api-reference.md) — `/api/public-url` will live there once shipped.
- Related: [firmware-modules.md](firmware-modules.md) — `comms/relay_client.py` will be added to the layout.
