# TODO

Active development on `dev/lokki-v1`. See [docs/firmware-modules.md](docs/firmware-modules.md) for the full architecture.

## Status

Phases 1–3 substantially implemented and field-tested across coord + leaves. Phase 2 dashboard/control surface is complete. Hardware shakedown ongoing — see "Hardware-side open items" below for issues firmware can't fix.

## Known gaps to address before LoRa integration testing

- [x] End-to-end LoRa test on actual E220-900T22D modules: HB, SR/SRP, MO, EO, SC, CFG_* chunked transfer — running in the field
- [x] Verify SRP packet size stays under 200 B with realistic scene names — fitter in place + observed
- [ ] Confirm AUX-disciplined transmit doesn't deadlock under heavy bidirectional traffic (e.g. coordinator broadcasting TS while a leaf is sending HB) — no observed issue, not specifically stress-tested
- [x] **E220 RSSI byte-append** — enabled in `lora_transport.recv()`, `last_rx_rssi` now populated, dashboard shows real dBm values
- [ ] DST handling for `timezone.utc_offset_hours` — currently manual; document the seasonal flip in the user guide

## Phase 4 (deferred)

- [x] OTA config push for leaf units over LoRa — field-tested; claim wizard + config-builder push both work
- [ ] Persistent error log (ring buffer to flash) — design carefully to avoid wear
- [~] Optional auth on web API — **landing as HTTP Basic for LAN-only access this session**; relay-based public auth lives in [docs/relay-design.md](docs/relay-design.md)
- [x] Auto-discovery of leaf units — **covered by the claim wizard**: factory-reset leaves announce at unit_id=99 with chip UID, surface in the dashboard as "New device" cards, operator picks a unit_id and name
- [x] Periodic NTP+TS broadcast — 60 s while unsynced, 24 h once synced
- [ ] **LoRa frame authentication** — HMAC-SHA256(truncated to 8 B) signing + per-source replay window on every LoRa frame, key in `/secrets.json` (separate from `config.json`). Already implemented on branch [`feature/lora-shared-secret`](https://github.com/m-anish/lokki/tree/feature/lora-shared-secret) (commit `47f6d88`); +312 lines across 8 files. Not urgent for lab / friends-and-family use because the E220's `crypt_h`/`crypt_l` radio-level encryption already keeps the casually curious out — revisit before any deployment where a motivated attacker could plausibly be in radio range. **Merge effort ~2–3 h:** conflict-prone files are `lora_protocol.py`, `main.py`, `update.sh`, `docs/lora-protocol.md` (all touched heavily since the branch was cut); re-validate HB/SRP size budgets (HMAC adds 22 B per frame, but our recent short-key + conditional-field work freed ~40 B so the math works out better than the branch author saw); confirm the claim-wizard flow still works under signing.

## Phase 5+ ideas worth surfacing

- [ ] **Starter profiles in Config Builder** — claim wizard currently produces a blank leaf; operator has to fill in everything via the Builder. Starter profiles (e.g. "Indoor 8-channel", "Outdoor PIR-triggered floodlight") would shave the setup cycle. _Landing this session._
- [ ] **`POST /api/time-sync` + "Set time now" button** on the dashboard — finishes the time-sync UX so a coord stuck with no NTP and no DS3231 can be unblocked from the browser. _Landing this session._
- [ ] **`docs/auth-design.md`** — sibling of `relay-design.md`, covering a reusable auth library design once Lokki has more company. Defer until there's a second project that would consume it.

## Polish

- [ ] Reconcile `_dashboard_html`-era debug behaviour in any remaining scripts
- [ ] Consolidate per-handler imports in `api_handlers.py` to top-of-file (only if it helps boot RAM)

## Hardware-side open items (firmware can't fix)

- [ ] **LM2596 brownout** theory for the simultaneous-EIO pattern across I²C / UART / WiFi — add 100 µF + 100 nF at VSYS / E220 V+ on the next board rev
- [ ] **DS3231 backup battery** health audit across deployed units (the recent EIO turned out to be a cold solder joint, but battery state across the fleet is unaudited)
- [ ] Next PCB rev: MOSFET-on-GPIO for forced E220 power-cycle as a hardware recovery path
