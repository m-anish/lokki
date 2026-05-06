import asyncio
import gc

from core.config_manager import config_manager
from core.schedule_engine import schedule_engine
from core.priority_arbiter import priority_arbiter
from hardware.pwm_control import pwm_controller
from hardware.relay_control import relay_controller
from hardware.pir_manager import pir_manager
from hardware.ldr_monitor import ldr_monitor
from hardware.status_led import status_led
from hardware.rtc_shared import rtc
from comms.lora_protocol import lora_protocol
from shared.simple_logger import Logger
from shared.system_status import system_status

log = Logger()


# ------------------------------------------------------------------
# PIR action executor
# Translates config on_motion / on_vacancy actions into arbiter calls
# ------------------------------------------------------------------

def _build_pir_handler(action_cfg, scenes_by_name):
    act = action_cfg.get("action", "revert_to_schedule")

    if act == "revert_to_schedule":
        def handler(pir_id):
            # Clear PIR state for all outputs — schedule takes over
            priority_arbiter.clear_all_pir()
        return handler

    if act == "set_scene":
        scene_name = action_cfg.get("scene_name", "")
        def handler(pir_id):
            scene = scenes_by_name.get(scene_name)
            if scene:
                priority_arbiter.apply_scene(scene)
        return handler

    if act == "set_led_channels":
        channels = action_cfg.get("channels", [])
        duty = action_cfg.get("duty_percent", 100)
        fade = action_cfg.get("fade_ms", 0)
        def handler(pir_id):
            for cid in channels:
                priority_arbiter.set_pir(cid, duty, fade_ms=fade)
        return handler

    if act == "set_relay":
        relay_id = action_cfg.get("relay_id", "")
        state = action_cfg.get("state", "on")
        def handler(pir_id):
            priority_arbiter.set_pir(relay_id, state)
        return handler

    return lambda pir_id: None


def _setup_pir_handlers(pir_cfg, scenes):
    scenes_by_name = {s["name"]: s for s in scenes}
    for p in pir_cfg:
        if not p.get("enabled", False):
            continue
        pid = p["id"]
        on_motion_handler = _build_pir_handler(p.get("on_motion", {}), scenes_by_name)
        on_vacancy_handler = _build_pir_handler(p.get("on_vacancy", {}), scenes_by_name)
        pir_manager.on_motion(pid, on_motion_handler)
        pir_manager.on_vacancy(pid, on_vacancy_handler)


# ------------------------------------------------------------------
# Async tasks
# ------------------------------------------------------------------

def _ok_led_state():
    """Return the appropriate steady LED state depending on LoRa connectivity."""
    return "running_lora_ok" if system_status.lora_connected else "running_ok"


async def schedule_task(interval_ms):
    while True:
        try:
            desired = schedule_engine.get_desired_state()
            priority_arbiter.set_schedule(desired)
        except Exception as e:
            log.error(f"[SCHEDULE] {e}")
            system_status.record_error(f"schedule: {e}")
        await asyncio.sleep_ms(interval_ms)


async def ram_telemetry_task(interval_s):
    while True:
        gc.collect()
        free = gc.mem_free()
        log.debug(f"[RAM] free={free} bytes")
        await asyncio.sleep(interval_s)


async def heartbeat_broadcast_task(interval_s, unit_id):
    """Leaf task: send HB to coordinator at regular intervals with jitter."""
    jitter_ms = unit_id * 500
    await asyncio.sleep_ms(jitter_ms)
    # Cached at task entry so we don't pay config_manager attribute lookups every HB.
    name = config_manager.unit_name
    while True:
        try:
            payload = {
                "name":    name,
                "uptime":  system_status.get_uptime(),
                "ch":      pwm_controller.get_all(),
                "rl":      list(relay_controller.get_all().values()),
                "pir":     list(pir_manager.get_all_states().values()),
                "ldr":     ldr_monitor.ambient_percent,
                "err":     system_status.error_count,
                "rssi":    lora_protocol.last_rx_rssi,
            }
            lora_protocol.send_heartbeat(payload)
        except Exception as e:
            log.error(f"[HB] Broadcast error: {e}")
            system_status.record_error(f"hb: {e}")
        await asyncio.sleep(interval_s)


