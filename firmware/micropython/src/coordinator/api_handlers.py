import json
from coordinator.fleet_manager import fleet_manager
from comms.lora_protocol import lora_protocol
from core.config_manager import config_manager
from core.priority_arbiter import priority_arbiter
from shared.system_status import system_status
from shared.simple_logger import Logger

log = Logger()


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
    from hardware.pwm_control import pwm_controller
    from hardware.relay_control import relay_controller
    from hardware.pir_manager import pir_manager
    from hardware.ldr_monitor import ldr_monitor
    
    # Get all leaf units
    fleet = fleet_manager.get_all()
    
    # Add coordinator's own status as unit 0
    fleet[0] = {
        "online": True,
        "uptime": system_status.get_uptime(),
        "ch": pwm_controller.get_all(),  # Already returns sorted list
        "rl": list(relay_controller.get_all().values()),
        "pir": list(pir_manager.get_all_states().values()),
        "ldr": ldr_monitor.ambient_percent,
        "err": system_status.error_count,
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
            config_manager.replace(config_str)
            log.info("[API] Local config replaced")
            return _ok({"applied": "local"})
        except Exception as e:
            return _err(str(e))

    ok = await lora_protocol.send_config(unit_id, config_str)
    if ok:
        return _ok({"sent_to": unit_id})
    return _err(f"Config transfer to unit {unit_id} failed — check LoRa link", 502)


def handle_unit_config(unit_id):
    if unit_id == 0:
        # Return coordinator's config including channel/relay details
        led_channels = config_manager.get("led_channels")
        relays = config_manager.get("relays")
        
        return _ok({
            "version":   config_manager.version,
            "role":      config_manager.role,
            "unit_id":   config_manager.unit_id,
            "unit_name": config_manager.unit_name,
            "led_channels": [{"id": ch["id"], "name": ch.get("name", ch["id"]), "enabled": ch.get("enabled", True), "default_duty_percent": ch.get("default_duty_percent", 0)} for ch in led_channels],
            "relays": [{"id": r["id"], "name": r.get("name", r["id"]), "enabled": r.get("enabled", True), "default_state": r.get("default_state", "off")} for r in relays],
        })
    u = fleet_manager.get(unit_id)
    if u is None:
        return _err(f"Unknown unit {unit_id}", 404)
    return _ok({"unit_id": unit_id, "note": "fetch config via direct connection to leaf"})


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
            results[0] = "applied_local"
        else:
            seq = lora_protocol.send_scene(uid, scene_name)
            results[uid] = "sent" if seq else "send_failed"
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
        readings[uid] = u.get("sensors", {})
    return _ok(readings)


# ------------------------------------------------------------------
# Request status from a leaf
# ------------------------------------------------------------------

def handle_request_status(unit_id):
    lora_protocol.request_status(unit_id)
    return _ok({"requested": unit_id})
