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

    def init_from_config(self, ldr_cfg, hardware_cfg):
        from shared.simple_logger import Logger
        log = Logger()
        self._enabled = ldr_cfg.get("enabled", False)
        if not self._enabled:
            log.info("[LDR] Disabled in config")
            return
        pin = hardware_cfg.get("ldr_adc_pin", 26)
        self._adc = ADC(Pin(pin))
        self._window_size = ldr_cfg.get("smoothing_window_s", 60)
        rules = sorted(
            ldr_cfg.get("cap_rules", []),
            key=lambda r: r.get("above_percent", 0),
            reverse=True,
        )
        self._cap_rules = rules
        log.info(f"[LDR] Initialized on GP{pin}, window={self._window_size}s, rules={len(rules)}")

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
        # Invert: LDR is on top of voltage divider, so low voltage = bright light
        # 0 ADC = bright (100%), 65535 ADC = dark (0%)
        return 100 - int(raw * 100 / 65535)

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
                log.info(f"[LDR] raw_adc={raw_adc}, inverted={reading}%, smoothed={self._ambient_percent}%, cap={new_cap}")
                log_counter = 0

            if new_cap != self._cap_percent:
                self._cap_percent = new_cap
                if self._on_cap_change:
                    self._on_cap_change(new_cap)

            await asyncio.sleep_ms(1000)


ldr_monitor = LDRMonitor()
