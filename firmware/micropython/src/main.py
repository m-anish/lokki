import asyncio
import gc

from core.config_manager import config_manager, SafeModeError
from core.schedule_engine import schedule_engine
from core.priority_arbiter import priority_arbiter
from hardware.pwm_control import pwm_controller
from hardware.relay_control import relay_controller
from hardware.pir_manager import pir_manager
from hardware.ldr_monitor import ldr_monitor
from hardware.status_led import status_led
from hardware.rtc_shared import rtc
from shared.simple_logger import Logger

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
    pwm_controller.init_from_config(cfg.get("led_channels"), freq_hz)
    relay_controller.init_from_config(cfg.get("relays"))
    pir_manager.init_from_config(cfg.get("pir"))
    ldr_monitor.init_from_config(cfg.get("ldr"), hw)

    # Wire LDR cap changes into arbiter
    ldr_monitor.on_cap_change(priority_arbiter.set_ldr_cap)

    # Wire PIR callbacks
    _setup_pir_handlers(cfg.get("pir"), cfg.get("scenes"))

    # --- Schedule and arbiter init ---
    schedule_engine.init_from_config(cfg.get("led_channels"), cfg.get("relays"))
    priority_arbiter.init_from_config(cfg.get("led_channels"), cfg.get("relays"))

    # --- WiFi + NTP (coordinator only) ---
    wifi_ok = False
    if role == "coordinator":
        status_led.set_state("wifi_connecting")
        try:
            from comms.wifi_connect import connect_wifi, sync_time_ntp
            wifi_ok = connect_wifi()
            if wifi_ok:
                log.info("[MAIN] WiFi connected")
                sync_time_ntp()
                log.info("[MAIN] NTP synced")
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

    if sys.get("log_level") == "DEBUG":
        tasks.append(asyncio.create_task(ram_telemetry_task(60)))

    # Coordinator extras (web server — Phase 3; LoRa — Phase 2)
    if role == "coordinator" and wifi_ok:
        try:
            from coordinator.web_server import web_server
            tasks.append(asyncio.create_task(web_server.start_and_serve()))
            log.info("[MAIN] Web server task added")
        except Exception as e:
            log.error(f"[MAIN] Web server init failed: {e}")

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
