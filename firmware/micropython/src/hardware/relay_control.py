from machine import Pin
from shared.simple_logger import Logger

log = Logger()


class RelayChannel:

    def __init__(self, relay_id, gpio_pin, default_state="off"):
        self.relay_id = relay_id
        self.gpio_pin = gpio_pin
        # Active HIGH — MOSFET gate drives relay coil
        self._pin = Pin(gpio_pin, Pin.OUT, value=0)
        self._state = False
        if default_state == "on":
            self.set(True)

    def set(self, state):
        # Accept bool, "on"/"off", or 1/0
        if isinstance(state, str):
            state = state.lower() == "on"
        else:
            state = bool(state)
        self._state = state
        self._pin.value(1 if state else 0)

    @property
    def state(self):
        return self._state

    @property
    def state_str(self):
        return "on" if self._state else "off"

    def deinit(self):
        self._pin.value(0)


class RelayController:

    def __init__(self):
        self._relays = {}

    def init_from_config(self, relays_cfg):
        log.info("[RELAY] Initializing...")
        for r in relays_cfg:
            rid = r.get("id")
            pin = r.get("gpio_pin")
            default = r.get("default_state", "off")
            enabled = r.get("enabled", False)
            if rid and pin is not None:
                if rid in self._relays:
                    self._relays[rid].deinit()
                self._relays[rid] = RelayChannel(rid, pin, default)
                log.info(f"[RELAY] {rid}: GPIO{pin}, default={default}, enabled={enabled}")
        log.info(f"[RELAY] Initialized {len(self._relays)} relay(s)")

    def set(self, relay_id, state):
        r = self._relays.get(relay_id)
        if r:
            r.set(state)

    def get(self, relay_id):
        r = self._relays.get(relay_id)
        return r.state if r else False

    def get_all(self):
        return {rid: r.state for rid, r in self._relays.items()}

    def deinit(self):
        for r in self._relays.values():
            r.deinit()
        self._relays.clear()


relay_controller = RelayController()
