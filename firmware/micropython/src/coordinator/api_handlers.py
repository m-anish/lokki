import json
from coordinator.fleet_manager import fleet_manager
from comms.lora_protocol import lora_protocol
from core.config_manager import config_manager
from core.priority_arbiter import priority_arbiter
from shared.system_status import system_status
from shared.simple_logger import Logger
from shared.event_bus import event_bus

log = Logger()


# ----------------------------------------------------------------------
# Leaf-config cache
# ----------------------------------------------------------------------
# The coordinator is the source of truth for every leaf's config. Every
# time the user pushes a config to leaf N (via POST /api/units/N/config),
# we cache the full parsed config here AND mirror it to flash at
# /leaf-configs/N.json. On coord boot we re-hydrate from flash so the
# dashboard's Control modal and Config Builder's "Load Leaf N" both work
# without needing the leaf to be online.
#
# {unit_id: {full config dict}}

_leaf_config_cache = {}
_LEAF_CFG_DIR = "/leaf-configs"


def _ensure_leaf_cfg_dir():
    try:
        import os
        os.mkdir(_LEAF_CFG_DIR)
    except OSError:
        pass  # already exists


def _persist_leaf_cfg(unit_id, cfg):
    """Atomic write of the leaf cache to flash. Best-effort — failure is logged but not raised."""
    try:
        import os
        _ensure_leaf_cfg_dir()
        path = f"{_LEAF_CFG_DIR}/{unit_id}.json"
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(config_manager.prettify_json(cfg))
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(tmp, path)
    except Exception as e:
        log.warn(f"[API] Could not persist leaf {unit_id} cache: {e}")


def load_leaf_cache_from_flash():
    """Called once at coord boot from main.py. Re-hydrates _leaf_config_cache."""
    try:
        import os
        try:
            entries = os.listdir(_LEAF_CFG_DIR)
        except OSError:
            return  # dir doesn't exist yet — nothing cached
        for name in entries:
            if not name.endswith(".json"):
                continue
            try:
                uid = int(name[:-5])  # strip ".json"
            except ValueError:
                continue
            try:
                with open(f"{_LEAF_CFG_DIR}/{name}", "r") as f:
                    _leaf_config_cache[uid] = json.load(f)
                log.info(f"[API] Restored cached config for leaf {uid}")
            except Exception as e:
                log.warn(f"[API] Could not load leaf {uid} cache: {e}")
    except Exception as e:
        log.warn(f"[API] Leaf cache hydrate failed: {e}")


def _extract_leaf_meta(cfg):
    """Channel/relay metadata for the dashboard Control modal."""
    return {
        "led_channels": [
            {
                "id":   c["id"],
                "name": c.get("name", f"ch{c['id']}"),
                "enabled": c.get("enabled", True),
                "default_duty_percent": c.get("default_duty_percent", 0),
            }
            for c in cfg.get("led_channels", []) if isinstance(c, dict) and "id" in c
        ],
        "relays": [
            {
                "id":   r["id"],
                "name": r.get("name", f"rl{r['id']}"),
                "enabled": r.get("enabled", True),
                "default_state": r.get("default_state", "off"),
            }
            for r in cfg.get("relays", []) if isinstance(r, dict) and "id" in r
        ],
    }


def _ok_led_state():
    return "running_lora_ok" if system_status.lora_connected else "running_ok"


def _ok(data=None):
    if data is None:
        return {"ok": True}
    return {"ok": True, "data": data}


def _err(msg, code=400):
    return {"ok": False, "error": msg, "_status": code}


# ------------------------------------------------------------------
# Fleet
# ------------------------------------------------------------------

def _positional_names(items, slots, prefix):
    """Build a fixed-length name list. items is a list of {id, name, ...} dicts;
    output[i] = name of item with id (i+1), or "<prefix>(i+1)" if not configured.
    """
    out = [f"{prefix}{i+1}" for i in range(slots)]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        oid = it.get("id")
        if isinstance(oid, int) and 1 <= oid <= slots:
            out[oid - 1] = it.get("name") or f"{prefix}{oid}"
    return out


