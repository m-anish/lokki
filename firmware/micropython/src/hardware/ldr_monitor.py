import asyncio
from machine import ADC, Pin


class LDRMonitor:

    def __init__(self):
        self._adc = None
        self._window = []
        self._window_size = 60
        self._cap_rules = []        # sorted desc by above_percent
        self._cap_percent = None
        self._ambient_percent = 0
        self._enabled = False
        self._on_cap_change = None
        # Single-point calibration. After mapping ADC → raw %, this
        # rescales so that `calibration_max_percent` reads as 100%.
        # Useful when the LDR sits inside an enclosure that attenuates
        # the maximum brightness: if "exposed = 70%, enclosed = 26%"
        # then set calibration_max_percent = 37 (= 26/70 × 100) so the
        # 26% raw maps back to ~70% for downstream cap rules. Default
        # 100 means no calibration. Range 1..100.
        self._cal_max_percent = 100

    def init_from_config(self, ldr_cfg, hardware_cfg):
        from shared.simple_logger import Logger
        log = Logger()

        # Always initialize ADC for sensor reading
        pin = hardware_cfg.get("ldr_adc_pin", 26)
        self._adc = ADC(Pin(pin))
        self._window_size = ldr_cfg.get("smoothing_window_s", 60)

        # Calibration. Clamped to [1, 100] — 0 would divide by zero,
        # >100 would be a no-op compression.
        cal = ldr_cfg.get("calibration_max_percent", 100)
        if not isinstance(cal, int) or cal < 1:
            cal = 100
        elif cal > 100:
            cal = 100
        self._cal_max_percent = cal
        # Reset smoothing window so old un-calibrated samples don't
        # drag the smoothed value while the new calibration settles.
        self._window = []

        # Cap rules are optional (enabled flag controls whether caps are applied)
        cap_enabled = ldr_cfg.get("enabled", False)
        if cap_enabled:
            rules = sorted(
                ldr_cfg.get("cap_rules", []),
                key=lambda r: r.get("above_percent", 0),
                reverse=True,
            )
            self._cap_rules = rules
            log.info(f"[LDR] Initialized on GP{pin}, window={self._window_size}s, cal_max={cal}%, cap_rules={len(rules)} (ENABLED)")
        else:
            self._cap_rules = []
            log.info(f"[LDR] Initialized on GP{pin}, window={self._window_size}s, cal_max={cal}%, cap_rules=DISABLED")

        # Always enable sensor reading
        self._enabled = True

    def on_cap_change(self, callback):
        self._on_cap_change = callback

    @property
    def ambient_percent(self):
        return self._ambient_percent

    @property
    def cap_percent(self):
        return self._cap_percent

    def _read_adc(self):
        raw = self._adc.read_u16()       # 0–65535
        # LDR is on bottom of voltage divider (connected to GND)
        # Low ADC = dark (LDR high resistance), High ADC = bright (LDR low resistance)
        # 0 ADC = dark (0%), 65535 ADC = bright (100%)
        raw_percent = int(raw * 100 / 65535)
        # Single-point calibration: rescale so that cal_max_percent
        # reads as 100. With the default cal_max=100 this is a no-op.
        # When the LDR is inside an enclosure the max practical reading
        # is well below 100% (it never sees direct full daylight); the
        # operator measures the enclosed reading vs an exposed
        # reference and sets cal_max_percent accordingly, restoring a
        # meaningful 0-100 scale for downstream cap rules + dashboard
        # display.
        if self._cal_max_percent >= 100:
            return raw_percent
        scaled = (raw_percent * 100) // self._cal_max_percent
        if scaled > 100:
            return 100
        if scaled < 0:
            return 0
        return scaled

    def _compute_cap(self, ambient):
        for rule in self._cap_rules:
            if ambient > rule.get("above_percent", 0):
                return rule.get("cap_percent", 100)
        return None

    async def run(self):
        from shared.simple_logger import Logger
        log = Logger()
        
        if not self._enabled:
            while True:
                await asyncio.sleep_ms(1000)

        log_counter = 0
        while True:
            raw_adc = self._adc.read_u16() if self._adc else 0
            reading = self._read_adc()
            self._window.append(reading)
            if len(self._window) > self._window_size:
                self._window.pop(0)

            self._ambient_percent = sum(self._window) // len(self._window)
            new_cap = self._compute_cap(self._ambient_percent)

            # Debug logging every 10 seconds
            log_counter += 1
            if log_counter >= 10:
                log.debug(f"[LDR] raw_adc={raw_adc}, reading={reading}%, smoothed={self._ambient_percent}%, cap={new_cap}")
                log_counter = 0

            if new_cap != self._cap_percent:
                self._cap_percent = new_cap
                if self._on_cap_change:
                    self._on_cap_change(new_cap)

            await asyncio.sleep_ms(1000)


ldr_monitor = LDRMonitor()