async def fleet_timeout_task(fleet_manager, interval_s=10):
    """Coordinator task: mark leaves offline on heartbeat timeout."""
    while True:
        fleet_manager.check_timeouts()
        any_offline = any(
            not u["online"] for u in fleet_manager.get_all().values()
        )
        if any_offline:
            status_led.set_state("leaf_offline")
        elif priority_arbiter.has_manual():
            status_led.set_state("manual_override")
        else:
            status_led.set_state(_ok_led_state())
        await asyncio.sleep(interval_s)


def _register_lora_handlers(role, fleet_manager=None):
    """Wire all inbound LoRa message handlers."""
    scenes = {s["name"]: s for s in config_manager.get("scenes")}

    if role == "coordinator" and fleet_manager:
        def on_heartbeat(src, payload):
            fleet_manager.update(src, payload)
        lora_protocol.on("HB", on_heartbeat)

        def on_status_response(src, payload):
            fleet_manager.update(src, payload)
        lora_protocol.on("SRP", on_status_response)

    def on_time_sync(src, payload):
        # Leaf only — set local RTC from coordinator TS broadcast
        epoch = payload.get("epoch")
        if epoch and role == "leaf":
            try:
                from hardware import urtc
                tz = payload.get("tz", 0)
                local_sec = int(epoch) + int(tz * 3600)
                dt = urtc.seconds2tuple(local_sec)
                rtc.datetime(dt)
                log.info(f"[LORA] Time synced from coordinator: {dt}")
            except Exception as e:
                log.warn(f"[LORA] Time sync apply failed: {e}")
    lora_protocol.on("TS", on_time_sync)

    def on_scene(src, payload):
        scene_name = payload.get("scene")
        scene = scenes.get(scene_name)
        if scene:
            priority_arbiter.apply_scene(scene)
            log.info(f"[LORA] Scene '{scene_name}' applied from {src}")
        else:
            log.warn(f"[LORA] Unknown scene '{scene_name}' from {src}")
    lora_protocol.on("SC", on_scene)

    def on_manual_override(src, payload):
        channels = payload.get("ch", [])
        relays   = payload.get("rl", [])
        revert_s = payload.get("revert_s", 0)
        fade_ms  = payload.get("fade_ms", 0)
        if revert_s == -1:
            priority_arbiter.clear_all_manual()
        else:
            for item in channels:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    priority_arbiter.set_manual(item[0], item[1], fade_ms, revert_s)
            for item in relays:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    priority_arbiter.set_manual(item[0], item[1], 0, revert_s)
        status_led.set_state("manual_override" if priority_arbiter.has_manual() else _ok_led_state())
    lora_protocol.on("MO", on_manual_override)

    def on_status_request(src, payload):
        # Wire budget: 200B E220 packet limit minus envelope (~30B) minus
        # HMAC field (~24B when signed) leaves ~146B for the payload dict.
        # Drop scene names from the end until we fit.
        scene_names = list(scenes.keys())
        response = {
            "name":    config_manager.unit_name,
            "uptime":  system_status.get_uptime(),
            "ch":      pwm_controller.get_all(),
            "rl":      list(relay_controller.get_all().values()),
            "pir":     list(pir_manager.get_all_states().values()),
            "ldr":     ldr_monitor.ambient_percent,
            "err":     system_status.error_count,
            "rssi":    lora_protocol.last_rx_rssi,
            "sc":      scene_names,
        }
        import json as _json
        while len(_json.dumps(response).encode()) > 146 and response["sc"]:
            response["sc"].pop()
        lora_protocol.send("SRP", src, response)
    lora_protocol.on("SR", on_status_request)

    def on_emergency_off(src, _payload):
        for ch in config_manager.get("led_channels"):
            priority_arbiter.set_manual(ch["id"], 0, 0, 0)
        for r in config_manager.get("relays"):
            priority_arbiter.set_manual(r["id"], 0, 0, 0)
        status_led.set_state("manual_override")
        log.info(f"[LORA] Emergency off from {src}")
    lora_protocol.on("EO", on_emergency_off)

    if role == "leaf":
        import time
        _cfg_transfers = {}
        
        def on_cfg_start(src, payload):
            tid = payload.get("transfer_id")
            if tid:
                _cfg_transfers[tid] = {"chunks": {}, "total": payload.get("total_chunks", 0), "last": time.time()}
                log.info(f"[LORA] Started config transfer {tid} from {src}")
        lora_protocol.on("CFG_START", on_cfg_start)

        def on_cfg_chunk(src, payload):
            tid = payload.get("transfer_id")
            idx = payload.get("chunk_index")
            data = payload.get("data")
            if tid in _cfg_transfers and idx is not None and data is not None:
                _cfg_transfers[tid]["chunks"][idx] = data
                _cfg_transfers[tid]["last"] = time.time()
        lora_protocol.on("CFG_CHUNK", on_cfg_chunk)

        def on_cfg_end(src, payload):
            tid = payload.get("transfer_id")
            seq = payload.get("_seq")
            
            if tid in _cfg_transfers:
                transfer = _cfg_transfers[tid]
                total = transfer["total"]
                chunks = transfer["chunks"]
                
                if len(chunks) == total:
                    config_str = "".join(chunks.get(i, "") for i in range(total))
                    expected_crc = payload.get("checksum")
                    from comms.lora_protocol import _crc32
                    actual_crc = "{:08x}".format(_crc32(config_str))
                    if expected_crc == actual_crc:
                        log.info(f"[LORA] Config transfer {tid} verified. Rebooting to apply.")
                        # Send success ACK BEFORE rebooting!
                        if seq is not None:
                            lora_protocol.send("ACK", src, {"ack_seq": seq, "ok": True})
                        del _cfg_transfers[tid]
                        config_manager.replace(config_str)
                        import machine
                        import asyncio
                        async def do_reboot():
                            await asyncio.sleep(1)
                            machine.reset()
                        asyncio.create_task(do_reboot())
                        return
                    else:
                        log.error(f"[LORA] Config transfer {tid} checksum mismatch")
                else:
                    log.error(f"[LORA] Config transfer {tid} missing {total - len(chunks)} chunks")
                    
                # If we are here, it failed. Tell coordinator which chunks we need!
                missing = [i for i in range(total) if i not in chunks]
                if seq is not None:
                    lora_protocol.send("ACK", src, {
                        "ack_seq": seq, 
                        "ok": False, 
                        "reason": "CHECKSUM_FAIL" if len(chunks) == total else "MISSING_CHUNKS",
                        "missing": missing
                    })
                # Don't delete transfer if missing chunks, so smart retry can fill them in!
                if len(chunks) == total:
                    del _cfg_transfers[tid]
            else:
                # Unknown transfer
                if seq is not None:
                    lora_protocol.send("ACK", src, {"ack_seq": seq, "ok": False, "reason": "UNKNOWN_TRANSFER"})
                    
        lora_protocol.on("CFG_END", on_cfg_end)


