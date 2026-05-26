"""Cross-field and positional invariants the JSON Schema can't express.

JSON Schema validates types, ranges, enums, and presence. These are the
rules that are too dynamic for our Schema subset:

- Positional IDs in `led_channels`, `relays`, `pir` — entry `i` must
  have `id == i+1` (so the firmware's positional arrays line up with
  the config).
- Pin uniqueness within a section (two LED channels can't share a GPIO).
- Scene-name uniqueness.
- `heartbeat_timeout_s >= heartbeat_interval_s` cross-field rule.
- PIR `on_motion` / `on_vacancy` action-specific required fields
  (e.g. `action == "set_led_channels"` requires `channels` and
  `duty_percent`).
- `time_windows` start/end format (HH:MM or `sunrise`/`sunset`).
- Coordinator must have `unit_id == 0`; leaf must have `unit_id` in
  1..8 or 99. (Could be expressed in Schema via if/then/else on role
  but it's clearer here alongside the rest.)

Returns a list of error strings. Empty list = valid. Called by
`config_manager._validate()` AFTER `schema_validator.validate()` so the
semantic checks can assume basic types/ranges already hold.
"""

_MAX_CHANNELS = 8
_MAX_RELAYS   = 2
_MAX_PIRS     = 4

_VALID_PIR_PINS   = {6, 7, 8, 9}
_VALID_RELAY_PINS = {10, 11}
_VALID_LED_PINS   = {13, 14, 15, 16, 17, 18, 19, 22}

_PIR_ACTIONS      = ("set_scene", "set_led_channels", "set_relay", "revert_to_schedule")


def check(cfg):
    """Run every semantic invariant. Returns a list of error strings."""
    errors = []
    _check_system(cfg, errors)
    _check_pir(cfg, errors)
    _check_relays(cfg, errors)
    _check_led_channels(cfg, errors)
    _check_scenes(cfg, errors)
    return errors


# --- system --------------------------------------------------------------

def _check_system(cfg, errors):
    s = cfg.get("system", {})
    if not isinstance(s, dict):
        return                       # schema layer caught this
    role = s.get("role")
    uid = s.get("unit_id")
    if role == "coordinator" and uid != 0:
        errors.append("coordinator must have unit_id 0")
    if role == "leaf" and uid == 0:
        errors.append("leaf unit_id must be 1–8 (or 99 if unclaimed)")

    hb_i = s.get("heartbeat_interval_s", 30)
    hb_t = s.get("heartbeat_timeout_s", 120)
    if isinstance(hb_i, int) and isinstance(hb_t, int) and hb_t < hb_i:
        errors.append(
            f"system.heartbeat_timeout_s ({hb_t}) must be >= heartbeat_interval_s ({hb_i})"
        )


# --- pir -----------------------------------------------------------------

def _check_pir(cfg, errors):
    pirs = cfg.get("pir", [])
    if not isinstance(pirs, list):
        return
    seen_pins = set()
    for i, p in enumerate(pirs):
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if pid != i + 1:
            errors.append(
                f"pir[{i}].id must be {i + 1} (position-bound id, got {pid!r})"
            )
        pin = p.get("gpio_pin")
        if pin not in _VALID_PIR_PINS:
            errors.append(
                f"pir[{i}].gpio_pin must be one of {sorted(_VALID_PIR_PINS)} (got {pin!r})"
            )
        elif pin in seen_pins:
            errors.append(f"pir[{i}].gpio_pin {pin} duplicate")
        seen_pins.add(pin)
        for field in ("on_motion", "on_vacancy"):
            action = p.get(field)
            if isinstance(action, dict):
                _check_pir_action(action, f"pir[{i}].{field}", errors)


