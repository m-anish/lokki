import neopixel
from machine import Pin


# Named states → (r, g, b, brightness 0.0–1.0, pattern)
# Patterns: "solid", "pulse", "blink", "heartbeat"
_STATES = {
    "booting":           (255, 255, 255, 0.15, "pulse"), # white pulse — booting up, not yet ready
    "wifi_connecting":   (0,   100, 255, 0.4,  "blink"), # blue blink — trying to connect to WiFi
    "lora_init":         (0,   255, 220, 0.3,  "solid"), # cyan solid — WiFi up, now initializing LoRa
    "running_ok":        (0,   255, 0,   0.08, "solid"), # green solid — all systems nominal, leaf is online and connected to coordinator
    # Green base + periodic blue flash — indicates LoRa is up and active
    "running_lora_ok":   (0,   255, 0,   0.08, "heartbeat"), # same as running_ok but with heartbeat pattern to show LoRa is active
    "leaf_offline":      (255, 180, 0,   0.15, "solid"), # orange solid — leaf is running but not connected to coordinator (e.g. coordinator offline, out of range, or WiFi down on coordinator)
    "manual_override":   (255, 0,   160, 0.2,  "solid"), # magenta — manual control active (more red than blue so it's clearly distinct from the blue heartbeat flash)
    "error":             (255, 0,   0,   0.5,  "blink"), # red blink — something's wrong, e.g. failed to connect to WiFi or LoRa
    "off":               (0,   0,   0,   0.0,  "solid"), # off
}

_BLINK_ON_MS   = 200
_BLINK_OFF_MS  = 200
_PULSE_STEP_MS = 20

# Heartbeat: hold base colour for BASE ms, then flash blue for FLASH ms
_HB_BASE_MS  = 3500
_HB_FLASH_MS = 500
_HB_R, _HB_G, _HB_B = 0, 80, 255   # blue — same convention as Meshtastic
_HB_BRIGHTNESS = 0.4


class StatusLED:

    def __init__(self, gpio_pin=5, num_leds=1):
        self._gpio_pin = gpio_pin
        self._np = neopixel.NeoPixel(Pin(gpio_pin), num_leds)
        self._state_name = "off"
        self._r = self._g = self._b = 0
        self._brightness = 0.0
        self._pattern = "solid"
        self._task = None
        # Byte order on the wire. MicroPython's neopixel writes in GRB which
        # matches standard WS2812 chips. Some clones / variants are RGB-native
        # — same wire bytes get interpreted with R and G swapped, so what we
        # call green displays as red and vice-versa. The config field
        # hardware.led_color_order = "RGB" tells us to swap them ourselves.
        self._color_order = "GRB"
        from shared.simple_logger import Logger
        self._log = Logger()

    def init_from_config(self, hardware_cfg):
        pin = hardware_cfg.get("status_led_pin", 5)
        order = hardware_cfg.get("led_color_order", "GRB")
        if order not in ("GRB", "RGB"):
            order = "GRB"
        self._color_order = order
        if pin != self._gpio_pin:
            self._gpio_pin = pin
            self._np = neopixel.NeoPixel(Pin(pin), 1)
        if self._pattern == "solid":
            self._write(self._brightness)

    def set_state(self, state_name):
        entry = _STATES.get(state_name, _STATES["off"])
        self._r, self._g, self._b, self._brightness, self._pattern = entry
        self._state_name = state_name
        self._log.debug(f"[LED] set_state({state_name}) -> pattern={self._pattern}")
        if self._pattern == "solid":
            self._write(self._brightness)

    @property
    def state_name(self):
        return self._state_name

    def set_colour(self, r, g, b, brightness=1.0):
        self._r, self._g, self._b = r, g, b
        self._brightness = brightness
        self._pattern = "solid"
        self._write(brightness)

    def off(self):
        self.set_state("off")

    def _write(self, brightness):
        b = max(0.0, min(1.0, brightness))
        r = int(self._r * b)
        g = int(self._g * b)
        bb = int(self._b * b)
        # MicroPython's neopixel writes whatever 3-tuple we give it in GRB
        # wire order. For an RGB-native chip we swap r and g so the wire
        # bytes come out correct from the chip's perspective.
        if self._color_order == "RGB":
            self._np[0] = (g, r, bb)
        else:
            self._np[0] = (r, g, bb)
        self._np.write()

    async def run_pattern(self):
        import asyncio
        while True:
            if self._pattern == "blink":
                self._write(self._brightness)
                await asyncio.sleep_ms(_BLINK_ON_MS)
                self._write(0)
                await asyncio.sleep_ms(_BLINK_OFF_MS)
            elif self._pattern == "pulse":
                for step in range(0, 20):
                    self._write(self._brightness * step / 20)
                    await asyncio.sleep_ms(_PULSE_STEP_MS)
                for step in range(20, 0, -1):
                    self._write(self._brightness * step / 20)
                    await asyncio.sleep_ms(_PULSE_STEP_MS)
            elif self._pattern == "heartbeat":
                # Hold base colour, checking every 100 ms for a state change
                steps = _HB_BASE_MS // 100
                for _ in range(steps):
                    self._write(self._brightness)
                    await asyncio.sleep_ms(100)
                    if self._pattern != "heartbeat":
                        break
                else:
                    # State unchanged — fire the blue flash. Honour the same
                    # color-order swap the rest of the LED uses.
                    fr = int(_HB_R * _HB_BRIGHTNESS)
                    fg = int(_HB_G * _HB_BRIGHTNESS)
                    fb = int(_HB_B * _HB_BRIGHTNESS)
                    if self._color_order == "RGB":
                        self._np[0] = (fg, fr, fb)
                    else:
                        self._np[0] = (fr, fg, fb)
                    self._np.write()
                    self._log.debug(f"[LED] Heartbeat blue flash: color_order={self._color_order} rgb=({fr},{fg},{fb})")
                    await asyncio.sleep_ms(_HB_FLASH_MS)
            else:
                # solid — nothing to animate, yield and wait
                await asyncio.sleep_ms(100)


status_led = StatusLED()
