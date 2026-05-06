import asyncio
import binascii
import json
import time
from comms.lora_transport import lora_transport, LoRaTimeoutError
from core.config_manager import config_manager
from shared.hmac_sha256 import hmac_sha256
from shared.secrets_loader import get_lora_key
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
EO        = "EO"          # Emergency Off — all outputs to zero

# ACK required for these types
_ACK_REQUIRED = {SC, MO, EO, CFG_END}

_ACK_TIMEOUT_S  = 10
_CHUNK_SIZE     = 64
_CHUNK_DELAY_MS = 200
_MAX_RETRIES    = 3
_BROADCAST      = 255

# E220-900T22D maximum payload per packet. Chunked transfers handle larger.
_MAX_PACKET_BYTES = 200

# HMAC parameters. Truncated to 8 bytes — gives ~2^-64 forgery prob per attempt.
# Hex-encoded the tag is 16 chars; envelope overhead with quotes/key is ~24 B.
_MAC_BYTES = 8
_MAC_FIELD = "mac"

# Replay window. We track the highest seq we've seen from each source. Frames
# with seq <= last_seq are dropped UNLESS we detect rollover (last seq was near
# 255 and incoming seq is small). The 8-bit seq rolls every 256 messages, so
# we must allow that without re-opening the door to true replays. Use a
# window of WINDOW after rollover: anything in (last_high .. 255] OR [0 ..
# WINDOW) is acceptable for one tick following last_seq > 255-WINDOW.
_REPLAY_WINDOW = 16


def _ct_eq(a, b):
    """Constant-time bytes comparison.

    Avoids timing oracle on the HMAC check. Pure-Python on MicroPython is
    slow enough that the timing leak is dwarfed by everything else, but it
    costs nothing to do this right.
    """
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


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
        self._pending  = {}          # {seq: {msg, sent_at, retries}}
        self._unit_id  = 0
        # RSSI of the most recent received packet in dBm (signed int) or None.
        # Populated by the transport when E220 RSSI-byte append is enabled
        # (see TODO in lora-protocol.md). Leaves include this in HB so the
        # coordinator can show link quality on the dashboard.
        self.last_rx_rssi = None
        # Network key (bytes) loaded from /secrets.json, or None if unsigned
        # mode. When set, every outbound frame carries an HMAC tag and every
        # inbound frame is verified before dispatch.
        self._key = None
        # Replay protection: highest seq seen per source unit_id.
        # {src_id: last_seq_int}. Updated only on successfully verified frames.
        self._last_seq = {}

    def init(self):
        self._unit_id = config_manager.unit_id
        self._key = get_lora_key()
        if self._key:
            log.info(f"[LORA_PROTO] Network key loaded ({len(self._key)} B) — frames will be HMAC-signed")
        else:
            log.warn("[LORA_PROTO] No network key — running unsigned. Add secrets.json to enable.")
        lora_transport.init()
        log.info(f"[LORA_PROTO] Init, unit_id={self._unit_id}")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on(self, msg_type, handler):
        """Register handler: handler(src_id, payload_dict)"""
        self._handlers[msg_type] = handler

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, msg_type, dest, payload=None):
        self._seq = (self._seq + 1) & 0xFF
        seq = self._seq
        envelope = {"s": self._unit_id, "d": dest, "t": msg_type, "seq": seq}
        if payload:
            envelope["p"] = payload

        # HMAC-sign before serialising the final wire frame. The tag covers
        # the canonical envelope WITHOUT the "mac" field so receiver and
        # sender can re-derive the same input deterministically.
        if self._key is not None:
            body = json.dumps(envelope).encode()
            tag = hmac_sha256(self._key, body)[:_MAC_BYTES]
            envelope[_MAC_FIELD] = binascii.hexlify(tag).decode()

        try:
            raw = json.dumps(envelope).encode()
            # CFG_CHUNK is pre-sized to fit; everything else must stay under the
            # E220 packet limit or the receiver will see a truncated, unparseable
            # frame. Drop here with a loud log rather than transmit garbage.
            if len(raw) > _MAX_PACKET_BYTES:
                log.error(
                    f"[LORA_PROTO] {msg_type} dropped: {len(raw)}B exceeds "
                    f"{_MAX_PACKET_BYTES}B limit"
                )
                return None
            lora_transport.send(
                dest if dest != _BROADCAST else 0xFFFF,
                raw
            )
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

    async def send_config(self, dest_id, config_str):
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
                 f"{len(chunks)} chunks → unit {dest_id}")

        for attempt in range(1, _MAX_RETRIES + 1):
            # CFG_START
            seq = self.send(CFG_START, dest_id, {
                "transfer_id": transfer_id,
                "total_chunks": len(chunks),
                "total_bytes": total,
            })
            ack = await self._wait_ack(seq)
            if not ack or not ack.get("ok", True):
                log.warn(f"[LORA_PROTO] CFG_START no ACK or rejected (attempt {attempt})")
                continue

            # CFG_CHUNKs
            ok = True
            for i, chunk in enumerate(chunks):
                self.send(CFG_CHUNK, dest_id, {
                    "transfer_id": transfer_id,
                    "chunk_index": i,
                    "data": chunk.decode("utf-8", "ignore"),
                })
                await asyncio.sleep_ms(_CHUNK_DELAY_MS)

            # CFG_END
            seq = self.send(CFG_END, dest_id, {
                "transfer_id": transfer_id,
                "checksum": checksum,
            })
            
            ack = await self._wait_ack(seq)
            if ack:
                if ack.get("ok", True):
                    log.info(f"[LORA_PROTO] Config transfer {transfer_id} complete")
                    return True
                    
                # The leaf rejected the checksum. Did it tell us which chunks are missing?
                missing = ack.get("missing")
                if missing and isinstance(missing, list) and len(missing) > 0:
                    log.warn(f"[LORA_PROTO] CFG_END rejected. Leaf is missing {len(missing)} chunks. Retrying only missing chunks...")
                    # We have a smart retry! Instead of aborting to the outer loop,
                    # we can just re-send the missing chunks right now and jump back
                    # to the CFG_END stage!
                    # Wait, we need to do this carefully without breaking the attempt loop.
                    # Let's just update `chunks` to only contain the missing ones?
                    # No, we can't because the indices would change.
                    for attempt_missing in range(3):
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
                        seq = self.send(CFG_END, dest_id, {
                            "transfer_id": transfer_id,
                            "checksum": checksum,
                        })
                        ack = await self._wait_ack(seq)
                        if ack and ack.get("ok", True):
                            log.info(f"[LORA_PROTO] Config transfer {transfer_id} complete after smart retry")
                            return True
                        missing = ack.get("missing") if ack else None
                        if not missing:
                            break # fallback to full retry
            
            log.warn(f"[LORA_PROTO] CFG_END no ACK or failed (attempt {attempt})")

        log.error(f"[LORA_PROTO] Config transfer {transfer_id} failed after {_MAX_RETRIES} attempts")
        return False

    # ------------------------------------------------------------------
    # Incoming message listener (run as async task)
    # ------------------------------------------------------------------

    async def listen_task(self):
        log.info("[LORA_PROTO] Listener started")
        while True:
            try:
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

        # ---- HMAC verification ----
        # When a network key is configured, every accepted frame must carry a
        # valid tag. Frames without a tag, with a malformed tag, or with a tag
        # that doesn't match are silently dropped — the only signal we leak
        # to a would-be attacker is "your frame did nothing", which is the
        # whole point.
        if self._key is not None:
            mac_hex = msg.pop(_MAC_FIELD, None)
            if not mac_hex:
                log.debug("[LORA_PROTO] dropped unsigned frame")
                return
            try:
                given = binascii.unhexlify(mac_hex)
            except Exception:
                log.debug("[LORA_PROTO] dropped frame with malformed mac")
                return
            body = json.dumps(msg).encode()
            expected = hmac_sha256(self._key, body)[:_MAC_BYTES]
            if not _ct_eq(expected, given):
                log.debug(f"[LORA_PROTO] dropped frame with bad mac from {msg.get('s')}")
                return

        msg_type = msg.get("t")
        src      = msg.get("s", 0)
        dest     = msg.get("d", 0)
        seq      = msg.get("seq", 0)
        payload  = msg.get("p", {})

