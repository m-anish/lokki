"""E220-900T22D LoRa transport layer.

Runtime philosophy: program the E220's volatile registers at boot from
config.json (one source of truth), then operate in FIXED-mode for
hardware-directed addressing. NVRAM is left untouched — reboot
re-derives. This replaces the earlier bridge-only provisioning workflow,
which we kept around because we *thought* runtime register writes
wedged the RF path; extensive testing (tests/lora_e220_inband_test.py)
showed that was actually a sequencing bug in the old transport, not
silicon.

Address scheme (set per-unit in lora_config.apply_from_config):
  coord (unit_id == 0):  ADDR = 0xFFFF (monitor / sees everything)
  leaf  (unit_id  > 0):  ADDR = 0x00<id>

Wire framing in FIXED mode: every TX is prefixed by [DESTH, DESTL, CHAN]
which the module strips before air transmission and which the receiver
never sees. send() handles this prefixing transparently — callers pass
a logical `dest_id` (0..8 or 0xFF for broadcast).

RSSI byte: forced ON in REG3 bit 7. recv() unconditionally strips the
trailing byte and stashes the decoded dBm on last_rssi_dbm.
"""

import time
from machine import UART, Pin
from core.config_manager import config_manager
from shared.simple_logger import Logger
from comms import lora_config

log = Logger()

# AUX is the module's ready/busy line. HIGH = idle.
_AUX_POLL_MS    = 10
_AUX_TIMEOUT_S  = 5
_BAUD           = 9600          # Pico ↔ E220 UART rate. Matches PROGRAM-mode
                                # required baud so we never deinit/reinit
                                # the UART around register operations.
_BROADCAST_ADDR = 0xFF          # high-level "send to everyone"


class LoRaTimeoutError(Exception):
    pass


