import asyncio
from machine import Pin
from shared.simple_logger import Logger

log = Logger()

_DEBOUNCE_MS = 500


class PIRSensor:

    VACANT = "vacant"
    MOTION = "motion"

    def __init__(self, pir_id, gpio_pin, vacancy_timeout_s, on_motion_cb, on_vacancy_cb):
        self.pir_id = pir_id
        self.gpio_pin = gpio_pin
        self.vacancy_timeout_s = vacancy_timeout_s
        self._on_motion = on_motion_cb
        self._on_vacancy = on_vacancy_cb
        self._pin = Pin(gpio_pin, Pin.IN)
        self._state = self.VACANT
        self._last_motion_ms = 0

    @property
    def state(self):
        return self._state

    async def run(self):
        import time
        debounce_count = 0
        debounce_needed = max(1, _DEBOUNCE_MS // 50)

        while True:
            raw = self._pin.value()

            if raw == 1:
                debounce_count += 1
                if debounce_count >= debounce_needed:
                    self._last_motion_ms = time.ticks_ms()
                    if self._state == self.VACANT:
                        self._state = self.MOTION
                        log.info(
                            f"[PIR] pir{self.pir_id} (GPIO{self.gpio_pin}): "
                            f"VACANT → MOTION"
                        )
                        if self._on_motion:
                            self._on_motion(self.pir_id)
            else:
                debounce_count = 0
                if self._state == self.MOTION:
                    elapsed_s = time.ticks_diff(
                        time.ticks_ms(), self._last_motion_ms
                    ) // 1000
                    if elapsed_s >= self.vacancy_timeout_s:
                        self._state = self.VACANT
                        log.info(
                            f"[PIR] pir{self.pir_id} (GPIO{self.gpio_pin}): "
                            f"MOTION → VACANT (after {elapsed_s}s)"
                        )
                        if self._on_vacancy:
                            self._on_vacancy(self.pir_id)

            await asyncio.sleep_ms(50)


class PIRManager:

    def __init__(self):
        self._sensors = {}
        self._motion_callbacks = {}
        self._vacancy_callbacks = {}

    def on_motion(self, pir_id, callback):
        self._motion_callbacks[pir_id] = callback

    def on_vacancy(self, pir_id, callback):
        self._vacancy_callbacks[pir_id] = callback

    def _broadcast_event(self, pir_id, state):
        """Forward a PIR transition to the coordinator over LoRa so the
        dashboard sees motion/vacancy events near-realtime instead of
        waiting for the next HB (up to 30 s of lag). Lazy-imported to
        keep this module loadable in isolation (unit tests / mocked
        environments) and silently no-ops on the coord (where role
        != leaf) and when LoRa isn't ready (the send-gate in
        lora_protocol.send drops it). state is the string 'motion' or
        'vacancy'."""
        try:
            from comms.lora_protocol import lora_protocol
            from core.config_manager import config_manager
            if config_manager.role != "leaf":
                return
            lora_protocol.send_pir_event(pir_id, state)
        except Exception as e:
            # Never let a forwarding failure break the local PIR
            # action path. The forward is best-effort.
            log.debug(f"[PIR] PIR_EV forward failed: {e}")

    def _motion_fired(self, pir_id):
        self._broadcast_event(pir_id, PIRSensor.MOTION)
        cb = self._motion_callbacks.get(pir_id)
        if cb:
            cb(pir_id)

    def _vacancy_fired(self, pir_id):
        self._broadcast_event(pir_id, PIRSensor.VACANT)
        cb = self._vacancy_callbacks.get(pir_id)
        if cb:
            cb(pir_id)

    def init_from_config(self, pir_cfg):
        log.info("[PIR] Initializing...")
        for p in pir_cfg:
            enabled = p.get("enabled", False)
            pid = p["id"]
            pin = p["gpio_pin"]
            timeout = p.get("vacancy_timeout_s", 60)
            if not enabled:
                log.debug(f"[PIR] pir{pid}: GPIO{pin}, disabled")
                continue
            self._sensors[pid] = PIRSensor(
                pir_id=pid,
                gpio_pin=pin,
                vacancy_timeout_s=timeout,
                on_motion_cb=self._motion_fired,
                on_vacancy_cb=self._vacancy_fired,
            )
            log.debug(f"[PIR] pir{pid}: GPIO{pin}, timeout={timeout}s, enabled=True")
        log.info(f"[PIR] Initialized {len(self._sensors)} sensor(s)")

    def get_state(self, pir_id):
        s = self._sensors.get(pir_id)
        return s.state if s else PIRSensor.VACANT

    def get_all_states(self):
        # Fixed 4-slot positional list. Index i holds 1 if pir id (i+1) is in MOTION, else 0.
        out = [0, 0, 0, 0]
        for pid, s in self._sensors.items():
            if 1 <= pid <= 4:
                out[pid - 1] = 1 if s.state == PIRSensor.MOTION else 0
        return out

    async def run_all(self):
        if not self._sensors:
            while True:
                await asyncio.sleep_ms(1000)
        tasks = [asyncio.create_task(s.run()) for s in self._sensors.values()]
        await asyncio.gather(*tasks)


pir_manager = PIRManager()
