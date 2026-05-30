import asyncio
from hardware.pwm_control import pwm_controller
from hardware.relay_control import relay_controller
from shared.simple_logger import Logger

log = Logger()


class PriorityArbiter:
    """
    Single source of truth for all output states. Channels and relays are
    tracked in separate dicts keyed by their fixed integer IDs.

    Priority stack (highest wins):
      1. manual   — set via API / web UI, optional revert_s timer
      2. pir      — set by pir_manager on motion, cleared on vacancy
      3. schedule — set by schedule_engine each tick
      4. ldr_cap  — channel-only modifier; applied ONLY when the active source
                    is schedule. Manual and PIR overrides bypass the cap so an
                    explicit user/motion request is honored even in daylight.

    Phase 5 — calendar_override:
      Calendar overrides will slot at the SCHEDULE LAYER (not above PIR).
      During an active calendar event, schedule_engine should consult the
      event's overlay schedule (and pir actions, if the event supplies any)
      instead of the baseline config. Manual still wins, PIR still gets to
      bump on motion — only the schedule baseline is swapped. Putting the
      override above PIR would break "during the course, briefly bump
      lights on motion in the corridor" use cases.

      Each leaf evaluates the calendar locally from its own DS3231 clock —
      coord is just the config/UI surface, not a runtime dependency for
      calendar activation. See ROADMAP.md Phase 5 for the data model.
    """

    def __init__(self):
        # cid (int) -> {manual, pir, schedule:{duty_percent,fade_ms}, ldr_cap, actual}
        self._channel_state = {}
        # rid (int) -> {manual, pir, schedule:{state}, actual}
        self._relay_state = {}
        # ("ch"|"rl", id) -> asyncio.Task
        self._revert_tasks = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init_from_config(self, led_channels_cfg, relays_cfg):
        log.info("[ARBITER] Initializing from config...")
        for ch in led_channels_cfg:
            cid = ch["id"]
            default_duty = ch.get("default_duty_percent", 0)
            self._channel_state[cid] = {
                "manual": None,
                "pir": None,
                "schedule": {"duty_percent": default_duty, "fade_ms": 0},
                "ldr_cap": None,
                "actual": default_duty,
            }
        for r in relays_cfg:
            rid = r["id"]
            default_state = r.get("default_state", "off")
            self._relay_state[rid] = {
                "manual": None,
                "pir": None,
                "schedule": {"state": default_state},
                "actual": default_state,
            }
        log.info("[ARBITER] Applying initial defaults to hardware...")
        self._apply_all(force=True)
        log.info("[ARBITER] Initialization complete")

    # ------------------------------------------------------------------
    # Manual overrides
    # ------------------------------------------------------------------

    def set_manual_channel(self, cid, duty_percent, fade_ms=0, revert_s=0):
        s = self._channel_state.get(cid)
        if not s:
            return
        s["manual"] = {"duty_percent": duty_percent, "fade_ms": fade_ms}
        self._apply_channel(cid)
        if revert_s and revert_s > 0:
            self._schedule_revert(("ch", cid), revert_s)

    def set_manual_relay(self, rid, state, revert_s=0):
        s = self._relay_state.get(rid)
        if not s:
            return
        s["manual"] = {"state": state}
        self._apply_relay(rid)
        if revert_s and revert_s > 0:
            self._schedule_revert(("rl", rid), revert_s)

    def clear_manual_channel(self, cid):
        s = self._channel_state.get(cid)
        if s:
            s["manual"] = None
            self._cancel_revert(("ch", cid))
            self._apply_channel(cid)

    def clear_manual_relay(self, rid):
        s = self._relay_state.get(rid)
        if s:
            s["manual"] = None
            self._cancel_revert(("rl", rid))
            self._apply_relay(rid)

    def clear_all_manual(self):
        for cid, s in self._channel_state.items():
            s["manual"] = None
            self._cancel_revert(("ch", cid))
        for rid, s in self._relay_state.items():
            s["manual"] = None
            self._cancel_revert(("rl", rid))
        self._apply_all()

    def has_manual(self):
        for s in self._channel_state.values():
            if s["manual"] is not None:
                return True
        for s in self._relay_state.values():
            if s["manual"] is not None:
                return True
        return False

    def _schedule_revert(self, key, revert_s):
        self._cancel_revert(key)
        self._revert_tasks[key] = asyncio.create_task(self._revert_after(key, revert_s))

    def _cancel_revert(self, key):
        t = self._revert_tasks.pop(key, None)
        if t and t != asyncio.current_task():
            t.cancel()

    async def _revert_after(self, key, delay_s):
        await asyncio.sleep(delay_s)
        kind, oid = key
        if kind == "ch":
            self.clear_manual_channel(oid)
        else:
            self.clear_manual_relay(oid)

    # ------------------------------------------------------------------
    # PIR overrides
    # ------------------------------------------------------------------

    def set_pir_channel(self, cid, duty_percent, fade_ms=0):
        s = self._channel_state.get(cid)
        if not s:
            return
        s["pir"] = {"duty_percent": duty_percent, "fade_ms": fade_ms}
        self._apply_channel(cid)

    def set_pir_relay(self, rid, state):
        s = self._relay_state.get(rid)
        if not s:
            return
        s["pir"] = {"state": state}
        self._apply_relay(rid)

    def clear_all_pir(self):
        for s in self._channel_state.values():
            s["pir"] = None
        for s in self._relay_state.values():
            s["pir"] = None
        self._apply_all()

    # ------------------------------------------------------------------
    # Schedule updates (called every tick by schedule engine)
    # ------------------------------------------------------------------

    def set_schedule(self, channel_desired, relay_desired):
        for cid, desired in channel_desired.items():
            s = self._channel_state.get(cid)
            if not s:
                continue
            s["schedule"] = desired
            if self._resolve_channel(cid) != s["actual"]:
                self._apply_channel(cid)
        for rid, desired in relay_desired.items():
            s = self._relay_state.get(rid)
            if not s:
                continue
            s["schedule"] = desired
            if self._resolve_relay(rid) != s["actual"]:
                self._apply_relay(rid)

    # ------------------------------------------------------------------
    # LDR cap (channel-only modifier)
    # ------------------------------------------------------------------

    def set_ldr_cap(self, cap_percent):
        for s in self._channel_state.values():
            s["ldr_cap"] = cap_percent
        for cid in self._channel_state:
            self._apply_channel(cid)

    # ------------------------------------------------------------------
    # Scene application
    # ------------------------------------------------------------------

    def apply_scene(self, scene, revert_s=0):
        for entry in scene.get("led_channels", []):
            cid = entry.get("id")
            duty = entry.get("duty_percent", 0)
            fade = entry.get("fade_ms", 0)
            self.set_manual_channel(cid, duty, fade_ms=fade, revert_s=revert_s)
        for entry in scene.get("relays", []):
            rid = entry.get("id")
            state = entry.get("state", "off")
            self.set_manual_relay(rid, state, revert_s=revert_s)

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def get_actual_channels(self):
        return {cid: s["actual"] for cid, s in self._channel_state.items()}

    def get_actual_relays(self):
        return {rid: s["actual"] for rid, s in self._relay_state.items()}

    # ------------------------------------------------------------------
    # Internal resolution and hardware drive
    # ------------------------------------------------------------------

    def _resolve_channel(self, cid):
        s = self._channel_state[cid]
        # LDR cap only modifies the schedule layer. Manual and PIR overrides
        # bypass it — if a user (or motion trigger) explicitly asks for 100%,
        # we honor that even in bright daylight.
        if s["manual"] is not None:
            return s["manual"].get("duty_percent", 0)
        if s["pir"] is not None:
            return s["pir"].get("duty_percent", 0)
        duty = s["schedule"].get("duty_percent", 0)
        cap = s["ldr_cap"]
        if cap is not None:
            duty = min(duty, cap)
        return duty

    def _resolve_relay(self, rid):
        s = self._relay_state[rid]
        source = s["manual"] or s["pir"] or s["schedule"]
        return source.get("state", "off")

    def _apply_channel(self, cid, force=False):
        s = self._channel_state[cid]
        source = s["manual"] or s["pir"] or s["schedule"]
        new_actual = self._resolve_channel(cid)
        if not force and new_actual == s["actual"]:
            return
        s["actual"] = new_actual
        fade_ms = source.get("fade_ms", 0) if source else 0
        src_name = "manual" if s["manual"] else ("pir" if s["pir"] else "schedule")
        if fade_ms > 0:
            log.debug(f"[ARBITER] ch{cid}: {new_actual}% (fade {fade_ms}ms) from {src_name}")
            asyncio.create_task(pwm_controller.fade_to(cid, new_actual, fade_ms))
        else:
            log.debug(f"[ARBITER] ch{cid}: {new_actual}% from {src_name}")
            pwm_controller.set(cid, new_actual)

    def _apply_relay(self, rid, force=False):
        s = self._relay_state[rid]
        new_actual = self._resolve_relay(rid)
        if not force and new_actual == s["actual"]:
            return
        s["actual"] = new_actual
        src_name = "manual" if s["manual"] else ("pir" if s["pir"] else "schedule")
        log.debug(f"[ARBITER] rl{rid}: {new_actual} from {src_name}")
        relay_controller.set(rid, new_actual)

    def _apply_all(self, force=False):
        for cid in self._channel_state:
            self._apply_channel(cid, force=force)
        for rid in self._relay_state:
            self._apply_relay(rid, force=force)


priority_arbiter = PriorityArbiter()
