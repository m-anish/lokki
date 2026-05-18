# ----------------------------------------------------------------------
# E220 RAM-only boot-config test
#
# What this script does, in order:
#   1.  Boot. M0/M1 driven to 1/1 → CONFIG mode.
#   2.  Wait for AUX HIGH (module ready to accept a command).
#   3.  Drain UART RX so nothing stale gets parsed as a reply.
#   4.  Issue 0xC1 (read 8 bytes from addr 0x00) → log the *current* register
#       state of the module. Useful for confirming what NVRAM holds.
#   5.  Issue 0xC2 (write 8 bytes from addr 0x00) with our desired settings.
#       0xC2 = volatile/RAM. Survives nothing — power-cycle wipes back to
#       NVRAM. Zero flash wear, safe to spam.
#   6.  Issue 0xC1 again → read back the just-written values, log them, and
#       compare to our intent. Pass / fail printed clearly.
#   7.  Exit CONFIG mode, datasheet-strict: wait AUX HIGH, hold 2 ms, then
#       drop M0/M1 to 0/0 (NORMAL), then wait AUX HIGH again so we know the
#       radio has re-initialised.
#   8.  Enter the test loop: send a ping every PING_INTERVAL_MS, log anything
#       received, blink the WS2812 for TX/RX feedback.
#
# Use case: two Picos with E220s wired in. Flash both with this script —
# UNIT_ID, PEER_ID swapped — and they should ping-pong without touching NVRAM.
# Power-cycle either one and the RAM settings vanish, so you can keep
# iterating on FREQ/CHANNEL/AIR_RATE without burning the module's flash.
#
# To make settings persist across power cycles, change WRITE_CMD to 0xC0
# (NVRAM). Don't do that until you're confident the values you're writing
# are the ones you want — the E220's flash isn't infinitely rewritable.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# Test config — change these per-board
# ============================================================
UNIT_ID  = 1                 # this Pico's address (1 or 2 typically)
PEER_ID  = 2                 # informational; payload labels include this
FREQ_MHZ = 868
PING_INTERVAL_MS = 2500

# 0xC2 = volatile (RAM only, lost on power cycle)
# 0xC0 = NVRAM   (persists, wears flash)
WRITE_CMD = 0xC2

# Pin map — matches firmware/micropython/src/config/samples/config.json.sample
UART_ID  = 0
TX_PIN   = 0
RX_PIN   = 1
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4
LED_PIN  = 5
LED_ORDER = "RGB"

# ============================================================
# Desired register values (datasheet §7.2 layout)
# ============================================================
# 0x00 ADDH, 0x01 ADDL
# 0x02 REG0 — UART/parity/air rate
# 0x03 REG1 — sub-packet/ambient/TX power
# 0x04 REG2 — CHANNEL (frequency = 850 + ch MHz)
# 0x05 REG3 — RSSI-byte/fixed-point/LBT/WOR
# 0x06 CRYPT_H, 0x07 CRYPT_L
REG0   = 0x62   # 9600 baud, 8N1, 2.4 kbps air rate (factory defaults)
REG1   = 0x00   # 200 B sub-packet, ambient off, 22 dBm
REG3   = 0x00   # transparent, no LBT, no RSSI byte, WOR period 0
CRYPT  = (0x00, 0x00)

CHANNEL = max(0, min(80, round(FREQ_MHZ - 850)))   # 868 → 18


# ============================================================
# LED helper
# ============================================================
_np = neopixel.NeoPixel(Pin(LED_PIN), 1)

def led(r, g, b):
    if LED_ORDER == "RGB":
        _np[0] = (g, r, b)
    else:
        _np[0] = (r, g, b)
    _np.write()


# ============================================================
# Pins + UART
# ============================================================
m0  = Pin(M0_PIN, Pin.OUT)
m1  = Pin(M1_PIN, Pin.OUT)
aux = Pin(AUX_PIN, Pin.IN)
uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


# ============================================================
# AUX / UART helpers
# ============================================================
def wait_aux(timeout_ms=2000):
    """Block until AUX reads HIGH or timeout. Returns True if HIGH."""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while not aux.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(5)
    return True


def drain():
    """Empty the UART RX buffer."""
    while True:
        chunk = uart.read()
        if not chunk:
            return
        time.sleep_ms(5)


def read_reply(expected_len=11, timeout_ms=1500):
    """Collect up to expected_len bytes within timeout."""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
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
# Mode transitions — datasheet-strict
# ============================================================
def enter_config():
    m0.value(1); m1.value(1)
    if not wait_aux(2000):
        print("[RAM] enter_config: AUX did not go HIGH within 2s")
        return False
    drain()
    return True


def exit_to_normal():
    # Wait for the module to finish whatever it's doing, then hold 2 ms
    # (datasheet requirement) before dropping the mode pins.
    if not wait_aux(2000):
        print("[RAM] exit_to_normal: AUX did not settle before mode switch")
    time.sleep_ms(2)
    m0.value(0); m1.value(0)
    # NORMAL-mode entry cycles AUX briefly while the radio re-initialises.
    if not wait_aux(2000):
        print("[RAM] exit_to_normal: AUX did not go HIGH after switch")
        return False
    return True


