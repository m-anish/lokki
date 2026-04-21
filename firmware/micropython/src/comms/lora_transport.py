import time
from machine import UART, Pin
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

# E220 operating modes via M0/M1
_MODE_NORMAL = (0, 0)   # data transmission
_MODE_SLEEP  = (1, 1)   # AT command configuration

_AUX_POLL_MS   = 10
_AUX_TIMEOUT_S = 5        # increased for hot reboots
_AT_TIMEOUT_MS = 150      # increased for reliability
_MODE_DELAY_MS = 250      # delay after mode switch
_AT_RETRIES    = 3        # number of retries for AT commands
_BAUD          = 9600
_BROADCAST_ADDR = 0xFFFF


class LoRaTimeoutError(Exception):
    pass


class LoRaTransport:

    def __init__(self):
        self._uart   = None
        self._m0     = None
        self._m1     = None
        self._aux    = None
        self._channel = 0
        self._ready  = False

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
        """Return bytes from UART buffer, or None if empty."""
        if not self._ready or not self._uart.any():
            return None
        # Read all available bytes (up to 256)
        return self._uart.read(256)

    def available(self):
        return self._ready and self._uart.any() > 0

    # ------------------------------------------------------------------
    # E220 configuration (runs in sleep mode via AT commands)
    # ------------------------------------------------------------------

    def _configure(self, lora_cfg):
        unit_id  = config_manager.unit_id
        freq_hz  = int(lora_cfg.get("frequency_mhz", 868) * 1_000_000)
        tx_power = lora_cfg.get("tx_power_dbm", 22)
        channel  = lora_cfg.get("channel", 0)

        self._set_mode(*_MODE_SLEEP)
        time.sleep_ms(_MODE_DELAY_MS)

        # Aggressive UART flushing for hot reboots
        for _ in range(3):
            self._uart.read()
            time.sleep_ms(50)
        
        log.debug(f"[LORA] Configuring: unit_id={unit_id}, freq={freq_hz}Hz, tx_power={tx_power}dBm, ch={channel}")

        cmds = [
            f"AT+ADDRESS={unit_id}",
            f"AT+NETWORKID=0",
            f"AT+BAND={freq_hz}",
            f"AT+CHANNEL={channel}",
            f"AT+PARAMETER=9,7,1,4",     # SF9, BW125kHz, CR4/5, preamble 4
            f"AT+CRFOP={tx_power}",       # TX power
            "AT+MODE=1",                  # fixed-point transmission mode
        ]

        for cmd in cmds:
            resp = self._at(cmd)
            if resp and "+ERR" in resp:
                log.warn(f"[LORA] AT warn: {cmd} → {resp}")
            else:
                log.debug(f"[LORA] {cmd} → {resp}")

        self._set_mode(*_MODE_NORMAL)
        time.sleep_ms(_MODE_DELAY_MS)
        
        # Wait for AUX to settle HIGH after mode switch
        log.debug(f"[LORA] Waiting for AUX to settle (timeout={_AUX_TIMEOUT_S}s)...")
        deadline = time.time() + _AUX_TIMEOUT_S
        while self._aux.value() == 0 and time.time() < deadline:
            time.sleep_ms(10)
        
        if self._aux.value() == 0:
            log.warn("[LORA] AUX still LOW after timeout, but continuing...")
        else:
            log.debug("[LORA] AUX settled HIGH")

    def _at(self, cmd):
        try:
            self._uart.write((cmd + "\r\n").encode())
            time.sleep_ms(_AT_TIMEOUT_MS)
            raw = self._uart.read()
            if raw:
                try:
                    return raw.decode("utf-8", "ignore").strip()
                except Exception:
                    return ""
            return ""
        except Exception as e:
            log.error(f"[LORA] AT command failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # AUX discipline and mode control
    # ------------------------------------------------------------------

    def _wait_aux(self):
        deadline = time.time() + _AUX_TIMEOUT_S
        while self._aux.value() == 0:
            if time.time() > deadline:
                raise LoRaTimeoutError("AUX timeout — channel busy")
            time.sleep_ms(_AUX_POLL_MS)

    def _set_mode(self, m0, m1):
        log.debug(f"[LORA] Setting mode: M0={m0}, M1={m1}")
        self._m0.value(m0)
        self._m1.value(m1)
        time.sleep_ms(50)  # Give pins time to settle


lora_transport = LoRaTransport()
