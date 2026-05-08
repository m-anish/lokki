# ----------------------------------------------------------------------
# Step 1 — datasheet-strict flow.
#
# Same intent as lora_e220_step1_config.py but the mode-pin / UART
# choreography follows what the EBYTE E220 datasheet actually prescribes,
# rather than the xreef library's "write in CONFIG → mode-switch back →
# read reply" pattern.
#
# Per the datasheet, the correct sequence for a register read or write is:
#
#   ENTER CONFIG mode (read OR write):
#     1. M0=1, M1=1
#     2. Wait for AUX=1 (module is ready to accept a command)
#     3. Drain anything pending in the UART RX buffer
#     4. Send the command (0xC0 / 0xC1 / 0xC2 + start + length [+ payload])
#     5. Read the reply  ← STILL IN CONFIG MODE, M0/M1 stay at 1/1
#
#   EXIT CONFIG mode (after reply received):
#     6. Wait for AUX=1 (module finished processing).
#        If AUX is already 1 when we get here, still wait at least 2 ms.
#     7. Drop M0=0, M1=0 (NORMAL mode).
#     8. Wait for AUX=1 again (radio re-init complete in NORMAL).
#
# Compared to step1_config.py, the meaningful change is step 5: the reply
# is read while we're still in CONFIG mode, not after we've switched
# back to NORMAL. This avoids any race between the module finishing
# its register-write housekeeping and us trying to consume the reply
# from a half-stripped UART.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# CONFIG
# ============================================================
UNIT_ID  = 0
FREQ_MHZ = 868
NETID    = 0          # E220 has no NETID register; preserved for compat

UART_ID  = 0
TX_PIN   = 0
RX_PIN   = 1
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4
LED_PIN  = 5

LED_ORDER = "RGB"
PING_INTERVAL_MS = 2500

# If True, do the full read-back + write_config flow.
# If False, do nothing in CONFIG mode (just verify the loop runs).
DO_REGISTER_WRITES = True

# If True, persist via 0xC0 (NVRAM, survives power cycles, wears flash).
# If False, use 0xC2 (volatile, lost on power cycle, no flash wear).
PERSIST = False


# ============================================================
# Derived
# ============================================================
CHANNEL = max(0, min(80, round(FREQ_MHZ - 850)))   # 868 → 18

# Register layout per E220-900T22D datasheet §7.2:
#   0x00 ADDH, 0x01 ADDL, 0x02 REG0 (UART/parity/air),
#   0x03 REG1 (sub-pkt/ambient/power), 0x04 REG2 (CHAN),
#   0x05 REG3 (RSSI byte/fixed-point/LBT/WOR), 0x06 CRYPT_H, 0x07 CRYPT_L

_REG0 = 0x62   # 9600 baud / 8N1 / 2.4 kbps air rate (factory default)
_REG1 = 0x00   # 200 B sub-pkt / ambient off / 22 dBm
_REG3 = 0x00   # transparent / no LBT / no RSSI byte / WOR period 0
_CRYPT_H = 0x00
_CRYPT_L = 0x00


# ============================================================
# WS2812 helper
# ============================================================
_np = neopixel.NeoPixel(Pin(LED_PIN), 1)

def led(r, g, b):
    if LED_ORDER == "RGB":
        _np[0] = (g, r, b)
    else:
        _np[0] = (r, g, b)
    _np.write()


# ============================================================
# Module pins + UART
# ============================================================
m0 = Pin(M0_PIN, Pin.OUT)
m1 = Pin(M1_PIN, Pin.OUT)
aux = Pin(AUX_PIN, Pin.IN)
uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


# ============================================================
# AUX + UART helpers
# ============================================================

def _wait_aux_high(timeout_ms):
    """Block until AUX reads HIGH, or timeout. Returns True if HIGH."""
    deadline = time.ticks_ms() + timeout_ms
    while not aux.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(5)
    return True