async def safe_mode():
    log.error("[MAIN] Entering safe mode — all outputs off")
    status_led.set_state("error")
    try:
        pwm_controller.set_all(0)
    except Exception:
        pass
    try:
        relay_controller.deinit()
    except Exception:
        pass
    # In safe mode: keep status LED pattern running, do nothing else
    await status_led.run_pattern()


# ------------------------------------------------------------------
# Boot
# ------------------------------------------------------------------

async def main():
    status_led.set_state("booting")
    asyncio.create_task(status_led.run_pattern())

    # --- Config ---
    if config_manager.safe_mode_reason:
        log.error(f"[MAIN] Config load failed: {config_manager.safe_mode_reason}")
        await safe_mode()
        return
    cfg = config_manager

    hw  = cfg.get("hardware")
    sys = cfg.get("system")
    role = cfg.role

    log.info(f"[MAIN] Lokki booting — role={role} unit_id={cfg.unit_id} name={cfg.unit_name}")

    # Re-bind status LED to configured pin (default singleton uses GPIO 5)
    status_led.init_from_config(hw)

    # --- Hardware init ---
    freq_hz = hw.get("pwm_freq_hz", 1000)
    gamma   = hw.get("gamma", 2.2)
    pwm_controller.init_from_config(cfg.get("led_channels"), freq_hz, gamma)
    relay_controller.init_from_config(cfg.get("relays"))
    pir_manager.init_from_config(cfg.get("pir"))
    ldr_monitor.init_from_config(cfg.get("ldr"), hw)

    from hardware.i2c_sensors import i2c_sensors
    i2c_sensors.init()

    # Wire LDR cap changes into arbiter
    ldr_monitor.on_cap_change(priority_arbiter.set_ldr_cap)

    # Wire PIR callbacks
    _setup_pir_handlers(cfg.get("pir"), cfg.get("scenes"))

    # --- Schedule and arbiter init ---
    schedule_engine.init_from_config(cfg.get("led_channels"), cfg.get("relays"))
    priority_arbiter.init_from_config(cfg.get("led_channels"), cfg.get("relays"))

    # --- LoRa init ---
    # No runtime register writes any more. The LoRa module is provisioned
    # ONCE via utils/e220_provisioner_cli.py (over a Pico-side bridge) and
    # then runs forever in NVRAM-set state. lora_transport.init() just
    # opens the UART, drives M0=0/M1=0, and waits for AUX HIGH.
    #
    # Modules MUST be provisioned with --rssi-byte (REG3 bit 7 = 1) — the
    # transport's recv() unconditionally strips a trailing byte and treats
    # it as RSSI. Without that provisioning the strip eats real payload
    # data and JSON parsing fails.
    status_led.set_state("lora_init")
    try:
        lora_protocol.init()
        system_status.set_connection_status(lora=True)
    except Exception as e:
        log.error(f"[MAIN] LoRa init failed: {e}")
        system_status.set_connection_status(lora=False)
        system_status.record_error(f"lora_init: {e}")

    # --- Fleet manager init (coordinator) ---
    fleet_mgr = None
    if role == "coordinator":
        from coordinator.fleet_manager import fleet_manager as fleet_mgr
        fleet_mgr.init()
        # Re-hydrate any leaf configs cached on flash from prior pushes.
        from coordinator.api_handlers import load_leaf_cache_from_flash
        load_leaf_cache_from_flash()

    # --- Register LoRa message handlers ---
    _register_lora_handlers(role, fleet_mgr)

    # --- WiFi + NTP (coordinator only) ---
    wifi_ok = False
    if role == "coordinator":
        status_led.set_state("wifi_connecting")
        try:
            from comms.wifi_connect import connect_wifi, sync_time_ntp
            wifi_ok = connect_wifi()
            if wifi_ok:
                log.info("[MAIN] WiFi connected")
                system_status.set_connection_status(wifi=True)
                # NTP sync is optional - can be disabled in config
                tz_config = cfg.get("timezone") or {}
                ntp_enabled = tz_config.get("ntp_enabled", False)
                if ntp_enabled:
                    log.info("[MAIN] NTP enabled, attempting sync...")
                    if sync_time_ntp():
                        log.info("[MAIN] NTP synced successfully")
                    else:
                        log.warn("[MAIN] NTP sync failed — continuing with RTC time")
                else:
                    log.info("[MAIN] NTP disabled in config — using RTC time")
            else:
                log.warn("[MAIN] WiFi failed — running on RTC")
                system_status.record_error("wifi_connect failed")
        except Exception as e:
            log.error(f"[MAIN] WiFi/NTP error: {e}")
            system_status.record_error(f"wifi: {e}")

    status_led.set_state(_ok_led_state())

    # --- Task list ---
    tasks = []

    interval_ms = sys.get("pwm_update_interval_ms", 500)
    tasks.append(asyncio.create_task(schedule_task(interval_ms)))
    tasks.append(asyncio.create_task(pir_manager.run_all()))
    tasks.append(asyncio.create_task(ldr_monitor.run()))
    tasks.append(asyncio.create_task(lora_protocol.listen_task()))

    if i2c_sensors.has_sensors:
        tasks.append(asyncio.create_task(i2c_sensors.run()))

    if sys.get("log_level") == "DEBUG":
        tasks.append(asyncio.create_task(ram_telemetry_task(60)))

    hb_interval = cfg.get("lora").get("heartbeat_interval_s", 30)

    if role == "coordinator":
        tasks.append(asyncio.create_task(fleet_timeout_task(fleet_mgr)))
        if wifi_ok:
            # MQTT notifications (if enabled)
            try:
                from comms.mqtt_notifier import mqtt_notifier
                if mqtt_notifier.connect():
                    log.info("[MAIN] MQTT connected")
                else:
                    log.info("[MAIN] MQTT disabled or unavailable")
            except Exception as e:
                log.error(f"[MAIN] MQTT init failed: {e}")
            # Web server
            try:
                from coordinator.web_server import web_server
                tasks.append(asyncio.create_task(web_server.start_and_serve()))
                log.info("[MAIN] Web server task added")
            except Exception as e:
                log.error(f"[MAIN] Web server init failed: {e}")
    else:
        # Leaf: broadcast heartbeats to coordinator
        tasks.append(asyncio.create_task(
            heartbeat_broadcast_task(hb_interval, cfg.unit_id)
        ))

    log.info(f"[MAIN] Running {len(tasks)} tasks")

    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        log.error(f"[MAIN] Fatal task error: {e}")
        await safe_mode()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FATAL:", e)
