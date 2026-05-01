import neopixel
from machine import Pin


# Named states → (r, g, b, brightness 0.0–1.0, pattern)
# Patterns: "solid", "pulse", "blink", "heartbeat"
_STATES = {
    "booting":           (255, 255, 255, 0.15, "pulse"),
    "wifi_connecting":   (0,   100, 255, 0.4,  "blink"),
    "lora_init":         (0,   255, 220, 0.3,  "solid"),
    "running_ok":        (0,   255, 0,   0.08, "solid"),
    # Green base + periodic blue flash — indicates LoRa is up and active
    "running_lora_ok":   (0,   255, 0,   0.08, "heartbeat"),
    "leaf_offline":      (255, 180, 0,   0.15, "solid"),
    "manual_override":   (160, 0,   255, 0.1,  "solid"),
    "error":             (255, 0,   0,   0.5,  "blink"),
    "off":               (0,   0,   0,   0.0,  "solid"),
}

_BLINK_ON_MS   = 200
_BLINK_OFF_MS  = 200
_PULSE_STEP_MS = 20

# Heartbeat: hold base colour for BASE ms, then flash blue for FLASH ms
_HB_BASE_MS  = 3900
_HB_FLASH_MS = 100
_HB_R, _HB_G, _HB_B = 0, 80, 255   # blue — same convention as Meshtastic
_HB_BRIGHTNESS = 0.4


class StatusLED:

    def __init__(self, gpio_pin=5, num_leds=1):
        self._np = neopixel.NeoPixel(Pin(gpio_pin), num_leds)
        self._state_name = "off"
        self._r = self._g = self._b = 0
        self._brightness = 0.0
        self._pattern = "solid"
        self._task = None

    def set_state(self, state_name):
        entry = _STATES.get(state_name, _STATES["off"])
        self._r, self._g, self._b, self._brightness, self._pattern = entry
        self._state_name = state_name
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
        self._np[0] = (
            int(self._r * b),
            int(self._g * b),
            int(self._b * b),
        )
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
                    # State unchanged — fire the blue flash
                    self._np[0] = (
                        int(_HB_R * _HB_BRIGHTNESS),
                        int(_HB_G * _HB_BRIGHTNESS),
                        int(_HB_B * _HB_BRIGHTNESS),
                    )
                    self._np.write()
                    await asyncio.sleep_ms(_HB_FLASH_MS)
            else:
                # solid — nothing to animate, yield and wait
                await asyncio.sleep_ms(100)


status_led = StatusLED()
