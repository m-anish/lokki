# ----------------------------------------------------------------------
# Standalone E220-900T22D LoRa link test.
#
# Self-contained: imports nothing from the Lokki project. Drop this onto
# a freshly-wiped Pico as `main.py` and it'll boot, configure the E220
# in fixed-point mode at 868 MHz, then PING-broadcast every 3 s and print
# anything it receives.
#
# Each unit ships the same code; edit UNIT_ID at the top before flashing
# the second board so the two units have distinct addresses.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# CONFIG — edit these for your hardware/setup
# ============================================================
UNIT_ID  = 0          # 0 = coordinator-side, 1..8 = leaves
FREQ_MHZ = 868        # E220 covers 850.125 + ch MHz, ch ∈ [0,80]
TX_POWER = 22         # dBm. 22 / 17 / 13 / 10 supported.
NETID    = 0

UART_ID  = 0
TX_PIN   = 0          # Pico TX → module RX
RX_PIN   = 1          # Pico RX → module TX
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4
LED_PIN  = 5          # WS2812 status pixel

# Some WS2812 variants are RGB-native (most are GRB). If green looks red
# and red looks green, set this to "RGB" so we swap in software.
LED_ORDER = "RGB"

# How often to ping (ms).
PING_INTERVAL_MS = 3000


# ============================================================
# Derived values
# ============================================================
CHANNEL = max(0, min(80, round(FREQ_MHZ - 850)))


# ============================================================
# WS2812 status helper
# ============================================================
_np = neopixel.NeoPixel(Pin(LED_PIN), 1)

def led(r, g, b, brightness=0.2):
    bb = max(0.0, min(1.0, brightness))
    rr = int(r * bb); gg = int(g * bb); bbb = int(b * bb)
    if LED_ORDER == "RGB":
        _np[0] = (gg, rr, bbb)         # swap so RGB-native chips display correctly
    else:
        _np[0] = (rr, gg, bbb)
    _np.write()

def led_off():
    led(0, 0, 0)

def led_pulse(r, g, b, n=2, period_ms=120):
    for _ in range(n):
        led(r, g, b, 0.4)
        time.sleep_ms(period_ms)
        led_off()
        time.sleep_ms(period_ms)


# ============================================================
# E220 module
# ============================================================
m0  = Pin(M0_PIN,  Pin.OUT, value=0)
m1  = Pin(M1_PIN,  Pin.OUT, value=0)
aux = Pin(AUX_PIN, Pin.IN)
uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))

# REG0: 9600 baud (0b011) | 8N1 (0b00) | 2.4 kbps air rate (0b010) → 0x62
# REG1: 200 B sub-pkt (0b00) | RSSI ambient off | 22 dBm (0b00)    → 0x00
# REG3: bit 7 = RSSI byte append, bit 6 = fixed-point transmission → 0xC0
_REG0 = 0x62
_REG1 = 0x00
_REG3 = 0xC0


def _wait_aux_high(timeout_ms=3000):
    deadline = time.ticks_ms() + timeout_ms
    while not aux.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(10)
    return True


def _drain_uart():
    """Read until two consecutive empty reads — guarantees RX FIFO is quiet."""
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
    """Configure the E220 in register mode. Returns True on success."""
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    cmd = bytes([0xC2, 0x00, 0x07, addh, addl, NETID,
                 _REG0, _REG1, CHANNEL, _REG3])

    print("[TEST] Pin readback at boot: AUX=" + str(aux.value()))
    print("[TEST] Configuring E220: addr={} freq={}MHz ch={} tx={}dBm"
          .format(UNIT_ID, FREQ_MHZ, CHANNEL, TX_POWER))

    for attempt in range(1, max_attempts + 1):
        # Bounce M0/M1 to reset module state machine, with growing delays
        # between attempts.
        delay_ms = (300, 800, 1500, 2500, 4000)[min(attempt - 1, 4)]
        led(0, 80, 80, 0.3)        # cyan = configuring
        m0.value(0); m1.value(0)
        time.sleep_ms(delay_ms)
        m0.value(1); m1.value(1)
        time.sleep_ms(delay_ms)
        _drain_uart()
        if not _wait_aux_high(3000):
            print("[TEST] Attempt {}: AUX did not settle HIGH".format(attempt))

        print("[TEST] Attempt {}: TX {}".format(
            attempt, " ".join("{:02x}".format(b) for b in cmd)))
        uart.write(cmd)

        # Wait for reply (up to 1.5 s — observed cold-boot reply latency).
        deadline = time.ticks_ms() + 1500
        resp = b""
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            chunk = uart.read()
            if chunk:
                resp += chunk
                if len(resp) >= 10:
                    break
            time.sleep_ms(20)

        if not resp:
            print("[TEST] Attempt {}: no reply".format(attempt))
            led_pulse(255, 0, 0, n=1, period_ms=80)
            continue
        print("[TEST] Attempt {}: RX {}".format(
            attempt, " ".join("{:02x}".format(b) for b in resp)))
        if (len(resp) >= 10 and resp[0] in (0xC0, 0xC1, 0xC2)
                and resp[3] == addh and resp[4] == addl
                and resp[5] == NETID and resp[8] == CHANNEL):
            # Success — flip to normal mode for runtime
            m0.value(0); m1.value(0)
            time.sleep_ms(300)
            print("[TEST] CONFIG OK after {} attempt(s)".format(attempt))
            led_pulse(0, 255, 0, n=2, period_ms=120)
            return True
        print("[TEST] Attempt {}: bad reply, retrying".format(attempt))

    print("[TEST] CONFIG FAILED after {} attempts".format(max_attempts))
    return False


