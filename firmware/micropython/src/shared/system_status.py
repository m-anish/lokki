import time


# Earliest epoch we'll trust as a "real" time. Anything earlier is the
# MCU sitting at 2000-01-01 (MicroPython's default) or 1970-01-01
# (Unix epoch) — both signal "the RTC didn't keep time across power
# loss and NTP hasn't run yet". 1_700_000_000 ≈ Nov 14 2023, comfortably
# before Lokki's earliest plausible deployment date.
_SANE_TIME_THRESHOLD_S = 1_700_000_000


def time_is_sane():
    """True iff the MCU's wall clock looks like a real date.

    Used as the gate around schedule_task and any other code path that
    would do wrong things with a bogus year. Cheap to call (just reads
    time.time() and compares); safe from any task.
    """
    try:
        return time.time() > _SANE_TIME_THRESHOLD_S
    except Exception:
        return False


class SystemStatus:
    """Runtime status for this unit — uptime, connections, output state, errors."""

    def __init__(self):
        # Use ticks_ms for uptime - it's monotonic and not affected by time sync
        self._boot_ticks = time.ticks_ms()
        self.wifi_connected = False
        self.lora_connected = False
        self.web_server_running = False
        self.mqtt_connected = False
        # Set True once we've confirmed a real wall-clock time — from
        # NTP (coord), the DS3231 returning a sane value (either role),
        # a LoRa TS broadcast (leaf), or operator override. Until this
        # is True, schedule_task skips its tick and the status LED
        # shows the time_waiting pattern. See firmware/.../main.py for
        # the wiring at boot.
        self.time_synced = False
        self.time_synced_source = None   # "ntp" | "rtc" | "ts" | "manual" | None
        self.error_count = 0
        self.last_error = None

    def mark_time_synced(self, source):
        """Flip time_synced to True. Idempotent — first source wins
        for the diagnostic field; further calls bump nothing."""
        if not self.time_synced:
            self.time_synced = True
            self.time_synced_source = source

    def set_connection_status(self, wifi=None, lora=None, web_server=None, mqtt=None):
        if wifi is not None:
            self.wifi_connected = wifi
        if lora is not None:
            self.lora_connected = lora
        if web_server is not None:
            self.web_server_running = web_server
        if mqtt is not None:
            self.mqtt_connected = mqtt

    def record_error(self, msg):
        self.error_count += 1
        self.last_error = {"message": msg, "timestamp": time.time()}

    def get_uptime(self):
        # Use ticks_diff for accurate uptime regardless of time sync
        uptime_ms = time.ticks_diff(time.ticks_ms(), self._boot_ticks)
        return uptime_ms // 1000  # Convert to seconds

    def get_uptime_string(self):
        s = self.get_uptime()
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if d:
            return f"{d}d {h}h {m}m {s}s"
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def get_status_dict(self):
        from hardware.pwm_control import pwm_controller
        from hardware.relay_control import relay_controller
        from hardware.pir_manager import pir_manager
        from hardware.ldr_monitor import ldr_monitor
        from hardware.i2c_sensors import i2c_sensors
        from core.config_manager import config_manager
        return {
            "unit_name": config_manager.unit_name,
            "unit_id":   config_manager.unit_id,
            "role":      config_manager.role,
            "uptime_s": self.get_uptime(),
            "uptime": self.get_uptime_string(),
            "connections": {
                "wifi": self.wifi_connected,
                "lora": self.lora_connected,
                "web_server": self.web_server_running,
                "mqtt": self.mqtt_connected,
            },
            "time_synced":        self.time_synced,
            "time_synced_source": self.time_synced_source,
            "led_channels": pwm_controller.get_all(),
            "relays": relay_controller.get_all(),
            "pir": pir_manager.get_all_states(),
            "ldr_ambient": ldr_monitor.ambient_percent,
            "ldr_cap": ldr_monitor.cap_percent,
            "sensors": i2c_sensors.get_readings(),
            "error_count": self.error_count,
            "last_error": self.last_error,
        }


system_status = SystemStatus()
