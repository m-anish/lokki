import neopixel
from machine import Pin


# Named states → (r, g, b, brightness 0.0–1.0, pattern)
# Patterns: "solid", "pulse", "blink"
_STATES = {
    "booting":           (255, 255, 255, 0.15, "pulse"), # white pulse — booting up, not yet ready
    "wifi_connecting":   (0,   100, 255, 0.4,  "blink"), # blue blink — trying to connect to WiFi
    "lora_init":         (0,   255, 220, 0.3,  "solid"), # cyan solid — WiFi up, now initializing LoRa
    "running_ok":        (0,   255, 0,   0.08, "solid"), # green solid — all systems nominal, leaf is online and connected to coordinator
    # Same green-solid base as running_ok. The "lora is alive" cue is now
    # an event-driven flash via flash_event() — see callers in main.py
    # heartbeat send/receive paths. Color of the flash (blue vs red)
    # surfaces the boot-time lora_transport.config_ok result.
    "running_lora_ok":   (0,   255, 0,   0.08, "solid"),
    "leaf_offline":      (255, 180, 0,   0.15, "solid"), # orange solid — leaf is running but not connected to coordinator (e.g. coordinator offline, out of range, or WiFi down on coordinator)
    "manual_override":   (255, 0,   160, 0.2,  "solid"), # magenta — manual control active (more red than blue so it's clearly distinct from the blue heartbeat flash)
    "error":             (255, 0,   0,   0.5,  "blink"), # red blink — something's wrong, e.g. failed to connect to WiFi or LoRa
    "lora_recovering":   (255, 0,   0,   0.3,  "pulse"), # slow red pulse — LoRa init failed, soft-resetting to retry
    "lora_disabled":     (180, 0,   200, 0.25, "solid"), # purple solid — LoRa init failed 3× → running without LoRa
    # Reset-button hold feedback. Operator presses → debounce → armed
    # (amber) → warning (red blink) → factory reset.
    "reset_armed":       (255, 140, 0,   0.6,  "solid"), # amber solid — button is held; release for soft_reset
    "reset_warning":     (255, 0,   0,   0.9,  "blink"), # red fast blink — keep holding to commit factory reset
    "off":               (0,   0,   0,   0.0,  "solid"), # off
}

_BLINK_ON_MS   = 200
_BLINK_OFF_MS  = 200
_PULSE_STEP_MS = 20

# Event-flash defaults (used by main.py's HB send/receive paths via
# flash_event). Same blue convention as Meshtastic; red for the
# "lora config failed at boot" case.
_FLASH_MS              = 80      # short, snappy event indicator
_FLASH_BRIGHTNESS      = 0.4
FLASH_LORA_OK_RGB      = (0,   80,  255)   # blue
FLASH_LORA_FAIL_RGB    = (255, 0,   0)     # red
FLASH_BOOT_RGB         = (255, 255, 255)   # white — "I just woke up"


class StatusLED:

    def __init__(self, gpio_pin=5, num_leds=1):
        self._gpio_pin = gpio_pin
        self._np = neopixel.NeoPixel(Pin(gpio_pin), num_leds)
        self._state_name = "off"
        self._r = self._g = self._b = 0
        self._brightness = 0.0
        self._pattern = "solid"
        self._task = None
        # One-shot flash request. None = no pending flash; otherwise
        # (r, g, b, brightness, ms). run_pattern picks it up on its
        # next loop iteration, overrides the base color for ms, then
        # restores. Calling flash_event() from sync code is safe — the
        # request is just a tuple swap, no async overhead.
        self._flash_pending = None
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

    def flash_event(self, r, g, b, brightness=_FLASH_BRIGHTNESS, ms=_FLASH_MS):
        """Request a one-shot LED flash, overriding the current base color
        for `ms`. Picked up by run_pattern on its next loop tick (≤100 ms
        latency). Safe to call from sync code (LoRa receive handlers,
        heartbeat send path). Coalesces: if a flash is already pending,
        the new one replaces it — better than letting requests queue up
        unbounded during a heartbeat storm."""
        self._flash_pending = (r, g, b, brightness, ms)

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
            # Event-flash takes priority over the base pattern. This is
            # how heartbeat send/receive surfaces visually: callers fire
            # flash_event() and we briefly override the base color here,
            # then restore. Polled rather than awaited so sync callers
            # don't need an event loop reference.
            if self._flash_pending is not None:
                fr, fg, fb, fbright, fms = self._flash_pending
                self._flash_pending = None
                # Write the flash color directly, bypassing self._r/g/b
                # so we don't corrupt the base state.
                br = max(0.0, min(1.0, fbright))
                pr, pg, pb = int(fr * br), int(fg * br), int(fb * br)
                if self._color_order == "RGB":
                    self._np[0] = (pg, pr, pb)
                else:
                    self._np[0] = (pr, pg, pb)
                self._np.write()
                await asyncio.sleep_ms(fms)
                # Restore base. For animated patterns the next tick of
                # the loop redraws naturally; for solid we redraw now.
                if self._pattern == "solid":
                    self._write(self._brightness)
                continue

            # Snapshot the state we entered the loop with. If anything
            # in this tuple changes mid-cycle (a set_state from the
            # main flow, or a flash_event waiting to be picked up),
            # we bail out early so the loop doesn't keep rendering
            # the previous pattern's brightness curve with the *new*
            # color/brightness — the "green-fadeout-from-a-purple-base"
            # bug that previously made set_state transitions look
            # weird and ate flash_event() requests for up to 800 ms.
            token = (self._pattern, self._r, self._g, self._b, self._brightness)

            def _state_unchanged():
                return ((self._pattern, self._r, self._g, self._b, self._brightness) == token
                        and self._flash_pending is None)

            if self._pattern == "blink":
                self._write(self._brightness)
                # Poll every 20 ms during the ON half so a fresh
                # set_state / flash_event is honored within ~20 ms.
                slept = 0
                while slept < _BLINK_ON_MS and _state_unchanged():
                    await asyncio.sleep_ms(20)
                    slept += 20
                if not _state_unchanged():
                    continue
                self._write(0)
                slept = 0
                while slept < _BLINK_OFF_MS and _state_unchanged():
                    await asyncio.sleep_ms(20)
                    slept += 20
            elif self._pattern == "pulse":
                interrupted = False
                for step in range(0, 20):
                    if not _state_unchanged():
                        interrupted = True
                        break
                    self._write(self._brightness * step / 20)
                    await asyncio.sleep_ms(_PULSE_STEP_MS)
                if interrupted:
                    continue
                for step in range(20, 0, -1):
                    if not _state_unchanged():
                        break
                    self._write(self._brightness * step / 20)
                    await asyncio.sleep_ms(_PULSE_STEP_MS)
            else:
                # solid — nothing to animate, just yield so flash_event
                # gets polled on a tight cadence.
                await asyncio.sleep_ms(50)


status_led = StatusLED()
