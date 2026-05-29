import asyncio
import gc

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
from shared.system_status import system_status, time_is_sane
from shared.event_bus import event_bus

log = Logger()


# ------------------------------------------------------------------
# LoRa boot init: one attempt + deferred retries
# ------------------------------------------------------------------
# Field-tested behavior: the E220's register-write path is unreliable
# in the first ~30–60 seconds after power-on when the board is fed
# from an LM2596 buck (switching ripple + cap-charge transient + LDO
# warmup combine to corrupt the borderline-timing register exchange).
# Manual button presses after the board has been running for a while
# succeed on the first try, where rapid soft_reset retries do not.
#
# So: one attempt at boot. If it fails, we continue booting (LoRa
# tasks all start anyway — they're no-ops while the transport is
# not ready). A background task then sleeps 100 s and retries —
# silently, no LED noise — up to 3 times. If a deferred attempt
# succeeds, the running listen_task / heartbeat / event_forward
# tasks pick up real traffic automatically without re-registration.
# After 3 deferred failures we settle into lora_disabled and stop.
#
# No soft_reset, no persistent counter file, fast boot.
_LORA_DEFERRED_DELAY_S    = 100
_LORA_DEFERRED_MAX_TRIES  = 3


# ------------------------------------------------------------------
# PIR action executor
# Translates config on_motion / on_vacancy actions into arbiter calls
# ------------------------------------------------------------------

def _build_pir_handler(action_cfg, scenes_by_name, trigger_label):
    """Build the closure that fires when a PIR transitions in
    `trigger_label` direction (`"motion"` or `"vacancy"`). Logs at
    INFO every time the handler runs so operators can see exactly
    what was triggered and how it was acted on — previously these
    fired silently, making debugging "did the PIR even see motion?"
    a serial-console problem."""
    act = action_cfg.get("action", "revert_to_schedule")

    if act == "revert_to_schedule":
        def handler(pir_id):
            log.info(f"[PIR] pir{pir_id} {trigger_label} → revert_to_schedule")
            priority_arbiter.clear_all_pir()
        return handler

    if act == "set_scene":
        scene_name = action_cfg.get("scene_name", "")
        def handler(pir_id):
            scene = scenes_by_name.get(scene_name)
            if scene:
                log.info(f"[PIR] pir{pir_id} {trigger_label} → set_scene('{scene_name}')")
                priority_arbiter.apply_scene(scene)
            else:
                log.warn(
                    f"[PIR] pir{pir_id} {trigger_label} → set_scene('{scene_name}'): "
                    f"scene not found in config (have: {list(scenes_by_name.keys())})"
                )
        return handler

    if act == "set_led_channels":
        channels = action_cfg.get("channels", [])
        duty = action_cfg.get("duty_percent", 100)
        fade = action_cfg.get("fade_ms", 0)
        def handler(pir_id):
            log.info(
                f"[PIR] pir{pir_id} {trigger_label} → set_led_channels "
                f"channels={channels} duty={duty}% fade={fade}ms"
            )
            for cid in channels:
                priority_arbiter.set_pir_channel(cid, duty, fade_ms=fade)
        return handler

    if act == "set_relay":
        relay_id = action_cfg.get("relay_id")
        state = action_cfg.get("state", "on")
        def handler(pir_id):
            if relay_id is None:
                log.warn(f"[PIR] pir{pir_id} {trigger_label} → set_relay: relay_id missing in config")
                return
            log.info(f"[PIR] pir{pir_id} {trigger_label} → set_relay({relay_id}={state})")
            priority_arbiter.set_pir_relay(relay_id, state)
        return handler

    def _noop(pir_id):
        log.warn(f"[PIR] pir{pir_id} {trigger_label} → unknown action '{act}', ignored")
    return _noop


def _setup_pir_handlers(pir_cfg, scenes):
    scenes_by_name = {s["name"]: s for s in scenes}
    registered = 0
    for p in pir_cfg:
        if not p.get("enabled", False):
            continue
        pid = p["id"]
        on_motion_cfg  = p.get("on_motion",  {}) or {}
        on_vacancy_cfg = p.get("on_vacancy", {}) or {}
        on_motion_handler  = _build_pir_handler(on_motion_cfg,  scenes_by_name, "motion")
        on_vacancy_handler = _build_pir_handler(on_vacancy_cfg, scenes_by_name, "vacancy")
        pir_manager.on_motion(pid, on_motion_handler)
        pir_manager.on_vacancy(pid, on_vacancy_handler)
        log.info(
            f"[PIR] pir{pid} '{p.get('name', '')}' handlers wired: "
            f"on_motion={on_motion_cfg.get('action', 'revert_to_schedule')}, "
            f"on_vacancy={on_vacancy_cfg.get('action', 'revert_to_schedule')}"
        )
        registered += 1
    if registered == 0 and pir_cfg:
        log.info(f"[PIR] {len(pir_cfg)} PIR entries in config — none enabled, no handlers wired")


# ------------------------------------------------------------------
# Async tasks
# ------------------------------------------------------------------

