"""Hot-apply config changes without rebooting the device.

Every CFG_PATCH (and CFG_START-with-target_path) goes through this
module after the leaf has persisted the new config to flash. Two
questions get answered:

1. `requires_reboot(path, old_value, new_value)` — does this change
   touch something that's wired at boot only? If yes, the leaf
   reboots after ACK (existing behaviour). Concrete reboot triggers:

     - `lora.*`        (E220 registers programmed once at boot)
     - `hardware.*`    (Pin objects constructed at boot)
     - `wifi.*`        (connect_wifi runs once at coord boot)
     - `system.role`, `system.unit_id`
     - `system.log_level`, `system.log_buffer_size`
     - `system.heartbeat_interval_s`, `system.pwm_update_interval_ms`
     - any `led_channels[*].gpio_pin` / `.enabled` change
     - any `relays[*].gpio_pin` / `.enabled` change
     - any `pir[*].gpio_pin` / `.enabled` change
     - `notifications.*` (MQTT connects at coord boot)

   Everything else hot-applies.

2. `apply_changes(path, new_cfg)` — for the hot-apply path, re-runs
   the relevant subsystems' `init_from_config()` (schedule_engine,
   priority_arbiter, ldr_monitor) and updates in-place state on
   long-lived objects (PIRSensor.vacancy_timeout_s,
   simple_logger._TIMEZONE_*) so the new config takes effect without
   a reboot. Caller has already validated and persisted; this
   function only reshapes runtime state.

`enabled` toggles for led_channels / relays / pir currently force a
reboot — they require subsystem lifecycle dances (PWM channel
deinit/init, Pin teardown, async task cancel+create) that are
doable but riskier; deferred to a future pass. Most operator edits
(name, default duty, time windows, vacancy timeout, PIR action) hit
the hot path and apply in ~50 ms instead of ~30 s.

Safety net: if `apply_changes` raises, the caller logs the failure
and reboots as fallback — runtime state is potentially inconsistent
but config.json is durably saved, so post-reboot the leaf comes up
in the intended state.
"""

# Paths that always require a reboot if they're the patch target
# (or a prefix of it). Anything not matched here AND not matched by
# the section-specific field check below is eligible for hot-apply.
_REBOOT_PATHS = (
    "lora",
    "hardware",
    "wifi",
    "notifications",
    "system/role",
    "system/unit_id",
    "system/log_level",          # Logger instances cache it at construction
    "system/log_buffer_size",    # event_bus sized at boot
    # NOTE: system/heartbeat_interval_s and system/pwm_update_interval_ms
    # WERE here. Both tasks now re-read their cadence from config_manager
    # each iteration so the changes hot-apply on the next tick. Same for
    # system/heartbeat_timeout_s (read dynamically by fleet_timeout_task
    # via config_manager.get("system")).
)

# Per-section fields that, when changed inside a section/index patch
# like `led_channels/2`, force a reboot even though other fields in
# the same entry would hot-apply.
_REBOOT_FIELDS = {
    "led_channels": ("gpio_pin", "enabled"),
    "relays":       ("gpio_pin", "enabled"),
    "pir":          ("gpio_pin", "enabled"),
}


