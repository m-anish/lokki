# TODO

Active development on `dev/lokki-v1`. See [docs/firmware-modules.md](docs/firmware-modules.md) for the full architecture.

## Status

Phases 1–3 are largely implemented and pass on the bench (single-unit). LoRa hardware testing (Phase 2 integration) is still pending.

## Known gaps to address before LoRa integration testing

- [ ] End-to-end LoRa test on actual E220-900T22D modules: HB, SR/SRP, MO, EO, SC, CFG_* chunked transfer
- [ ] Verify SRP packet size stays under 200 B with realistic scene names; the truncation guard in `main.on_status_request` is in place but unverified on the wire
- [ ] Confirm AUX-disciplined transmit doesn't deadlock under heavy bidirectional traffic (e.g. coordinator broadcasting TS while a leaf is sending HB)
- [ ] **E220 RSSI byte-append** — enable per-packet RSSI reporting on the module (register/AT config) and parse the trailing RSSI byte in `lora_transport.recv()`. Plumb into `lora_protocol.last_rx_rssi` so HBs/SRPs carry meaningful signal-strength values. Currently `last_rx_rssi` stays `None` and the dashboard shows "—".
- [ ] DST handling for `timezone.utc_offset_hours` — currently manual; document the seasonal flip in the user guide

## Phase 4 (deferred)

- [ ] OTA config push for leaf units over LoRa (already wired via CFG_* — needs field testing)
- [ ] Persistent error log (ring buffer to flash) — design carefully to avoid wear
- [ ] Optional auth on web API (HTTP Basic + reverse-proxy TLS as the deployment story)
- [ ] Auto-discovery of leaf units (any HB → fleet, optionally pending coordinator approval)
- [ ] Periodic NTP+TS broadcast (currently boot-only)
- [ ] **LoRa frame authentication** — HMAC-SHA256(truncated to 8 B) signing + per-source replay window on every LoRa frame, key in `/secrets.json` (separate from `config.json`). Already implemented on branch [`feature/lora-shared-secret`](https://github.com/m-anish/lokki/tree/feature/lora-shared-secret) (commit `47f6d88`); +312 lines across 8 files. Not urgent for lab / friends-and-family use because the E220's `crypt_h`/`crypt_l` radio-level encryption already keeps the casually curious out — revisit before any deployment where a motivated attacker could plausibly be in radio range. **Merge effort ~2–3 h:** conflict-prone files are `lora_protocol.py`, `main.py`, `update.sh`, `docs/lora-protocol.md` (all touched heavily since the branch was cut); re-validate HB/SRP size budgets (HMAC adds 22 B per frame, but our recent short-key + conditional-field work freed ~40 B so the math works out better than the branch author saw); confirm the claim-wizard flow still works under signing.

## Polish

- [ ] Reconcile `_dashboard_html`-era debug behaviour in any remaining scripts
- [ ] Consolidate per-handler imports in `api_handlers.py` to top-of-file (only if it helps boot RAM)
- [ ] Add `POST /api/time-sync` to broadcast TS on demand