def _positional_enabled(items, slots):
    """Returns a fixed-length list of bools — True if the slot is configured AND enabled."""
    out = [False] * slots
    for it in items or []:
        if not isinstance(it, dict):
            continue
        oid = it.get("id")
        if isinstance(oid, int) and 1 <= oid <= slots:
            out[oid - 1] = bool(it.get("enabled", False))
    return out


def handle_fleet_status():
    import time
    from hardware.pwm_control import pwm_controller
    from hardware.relay_control import relay_controller
    from hardware.pir_manager import pir_manager
    from hardware.ldr_monitor import ldr_monitor

    now = time.time()
    fleet = {}
    for uid, u in fleet_manager.get_all().items():
        udata = dict(u)
        # Compute relative age to avoid 5-year gap if NTP sync happens between
        # the time the leaf was last seen and now.
        udata["last_seen_ago_s"] = int(now - u["last_seen"]) if u["last_seen"] else -1

        # Inject friendly names from the cached leaf config. Positional:
        # ch_names[i] is for channel id (i+1), and so on.
        cached = _leaf_config_cache.get(uid, {})
        udata["ch_names"]    = _positional_names(cached.get("led_channels"), 8, "ch")
        udata["rl_names"]    = _positional_names(cached.get("relays"),       2, "rl")
        udata["pir_names"]   = _positional_names(cached.get("pir"),          4, "pir")
        udata["ch_enabled"]  = _positional_enabled(cached.get("led_channels"), 8)
        udata["rl_enabled"]  = _positional_enabled(cached.get("relays"),       2)
        udata["pir_enabled"] = _positional_enabled(cached.get("pir"),          4)
        fleet[str(uid)] = udata

    from hardware.rtc_module import get_rtc_temp_c
    _rtc_t = get_rtc_temp_c()
    fleet["0"] = {
        "name": config_manager.unit_name,
        "online": True,
        "last_seen_ago_s": 0,             # coordinator is "always now"
        "uptime": system_status.get_uptime(),
        "ch": pwm_controller.get_all(),
        "ch_names":   _positional_names(config_manager.get("led_channels"), 8, "ch"),
        "ch_enabled": _positional_enabled(config_manager.get("led_channels"), 8),
        "rl": relay_controller.get_all(),
        "rl_names":   _positional_names(config_manager.get("relays"),       2, "rl"),
        "rl_enabled": _positional_enabled(config_manager.get("relays"),       2),
        "pir": pir_manager.get_all_states(),
        "pir_names":  _positional_names(config_manager.get("pir"),          4, "pir"),
        "pir_enabled":_positional_enabled(config_manager.get("pir"),          4),
        "ldr": ldr_monitor.ambient_percent,
        "err": system_status.error_count,
        "rssi": None,                     # local, no link
        "rtc_t": round(_rtc_t, 1) if _rtc_t is not None else None,
    }

    # Unclaimed leaves (factory-reset devices waiting for the claim
    # wizard) live in a separate dict keyed by chip UID. Surface
    # them under a separate top-level key so the dashboard can
    # render them as "New device" cards distinct from the regular
    # fleet view. Keys are the chip UIDs themselves.
    unclaimed = {}
    for chip_uid, u in fleet_manager.get_unclaimed_all().items():
        udata = dict(u)
        udata["last_seen_ago_s"] = int(now - u["last_seen"]) if u["last_seen"] else -1
        unclaimed[chip_uid] = udata

    return _ok({"fleet": fleet, "unclaimed": unclaimed})


def handle_unit_status(unit_id):
    if unit_id == 0:
        # Return coordinator's own status
        from hardware.pwm_control import pwm_controller
        from hardware.relay_control import relay_controller
        from hardware.pir_manager import pir_manager
        from hardware.ldr_monitor import ldr_monitor
        
        return _ok({
            "online": True,
            "uptime": system_status.get_uptime(),
            "ch": pwm_controller.get_all(),   # 8-slot positional list
            "rl": relay_controller.get_all(), # 2-slot positional list
            "pir": pir_manager.get_all_states(), # 4-slot positional list
            "ldr": ldr_monitor.ambient_percent,
            "err": system_status.error_count,
        })
    
    u = fleet_manager.get(unit_id)
    if u is None:
        return _err(f"Unknown unit {unit_id}", 404)
    return _ok(u)


