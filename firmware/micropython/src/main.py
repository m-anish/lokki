import asyncio
import gc
import os
import time as _time
import machine as _machine

from core.config_manager import config_manager
from core.schedule_engine import schedule_engine
from core.priority_arbiter import priority_arbiter
from hardware.pwm_control import pwm_controller
from hardware.relay_control import relay_controller
from hardware.pir_manager import pir_manager
from hardware.ldr_monitor import ldr_monitor
from hardware.status_led import (
    status_led,
    FLASH_LORA_OK_RGB,
    FLASH_LORA_FAIL_RGB,
    FLASH_BOOT_RGB,
)
from hardware.rtc_shared import rtc
from comms.lora_protocol import lora_protocol
from comms.lora_transport import lora_transport
from shared.simple_logger import Logger
from shared.system_status import system_status
from shared.event_bus import event_bus

log = Logger()


# ------------------------------------------------------------------
# LoRa boot retry — persistent counter across soft_reset
# ------------------------------------------------------------------
# The E220 module is timing-sensitive at power-on: sometimes the first
# register-write round-trip in apply_from_config fails ("short reply
# None") immediately after a cold boot, but a subsequent Thonny
# "Stop/Restart Backend" (= machine.soft_reset()) reliably gets it
# right. We use a tiny on-flash counter to do the same thing
# automatically: if LoRa init fails, increment the counter, soft-reset,
# and try again. After 3 failed attempts we give up and continue boot
# with LoRa disabled (slot 'lora_disabled' LED state — purple solid).
_LORA_RETRY_FILE  = "/lora_retry_count"
_LORA_MAX_RETRIES = 3


def _lora_retry_read():
    try:
        with open(_LORA_RETRY_FILE, "r") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _lora_retry_write(n):
    try:
        with open(_LORA_RETRY_FILE, "w") as f:
            f.write(str(n))
    except Exception:
        pass


def _lora_retry_clear():
    try:
        os.remove(_LORA_RETRY_FILE)
    except Exception:
        pass


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
                priority_arbiter.set_pir_channel(cid, duty, fade_ms=fade)
        return handler

    if act == "set_relay":
        relay_id = action_cfg.get("relay_id")
        state = action_cfg.get("state", "on")
        def handler(pir_id):
            if relay_id is not None:
                priority_arbiter.set_pir_relay(relay_id, state)
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


async def leaf_status_task():
    """Leaf task: Ensure status LED reverts when manual overrides expire."""
    while True:
        if priority_arbiter.has_manual():
            status_led.set_state("manual_override")
        else:
            status_led.set_state(_ok_led_state())
        await asyncio.sleep(2)


async def time_sync_task():
    """Coordinator task: Sync NTP and broadcast time to leaves periodically."""
    import time
    while True:
        tz_config = config_manager.get("timezone") or {}
        if tz_config.get("ntp_enabled", True):
            try:
                from comms.wifi_connect import sync_time_ntp
                if sync_time_ntp():
                    log.info("[MAIN] Periodic NTP sync successful")
                else:
                    log.warn("[MAIN] Periodic NTP sync failed")
            except Exception as e:
                log.warn(f"[MAIN] NTP sync exception: {e}")
        try:
            tz_offset = tz_config.get("utc_offset_hours", 0)
            lora_protocol.broadcast_time_sync(time.time(), tz_offset)
        except Exception as e:
            log.warn(f"[MAIN] TS broadcast failed: {e}")
        await asyncio.sleep(86400)


async def schedule_task(interval_ms):
    while True:
        try:
            channel_desired, relay_desired = schedule_engine.get_desired_state()
            priority_arbiter.set_schedule(channel_desired, relay_desired)
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


def _hb_flash_rgb():
    """Pick the heartbeat-flash color from the boot-time LoRa config
    outcome. Blue when the volatile-register write succeeded; red when
    it didn't (the module is running in whatever NVRAM state it had,
    which is probably wrong and the operator should know)."""
    from hardware.status_led import FLASH_LORA_OK_RGB, FLASH_LORA_FAIL_RGB
    from comms.lora_transport import lora_transport as _lt
    return FLASH_LORA_OK_RGB if getattr(_lt, "config_ok", False) else FLASH_LORA_FAIL_RGB


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
                "rl":      relay_controller.get_all(),
                "pir":     pir_manager.get_all_states(),
                "ldr":     ldr_monitor.ambient_percent,
                "err":     system_status.error_count,
                "rssi":    lora_protocol.last_rx_rssi,
            }
            lora_protocol.send_heartbeat(payload)
            # Flash the LED on the actual send event (not on a periodic
            # timer). Blue = lora config OK at boot; red = config failed.
            r, g, b = _hb_flash_rgb()
            status_led.flash_event(r, g, b)
        except Exception as e:
            log.error(f"[HB] Broadcast error: {e}")
            system_status.record_error(f"hb: {e}")
        await asyncio.sleep(interval_s)