def _try_seed_time_from_rtc():
    """Boot-time helper: if the DS3231 has retained a sane wall-clock
    across power-off (battery still good), copy it into the MCU's
    internal RTC so time.time()/time.localtime() return real values
    immediately. Marks system_status.time_synced on success.

    Returns True iff we successfully seeded a sane time.
    """
    try:
        from hardware.rtc_shared import rtc as _rtc
        dt = _rtc.datetime()
    except Exception as e:
        log.warn(f"[TIME] DS3231 read at boot failed: {e}")
        return False
    if dt.year < 2024:
        # DS3231 lost its time (flat battery / first power-on of a new
        # board). Don't seed; let NTP or the LoRa TS broadcast handle
        # this boot.
        log.warn(f"[TIME] DS3231 year={dt.year} — backup battery flat or first boot; awaiting NTP/TS")
        return False
    try:
        import machine
        # MicroPython's machine.RTC().datetime() takes
        # (year, month, day, weekday, hour, minute, second, subsec).
        # Note the weekday slot is in a different position than the
        # tuple urtc returns from DS3231.datetime().
        machine.RTC().datetime((dt.year, dt.month, dt.day, dt.weekday,
                                dt.hour, dt.minute, dt.second, 0))
        if time_is_sane():
            system_status.mark_time_synced("rtc")
            log.info(f"[TIME] Seeded MCU clock from DS3231: {dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
            return True
    except Exception as e:
        log.warn(f"[TIME] Could not seed MCU clock from DS3231: {e}")
    return False


def _ok_led_state():
    """Return the appropriate steady LED state depending on LoRa connectivity.

    Time sync takes precedence: if we haven't confirmed a real wall-clock
    yet, the LED stays on `time_waiting` (slow cyan pulse) so the operator
    sees that the schedule is paused. Otherwise fall back to the normal
    green-solid running state.
    """
    if not system_status.time_synced:
        return "time_waiting"
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
    """Coordinator task: Sync NTP and broadcast time to leaves periodically.

    Cadence depends on whether we currently have time:
      * unsynced → retry NTP every 60 s. Schedule + TS broadcast are
        gated on having a real clock, so we want to recover quickly.
      * synced   → daily resync (24 h) is plenty for keeping drift
        below the schedule engine's minute-level resolution.

    TS broadcast (coord → leaves) is suppressed while we have no time
    ourselves — broadcasting a bogus epoch would only spread the
    problem to every leaf.
    """
    import time
    while True:
        tz_config = config_manager.get("timezone") or {}
        if tz_config.get("ntp_enabled", True):
            try:
                from comms.wifi_connect import sync_time_ntp
                if sync_time_ntp():
                    log.info("[MAIN] Periodic NTP sync successful")
                    if time_is_sane():
                        system_status.mark_time_synced("ntp")
                else:
                    log.warn("[MAIN] Periodic NTP sync failed")
            except Exception as e:
                log.warn(f"[MAIN] NTP sync exception: {e}")
        # Belt-and-suspenders: if some upstream side effect (DS3231
        # write, TS broadcast) had previously raised AFTER NTP set the
        # MCU clock — masking a real sync as a failure — observing a
        # sane wall-clock here is enough to flip the gate. The
        # explicit mark above is the happy path; this is the safety
        # net for partial-failure paths we haven't anticipated.
        if not system_status.time_synced and time_is_sane():
            system_status.mark_time_synced("ntp")
            log.info("[MAIN] Wall-clock looks sane; unblocking schedule")
        if system_status.time_synced:
            try:
                tz_offset = tz_config.get("utc_offset_hours", 0)
                lora_protocol.broadcast_time_sync(time.time(), tz_offset)
            except Exception as e:
                log.warn(f"[MAIN] TS broadcast failed: {e}")
        # Cadence: 60 s while we're still chasing a wall-clock; 1 h
        # once we have one. The 1 h rate (down from the original 24 h)
        # bounds the worst-case recovery time for a leaf that misses
        # a broadcast — TS is tiny (~50 B) so airtime cost is
        # negligible at this rate, and 1 h is short enough that any
        # leaf rejoining the fleet mid-day catches up before the next
        # day. Leaves that can't wait that long send TS_REQ.
        await asyncio.sleep(60 if not system_status.time_synced else 3600)


async def schedule_task(interval_ms):
    """Drive scheduled output state.

    Gated behind system_status.time_synced — until we've confirmed a
    real wall-clock time (from NTP on coord, DS3231 sane on either
    role, LoRa TS on leaf, or operator override), this skips its tick
    entirely. Otherwise the schedule engine would happily decide that
    11:42 on Jan 1 2000 means "all night windows active" and drive
    outputs incorrectly until time arrives. The arbiter falls back to
    each channel's default_state when nothing populates the schedule
    layer, which is the right safe behaviour.
    """
    while True:
        if system_status.time_synced:
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


async def lora_deferred_retry_task():
    """If the boot-time LoRa init failed, sit quiet for a while then
    try again. Field observation: an LM2596-powered E220 needs ~60–100
    seconds of being powered before its register-write path becomes
    reliable. Rapid soft_reset retries never give the supply enough
    time to settle; a single deferred attempt usually does.

    Short-circuits immediately if the boot init already worked.
    Otherwise sleeps _LORA_DEFERRED_DELAY_S between attempts, up to
    _LORA_DEFERRED_MAX_TRIES. On success, registered LoRa handlers and
    the already-running listen_task / heartbeat_broadcast_task / etc.
    pick up real traffic without any re-wiring needed."""
    if lora_transport.config_ok:
        return                                # boot worked; nothing to do

    for attempt in range(1, _LORA_DEFERRED_MAX_TRIES + 1):
        await asyncio.sleep(_LORA_DEFERRED_DELAY_S)
        if lora_transport.config_ok:
            return                            # something else fixed it; bail
        log.warn(f"[MAIN] Deferred LoRa init: attempt {attempt}/{_LORA_DEFERRED_MAX_TRIES}")
        try:
            lora_protocol.init()              # re-runs lora_transport.init() → apply_from_config
        except Exception as e:
            log.error(f"[MAIN] Deferred LoRa init exception: {e}")

        if lora_transport.config_ok:
            log.info(f"[MAIN] LoRa came up on deferred attempt {attempt}")
            system_status.set_connection_status(lora=True)
            status_led.flash_event(*FLASH_LORA_OK_RGB, brightness=0.9, ms=500)
            await asyncio.sleep_ms(550)
            status_led.set_state(_ok_led_state())
            # Catch-up TS broadcast (coord only). Without this, a coord
            # whose boot-time LoRa init failed but whose NTP succeeded
            # would have its TS broadcast suppressed at the gate (see
            # lora_protocol.send), and leaves would stay in
            # time_waiting until the next periodic broadcast — up to
            # an hour away. Firing a TS right now means any leaf
            # that's been waiting unblocks within a few seconds of
            # the coord's radio coming online.
            if config_manager.role == "coordinator" and system_status.time_synced:
                try:
                    import time as _t
                    tz_offset = (config_manager.get("timezone") or {}).get("utc_offset_hours", 0)
                    lora_protocol.broadcast_time_sync(_t.time(), tz_offset)
                    log.info("[MAIN] Catch-up TS broadcast (LoRa just came up)")
                except Exception as e:
                    log.warn(f"[MAIN] Catch-up TS broadcast failed: {e}")
            return

    log.error(f"[MAIN] LoRa init failed after {_LORA_DEFERRED_MAX_TRIES} deferred "
              f"attempts — accepting lora_disabled for this boot")
    status_led.set_state("lora_disabled")


# --- Stable per-device identity for HB/SRP payloads ---
# The last 4 bytes of machine.unique_id() as 8-char hex. Included in
# every HB and SRP so the coordinator can disambiguate multiple
# unclaimed leaves (all at unit_id=99) — claim wizard targets a
# specific UID, leaves with other UIDs ignore the claim. Also handy
# for diagnostics on claimed leaves: the dashboard can show "Leaf 1
# (chip ABCD1234)" so a physical-to-virtual mapping is always clear.
_CHIP_UID_HEX = None

def _chip_uid_hex():
    global _CHIP_UID_HEX
    if _CHIP_UID_HEX is None:
        import machine
        _CHIP_UID_HEX = "".join("{:02X}".format(b) for b in machine.unique_id()[-4:])
    return _CHIP_UID_HEX


def _hb_flash_rgb():
    """Pick the heartbeat-flash color from the boot-time LoRa config
    outcome. Blue when the volatile-register write succeeded; red when
    it didn't (the module is running in whatever NVRAM state it had,
    which is probably wrong and the operator should know)."""
    from hardware.status_led import FLASH_LORA_OK_RGB, FLASH_LORA_FAIL_RGB
    from comms.lora_transport import lora_transport as _lt
    return FLASH_LORA_OK_RGB if getattr(_lt, "config_ok", False) else FLASH_LORA_FAIL_RGB


async def heartbeat_broadcast_task(interval_s, unit_id):
    """Leaf task: send HB to coordinator at regular intervals with jitter.

    Wire format note — keys here are *short* to keep HB under the 200 B
    LoRa packet limit even with a long unit_name. They are deliberately
    NOT the same as the internal/API keys that fleet_manager stores and
    the dashboard reads; fleet_manager._fill maps wire→internal. Short
    key inventory:
      n    unit_name            (was "name")
      up   uptime seconds       (was "uptime")
      ch   channel duty array
      rl   relay state array
      pir  PIR state array
      ldr  ambient %            (optional, dropped by fitter if needed)
      r    last-RX RSSI         (was "rssi", optional)
      tc   chip temperature °C  (was "rtc_t", optional)
      uid  chip UID             (ONLY when unit_id==99 / unclaimed)
      err  error count          (ONLY when non-zero)
    """
    jitter_ms = unit_id * 500
    await asyncio.sleep_ms(jitter_ms)
    uid = _chip_uid_hex()
    is_unclaimed = (unit_id == 99)
    from hardware.rtc_module import get_rtc_temp_c
    while True:
        try:
            t = get_rtc_temp_c()
            err = system_status.error_count
            # Read unit_name each iteration so a hot-applied
            # system.unit_name patch shows up on the next HB without
            # waiting for a reboot. The lookup is a dict get — cheap.
            payload = {
                "n":       config_manager.unit_name,
                "up":      system_status.get_uptime(),
                "ch":      pwm_controller.get_all(),
                "rl":      relay_controller.get_all(),
                "pir":     pir_manager.get_all_states(),
                "ldr":     ldr_monitor.ambient_percent,
                "r":       lora_protocol.last_rx_rssi,
            }
            # uid is only meaningful on the wire for unclaimed leaves
            # (claim-wizard target_uid disambiguation). On a claimed
            # leaf, the unit_id in the envelope already identifies the
            # device — sending uid every HB just wastes ~18 B.
            if is_unclaimed:
                payload["uid"] = uid
            # Only carry err when there's actually been an error.
            # fleet_manager._fill treats absence as 0.
            if err:
                payload["err"] = err
            if t is not None:
                # Round to 0.1 °C to save bytes on the wire — the
                # underlying sensor is 0.25 °C resolution anyway.
                payload["tc"] = round(t, 1)
            lora_protocol.send_heartbeat(payload)
            # Flash the LED on the actual send event (not on a periodic
            # timer). Blue = lora config OK at boot; red = config failed.
            r, g, b = _hb_flash_rgb()
            status_led.flash_event(r, g, b)
        except Exception as e:
            log.error(f"[HB] Broadcast error: {e}")
            system_status.record_error(f"hb: {e}")
        await asyncio.sleep(interval_s)


async def time_sync_request_task():
    """Leaf-only: if no TS broadcast arrives in the first ~90 s after
    boot, proactively ask the coord for one (TS_REQ). Retries every
    60 s up to 5 times, then gives up and lets the next periodic TS
    broadcast (~1 h cadence) handle it. Exits immediately if
    system_status.time_synced flips True at any point.

    The 90 s initial delay matches the LoRa-deferred-retry window —
    if our own LoRa came up slowly, we don't want to spam TS_REQ
    before our radio is even configured. The 60 s inter-retry gap
    is short enough that an operator setting up a new fleet sees
    leaves come out of time_waiting quickly, long enough that we
    don't burn airtime if the coord itself is having issues.

    No-op when called on the coord — the task isn't added to the
    coord's task list (see main()), so this lives unguarded here
    for clarity."""
    await asyncio.sleep(90)
    for attempt in range(1, 6):
        if system_status.time_synced:
            return
        if not lora_transport.config_ok:
            # No point asking — our radio isn't talking yet. Wait
            # for the deferred-retry task to bring LoRa up, then
            # try again on the next iteration.
            await asyncio.sleep(60)
            continue
        log.info(f"[TIME] Requesting TS from coord (attempt {attempt}/5)")
        lora_protocol.request_time_sync(dest=0)
        await asyncio.sleep(60)
    if not system_status.time_synced:
        log.warn("[TIME] TS_REQ attempts exhausted — relying on next periodic TS broadcast")


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


async def lora_status_flash_task(interval_s=10):
    """Coordinator task: periodically flash the WS2812 to surface LoRa
    health. Leaves get this for free via the HB-send path
    (heartbeat_broadcast_task fires flash_event() on every TX, every
    ~30 s). Coord doesn't transmit HBs — it only flashes on receive,
    which means a coord sitting in a fleet with no online leaves has
    no visible heartbeat at all. This task adds one: blue flash if
    LoRa is up, red flash if not, every interval_s seconds.

    Stays passive — uses flash_event(), so the base state (running_ok
    green / leaf_offline orange / lora_disabled purple / manual_override
    magenta) keeps showing between flashes. The flash itself is the
    'I'm alive' cue."""
    while True:
        await asyncio.sleep(interval_s)
        if system_status.lora_connected:
            r, g, b = FLASH_LORA_OK_RGB
        else:
            r, g, b = FLASH_LORA_FAIL_RGB
        status_led.flash_event(r, g, b)


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

        # PIR_EV: leaf reports a motion/vacancy transition the moment
        # it happens. The dashboard's `pir` array is also updated via
        # the periodic HB (every ~30 s), but events lag that
        # cadence — pushing PIR_EV into the event bus + bumping
        # fleet_manager's per-leaf pir array gives near-realtime
        # feedback on the Motion (PIR) column of the fleet view.
        def on_pir_event(src, payload):
            pid = payload.get("id")
            st  = payload.get("state")     # "motion" | "vacancy"
            if not isinstance(pid, int) or pid < 1 or pid > 4:
                log.warn(f"[LORA] PIR_EV from {src} ignored: bad id={pid!r}")
                return
            # Update fleet_manager's cached pir array so the dashboard
            # sees the transition immediately on its next /api/fleet
            # poll (rather than waiting for the next HB to refresh
            # the array).
            u = fleet_manager.get(src)
            if u is not None and isinstance(u.get("pir"), list) and 1 <= pid <= 4:
                u["pir"][pid - 1] = 1 if st == "motion" else 0
            # Also surface as a tagged event so the Logs view shows
            # the activity stream of motion events across the fleet.
            event_bus.push(
                "INFO",
                f"[PIR] pir{pid} {st}",
                src=src, tag="pir",
            )
            log.info(f"[LORA] PIR_EV from unit {src}: pir{pid} → {st}")
        lora_protocol.on("PIR", on_pir_event)

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
        # Leaf only — apply the coordinator's TS broadcast to both the
        # DS3231 (battery-backed, survives power loss) AND the MCU's
        # internal clock (what time.time()/time.localtime() actually
        # read). Previously we only wrote the DS3231, so a leaf with a
        # dead RTC battery would never get a sane time.time() until
        # the rtc_module fallback path ran — and the schedule was
        # already paused waiting for that.
        epoch = payload.get("epoch")
        if epoch and role == "leaf":
            try:
                from hardware import urtc
                tz = payload.get("tz", 0)
                local_sec = int(epoch) + int(tz * 3600)
                dt = urtc.seconds2tuple(local_sec)
                rtc.datetime(dt)
                try:
                    import machine
                    machine.RTC().datetime((dt.year, dt.month, dt.day,
                                            dt.weekday, dt.hour,
                                            dt.minute, dt.second, 0))
                except Exception as me:
                    log.warn(f"[LORA] Could not set MCU clock from TS: {me}")
                if time_is_sane():
                    system_status.mark_time_synced("ts")
                log.info(f"[LORA] Time synced from coordinator: {dt}")
            except Exception as e:
                log.warn(f"[LORA] Time sync apply failed: {e}")
    lora_protocol.on("TS", on_time_sync)

    if role == "coordinator":
        def on_time_sync_request(src, payload):
            """A leaf is asking for a TS broadcast — usually because it
            booted without a wall-clock (dead DS3231 battery, no
            previous TS heard) and is sitting in time_waiting state.
            Honour the request only if WE ourselves have synced time;
            otherwise we'd be broadcasting a bogus epoch (boot uptime
            ≈ Jan 2000) which would corrupt every leaf that was
            actually OK. The leaf-side request task retries on a
            cadence, so silently dropping is the right move."""
            if not system_status.time_synced:
                log.warn(f"[LORA] TS_REQ from {src} ignored — coord not synced yet")
                return
            try:
                import time
                tz_offset = (config_manager.get("timezone") or {}).get("utc_offset_hours", 0)
                lora_protocol.broadcast_time_sync(time.time(), tz_offset)
                log.info(f"[LORA] TS_REQ from {src} — broadcast TS")
            except Exception as e:
                log.warn(f"[LORA] TS_REQ from {src} — broadcast failed: {e}")
        lora_protocol.on("TS_REQ", on_time_sync_request)

    def on_scene(src, payload):
        # Read scenes from config_manager each call (rather than the
        # closure-captured `scenes` dict above) so a hot-applied
        # `scenes` patch takes effect on the next SC without needing
        # a reboot. SC fires only on operator action so the lookup
        # cost is irrelevant.
        scene_name = payload.get("scene")
        cur_scenes = {s["name"]: s for s in config_manager.get("scenes") if isinstance(s, dict)}
        scene = cur_scenes.get(scene_name)
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
        # SRP shares the HB wire-key scheme (short keys, optional uid/
        # err) plus its own `sc` (scene names list). See the comment
        # in heartbeat_broadcast_task for the wire→internal mapping.
        from hardware.rtc_module import get_rtc_temp_c
        t = get_rtc_temp_c()
        err = system_status.error_count
        response = {
            "n":       config_manager.unit_name,
            "up":      system_status.get_uptime(),
            "ch":      pwm_controller.get_all(),
            "rl":      relay_controller.get_all(),
            "pir":     pir_manager.get_all_states(),
            "ldr":     ldr_monitor.ambient_percent,
            "r":       lora_protocol.last_rx_rssi,
            "sc":      list(scenes.keys()),
        }
        if config_manager.unit_id == 99:
            response["uid"] = _chip_uid_hex()
        if err:
            response["err"] = err
        if t is not None:
            response["tc"] = round(t, 1)
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

    # Fitter: HB baseline is ~110 B after the short-key rename; a long
    # unit_name plus all diagnostic fields can still push it past 200 B.
    # Drop optional diagnostic fields first (short-key names: tc, r, ldr),
    # then truncate the name as a last resort. Losing a few chars of
    # display name is better than dropping the whole heartbeat at the
    # protocol layer and never reaching the coordinator.
    def _fit_hb(payload, budget):
        import json as _json
        # Order matters: shed cheapest losses first.
        for key in ("tc", "r", "ldr"):
            if len(_json.dumps(payload).encode()) <= budget:
                break
            payload.pop(key, None)
        # Truncate name last. Keep at least 4 chars so the coord can
        # still display something recognisable.
        while len(_json.dumps(payload).encode()) > budget:
            n = payload.get("n", "")
            if len(n) <= 4:
                break
            payload["n"] = n[:-1]
        return payload
    lora_protocol.fitter("HB", _fit_hb)

    def on_emergency_off(src, _payload):
        for ch in config_manager.get("led_channels"):
            priority_arbiter.set_manual_channel(ch["id"], 0, 0, 0)
        for r in config_manager.get("relays"):
            priority_arbiter.set_manual_relay(r["id"], "off", 0)
        status_led.set_state("manual_override")
        log.info(f"[LORA] Emergency off from {src}")
    lora_protocol.on("EO", on_emergency_off)

    def on_reboot_request(src, _payload):
        """Coord → Leaf reboot. Scheduled via the existing dispatcher
        auto-ACK path (RB is in _ACK_REQUIRED so the lora_protocol
        layer ACKs before this handler runs); we then sleep briefly
        to let the ACK propagate to the coord before the radio goes
        silent, and reset. Registered for both roles so a coord can
        also be rebooted via RB from another coord in a multi-coord
        deployment (Phase 6 idea), but in practice today only leaves
        receive RB."""
        log.info(f"[LORA] Reboot requested from {src}")
        import machine, asyncio
        async def do_reboot():
            await asyncio.sleep(1)
            machine.reset()
        asyncio.create_task(do_reboot())
    lora_protocol.on("RB", on_reboot_request)

    if role == "leaf":
        import time
        _cfg_transfers = {}

        # Claim wizard "flash to identify" — coord broadcasts BLINK with a
        # target_uid; only the leaf whose chip UID matches lights up.
        # 3 s magenta flash so the operator can spot which board on the
        # bench is the one they're about to claim from the dashboard.
        def on_blink(src, payload):
            target = payload.get("target_uid")
            if target and target != _chip_uid_hex():
                return
            from hardware.status_led import COLOR_MAGENTA
            status_led.flash_event(*COLOR_MAGENTA, brightness=0.6, ms=3000)
            log.info(f"[LORA] BLINK from {src} (target_uid={target})")
        lora_protocol.on("BLINK", on_blink)

        def on_cfg_patch(src, payload):
            """Apply a single-field config patch. Single LoRa packet
            (no chunking), payload `{path, value}`. Validates the
            merged config against the schema BEFORE applying; rolls
            back on failure. ACKs with `ok: True, rebooted: <bool>`
            on success or `ok: False, reason: ..., err: ...` on
            rejection.

            Hot-apply path: for changes that don't touch boot-wired
            things (LoRa registers, GPIO pins, role/unit_id, etc. —
            see core.hot_apply.requires_reboot for the full list),
            re-runs the affected subsystems' init_from_config without
            a reboot. ~50 ms wallclock instead of ~30 s. For
            boot-critical paths the reboot path is unchanged.
            """
            seq = payload.get("_seq")
            path = payload.get("path")
            if not isinstance(path, str) or not path:
                if seq is not None:
                    lora_protocol.send("ACK", src, {
                        "ack_seq": seq, "ok": False,
                        "reason": "BAD_PATCH",
                        "err": "missing or non-string path",
                    })
                return
            # `value` may legitimately be None (delete-like) — distinguish
            # via key presence rather than truthiness.
            if "value" not in payload:
                if seq is not None:
                    lora_protocol.send("ACK", src, {
                        "ack_seq": seq, "ok": False,
                        "reason": "BAD_PATCH",
                        "err": "missing value key",
                    })
                return
            value = payload["value"]

            # Capture the BEFORE value at this path so the
            # requires_reboot check can compare field-by-field on
            # section/index patches.
            from core import json_path, hot_apply
            ok_get, old_value = json_path.get_at(config_manager.get_all(), path)
            if not ok_get:
                old_value = None

            try:
                import json as _json
                # Deep-copy via round-trip — avoids mutating the live
                # config until we know validation passes. On the Pico
                # this costs a few KB transient heap, accepted for
                # correctness.
                candidate = _json.loads(_json.dumps(config_manager.get_all()))
                ok, err = json_path.set_at(candidate, path, value)
                if not ok:
                    raise ValueError(err)
                config_manager.replace(_json.dumps(candidate))
            except Exception as e:
                log.error(f"[LORA] CFG_PATCH {path} apply failed: {e}")
                if seq is not None:
                    lora_protocol.send("ACK", src, {
                        "ack_seq": seq, "ok": False,
                        "reason": "APPLY_FAILED",
                        "err": str(e)[:80],
                    })
                return

            # Decide: hot-apply or reboot?
            needs_reboot = hot_apply.requires_reboot(path, old_value, value)
            if not needs_reboot:
                try:
                    hot_apply.apply_changes(path, config_manager.get_all())
                    log.info(f"[LORA] CFG_PATCH {path}={value!r} applied (hot, no reboot)")
                    if seq is not None:
                        lora_protocol.send("ACK", src, {
                            "ack_seq": seq, "ok": True, "rebooted": False
                        })
                    return
                except Exception as e:
                    # Subsystem re-init failed mid-flight. Config is
                    # durably saved, so rebooting recovers to a
                    # consistent state. Log loudly and fall through
                    # to the reboot path.
                    log.error(f"[LORA] CFG_PATCH {path} hot-apply failed: {e}; rebooting to recover")

            log.info(f"[LORA] CFG_PATCH {path}={value!r} applied. Rebooting.")
            if seq is not None:
                lora_protocol.send("ACK", src, {
                    "ack_seq": seq, "ok": True, "rebooted": True
                })
            import machine, asyncio
            async def do_reboot():
                await asyncio.sleep(1)
                machine.reset()
            asyncio.create_task(do_reboot())
        lora_protocol.on("CFG_PATCH", on_cfg_patch)

        def on_cfg_start(src, payload):
            tid = payload.get("transfer_id")
            # If CFG_START carries a target_uid, only the matching leaf
            # accepts the transfer — protects against multiple unclaimed
            # leaves on unit_id=99 all swallowing the same config.
            target = payload.get("target_uid")
            if target and target != _chip_uid_hex():
                return
            if tid:
                # target_path: if present, the assembled config string
                # is parsed as JSON and SET at that path in the
                # current config (incremental update). If absent, the
                # assembled string is the entire new config (existing
                # behaviour). Stored on the transfer state and read
                # by on_cfg_end at apply time.
                _cfg_transfers[tid] = {
                    "chunks":      {},
                    "total":       payload.get("total_chunks", 0),
                    "last":        time.time(),
                    "target_path": payload.get("target_path"),
                }
                log.info(f"[LORA] Started config transfer {tid} from {src}"
                         + (f" (target_uid={target})" if target else "")
                         + (f" (target_path={payload.get('target_path')})" if payload.get("target_path") else ""))
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
                        target_path = transfer.get("target_path")
                        old_value_at_path = None   # for target_path patches
                        try:
                            if target_path:
                                # Incremental: parse the assembled blob as
                                # the value to set at target_path on the
                                # current config. Snapshot the old value
                                # so hot_apply.requires_reboot can do its
                                # field-by-field check on section/index
                                # patches.
                                import json as _json
                                from core import json_path
                                value = _json.loads(config_str)
                                ok_get, old_value_at_path = json_path.get_at(
                                    config_manager.get_all(), target_path
                                )
                                if not ok_get:
                                    old_value_at_path = None
                                candidate = _json.loads(_json.dumps(config_manager.get_all()))
                                ok, err = json_path.set_at(candidate, target_path, value)
                                if not ok:
                                    raise ValueError(f"path apply failed: {err}")
                                config_manager.replace(_json.dumps(candidate))
                            else:
                                config_manager.replace(config_str)
                        except Exception as e:
                            apply_err = e
                            log.error(f"[LORA] Config transfer {tid} apply failed: {e}")

                        if apply_err is None:
                            # For full-config push (no target_path),
                            # always reboot — the diff scope is the
                            # whole config and any one of dozens of
                            # boot-critical fields might have changed.
                            # For target_path patches, defer to
                            # hot_apply like CFG_PATCH does.
                            needs_reboot = True
                            if target_path:
                                from core import hot_apply
                                needs_reboot = hot_apply.requires_reboot(
                                    target_path, old_value_at_path,
                                    value if target_path else None,
                                )
                                if not needs_reboot:
                                    try:
                                        hot_apply.apply_changes(target_path, config_manager.get_all())
                                    except Exception as e:
                                        log.error(f"[LORA] Config transfer {tid} hot-apply failed: {e}; rebooting to recover")
                                        needs_reboot = True

                            if not needs_reboot:
                                log.info(f"[LORA] Config transfer {tid} applied (hot, no reboot)")
                                if seq is not None:
                                    lora_protocol.send("ACK", src, {
                                        "ack_seq": seq, "ok": True, "rebooted": False,
                                    })
                                del _cfg_transfers[tid]
                                return

                            log.info(f"[LORA] Config transfer {tid} applied. Rebooting.")
                            if seq is not None:
                                lora_protocol.send("ACK", src, {
                                    "ack_seq": seq, "ok": True, "rebooted": True,
                                })
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
                # Unknown transfer. If we're an unclaimed (id=99) leaf,
                # this almost certainly means the coord was targeting a
                # *different* board's chip UID — staying silent prevents
                # us from racing the real target's ACK back to the
                # coord. Claimed leaves still emit UNKNOWN_TRANSFER so
                # genuine stale CFG_ENDs fast-fail at the coord.
                if seq is not None and config_manager.unit_id != 99:
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

    # Event bus: configure size from config.json and stamp src on every event
    # we emit locally. Done before any meaningful work so the Logs view shows
    # boot-time activity too.
    event_bus.set_size(sys.get("log_buffer_size", 100))
    event_bus.set_unit_id(cfg.unit_id)

    log.info(f"[MAIN] Lokki booting — role={role} unit_id={cfg.unit_id} name={cfg.unit_name}")

    # Try to load wall-clock time from the DS3231 immediately. If the
    # backup battery is still good, we'll mark time_synced here and
    # unblock the schedule before WiFi/NTP even starts. If the battery
    # is flat, this is a no-op — NTP (coord) or the LoRa TS broadcast
    # (leaf) brings time online later, and the schedule waits.
    _try_seed_time_from_rtc()

    # Re-bind status LED to configured pin + color order BEFORE the boot
    # flash, otherwise we'd flash with the default GRB ordering even on
    # boards configured as RGB. White is r=g=b so this doesn't matter
    # cosmetically today, but it's the correct order-of-operations and
    # protects us if FLASH_BOOT_RGB ever changes to a non-grayscale colour.
    status_led.init_from_config(hw)

    # Deliberate half-second white flash so the operator sees a clear
    # "I just woke up" cue, regardless of how fast the rest of init
    # runs. brightness=0.9 because the default 0.4 was hard to see in
    # ambient light; this one is meant to be unmissable.
    status_led.flash_event(*FLASH_BOOT_RGB, brightness=0.9, ms=500)
    await asyncio.sleep_ms(550)

    # --- LoRa init — one attempt at boot, deferred retry if it fails.
    # See the comment block at the top of this file. We don't soft_reset
    # on failure; instead, every LoRa-dependent task starts anyway and
    # no-ops while the transport is not ready. A background task wakes
    # 100 s later and re-attempts the register write, by which point
    # the LM2596 buck output has settled and the module's internal
    # state is reliable. Three deferred attempts; after that we accept
    # lora_disabled and stop.
    lora_explicitly_disabled = not cfg.get("lora").get("enabled", True)

    if lora_explicitly_disabled:
        log.warn("[MAIN] LoRa disabled by config (lora.enabled = false) — skipping init")
        status_led.set_state("lora_disabled")
        lora_ok = False
    else:
        status_led.set_state("lora_init")
        try:
            lora_protocol.init()                    # calls lora_transport.init() → apply_from_config
            lora_ok = lora_transport.config_ok
        except Exception as e:
            log.error(f"[MAIN] LoRa init exception: {e}")
            lora_ok = False

        if lora_ok:
            status_led.flash_event(*FLASH_LORA_OK_RGB, brightness=0.9, ms=500)
            await asyncio.sleep_ms(550)
            log.info("[MAIN] LoRa init OK")
        else:
            # Boot didn't get LoRa up. Flash red so the operator sees
            # it, set the recovering LED, and let the deferred-retry
            # task (started below in the task list) try again in 100 s.
            # Boot continues normally — no soft_reset, no blocking wait.
            status_led.flash_event(*FLASH_LORA_FAIL_RGB, brightness=0.9, ms=500)
            await asyncio.sleep_ms(550)
            status_led.set_state("lora_recovering")
            log.warn(f"[MAIN] LoRa init failed — will retry silently in "
                     f"{_LORA_DEFERRED_DELAY_S} s (up to {_LORA_DEFERRED_MAX_TRIES} times)")

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

    # --- Register LoRa message handlers ---
    # Always wire these, regardless of whether boot-time LoRa init
    # succeeded. The deferred-retry task may bring LoRa up later, and
    # we want handlers to be in place when that happens. They're
    # harmless if no frames ever arrive (lookup misses → no-op).
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
                # mDNS: handled entirely by lwIP's built-in responder.
                # network.hostname() in wifi_connect.connect_wifi() is
                # what tells lwIP what to advertise; the Python-side
                # fallback we used to ship was redundant on every build
                # we've tested and has been removed.
                # NTP sync — enabled by default, can be disabled in config
                tz_config = cfg.get("timezone") or {}
                ntp_enabled = tz_config.get("ntp_enabled", True)
                if ntp_enabled:
                    log.info("[MAIN] NTP enabled, attempting sync...")
                    try:
                        if sync_time_ntp():
                            log.info("[MAIN] NTP synced successfully")
                            if time_is_sane():
                                system_status.mark_time_synced("ntp")
                        else:
                            log.warn("[MAIN] NTP sync failed — continuing with RTC time")
                    except Exception as ntp_e:
                        log.warn(f"[MAIN] NTP sync exception: {ntp_e}")
                else:
                    log.info("[MAIN] NTP disabled in config — using RTC time")
                    # If the operator turned NTP off, the DS3231 is the
                    # only source for the coord. If _try_seed_time_from_rtc
                    # didn't already mark synced (battery flat etc.), we
                    # stay paused — same as any other coord with no time.
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

    # Physical reset button → machine.soft_reset() after ~200 ms hold.
    # Quick way to force-restart a unit in the field without pulling
    # power, and lets us drive the (A)+(B) LoRa-retry recovery loop
    # manually if we ever need to.
    reset_btn_pin = hw.get("reset_btn_pin")
    if reset_btn_pin is not None:
        from hardware.reset_button import run as run_reset_button_task
        tasks.append(asyncio.create_task(run_reset_button_task(reset_btn_pin)))
    # Always start the LoRa listener. If the transport isn't ready
    # (boot init failed; deferred retry hasn't succeeded yet), recv()
    # returns None and the loop just sleeps. Cheap. When the deferred
    # retry brings LoRa up, this task picks up real traffic without
    # any further wiring.
    tasks.append(asyncio.create_task(lora_protocol.listen_task()))

    # Deferred LoRa retry: silent re-attempt 100 s after boot if the
    # initial attempt failed. Short-circuits immediately on the
    # happy path. Skipped only when LoRa is explicitly disabled.
    if not lora_explicitly_disabled:
        tasks.append(asyncio.create_task(lora_deferred_retry_task()))

    if i2c_sensors.has_sensors:
        tasks.append(asyncio.create_task(i2c_sensors.run()))

    if sys.get("log_level") == "DEBUG":
        tasks.append(asyncio.create_task(ram_telemetry_task(60)))

    # Pre-existing bug: this used to read from `lora` but the field
    # has always lived under `system` in every shipped config, sample,
    # and validator. The default `30` therefore always won, silently
    # ignoring any operator override. Now reads from `system` as it
    # should have all along.
    hb_interval = sys.get("heartbeat_interval_s", 30)

    if role == "coordinator":
        tasks.append(asyncio.create_task(fleet_timeout_task(fleet_mgr)))
        # Periodic blue/red flash so a coord with no online leaves
        # still shows visible LoRa-health feedback. Runs regardless of
        # lora_ok — when LoRa is disabled it flashes red, which is
        # exactly the info the operator needs to see.
        tasks.append(asyncio.create_task(lora_status_flash_task()))
        # Always start the periodic TS broadcast. Broadcast is a no-op
        # while transport isn't ready; once deferred retry brings LoRa
        # up, periodic TS resumes naturally.
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
        # Leaf: always start HB broadcast + event forwarder. If the
        # transport isn't ready, lora_protocol.send_heartbeat/
        # send_error return early without touching the radio — the
        # tasks just spin at their normal cadence with nothing to
        # send. Once the deferred retry brings LoRa up, real HBs
        # and forwarded events start flowing automatically.
        tasks.append(asyncio.create_task(
            heartbeat_broadcast_task(hb_interval, cfg.unit_id)
        ))
        tasks.append(asyncio.create_task(event_forward_task()))
        tasks.append(asyncio.create_task(leaf_status_task()))
        tasks.append(asyncio.create_task(time_sync_request_task()))

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