class LoRaTransport:

    def __init__(self):
        self._uart    = None
        self._m0      = None
        self._m1      = None
        self._aux     = None
        self._channel = 0
        self._ready   = False
        self.config_ok = False
        # RSSI of the most recently received frame in dBm. None until we
        # actually receive something. Populated by recv() from the
        # trailing byte the module appends (REG3 bit 7 = 1 enforced in
        # lora_config.build_register_payload).
        self.last_rssi_dbm = None
        # Cooperative "stop touching M0/M1 and the UART" flag. set by
        # lora_config when a register read/write is in flight; observed
        # by send() and the protocol-layer listen task. Cheaper than a
        # full asyncio.Lock and avoids propagating async-ness through
        # every caller in the codebase.
        self.config_in_progress = False
        # Live decoded view of the registers we last applied — exposed
        # via /api/lora-config for the dashboard.
        self.last_applied = None

    def init(self):
        log.info("[LORA] Starting initialization...")
        try:
            hw   = config_manager.get("hardware")
            lora = config_manager.get("lora")

            self._m0  = Pin(hw.get("lora_m0_pin",  2), Pin.OUT)
            self._m1  = Pin(hw.get("lora_m1_pin",  3), Pin.OUT)
            self._aux = Pin(hw.get("lora_aux_pin", 4), Pin.IN)

            uart_id = hw.get("lora_uart_id", 0)
            tx_pin  = hw.get("lora_tx_pin",  0)
            rx_pin  = hw.get("lora_rx_pin",  1)

            log.info(f"[LORA] UART{uart_id}, TX={tx_pin}, RX={rx_pin}, "
                     f"M0={hw.get('lora_m0_pin', 2)}, M1={hw.get('lora_m1_pin', 3)}, "
                     f"AUX={hw.get('lora_aux_pin', 4)}")

            self._uart = UART(uart_id, baudrate=_BAUD,
                              tx=Pin(tx_pin), rx=Pin(rx_pin))
            self._channel = int(lora.get("channel", 18))

            log.info(f"[LORA] AUX at boot = {self._aux.value()} "
                     f"(should be 1; if 0, module is busy or wired wrong)")

            # Start in NORMAL mode so the module is in a known state
            # before we attempt the register write.
            self._m0.value(0)
            self._m1.value(0)
            time.sleep_ms(100)

            if not self._wait_aux_settled(timeout_s=_AUX_TIMEOUT_S):
                log.warn("[LORA] AUX did not settle HIGH at boot — module "
                         "may be busy or wired wrong. Continuing anyway.")
            else:
                log.debug("[LORA] AUX settled HIGH — radio is idle")

            # Program the module's volatile registers from config.json.
            # FIXED mode + per-unit ADDR + RSSI byte ON. NVRAM is left
            # untouched; reboot re-derives. See lora_config.py for the
            # full payload composition.
            unit_id = config_manager.unit_id
            ok = lora_config.apply_from_config(self, unit_id, lora,
                                               persist=False)
            if ok:
                self.last_applied = lora_config.decode_register_payload(
                    lora_config.build_register_payload(unit_id, lora)
                )
                log.info(f"[LORA] Volatile registers programmed for unit {unit_id} "
                         f"(ADDR={self.last_applied['addr_hex']}, "
                         f"channel={self.last_applied['channel']}, "
                         f"air_rate={self.last_applied['air_rate']}, "
                         f"tx_power={self.last_applied['tx_power_dbm']} dBm)")
            else:
                log.error("[LORA] Failed to program volatile registers. "
                          "Module will run in whatever state NVRAM holds — "
                          "expect address/mode mismatch.")

            self._ready    = True
            self.config_ok = ok

        except Exception as e:
            log.error(f"[LORA] Init failed: {e}")
            import sys
            sys.print_exception(e)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, dest_id, payload_bytes):
        """Transmit `payload_bytes` to `dest_id`.

        `dest_id` semantics:
          0           → coord (DEST = 0x0000)
          1..8        → leaf with that unit_id (DEST = 0x00<id>)
          255 / 0xFFFF→ broadcast (DEST = 0xFFFF)

        In FIXED mode the module strips the 3-byte [DESTH, DESTL, CHAN]
        header before air transmission, so the receiver never sees it.
        """
        if not self._ready:
            return False
        if self.config_in_progress:
            # Don't touch the UART while a register op is in flight.
            # Caller (protocol layer) will retry on its next tick.
            return False

        destH, destL = self._encode_dest(dest_id)
        header = bytes([destH, destL, self._channel])

        self._wait_aux()
        self._uart.write(header + payload_bytes)

        # Brief AUX-low watch — see commit history for the rationale.
        deadline_ms = time.ticks_add(time.ticks_ms(), 150)
        while self._aux.value() == 1:
            if time.ticks_diff(deadline_ms, time.ticks_ms()) <= 0:
                log.debug("[LORA] AUX low edge not observed within 150ms after send "
                          "(likely missed the pulse; data was transmitted)")
                break
            time.sleep_ms(1)
        return True

    def recv(self):
        """Return application-payload bytes from the UART buffer, or None.

        The trailing RSSI byte (REG3 bit 7 = 1, enforced at init) is
        stripped and decoded into last_rssi_dbm. FIXED-mode RX does NOT
        include the destination header — the module strips it before
        forwarding to UART.
        """
        if not self._ready or not self._uart.any():
            return None
        if self.config_in_progress:
            return None

        # AUX is LOW while the module forwards data to UART. Read after
        # AUX HIGH to ensure we have the complete frame.
        deadline = time.time() + 2
        waited = False
        while self._aux.value() == 0:
            waited = True
            if time.time() > deadline:
                log.warn("[LORA] AUX timeout while receiving")
                break
            time.sleep_ms(2)

        if waited:
            log.debug("[LORA] recv waited for AUX HIGH to ensure complete packet")

        time.sleep_ms(2)
        raw = self._uart.read(256)
        while self._uart.any():
            more = self._uart.read(256)
            if more:
                raw += more

        if not raw or len(raw) < 2:
            return None

        rssi_byte = raw[-1]
        self.last_rssi_dbm = -(256 - rssi_byte)
        return raw[:-1]

    def available(self):
        return self._ready and self._uart.any() > 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _encode_dest(self, dest_id):
        """Map logical dest_id → (DESTH, DESTL) per the project's address
        scheme. Anything that looks like a broadcast → 0xFFFF."""
        if dest_id is None or dest_id == _BROADCAST_ADDR or dest_id == 0xFFFF:
            return (0xFF, 0xFF)
        return (0x00, int(dest_id) & 0xFF)

    def _wait_aux_settled(self, timeout_s):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._aux.value() == 1:
                return True
            time.sleep_ms(10)
        return False

    def _wait_aux(self):
        deadline = time.time() + _AUX_TIMEOUT_S
        while self._aux.value() == 0:
            if time.time() > deadline:
                log.error(f"[LORA] AUX stuck LOW for {_AUX_TIMEOUT_S}s — "
                          "verify wiring and register state")
                raise LoRaTimeoutError("AUX timeout — channel busy")
            time.sleep_ms(_AUX_POLL_MS)


lora_transport = LoRaTransport()