def handle_coordinator_status():
    return _ok(system_status.get_status_dict())


# ------------------------------------------------------------------
# Config push
# ------------------------------------------------------------------

async def handle_config_push(unit_id, config_str):
    if unit_id == 0:
        # Coordinator: apply locally
        try:
            # Restore any "********" placeholders from the live config
            # before applying, so round-tripping through the (now
            # masked) /api/config response doesn't break wifi or the
            # dashboard auth gate.
            try:
                incoming = json.loads(config_str)
            except Exception:
                incoming = None
            if isinstance(incoming, dict):
                changed = False
                if (isinstance(incoming.get("wifi"), dict)
                        and incoming["wifi"].get("password") == "********"):
                    live_pw = config_manager.get("wifi").get("password", "")
                    incoming["wifi"]["password"] = live_pw
                    changed = True
                if (isinstance(incoming.get("dashboard"), dict)
                        and incoming["dashboard"].get("auth_password") == "********"):
                    live_pw = (config_manager.get("dashboard") or {}).get("auth_password", "")
                    incoming["dashboard"]["auth_password"] = live_pw
                    changed = True
                if changed:
                    config_str = json.dumps(incoming)
            config_manager.replace(config_str)
            log.info("[API] Local config replaced")
            return _ok({"applied": "local"})
        except Exception as e:
            return _err(str(e))

    # To save bandwidth over LoRa, we strip all whitespace/indentation before sending
    try:
        compact_str = json.dumps(json.loads(config_str))
    except Exception:
        compact_str = config_str
        
    ok = await lora_protocol.send_config(unit_id, compact_str)
    if ok:
        # Cache the FULL leaf config ONLY AFTER a successful LoRa transfer
        # so the coordinator's view perfectly matches the leaf's actual state.
        try:
            cfg = json.loads(compact_str)
            _leaf_config_cache[unit_id] = cfg
            _persist_leaf_cfg(unit_id, cfg)
            log.info(f"[API] Cached leaf {unit_id} config")
        except Exception as e:
            log.warn(f"[API] Could not cache leaf {unit_id} config: {e}")
        return _ok({"sent_to": unit_id})

    # Surface the failure reason from cfg_progress so the dashboard toast/modal
    # can show *why* the push failed instead of the generic LoRa link blame.
    # Phases at this point: "failed" (with .message set) for APPLY_FAILED or
    # exhausted retries; otherwise fall through to the generic message.
    prog = lora_protocol.cfg_progress
    if prog.get("phase") == "failed" and prog.get("message"):
        return _err(prog["message"], 502)
    return _err(f"Config transfer to unit {unit_id} failed — check LoRa link", 502)


def handle_full_config():
    """Return full config but mask the secret fields.

    Why: dashboard auth is optional and LAN-only, so even when it's
    enabled the response shouldn't expose stored credentials to anyone
    who's logged in. Currently masked: `wifi.password` and
    `dashboard.auth_password`. The Config Builder doesn't need the
    real values to edit other fields — if the user wants to change
    them they re-enter the new value, and the round-trip through
    handle_config_push restores any "********" placeholder that came
    back unchanged.
    """
    return _ok(_mask_secrets(config_manager.get_all()))


def _mask_secrets(cfg):
    """Return a shallow-copy of cfg with sensitive fields replaced by
    `********`. Two fields today: `wifi.password` and
    `dashboard.auth_password`. Round-trips through the Config Builder
    work because handle_config_push restores the live values when it
    sees a `********` placeholder coming back."""
    if not isinstance(cfg, dict):
        return cfg
    out = cfg
    copied = False
    def _copy_on_write():
        nonlocal out, copied
        if not copied:
            out = dict(cfg)
            copied = True
        return out
    if isinstance(cfg.get("wifi"), dict) and cfg["wifi"].get("password"):
        c = _copy_on_write()
        c["wifi"] = dict(c["wifi"])
        c["wifi"]["password"] = "********"
    if isinstance(cfg.get("dashboard"), dict) and cfg["dashboard"].get("auth_password"):
        c = _copy_on_write()
        c["dashboard"] = dict(c["dashboard"])
        c["dashboard"]["auth_password"] = "********"
    return out


