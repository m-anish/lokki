"""In-band E220 register configuration.

We program the E220 from the same firmware that drives data transmission
— no separate bridge tooling, no NVRAM provisioning step. At boot,
lora_transport.init() calls apply_from_config() which derives the right
8-byte register payload from config.json + system.unit_id and writes it
to VOLATILE registers (RAM, not NVRAM). NVRAM is left untouched; reboot
re-derives from config.json. One source of truth.

Address scheme baked in:
  coord (unit_id == 0):  ADDR = 0xFFFF (monitor / sees every frame on the channel)
  leaf  (unit_id  > 0):  ADDR = 0x00<unit_id>   (e.g. leaf 3 → 0x0003)

Mode is forced to FIXED (REG3 bit 6 = 1) so coord can do hardware-level
directed transfers to a specific leaf (DEST byte == leaf's ADDR) or
broadcast (DEST == 0xFFFF). Transparent mode would force same-ADDR-or-no-
filter; that doesn't scale past two units.

RSSI byte is forced ON (REG3 bit 7 = 1) because lora_transport.recv()
unconditionally strips the trailing byte. Disabling RSSI in the
registers would cause the strip to eat real payload data.

Concurrency note: register operations toggle M0/M1 and tear up the
UART. They MUST NOT run concurrently with send()/recv(). We coordinate
cooperatively via `lora_transport.config_in_progress`: senders check
the flag and bail out, the listen task pauses while it's set. Locks
proper would be cleaner but would require making send/recv async,
which propagates through every caller in the codebase. The cooperative
flag is good enough because config ops are rare (boot + manual user
request) and short (~150 ms).
"""

import time
import json
from machine import Pin
from shared.simple_logger import Logger

log = Logger()


# Command bytes per datasheet
_CMD_WRITE_NVRAM = 0xC0
_CMD_READ_REGS   = 0xC1
_CMD_WRITE_RAM   = 0xC2
_RETURNED_CMD    = 0xC1
_REG_ADDR_CFG    = 0x00
_PL_CONFIG       = 0x08

# Encoding tables — UART baud bits, parity bits, air rate bits, sub-packet
# size bits, TX power bits. Match utils/e220_provisioner_cli.py exactly so
# both code paths produce identical wire bytes.
_BAUD_BITS = {1200: 0b000, 2400: 0b001, 4800: 0b010, 9600: 0b011,
              19200: 0b100, 38400: 0b101, 57600: 0b110, 115200: 0b111}
_AIR_BITS  = {300: 0b000, 1200: 0b001, 2400: 0b010, 4800: 0b011,
              9600: 0b100, 19200: 0b101, 38400: 0b110, 62500: 0b111}
_PAR_BITS  = {"8N1": 0b00, "8O1": 0b01, "8E1": 0b10}
_SUB_BITS  = {200: 0b00, 128: 0b01, 64: 0b10, 32: 0b11}
_PWR_BITS  = {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}

# Pico ↔ E220 UART baud. We force 9600 because that's what PROGRAM mode
# requires, and keeping it constant means we never have to deinit/reinit
# the UART around mode transitions (which the test harness showed leaves
# the RP2350's UART peripheral in a state that breaks the next register
# read).
_PICO_UART_BAUD = 9600


def _addr_for_unit(unit_id):
    """Return the (ADDH, ADDL) pair this unit should program into its
    own E220 registers."""
    if unit_id == 0:
        return (0xFF, 0xFF)            # coord: monitor address
    return (0x00, unit_id & 0xFF)      # leaf: 0x00<id>


