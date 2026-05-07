# ----------------------------------------------------------------------
# Step 1 — baseline + minimal register-mode config write.
# Adopts xreef/EByte_LoRa_E220_micropython_library's flow:
#
#   set_mode(CONFIG)
#     40 ms delay → set M0/M1 → 40 ms delay → wait AUX HIGH → 20 ms
#   write config bytes (in CONFIG mode)
#   set_mode(NORMAL)             ← switches back BEFORE reading reply
#     40 ms delay → set M0/M1 → 40 ms delay → wait AUX HIGH → 20 ms
#   uart.read()                  ← reads reply that arrived during re-init
#
# The interesting departure from what we'd been doing is reading the
# reply *after* the mode-switch back to NORMAL, not while still in CONFIG
# mode. In xreef's flow the reply is collected during the radio re-init
# phase the module performs on the mode transition.
#
# Also reads back current register state at boot so we can see what the
# module had configured BEFORE step1 wrote — useful for debugging cases
# where the modules' baseline state and our desired state disagree.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# CONFIG
# ============================================================
UNIT_ID  = 0
FREQ_MHZ = 868
TX_POWER = 22
NETID    = 0

# Diagnostic toggle:
#   True  — do the full read-back + write_config flow (default)
#   False — do the mode-pin bouncing through CONFIG → NORMAL but do NOT
#           issue any register read/write commands. Used to isolate whether
#           the mode bounce itself or the writes break the radio's RX path.
DO_REGISTER_WRITES = True

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

# REG0: 9600 baud + 8N1 + 2.4 kbps air rate (factory default = 0x62)
# REG1: 200 B sub-pkt + ambient off + 22 dBm (factory default = 0x00)
# REG3: transparent + no RSSI byte append + no LBT, WOR cycle 0 → 0x00
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
# Module pins + UART
# ============================================================
m0 = Pin(M0_PIN, Pin.OUT)
m1 = Pin(M1_PIN, Pin.OUT)
aux = Pin(AUX_PIN, Pin.IN)
uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


# ============================================================
# Mode control — xreef-style: 40 / pins / 40 / AUX / 20
# ============================================================

def _wait_aux_high(timeout_ms=1000):
    t0 = time.ticks_ms()
    while not aux.value():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            return False
        time.sleep_ms(10)
    return True


def set_mode_normal():
    time.sleep_ms(40)
    m0.value(0); m1.value(0)
    time.sleep_ms(40)
    ok = _wait_aux_high(1000)
    time.sleep_ms(20)
    return ok


def set_mode_config():
    time.sleep_ms(40)
    m0.value(1); m1.value(1)
    time.sleep_ms(40)
    ok = _wait_aux_high(1000)
    time.sleep_ms(20)
    return ok


def _drain_uart():
    while True:
        chunk = uart.read()
        if not chunk:
            return


def _print_reg(label, resp):
    if not resp or len(resp) < 10:
        print("[STEP1] {}: <truncated, {} bytes>".format(label, len(resp) if resp else 0))
        return
    addh, addl, netid = resp[3], resp[4], resp[5]
    reg0, reg1, ch, reg3 = resp[6], resp[7], resp[8], resp[9]
    tx_method = "FIXED-POINT" if (reg3 & 0x40) else "TRANSPARENT"
    rssi_byte = "ON" if (reg3 & 0x80) else "OFF"
    print("[STEP1] {} (RX={}):".format(label,
          " ".join("{:02x}".format(b) for b in resp[:10])))
    print("[STEP1]   addr=0x{:02x}{:02x} netid=0x{:02x}  reg0=0x{:02x} reg1=0x{:02x}  "
          "channel={} (~{}.125 MHz)  reg3=0x{:02x} ({}/{})"
          .format(addh, addl, netid, reg0, reg1, ch, 850 + ch, reg3, tx_method,
                  "RSSI" if (reg3 & 0x80) else "no RSSI"))


# ============================================================
# Read current config (0xC1 read command)
# ============================================================

def read_config():
    """Issue 0xC1 to read registers 0..6. Must be called while in CONFIG mode.
    Returns the response bytes (or empty)."""
    _drain_uart()
    cmd = bytes([0xC1, 0x00, 0x07])
    uart.write(cmd)
    # xreef-style: switch back to NORMAL, then read
    set_mode_normal()
    resp = uart.read() or b""
    return resp


# ============================================================
# Write config (0xC2 volatile)
# ============================================================

def write_config():
    """Issue 0xC2 with our chosen values. Must be called while in CONFIG mode.
    Returns the response bytes."""
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    cmd = bytes([0xC2, 0x00, 0x07, addh, addl, NETID,
                 _REG0, _REG1, CHANNEL, _REG3])
    print("[STEP1] write_config TX: " + " ".join("{:02x}".format(b) for b in cmd))
    _drain_uart()
    uart.write(cmd)
    set_mode_normal()                # ← xreef order: switch back BEFORE reading reply
    resp = uart.read() or b""
    return resp


# ============================================================
# Configure: read current state, write desired state, verify
# ============================================================

def configure():
    print("[STEP1] Configuring E220: addr={} freq={}MHz ch={} mode=TRANSPARENT  DO_REGISTER_WRITES={}"
          .format(UNIT_ID, FREQ_MHZ, CHANNEL, DO_REGISTER_WRITES))
    print("[STEP1] AUX at boot = {}".format(aux.value()))

    if not DO_REGISTER_WRITES:
        # Mode-bounce only — no register reads/writes — used to isolate
        # whether mode bouncing itself breaks the radio RX path.
        print("[STEP1] DO_REGISTER_WRITES=False — mode-bouncing only, no UART config commands")
        if not set_mode_config():
            print("[STEP1] WARN: AUX not HIGH after entering CONFIG mode")
        # Stay briefly in CONFIG mode so the bounce is real.
        time.sleep_ms(200)
        if not set_mode_normal():
            print("[STEP1] WARN: AUX not HIGH after returning to NORMAL mode")
        return True

    # --- Read current state (diagnostic) ---
    if not set_mode_config():
        print("[STEP1] WARN: AUX not HIGH after entering CONFIG mode (read-back)")
    pre = read_config()
    _print_reg("BEFORE step1 write", pre)

    # --- Write desired state ---
    if not set_mode_config():
        print("[STEP1] WARN: AUX not HIGH after entering CONFIG mode (write)")
    resp = write_config()
    _print_reg("AFTER step1 write ", resp)

    # Validate
    addh = (UNIT_ID >> 8) & 0xFF
    addl = UNIT_ID & 0xFF
    if (len(resp) >= 10 and resp[0] in (0xC0, 0xC1, 0xC2)
            and resp[3] == addh and resp[4] == addl
            and resp[5] == NETID and resp[8] == CHANNEL and resp[9] == _REG3):
        print("[STEP1] CONFIG OK")
        return True
    print("[STEP1] CONFIG FAILED — values don't match expected")
    return False


# ============================================================
# Main
# ============================================================

led(0, 0, 40)        # blue: startup

config_ok = configure()

if not config_ok:
    for _ in range(3):
        led(40, 0, 0); time.sleep_ms(250)
        led(0, 0, 0);  time.sleep_ms(150)

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