# ============================================================
# Register read / write (both happen while still in CONFIG mode)
# ============================================================
def read_registers():
    cmd = bytes([0xC1, 0x00, 0x08])
    print("[RAM] read TX: " + " ".join("{:02x}".format(b) for b in cmd))
    uart.write(cmd)
    return read_reply(11)


def write_registers():
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    cmd = bytes([
        WRITE_CMD, 0x00, 0x08,
        addh, addl,
        REG0, REG1, CHANNEL, REG3,
        CRYPT[0], CRYPT[1],
    ])
    label = "0xC0 NVRAM" if WRITE_CMD == 0xC0 else "0xC2 RAM"
    print("[RAM] write TX ({}): {}".format(
        label, " ".join("{:02x}".format(b) for b in cmd)))
    uart.write(cmd)
    return read_reply(11)


def show(label, resp):
    if not resp or len(resp) < 11:
        print("[RAM] {} <truncated, {} bytes: {}>".format(
            label, len(resp) if resp else 0,
            " ".join("{:02x}".format(b) for b in (resp or b""))))
        return
    addh, addl     = resp[3], resp[4]
    reg0, reg1, ch = resp[5], resp[6], resp[7]
    reg3           = resp[8]
    cl, cm         = resp[9], resp[10]
    tx_mode  = "FIXED" if (reg3 & 0x40) else "TRANS"
    rssi_byte = "ON" if (reg3 & 0x80) else "OFF"
    print("[RAM] {} addr=0x{:02x}{:02x} reg0=0x{:02x} reg1=0x{:02x} "
          "ch={} (~{}.125 MHz) reg3=0x{:02x} ({} RSSI={}) crypt=0x{:02x}{:02x}"
          .format(label, addh, addl, reg0, reg1, ch, 850 + ch, reg3,
                  tx_mode, rssi_byte, cl, cm))


def verify(resp):
    """True iff the just-read reply matches what we asked for."""
    if not resp or len(resp) < 11:
        return False
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    return (resp[0] in (0xC0, 0xC1, 0xC2)
            and resp[3] == addh and resp[4] == addl
            and resp[5] == REG0 and resp[6] == REG1
            and resp[7] == CHANNEL and resp[8] == REG3
            and resp[9] == CRYPT[0] and resp[10] == CRYPT[1])


# ============================================================
# Boot configuration
# ============================================================
def configure():
    print("[RAM] Boot config for unit {} → {} on ch={} ({}.125 MHz)".format(
        UNIT_ID, "RAM" if WRITE_CMD == 0xC2 else "NVRAM",
        CHANNEL, 850 + CHANNEL))
    print("[RAM] AUX at boot = {}".format(aux.value()))

    if not enter_config():
        return False

    pre = read_registers()
    show("BEFORE:", pre)

    drain()
    resp = write_registers()
    show("AFTER :", resp)

    if not exit_to_normal():
        return False

    if verify(resp):
        print("[RAM] CONFIG OK — module reflects requested settings")
        return True
    print("[RAM] CONFIG FAILED — reply did not match expected values")
    return False


# ============================================================
# Main
# ============================================================
led(0, 0, 40)                 # blue while booting

ok = configure()

if not ok:
    # Three slow red blinks → boot config failed. Halt in red so it's obvious.
    for _ in range(3):
        led(40, 0, 0); time.sleep_ms(250)
        led(0, 0, 0);  time.sleep_ms(150)
    led(40, 0, 0)
    while True:
        time.sleep_ms(1000)

led(20, 20, 0)                # yellow idle once configured

print("[RAM] Loop running — unit={} peer={} ping every {}ms".format(
    UNIT_ID, PEER_ID, PING_INTERVAL_MS))

tx_count = 0
rx_count = 0
last_send_ms = time.ticks_ms()

while True:
    # ----- RX -----
    if uart.any():
        data = uart.read()
        if data:
            rx_count += 1
            try:
                text = data.decode("utf-8", "ignore").strip()
            except Exception:
                text = repr(data)
            if text:
                print("[RX #{}] {!r}".format(rx_count, text))
                led(40, 0, 0); time.sleep_ms(150); led(20, 20, 0)

    # ----- TX -----
    if time.ticks_diff(time.ticks_ms(), last_send_ms) >= PING_INTERVAL_MS:
        msg = "u{}->u{} #{}".format(UNIT_ID, PEER_ID, tx_count)
        uart.write(msg + "\n")
        print("[TX #{}] {!r}".format(tx_count, msg))
        led(0, 40, 0); time.sleep_ms(100); led(20, 20, 0)
        tx_count += 1
        last_send_ms = time.ticks_ms()

    time.sleep_ms(20)
