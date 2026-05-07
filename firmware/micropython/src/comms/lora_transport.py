import time
from machine import UART, Pin
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

# -----------------------------------------------------------------------------
# EBYTE E220-900T22D register-mode driver.
#
# These modules do NOT speak AT commands. Earlier code did, which silently
# left the modules in factory defaults (transparent transmission, channel 23,
# fresh address) — explaining why nothing on the wire ever made it through.
#
# Configuration protocol (in sleep mode M0=1, M1=1):
#   Host → module:   [0xC0] [start_reg] [length] [reg0..regN]   write to NVRAM
#                    [0xC2] [start_reg] [length] [reg0..regN]   write volatile
#                    [0xC1] [start_reg] [length]                read
#   Module → host:   [0xC1] [start_reg] [length] [reg0..regN]   reply with current values
#
# Register layout (E220-900T22D, datasheet v1.x):
#   0x00 ADDH       address high byte
#   0x01 ADDL       address low byte
#   0x02 NETID      network id (0..255), peers must match
#   0x03 REG0       UART baud [7:5] | parity [4:3] | air rate [2:0]
#   0x04 REG1       sub-packet [7:6] | RSSI ambient [5] | reserved | TX power [1:0]
#   0x05 REG2       channel (frequency = 850.125 + REG2 MHz, range 0..80)
#   0x06 REG3       RSSI byte append [7] | transmit method [6] | reserved | LBT | WOR
#   0x07 CRYPT_H    AES-key high byte
#   0x08 CRYPT_L    AES-key low byte
#
# After a frame is received in fixed-point mode WITH RSSI byte enabled, the
# module appends one trailing byte: rssi_byte. RSSI_dBm = -(256 - rssi_byte).
# -----------------------------------------------------------------------------

_MODE_NORMAL = (0, 0)   # transmit/receive
_MODE_CONFIG = (1, 1)   # M0=M1=1 → sleep / register-mode config
# Keep the old name for any external import — they're aliases now.
_MODE_SLEEP  = _MODE_CONFIG

_AUX_POLL_MS   = 10
_AUX_TIMEOUT_S = 5
_MODE_DELAY_MS = 250
_BAUD          = 9600
_BROADCAST_ADDR = 0xFFFF

# Cold-boot reply latency on the E220 has been observed at ~1 s — bump the
# read window well past that. Retry a few times if the very first attempt
# misses; observed pattern: first boot times out, second succeeds.
_CONFIG_REPLY_TIMEOUT_MS = 1000
_CONFIG_WRITE_RETRIES    = 3

# Register-mode commands.
# We use 0xC2 (volatile / RAM-only) by design. _configure() is called on every
# boot so persisting to NVRAM (0xC0) buys nothing, costs flash wear, and on
# some E220 firmware variants leaves AUX stuck LOW for a noticeable interval
# after the commit — manifesting as runtime "AUX timeout" errors that look
# like the radio link is dead.
_CMD_SET_NV  = 0xC0  # write to non-volatile memory (persists across power cycles)
_CMD_READ    = 0xC1
_CMD_SET_VOL = 0xC2  # volatile, lost on power cycle — preferred for our use
_CMD_REPLY   = 0xC1  # module's reply prefix to a successful set/get

# Register addresses
_REG_ADDH    = 0x00
_REG_NETID   = 0x02
_REG0_OFF    = 0x03
_REG3_OFF    = 0x06

# REG0: 9600 baud (0b011) | 8N1 (0b00) | 2.4kbps air rate (0b010) → 0x62
# 2.4kbps gives the longest range; faster rates trade range for throughput.
_REG0_BYTE   = 0b01100010

# REG1 base: sub-packet 200B (0b00) | ambient RSSI off (0b0) | reserved (0b00)
# TX power bits [1:0] are filled in at runtime from lora.tx_power_dbm.
_REG1_BASE   = 0b00000000
_TX_POWER_BITS = {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}

# REG3: bit7=1 → append RSSI byte to every received packet
#       bit6=1 → fixed-point transmission (sender prepends [ADDH][ADDL][CHAN])
#       Everything else off.
_REG3_BYTE   = 0b11000000  # 0xC0