def _drain_uart():
    """Read repeatedly until the RX buffer is genuinely empty."""
    while True:
        chunk = uart.read()
        if not chunk:
            return
        time.sleep_ms(5)


def _read_reply(expected_len, timeout_ms=1500):
    """Poll the UART for up to `expected_len` bytes back, with timeout.
    Returns whatever was read (may be shorter on timeout)."""
    deadline = time.ticks_ms() + timeout_ms
    buf = b""
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        chunk = uart.read()
        if chunk:
            buf += chunk
            if len(buf) >= expected_len:
                break
        time.sleep_ms(20)
    return buf


# ============================================================
# Datasheet-strict mode transitions
# ============================================================

def enter_config():
    """Per datasheet: M0=M1=1, ensure AUX=1, drain UART. After this the
    caller is free to issue C0/C1/C2 commands and read replies — all
    while M0/M1 remain at 1/1."""
    m0.value(1); m1.value(1)
    if not _wait_aux_high(2000):
        print("[STRICT] enter_config: AUX did not settle HIGH in 2s "
              "(module may be busy or wiring is wrong)")
        return False
    _drain_uart()
    return True


def exit_config():
    """Per datasheet: wait until AUX=1 (or, if already 1, hold at least
    2 ms more), then drop M0=0, M1=0. After the mode switch wait again
    for AUX HIGH so we know the radio finished re-initialising for
    NORMAL operation."""
    if _wait_aux_high(2000):
        # Datasheet specifies a 2 ms minimum settle even if AUX is
        # already HIGH when we check — give the module a moment for
        # any pending NVRAM commit / state-machine reorganisation.
        time.sleep_ms(2)
    else:
        # AUX never went HIGH within timeout — fall through anyway with
        # a 2 ms hold. The mode-pin transition itself will trigger a
        # fresh AUX cycle that we wait on below.
        print("[STRICT] exit_config: AUX did not settle HIGH before mode switch")
        time.sleep_ms(2)

    m0.value(0); m1.value(0)
    # Module re-initialises the radio on NORMAL-mode entry; AUX cycles
    # LOW briefly then HIGH when the radio is ready to receive.
    if not _wait_aux_high(2000):
        print("[STRICT] exit_config: AUX did not settle HIGH after NORMAL switch")
        return False
    return True


# ============================================================
# Register read/write (caller must be in CONFIG mode)
# ============================================================

def read_config_inplace():
    """Issue 0xC1 + read reply, all in CONFIG mode. Returns reply bytes."""
    cmd = bytes([0xC1, 0x00, 0x08])
    print("[STRICT] read_config TX: " + " ".join("{:02x}".format(b) for b in cmd))
    uart.write(cmd)
    return _read_reply(expected_len=11)


def write_config_inplace(persist):
    """Issue 0xC2 (volatile) or 0xC0 (NVRAM persist) + read reply,
    all in CONFIG mode. Returns reply bytes."""
    cmd_byte = 0xC0 if persist else 0xC2
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    cmd = bytes([
        cmd_byte, 0x00, 0x08,
        addh, addl,
        _REG0, _REG1, CHANNEL, _REG3,
        _CRYPT_H, _CRYPT_L,
    ])
    print("[STRICT] write_config TX ({}): {}".format(
        "0xC0 NVRAM" if persist else "0xC2 volatile",
        " ".join("{:02x}".format(b) for b in cmd)))
    uart.write(cmd)
    return _read_reply(expected_len=11)


