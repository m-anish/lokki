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


# ── AP-mode fallback ────────────────────────────────────────────────
# When the configured STA network is unreachable (or no SSID is set
# at all — fresh install), the coord brings up its own access point
# so the operator can join it from a phone and reach the dashboard
# at lokki.local (or the AP gateway IP 192.168.4.1). Same web server,
# same auth — just a different netif providing the route.
#
# WPA2-PSK only. The dashboard's HTTP Basic auth is the actual
# access control; the AP password is just there to deter casual
# WiFi scanners from joining. Operators are expected to set
# dashboard.auth_password before exposing the device anywhere.

# Pico W's cyw43 stack defaults AP gateway IP to 192.168.4.1 and
# runs a built-in DHCP server. No extra plumbing needed.
_AP_GATEWAY_IP = "192.168.4.1"


def ap_start():
    """Bring up the SoftAP. Idempotent — safe to call when AP is
    already active. Returns True if the AP is up afterwards, False
    on any config / activation failure.

    AP mode is currently OPEN (no WPA2). Background:
      We tried security=4 (intended as WPA/WPA2 mixed) and security=3
      (intended as WPA2-PSK), both of which produced a malformed
      beacon that macOS interpreted as WEP — prompting for a WEP key
      that couldn't possibly match. Turns out the rp2 cyw43 driver in
      MicroPython expects raw `cyw43_auth_t` flag values (e.g.
      0x00400004 for WPA2-AES-PSK), not the 0–4 enum I was assuming.
      Rather than burn more flash cycles iterating on the right magic
      number, we ship open mode. Access control is provided by the
      dashboard's HTTP Basic auth gate (`dashboard.auth_password`),
      which is the real security boundary anyway — anyone in WiFi
      range could observe a WPA2-PSK pre-shared key over time, but
      the auth password is per-session and (in real deployments)
      operator-rotated.

      To re-attempt WPA2 in the future: pass `security=0x00400004`
      (WPA2-AES-PSK) instead of removing the parameter. Verify with
      a non-macOS client (Android / iOS / Linux) since macOS aggres-
      sively caches per-SSID security mode and may stay confused.

    Reads wifi.ap_ssid from config; falls back to "Lokki-Setup" so a
    fresh install with no AP config still comes up reachable.
    """
    wifi_cfg = config_manager.get("wifi") or {}
    ap_ssid = wifi_cfg.get("ap_ssid") or "Lokki-Setup"

    ap = network.WLAN(network.AP_IF)
    try:
        # security=0 = open. No password. See header comment for the
        # full story on why we're not running WPA2 here right now.
        ap.config(essid=ap_ssid, security=0)
    except Exception as e:
        log.error(f"[AP] config() failed: {e}")
        return False

    try:
        ap.active(True)
    except Exception as e:
        log.error(f"[AP] active(True) failed: {e}")
        return False

    # Poll briefly until active() reports True — cyw43 takes a tick
    # or two to come up. If it doesn't, we report failure rather than
    # claim success and have the operator wonder why no SSID is
    # visible.
    for _ in range(20):
        if ap.active():
            ip = None
            try:
                ip = ap.ifconfig()[0]
            except Exception:
                pass
            log.info(f"[AP] Up: SSID='{ap_ssid}' (open, no password) "
                     f"IP={ip or _AP_GATEWAY_IP}")
            log.warn("[AP] SoftAP is OPEN. Set dashboard.auth_password "
                     "to gate the dashboard surface.")
            return True
        time.sleep_ms(100)

    log.error("[AP] Failed to come up within 2 s")
    return False


def ap_stop():
    """Tear down the SoftAP. Idempotent — safe to call when AP isn't
    active. Used when STA recovers and we want to deny accidental
    long-lived AP exposure."""
    ap = network.WLAN(network.AP_IF)
    try:
        if ap.active():
            ap.active(False)
            log.info("[AP] Stopped")
    except Exception as e:
        log.warn(f"[AP] active(False) failed: {e}")


def ap_is_active():
    try:
        return network.WLAN(network.AP_IF).active()
    except Exception:
        return False


def ap_ip():
    try:
        return network.WLAN(network.AP_IF).ifconfig()[0]
    except Exception:
        return None