def build_register_payload(unit_id, lora_cfg):
    """Compute the 8 register bytes [ADDH, ADDL, REG0, REG1, CHAN, REG3,
    CRYPT_H, CRYPT_L] for this unit, given the lora section of config.json
    and the unit's own id. Caller writes this via write_volatile() or
    write_nvram()."""
    addh, addl = _addr_for_unit(unit_id)

    air = int(lora_cfg.get("air_data_rate", 2400))
    if air not in _AIR_BITS:
        log.warn(f"[LORA_CFG] air_data_rate {air} not in {sorted(_AIR_BITS)}; defaulting to 2400")
        air = 2400
    pwr = int(lora_cfg.get("tx_power_dbm", 22))
    if pwr not in _PWR_BITS:
        log.warn(f"[LORA_CFG] tx_power_dbm {pwr} not in {sorted(_PWR_BITS)}; defaulting to 22")
        pwr = 22
    sub = int(lora_cfg.get("subpacket_size", 200))
    if sub not in _SUB_BITS:
        sub = 200

    chan = int(lora_cfg.get("channel", 18))
    lbt  = bool(lora_cfg.get("lbt_enable", False))
    ambient = bool(lora_cfg.get("ambient_rssi_enable", False))

    # REG0: bits 7-5 UART baud, bits 4-3 parity, bits 2-0 air rate
    reg0 = (_BAUD_BITS[_PICO_UART_BAUD] << 5) | (_PAR_BITS["8N1"] << 3) | _AIR_BITS[air]
    # REG1: bits 7-6 sub-packet, bit 5 ambient RSSI, bits 1-0 TX power
    reg1 = (_SUB_BITS[sub] << 6) | ((1 if ambient else 0) << 5) | _PWR_BITS[pwr]
    # REG3: bit 7 RSSI-byte (FORCED on — recv() strips it), bit 6 FIXED
    # mode (FORCED on), bit 4 LBT, bits 2-0 WOR cycle (we don't use WOR;
    # 0b011 = 2000 ms is a benign default that matches the test harness)
    reg3 = (1 << 7) | (1 << 6) | ((1 if lbt else 0) << 4) | 0b011

    return bytes([addh, addl, reg0, reg1, chan, reg3, 0x00, 0x00])


def decode_register_payload(b):
    """Inverse of build_register_payload. Used by the live status API to
    show the operator what's actually programmed in volatile right now."""
    if len(b) != 8:
        return {"error": "expected 8 register bytes, got %d" % len(b)}
    addh, addl, reg0, reg1, chan, reg3, ch, cl = b
    return {
        "addr_hex":    "%02X%02X" % (addh, addl),
        "uart_baud":   _decode_bits(reg0 >> 5, _BAUD_BITS),
        "air_rate":    _decode_bits(reg0 & 0b111, _AIR_BITS),
        "subpacket":   _decode_bits(reg1 >> 6, _SUB_BITS),
        "ambient_rssi": bool((reg1 >> 5) & 1),
        "tx_power_dbm": _decode_bits(reg1 & 0b11, _PWR_BITS),
        "channel":     chan,
        "rssi_byte":   bool((reg3 >> 7) & 1),
        "fixed_mode":  bool((reg3 >> 6) & 1),
        "lbt":         bool((reg3 >> 4) & 1),
        "crypt":       "%02X%02X" % (ch, cl),
        "raw_hex":     " ".join("%02X" % x for x in b),
    }


def _decode_bits(bits_value, table):
    for k, v in table.items():
        if v == bits_value:
            return k
    return None


# ------------------------------------------------------------------
# Module-private helpers — assume bus_lock-equivalent (config_in_progress
# flag) is held by the caller. Caller also passes in the transport so
# we share its pins/UART instance (no second UART instance fighting).
# ------------------------------------------------------------------

def _set_mode(transport, mode_pair):
    """Drive M0/M1 to mode_pair (e.g. (1,1) for PROGRAM). 40 ms pre/post
    delay matches the xreef library + datasheet timing. AUX HIGH is the
    "done with mode change" semaphore."""
    time.sleep_ms(40)
    transport._m0.value(mode_pair[0])
    transport._m1.value(mode_pair[1])
    time.sleep_ms(40)
    deadline = time.ticks_add(time.ticks_ms(), 1000)
    while transport._aux.value() == 0:
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            log.warn(f"[LORA_CFG] set_mode({mode_pair}): AUX did not go HIGH within 1s")
            return False
        time.sleep_ms(2)
    time.sleep_ms(20)
    return True


