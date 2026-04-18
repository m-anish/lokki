"""
WiFi connection and NTP time synchronization module.

Handles:
- Connecting to WiFi network using credentials from config.
- Synchronizing system RTC time from NTP server with timezone adjustment.
- Writing updated time to DS3231 RTC using urtc library.
- Controls LED status for connection state.
- Logs status messages using the custom Logger.
"""

import network
import time
import ntptime
from lib.config_manager import WIFI_SSID, WIFI_PASSWORD, TIMEZONE_OFFSET, config_manager
from machine import Pin
import urtc
from simple_logger import Logger
from lib.rtc_shared import rtc

# For Pico W, the onboard LED is on "LED" pin, not GPIO 25
try:
    led = Pin("LED", Pin.OUT)  # Pico W onboard LED
except:
    led = Pin(25, Pin.OUT)  # Fallback for regular Pico
log = Logger()


def connect_wifi(timeout=10, max_attempts=3):
    """
    Connects to WiFi using credentials from config file with retry logic.

    Args:
        timeout (int): How many seconds to wait per attempt before giving up.
        max_attempts (int): Maximum number of connection attempts.

    Returns:
        bool: True if connected, False on failure after all attempts.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    # Set hostname from config
    hostname = config_manager.get_config_dict().get('hostname', 'PagodaLightPico')
    try:
        network.hostname(hostname)
        log.debug(f"[WIFI] Set network hostname to: {hostname}")
    except Exception as e:
        log.warn(f"[WIFI] Failed to set hostname: {e}")
    
    # Check if already connected
    if wlan.isconnected():
        ip_info = wlan.ifconfig()
        log.info(f"[WIFI] Already connected, IP: {ip_info[0]}")
        log.debug(f"[WIFI] Network Details:")
        log.debug(f"[WIFI]   Subnet Mask: {ip_info[1]}")
        log.debug(f"[WIFI]   Gateway: {ip_info[2]}")
        log.debug(f"[WIFI]   DNS Server: {ip_info[3]}")
        log.info(f"[WIFI] Web interface: http://{ip_info[0]}/")
        led.value(1)  # LED ON when connected
        return True
    
    # Attempt to connect with retries
    for attempt in range(1, max_attempts + 1):
        log.debug(f"[WIFI] Connection attempt {attempt}/{max_attempts}")
        
        try:
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            start = time.time()
            
            # Wait for connection or timeout (reduced polling frequency)
            while not wlan.isconnected():
                if time.time() - start > timeout:
                    log.warn(f"[WIFI] Attempt {attempt} timed out after {timeout} seconds")
                    break
                time.sleep(0.25)
            
            # Check if connection was successful
            if wlan.isconnected():
                ip_info = wlan.ifconfig()
                log.info(f"[WIFI] Connected (attempt {attempt}), IP: {ip_info[0]}")
                log.debug(f"[WIFI] Network Details:")
                log.debug(f"[WIFI]   Subnet Mask: {ip_info[1]}")
                log.debug(f"[WIFI]   Gateway: {ip_info[2]}")
                log.debug(f"[WIFI]   DNS Server: {ip_info[3]}")
                log.info(f"[WIFI] Web interface: http://{ip_info[0]}/")
                led.value(1)  # LED ON when connected
                return True
            
        except Exception as e:
            log.error(f"[WIFI] Connection attempt {attempt} failed with error: {e}")
        
        # Wait before retry (except on last attempt) - relaxed backoff
        if attempt < max_attempts:
            log.debug(f"[WIFI] Waiting 4 seconds before retry...")
            time.sleep(4)
    
    # All attempts failed
    log.error(f"[WIFI] Failed to connect after {max_attempts} attempts")
    led.value(0)  # LED OFF when not connected
    return False


def get_network_status():
    """
    Get current network connection status and information.
    
    Returns:
        dict: Network status information including connection state, IP, etc.
    """
    wlan = network.WLAN(network.STA_IF)
    
    if not wlan.active():
        return {
            "active": False,
            "connected": False,
            "hostname": None,
            "ip": None,
            "gateway": None,
            "dns": None,
            "signal_strength": None
        }
    
    connected = wlan.isconnected()
    ip_info = wlan.ifconfig() if connected else [None, None, None, None]
    
    # Get hostname if available
    hostname = None
    try:
        hostname = network.hostname()
    except:
        pass
    
    # Get signal strength if connected
    signal_strength = None
    if connected:
        try:
            signal_strength = wlan.status('rssi')
        except:
            pass
    
    return {
        "active": wlan.active(),
        "connected": connected,
        "hostname": hostname,
        "ip": ip_info[0],
        "subnet": ip_info[1],
        "gateway": ip_info[2],
        "dns": ip_info[3],
        "signal_strength": signal_strength
    }


def sync_time_ntp():
    """
    Synchronize system time from NTP server using ntptime.

    Applies timezone offset from config and writes corrected time back to
    DS3231 RTC.

    Returns:
        bool: True if successful, False otherwise.
    """
    # List of NTP servers to try
    ntp_servers = [
        "pool.ntp.org",
        "time.nist.gov", 
        "time.google.com",
        "0.pool.ntp.org"
    ]
    
    for server in ntp_servers:
        try:
            log.debug(f"[NTP] Trying time synchronization with {server}")
            ntptime.host = server
            ntptime.timeout = 5  # 5 second timeout
            ntptime.settime()  # syncs to UTC time
            log.info(f"[NTP] Successfully synchronized with {server}")
            break
        except Exception as e:
            log.warn(f"[NTP] Failed to sync with {server}: {e}")
            if server == ntp_servers[-1]:  # Last server
                raise e
            continue
    
    try:

        # Get current UTC timestamp in seconds
        utc_sec = time.time()
        log.debug(f"[NTP] System RTC time after sync (UTC seconds): {utc_sec}")

        # Apply timezone offset in seconds
        offset_sec = int(TIMEZONE_OFFSET * 3600)
        log.debug(f"[NTP] Timezone offset in seconds (from config): {offset_sec}")

        local_sec = utc_sec + offset_sec
        log.debug(f"[NTP] Local time in seconds after applying offset: {local_sec}")

        # Convert to tuple compatible with urtc DS3231 datetime
        dt_tuple = urtc.seconds2tuple(local_sec)
        log.debug(f"[NTP] Converted local time tuple for DS3231: {dt_tuple}")

        # Write corrected time to DS3231 RTC
        rtc.datetime(dt_tuple)
        log.info("[NTP] DS3231 RTC datetime updated successfully with local time")

        # Verify by reading back from DS3231 RTC
        readback = rtc.datetime()
        log.debug(f"[NTP] RTC datetime read back for verification: {readback}")

        return True
    except Exception as e:
        log.error(f"[NTP] Time synchronization failed: {e}")
        return False
