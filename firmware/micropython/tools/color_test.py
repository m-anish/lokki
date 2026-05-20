"""Status-LED color cycle test.

Walks the WS2812 status LED through Red → Green → Blue → Yellow →
Amber → Purple → Cyan → White, one colour per second, in a loop.

Useful for:
  * Confirming your WS2812 actually responds to colour writes from
    the Pico (vs. some channel being dead).
  * Eyeballing how a given (R, G, B) tuple actually renders on your
    specific chip — useful when calibrating named-colour constants
    in src/hardware/status_led.py.

Reads hardware.status_led_pin and hardware.led_color_order from
/config.json so it Just Works on a configured device. Falls back to
GPIO 5 + GRB if no config is present.

Run via mpremote without flashing:

    mpremote run firmware/micropython/tools/color_test.py

…or after flashing with update.sh (which ships this file to /tools/
on the device):

    mpremote exec "exec(open('/tools/color_test.py').read())"

Ctrl-C to stop.
"""

import time
import json
import neopixel
from machine import Pin


# Colours, in display order. These are the same constants we use in
# status_led.py — picking them here lets you preview what each
# named-colour state will actually look like on the LED.
COLORS = [
    ("RED",     (255, 0,   0)),
    ("GREEN",   (0,   255, 0)),
    ("BLUE",    (0,   80,  255)),
    ("YELLOW",  (255, 200, 0)),
    ("AMBER",   (255, 100, 0)),
    ("PURPLE",  (180, 0,   200)),
    ("CYAN",    (0,   255, 220)),
    ("WHITE",   (255, 255, 255)),
]

BRIGHTNESS = 0.3        # match status_led.py's reset_armed brightness
HOLD_S     = 1.0


def _load_hw():
    """Read status_led_pin + led_color_order from /config.json.
    Returns (pin, order) with defaults if config is missing or unreadable."""
    pin   = 5
    order = "GRB"
    try:
        with open("/config.json", "r") as f:
            cfg = json.loads(f.read())
        hw = cfg.get("hardware", {}) or {}
        pin   = int(hw.get("status_led_pin", 5))
        order = hw.get("led_color_order", "GRB")
        if order not in ("GRB", "RGB"):
            order = "GRB"
    except Exception as e:
        print("color_test: couldn't read /config.json, using defaults:", e)
    return pin, order


def _write(np, color_order, r, g, b):
    if color_order == "RGB":
        np[0] = (g, r, b)        # match status_led.py's swap logic
    else:
        np[0] = (r, g, b)
    np.write()


def main():
    pin, order = _load_hw()
    print(f"color_test: GP{pin}, color_order={order}, brightness={BRIGHTNESS}")
    print("Ctrl-C to stop.\n")
    np = neopixel.NeoPixel(Pin(pin), 1)
    try:
        while True:
            for name, (r, g, b) in COLORS:
                rb = int(r * BRIGHTNESS)
                gb = int(g * BRIGHTNESS)
                bb = int(b * BRIGHTNESS)
                _write(np, order, rb, gb, bb)
                print(f"  {name:7s} ({r:3d}, {g:3d}, {b:3d}) → np[0]={(rb, gb, bb)!r}")
                time.sleep(HOLD_S)
    except KeyboardInterrupt:
        print("\ncolor_test: stopped. Turning LED off.")
        _write(np, order, 0, 0, 0)


main()