def _drain_uart(uart):
    n = uart.any()
    if n:
        uart.read(n)


def _wait_aux_high(transport, timeout_ms):
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while transport._aux.value() == 0:
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(2)
    return True


# ------------------------------------------------------------------
# Public ops
# ------------------------------------------------------------------

def read(transport):
    """Read the E220's current 8 register bytes. Returns the decoded
    dict (see decode_register_payload), or None on failure.

    Caller must set transport.config_in_progress = True before calling
    and clear it afterwards. apply_from_config() handles this pattern;
    call read() through that wrapper if you want it from async code."""
    if not _wait_aux_high(transport, 2000):
        log.warn("[LORA_CFG] read: AUX stuck LOW before mode change")
        return None
    if not _set_mode(transport, (1, 1)):     # PROGRAM
        return None
    _drain_uart(transport._uart)
    transport._uart.write(bytes([_CMD_READ_REGS, _REG_ADDR_CFG, _PL_CONFIG]))
    time.sleep_ms(200)
    reply = transport._uart.read()
    _set_mode(transport, (0, 0))             # back to NORMAL
    if reply is None or len(reply) < 11:
        log.warn(f"[LORA_CFG] read: short reply {reply!r}")
        return None
    return decode_register_payload(reply[3:11])


def write(transport, payload8, persist=False):
    """Write the 8 register bytes. `persist=False` → volatile (0xC2),
    `persist=True` → NVRAM (0xC0, costs a flash write). Returns True if
    the module echoed back the same bytes we sent."""
    if len(payload8) != 8:
        raise ValueError("payload must be 8 register bytes")
    cmd = _CMD_WRITE_NVRAM if persist else _CMD_WRITE_RAM

    if not _wait_aux_high(transport, 2000):
        log.warn("[LORA_CFG] write: AUX stuck LOW before mode change")
        return False
    if not _set_mode(transport, (1, 1)):     # PROGRAM
        return False
    _drain_uart(transport._uart)
    frame = bytes([cmd, _REG_ADDR_CFG, _PL_CONFIG]) + bytes(payload8)
    transport._uart.write(frame)
    time.sleep_ms(300)
    reply = transport._uart.read()
    _set_mode(transport, (0, 0))             # back to NORMAL
    if reply is None or len(reply) < 11:
        log.warn(f"[LORA_CFG] write: short reply {reply!r}")
        return False
    ok = (reply[0] == _RETURNED_CMD and reply[3:11] == bytes(payload8))
    if not ok:
        log.warn(f"[LORA_CFG] write: echo mismatch. sent={frame.hex()} got={reply[:11].hex()}")
    return ok


_APPLY_MAX_ATTEMPTS = 3


def apply_from_config(transport, unit_id, lora_cfg, persist=False):
    """Compose the right register payload for this unit and write it
    via write(). This is the boot-time entry point — call it once after
    transport.init() has set up the UART and pins.

    Holds transport.config_in_progress for the duration so the listen
    task and senders cooperatively step aside. Retries up to 3 times
    on transient failures (occasional short reply from the module
    when state is borderline) before giving up. Returns True iff the
    written values readback verbatim within the retry budget."""
    payload = build_register_payload(unit_id, lora_cfg)
    log.info(f"[LORA_CFG] apply_from_config unit_id={unit_id} payload={payload.hex()} "
             f"persist={persist}")

    transport.config_in_progress = True
    try:
        for attempt in range(1, _APPLY_MAX_ATTEMPTS + 1):
            ok = write(transport, payload, persist=persist)
            if ok:
                if attempt > 1:
                    log.info(f"[LORA_CFG] write succeeded on attempt {attempt}")
                break
            # Failed. Give the module a moment to settle, then try again.
            log.warn(f"[LORA_CFG] write attempt {attempt}/{_APPLY_MAX_ATTEMPTS} "
                     f"failed; retrying after 200 ms")
            time.sleep_ms(200)
    finally:
        transport.config_in_progress = False

    if ok:
        decoded = decode_register_payload(payload)
        log.info(f"[LORA_CFG] applied: {decoded}")
    return ok
