import asyncio
import json
import time
from comms.lora_transport import lora_transport, LoRaTimeoutError
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

# Message types
HB        = "HB"
TS        = "TS"
PIR_EV    = "PIR"
SC        = "SC"
MO        = "MO"
SR        = "SR"
SRP       = "SRP"
ACK       = "ACK"
ERR       = "ERR"
CFG_START = "CFG_START"
CFG_CHUNK = "CFG_CHUNK"
CFG_END   = "CFG_END"
CFG_PATCH = "CFG_PATCH"   # single-packet single-field update; see send_patch()
EO        = "EO"          # Emergency Off — all outputs to zero
BLINK     = "BLINK"       # "blink your status LED so the operator can identify you" — used by the claim wizard

# ACK required for these types
_ACK_REQUIRED = {SC, MO, EO, CFG_END, CFG_PATCH}

_ACK_TIMEOUT_S  = 10
_CHUNK_SIZE     = 64
_CHUNK_DELAY_MS = 200
_MAX_RETRIES    = 3
_BROADCAST      = 255

# E220-900T22D maximum payload per packet. Chunked transfers handle larger.
_MAX_PACKET_BYTES = 200


def _envelope_overhead():
    """Worst-case bytes added by the envelope wrapper around the payload.
    Computed once at module load with the longest msg type ("CFG_CHUNK") and
    3-digit src/dest/seq so callers can rely on a single conservative number
    instead of measuring per-message. Reserve a small safety margin (4 B) for
    JSON whitespace variance and any future field tweaks."""
    skel = {"s": 255, "d": 255, "t": CFG_CHUNK, "seq": 255, "p": {}}
    return len(json.dumps(skel).encode()) + 4


_ENVELOPE_OVERHEAD = _envelope_overhead()
# Max bytes a payload dict may serialize to and still fit. Fitters target this.
_MAX_PAYLOAD_BYTES = _MAX_PACKET_BYTES - _ENVELOPE_OVERHEAD


def _crc32(data):
    if isinstance(data, str):
        data = data.encode()
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


