import time
from core.config_manager import config_manager
from shared.simple_logger import Logger

log = Logger()

_HEARTBEAT_TIMEOUT_S = 90

# Sentinel unit_id for "this leaf has been factory-reset and is
# waiting to be claimed via the dashboard wizard." Multiple unclaimed
# leaves share this unit_id on the air, but each one carries a unique
# chip UID in its HB payload so the coord can tell them apart and the
# claim wizard can target a specific device.
_UNCLAIMED_UNIT_ID = 99


class FleetManager:
    """Tracks state of all leaf units, updated by incoming heartbeats."""

    def __init__(self):
        # Claimed leaves are keyed by their unit_id (1..8). The coordinator
        # itself is also tracked here at id 0 by handle_fleet_status's
        # synthetic injection, but never through fleet_manager.update.
        self._units = {}                # {unit_id: state_dict}
        # Unclaimed leaves all share unit_id=99 on the air, so we key
        # them by chip UID instead. The dashboard renders these as
        # "New device" cards and the claim wizard targets a specific
        # UID. Cleared per-entry on successful claim.
        self._unclaimed = {}             # {chip_uid_hex: state_dict}
        self._timeout_s = _HEARTBEAT_TIMEOUT_S

    def init(self):
        peers = config_manager.get("system").get("peers", [])
        for uid in peers:
            self._units[uid] = self._empty(uid)
        # heartbeat_timeout_s lives in `system`, not `lora` — a long-
        # standing latent bug. Read here only for the boot log; the
        # actual timeout check in check_timeouts() consults
        # config_manager dynamically so hot-applied changes take
        # effect on the next tick.
        boot_timeout = config_manager.get("system").get("heartbeat_timeout_s", _HEARTBEAT_TIMEOUT_S)
        self._timeout_s = boot_timeout
        log.info(f"[FLEET] Tracking {len(peers)} peer(s), timeout={boot_timeout}s")

    # ------------------------------------------------------------------
    # Heartbeat update
    # ------------------------------------------------------------------

    def update(self, unit_id, payload):
        """Called by lora_protocol HB and SRP handlers. Routes the
        payload to either the claimed-leaves dict (keyed by unit_id)
        or the unclaimed-leaves dict (keyed by chip UID) based on
        whether unit_id == _UNCLAIMED_UNIT_ID.

        Note on RSSI: we prefer the coordinator's *locally measured*
        RSSI of this very frame (from `lora_protocol.last_rx_rssi`)
        over whatever the remote unit reported in the payload.
        Coord-side RSSI tells us "how well the coord is hearing this
        leaf right now", which is what the dashboard signal column
        should reflect. The leaf-reported value (their view of the
        coord) is kept on `rssi_remote` for diagnostics.
        """
        # Unclaimed leaves: key by chip UID, not by unit_id (which
        # collides at 99 for every freshly-factory-reset device).
        if unit_id == _UNCLAIMED_UNIT_ID:
            chip_uid = payload.get("uid")
            if not chip_uid:
                # No UID in payload — older firmware or corrupt frame.
                # We can't disambiguate, so drop it.
                log.warn("[FLEET] Ignored unclaimed HB without 'uid' field")
                return
            if chip_uid not in self._unclaimed:
                self._unclaimed[chip_uid] = self._empty_unclaimed(chip_uid)
                log.info(f"[FLEET] New unclaimed leaf detected, chip UID {chip_uid}")
            self._fill(self._unclaimed[chip_uid], payload)
            return

        # Claimed leaves: keyed by unit_id as before. If the leaf is
        # sending a UID, stash it on the state too — handy for the
        # dashboard to render alongside the leaf's name.
        if unit_id not in self._units:
            self._units[unit_id] = self._empty(unit_id)
        u = self._units[unit_id]
        was_offline = not u["online"]
        self._fill(u, payload)
        if was_offline:
            log.info(f"[FLEET] Unit {unit_id} ({u['name'] or unit_id}) is back online")

    # ------------------------------------------------------------------
    # Internal: fill a state dict from an HB/SRP payload
    # ------------------------------------------------------------------

    def _fill(self, u, payload):
        """Apply an HB/SRP payload to the per-unit state dict.

        Wire keys are short (see main.heartbeat_broadcast_task for the
        full table); internal/API keys here are long and stable so the
        dashboard and /api/fleet shape don't change.

        Wire → internal mapping:
          n   → name         (last-known unit_name)
          up  → uptime
          ch/rl/pir/ldr      → same (already short)
          r   → rssi_remote  (leaf's view of coord)
          tc  → rtc_t        (DS3231 die temp °C, optional)
          uid → uid          (only present on unclaimed-leaf HBs)
          err → err          (only present when non-zero; absent → 0)
          sc  → scenes       (SRP only)
        """
        u["online"]    = True
        u["last_seen"] = time.time()
        u["uptime"]    = payload.get("up", u["uptime"] or 0)
        u["name"]      = payload.get("n", u.get("name") or "")
        # uid only carried by unclaimed leaves. Don't overwrite a
        # cached UID on a claimed leaf just because the new HB omits
        # the field.
        if "uid" in payload:
            u["uid"]   = payload["uid"]
        u["ch"]        = payload.get("ch", u["ch"])
        u["rl"]        = payload.get("rl", u["rl"])
        u["pir"]       = payload.get("pir", u["pir"])
        u["ldr"]       = payload.get("ldr", u["ldr"])
        u["sensors"]   = payload.get("sensors", u["sensors"])
        # Error count: leaves omit this field when zero. An absent
        # field thus means "current count is 0", NOT "no update". This
        # also lets the count correctly reset to 0 after a leaf reboot.
        u["err"]       = payload.get("err", 0)
        u["scenes"]    = payload.get("sc", u["scenes"])
        # DS3231 die temperature (°C). Optional in HB — leaves with a
        # dead RTC bus simply omit the field. Useful trend signal even
        # when no BME280 / SHT3x is wired in.
        u["rtc_t"]     = payload.get("tc", u.get("rtc_t"))
        # Coord-side RSSI from the protocol layer; fall back to the
        # remote-reported value if not present. "r" (short) is the
        # leaf's reported RSSI of the *coord*'s last frame it heard.
        from comms.lora_protocol import lora_protocol as _proto
        local_rssi = _proto.last_rx_rssi
        if local_rssi is not None:
            u["rssi"] = local_rssi
        elif "r" in payload:
            u["rssi"] = payload.get("r")
        u["rssi_remote"] = payload.get("r", u.get("rssi_remote"))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, unit_id):
        return self._units.get(unit_id)

    def get_all(self):
        return dict(self._units)

    def get_unclaimed_all(self):
        """Returns {chip_uid: state_dict} for every unclaimed leaf
        we've seen recently. Caller is responsible for filtering by
        last_seen / online if they want a "currently active" view."""
        return dict(self._unclaimed)

    def get_unclaimed(self, chip_uid):
        return self._unclaimed.get(chip_uid)

    def drop_unclaimed(self, chip_uid):
        """Remove an unclaimed entry. Called after a successful claim
        push so the leaf doesn't keep showing up as 'unclaimed' in
        the dashboard alongside its new claimed unit_id."""
        if chip_uid in self._unclaimed:
            del self._unclaimed[chip_uid]
            log.info(f"[FLEET] Dropped unclaimed entry for chip UID {chip_uid}")

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
        # Read live so a hot-applied system.heartbeat_timeout_s patch
        # takes effect on the next sweep without a reboot. Cheap —
        # this runs every ~10 s.
        timeout_s = config_manager.get("system").get("heartbeat_timeout_s", _HEARTBEAT_TIMEOUT_S)
        now = time.time()
        for uid, u in self._units.items():
            if u["online"] and (now - u["last_seen"]) > timeout_s:
                self.mark_offline(uid)
        # Unclaimed leaves also time out — if we haven't heard from a
        # freshly-factory-reset device for a while, drop it from the
        # dashboard so stale "New device" cards don't accumulate.
        stale_uids = [k for k, v in self._unclaimed.items()
                      if v["online"] and (now - v["last_seen"]) > timeout_s]
        for k in stale_uids:
            self._unclaimed[k]["online"] = False
            log.warn(f"[FLEET] Unclaimed chip UID {k} marked offline")

    # ------------------------------------------------------------------
    # Internal: per-unit state templates
    # ------------------------------------------------------------------

    @staticmethod
    def _empty(unit_id):
        return {
            "unit_id":   unit_id,
            "name":      "",
            "uid":       "",      # chip UID, populated on first HB carrying one
            "online":    False,
            "last_seen": 0,
            "uptime":    0,
            "ch":        [0] * 8,
            "rl":        [0, 0],
            "pir":       [0, 0, 0, 0],
            "ldr":       None,
            "sensors":   {},
            "err":       0,
            "rssi":      None,
            "scenes":    [],
            "rtc_t":     None,
        }

    @staticmethod
    def _empty_unclaimed(chip_uid):
        return {
            "unit_id":   _UNCLAIMED_UNIT_ID,
            "name":      "",
            "uid":       chip_uid,
            "online":    False,
            "last_seen": 0,
            "uptime":    0,
            "ch":        [0] * 8,
            "rl":        [0, 0],
            "pir":       [0, 0, 0, 0],
            "ldr":       None,
            "sensors":   {},
            "err":       0,
            "rssi":      None,
            "scenes":    [],
            "rtc_t":     None,
            "unclaimed": True,    # marker for dashboard rendering
        }


fleet_manager = FleetManager()