# Channel arithmetic. The E220-900T22D covers 850.125 MHz + (channel × 1 MHz),
# channels 0..80. We derive from lora.frequency_mhz.
_CHAN_BASE_MHZ = 850


class LoRaTimeoutError(Exception):
    pass


def _freq_mhz_to_channel(freq_mhz):
    """Map an MHz centre frequency to the E220's channel index."""
    ch = int(round(freq_mhz - _CHAN_BASE_MHZ))
    return max(0, min(80, ch))


def _aux_pin_label(pin):
    """Best-effort label for a Pin object for diagnostic messages.
    MicroPython exposes the GPIO number in str(pin) like 'Pin(GPIO4, ...)'."""
    try:
        s = str(pin)
        # Snip out "GPIO<n>" if it's there.
        i = s.find("GPIO")
        if i >= 0:
            return s[i+4:].split(",")[0].strip(" )")
    except Exception:
        pass
    return "?"


class LoRaTransport:

    def __init__(self):
        self._uart   = None
        self._m0     = None
        self._m1     = None
        self._aux    = None
        self._channel = 0
        self._ready  = False
        # RSSI of the most recently received frame, in dBm. None if either
        # nothing has been received yet or RSSI byte append is disabled.
        # Populated in recv() by stripping the trailing byte the E220 appends.
        self.last_rssi_dbm = None

    def init(self):
        log.info("[LORA] Starting initialization...")
        try:
            hw   = config_manager.get("hardware")
            lora = config_manager.get("lora")

            self._m0  = Pin(hw.get("lora_m0_pin",  2), Pin.OUT)
            self._m1  = Pin(hw.get("lora_m1_pin",  3), Pin.OUT)
            self._aux = Pin(hw.get("lora_aux_pin",  4), Pin.IN)

            uart_id = hw.get("lora_uart_id", 0)
            tx_pin  = hw.get("lora_tx_pin",  0)
            rx_pin  = hw.get("lora_rx_pin",  1)

            log.info(f"[LORA] UART{uart_id}, TX={tx_pin}, RX={rx_pin}, M0={hw.get('lora_m0_pin',2)}, M1={hw.get('lora_m1_pin',3)}, AUX={hw.get('lora_aux_pin',4)}")

            self._uart = UART(uart_id, baudrate=_BAUD,
                             tx=Pin(tx_pin), rx=Pin(rx_pin))
            self._channel = lora.get("channel", 0)

            # Sample AUX before doing anything. With the module powered and
            # not yet commanded, AUX should be HIGH (idle). If it reads 0
            # here, the pin is either floating (read as 0 with no pull-up)
            # or shorted/wired wrong — every subsequent timeout will trace
            # back to this. Worth shouting about up front.
            log.info(f"[LORA] Pin readback at boot: AUX={self._aux.value()} "
                     f"(should be 1; if 0, check wiring on GP{hw.get('lora_aux_pin',4)})")

            self._configure(lora)
            self._ready = True
            log.info("[LORA] Transport ready")
        except Exception as e:
            log.error(f"[LORA] Init failed: {e}")
            import sys
            sys.print_exception(e)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, dest_id, payload_bytes):
        """Send payload to dest_id (0–8) or 0xFFFF for broadcast."""
        if not self._ready:
            return False
        self._wait_aux()
        if dest_id == _BROADCAST_ADDR:
            addh, addl = 0xFF, 0xFF
        else:
            addh, addl = 0x00, dest_id & 0xFF
        header = bytes([addh, addl, self._channel])
        self._uart.write(header + payload_bytes)
        return True

    def recv(self):
        """Return application-payload bytes from the UART buffer, or None if empty.

        With RSSI byte append enabled (bit 7 of REG3), the E220 tacks one byte
        of RSSI onto the end of every received frame. We strip it, decode it
        (`RSSI_dBm = -(256 - byte)`), and stash on `self.last_rssi_dbm` for
        the protocol layer to surface upstream.

        Note: this assumes one frame per UART read. With the E220's 200-byte
        sub-packet and our protocol's strict <200B envelope cap, that holds —
        the module never delivers two frames concatenated to a single read.
        """
        if not self._ready or not self._uart.any():
            return None
        raw = self._uart.read(256)
        if not raw:
            return None
        if len(raw) >= 2:
            # Last byte is RSSI; strip it from what the protocol layer sees.
            rssi_byte = raw[-1]
            self.last_rssi_dbm = -(256 - rssi_byte)
            return raw[:-1]
        # Single-byte frame is junk (probably a stray noise byte) — drop.
        return None

    def available(self):
        return self._ready and self._uart.any() > 0

    # ------------------------------------------------------------------
    # E220 configuration (register-mode binary protocol, NOT AT)
    # ------------------------------------------------------------------

    def _configure(self, lora_cfg):
        unit_id  = config_manager.unit_id
        freq_mhz = lora_cfg.get("frequency_mhz", 868)
        tx_power = lora_cfg.get("tx_power_dbm", 22)
        channel  = _freq_mhz_to_channel(freq_mhz)
        # Cache for the runtime header prepend — see send().
        self._channel = channel

        # Build REG1 with the requested TX power.
        tx_bits = _TX_POWER_BITS.get(tx_power, 0b00)
        reg1_byte = (_REG1_BASE & ~0b11) | tx_bits

        log.debug(f"[LORA] Configuring (register-mode): unit_id={unit_id} "
                  f"freq={freq_mhz}MHz → ch={channel} tx={tx_power}dBm")

        # Enter config mode (M0=1, M1=1) and let the module settle.
        self._set_mode(*_MODE_CONFIG)
        time.sleep_ms(_MODE_DELAY_MS)
        # Drain any leftover bytes from the UART — keeps stale data from
        # earlier modes out of the reply parsing.
        for _ in range(3):
            self._uart.read()
            time.sleep_ms(50)

        # Build the 10-byte parameter write covering registers 0x00..0x06:
        #   [CMD_SET_VOL] [start=0x00] [len=7] [ADDH ADDL NETID REG0 REG1 REG2 REG3]
        # Volatile (0xC2): config takes effect immediately, no flash latency,
        # no NVRAM wear. We re-configure on every boot anyway.
        addh = (unit_id >> 8) & 0xFF
        addl = unit_id & 0xFF
        cmd = bytes([
            _CMD_SET_VOL,
            _REG_ADDH,
            7,
            addh, addl,
            0,                # NETID
            _REG0_BYTE,
            reg1_byte,
            channel,
            _REG3_BYTE,
        ])

        # Log AUX/M0/M1 state up front so wiring problems are obvious. If
        # AUX reads LOW immediately after switching to config mode, the leaf
        # is either not actually wired or the module isn't responding.
        log.debug(f"[LORA] Pre-write state: AUX={self._aux.value()} M0={self._m0.value()} M1={self._m1.value()}")
        if not self._wait_aux_settled(timeout_s=2):
            log.warn("[LORA] AUX did not settle HIGH within 2s before config write. "
                     "Possible causes: (a) AUX not wired to the configured GPIO, "
                     "(b) M0/M1 not wired so the module never entered config mode, "
                     "(c) module power/ground problem. Continuing anyway.")

        # Retry the register write a few times. The first reply after a cold
        # power-on can take ≈1 s (observed on hardware) and occasionally goes
        # missing entirely — drain races with stray UART noise from the
        # mode-switch window. Re-issuing the same write is safe (idempotent
        # for 0xC2) and almost always succeeds on the second attempt.
        resp = b""
        for attempt in range(1, _CONFIG_WRITE_RETRIES + 1):
            log.debug(f"[LORA] TX config (attempt {attempt}/{_CONFIG_WRITE_RETRIES}): "
                      + " ".join("{:02x}".format(b) for b in cmd))
            self._uart.write(cmd)
            resp = self._read_with_timeout(expected_len=len(cmd),
                                           timeout_ms=_CONFIG_REPLY_TIMEOUT_MS)
            if resp:
                break
            log.warn(f"[LORA] No reply to register write (attempt {attempt}). "
                     "Draining UART and retrying.")
            for _ in range(2):
                self._uart.read()
                time.sleep_ms(80)

        if not resp:
            log.error(f"[LORA] No reply after {_CONFIG_WRITE_RETRIES} attempts — "
                      "module may be a non-EBYTE variant, wiring fault, or power issue")
        else:
            log.debug("[LORA] RX config: " + " ".join("{:02x}".format(b) for b in resp))
            if resp[0] not in (_CMD_REPLY, _CMD_SET_NV, _CMD_SET_VOL):
                log.warn(f"[LORA] Unexpected reply prefix 0x{resp[0]:02x} — config may not have taken")
            elif len(resp) >= 10 and (resp[3] != addh or resp[4] != addl or resp[5] != 0
                                      or resp[8] != channel):
                log.warn("[LORA] Reply values don't match what we asked for — module rejected the write")
            else:
                log.info(f"[LORA] Module configured: addr={unit_id} ch={channel} "
                         f"freq~{_CHAN_BASE_MHZ + channel}.125 MHz tx={tx_power}dBm "
                         f"(RSSI append + fixed-point ON)")

        # Switch to normal mode for runtime traffic.
        self._set_mode(*_MODE_NORMAL)
        time.sleep_ms(_MODE_DELAY_MS)

        log.debug(f"[LORA] Waiting for AUX to settle after mode switch (timeout={_AUX_TIMEOUT_S}s)...")
        if self._wait_aux_settled(timeout_s=_AUX_TIMEOUT_S):
            log.debug("[LORA] AUX settled HIGH")
        else:
            log.warn("[LORA] AUX still LOW after timeout, continuing anyway")

    def _read_with_timeout(self, expected_len, timeout_ms=400, poll_ms=20):
        """Block up to timeout_ms for the UART to give us at least expected_len
        bytes (or whatever it has when the timer expires)."""
        deadline_ms = time.ticks_ms() + timeout_ms
        buf = b""
        while time.ticks_diff(deadline_ms, time.ticks_ms()) > 0:
            chunk = self._uart.read()
            if chunk:
                buf += chunk
                if len(buf) >= expected_len:
                    break
            time.sleep_ms(poll_ms)
        return buf

    def _wait_aux_settled(self, timeout_s):
        """Wait for AUX to go HIGH (idle) within timeout_s seconds. Returns True
        if it did, False on timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._aux.value() == 1:
                return True
            time.sleep_ms(10)
        return False

    # ------------------------------------------------------------------
    # AUX discipline and mode control
    # ------------------------------------------------------------------

    def _wait_aux(self):
        """Block until AUX reads HIGH (module idle). Raises LoRaTimeoutError
        if AUX stays LOW past _AUX_TIMEOUT_S.

        Diagnostic logging at 1 Hz so a stuck-low pin is loudly visible —
        without it, "Send timeout: <type>" gives no clue whether the wait
        was 5s of actual busy-but-clearing AUX or 5s of a floating pin
        reading 0 the whole time.
        """
        deadline = time.time() + _AUX_TIMEOUT_S
        polls = 0
        while self._aux.value() == 0:
            if time.time() > deadline:
                log.error(
                    f"[LORA] AUX stuck LOW for {_AUX_TIMEOUT_S}s — "
                    f"verify wiring (AUX→GP{_aux_pin_label(self._aux)}). "
                    "If AUX reads 0 with the module powered and idle, the pin "
                    "is either disconnected or shorted to GND."
                )
                raise LoRaTimeoutError("AUX timeout — channel busy")
            time.sleep_ms(_AUX_POLL_MS)
            polls += 1
            # Every full second of waiting, log at DEBUG so the diagnostic
            # trail makes it clear whether AUX ever flickered HIGH or just
            # stayed flat-LOW the whole time.
            if polls * _AUX_POLL_MS % 1000 == 0:
                log.debug(f"[LORA] AUX still LOW after {polls * _AUX_POLL_MS}ms")

    def _set_mode(self, m0, m1):
        log.debug(f"[LORA] Setting mode: M0={m0}, M1={m1}")
        self._m0.value(m0)
        self._m1.value(m1)
        time.sleep_ms(50)  # Give pins time to settle


lora_transport = LoRaTransport()
