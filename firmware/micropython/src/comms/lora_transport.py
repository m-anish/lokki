"""E220-900T22D LoRa transport layer.

Runtime philosophy: the firmware does NOT enter CONFIG mode at boot, does
NOT issue any register-mode commands, and does NOT bounce M0/M1. Modules
are provisioned ONCE via utils/e220_provisioner_cli.py (over a Pico-side
bridge that runs separately) and then live forever in their NVRAM-set
state.

Why: extensive hardware testing on this Pico-2 + E220-900T22D combination
showed that any UART command exchange in CONFIG mode (read OR write) wedges
the radio's RX path, even when written values are identical to the existing
state. Mode-pin bouncing alone is fine. The fix is to never enter CONFIG
mode at runtime — and the cost is zero: addressing happens in our own
JSON envelope (`s`/`d` fields), not at the hardware level.

What this transport DOES do:
  - Open UART0 at 9600 8N1 (matches the modules' provisioned UART rate)
  - Drive M0=0, M1=0 (NORMAL mode — transmit + receive)
  - Wait for AUX HIGH so we know the module is actually ready
  - Send raw payload bytes via uart.write()
  - Receive raw payload bytes via uart.read(), stripping the trailing
    RSSI byte that the module appends per provisioning (REG3 bit 7 = 1)

If a module hasn't been provisioned (still at factory defaults), the
trailing-byte strip will eat real payload data. So provisioning with
--rssi-byte is a hard requirement; documented at the deploy step.
"""

import time
from machine import UART, Pin
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

# Operating modes via M0/M1 — we only ever use NORMAL.
_MODE_NORMAL = (0, 0)

# AUX is the module's ready/busy line. HIGH = idle (radio listening). LOW
# = transmitting / receiving / processing. Before transmit we wait for AUX
# HIGH, otherwise we'd race the previous TX or an in-flight RX.
_AUX_POLL_MS    = 10
_AUX_TIMEOUT_S  = 5
_BAUD           = 9600
_BROADCAST_ADDR = 0xFFFF


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
        # Set True once UART + mode pins are up. There's no register-mode
        # round-trip to fail anymore, so this is purely a "did the
        # constructor run cleanly" flag.
        self.config_ok = False
        # RSSI of the most recently received frame, in dBm. None until we
        # actually receive something. Populated in recv() by stripping
        # the RSSI byte the module appends (when provisioned with REG3
        # bit 7 = 1).
        self.last_rssi_dbm = None

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
            # Channel is informational only — the module's actual channel
            # is whatever was set in NVRAM during provisioning. We log
            # the configured value so any mismatch with the provisioned
            # state is easy to spot.
            self._channel = lora.get("channel", 0)

            log.info(f"[LORA] AUX at boot = {self._aux.value()} "
                     f"(should be 1; if 0, module is busy or wired wrong)")

            # Drive M0=0, M1=0. That's all we ever do to the mode pins.
            self._m0.value(0)
            self._m1.value(0)
            time.sleep_ms(100)

            # Wait for AUX HIGH so we know the radio is actually idle and
            # listening before we try to transmit.
            if not self._wait_aux_settled(timeout_s=_AUX_TIMEOUT_S):
                log.warn("[LORA] AUX did not settle HIGH at boot — module "
                         "may be busy or unprovisioned. Continuing anyway.")
            else:
                log.debug("[LORA] AUX settled HIGH — radio is idle")

            self._ready    = True
            self.config_ok = True
            log.info(f"[LORA] Transport ready (NORMAL mode, configured channel={self._channel}, "
                     "module config from NVRAM — no runtime register writes)")

        except Exception as e:
            log.error(f"[LORA] Init failed: {e}")
            import sys
            sys.print_exception(e)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, dest_id, payload_bytes):
        """Transmit `payload_bytes`. `dest_id` is informational at the
        transport layer (modules are in transparent mode and the byte
        stream goes out as-is). It's surfaced here for symmetry with
        what the protocol layer expects.
        """
        if not self._ready:
            return False
        self._wait_aux()
        # Transparent mode: no [DESTH][DESTL][CHAN] header. Bytes go
        # straight to the air. Destination filtering happens at the
        # protocol layer via the JSON envelope's `d` field.
        self._uart.write(payload_bytes)
        
        # Give the E220 a moment to pull AUX LOW. At 9600 baud, the first byte
        # takes ~1ms to transmit over UART, after which the module drops AUX.
        # We poll briefly so back-to-back send()s don't concatenate packets in
        # the Pico's UART TX buffer (would exceed the module's 200B limit).
        #
        # The AUX-low pulse is short (a few ms), and on a busy CPU we routinely
        # *miss* the falling edge — by the time we look, AUX has already
        # cycled low-and-back-high. The old code waited a full second and
        # logged a WARN, flooding the notifications surface with cosmetic
        # noise on every missed edge (see investigation in the dashboard
        # observability work). The wait now caps at 150 ms (plenty for the
        # back-to-back saturation case we actually care about) and the
        # "didn't see AUX low" condition is logged at DEBUG instead of WARN
        # since it usually means we missed the pulse, not that the module
        # ignored the UART.
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

        Modules provisioned with REG3 bit 7 = 1 append one RSSI byte to
        every received frame. We strip it, decode `RSSI_dBm = -(256 - byte)`,
        and stash on `self.last_rssi_dbm` for the protocol layer to surface.

        IMPORTANT: this assumes the module *is* provisioned with
        --rssi-byte. If it's not, the trailing-byte strip eats real
        payload data and JSON parsing will fail.
        """
        if not self._ready or not self._uart.any():
            return None
            
        # The E220's AUX pin goes LOW while it is transmitting data to us over UART.
        # If we read immediately while AUX is LOW, we might read a partial packet
        # (and erroneously strip the last byte of the chunk as the RSSI byte).
        # We wait for AUX to go HIGH, indicating the module has finished its output.
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
            
        # Read the complete packet
        # Wait a tiny bit more just in case the last byte is still shifting in
        time.sleep_ms(2)
        
        # Read up to 256 bytes (default MicroPython UART RX buffer size, which fits
        # our 200B max payload + headers + RSSI byte).
        raw = self._uart.read(256)
        
        # We might have read in a while loop until everything is gathered, 
        # but waiting for AUX should ensure everything is in the RX buffer.
        # Let's see if there is more.
        while self._uart.any():
            more = self._uart.read(256)
            if more:
                raw += more
                
        if not raw:
            return None
        if len(raw) < 2:
            # One-byte frame is junk (probably noise). Drop.
            return None
            
        rssi_byte = raw[-1]
        self.last_rssi_dbm = -(256 - rssi_byte)
        return raw[:-1]

    def available(self):
        return self._ready and self._uart.any() > 0

    # ------------------------------------------------------------------
    # AUX discipline
    # ------------------------------------------------------------------

    def _wait_aux_settled(self, timeout_s):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._aux.value() == 1:
                return True
            time.sleep_ms(10)
        return False

    def _wait_aux(self):
        """Pre-transmit guard: AUX must be HIGH before we send. Raises
        LoRaTimeoutError if AUX stays LOW past _AUX_TIMEOUT_S — that
        usually means the module is wedged or the wire is broken."""
        deadline = time.time() + _AUX_TIMEOUT_S
        while self._aux.value() == 0:
            if time.time() > deadline:
                log.error(f"[LORA] AUX stuck LOW for {_AUX_TIMEOUT_S}s — "
                          "verify wiring and provisioning state")
                raise LoRaTimeoutError("AUX timeout — channel busy")
            time.sleep_ms(_AUX_POLL_MS)


lora_transport = LoRaTransport()