async def event_forward_task(min_level="WARN", interval_s=2, max_per_tick=3):
    """Leaf task: drain the local event bus, forward only WARN+ entries to the
    coordinator as ERR packets. Rate-limited so a fault loop on the leaf can't
    flood the LoRa band — at most `max_per_tick` events per `interval_s`, and
    we keep a small dedupe window so repeated identical lines coalesce.

    Why this design:
      * Each leaf decides what's worth forwarding (severity filter).
      * The bus is the single source of truth — Logger already populates it.
      * The forwarder is a passive subscriber; no log-call-site changes."""
    last_seq = event_bus.stats()["last_seq"]
    last_msgs = []     # short ring of recent forwarded msgs for dedupe
    _DEDUPE = 6        # how many recent lines to compare against
    while True:
        try:
            evts = event_bus.events_since(last_seq, level=min_level, limit=max_per_tick)
            for evt in evts:
                msg = evt["msg"]
                if msg in last_msgs:
                    last_seq = evt["seq"]
                    continue
                # Truncate to leave room for envelope + lvl/ts/seq fields.
                # ERR fitter (registered below) will also trim if needed.
                if len(msg) > 140:
                    msg = msg[:140]
                lora_protocol.send_error(
                    evt["level"], msg, ts=evt["ts"], src_seq=evt["seq"]
                )
                last_msgs.append(msg)
                if len(last_msgs) > _DEDUPE:
                    last_msgs.pop(0)
                last_seq = evt["seq"]
        except Exception as e:
            # Forwarder must never tank the leaf — fall through quietly.
            print("[EVT_FWD] error:", e)
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
            # Visible "HB arrived" pulse — blue if our LoRa config came up
            # clean at boot, red if it didn't. Per-event flash, not a
            # periodic timer.
            r, g, b = _hb_flash_rgb()
            status_led.flash_event(r, g, b)
        lora_protocol.on("HB", on_heartbeat)

        def on_status_response(src, payload):
            fleet_manager.update(src, payload)
        lora_protocol.on("SRP", on_status_response)

        # ERR from a leaf → push into the coordinator's event bus so the
        # dashboard's Logs view and notification bell see leaf-side failures
        # alongside coordinator-local activity. Per-leaf dedupe by src_seq
        # avoids re-pushing the same event if the leaf retried mid-flight.
        _seen_err = {}   # {leaf_id: set of recently-seen src_seq}
        def on_remote_error(src, payload):
            try:
                level = payload.get("lvl", "ERROR")
                msg   = payload.get("msg", "")
                ts    = payload.get("ts")
                sq    = payload.get("sq")
                if sq is not None:
                    seen = _seen_err.get(src)
                    if seen is None:
                        seen = []
                        _seen_err[src] = seen
                    if sq in seen:
                        return
                    seen.append(sq)
                    if len(seen) > 16:
                        seen.pop(0)
                event_bus.push(level, msg, src=src, tag="leaf", ts=ts)
            except Exception:
                pass
        lora_protocol.on("ERR", on_remote_error)

    # Fitter for ERR: trim `msg` if the envelope would overflow. Drops about
    # 8 B at a time until it fits, leaving level/ts/sq intact for correlation.
    def _fit_err(payload, budget):
        import json as _json
        while len(_json.dumps(payload).encode()) > budget:
            msg = payload.get("msg", "")
            if len(msg) <= 8:
                break
            payload["msg"] = msg[:-8]
        return payload
    lora_protocol.fitter("ERR", _fit_err)

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
        # Wire format: ch and rl are lists of [int_id, value] pairs.
        channels = payload.get("ch", [])
        relays   = payload.get("rl", [])
        revert_s = payload.get("revert_s", 0)
        fade_ms  = payload.get("fade_ms", 0)
        if revert_s == -1:
            priority_arbiter.clear_all_manual()
        else:
            for item in channels:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    priority_arbiter.set_manual_channel(item[0], item[1], fade_ms, revert_s)
            for item in relays:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    priority_arbiter.set_manual_relay(item[0], item[1], revert_s)
        status_led.set_state("manual_override" if priority_arbiter.has_manual() else _ok_led_state())
    lora_protocol.on("MO", on_manual_override)

    def on_status_request(src, payload):
        response = {
            "name":    config_manager.unit_name,
            "uptime":  system_status.get_uptime(),
            "ch":      pwm_controller.get_all(),
            "rl":      relay_controller.get_all(),
            "pir":     pir_manager.get_all_states(),
            "ldr":     ldr_monitor.ambient_percent,
            "err":     system_status.error_count,
            "rssi":    lora_protocol.last_rx_rssi,
            "sc":      list(scenes.keys()),
        }
        # Size fitting is handled by the registered SRP fitter below.
        lora_protocol.send("SRP", src, response)
    lora_protocol.on("SR", on_status_request)

    # Fitter: SRP is ~120B baseline; "sc" is the only growth field. Drop scene
    # names from the end until the JSON-encoded payload fits the budget.
    def _fit_srp(payload, budget):
        import json as _json
        sc = payload.get("sc")
        if not isinstance(sc, list):
            return payload
        while len(_json.dumps(payload).encode()) > budget and sc:
            sc.pop()
        return payload
    lora_protocol.fitter("SRP", _fit_srp)

    def on_emergency_off(src, _payload):
        for ch in config_manager.get("led_channels"):
            priority_arbiter.set_manual_channel(ch["id"], 0, 0, 0)
        for r in config_manager.get("relays"):
            priority_arbiter.set_manual_relay(r["id"], "off", 0)
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
                        # CRC is good, but the config may still fail the leaf's
                        # validator or the atomic flash write. Apply FIRST so we
                        # only ACK ok=True after the new config is durably saved.
                        # Otherwise the coord would cache a config the leaf never
                        # actually applied — and the dashboard would show enabled
                        # channels with stale 0% duty on the leaf row.
                        apply_err = None
                        try:
                            config_manager.replace(config_str)
                        except Exception as e:
                            apply_err = e
                            log.error(f"[LORA] Config transfer {tid} apply failed: {e}")

                        if apply_err is None:
                            log.info(f"[LORA] Config transfer {tid} applied. Rebooting.")
                            if seq is not None:
                                lora_protocol.send("ACK", src, {"ack_seq": seq, "ok": True})
                            del _cfg_transfers[tid]
                            import machine
                            import asyncio
                            async def do_reboot():
                                # Give the ACK a moment to reach the coord before
                                # we pull the rug out from under the UART.
                                await asyncio.sleep(1)
                                machine.reset()
                            asyncio.create_task(do_reboot())
                            return

                        # Apply failed — tell the coord so it does NOT cache.
                        if seq is not None:
                            lora_protocol.send("ACK", src, {
                                "ack_seq": seq,
                                "ok": False,
                                "reason": "APPLY_FAILED",
                                "err": str(apply_err)[:80],
                            })
                        del _cfg_transfers[tid]
                        return
                    else:
                        # Smart retry on coord will ask for whichever chunks
                        # didn't decode. This is recoverable, not an error.
                        log.warn(f"[LORA] Config transfer {tid} checksum mismatch — coord will retry")
                else:
                    # Missing-chunks is a normal-life condition on a noisy
                    # channel; the coord's smart-retry path is designed
                    # exactly for this. Demoted from ERROR to WARN so it
                    # doesn't trip the bell badge on every config push.
                    log.warn(f"[LORA] Config transfer {tid} missing {total - len(chunks)} chunks — coord will resend")
                    
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

    # Deliberate half-second white flash so the operator sees a clear
    # "I just woke up" cue, regardless of how fast the rest of init runs.
    # Without this, the brief dim-white set_state("booting") frame gets
    # overwritten by lora_init (cyan) within ~30 ms — below the
    # threshold of perception. The flash uses run_pattern's flash_event
    # path; the explicit await lets it actually display before we move on.
    status_led.flash_event(*FLASH_BOOT_RGB, ms=500)
    await asyncio.sleep_ms(550)

    # --- Config ---
    if config_manager.safe_mode_reason:
        log.error(f"[MAIN] Config load failed: {config_manager.safe_mode_reason}")
        await safe_mode()
        return
    cfg = config_manager

    hw  = cfg.get("hardware")
    sys = cfg.get("system")
    role = cfg.role

    # Event bus: configure size from config.json and stamp src on every event
    # we emit locally. Done before any meaningful work so the Logs view shows
    # boot-time activity too.
    event_bus.set_size(sys.get("log_buffer_size", 100))
    event_bus.set_unit_id(cfg.unit_id)

    log.info(f"[MAIN] Lokki booting — role={role} unit_id={cfg.unit_id} name={cfg.unit_name}")

    # Re-bind status LED to configured pin (default singleton uses GPIO 5)
    status_led.init_from_config(hw)

    # --- LoRa init — FIRST so any failure can be retried via soft-reset
    # ---
    # The E220 module's register-write path is borderline-flaky after a
    # cold power-on; a Thonny soft-restart reliably gets it working.
    # Mimic that automatically: if apply_from_config fails inside
    # lora_transport.init(), increment a persistent counter and
    # machine.soft_reset() to retry. After 3 failed soft-resets, give
    # up and continue boot with LoRa disabled.
    lora_retry = _lora_retry_read()
    if lora_retry >= _LORA_MAX_RETRIES:
        log.error(f"[MAIN] LoRa init failed {lora_retry}× — proceeding without LoRa")
        status_led.set_state("lora_disabled")
        _lora_retry_clear()
        lora_ok = False
    else:
        if lora_retry > 0:
            log.warn(f"[MAIN] LoRa recovery boot — attempt {lora_retry + 1}/{_LORA_MAX_RETRIES}")
            status_led.set_state("lora_recovering")
        else:
            status_led.set_state("lora_init")
        try:
            lora_protocol.init()                    # calls lora_transport.init() → apply_from_config
            lora_ok = lora_transport.config_ok
        except Exception as e:
            log.error(f"[MAIN] LoRa init exception: {e}")
            lora_ok = False

        if not lora_ok:
            # Half-second red flash so the operator can see the failure
            # at a glance before the soft-reset blanks the LED. Then
            # flush log lines and reboot.
            status_led.flash_event(*FLASH_LORA_FAIL_RGB, ms=500)
            await asyncio.sleep_ms(550)             # let the flash actually display
            new_count = lora_retry + 1
            _lora_retry_write(new_count)
            log.warn(f"[MAIN] LoRa init failed — soft-resetting (attempt {new_count}/{_LORA_MAX_RETRIES})")
            _time.sleep_ms(500)                     # let log line flush over USB / event bus
            _machine.soft_reset()                   # does not return
        # success path: half-second blue flash to confirm, then clear
        # the retry counter and continue boot.
        status_led.flash_event(*FLASH_LORA_OK_RGB, ms=500)
        await asyncio.sleep_ms(550)
        _lora_retry_clear()
        log.info("[MAIN] LoRa init OK")

    system_status.set_connection_status(lora=lora_ok)

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

    # --- Fleet manager init (coordinator) ---
    fleet_mgr = None
    if role == "coordinator":
        from coordinator.fleet_manager import fleet_manager as fleet_mgr
        fleet_mgr.init()
        # Re-hydrate any leaf configs cached on flash from prior pushes.
        from coordinator.api_handlers import load_leaf_cache_from_flash
        load_leaf_cache_from_flash()

    # --- Register LoRa message handlers (only if transport is healthy) ---
    if lora_ok:
        _register_lora_handlers(role, fleet_mgr)
    else:
        log.warn("[MAIN] LoRa disabled — skipping protocol handler registration")

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
                # NTP sync — enabled by default, can be disabled in config
                tz_config = cfg.get("timezone") or {}
                ntp_enabled = tz_config.get("ntp_enabled", True)
                if ntp_enabled:
                    log.info("[MAIN] NTP enabled, attempting sync...")
                    try:
                        if sync_time_ntp():
                            log.info("[MAIN] NTP synced successfully")
                        else:
                            log.warn("[MAIN] NTP sync failed — continuing with RTC time")
                    except Exception as ntp_e:
                        log.warn(f"[MAIN] NTP sync exception: {ntp_e}")
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
    # Only start the LoRa listener if init actually succeeded. With LoRa
    # disabled the listen loop would spin on a transport that never
    # gets frames; cleaner to skip it entirely.
    if lora_ok:
        tasks.append(asyncio.create_task(lora_protocol.listen_task()))

    if i2c_sensors.has_sensors:
        tasks.append(asyncio.create_task(i2c_sensors.run()))

    if sys.get("log_level") == "DEBUG":
        tasks.append(asyncio.create_task(ram_telemetry_task(60)))

    hb_interval = cfg.get("lora").get("heartbeat_interval_s", 30)

    if role == "coordinator":
        tasks.append(asyncio.create_task(fleet_timeout_task(fleet_mgr)))
        if lora_ok:
            tasks.append(asyncio.create_task(time_sync_task()))
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
        # Leaf: broadcast heartbeats to coordinator. Skip the LoRa-
        # dependent tasks if init failed; leaf still runs schedule/PWM/
        # relays locally, just no fleet visibility.
        if lora_ok:
            tasks.append(asyncio.create_task(
                heartbeat_broadcast_task(hb_interval, cfg.unit_id)
            ))
            tasks.append(asyncio.create_task(event_forward_task()))
        tasks.append(asyncio.create_task(leaf_status_task()))

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
