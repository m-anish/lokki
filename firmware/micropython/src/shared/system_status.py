"""
System status tracking for PagodaLightPico

Maintains current system state including LED status, active window,
and other runtime information for display on web interface and API endpoints.
"""

import time
import rtc_module
from lib import sun_times
from lib.config_manager import config_manager
from simple_logger import Logger

log = Logger()

class SystemStatus:
    """
    Tracks and provides current system status information.
    
    Maintains state about current LED duty cycle, active time window,
    system uptime, and other runtime information.
    """
    
    def __init__(self):
        self.startup_time = time.time()
        self.pin_status = {}  # {pin_key: {name, duty_cycle, window, window_start, window_end, gpio_pin}}
        self.last_update_time = 0
        self.total_updates = 0
        self.error_count = 0
        self.last_error = None
        self.wifi_connected = False
        self.mqtt_connected = False
        self.web_server_running = False
    
    def update_multi_pin_status(self, pin_updates):
        """
        Update status for multiple PWM pins.
        
        Args:
            pin_updates (dict): {pin_key: {name, window, duty_cycle, window_start, window_end}}
        """
        # Add GPIO pin information from config
        config_dict = config_manager.get_config_dict()
        pwm_pins = config_dict.get('pwm_pins', {})
        
        for pin_key, update_info in pin_updates.items():
            pin_config = pwm_pins.get(pin_key, {})
            gpio_pin = pin_config.get('gpio_pin')
            
            self.pin_status[pin_key] = {
                'name': update_info.get('name', pin_key),
                'duty_cycle': update_info.get('duty_cycle', 0),
                'window': update_info.get('window'),
                'window_start': update_info.get('window_start'),
                'window_end': update_info.get('window_end'),
                'gpio_pin': gpio_pin
            }
        
        # Remove pins that are no longer in the update
        current_pins = set(pin_updates.keys())
        stored_pins = set(self.pin_status.keys())
        for removed_pin in stored_pins - current_pins:
            del self.pin_status[removed_pin]
        
        self.last_update_time = time.time()
        self.total_updates += 1
        
        active_count = sum(1 for info in pin_updates.values() if info.get('duty_cycle', 0) > 0)
        log.debug(f"[STATUS] Updated {len(pin_updates)} pins, {active_count} active")
    
    def record_error(self, error_message):
        """
        Record system error.
        
        Args:
            error_message (str): Error description
        """
        self.error_count += 1
        self.last_error = {
            "message": error_message,
            "timestamp": time.time()
        }
        log.debug(f"[STATUS] Error recorded: {error_message}")
    
    def set_connection_status(self, wifi=None, mqtt=None, web_server=None):
        """
        Update connection status.
        
        Args:
            wifi (bool): WiFi connection status
            mqtt (bool): MQTT connection status  
            web_server (bool): Web server running status
        """
        if wifi is not None:
            self.wifi_connected = wifi
        if mqtt is not None:
            self.mqtt_connected = mqtt
        if web_server is not None:
            self.web_server_running = web_server
    
    def get_current_time_info(self):
        """
        Get current time and sunrise/sunset information.
        
        Returns:
            dict: Time information including current time, sunrise, sunset
        """
        try:
            current_time_tuple = rtc_module.get_current_time()
            month = current_time_tuple[1]
            day = current_time_tuple[2]
            
            # Get sunrise/sunset times
            sunrise_h, sunrise_m, sunset_h, sunset_m = sun_times.get_sunrise_sunset(month, day)
            
            # Format current time
            current_time_str = f"{current_time_tuple[3]:02d}:{current_time_tuple[4]:02d}:{current_time_tuple[5]:02d}"
            current_date_str = f"{current_time_tuple[2]:02d}/{current_time_tuple[1]:02d}/{current_time_tuple[0]}"
            
            return {
                "current_time": current_time_str,
                "current_date": current_date_str,
                "sunrise_time": f"{sunrise_h:02d}:{sunrise_m:02d}",
                "sunset_time": f"{sunset_h:02d}:{sunset_m:02d}",
                "timezone": config_manager.TIMEZONE_NAME
            }
        except Exception as e:
            log.error(f"[STATUS] Error getting time info: {e}")
            return {
                "current_time": "Unknown",
                "current_date": "Unknown", 
                "sunrise_time": "Unknown",
                "sunset_time": "Unknown",
                "timezone": "Unknown"
            }
    
    def get_uptime(self):
        """
        Get system uptime in seconds.
        
        Returns:
            float: Uptime in seconds
        """
        return time.time() - self.startup_time
    
    def get_uptime_string(self):
        """
        Get formatted uptime string.
        
        Returns:
            str: Human-readable uptime
        """
        uptime_seconds = self.get_uptime()
        
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)
        
        if days > 0:
            return f"{days}d {hours}h {minutes}m {seconds}s"
        elif hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    
    def get_network_info(self):
        """
        Get network status information.
        
        Returns:
            dict: Network status information
        """
        try:
            # Import here to avoid circular imports
            from lib.wifi_connect import get_network_status
            return get_network_status()
        except Exception as e:
            log.error(f"[STATUS] Error getting network info: {e}")
            return {
                "active": False,
                "connected": False,
                "hostname": None,
                "ip": None,
                "gateway": None,
                "dns": None,
                "signal_strength": None
            }
    
    def get_status_dict(self):
        """
        Get complete system status as dictionary.
        
        Returns:
            dict: Complete system status information
        """
        time_info = self.get_current_time_info()
        network_info = self.get_network_info()
        
        # Format pin status for display
        pins_display = {}
        
        # If pin_status is empty, get current status from PWM manager
        if not self.pin_status:
            try:
                from lib.pwm_control import multi_pwm
                pwm_status = multi_pwm.get_pin_status()
                config_dict = config_manager.get_config_dict()
                
                for pin_key, pin_info in pwm_status.items():
                    pin_config = config_dict.get('pwm_pins', {}).get(pin_key, {})
                    duty_percent = pin_info.get('duty_percent', 0)
                    
                    pins_display[pin_key] = {
                        'name': pin_info.get('name', pin_key),
                        'gpio_pin': pin_info.get('gpio_pin'),
                        'duty_cycle': duty_percent,
                        'duty_cycle_display': f"{duty_percent}%",
                        'status': "ON" if duty_percent > 0 else "OFF",
                        'window': None,
                        'window_display': "Unknown",
                        'window_start': None,
                        'window_end': None
                    }
            except Exception as e:
                log.error(f"[STATUS] Error getting PWM status: {e}")
        else:
            # Use existing pin_status data
            for pin_key, pin_info in self.pin_status.items():
                pins_display[pin_key] = {
                    'name': pin_info.get('name', pin_key),
                    'gpio_pin': pin_info.get('gpio_pin'),
                    'duty_cycle': pin_info.get('duty_cycle', 0),
                    'duty_cycle_display': f"{pin_info.get('duty_cycle', 0)}%",
                    'status': "ON" if pin_info.get('duty_cycle', 0) > 0 else "OFF",
                    'window': pin_info.get('window'),
                    'window_display': self._safe_format_window_name(pin_info.get('window')),
                    'window_start': pin_info.get('window_start'),
                    'window_end': pin_info.get('window_end')
                }
        
        return {
            "system": {
                "uptime": self.get_uptime(),
                "uptime_string": self.get_uptime_string(),
                "total_updates": self.total_updates,
                "error_count": self.error_count,
                "last_error": self.last_error
            },
            "connections": {
                "wifi": self.wifi_connected,
                "mqtt": self.mqtt_connected,
                "web_server": self.web_server_running
            },
            "network": network_info,
            "pins": pins_display,
            "time": time_info,
            "config": {
                "update_interval": config_manager.UPDATE_INTERVAL,
                "log_level": config_manager.LOG_LEVEL,
                "notifications_enabled": getattr(config_manager, 'NOTIFICATIONS_ENABLED', False)
            }
        }
    
    def _safe_format_window_name(self, window_name):
        """
        Safely format window name for display.
        
        Args:
            window_name (str): Raw window name
            
        Returns:
            str: Formatted window name
        """
        try:
            if not window_name:
                return "None"
            
            # Convert to string safely
            safe_name = str(window_name) if window_name is not None else ''
            if not safe_name:
                return "None"
                
            # Format the name
            formatted = safe_name.replace('_', ' ')
            if hasattr(formatted, 'title'):
                return formatted.title()
            else:
                return formatted
        except Exception as e:
            log.error(f"[STATUS] Error formatting window name '{window_name}': {e}")
            return "Unknown"
    
    def get_status_summary(self):
        """
        Get brief status summary for logging.
        
        Returns:
            str: Status summary string
        """
        time_info = self.get_current_time_info()
        
        # Count active pins
        active_pins = sum(1 for info in self.pin_status.values() if info.get('duty_cycle', 0) > 0)
        total_pins = len(self.pin_status)
        
        return (f"Pins: {active_pins}/{total_pins} active, "
                f"Time: {time_info['current_time']}, "
                f"Uptime: {self.get_uptime_string()}")


# Global system status instance
system_status = SystemStatus()
