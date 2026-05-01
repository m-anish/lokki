import time
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

_HEARTBEAT_TIMEOUT_S = 90


class FleetManager:
    """Tracks state of all leaf units, updated by incoming heartbeats."""

    def __init__(self):
        self._units = {}      # {unit_id: state_dict}
        self._timeout_s = _HEARTBEAT_TIMEOUT_S

    def init(self):
        peers = config_manager.get("system").get("peers", [])
        for uid in peers:
            self._units[uid] = self._empty(uid)
        t = config_manager.get("lora").get("heartbeat_timeout_s", _HEARTBEAT_TIMEOUT_S)
        self._timeout_s = t
        log.info(f"[FLEET] Tracking {len(peers)} peer(s), timeout={self._timeout_s}s")

    # ------------------------------------------------------------------
    # Heartbeat update
    # ------------------------------------------------------------------

    def update(self, unit_id, payload):
        """Called by lora_protocol HB handler."""
        if unit_id not in self._units:
            self._units[unit_id] = self._empty(unit_id)

        u = self._units[unit_id]
        was_offline = not u["online"]
        u["online"]    = True
        u["last_seen"] = time.time()
        u["uptime"]    = payload.get("uptime", 0)
        u["ch"]        = payload.get("ch", u["ch"])
        u["rl"]        = payload.get("rl", u["rl"])
        u["pir"]       = payload.get("pir", u["pir"])
        u["ldr"]       = payload.get("ldr", u["ldr"])
        u["sensors"]   = payload.get("sensors", u["sensors"])
        u["err"]       = payload.get("err", u["err"])
        u["scenes"]    = payload.get("sc", u["scenes"])

        if was_offline:
            log.info(f"[FLEET] Unit {unit_id} is back online")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, unit_id):
        return self._units.get(unit_id)

    def get_all(self):
        return dict(self._units)

    def is_online(self, unit_id):
        u = self._units.get(unit_id)
        return u is not None and u["online"]

    def mark_offline(self, unit_id):
        if unit_id in self._units and self._units[unit_id]["online"]:
            self._units[unit_id]["online"] = False
            log.warn(f"[FLEET] Unit {unit_id} marked offline")

    # ------------------------------------------------------------------
    # Timeout check (called from async task in main.py)
    # ------------------------------------------------------------------

    def check_timeouts(self):
        now = time.time()
        for uid, u in self._units.items():
            if u["online"] and (now - u["last_seen"]) > self._timeout_s:
                self.mark_offline(uid)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _empty(unit_id):
        return {
            "unit_id":   unit_id,
            "online":    False,
            "last_seen": 0,
            "uptime":    0,
            "ch":        [0] * 8,
            "rl":        [0, 0],
            "pir":       [0, 0, 0, 0],
            "ldr":       None,
            "sensors":   {},
            "err":       0,
            "scenes":    [],
        }


fleet_manager = FleetManager()
