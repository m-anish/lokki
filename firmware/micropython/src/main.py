import asyncio
import gc
import time

from core.config_manager import config_manager, SafeModeError
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

async def schedule_task(interval_ms):
    while True:
        try:
            desired = schedule_engine.get_desired_state()
            priority_arbiter.set_schedule(desired)
        except Exception as e:
            log.error(f"[SCHEDULE] {e}")
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
    while True:
        try:
            payload = {
                "uptime":  system_status.get_uptime(),
                "ch":      list(pwm_controller.get_all().values()),
                "rl":      list(relay_controller.get_all().values()),
                "pir":     list(pir_manager.get_all_states().values()),
                "ldr":     ldr_monitor.ambient_percent,
                "err":     system_status.error_count,
            }
            lora_protocol.send_heartbeat(payload)
        except Exception as e:
            log.error(f"[HB] Broadcast error: {e}")
        await asyncio.sleep(interval_s)


async def fleet_timeout_task(fleet_manager, interval_s=10):
    """Coordinator task: mark leaves offline on heartbeat timeout."""
    while True:
        fleet_manager.check_timeouts()
        any_offline = any(
            not u["online"] for u in fleet_manager.get_all().values()
        )
        status_led.set_state("leaf_offline" if any_offline else "running_ok")
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
        status_led.set_state("manual_override" if priority_arbiter.has_manual() else "running_ok")
    lora_protocol.on("MO", on_manual_override)

    def on_status_request(src, payload):
        response = {
            "uptime":  system_status.get_uptime(),
            "ch":      list(pwm_controller.get_all().values()),
            "rl":      list(relay_controller.get_all().values()),
            "pir":     list(pir_manager.get_all_states().values()),
            "ldr":     ldr_monitor.ambient_percent,
            "err":     system_status.error_count,
        }
        lora_protocol.send("SRP", src, response)
    lora_protocol.on("SR", on_status_request)


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
    try:
        cfg = config_manager
    except SafeModeError as e:
        log.error(f"[MAIN] SafeModeError: {e}")
        await safe_mode()
        return

    hw  = cfg.get("hardware")
    sys = cfg.get("system")
    role = cfg.role

    log.info(f"[MAIN] Lokki booting — role={role} unit_id={cfg.unit_id} name={cfg.unit_name}")

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
    status_led.set_state("lora_init")
    try:
        lora_protocol.init()
        system_status.set_connection_status(lora=True)
    except Exception as e:
        log.error(f"[MAIN] LoRa init failed: {e}")
        system_status.set_connection_status(lora=False)

    # --- Fleet manager init (coordinator) ---
    fleet_mgr = None
    if role == "coordinator":
        from coordinator.fleet_manager import fleet_manager as fleet_mgr
        fleet_mgr.init()

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
                try:
                    sync_time_ntp()
                    log.info("[MAIN] NTP synced + TIME_SYNC broadcast sent")
                except Exception as e:
                    log.warn(f"[MAIN] NTP sync failed: {e} — continuing with RTC time")
            else:
                log.warn("[MAIN] WiFi failed — running on RTC")
        except Exception as e:
            log.error(f"[MAIN] WiFi/NTP error: {e}")

    status_led.set_state("running_ok")

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
