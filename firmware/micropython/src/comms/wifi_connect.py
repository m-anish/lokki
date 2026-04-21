import network
import time
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


def connect_wifi(timeout=10, max_attempts=3):
    wifi_cfg = config_manager.get("wifi")
    ssid     = wifi_cfg.get("ssid", "")
    password = wifi_cfg.get("password", "")
    hostname = wifi_cfg.get("hostname", "lokki")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    try:
        network.hostname(hostname)
    except Exception:
        pass

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


def sync_time_ntp():
    tz_offset = config_manager.get("timezone").get("utc_offset_hours", 0)
    servers = ["pool.ntp.org"]  # Try only one server to avoid long delays
    
    synced = False
    for server in servers:
        start_time = time.time()
        try:
            ntptime.host = server
            ntptime.timeout = 3  # Increased from 2 to 3 seconds
            log.info(f"[NTP] Attempting sync with {server}...")
            
            # Add manual timeout check in case ntptime.settime() blocks
            ntptime.settime()
            
            elapsed = time.time() - start_time
            log.info(f"[NTP] Synced with {server} in {elapsed:.1f}s")
            synced = True
            break
        except OSError as e:
            elapsed = time.time() - start_time
            log.warn(f"[NTP] {server} network error after {elapsed:.1f}s: {e}")
            # Network errors are usually quick, continue to next attempt
        except Exception as e:
            elapsed = time.time() - start_time
            log.warn(f"[NTP] {server} failed after {elapsed:.1f}s: {e}")
        
        # If we spent too long, don't retry
        if time.time() - start_time > 5:
            log.error("[NTP] Timeout exceeded, aborting")
            break
    
    if not synced:
        log.error("[NTP] All sync attempts failed")
        raise Exception("NTP sync failed")

    utc_sec   = time.time()
    local_sec = utc_sec + int(tz_offset * 3600)
    dt_tuple  = urtc.seconds2tuple(local_sec)
    rtc.datetime(dt_tuple)
    log.info("[NTP] DS3231 updated with local time")

    try:
        from comms.lora_protocol import lora_protocol
        lora_protocol.broadcast_time_sync(utc_sec, tz_offset)
        log.info("[NTP] TIME_SYNC broadcast sent")
    except Exception as e:
        log.warn(f"[NTP] TIME_SYNC broadcast failed: {e}")

    return True


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
