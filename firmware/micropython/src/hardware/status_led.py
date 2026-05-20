import neopixel
from machine import Pin


# WS2812 visually-calibrated color palette.
#
# Why a palette: the three channels on a WS2812 are not perceptually
# linear. Pure red at digital 255 is much brighter than pure blue at 255.
# Green at low values looks weak relative to red at the same value, so
# CSS amber (255, 191, 0) actually displays as red-orange on a WS2812 —
# the green channel disappears. These constants are tuned for "this is
# what the colour name suggests on a WS2812" rather than matching their
# sRGB monitor counterparts. Use these constants in _STATES, never raw
# tuples, so colours stay consistent and recalibratable in one place.
COLOR_WHITE   = (255, 255, 255)
COLOR_RED     = (255, 0,   0)
COLOR_GREEN   = (0,   255, 0)
COLOR_BLUE    = (0,   80,  255)
COLOR_AMBER   = (220, 255, 0)    # green-dominant so it reads as amber/yellow on WS2812 (red appears stronger than green at same digital value)
COLOR_ORANGE  = (255, 100, 0)
COLOR_YELLOW  = (255, 220, 0)
COLOR_CYAN    = (0,   255, 220)
COLOR_MAGENTA = (255, 0,   160)
COLOR_PURPLE  = (180, 0,   200)


# Named states → (r, g, b, brightness 0.0–1.0, pattern)
# Patterns: "solid", "pulse", "blink"
_STATES = {
    "booting":           COLOR_WHITE   + (0.15, "pulse"), # white pulse — booting up, not yet ready
    "wifi_connecting":   (0, 100, 255) + (0.4,  "blink"), # blue blink — trying to connect to WiFi
    "lora_init":         COLOR_CYAN    + (0.3,  "solid"), # cyan solid — WiFi up, now initializing LoRa
    "running_ok":        COLOR_GREEN   + (0.08, "solid"), # green solid — all systems nominal
    # Same green-solid base as running_ok. The "lora is alive" cue is
    # now an event-driven flash via flash_event() — see callers in
    # main.py heartbeat send/receive paths.
    "running_lora_ok":   COLOR_GREEN   + (0.08, "solid"),
    "leaf_offline":      COLOR_ORANGE  + (0.15, "solid"), # orange solid — leaf is running but not connected to coord
    "manual_override":   COLOR_MAGENTA + (0.2,  "solid"), # magenta — manual control active
    "error":             COLOR_RED     + (0.5,  "blink"), # red blink — something's wrong
    "lora_recovering":   COLOR_RED     + (0.3,  "pulse"), # slow red pulse — LoRa init retry
    "lora_disabled":     COLOR_PURPLE  + (0.25, "solid"), # purple solid — LoRa init failed 3×
    "reset_armed":       COLOR_AMBER   + (0.6,  "solid"), # amber solid — button is held; release for soft_reset
    "reset_warning":     COLOR_RED     + (0.9,  "blink"), # red fast blink — keep holding to commit factory reset
    "off":               (0, 0, 0)     + (0.0,  "solid"), # off
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
        # When locked, set_state() and flash_event() both become no-ops.
        # Used by callers who own the LED for an irreversible UI moment
        # (currently: the reset_button hold-time feedback) so periodic
        # background tasks — leaf_status_task fires every 2 s,
        # fleet_timeout_task every 10 s, HB flash on every receive —
        # don't trample the amber/red-blink sequence mid-gesture.
        # Callers acquire via lock(True), pass force=True on set_state
        # to write through the lock, and release via lock(False).
        self._locked = False
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
        # Same rationale as set_state: write immediately so the LED
        # reflects the current state right after config init, not
        # waiting for run_pattern's next tick.
        self._write(self._brightness)

    def set_state(self, state_name, force=False):
        # Respect lock unless the caller explicitly opts out. Callers
        # that hold the lock pass force=True to update the locked LED.
        if self._locked and not force:
            return
        entry = _STATES.get(state_name, _STATES["off"])
        self._r, self._g, self._b, self._brightness, self._pattern = entry
        self._state_name = state_name
        self._log.debug(f"[LED] set_state({state_name}) -> pattern={self._pattern}")
        # Always write the new color immediately, regardless of pattern.
        # If we only wrote for "solid" (as before), the LED would hold
        # the previous color until run_pattern's next loop iteration
        # picked up the new pattern — up to 50 ms later. That gap can
        # produce a visible mid-transition glitch (e.g. amber→red blink
        # showing a brief green frame on the WS2812 update boundary).
        # Writing immediately makes set_state visually atomic; the
        # blink/pulse pattern then takes over from there.
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
        latency). Safe to call from sync code. Coalesces: if a flash is
        already pending, the new one replaces it — better than letting
        requests queue up unbounded during a heartbeat storm. Becomes
        a no-op when the LED is locked."""
        if self._locked:
            return
        self._flash_pending = (r, g, b, brightness, ms)

    def lock(self, locked):
        """Acquire / release exclusive ownership of the LED. While
        locked, both set_state() (without force=True) and flash_event()
        are no-ops. Used by reset_button so leaf_status_task / HB-flash
        / fleet_timeout_task can't trample the hold-time feedback."""
        self._locked = bool(locked)
        if locked:
            # Clear any flash that was already queued but not yet displayed.
            self._flash_pending = None

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