def handle_unit_config(unit_id):
    """Return the full config for a unit — coordinator reads its live config,
    leaves return the coordinator's cached copy. Used by both the dashboard
    Control modal (which reads led_channels/relays) AND the Config Builder
    (which reads every section to populate its form)."""
    if unit_id == 0:
        cfg = _mask_secrets(config_manager.get_all())
        out = dict(cfg)
        out["source"] = "live"
        return _ok(out)

    cached = _leaf_config_cache.get(unit_id)
    if cached:
        out = dict(cached)
        out["source"] = "cached"
        return _ok(out)

    # No cache. Either the leaf was set up via USB only, or the coord just
    # rebooted and the user hasn't re-pushed yet. Return a minimal stub the
    # dashboard hint logic and Config Builder can both detect.
    u = fleet_manager.get(unit_id)
    if u is None:
        return _err(f"Unknown unit {unit_id}", 404)
    return _ok({
        "unit_id":      unit_id,
        "unit_name":    u.get("name", "") or f"Unit {unit_id}",
        "led_channels": [],
        "relays":       [],
        "source":       "none",
        "note":         "No config cached on the coordinator for this leaf. Open the Config Builder, fill in this leaf's config, and Save to device.",
    })


# ------------------------------------------------------------------
# Scenes
# ------------------------------------------------------------------

def handle_list_scenes():
    scenes = config_manager.get("scenes")
    # scenes is a list of dicts, extract names
    scene_names = [s.get("name", "") for s in scenes if isinstance(s, dict)]
    return _ok(scene_names)


def handle_scene_apply(scene_name, unit_ids=None):
    scenes = config_manager.get("scenes")
    # Find the scene by name (scenes is a list of dicts)
    scene = None
    for s in scenes:
        if isinstance(s, dict) and s.get("name") == scene_name:
            scene = s
            break
    
    if scene is None:
        return _err(f"Unknown scene '{scene_name}'", 404)

    targets = unit_ids or [0]
    results = {}
    for uid in targets:
        if uid == 0:
            priority_arbiter.apply_scene(scene)
            results["0"] = "applied_local"
        else:
            seq = lora_protocol.send_scene(uid, scene_name)
            results[str(uid)] = "sent" if seq else "send_failed"
    return _ok(results)


# ------------------------------------------------------------------
# Manual override
# ------------------------------------------------------------------

async def handle_manual_override(unit_id, payload):
    log.info(f"[API] Manual override request for unit {unit_id}, payload: {payload}")

    try:
        channels  = payload.get("ch", [])
        relays    = payload.get("rl", [])
        revert_s  = payload.get("revert_s", 0)
        fade_ms   = payload.get("fade_ms", 0)

        log.info(f"[API] Parsed: {len(channels)} channels, {len(relays)} relays")

        if unit_id == 0:
            for cid, val in channels:
                log.info(f"[API] Setting LED ch{cid}={val}%, fade={fade_ms}ms, revert={revert_s}s")
                priority_arbiter.set_manual_channel(cid, val, fade_ms, revert_s)
            for rid, val in relays:
                log.info(f"[API] Setting relay rl{rid}={val}, revert={revert_s}s")
                priority_arbiter.set_manual_relay(rid, val, revert_s)
            log.info("[API] Manual override applied successfully")
            from hardware.status_led import status_led
            status_led.set_state("manual_override" if priority_arbiter.has_manual() else _ok_led_state())
            return _ok({"applied": "local"})

        # The protocol layer owns packet-size policy and splits internally if
        # the combined payload would exceed the 200B LoRa cap. fade_ms is
        # forwarded to the leaf so the slider's "Fade" setting actually
        # applies remotely (it was being dropped on the wire previously).
        ok = await lora_protocol.send_manual_override_batched(
            unit_id, channels, relays, revert_s, fade_ms
        )
        return _ok({"sent_to": unit_id}) if ok else _err("Send failed", 502)
    except Exception as e:
        log.error(f"[API] Manual override exception: {e}")
        import sys
        sys.print_exception(e)
        return _err(f"Manual override failed: {e}")