def _print_reg(label, resp):
    """Parse an 11-byte reply: cmd + start + length + 8 register bytes."""
    if not resp or len(resp) < 11:
        print("[STRICT] {}: <truncated, {} bytes: {}>".format(
            label, len(resp) if resp else 0,
            " ".join("{:02x}".format(b) for b in (resp or b""))))
        return
    addh, addl       = resp[3], resp[4]
    reg0, reg1, ch   = resp[5], resp[6], resp[7]
    reg3             = resp[8]
    crypt_h, crypt_l = resp[9], resp[10]
    tx_method = "FIXED-POINT" if (reg3 & 0x40) else "TRANSPARENT"
    rssi_byte = "ON" if (reg3 & 0x80) else "OFF"
    print("[STRICT] {} (RX={}):".format(label,
          " ".join("{:02x}".format(b) for b in resp[:11])))
    print("[STRICT]   addr=0x{:02x}{:02x}  reg0=0x{:02x}  reg1=0x{:02x}  "
          "channel={} (~{}.125 MHz)  reg3=0x{:02x} ({}, RSSI byte {})  "
          "crypt=0x{:02x}{:02x}".format(addh, addl, reg0, reg1, ch, 850 + ch,
                                         reg3, tx_method, rssi_byte,
                                         crypt_h, crypt_l))


# ============================================================
# Top-level orchestration
# ============================================================

def configure():
    print("[STRICT] Configuring E220 per datasheet flow:")
    print("[STRICT]   addr={} freq={}MHz ch={} mode=TRANSPARENT  "
          "DO_REGISTER_WRITES={}  PERSIST={}".format(
              UNIT_ID, FREQ_MHZ, CHANNEL, DO_REGISTER_WRITES, PERSIST))
    print("[STRICT]   AUX at boot = {}".format(aux.value()))

    if not DO_REGISTER_WRITES:
        # Mode-bounce only — no UART config commands at all
        print("[STRICT] DO_REGISTER_WRITES=False → mode-bounce only, no UART config")
        if not enter_config():
            return False
        time.sleep_ms(200)
        if not exit_config():
            return False
        return True

    # --- Step 1: enter CONFIG ---
    if not enter_config():
        return False

    # --- Step 2: read current state (still in CONFIG) ---
    pre = read_config_inplace()
    _print_reg("BEFORE write", pre)

    # --- Step 3: drain UART before next command (defensive) ---
    _drain_uart()

    # --- Step 4: write desired state (still in CONFIG) ---
    resp = write_config_inplace(persist=PERSIST)
    _print_reg("AFTER write ", resp)

    # --- Step 5: exit CONFIG (datasheet-strict: wait AUX, 2 ms, switch) ---
    if not exit_config():
        return False

    # --- Step 6: validate the reply matched our intent ---
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    if (len(resp) >= 11 and resp[0] in (0xC0, 0xC1, 0xC2)
            and resp[3] == addh and resp[4] == addl
            and resp[5] == _REG0 and resp[7] == CHANNEL and resp[8] == _REG3):
        print("[STRICT] CONFIG OK")
        return True
    print("[STRICT] CONFIG FAILED — reply doesn't match expected values")
    return False


# ============================================================
# Main
# ============================================================

led(0, 0, 40)              # blue: startup

ok = configure()

if not ok:
    # Slow red blinks: configure failed
    for _ in range(3):
        led(40, 0, 0); time.sleep_ms(250)
        led(0, 0, 0);  time.sleep_ms(150)

led(20, 20, 0)             # idle yellow

print("[STRICT] Test loop running. unit_id={}".format(UNIT_ID))

counter = 0
last_send_ms = time.ticks_ms()

while True:
    if uart.any():
        data = uart.read()
        if data:
            try:
                text = data.decode("utf-8", "ignore").strip()
            except Exception:
                text = repr(data)
            if text:
                print("[RX] {!r}".format(text))
                led(40, 0, 0)
                time.sleep_ms(150)
                led(20, 20, 0)

    if time.ticks_diff(time.ticks_ms(), last_send_ms) >= PING_INTERVAL_MS:
        msg = "Hello {} from unit {}".format(counter, UNIT_ID)
        uart.write(msg + "\n")
        print("[TX] {!r}".format(msg))
        led(0, 40, 0)
        time.sleep_ms(100)
        led(20, 20, 0)
        counter += 1
        last_send_ms = time.ticks_ms()

    time.sleep_ms(20)