# ============================================================
# Send / receive
# ============================================================

def send(dest_addr, payload_bytes):
    """Fixed-point transmit. Module prepends [ADDH][ADDL][CHAN] to wire."""
    if not _wait_aux_high(5000):
        print("[TEST] AUX timeout on send")
        return False
    addh = (dest_addr >> 8) & 0xFF
    addl = dest_addr & 0xFF
    uart.write(bytes([addh, addl, CHANNEL]) + payload_bytes)
    return True


def recv():
    """Returns (payload_bytes, rssi_dbm) or (None, None)."""
    if not uart.any():
        return None, None
    raw = uart.read(256)
    if not raw or len(raw) < 2:
        return None, None
    rssi = -(256 - raw[-1])
    return raw[:-1], rssi


# ============================================================
# Main loop
# ============================================================

def main():
    led_pulse(255, 255, 255, n=3, period_ms=80)        # boot indicator (white)

    if not configure():
        # Solid red — config didn't take. User should power-cycle.
        while True:
            led(255, 0, 0, 0.4)
            time.sleep_ms(400)
            led_off()
            time.sleep_ms(400)

    print("[TEST] Test loop running. unit_id={}, ch={}, freq~{}.125 MHz"
          .format(UNIT_ID, CHANNEL, 850 + CHANNEL))

    counter = 0
    last_send_ms = time.ticks_ms()
    last_rx_ms   = 0
    last_rssi    = None

    while True:
        # --- RX path ---
        payload, rssi = recv()
        if payload is not None:
            try:
                msg = payload.decode("utf-8", "ignore")
            except Exception:
                msg = repr(payload)
            print("[RX] {!r} (rssi={} dBm)".format(msg, rssi))
            last_rx_ms = time.ticks_ms()
            last_rssi  = rssi
            led(0, 255, 0, 0.5)              # bright green flash on rx
            time.sleep_ms(100)

        # --- TX path ---
        if time.ticks_diff(time.ticks_ms(), last_send_ms) >= PING_INTERVAL_MS:
            counter += 1
            msg = "PING #{} from unit {}".format(counter, UNIT_ID)
            ok = send(0xFFFF, msg.encode())     # broadcast
            if ok:
                print("[TX] {!r}".format(msg))
                led(0, 0, 255, 0.5)             # bright blue flash on tx
                time.sleep_ms(100)
            last_send_ms = time.ticks_ms()

        # --- steady-state colour reflects link quality ---
        if last_rssi is None:
            led(0, 80, 80, 0.04)                # dim cyan = no peer heard yet
        else:
            secs_since_rx = time.ticks_diff(time.ticks_ms(), last_rx_ms) // 1000
            if secs_since_rx > 30:
                led(255, 100, 0, 0.08)          # amber = peer went quiet
            elif last_rssi >= -70:
                led(0, 255, 0, 0.06)            # green = good signal
            elif last_rssi >= -90:
                led(255, 200, 0, 0.08)          # yellow = weak signal
            else:
                led(255, 0, 0, 0.08)            # red = barely receiving

        time.sleep_ms(50)


main()