def handle_manual_clear(unit_id):
    if unit_id == 0:
        priority_arbiter.clear_all_manual()
        from hardware.status_led import status_led
        status_led.set_state(_ok_led_state())
        return _ok()

    seq = lora_protocol.send_manual_override(unit_id, [], [], revert_s=-1)
    return _ok({"sent_to": unit_id}) if seq else _err("Send failed", 502)


# ------------------------------------------------------------------
# Sensors
# ------------------------------------------------------------------

def handle_sensors():
    from hardware.i2c_sensors import i2c_sensors
    readings = {"coordinator": i2c_sensors.get_readings()}
    for uid, u in fleet_manager.get_all().items():
        readings[str(uid)] = u.get("sensors", {})
    return _ok(readings)


# ------------------------------------------------------------------
# Per-unit scenes (from fleet cache, or coordinator config)
# ------------------------------------------------------------------

def handle_unit_scenes(unit_id):
    if unit_id == 0:
        scenes = config_manager.get("scenes")
        return _ok([s.get("name", "") for s in scenes if isinstance(s, dict)])
    u = fleet_manager.get(unit_id)
    if u is None:
        return _err(f"Unknown unit {unit_id}", 404)
    return _ok(u.get("scenes", []))


# ------------------------------------------------------------------
# Emergency off — zero all outputs on coordinator + all leaf units
# ------------------------------------------------------------------

def handle_emergency_off():
    for ch in config_manager.get("led_channels"):
        priority_arbiter.set_manual_channel(ch["id"], 0, 0, 0)
    for r in config_manager.get("relays"):
        priority_arbiter.set_manual_relay(r["id"], "off", 0)
    from hardware.status_led import status_led
    status_led.set_state("manual_override")

    results = {"0": "applied_local"}
    for uid in fleet_manager.get_all():
        seq = lora_protocol.send_emergency_off(uid)
        results[str(uid)] = "sent" if seq else "send_failed"
    return _ok(results)


# ------------------------------------------------------------------
# Events / Logs
# ------------------------------------------------------------------

def handle_events(query):
    """Serve events from the in-RAM bus to the dashboard's Logs view.
    Query string supports:
      since=<int>   only events newer than this seq (default 0)
      level=<str>   minimum severity (DEBUG/INFO/WARN/ERROR/FATAL)
      unit=<int>    only events from this unit_id
      limit=<int>   max events returned (default 200, capped at 500)
    """
    q = query or {}
    try:
        since = int(q.get("since", 0))
    except Exception:
        since = 0
    level = q.get("level")
    src = q.get("unit")
    try:
        src = int(src) if src is not None else None
    except Exception:
        src = None
    try:
        limit = max(1, min(500, int(q.get("limit", 200))))
    except Exception:
        limit = 200

    evts = event_bus.events_since(since, level=level, src=src, limit=limit)
    return _ok({
        "events": evts,
        "stats":  event_bus.stats(),
    })


# ------------------------------------------------------------------
# Config push progress
# ------------------------------------------------------------------

def handle_config_progress():
    """Live progress for the most recent /api/units/N/config POST. The web UI
    polls this so the upload modal can show real chunk progress instead of a
    time-based guess."""
    return _ok(dict(lora_protocol.cfg_progress))


# ------------------------------------------------------------------
# Request status from a leaf
# ------------------------------------------------------------------

# Rate-limit SR per leaf so the dashboard can't saturate LoRa by clicking
# Refresh repeatedly or opening multiple control modals in quick succession.
_SR_COOLDOWN_S = 5
_last_sr_at = {}  # {unit_id: time.time()}


def handle_request_status(unit_id):
    import time
    now = time.time()
    last = _last_sr_at.get(unit_id, 0)
    if now - last < _SR_COOLDOWN_S:
        return _ok({"requested": unit_id, "throttled": True})
    _last_sr_at[unit_id] = now
    lora_protocol.request_status(unit_id)
    return _ok({"requested": unit_id})


