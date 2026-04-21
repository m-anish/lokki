import time


class SystemStatus:
    """Runtime status for this unit — uptime, connections, output state, errors."""

    def __init__(self):
        # Use ticks_ms for uptime - it's monotonic and not affected by time sync
        self._boot_ticks = time.ticks_ms()
        self.wifi_connected = False
        self.lora_connected = False
        self.web_server_running = False
        self.error_count = 0
        self.last_error = None

    def set_connection_status(self, wifi=None, lora=None, web_server=None):
        if wifi is not None:
            self.wifi_connected = wifi
        if lora is not None:
            self.lora_connected = lora
        if web_server is not None:
            self.web_server_running = web_server

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
        return {
            "uptime_s": self.get_uptime(),
            "uptime": self.get_uptime_string(),
            "connections": {
                "wifi": self.wifi_connected,
                "lora": self.lora_connected,
                "web_server": self.web_server_running,
            },
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
