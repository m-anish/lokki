# ----------------------------------------------------------------------
# Standalone E220-900T22D LoRa link test — minimal baseline.
#
# Mirrors the known-working reference: NO register-mode configuration of
# the module is performed. M0/M1 are pulled low (NORMAL mode), the UART
# is opened at 9600 baud, and we trust the module's factory defaults
# (transparent transmission, factory channel, factory address). All
# units flashed with this script will hear each other automatically.
#
# This is deliberately the simplest possible test, so a "no PINGs ever
# arrive" failure can be ruled out at the radio-path level before we
# add any of our own configuration logic on top.
#
# Each unit ships the same code; UNIT_ID below just goes into the
# message text so you can tell who is who in the logs.
#
# When this works reliably, we incrementally add:
#   1. register-mode config write (set per-unit address)
#   2. fixed-point addressing for unicast
#   3. RSSI byte append
#   4. our protocol envelope, etc.
# ----------------------------------------------------------------------

import time
import neopixel
from machine import Pin, UART


# ============================================================
# CONFIG — edit before flashing
# ============================================================
UNIT_ID  = 0          # 0 = coordinator-side, 1..8 = leaves (just used in messages)

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

PING_INTERVAL_MS = 2500


# ============================================================
# WS2812 helper (with optional RGB/GRB swap)
# ============================================================
_np = neopixel.NeoPixel(Pin(LED_PIN), 1)

def led(r, g, b):
    if LED_ORDER == "RGB":
        _np[0] = (g, r, b)
    else:
        _np[0] = (r, g, b)
    _np.write()


# ============================================================
# Module setup — ZERO register-mode configuration, factory defaults
# ============================================================
m0 = Pin(M0_PIN, Pin.OUT)
m1 = Pin(M1_PIN, Pin.OUT)
aux = Pin(AUX_PIN, Pin.IN)

m0.value(0)
m1.value(0)

uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


# ============================================================
# Main loop — bidirectional broadcast in factory transparent mode
# ============================================================

print("[TEST] Started. unit_id={}, no module config (factory defaults).".format(UNIT_ID))
led(0, 0, 40)               # blue = startup

counter = 0
last_send_ms = time.ticks_ms()

# Idle hint: dim yellow until we hear from the peer.
led(20, 20, 0)

while True:
    # ---- RX ----
    if uart.any():
        data = uart.read()
        if data:
            try:
                text = data.decode("utf-8", "ignore").strip()
            except Exception:
                text = repr(data)
            if text:
                print("[RX] {!r}".format(text))
                led(40, 0, 0)               # red flash on rx (per reference)
                time.sleep_ms(150)
                led(20, 20, 0)              # idle yellow

    # ---- TX every PING_INTERVAL_MS ----
    if time.ticks_diff(time.ticks_ms(), last_send_ms) >= PING_INTERVAL_MS:
        msg = "Hello {} from unit {}".format(counter, UNIT_ID)
        uart.write(msg + "\n")
        print("[TX] {!r}".format(msg))
        led(0, 40, 0)                       # green flash on tx
        time.sleep_ms(100)
        led(20, 20, 0)                      # idle yellow
        counter += 1
        last_send_ms = time.ticks_ms()

    time.sleep_ms(20)
