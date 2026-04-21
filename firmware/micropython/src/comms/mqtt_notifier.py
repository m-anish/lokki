import json
import time
from core.config_manager import config_manager
from shared.simple_logger import Logger

try:
    from comms.umqtt.simple import MQTTClient
    _MQTT_OK = True
except ImportError:
    _MQTT_OK = False

log = Logger()


class MQTTNotifier:

    def __init__(self):
        self.client = None
        self.connected = False
        self._load_config()

    def _load_config(self):
        n = config_manager.get("notifications")
        self.enabled     = n.get("mqtt_enabled", False)
        self.broker      = n.get("broker", "broker.hivemq.com")
        self.port        = n.get("port", 1883)
        self.topic       = n.get("topic_prefix", "lokki")
        self.client_id   = n.get("client_id", "lokki")

    def connect(self):
        from shared.system_status import system_status
        if not _MQTT_OK or not self.enabled:
            system_status.set_connection_status(mqtt=False)
            return False
        try:
            self.client = MQTTClient(self.client_id, self.broker, port=self.port)
            self.client.connect()
            self.connected = True
            system_status.set_connection_status(mqtt=True)
            log.info(f"[MQTT] Connected to {self.broker}:{self.port}")
            self._publish("system", {"event": "startup", "t": time.time()})
            return True
        except Exception as e:
            log.error(f"[MQTT] Connect failed: {e}")
            self.connected = False
            system_status.set_connection_status(mqtt=False)
            return False

    def disconnect(self):
        if self.client and self.connected:
            try:
                self._publish("system", {"event": "shutdown", "t": time.time()})
                self.client.disconnect()
            except Exception:
                pass
            self.connected = False

    def notify_error(self, msg):
        if self.connected:
            self._publish("error", {"event": "error", "msg": msg, "t": time.time()})

    def notify_output_change(self, output_id, value):
        if self.connected:
            self._publish("output", {"id": output_id, "value": value, "t": time.time()})

    def _publish(self, category, data):
        if not self.client or not self.connected:
            return
        try:
            self.client.publish(f"{self.topic}/{category}", json.dumps(data))
        except Exception as e:
            log.error(f"[MQTT] Publish failed: {e}")
            self.connected = False


mqtt_notifier = MQTTNotifier()
