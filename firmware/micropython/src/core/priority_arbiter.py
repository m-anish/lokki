import asyncio
from hardware.pwm_control import pwm_controller
from hardware.relay_control import relay_controller
from shared.simple_logger import Logger

log = Logger()


class PriorityArbiter:
    """
    Single source of truth for all output states.

    Priority stack (highest wins):
      1. manual   — set via API / web UI, optional revert_s timer
      2. pir      — set by pir_manager on motion, cleared on vacancy
      3. schedule — set by schedule_engine each tick
      4. ldr_cap  — modifier applied on top of resolved duty (never changes on/off)
    """

    def __init__(self):
        # {output_id: {manual, pir, schedule, ldr_cap, actual}}
        self._state = {}
        self._revert_tasks = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init_from_config(self, led_channels_cfg, relays_cfg):
        log.info("[ARBITER] Initializing from config...")
        for ch in led_channels_cfg:
            cid = ch["id"]
            default_duty = ch.get("default_duty_percent", 0)
            enabled = ch.get("enabled", False)
            log.info(f"[ARBITER] LED {cid}: default={default_duty}%, enabled={enabled}")
            self._state[cid] = {
                "type": "led",
                "manual": None,
                "pir": None,
                "schedule": {"duty_percent": default_duty, "fade_ms": 0},
                "ldr_cap": None,
                "actual": default_duty,
            }
        for r in relays_cfg:
            rid = r["id"]
            default_state = r.get("default_state", "off")
            enabled = r.get("enabled", False)
            log.info(f"[ARBITER] Relay {rid}: default={default_state}, enabled={enabled}")
            self._state[rid] = {
                "type": "relay",
                "manual": None,
                "pir": None,
                "schedule": {"state": default_state},
                "ldr_cap": None,
                "actual": default_state,
            }
        # Apply initial defaults to hardware
        log.info("[ARBITER] Applying initial defaults to hardware...")
        self._apply_all(force=True)
        log.info("[ARBITER] Initialization complete")

    # ------------------------------------------------------------------
    # Manual overrides
    # ------------------------------------------------------------------

    def set_manual(self, output_id, value, fade_ms=0, revert_s=0):
        s = self._state.get(output_id)
        if not s:
            return
        if s["type"] == "led":
            s["manual"] = {"duty_percent": value, "fade_ms": fade_ms}
        else:
            s["manual"] = {"state": value}
        self._apply(output_id)
        if revert_s and revert_s > 0:
            self._schedule_revert(output_id, revert_s)

    def clear_manual(self, output_id):
        s = self._state.get(output_id)
        if s:
            s["manual"] = None
            self._cancel_revert(output_id)
            self._apply(output_id)

    def clear_all_manual(self):
        for oid in self._state:
            self._state[oid]["manual"] = None
            self._cancel_revert(oid)
        self._apply_all()

    def has_manual(self):
        return any(s["manual"] is not None for s in self._state.values())

    def _schedule_revert(self, output_id, revert_s):
        self._cancel_revert(output_id)
        self._revert_tasks[output_id] = asyncio.create_task(
            self._revert_after(output_id, revert_s)
        )

    def _cancel_revert(self, output_id):
        t = self._revert_tasks.pop(output_id, None)
        if t and t != asyncio.current_task():
            t.cancel()

    async def _revert_after(self, output_id, delay_s):
        await asyncio.sleep(delay_s)
        self.clear_manual(output_id)

    # ------------------------------------------------------------------
    # PIR overrides
    # ------------------------------------------------------------------

    def set_pir(self, output_id, value, fade_ms=0):
        s = self._state.get(output_id)
        if not s:
            return
        if s["type"] == "led":
            s["pir"] = {"duty_percent": value, "fade_ms": fade_ms}
        else:
            s["pir"] = {"state": value}
        self._apply(output_id)

    def clear_pir(self, output_id):
        s = self._state.get(output_id)
        if s:
            s["pir"] = None
            self._apply(output_id)

    def clear_all_pir(self):
        for oid in self._state:
            self._state[oid]["pir"] = None
        self._apply_all()

    # ------------------------------------------------------------------
    # Schedule updates (called every tick by schedule engine)
    # ------------------------------------------------------------------

    def set_schedule(self, desired_state):
        changed = []
        for oid, desired in desired_state.items():
            s = self._state.get(oid)
            if not s:
                continue
            s["schedule"] = desired
            new_actual = self._resolve(oid)
            if new_actual != s["actual"]:
                changed.append(oid)
        for oid in changed:
            self._apply(oid)

    # ------------------------------------------------------------------
    # LDR cap (modifier)
    # ------------------------------------------------------------------

    def set_ldr_cap(self, cap_percent):
        for s in self._state.values():
            s["ldr_cap"] = cap_percent
        self._apply_all()

    # ------------------------------------------------------------------
    # Scene application
    # ------------------------------------------------------------------

    def apply_scene(self, scene, revert_s=0):
        for entry in scene.get("led_channels", []):
            oid = entry.get("id")
            duty = entry.get("duty_percent", 0)
            fade = entry.get("fade_ms", 0)
            self.set_manual(oid, duty, fade_ms=fade, revert_s=revert_s)
        for entry in scene.get("relays", []):
            oid = entry.get("id")
            state = entry.get("state", "off")
            self.set_manual(oid, state, revert_s=revert_s)

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def get_actual_state(self):
        return {oid: s["actual"] for oid, s in self._state.items()}

    # ------------------------------------------------------------------
    # Internal resolution and hardware drive
    # ------------------------------------------------------------------

    def _resolve(self, output_id):
        s = self._state[output_id]
        source = s["manual"] or s["pir"] or s["schedule"]
        if s["type"] == "led":
            duty = source.get("duty_percent", 0)
            cap = s["ldr_cap"]
            if cap is not None:
                duty = min(duty, cap)
            return duty
        else:
            return source.get("state", "off")

    def _apply(self, output_id, force=False):
        s = self._state[output_id]
        source = s["manual"] or s["pir"] or s["schedule"]
        new_actual = self._resolve(output_id)
        if not force and new_actual == s["actual"]:
            log.debug(f"[ARBITER] {output_id}: no change (already {new_actual})")
            return
        s["actual"] = new_actual
        if s["type"] == "led":
            fade_ms = source.get("fade_ms", 0) if source else 0
            source_name = "manual" if s["manual"] else ("pir" if s["pir"] else "schedule")
            if fade_ms > 0:
                log.info(f"[ARBITER] {output_id}: {new_actual}% (fade {fade_ms}ms) from {source_name}")
                asyncio.create_task(pwm_controller.fade_to(output_id, new_actual, fade_ms))
            else:
                log.info(f"[ARBITER] {output_id}: {new_actual}% from {source_name}")
                pwm_controller.set(output_id, new_actual)
        else:
            source_name = "manual" if s["manual"] else ("pir" if s["pir"] else "schedule")
            log.info(f"[ARBITER] {output_id}: {new_actual} from {source_name}")
            relay_controller.set(output_id, new_actual)

    def _apply_all(self, force=False):
        for oid in self._state:
            self._apply(oid, force=force)


priority_arbiter = PriorityArbiter()
