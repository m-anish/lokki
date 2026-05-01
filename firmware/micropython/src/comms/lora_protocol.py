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
EO        = "EO"          # Emergency Off — all outputs to zero

# ACK required for these types
_ACK_REQUIRED = {SC, MO, EO, CFG_END}

_ACK_TIMEOUT_S  = 10
_CHUNK_SIZE     = 150
_CHUNK_DELAY_MS = 200
_MAX_RETRIES    = 3
_BROADCAST      = 255


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

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, msg_type, dest, payload=None):
        self._seq = (self._seq + 1) & 0xFF
        seq = self._seq
        envelope = {"s": self._unit_id, "d": dest, "t": msg_type, "seq": seq}
        if payload:
            envelope["p"] = payload

        try:
            raw = json.dumps(envelope).encode()
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
            if not await self._wait_ack(seq):
                log.warn(f"[LORA_PROTO] CFG_START no ACK (attempt {attempt})")
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
            if await self._wait_ack(seq):
                log.info(f"[LORA_PROTO] Config transfer {transfer_id} complete")
                return True
            log.warn(f"[LORA_PROTO] CFG_END no ACK (attempt {attempt})")

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
        seq      = msg.get("seq", 0)
        payload  = msg.get("p", {})

        log.debug(f"[LORA_PROTO] RX {msg_type} from {src} seq={seq}")

        # Handle ACKs internally — resolve pending
        if msg_type == ACK:
            ack_seq = payload.get("ack_seq")
            if ack_seq in self._pending:
                ok = payload.get("ok", True)
                log.debug(f"[LORA_PROTO] ACK seq={ack_seq} ok={ok}")
                del self._pending[ack_seq]
            return

        # Send ACK for messages that require it
        if msg_type in _ACK_REQUIRED:
            self.send(ACK, src, {"ack_seq": seq, "ok": True})

        handler = self._handlers.get(msg_type)
        if handler:
            try:
                handler(src, payload)
            except Exception as e:
                log.error(f"[LORA_PROTO] Handler error for {msg_type}: {e}")

    # ------------------------------------------------------------------
    # ACK tracking
    # ------------------------------------------------------------------

    async def _wait_ack(self, seq, timeout_s=_ACK_TIMEOUT_S):
        if seq is None:
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if seq not in self._pending:
                return True     # ACK received and removed by _dispatch
            await asyncio.sleep_ms(100)
        # Timeout — remove from pending
        self._pending.pop(seq, None)
        return False

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