# ------------------------------------------------------------------
# Unclaimed-leaf onboarding (claim wizard)
# ------------------------------------------------------------------
# Leaves that have been factory-reset (long-press of the reset button)
# come up as unit_id=99 and broadcast HBs that include their chip UID.
# The dashboard surfaces them as "New device" cards; these endpoints
# back the wizard's two actions: "Flash to identify" (BLINK) and "Claim"
# (push a real config so the leaf reboots into its new unit_id).
# ------------------------------------------------------------------

_UNCLAIMED_UNIT_ID = 99


def handle_unclaimed_blink(chip_uid):
    """Tell whichever unclaimed leaf has this chip UID to flash its
    status LED so the operator can identify the physical board."""
    u = fleet_manager.get_unclaimed(chip_uid)
    if u is None:
        return _err(f"Unknown unclaimed device {chip_uid}", 404)
    lora_protocol.send_blink(_UNCLAIMED_UNIT_ID, target_uid=chip_uid)
    return _ok({"blinked": chip_uid})


def _build_blank_slate_config(new_unit_id, new_unit_name):
    """Build a minimal "blank slate" config for a freshly-claimed leaf.
    Reuses the coordinator's lora/hardware/timezone sections so the new
    leaf stays on the fleet's channel/crypt and reads its IO pins the
    same way the rest of the fleet does. Defers all user-facing config
    (channels enabled, relays, PIR, scenes) to the Config Builder.

    Why we have to send a *whole* config rather than a delta: the LoRa
    config push path (CFG_START/CHUNK/END) replaces the leaf's
    config.json wholesale. There's no merge-on-leaf mechanism, and the
    unclaimed defaults the leaf is currently running are already nearly
    identical to what we'd assemble here — the only fields that
    actually change are system.unit_id and system.unit_name.
    """
    coord = config_manager.get_all()
    return {
        "version": coord.get("version", "1.0"),
        "system": {
            "role":                   "leaf",
            "unit_id":                int(new_unit_id),
            "unit_name":              new_unit_name or f"Unit {new_unit_id}",
            "log_level":              "INFO",
            "log_buffer_size":        100,
            "heartbeat_interval_s":   coord.get("system", {}).get("heartbeat_interval_s", 30),
            "heartbeat_timeout_s":    coord.get("system", {}).get("heartbeat_timeout_s", 120),
            "pwm_update_interval_ms": 500,
        },
        "wifi":     {"ssid": "N/A", "password": ""},   # leaves don't use wifi
        "lora":     dict(coord.get("lora", {})),
        "timezone": dict(coord.get("timezone", {"name": "UTC", "utc_offset_hours": 0})),
        "hardware": dict(coord.get("hardware", {})),
        "ldr":      {"enabled": False, "smoothing_window_s": 60, "cap_rules": []},
        "pir":      [],
        "relays":   [],
        "led_channels": [
            {"id": i, "name": f"Channel {i}", "gpio_pin": pin,
             "enabled": False, "default_duty_percent": 0, "time_windows": []}
            for i, pin in zip(range(1, 9), (16, 17, 18, 19, 22, 15, 14, 13))
        ],
        "scenes":        [],
        "notifications": {"mqtt_enabled": False},
    }


async def handle_unclaimed_claim(chip_uid, body):
    """Claim a freshly-factory-reset leaf. Body: {"unit_id": 1..8, "name": "..."}.
    Builds a blank-slate config and pushes it to unit_id=99 with the
    target_uid set so only the matching board accepts the transfer.
    On success, drops the unclaimed entry so the "New device" card
    disappears from the dashboard once the leaf reboots into its new id.
    """
    u = fleet_manager.get_unclaimed(chip_uid)
    if u is None:
        return _err(f"Unknown unclaimed device {chip_uid}", 404)

    try:
        new_unit_id = int(body.get("unit_id"))
    except Exception:
        return _err("unit_id required (1..8)", 400)
    if not (1 <= new_unit_id <= 8):
        return _err("unit_id must be 1..8", 400)
    if new_unit_id in fleet_manager.get_all() and fleet_manager.is_online(new_unit_id):
        return _err(f"unit_id {new_unit_id} is already in use by an online leaf", 409)

    new_name = (body.get("name") or "").strip() or f"Unit {new_unit_id}"

    cfg = _build_blank_slate_config(new_unit_id, new_name)
    cfg_str = json.dumps(cfg)

    log.info(f"[API] Claiming chip {chip_uid} as unit {new_unit_id} ({new_name})")
    ok = await lora_protocol.send_config(_UNCLAIMED_UNIT_ID, cfg_str, target_uid=chip_uid)
    if not ok:
        prog = lora_protocol.cfg_progress
        msg = prog.get("message") if prog.get("phase") == "failed" else None
        return _err(msg or "Claim push failed — check LoRa link", 502)

    # Cache the config under the *new* unit_id and remove the unclaimed
    # entry. The leaf will reboot and start heartbeating as new_unit_id,
    # at which point fleet_manager.update() repopulates its claimed-leaves
    # slot from the incoming HB.
    try:
        _leaf_config_cache[new_unit_id] = cfg
        _persist_leaf_cfg(new_unit_id, cfg)
    except Exception as e:
        log.warn(f"[API] Could not cache claimed leaf {new_unit_id}: {e}")
    fleet_manager.drop_unclaimed(chip_uid)
    return _ok({"claimed": chip_uid, "unit_id": new_unit_id, "name": new_name})


