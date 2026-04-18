"""
MQTT Notification system for PagodaLightPico

Sends push notifications about time window changes and system events
via MQTT broker for delivery to mobile devices.

Supported notification methods:
- MQTT → Pushover (recommended)
- MQTT → Home Assistant → Mobile app
- MQTT → Telegram Bot
- MQTT → Custom webhook services
"""

import json
import time
from lib.config_manager import config_manager
from simple_logger import Logger

# Try to import MQTT library (may not be available in all MicroPython builds)
try:
    from umqtt.simple import MQTTClient
    MQTT_AVAILABLE = True
except ImportError:
    try:
        from mqtt.simple import MQTTClient
        MQTT_AVAILABLE = True
    except ImportError:
        MQTT_AVAILABLE = False

log = Logger()

class MQTTNotifier:
    """
    MQTT-based notification system for sending push notifications.
    
    Connects to configured MQTT broker and publishes notification messages
    that can be consumed by various push notification services.
    """
    
    def __init__(self):
        self.client = None
        self.connected = False
        self.last_windows = {}  # {pin_key: last_window}
        self.notifications_enabled = False
        self._load_config()
    
    def _load_config(self):
        """Load MQTT configuration from config manager."""
        config = config_manager.get_config_dict()
        notifications = config.get('notifications', {})
        
        self.notifications_enabled = notifications.get('enabled', False)
        self.broker = notifications.get('mqtt_broker', 'broker.hivemq.com')
        self.port = notifications.get('mqtt_port', 1883)
        self.topic = notifications.get('mqtt_topic', 'PagodaLightPico/notifications')
        self.client_id = notifications.get('mqtt_client_id', 'PagodaLightPico')
        self.notify_on_window_change = notifications.get('notify_on_window_change', True)
        self.notify_on_errors = notifications.get('notify_on_errors', True)
    
    def connect(self):
        """Connect to MQTT broker."""
        if not MQTT_AVAILABLE:
            log.warn("[MQTT] Library not available - notifications disabled")
            return False
        
        if not self.notifications_enabled:
            log.debug("[MQTT] Notifications disabled in configuration")
            return False
        
        try:
            self.client = MQTTClient(self.client_id, self.broker, port=self.port)
            self.client.connect()
            self.connected = True
            log.info(f"[MQTT] Connected to broker {self.broker}:{self.port}")
            
            # Send startup notification
            self._send_notification("system", {
                "event": "system_startup",
                "message": "[STARTUP] PagodaLight system started",
                "timestamp": time.time(),
                "device": self.client_id
            })
            
            return True
            
        except Exception as e:
            log.error(f"[MQTT] Failed to connect to broker: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Disconnect from MQTT broker."""
        if self.client and self.connected:
            try:
                # Send shutdown notification
                self._send_notification("system", {
                    "event": "system_shutdown", 
                    "message": "[SHUTDOWN] PagodaLight system stopping",
                    "timestamp": time.time(),
                    "device": self.client_id
                })
                self.client.disconnect()
                log.info("[MQTT] Disconnected from broker")
            except Exception as e:
                log.error(f"[MQTT] Error disconnecting: {e}")
            finally:
                self.connected = False
    
    
    def notify_error(self, error_message):
        """
        Send notification for system errors.
        
        Args:
            error_message (str): Error description
        """
        if not self.notify_on_errors or not self.connected:
            return
        
        notification_data = {
            "event": "error",
            "message": f"[ERROR] PagodaLight Error: {error_message}",
            "timestamp": time.time(),
            "device": self.client_id,
            "severity": "error"
        }
        
        self._send_notification("error", notification_data)
        log.debug(f"[MQTT] Sent error notification: {error_message}")
    
    def notify_multi_pin_changes(self, pin_updates):
        """
        Send notifications for multiple pin window changes.
        
        Args:
            pin_updates (dict): {pin_key: {name, window, duty_cycle, window_start, window_end}}
        """
        if not self.notify_on_window_change or not self.connected:
            return
        
        # Check each pin for window changes
        changed_pins = []
        for pin_key, update_info in pin_updates.items():
            current_window = update_info.get('window')
            last_window = self.last_windows.get(pin_key)
            
            # Only notify if window actually changed
            if current_window != last_window:
                self.last_windows[pin_key] = current_window
                changed_pins.append((pin_key, update_info))
        
        if not changed_pins:
            return
        
        # Send individual notifications for each changed pin
        for pin_key, update_info in changed_pins:
            pin_name = update_info.get('name', pin_key)
            window_name = update_info.get('window')
            duty_cycle = update_info.get('duty_cycle', 0)
            start_time = update_info.get('window_start')
            end_time = update_info.get('window_end')
            
            # Format notification message without emojis
            if window_name == "day":
                prefix = "DAY"
                description = "sunrise to sunset"
            elif duty_cycle == 0:
                prefix = "OFF"
                description = "lights off"
            else:
                prefix = "ON"
                description = f"meditation lighting"
            
            message = f"[{prefix}] {pin_name}: {description} - {duty_cycle}% brightness"
            
            notification_data = {
                "event": "pin_window_change",
                "pin_key": pin_key,
                "pin_name": pin_name,
                "window": window_name,
                "duty_cycle": duty_cycle,
                "start_time": start_time,
                "end_time": end_time,
                "message": message,
                "timestamp": time.time(),
                "device": self.client_id
            }
            
            self._send_notification("pin_change", notification_data)
            log.debug(f"[MQTT] Sent pin change notification: {message}")
        
        # Also send a summary notification if multiple pins changed
        if len(changed_pins) > 1:
            summary_message = f"[UPDATE] {len(changed_pins)} pins changed windows"
            summary_data = {
                "event": "multi_pin_change",
                "changed_pin_count": len(changed_pins),
                "changed_pins": [pin_key for pin_key, _ in changed_pins],
                "message": summary_message,
                "timestamp": time.time(),
                "device": self.client_id
            }
            
            self._send_notification("summary", summary_data)
            log.debug(f"[MQTT] Sent multi-pin summary: {summary_message}")
    
    def notify_config_change(self):
        """Send notification when configuration is updated."""
        if not self.connected:
            return
        
        notification_data = {
            "event": "config_update",
            "message": "[CONFIG] Configuration updated via web interface",
            "timestamp": time.time(),
            "device": self.client_id
        }
        
        self._send_notification("config", notification_data)
        log.debug("[MQTT] Sent configuration change notification")
    
    def _send_notification(self, category, data):
        """
        Send notification via MQTT.
        
        Args:
            category (str): Notification category (window_change, error, system, config)
            data (dict): Notification data
        """
        if not self.client or not self.connected:
            return
        
        try:
            # Create topic with category
            topic = f"{self.topic}/{category}"
            
            # Convert data to JSON
            message = json.dumps(data)
            
            # Publish message
            self.client.publish(topic, message)
            log.debug(f"[MQTT] Published notification to {topic}")
            
        except Exception as e:
            log.error(f"[MQTT] Failed to send notification: {e}")
            # Try to reconnect on next notification
            self.connected = False
    
    def reload_config(self):
        """Reload configuration and reconnect if needed."""
        old_enabled = self.notifications_enabled
        old_broker = self.broker
        old_port = self.port
        
        self._load_config()
        
        # Reconnect if configuration changed
        if (self.notifications_enabled != old_enabled or 
            self.broker != old_broker or 
            self.port != old_port):
            
            if self.connected:
                self.disconnect()
            
            if self.notifications_enabled:
                self.connect()
    
    def get_status(self):
        """Get current MQTT connection status."""
        return {
            "mqtt_available": MQTT_AVAILABLE,
            "notifications_enabled": self.notifications_enabled,
            "connected": self.connected,
            "broker": self.broker if self.notifications_enabled else None,
            "topic": self.topic if self.notifications_enabled else None
        }


# Global MQTT notifier instance
mqtt_notifier = MQTTNotifier()