def _matches_prefix(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def _walk(value, parts):
    """Walk a (potentially missing) nested dict by a list of keys.
    Returns the leaf value or None if any key is missing / non-dict
    encountered. Used by the parent-of-reboot-path descendant check
    below: when the patch path is e.g. "system" we need to compare
    the old/new dicts' "log_level", "heartbeat_interval_s", etc.
    sub-fields against the reboot list."""
    for p in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(p)
        if value is None:
            return None
    return value


def requires_reboot(path, old_value, new_value):
    """True iff applying this patch needs a leaf reboot to fully take
    effect. Conservative — anything we can't analyse → reboot.

    path: slash-separated JSON path (e.g. "led_channels/2",
          "system/heartbeat_interval_s", "lora/channel").
    old_value: the value at that path BEFORE the patch (None if the
               key didn't exist).
    new_value: the value at that path AFTER the patch.
    """
    # 1. Whole-section / leaf-path matches against the reboot list.
    #    Catches patches AT or BELOW a reboot-required path.
    for rp in _REBOOT_PATHS:
        if _matches_prefix(path, rp):
            return True

    # 2. Parent-of-reboot-path descendant check. Without this, a
    #    section-level patch like path="system" with a bundled value
    #    {role, unit_name, log_level, hb_interval_s, …} would
    #    hot-apply even when reboot-required sub-fields changed —
    #    log_level wouldn't actually update (Logger instances cache
    #    it) and the operator would think the change took effect.
    #    Compare each reboot path that lives strictly UNDER `path`.
    for rp in _REBOOT_PATHS:
        if rp == path or not rp.startswith(path + "/"):
            continue
        rel = rp[len(path) + 1:].split("/")
        if _walk(old_value, rel) != _walk(new_value, rel):
            return True

    parts = path.split("/")
    section = parts[0]

    # 2. Section/index patches: scan the entry's reboot-required
    #    fields for changes. e.g. path="led_channels/2" with value
    #    {id:3, name:"new", gpio_pin:18, enabled:true, ...}
    if section in _REBOOT_FIELDS:
        reboot_fields = _REBOOT_FIELDS[section]
        # Whole section replace: path == "led_channels" / "relays" / "pir"
        if len(parts) == 1:
            if not isinstance(new_value, list):
                return True
            old_list = old_value if isinstance(old_value, list) else []
            # Length changes mean entries were added/removed → safer
            # to reboot than try to figure out which slot is which.
            if len(old_list) != len(new_list):
                return True
            for ne in new_value:
                if not isinstance(ne, dict):
                    return True
                # Match against old entry by id (positional invariant
                # guarantees id == index+1, but searching by id is
                # explicit and resilient to ordering bugs).
                oid = ne.get("id")
                oe = next((e for e in old_list if isinstance(e, dict) and e.get("id") == oid), None)
                if oe is None:
                    return True
                for f in reboot_fields:
                    if oe.get(f) != ne.get(f):
                        return True
            return False

        # Entry-level patch: path == "led_channels/N" with value = whole dict
        if len(parts) == 2:
            if isinstance(old_value, dict) and isinstance(new_value, dict):
                for f in reboot_fields:
                    if old_value.get(f) != new_value.get(f):
                        return True
                return False
            # Type mismatch (e.g. clearing an entry to null) — be safe
            return True

        # Sub-field patch: path == "led_channels/N/<field>"
        if len(parts) >= 3:
            if parts[2] in reboot_fields:
                return True
            # Other fields (name, default_duty_percent, time_windows,
            # vacancy_timeout_s, on_motion, on_vacancy, ...) hot-apply.
            return False

    # 3. Everything else hot-applies.
    return False


def apply_changes(path, new_cfg):
    """Re-initialise the subsystems affected by this patch path so
    the new config takes effect without a reboot. Idempotent (safe
    to call even if there's nothing to do for the given path).

    Caller has already verified `requires_reboot()` returned False
    AND `config_manager.replace(new_cfg)` has succeeded. new_cfg
    is `config_manager.get_all()` post-patch.

    Raises on failure; caller catches and falls back to reboot.
    """
    section = path.split("/")[0]

    if section in ("led_channels", "relays"):
        _apply_schedule(new_cfg)
        return

    if section == "pir":
        _apply_pir(new_cfg)
        return

    if section == "scenes":
        # Scene definitions feed two places:
        #   - on_scene LoRa handler (looked up dynamically after
        #     refactor; no rebuild needed)
        #   - PIR set_scene action handlers (cached in _setup_pir_handlers)
        # Re-running the PIR handler setup re-binds against the new
        # scenes; the on_scene handler reads via config_manager on
        # demand so it sees the new scenes automatically.
        _apply_pir(new_cfg)
        return

    if section == "ldr":
        _apply_ldr(new_cfg)
        return

    if section == "timezone":
        _apply_timezone(new_cfg)
        return

    if section == "system":
        # The non-reboot system fields (unit_name, heartbeat_timeout_s
        # on the coord side) are looked up via config_manager each
        # use after the small refactor in main.py — no re-init needed
        # here. heartbeat_timeout_s on the coord updates
        # fleet_manager._timeout_s on the next check_timeouts call
        # because fleet_manager reads config_manager dynamically.
        return

    if section == "dashboard":
        # web_server checks config_manager per request — already hot.
        return

    # Unknown section: nothing to do. Caller's reboot check should
    # have ruled out the dangerous ones already.


# --- Per-section apply helpers --------------------------------------

def _apply_schedule(new_cfg):
    """LED / relay default-state, time-windows, names — re-init the
    schedule layer of priority_arbiter via schedule_engine, then
    trigger an immediate re-evaluation so outputs reflect the new
    config without waiting for the next schedule_task tick (which
    can be up to pwm_update_interval_ms away).

    Does NOT touch the MANUAL or PIR layers in priority_arbiter — an
    active manual override stays in force across a config edit, which
    is the right behaviour: the operator pushed an override, then
    changed an unrelated default, the override shouldn't silently
    evaporate."""
    from core.schedule_engine import schedule_engine
    from core.priority_arbiter import priority_arbiter
    led_channels = new_cfg.get("led_channels", [])
    relays       = new_cfg.get("relays", [])
    schedule_engine.init_from_config(led_channels, relays)
    priority_arbiter.init_from_config(led_channels, relays)
    try:
        ch_des, rl_des = schedule_engine.get_desired_state()
        priority_arbiter.set_schedule(ch_des, rl_des)
    except Exception:
        # Schedule evaluation can fail if time isn't synced — that's
        # OK, the gate in schedule_task will pick it up next tick.
        pass


def _apply_pir(new_cfg):
    """Update vacancy_timeout_s on existing PIRSensor instances and
    re-wire the on_motion / on_vacancy handler closures. Does not
    cancel/restart the polling asyncio tasks — that's needed only
    when `enabled` toggles, which our reboot check catches above.

    Re-running `_setup_pir_handlers` from main rebuilds the
    scenes-by-name cache too, so a scenes patch automatically
    refreshes PIR set_scene actions."""
    from hardware.pir_manager import pir_manager
    pir_cfg = new_cfg.get("pir", [])
    scenes  = new_cfg.get("scenes", [])
    # 1. Update vacancy_timeout_s on live sensors.
    for p in pir_cfg:
        pid = p.get("id")
        sensor = pir_manager._sensors.get(pid)
        if sensor is not None:
            sensor.vacancy_timeout_s = p.get("vacancy_timeout_s", 60)
    # 2. Clear and rebuild action handlers. Lazy-import main to
    #    avoid a circular import at module load (this file is
    #    imported from inside main's handlers).
    pir_manager._motion_callbacks.clear()
    pir_manager._vacancy_callbacks.clear()
    import main
    main._setup_pir_handlers(pir_cfg, scenes)


def _apply_ldr(new_cfg):
    """Re-init ldr_monitor with new smoothing window / cap rules /
    enabled state. The on_cap_change callback re-binds to
    priority_arbiter so changing the cap rules takes effect
    immediately for the next sample."""
    from hardware.ldr_monitor import ldr_monitor
    from core.priority_arbiter import priority_arbiter
    ldr_monitor.init_from_config(
        new_cfg.get("ldr", {}),
        new_cfg.get("hardware", {}),
    )
    ldr_monitor.on_cap_change(priority_arbiter.set_ldr_cap)


def _apply_timezone(new_cfg):
    """Update simple_logger's cached tz constants so subsequent log
    timestamps render in the new offset. Logger instances themselves
    don't need rebuilding — they consult these module globals at
    timestamp time."""
    from shared import simple_logger
    tz = new_cfg.get("timezone", {})
    simple_logger._TIMEZONE_NAME   = tz.get("name", "UTC")
    simple_logger._TIMEZONE_OFFSET = tz.get("utc_offset_hours", 0.0)