# ------------------------------------------------------------------
# Manual time-set (operator override)
# ------------------------------------------------------------------
# Use case: NTP is unreachable AND the DS3231 battery is dead, so the
# coord has no way to learn the wall-clock on its own. The dashboard's
# "Set time now" button in the time-waiting banner posts here with the
# browser's current epoch — we apply it to the MCU clock + DS3231 +
# broadcast it to leaves, and flip system_status.time_synced so the
# schedule resumes.

def handle_time_sync(body):
    import time as _time
    from shared.system_status import system_status, time_is_sane

    try:
        epoch = int(body.get("epoch"))
    except Exception:
        return _err("epoch (Unix seconds, integer) required", 400)
    # Sanity: refuse anything before Nov 2023 (Lokki's earliest plausible
    # deployment date) so a mis-clicked stale browser tab can't push us
    # back into the "time_synced but wrong year" hole.
    if epoch < 1_700_000_000:
        return _err("epoch must be >= 1700000000 (Nov 2023)", 400)
    tz_offset = config_manager.get("timezone").get("utc_offset_hours", 0)
    local_sec = epoch + int(tz_offset * 3600)

    # Set the MCU's internal RTC. (Year, Month, Day, weekday, Hour, Min,
    # Sec, Subsec) — note machine.RTC().datetime() takes weekday in the
    # 4th slot, distinct from time.localtime() and from DS3231's tuple.
    try:
        import machine
        lt = _time.localtime(local_sec)
        machine.RTC().datetime((lt[0], lt[1], lt[2], (lt[6] % 7) + 1,
                                lt[3], lt[4], lt[5], 0))
    except Exception as e:
        return _err(f"MCU RTC set failed: {e}", 500)

    # Best-effort DS3231 mirror so the time survives a power cycle.
    try:
        from hardware.rtc_shared import rtc as _rtc
        from hardware import urtc
        dt = urtc.seconds2tuple(local_sec)
        _rtc.datetime(dt)
        log.info("[API] DS3231 updated from manual time-sync")
    except Exception as e:
        log.warn(f"[API] DS3231 write failed (MCU clock still set): {e}")

    if time_is_sane():
        system_status.mark_time_synced("manual")

    # Broadcast TS so leaves catch up immediately instead of waiting for
    # the next periodic broadcast. Best-effort — a leaf without LoRa
    # picks up at the next regular cadence anyway.
    try:
        lora_protocol.broadcast_time_sync(epoch, tz_offset)
    except Exception as e:
        log.warn(f"[API] TS broadcast after manual sync failed: {e}")

    return _ok({"epoch": epoch, "source": "manual"})


# ------------------------------------------------------------------
# Reboot coordinator
# ------------------------------------------------------------------

def handle_reboot():
    import machine
    log.info("[API] Reboot requested")
    # Schedule reboot after response is sent
    import asyncio
    async def do_reboot():
        await asyncio.sleep(1)  # Give time for response to be sent
        machine.reset()
    asyncio.create_task(do_reboot())
    return _ok({"rebooting": True})
