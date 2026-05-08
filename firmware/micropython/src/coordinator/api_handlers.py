import json
from coordinator.fleet_manager import fleet_manager
from comms.lora_protocol import lora_protocol
from core.config_manager import config_manager
from core.priority_arbiter import priority_arbiter
from shared.system_status import system_status
from shared.simple_logger import Logger

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
            json.dump(cfg, f)
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
                "name": c.get("name", c["id"]),
                "enabled": c.get("enabled", True),
                "default_duty_percent": c.get("default_duty_percent", 0),
            }
            for c in cfg.get("led_channels", []) if isinstance(c, dict) and "id" in c
        ],
        "relays": [
            {
                "id":   r["id"],
                "name": r.get("name", r["id"]),
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

def handle_fleet_status():
    import time
    from hardware.pwm_control import pwm_controller
    from hardware.relay_control import relay_controller
    from hardware.pir_manager import pir_manager
    from hardware.ldr_monitor import ldr_monitor

    fleet = {str(uid): u for uid, u in fleet_manager.get_all().items()}
    fleet["0"] = {
        "name": config_manager.unit_name,
        "online": True,
        "last_seen": time.time(),         # coordinator is "always now"
        "uptime": system_status.get_uptime(),
        "ch": pwm_controller.get_all(),
        "rl": list(relay_controller.get_all().values()),
        "pir": list(pir_manager.get_all_states().values()),
        "ldr": ldr_monitor.ambient_percent,
        "err": system_status.error_count,
        "rssi": None,                     # local, no link
    }
    return _ok(fleet)


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
            "ch": pwm_controller.get_all(),  # Already returns sorted list
            "rl": list(relay_controller.get_all().values()),
            "pir": list(pir_manager.get_all_states().values()),
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
            # If the incoming config has the masked wifi password, restore the
            # real one from the live config — otherwise we'd silently break wifi.
            try:
                incoming = json.loads(config_str)
            except Exception:
                incoming = None
            if (isinstance(incoming, dict)
                    and isinstance(incoming.get("wifi"), dict)
                    and incoming["wifi"].get("password") == "********"):
                live_pw = config_manager.get("wifi").get("password", "")
                incoming["wifi"]["password"] = live_pw
                config_str = json.dumps(incoming)
            config_manager.replace(config_str)
            log.info("[API] Local config replaced")
            return _ok({"applied": "local"})
        except Exception as e:
            return _err(str(e))

    ok = await lora_protocol.send_config(unit_id, config_str)
    if ok:
        # Cache the FULL leaf config ONLY AFTER a successful LoRa transfer
        # so the coordinator's view perfectly matches the leaf's actual state.
        try:
            cfg = json.loads(config_str)
            _leaf_config_cache[unit_id] = cfg
            _persist_leaf_cfg(unit_id, cfg)
            log.info(f"[API] Cached leaf {unit_id} config")
        except Exception as e:
            log.warn(f"[API] Could not cache leaf {unit_id} config: {e}")
        return _ok({"sent_to": unit_id})
        
    return _err(f"Config transfer to unit {unit_id} failed — check LoRa link", 502)


def handle_full_config():
    """Return full config but mask the wifi password.

    Why: dashboard is unauthenticated; raw config exposes wifi.password to
    anyone on the LAN. The Config Builder doesn't need the real value to
    edit other fields — if the user wants to change the password they re-enter it.
    """
    cfg = config_manager.get_all()
    # Shallow-copy + replace the wifi sub-dict so we don't mutate the live config.
    if isinstance(cfg.get("wifi"), dict) and cfg["wifi"].get("password"):
        masked = dict(cfg)
        masked["wifi"] = dict(cfg["wifi"])
        masked["wifi"]["password"] = "********"
        return _ok(masked)
    return _ok(cfg)


def handle_unit_config(unit_id):
    """Return the full config for a unit — coordinator reads its live config,
    leaves return the coordinator's cached copy. Used by both the dashboard
    Control modal (which reads led_channels/relays) AND the Config Builder
    (which reads every section to populate its form)."""
    if unit_id == 0:
        cfg = config_manager.get_all()
        # Reuse the same wifi-password masking as /api/config so the Builder
        # round-trip works identically whether the user picks "Coordinator"
        # or unit 0 from the unit selector.
        if isinstance(cfg.get("wifi"), dict) and cfg["wifi"].get("password"):
            masked = dict(cfg)
            masked["wifi"] = dict(cfg["wifi"])
            masked["wifi"]["password"] = "********"
            cfg = masked
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

def handle_manual_override(unit_id, payload):
    log.info(f"[API] Manual override request for unit {unit_id}, payload: {payload}")
    
    try:
        channels  = payload.get("ch", [])
        relays    = payload.get("rl", [])
        revert_s  = payload.get("revert_s", 0)
        fade_ms   = payload.get("fade_ms", 0)
        
        log.info(f"[API] Parsed: {len(channels)} channels, {len(relays)} relays")

        if unit_id == 0:
            for cid, val in channels:
                log.info(f"[API] Setting LED {cid}={val}%, fade={fade_ms}ms, revert={revert_s}s")
                priority_arbiter.set_manual(cid, val, fade_ms, revert_s)
            for rid, val in relays:
                log.info(f"[API] Setting relay {rid}={val}, revert={revert_s}s")
                priority_arbiter.set_manual(rid, val, 0, revert_s)
            log.info("[API] Manual override applied successfully")
            from hardware.status_led import status_led
            status_led.set_state("manual_override" if priority_arbiter.has_manual() else _ok_led_state())
            return _ok({"applied": "local"})

        seq = lora_protocol.send_manual_override(unit_id, channels, relays, revert_s)
        return _ok({"sent_to": unit_id}) if seq else _err("Send failed", 502)
    except Exception as e:
        log.error(f"[API] Manual override exception: {e}")
        import sys
        sys.print_exception(e)
        return _err(f"Manual override failed: {e}")


def handle_manual_clear(unit_id, output_id=None):
    if unit_id == 0:
        if output_id:
            priority_arbiter.clear_manual(output_id)
        else:
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
        priority_arbiter.set_manual(ch["id"], 0, 0, 0)
    for r in config_manager.get("relays"):
        priority_arbiter.set_manual(r["id"], 0, 0, 0)
    from hardware.status_led import status_led
    status_led.set_state("manual_override")

    results = {"0": "applied_local"}
    for uid in fleet_manager.get_all():
        seq = lora_protocol.send_emergency_off(uid)
        results[str(uid)] = "sent" if seq else "send_failed"
    return _ok(results)


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