<<<<<<< HEAD
        # Application-layer destination filtering. The E220 modules run in
        # transparent mode (no hardware-level address filter), so every unit
        # on the same channel sees every packet. Drop frames not addressed
        # to us — except broadcasts (dest == _BROADCAST or 0xFFFF) and our
        # own ACKs (which can come from anywhere).
        if msg_type != ACK and dest != self._unit_id and dest != _BROADCAST and dest != 0xFFFF:
            return

        log.debug(f"[LORA_PROTO] RX {msg_type} from {src} → {dest} seq={seq}")
=======
        # ---- Replay protection ----
        # Once a frame is authenticated, refuse to process it twice. The seq
        # is 8-bit so we accept rollover: a small seq following a near-255
        # last_seq is treated as a fresh increment, not a replay.
        if not self._seq_is_fresh(src, seq):
            log.debug(f"[LORA_PROTO] dropped replay {msg_type} from {src} seq={seq}")
            return
        self._last_seq[src] = seq

        log.debug(f"[LORA_PROTO] RX {msg_type} from {src} seq={seq}")
>>>>>>> f10194a (Feature: Network HMAC authentication + replay protection for LoRa)

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

    def _seq_is_fresh(self, src, seq):
        """Return True if `seq` from `src` should be accepted as new.

        Rolling 8-bit window: accept seq strictly greater than last_seen, OR
        a small seq when last_seen was near 255 (rollover). Reject everything
        else.
        """
        if not isinstance(seq, int) or seq < 0 or seq > 255:
            return False
        last = self._last_seq.get(src)
        if last is None:
            return True
        if seq > last:
            return True
        # Rollover: last was near top, incoming small.
        if last >= 256 - _REPLAY_WINDOW and seq < _REPLAY_WINDOW:
            return True
        return False

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
        now     = time.time()
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

    def send_error(self, code, msg):
        self.send(ERR, 0, {"code": code, "msg": msg})

    def broadcast_time_sync(self, epoch, tz_offset):
        self.send(TS, _BROADCAST, {"epoch": epoch, "tz": tz_offset})

    def send_scene(self, dest, scene_name):
        return self.send(SC, dest, {"scene": scene_name})

    def send_manual_override(self, dest, channels, relays, revert_s=0):
        return self.send(MO, dest, {
            "ch": channels,
            "rl": relays,
            "revert_s": revert_s,
        })

    def request_status(self, dest):
        self.send(SR, dest)

    def send_emergency_off(self, dest):
        return self.send(EO, dest)


lora_protocol = LoRaProtocol()