class LoRaProtocol:

    def __init__(self):
        self._seq      = 0
        self._handlers = {}          # {msg_type: handler_fn}
        self._fitters  = {}          # {msg_type: fitter_fn(payload, budget) -> payload}
        self._pending  = {}          # {seq: {msg, sent_at, retries}}
        self._unit_id  = 0
        # RSSI of the most recent received packet in dBm (signed int) or None.
        # Populated by the transport when E220 RSSI-byte append is enabled
        # (see TODO in lora-protocol.md). Leaves include this in HB so the
        # coordinator can show link quality on the dashboard.
        self.last_rx_rssi = None
        # Live progress for the most recent config push. The web layer polls
        # this so the upload modal shows real chunk progress instead of a
        # time-based estimate that stalls.
        #   phase: "idle" | "starting" | "uploading" | "verifying"
        #          | "success" | "failed"
        self.cfg_progress = {
            "unit_id": None,
            "total":   0,    # total chunks
            "sent":    0,    # chunks transmitted so far
            "phase":   "idle",
            "message": "",
        }

    def init(self):
        self._unit_id = config_manager.unit_id
        lora_transport.init()
        log.info(f"[LORA_PROTO] Init, unit_id={self._unit_id}")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on(self, msg_type, handler):
        """Register handler: handler(src_id, payload_dict)"""
        self._handlers[msg_type] = handler

    def fitter(self, msg_type, fn):
        """Register a per-msg-type pre-flight fitter. `fn(payload, budget_bytes)`
        receives the payload dict and the max number of bytes the JSON-encoded
        payload may take to still fit a 200B LoRa packet. It returns a payload
        that fits (mutated in place is fine), or the original if no shrinking
        is needed. Called by send() before the envelope size check."""
        self._fitters[msg_type] = fn

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, msg_type, dest, payload=None):
        self._seq = (self._seq + 1) & 0xFF
        seq = self._seq

        # Pre-flight fit: give the msg-type's registered fitter (if any) a
        # chance to shrink the payload to the per-payload byte budget before
        # we build the envelope. Centralizes the "fits-on-the-wire" policy in
        # one place instead of forcing each caller to know about the 200B cap.
        if payload is not None:
            fit = self._fitters.get(msg_type)
            if fit:
                try:
                    payload = fit(payload, _MAX_PAYLOAD_BYTES)
                except Exception as e:
                    log.error(f"[LORA_PROTO] {msg_type} fitter raised: {e}")

        envelope = {"s": self._unit_id, "d": dest, "t": msg_type, "seq": seq}
        if payload:
            envelope["p"] = payload

        try:
            raw = json.dumps(envelope).encode()
            # CFG_CHUNK is pre-sized to fit; everything else must stay under the
            # E220 packet limit or the receiver will see a truncated, unparseable
            # frame. If we get here oversized it's a caller bug (fitter missing
            # or insufficient, or a splittable command sent un-split).
            if len(raw) > _MAX_PACKET_BYTES:
                log.error(
                    f"[LORA_PROTO] {msg_type} dropped: {len(raw)}B exceeds "
                    f"{_MAX_PACKET_BYTES}B limit (register a fitter for {msg_type}, "
                    f"or use a batched sender if the command is splittable)"
                )
                return None
            # Transport maps logical dest → (DESTH, DESTL) per the FIXED-mode
            # address scheme. _BROADCAST (255), 0xFFFF, and None all collapse
            # to the (0xFF, 0xFF) broadcast destination; 0..8 map to leaf or
            # coord directly.
            lora_transport.send(dest, raw)
            if msg_type in _ACK_REQUIRED:
                self._pending[seq] = {
                    "msg": envelope,
                    "dest": dest,
                    "sent_at": time.time(),
                    "retries": 0,
                }
            return seq
        except LoRaTimeoutError:
            log.error(f"[LORA_PROTO] Send timeout: {msg_type}")
            return None
        except Exception as e:
            log.error(f"[LORA_PROTO] Send error: {e}")
            return None

    # ------------------------------------------------------------------
    # Chunked config transfer (coordinator → leaf)
    # ------------------------------------------------------------------

    async def send_config(self, dest_id, config_str, target_uid=None, target_path=None):
        """Chunked config transfer.

        target_uid: if given, the leaf compares against its own chip
        UID and ignores mismatches. Used by the claim wizard so a
        CFG_START aimed at unit_id=99 only lands on the specific
        freshly-factory-reset board the operator picked.

        target_path: if given, the assembled string is parsed as JSON
        and SET AT THAT PATH in the leaf's current config (rather
        than replacing the whole config). Used by the incremental
        config protocol for section-level updates that don't fit
        CFG_PATCH's 200-byte single-packet budget — e.g. replacing
        the entire `led_channels` array (~1.5 KB) without touching
        `system`, `lora`, etc. Path syntax matches core.json_path:
        slash-separated, numeric segments index lists.
        """
        data    = config_str.encode()
        total   = len(data)
        chunks  = []
        offset  = 0
        while offset < total:
            chunks.append(data[offset:offset + _CHUNK_SIZE])
            offset += _CHUNK_SIZE

        transfer_id = "{:04x}".format(time.time() & 0xFFFF)
        checksum    = "{:08x}".format(_crc32(data))

        log.info(f"[LORA_PROTO] Config transfer {transfer_id}: "
                 f"{len(chunks)} chunks → unit {dest_id}"
                 + (f" (target_uid={target_uid})" if target_uid else ""))

        self.cfg_progress = {
            "unit_id": dest_id,
            "total":   len(chunks),
            "sent":    0,
            "phase":   "starting",
            "message": "",
        }

        for attempt in range(1, _MAX_RETRIES + 1):
            # CFG_START
            self.cfg_progress["phase"] = "starting"
            self.cfg_progress["sent"]  = 0
            cfg_start_payload = {
                "transfer_id": transfer_id,
                "total_chunks": len(chunks),
                "total_bytes": total,
            }
            if target_uid:
                cfg_start_payload["target_uid"] = target_uid
            if target_path:
                cfg_start_payload["target_path"] = target_path
            seq = self.send(CFG_START, dest_id, cfg_start_payload)
            ack = await self._wait_ack(seq)
            if not ack or not ack.get("ok", True):
                log.warn(f"[LORA_PROTO] CFG_START no ACK or rejected (attempt {attempt})")
                continue

            # CFG_CHUNKs
            self.cfg_progress["phase"] = "uploading"
            for i, chunk in enumerate(chunks):
                self.send(CFG_CHUNK, dest_id, {
                    "transfer_id": transfer_id,
                    "chunk_index": i,
                    "data": chunk.decode("utf-8", "ignore"),
                })
                self.cfg_progress["sent"] = i + 1
                await asyncio.sleep_ms(_CHUNK_DELAY_MS)

            # CFG_END
            self.cfg_progress["phase"] = "verifying"
            seq = self.send(CFG_END, dest_id, {
                "transfer_id": transfer_id,
                "checksum": checksum,
            })

            ack = await self._wait_ack(seq)
            if ack:
                if ack.get("ok", True):
                    log.info(f"[LORA_PROTO] Config transfer {transfer_id} complete")
                    self.cfg_progress["phase"]   = "success"
                    self.cfg_progress["message"] = ""
                    return True

                # If the leaf could not apply the config (validator rejected it,
                # flash write failed, etc.), retrying the same payload won't help.
                # Bail out so the caller surfaces the error to the user.
                if ack.get("reason") == "APPLY_FAILED":
                    err_str = ack.get("err", "")
                    log.error(
                        f"[LORA_PROTO] Config transfer {transfer_id} rejected by leaf: "
                        f"{err_str or 'no details'}"
                    )
                    self.cfg_progress["phase"]   = "failed"
                    self.cfg_progress["message"] = err_str or "Device rejected the config"
                    return False

                # The leaf rejected the checksum. Did it tell us which chunks are missing?
                missing = ack.get("missing")
                if missing and isinstance(missing, list) and len(missing) > 0:
                    log.warn(f"[LORA_PROTO] CFG_END rejected. Leaf is missing {len(missing)} chunks. Retrying only missing chunks...")
                    # Smart retry: re-send only the chunks the leaf didn't get.
                    # Indices stay the same so the leaf re-assembles correctly.
                    #
                    # IMPORTANT for the dashboard's progress bar: do NOT
                    # decrement cfg_progress["sent"] back to (total-missing)
                    # during retry. The user has already seen the bar fill
                    # to ~90%; resetting it to 60% and crawling back up
                    # looks like a regression and shakes confidence. We
                    # keep sent at its peak value (the loop already left
                    # it at `total`), and switch phase to a new
                    # "retrying" state. The dashboard renders that as
                    # "Patching up… ~95%" with the bar held still.
                    for attempt_missing in range(3):
                        self.cfg_progress["phase"] = "retrying"
                        log.info(f"[LORA_PROTO] Smart retry: sending {len(missing)} missing chunks...")
                        for m_idx in missing:
                            if 0 <= m_idx < len(chunks):
                                self.send(CFG_CHUNK, dest_id, {
                                    "transfer_id": transfer_id,
                                    "chunk_index": m_idx,
                                    "data": chunks[m_idx].decode("utf-8", "ignore"),
                                })
                                await asyncio.sleep_ms(_CHUNK_DELAY_MS)

                        # Re-send CFG_END
                        self.cfg_progress["phase"] = "verifying"
                        seq = self.send(CFG_END, dest_id, {
                            "transfer_id": transfer_id,
                            "checksum": checksum,
                        })
                        ack = await self._wait_ack(seq)
                        if ack and ack.get("ok", True):
                            log.info(f"[LORA_PROTO] Config transfer {transfer_id} complete after smart retry")
                            self.cfg_progress["phase"]   = "success"
                            self.cfg_progress["message"] = ""
                            return True
                        if ack and ack.get("reason") == "APPLY_FAILED":
                            err_str = ack.get("err", "")
                            self.cfg_progress["phase"]   = "failed"
                            self.cfg_progress["message"] = err_str or "Device rejected the config"
                            return False
                        missing = ack.get("missing") if ack else None
                        if not missing:
                            break # fallback to full retry
            
            log.warn(f"[LORA_PROTO] CFG_END no ACK or failed (attempt {attempt})")

        log.error(f"[LORA_PROTO] Config transfer {transfer_id} failed after {_MAX_RETRIES} attempts")
        self.cfg_progress["phase"]   = "failed"
        self.cfg_progress["message"] = "Couldn't reach the device"
        return False

    # ------------------------------------------------------------------
    # Incoming message listener (run as async task)
    # ------------------------------------------------------------------

    async def listen_task(self):
        log.info("[LORA_PROTO] Listener started")
        while True:
            try:
                # Cooperative pause while a register operation is in flight.
                # See lora_transport.config_in_progress + comms/lora_config.py.
                if lora_transport.config_in_progress:
                    await asyncio.sleep_ms(50)
                    continue
                raw = lora_transport.recv()
                if raw:
                    # The transport stripped the trailing RSSI byte the E220
                    # appends and stashed it on lora_transport.last_rssi_dbm.
                    # Surface it to the protocol layer so handlers (e.g. the
                    # leaf's HB task) can include it in payloads, and so the
                    # coordinator's fleet_manager can record it per-frame.
                    self.last_rx_rssi = lora_transport.last_rssi_dbm
                    log.debug(f"[LORA_PROTO] Received {len(raw)}B, RSSI={self.last_rx_rssi}dBm")
                    log.debug(f"[LORA_PROTO] Raw data: {raw}")
                    self._dispatch(raw)
                self._check_pending_acks()
            except Exception as e:
                log.error(f"[LORA_PROTO] Listener error: {e}")
            await asyncio.sleep_ms(50)

    def _dispatch(self, raw):
        try:
            text = raw.decode("utf-8", "ignore").strip()
            if not text:
                return
                
            # If the hardware is unprovisioned, lora_transport mistakenly strips
            # the last byte of our JSON (the '}') thinking it's an RSSI byte.
            # We can detect this and safely recover it!
            if not text.endswith("}"):
                text += "}"
                
            # Find JSON boundaries — UART may deliver partial/concatenated frames
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start < 0 or end <= start:
                return
            msg = json.loads(text[start:end])
        except Exception as e:
            log.error(f"[LORA_PROTO] Parse error: {e}")
            return

        msg_type = msg.get("t")
        src      = msg.get("s", 0)
        dest     = msg.get("d", 0)
        seq      = msg.get("seq", 0)
        payload  = msg.get("p", {})

        # Defence-in-depth destination filter. The E220 now runs in FIXED
        # mode and hardware-filters by ADDR (coord at 0xFFFF accepts all,
        # leaves only their own + 0xFFFF broadcasts), so most off-target
        # frames are dropped before reaching the UART. This check still
        # catches the broadcast-to-everyone-but-app-cares-only-about-N
        # case and is cheap.
        if msg_type != ACK and dest != self._unit_id and dest != _BROADCAST and dest != 0xFFFF:
            return

        log.debug(f"[LORA_PROTO] RX {msg_type} from {src} → {dest} seq={seq}")

        # Handle ACKs internally — resolve pending
        if msg_type == ACK:
            ack_seq = payload.get("ack_seq")
            if ack_seq in self._pending:
                # Store the whole payload in the pending entry so we can read 
                # custom fields like missing chunks
                self._pending[ack_seq]["ack_payload"] = payload
                ok = payload.get("ok", True)
                log.debug(f"[LORA_PROTO] ACK seq={ack_seq} ok={ok}")
                # We do NOT delete it here if ok is False and we want to process it,
                # wait, _wait_ack checks if seq is NOT in pending.
                # If we delete it, _wait_ack returns True immediately!
                # Instead of deleting, let's mark it as resolved.
                self._pending[ack_seq]["resolved"] = True
            return

        # Send ACK for messages that require it
        if msg_type in _ACK_REQUIRED and msg_type != CFG_END:
            self.send(ACK, src, {"ack_seq": seq, "ok": True})

        handler = self._handlers.get(msg_type)
        if handler:
            try:
                # Inject seq into payload so handlers can manually ACK
                payload["_seq"] = seq
                handler(src, payload)
            except Exception as e:
                log.error(f"[LORA_PROTO] Handler error for {msg_type}: {e}")

    # ------------------------------------------------------------------
    # ACK tracking
    # ------------------------------------------------------------------

    async def _wait_ack(self, seq, timeout_s=_ACK_TIMEOUT_S):
        if seq is None:
            return None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            entry = self._pending.get(seq)
            if not entry or entry.get("resolved"):
                payload = entry.get("ack_payload", {}) if entry else {"ok": True}
                if entry:
                    del self._pending[seq]
                return payload
            await asyncio.sleep_ms(100)
        # Timeout — remove from pending
        self._pending.pop(seq, None)
        return None

    def _check_pending_acks(self):
        now = time.time()

        # First sweep: drop entries whose ACK already came in but which
        # were never popped (because the caller used a fire-and-forget
        # send path — e.g. send_manual_override_batched — rather than
        # _wait_ack()). Without this, we retry a message that the peer
        # has already acknowledged: an attempt=1/2/3 warning storm in
        # the logs and unnecessary LoRa airtime, exactly the symptom
        # observed after the MO/HB-flood fix.
        resolved_seqs = [s for s, v in self._pending.items()
                         if v.get("resolved")]
        for seq in resolved_seqs:
            del self._pending[seq]

        # Second sweep: real timeouts.
        expired = [s for s, v in self._pending.items()
                   if now - v["sent_at"] > _ACK_TIMEOUT_S]
        for seq in expired:
            entry = self._pending.pop(seq)
            if entry["retries"] < _MAX_RETRIES - 1:
                entry["retries"] += 1
                entry["sent_at"] = now
                try:
                    raw = json.dumps(entry["msg"]).encode()
                    lora_transport.send(entry["dest"], raw)
                    self._pending[seq] = entry
                    log.warn(f"[LORA_PROTO] Retry seq={seq} attempt={entry['retries']}")
                except Exception as e:
                    log.error(f"[LORA_PROTO] Retry failed: {e}")
            else:
                log.error(f"[LORA_PROTO] seq={seq} unACKed after {_MAX_RETRIES} attempts")

    # ------------------------------------------------------------------
    # Convenience senders
    # ------------------------------------------------------------------

    def send_heartbeat(self, payload):
        self.send(HB, 0, payload)        # always to coordinator

    def send_pir_event(self, pir_id, state):
        self.send(PIR_EV, 0, {"id": pir_id, "state": state})

    def send_error(self, level, msg, ts=None, src_seq=None):
        """Forward a WARN/ERROR/FATAL line from this unit's event bus to the
        coordinator. `level` is the severity, `msg` is the log line, `ts` is the
        event's local epoch, `src_seq` is the event's local sequence number
        (useful for the coord-side dedup window). Truncated to fit the LoRa
        payload budget by the registered ERR fitter — see lora_protocol.fitter."""
        payload = {"lvl": level, "msg": msg}
        if ts is not None:
            payload["ts"] = ts
        if src_seq is not None:
            payload["sq"] = src_seq
        self.send(ERR, 0, payload)

    def broadcast_time_sync(self, epoch, tz_offset):
        self.send(TS, _BROADCAST, {"epoch": epoch, "tz": tz_offset})

    def send_scene(self, dest, scene_name):
        return self.send(SC, dest, {"scene": scene_name})

    def send_manual_override(self, dest, channels, relays, revert_s=0, fade_ms=0):
        """Send ONE MO packet. Assumes the caller already fit the payload — use
        send_manual_override_batched() for arbitrary-sized overrides.
        fade_ms is applied uniformly to every channel in this packet (the leaf
        reads it via payload.get('fade_ms', 0))."""
        payload = {
            "ch": channels,
            "rl": relays,
            "revert_s": revert_s,
        }
        # Only include fade_ms when non-zero so we don't grow the envelope
        # in the common case. The leaf's handler defaults to 0 anyway.
        if fade_ms:
            payload["fade_ms"] = fade_ms
        return self.send(MO, dest, payload)

    async def send_manual_override_batched(self, dest, channels, relays, revert_s=0, fade_ms=0):
        """Splittable manual-override sender. Packs as many [id, value] pairs
        as the LoRa byte budget allows per packet, sends sequentially with a
        small inter-packet gap so the receiver's UART can drain. Returns True
        iff every packet was accepted by send() (i.e. fit and transmitted).

        fade_ms is forwarded to the leaf on each packet so the leaf-side
        arbiter actually fades. Bug from before this fix: fade_ms was
        accepted by the API but dropped on the LoRa wire, so any leaf
        override happened instantly regardless of the slider setting.

        Why this lives here, not in api_handlers: the 200B cap is a transport
        property. Application code shouldn't need to know about it."""
        # Empty override (revert_s broadcast, e.g. revert_s=-1 to clear).
        if not channels and not relays:
            seq = self.send_manual_override(dest, [], [], revert_s, fade_ms)
            return seq is not None

        # Budget the items per packet. Base payload skeleton without any items
        # establishes the floor; each item adds a small, bounded delta. We
        # build greedily and flush whenever adding the next item would push the
        # serialized payload past the budget.
        ch_q = list(channels)
        rl_q = list(relays)

        def fits(buf_ch, buf_rl):
            probe = {"ch": buf_ch, "rl": buf_rl, "revert_s": revert_s}
            if fade_ms:
                probe["fade_ms"] = fade_ms
            return len(json.dumps(probe).encode()) <= _MAX_PAYLOAD_BYTES

        any_sent = False
        while ch_q or rl_q:
            buf_ch, buf_rl = [], []
            # Greedy: drain channels first, then relays, into this packet.
            while ch_q:
                trial_ch = buf_ch + [ch_q[0]]
                if fits(trial_ch, buf_rl):
                    buf_ch = trial_ch
                    ch_q.pop(0)
                else:
                    break
            while rl_q:
                trial_rl = buf_rl + [rl_q[0]]
                if fits(buf_ch, trial_rl):
                    buf_rl = trial_rl
                    rl_q.pop(0)
                else:
                    break

            if not buf_ch and not buf_rl:
                # A single item didn't fit the budget — impossible for normal
                # [int, int] pairs (each ≈ 8B vs ~150B budget) but guard anyway.
                log.error(
                    f"[LORA_PROTO] MO: single item exceeds {_MAX_PAYLOAD_BYTES}B "
                    f"payload budget; dropping the rest"
                )
                return False

            seq = self.send_manual_override(dest, buf_ch, buf_rl, revert_s, fade_ms)
            if seq is None:
                return False
            any_sent = True
            if ch_q or rl_q:
                await asyncio.sleep_ms(300)  # let UART/transport breathe
        return any_sent

    def request_status(self, dest):
        self.send(SR, dest)

    def send_emergency_off(self, dest):
        return self.send(EO, dest)

    def send_patch(self, dest, path, value):
        """Single-packet config patch (incremental config protocol).

        Used by the dashboard's inline-edit flow on the coord: small
        field tweaks (default_duty_percent, name, vacancy_timeout_s,
        etc.) go through here, ~100 B in one LoRa packet, ~300 ms
        round-trip including ACK — vs ~6 s for a full config push
        via send_config(). Caller is responsible for checking the
        serialized payload fits the per-packet budget before calling
        (smart-dispatch logic on the coord falls back to chunked
        send_config(target_path=...) when it doesn't).

        ACK is required: the leaf validates the merged config against
        the schema BEFORE applying, so ACK ok=False with a `reason`
        field is the way the leaf reports a validation failure.
        """
        return self.send(CFG_PATCH, dest, {"path": path, "value": value})

    def send_blink(self, dest, target_uid=None):
        """Tell a leaf (or all leaves with matching target_uid) to flash
        their status LED so the operator can physically identify which
        board they're about to claim. Fire-and-forget — no ACK."""
        payload = {}
        if target_uid:
            payload["target_uid"] = target_uid
        return self.send(BLINK, dest, payload)


lora_protocol = LoRaProtocol()