def _check_pir_action(action, path, errors):
    act = action.get("action")
    if act not in _PIR_ACTIONS:
        errors.append(f"{path}.action must be one of {_PIR_ACTIONS} (got {act!r})")
        return
    if act == "set_led_channels":
        channels = action.get("channels", [])
        if not isinstance(channels, list) or not channels:
            errors.append(f"{path}.channels must be a non-empty list")
        else:
            for j, cid in enumerate(channels):
                if not isinstance(cid, int) or not 1 <= cid <= _MAX_CHANNELS:
                    errors.append(
                        f"{path}.channels[{j}] must be int 1..{_MAX_CHANNELS} (got {cid!r})"
                    )
        duty = action.get("duty_percent")
        if not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
            errors.append(f"{path}.duty_percent must be 0–100 when action=set_led_channels")
    elif act == "set_relay":
        rid = action.get("relay_id")
        if not isinstance(rid, int) or not 1 <= rid <= _MAX_RELAYS:
            errors.append(f"{path}.relay_id must be int 1..{_MAX_RELAYS} when action=set_relay")
        st = action.get("state")
        if st not in ("on", "off"):
            errors.append(f"{path}.state must be 'on' or 'off' when action=set_relay")
    elif act == "set_scene":
        if not isinstance(action.get("scene_name"), str):
            errors.append(f"{path}.scene_name must be a string when action=set_scene")
    # revert_to_schedule: no extra fields required


# --- relays --------------------------------------------------------------

def _check_relays(cfg, errors):
    relays = cfg.get("relays", [])
    if not isinstance(relays, list):
        return
    seen_pins = set()
    for i, r in enumerate(relays):
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid != i + 1:
            errors.append(
                f"relays[{i}].id must be {i + 1} (position-bound id, got {rid!r})"
            )
        pin = r.get("gpio_pin")
        if pin not in _VALID_RELAY_PINS:
            errors.append(
                f"relays[{i}].gpio_pin must be one of {sorted(_VALID_RELAY_PINS)} (got {pin!r})"
            )
        elif pin in seen_pins:
            errors.append(f"relays[{i}].gpio_pin {pin} duplicate")
        seen_pins.add(pin)
        for j, w in enumerate(r.get("time_windows", [])):
            _check_time_window(w, f"relays[{i}].time_windows[{j}]", errors,
                               needs_state=True)


# --- led_channels --------------------------------------------------------

def _check_led_channels(cfg, errors):
    channels = cfg.get("led_channels", [])
    if not isinstance(channels, list):
        return
    if not channels:
        errors.append("led_channels must have at least one entry")
        return
    seen_pins = set()
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            continue
        cid = ch.get("id")
        if cid != i + 1:
            errors.append(
                f"led_channels[{i}].id must be {i + 1} (position-bound id, got {cid!r})"
            )
        pin = ch.get("gpio_pin")
        if pin not in _VALID_LED_PINS:
            errors.append(
                f"led_channels[{i}].gpio_pin must be one of {sorted(_VALID_LED_PINS)} (got {pin!r})"
            )
        elif pin in seen_pins:
            errors.append(f"led_channels[{i}].gpio_pin {pin} duplicate")
        seen_pins.add(pin)
        for j, w in enumerate(ch.get("time_windows", [])):
            _check_time_window(w, f"led_channels[{i}].time_windows[{j}]", errors,
                               needs_duty=True)


# --- time windows --------------------------------------------------------

def _check_time_window(w, path, errors, needs_duty=False, needs_state=False):
    if not isinstance(w, dict):
        return
    for key in ("start", "end"):
        v = w.get(key)
        if not _valid_time(v):
            errors.append(f"{path}.{key} must be 'HH:MM' or 'sunrise'/'sunset' (got {v!r})")
    if needs_duty:
        duty = w.get("duty_percent")
        if not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
            errors.append(f"{path}.duty_percent must be 0–100")
    if needs_state:
        if w.get("state") not in ("on", "off"):
            errors.append(f"{path}.state must be 'on' or 'off'")


def _valid_time(t):
    if not isinstance(t, str):
        return False
    low = t.strip().lower()
    if low in ("sunrise", "sunset"):
        return True
    try:
        parts = t.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, IndexError):
        return False


# --- scenes --------------------------------------------------------------

def _check_scenes(cfg, errors):
    scenes = cfg.get("scenes", [])
    if not isinstance(scenes, list):
        return
    seen_names = set()
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            continue
        name = s.get("name", "")
        if name in seen_names:
            errors.append(f"scenes[{i}].name '{name}' duplicate")
        seen_names.add(name)
