import asyncio
from machine import PWM, Pin
from shared.simple_logger import Logger

log = Logger()


_GAMMA = 2.2  # perceptual correction — set via set_gamma() at boot


def set_gamma(gamma):
    global _GAMMA
    _GAMMA = max(1.0, min(4.0, float(gamma)))


def _duty_from_percent(pct):
    pct = max(0.0, min(100.0, float(pct)))
    if pct <= 0:   return 0
    if pct >= 100: return 65535
    return int((pct / 100.0) ** _GAMMA * 65535)


class PWMChannel:

    def __init__(self, channel_id, gpio_pin, freq_hz=1000):
        self.channel_id = channel_id
        self.gpio_pin = gpio_pin
        self._pwm = PWM(Pin(gpio_pin), freq=freq_hz, duty_u16=0)
        self._duty_pct = 0
        self._fading = False

    def set(self, duty_pct):
        self._duty_pct = max(0, min(100, duty_pct))
        self._pwm.duty_u16(_duty_from_percent(self._duty_pct))

    async def fade_to(self, target_pct, fade_ms):
        target_pct = max(0, min(100, target_pct))
        if fade_ms <= 0 or target_pct == self._duty_pct:
            self.set(target_pct)
            return

        self._fading = True
        start = self._duty_pct
        delta = abs(target_pct - start)
        steps = max(1, delta)
        step_ms = max(10, fade_ms // steps)
        direction = 1 if target_pct > start else -1
        current = start

        while self._fading and current != target_pct:
            current = max(0, min(100, current + direction))
            self._duty_pct = current
            self._pwm.duty_u16(_duty_from_percent(current))
            await asyncio.sleep_ms(step_ms)

        self._fading = False

    def cancel_fade(self):
        self._fading = False

    @property
    def duty_percent(self):
        return self._duty_pct

    def deinit(self):
        self._fading = False
        self._pwm.deinit()


class PWMController:

    def __init__(self):
        self._channels = {}

    def init_from_config(self, led_channels_cfg, freq_hz=1000, gamma=2.2):
        log.info(f"[PWM] Initializing with freq={freq_hz}Hz, gamma={gamma}")
        set_gamma(gamma)
        for ch in led_channels_cfg:
            cid = ch.get("id")
            pin = ch.get("gpio_pin")
            enabled = ch.get("enabled", False)
            if cid and pin is not None:
                if cid in self._channels:
                    self._channels[cid].deinit()
                self._channels[cid] = PWMChannel(cid, pin, freq_hz)
                log.info(f"[PWM] {cid}: GPIO{pin}, enabled={enabled}")
        log.info(f"[PWM] Initialized {len(self._channels)} channel(s)")

    def set(self, channel_id, duty_pct):
        ch = self._channels.get(channel_id)
        if ch:
            ch.cancel_fade()
            ch.set(duty_pct)

    async def fade_to(self, channel_id, target_pct, fade_ms):
        ch = self._channels.get(channel_id)
        if ch:
            ch.cancel_fade()
            await ch.fade_to(target_pct, fade_ms)

    def set_all(self, duty_pct):
        for ch in self._channels.values():
            ch.cancel_fade()
            ch.set(duty_pct)

    def get(self, channel_id):
        ch = self._channels.get(channel_id)
        return ch.duty_percent if ch else 0

    def get_all(self):
        # Return list of duty percentages sorted by channel number
        # CRITICAL: MicroPython dicts don't maintain insertion order!
        # Must return values in a list, sorted by channel ID number
        
        # Sort channels by numeric ID: ch1=1, ch2=2, ..., ch8=8
        def sort_key(item):
            cid, ch = item
            try:
                if cid.startswith('ch'):
                    return int(cid[2:])
                return 999
            except (ValueError, IndexError):
                return 999
        
        sorted_items = sorted(self._channels.items(), key=sort_key)
        
        # Return list of values in sorted order
        result = [ch.duty_percent for cid, ch in sorted_items]
        
        # Debug: show channel IDs and their values
        channel_ids = [cid for cid, ch in sorted_items]
        log.debug(f"[PWM] get_all() channels: {channel_ids} -> values: {result}")
        
        return result

    def deinit(self):
        for ch in self._channels.values():
            ch.deinit()
        self._channels.clear()


pwm_controller = PWMController()
