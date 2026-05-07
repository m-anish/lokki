# ----------------------------------------------------------------------
# Step 1 — baseline + minimal register-mode config write.
#
# Same TX/RX loop as lora_e220_test.py (the known-working baseline), but
# with ONE addition before we drop to NORMAL mode: a single 0xC2 (volatile)
# register-mode write that puts known values into ADDH/ADDL/NETID/REG0/
# REG1/CHANNEL/REG3.
#
# Critically:
#   - Mode stays TRANSPARENT (REG3 bit 6 = 0). No fixed-point header.
#   - No RSSI byte append (REG3 bit 7 = 0).
#   - Channel = 18 (868.125 MHz). Same as the working baseline state.
#   - ADDH/ADDL set per UNIT_ID — but transparent mode ignores them, so
#     this doesn't change *who hears whom*. Both units still hear all
#     traffic on channel 18 / NETID 0 regardless of address.
#
# Expected outcome: identical observable behaviour to baseline (green
# flash on TX, red flash on RX, messages flowing both ways). The ONLY
# new thing is the boot log will show the config write succeeding.
#
# If TX still works but RX stops: the act of writing the registers
# disturbs the module's RX path. Worth knowing.
#
# If config write itself reports timeout / no reply: register-mode
# protocol is what's flaky and we need to debug that specifically.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# CONFIG
# ============================================================
UNIT_ID  = 0          # 0 = coordinator-side, 1..8 = leaves
FREQ_MHZ = 868
TX_POWER = 22
NETID    = 0

UART_ID  = 0
TX_PIN   = 0
RX_PIN   = 1
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4
LED_PIN  = 5

LED_ORDER = "RGB"

PING_INTERVAL_MS = 2500


# ============================================================
# Derived
# ============================================================
CHANNEL = max(0, min(80, round(FREQ_MHZ - 850)))   # 868 → 18

# REG0: 9600 baud + 8N1 + 2.4 kbps air → 0x62 (this IS the factory default)
# REG1: 200B sub-packet + ambient off + 22 dBm → 0x00 (factory default)
# REG3: transparent + no RSSI byte + no LBT, WOR cycle 0 → 0x00
#       (factory default is 0x03 — WOR cycle 4000 ms — but in NORMAL mode
#        WOR bits don't affect anything, and 0x00 is cleaner.)
_REG0 = 0x62
_REG1 = 0x00
_REG3 = 0x00


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
# Module pins
# ============================================================
m0 = Pin(M0_PIN, Pin.OUT)
m1 = Pin(M1_PIN, Pin.OUT)
aux = Pin(AUX_PIN, Pin.IN)

uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


# ============================================================
# Helpers
# ============================================================
def _wait_aux_high(timeout_ms):
    deadline = time.ticks_ms() + timeout_ms
    while not aux.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(10)
    return True

def _drain_uart():
    empty = 0
    for _ in range(20):
        if uart.read():
            empty = 0
        else:
            empty += 1
            if empty >= 2:
                return
        time.sleep_ms(30)


def configure(max_attempts=5):
    """Single register-mode write. Sets module to transparent, channel 18,
    addr=UNIT_ID, NETID=0. 0xC2 is volatile — no flash wear, no NVRAM
    state to inherit on next boot."""
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    cmd = bytes([0xC2, 0x00, 0x07, addh, addl, NETID,
                 _REG0, _REG1, CHANNEL, _REG3])

    print("[STEP1] Configuring E220: addr={} freq={}MHz ch={} mode=TRANSPARENT"
          .format(UNIT_ID, FREQ_MHZ, CHANNEL))
    print("[STEP1] AUX at start = {}".format(aux.value()))

    for attempt in range(1, max_attempts + 1):
        # Bounce M0/M1 to enter config mode cleanly.
        m0.value(0); m1.value(0)
        time.sleep_ms(300)
        m0.value(1); m1.value(1)
        time.sleep_ms(300)
        _drain_uart()
        if not _wait_aux_high(2000):
            print("[STEP1] Attempt {}: AUX not HIGH".format(attempt))

        print("[STEP1] Attempt {}: TX {}".format(
            attempt, " ".join("{:02x}".format(b) for b in cmd)))
        uart.write(cmd)

        deadline = time.ticks_ms() + 1500
        resp = b""
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            chunk = uart.read()
            if chunk:
                resp += chunk
                if len(resp) >= 10:
                    break
            time.sleep_ms(20)

        if resp:
            print("[STEP1] Attempt {}: RX {}".format(
                attempt, " ".join("{:02x}".format(b) for b in resp)))
            if (len(resp) >= 10 and resp[0] in (0xC0, 0xC1, 0xC2)
                    and resp[3] == addh and resp[4] == addl
                    and resp[5] == NETID and resp[8] == CHANNEL):
                # Drop back to NORMAL mode and wait for the module to
                # finish re-initialising the radio with the new parameters.
                # Per datasheet, no reboot is needed — but AUX going HIGH
                # after the mode switch IS the readiness signal.
                m0.value(0); m1.value(0)
                if _wait_aux_high(2000):
                    print("[STEP1] AUX settled HIGH after NORMAL-mode switch")
                else:
                    print("[STEP1] WARN: AUX did not settle HIGH after NORMAL mode")
                print("[STEP1] CONFIG OK after {} attempt(s)".format(attempt))
                return True
            print("[STEP1] Attempt {}: bad reply, retrying".format(attempt))
        else:
            print("[STEP1] Attempt {}: no reply".format(attempt))

    # Drop to NORMAL even on failure so the rest of the loop at least tries.
    m0.value(0); m1.value(0)
    _wait_aux_high(2000)
    print("[STEP1] CONFIG FAILED after {} attempts — running anyway".format(max_attempts))
    return False


# ============================================================
# Main
# ============================================================

led(0, 0, 40)        # blue: startup

config_ok = configure()

if config_ok:
    print("[STEP1] Test loop running. Should behave identically to baseline.")
else:
    # Two slow red blinks to indicate config failure, but continue running.
    for _ in range(2):
        led(40, 0, 0); time.sleep_ms(300)
        led(0, 0, 0);  time.sleep_ms(200)

led(20, 20, 0)       # idle yellow

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
