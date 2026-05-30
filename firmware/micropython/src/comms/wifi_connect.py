import network
import time
import asyncio
import ntptime
from hardware import urtc
from machine import Pin
from core.config_manager import config_manager
from hardware.rtc_shared import rtc
from shared.simple_logger import Logger

log = Logger()

try:
    led = Pin("LED", Pin.OUT)
except Exception:
    led = Pin(25, Pin.OUT)


def _prepare_wlan():
    """Common setup for connect_wifi / connect_wifi_async. Sets the
    hostname (which is what lwIP's mDNS responder advertises), activates
    the STA interface, and returns (wlan, ssid, password). Safe to call
    repeatedly — `wlan.active(True)` and `network.hostname()` are both
    idempotent on every RP2 build we've tested."""
    wifi_cfg = config_manager.get("wifi")
    ssid     = wifi_cfg.get("ssid", "")
    password = wifi_cfg.get("password", "")
    hostname = wifi_cfg.get("hostname", "lokki")

    # Hostname BEFORE active(True) — some lwIP builds latch the netif
    # name at activation time. Re-setting on every call (including
    # post-reconnect) keeps mDNS announcing the right name even after
    # the radio renegotiates with the AP.
    try:
        network.hostname(hostname)
        log.info(f"[WIFI] Hostname set to '{hostname}' (try {hostname}.local)")
    except Exception as e:
        log.warn(f"[WIFI] network.hostname() not supported on this build: {e}")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    return wlan, ssid, password


def connect_wifi(timeout=10, max_attempts=3):
    """BLOCKING WiFi connect. Used at boot — fine to block there since
    no async tasks are running yet. Do NOT call this from inside an
    async task; the 3×10 s gauntlet will freeze the event loop and
    things like the reset button won't poll. Use
    connect_wifi_async() from async code instead.
    """
    wlan, ssid, password = _prepare_wlan()

    if wlan.isconnected():
        log.info(f"[WIFI] Already connected, IP: {wlan.ifconfig()[0]}")
        led.value(1)
        return True

    for attempt in range(1, max_attempts + 1):
        try:
            wlan.connect(ssid, password)
            start = time.time()
            while not wlan.isconnected():
                if time.time() - start > timeout:
                    log.warn(f"[WIFI] Attempt {attempt} timed out")
                    break
                time.sleep(0.25)
            if wlan.isconnected():
                log.info(f"[WIFI] Connected, IP: {wlan.ifconfig()[0]}")
                led.value(1)
                return True
        except Exception as e:
            log.error(f"[WIFI] Attempt {attempt} error: {e}")
        if attempt < max_attempts:
            time.sleep(4)

    log.error(f"[WIFI] Failed after {max_attempts} attempts")
    led.value(0)
    return False


async def connect_wifi_async(timeout=10):
    """Cooperative single-attempt reconnect, for async callers (the
    wifi_monitor recovery loop). Yields to the event loop between
    polls so the reset button task and other asyncio coroutines
    keep ticking while we're waiting for the AP. One attempt only —
    callers schedule retries at the cadence that suits them.

    Returns True if connected, False otherwise. Does NOT log noisily;
    the monitor loop owns user-visible state-change logging.
    """
    wlan, ssid, password = _prepare_wlan()

    if wlan.isconnected():
        led.value(1)
        return True

    try:
        wlan.connect(ssid, password)
    except Exception as e:
        log.warn(f"[WIFI] connect() raised: {e}")
        return False

    # Poll wlan.isconnected() with short async sleeps. ticks_ms /
    # ticks_diff avoid time-of-day discontinuities if NTP fires
    # mid-wait (which is unlikely while WiFi is still coming up, but
    # cheap insurance).
    start = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), start) > timeout * 1000:
            return False
        await asyncio.sleep_ms(250)
    led.value(1)
    return True


def sync_time_ntp():
    """Attempt NTP sync. Returns True iff the MCU's wall-clock is now
    set, False otherwise. Non-fatal.

    Important separation: `ntptime.settime()` *itself* is what writes
    the MCU's RTC. The DS3231 write and the LoRa TS broadcast that
    follow are best-effort side actions. We deliberately do NOT let a
    failure in those (commonly an I2C EIO on a flaky DS3231 bus, or a
    LoRa transport that isn't up yet) tank the return value, because
    the clock is already correct at that point — the rest of the
    system (schedule gate, log timestamps) needs to learn that.
    """
    tz_offset = config_manager.get("timezone").get("utc_offset_hours", 0)
    servers = ["pool.ntp.org", "time.google.com"]

    for server in servers:
        start_time = time.time()
        try:
            ntptime.host = server
            ntptime.timeout = 3
            log.info(f"[NTP] Attempting sync with {server}...")
            try:
                ntptime.settime()        # ← THE write to the MCU RTC
            except OSError as e:
                log.warn(f"[NTP] {server} network error: {e}")
                continue
        except Exception as e:
            # Failure BEFORE settime() — host/timeout config or similar.
            elapsed = time.time() - start_time
            log.warn(f"[NTP] {server} setup error after {elapsed:.1f}s: {e}")
            continue

        # If we got here, ntptime.settime() returned without raising —
        # time.time() now reflects real UTC seconds-since-epoch. The
        # commit is durable from this line on, irrespective of what
        # happens in the secondary writes below.
        elapsed = time.time() - start_time
        log.info(f"[NTP] Synced with {server} in {elapsed:.1f}s")
        utc_sec = time.time()

        # Secondary: mirror to the DS3231 so the time survives a power
        # cycle. Tolerated to fail — a flaky I2C bus shouldn't lose us
        # the freshly-synced MCU clock.
        try:
            local_sec = utc_sec + int(tz_offset * 3600)
            dt_tuple  = urtc.seconds2tuple(local_sec)
            rtc.datetime(dt_tuple)
            log.info("[NTP] DS3231 updated with local time")
        except Exception as e:
            log.warn(f"[NTP] DS3231 write failed (MCU clock still set): {e}")

        # Secondary: broadcast TS to the leaves. Same tolerance — if
        # the LoRa transport is down, leaves catch up on the next
        # periodic broadcast. Doesn't affect coord-side time sync.
        try:
            from comms.lora_protocol import lora_protocol
            lora_protocol.broadcast_time_sync(utc_sec, tz_offset)
            log.info("[NTP] TIME_SYNC broadcast sent")
        except Exception as e:
            log.warn(f"[NTP] TIME_SYNC broadcast failed: {e}")

        return True

    log.error("[NTP] All sync attempts failed - continuing without NTP")
    return False


def get_network_status():
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        return {"active": False, "connected": False, "ip": None}
    connected = wlan.isconnected()
    ip_info = wlan.ifconfig() if connected else [None, None, None, None]
    rssi = None
    if connected:
        try:
            rssi = wlan.status("rssi")
        except Exception:
            pass
    return {
        "active": True,
        "connected": connected,
        "ip": ip_info[0],
        "subnet": ip_info[1],
        "gateway": ip_info[2],
        "dns": ip_info[3],
        "rssi": rssi,
    }
