import json
import os


class SafeModeError(Exception):
    pass


class _BasicLogger:
    def info(self, m): print("INFO:", m)
    def warn(self, m): print("WARN:", m)
    def error(self, m): print("ERROR:", m)
    def debug(self, m): pass


_log = _BasicLogger()

# Valid GPIO pins on RP2350 Pico 2 / Pico 2 W
_VALID_PINS = set(range(29))

# Reserved GPIOs that must not be used as LED channel or relay outputs
_RESERVED_PINS = {0, 1, 2, 3, 4, 5, 20, 21}  # LoRa UART + I2C + status LED

# Expected major version — bump when schema is incompatible
_MAJOR_VERSION = "1"


class ConfigManager:

    def __init__(self, config_file="config.json"):
        self.config_file = config_file
        self._config = {}
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self):
        try:
            os.stat(self.config_file)
        except OSError:
            raise SafeModeError("config.json not found")

        try:
            with open(self.config_file, "r") as f:
                self._config = json.load(f)
        except Exception as e:
            raise SafeModeError(f"config.json unreadable: {e}")

        self._validate()
        _log.info("Config loaded OK")

    def get(self, section):
        return self._config.get(section, {})

    def get_all(self):
        return self._config

    def save(self, section, data):
        self._config[section] = data
        try:
            with open(self.config_file, "w") as f:
                json.dump(self._config, f)
            return True
        except Exception as e:
            _log.error(f"Config save failed: {e}")
            return False

    def replace(self, new_config_str):
        try:
            candidate = json.loads(new_config_str)
        except Exception as e:
            raise ValueError(f"Invalid JSON: {e}")
        old = self._config
        self._config = candidate
        try:
            self._validate()
        except Exception:
            self._config = old
            raise
        with open(self.config_file, "w") as f:
            json.dump(self._config, f)

    def reload(self):
        self.load()

    @property
    def version(self):
        return self._config.get("version", "")

    @property
    def role(self):
        return self.get("system").get("role", "leaf")

    @property
    def unit_id(self):
        return self.get("system").get("unit_id", 1)

    @property
    def unit_name(self):
        return self.get("system").get("unit_name", "Lokki")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self):
        errors = []
        self._validate_version(errors)
        self._validate_system(errors)
        self._validate_timezone(errors)
        self._validate_hardware(errors)
        self._validate_lora(errors)
        self._validate_ldr(errors)
        self._validate_pir(errors)
        self._validate_relays(errors)
        self._validate_led_channels(errors)
        self._validate_scenes(errors)
        # wifi is coordinator-only and optional — no hard validation

        if errors:
            raise ValueError("Config invalid: " + "; ".join(errors))

    def _validate_version(self, errors):
        v = self._config.get("version", "")
        if not isinstance(v, str) or not v:
            errors.append("version string required")
            return
        parts = v.split(".")
        if len(parts) < 2:
            errors.append(f"version must be major.minor, got: {v}")
            return
        if parts[0] != _MAJOR_VERSION:
            raise SafeModeError(
                f"Config major version mismatch: expected {_MAJOR_VERSION}, got {parts[0]}"
            )

    def _validate_system(self, errors):
        s = self._config.get("system", {})
        if not isinstance(s, dict):
            errors.append("system must be a dict"); return

        role = s.get("role", "")
        if role not in ("coordinator", "leaf"):
            errors.append("system.role must be 'coordinator' or 'leaf'")

        uid = s.get("unit_id")
        if not isinstance(uid, int) or uid < 0 or uid > 8:
            errors.append("system.unit_id must be int 0–8")

        if role == "coordinator" and uid != 0:
            errors.append("coordinator must have unit_id 0")

        if role == "leaf" and uid == 0:
            errors.append("leaf unit_id must be 1–8")

        hb = s.get("heartbeat_interval_s", 30)
        if not isinstance(hb, int) or hb < 5 or hb > 3600:
            errors.append("system.heartbeat_interval_s must be int 5–3600")

        hbt = s.get("heartbeat_timeout_s", 120)
        if not isinstance(hbt, int) or hbt < hb:
            errors.append("system.heartbeat_timeout_s must be >= heartbeat_interval_s")

        upd = s.get("pwm_update_interval_ms", 500)
        if not isinstance(upd, int) or upd < 100 or upd > 60000:
            errors.append("system.pwm_update_interval_ms must be int 100–60000")

        ll = s.get("log_level", "INFO")
        if ll not in ("FATAL", "ERROR", "WARN", "INFO", "DEBUG"):
            errors.append("system.log_level must be FATAL/ERROR/WARN/INFO/DEBUG")

    def _validate_timezone(self, errors):
        tz = self._config.get("timezone", {})
        if not isinstance(tz, dict):
            errors.append("timezone must be a dict"); return
        offset = tz.get("utc_offset_hours", 0)
        if not isinstance(offset, (int, float)) or offset < -12 or offset > 14:
            errors.append("timezone.utc_offset_hours must be -12 to +14")

    def _validate_hardware(self, errors):
        hw = self._config.get("hardware", {})
        if not isinstance(hw, dict):
            errors.append("hardware must be a dict"); return

        for key in ("i2c_sda_pin", "i2c_scl_pin", "ldr_adc_pin",
                    "status_led_pin", "reset_btn_pin",
                    "lora_tx_pin", "lora_rx_pin", "lora_m0_pin",
                    "lora_m1_pin", "lora_aux_pin"):
            v = hw.get(key)
            if not isinstance(v, int) or v not in _VALID_PINS:
                errors.append(f"hardware.{key} must be valid GPIO 0–28")

        freq = hw.get("pwm_freq_hz", 1000)
        if not isinstance(freq, int) or freq < 1 or freq > 40000000:
            errors.append("hardware.pwm_freq_hz must be 1–40000000")

        i2c = hw.get("i2c_freq_hz", 400000)
        if not isinstance(i2c, int) or i2c not in (100000, 400000, 1000000):
            errors.append("hardware.i2c_freq_hz must be 100000, 400000, or 1000000")

    def _validate_lora(self, errors):
        lora = self._config.get("lora", {})
        if not isinstance(lora, dict):
            errors.append("lora must be a dict"); return
        if not isinstance(lora.get("enabled"), bool):
            errors.append("lora.enabled must be bool")
        freq = lora.get("frequency_mhz", 868)
        if not isinstance(freq, (int, float)) or freq < 800 or freq > 930:
            errors.append("lora.frequency_mhz must be 800–930")
        pwr = lora.get("tx_power_dbm", 22)
        if not isinstance(pwr, int) or pwr < 0 or pwr > 22:
            errors.append("lora.tx_power_dbm must be 0–22")

    def _validate_ldr(self, errors):
        ldr = self._config.get("ldr", {})
        if not isinstance(ldr, dict):
            errors.append("ldr must be a dict"); return
        if not isinstance(ldr.get("enabled"), bool):
            errors.append("ldr.enabled must be bool")
        sw = ldr.get("smoothing_window_s", 60)
        if not isinstance(sw, int) or sw < 1 or sw > 3600:
            errors.append("ldr.smoothing_window_s must be int 1–3600")
        rules = ldr.get("cap_rules", [])
        if not isinstance(rules, list):
            errors.append("ldr.cap_rules must be a list"); return
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                errors.append(f"ldr.cap_rules[{i}] must be a dict"); continue
            ab = r.get("above_percent")
            cp = r.get("cap_percent")
            if not isinstance(ab, (int, float)) or not 0 <= ab <= 100:
                errors.append(f"ldr.cap_rules[{i}].above_percent must be 0–100")
            if not isinstance(cp, (int, float)) or not 0 <= cp <= 100:
                errors.append(f"ldr.cap_rules[{i}].cap_percent must be 0–100")

    def _validate_pir(self, errors):
        pirs = self._config.get("pir", [])
        if not isinstance(pirs, list):
            errors.append("pir must be a list"); return
        if len(pirs) > 4:
            errors.append("pir: maximum 4 entries")
        seen_ids = set()
        seen_pins = set()
        valid_pins = {6, 7, 8, 9}
        for i, p in enumerate(pirs):
            if not isinstance(p, dict):
                errors.append(f"pir[{i}] must be a dict"); continue
            pid = p.get("id", "")
            if not pid:
                errors.append(f"pir[{i}].id required")
            elif pid in seen_ids:
                errors.append(f"pir[{i}].id '{pid}' duplicate")
            seen_ids.add(pid)
            pin = p.get("gpio_pin")
            if pin not in valid_pins:
                errors.append(f"pir[{i}].gpio_pin must be one of {sorted(valid_pins)}")
            elif pin in seen_pins:
                errors.append(f"pir[{i}].gpio_pin {pin} duplicate")
            seen_pins.add(pin)
            if not isinstance(p.get("enabled"), bool):
                errors.append(f"pir[{i}].enabled must be bool")
            timeout = p.get("vacancy_timeout_s", 60)
            if not isinstance(timeout, int) or timeout < 1:
                errors.append(f"pir[{i}].vacancy_timeout_s must be positive int")
            for field in ("on_motion", "on_vacancy"):
                action = p.get(field, {})
                if not isinstance(action, dict):
                    errors.append(f"pir[{i}].{field} must be a dict")
                    continue
                self._validate_pir_action(action, f"pir[{i}].{field}", errors)

    def _validate_pir_action(self, action, path, errors):
        valid = ("set_scene", "set_led_channels", "set_relay", "revert_to_schedule")
        act = action.get("action", "")
        if act not in valid:
            errors.append(f"{path}.action must be one of {valid}")
        if act == "set_led_channels":
            channels = action.get("channels", [])
            if not isinstance(channels, list) or not channels:
                errors.append(f"{path}.channels must be non-empty list")
            duty = action.get("duty_percent")
            if not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
                errors.append(f"{path}.duty_percent must be 0–100")

    def _validate_relays(self, errors):
        relays = self._config.get("relays", [])
        if not isinstance(relays, list):
            errors.append("relays must be a list"); return
        if len(relays) > 2:
            errors.append("relays: maximum 2 entries")
        seen_ids = set()
        valid_pins = {10, 11}
        seen_pins = set()
        for i, r in enumerate(relays):
            if not isinstance(r, dict):
                errors.append(f"relays[{i}] must be a dict"); continue
            rid = r.get("id", "")
            if not rid:
                errors.append(f"relays[{i}].id required")
            elif rid in seen_ids:
                errors.append(f"relays[{i}].id '{rid}' duplicate")
            seen_ids.add(rid)
            pin = r.get("gpio_pin")
            if pin not in valid_pins:
                errors.append(f"relays[{i}].gpio_pin must be one of {sorted(valid_pins)}")
            elif pin in seen_pins:
                errors.append(f"relays[{i}].gpio_pin {pin} duplicate")
            seen_pins.add(pin)
            if not isinstance(r.get("enabled"), bool):
                errors.append(f"relays[{i}].enabled must be bool")
            if r.get("default_state") not in ("on", "off"):
                errors.append(f"relays[{i}].default_state must be 'on' or 'off'")
            for j, w in enumerate(r.get("time_windows", [])):
                self._validate_relay_window(w, f"relays[{i}].time_windows[{j}]", errors)

    def _validate_relay_window(self, w, path, errors):
        if not isinstance(w, dict):
            errors.append(f"{path} must be a dict"); return
        if not self._valid_time(w.get("start")):
            errors.append(f"{path}.start invalid")
        if not self._valid_time(w.get("end")):
            errors.append(f"{path}.end invalid")
        if w.get("state") not in ("on", "off"):
            errors.append(f"{path}.state must be 'on' or 'off'")

    def _validate_led_channels(self, errors):
        channels = self._config.get("led_channels", [])
        if not isinstance(channels, list):
            errors.append("led_channels must be a list"); return
        if len(channels) > 8:
            errors.append("led_channels: maximum 8 entries")
        if not channels:
            errors.append("led_channels must have at least one entry"); return

        valid_pins = {13, 14, 15, 16, 17, 18, 19, 22}
        seen_ids = set()
        seen_pins = set()

        for i, ch in enumerate(channels):
            if not isinstance(ch, dict):
                errors.append(f"led_channels[{i}] must be a dict"); continue
            cid = ch.get("id", "")
            if not cid:
                errors.append(f"led_channels[{i}].id required")
            elif cid in seen_ids:
                errors.append(f"led_channels[{i}].id '{cid}' duplicate")
            seen_ids.add(cid)

            pin = ch.get("gpio_pin")
            if pin not in valid_pins:
                errors.append(
                    f"led_channels[{i}].gpio_pin must be one of {sorted(valid_pins)}"
                )
            elif pin in seen_pins:
                errors.append(f"led_channels[{i}].gpio_pin {pin} duplicate")
            seen_pins.add(pin)

            if not isinstance(ch.get("enabled"), bool):
                errors.append(f"led_channels[{i}].enabled must be bool")

            d = ch.get("default_duty_percent", 0)
            if not isinstance(d, (int, float)) or not 0 <= d <= 100:
                errors.append(f"led_channels[{i}].default_duty_percent must be 0–100")

            for j, w in enumerate(ch.get("time_windows", [])):
                self._validate_led_window(w, f"led_channels[{i}].time_windows[{j}]", errors)

    def _validate_led_window(self, w, path, errors):
        if not isinstance(w, dict):
            errors.append(f"{path} must be a dict"); return
        if not self._valid_time(w.get("start")):
            errors.append(f"{path}.start invalid")
        if not self._valid_time(w.get("end")):
            errors.append(f"{path}.end invalid")
        duty = w.get("duty_percent")
        if not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
            errors.append(f"{path}.duty_percent must be 0–100")
        fade = w.get("fade_ms", 0)
        if not isinstance(fade, int) or fade < 0:
            errors.append(f"{path}.fade_ms must be non-negative int")

    def _validate_scenes(self, errors):
        scenes = self._config.get("scenes", [])
        if not isinstance(scenes, list):
            errors.append("scenes must be a list"); return
        seen = set()
        for i, s in enumerate(scenes):
            if not isinstance(s, dict):
                errors.append(f"scenes[{i}] must be a dict"); continue
            name = s.get("name", "")
            if not name:
                errors.append(f"scenes[{i}].name required")
            elif name in seen:
                errors.append(f"scenes[{i}].name '{name}' duplicate")
            seen.add(name)
            for entry in s.get("led_channels", []):
                if not isinstance(entry, dict) or "id" not in entry:
                    errors.append(f"scenes[{i}] led_channels entry missing id")
                duty = entry.get("duty_percent")
                if not isinstance(duty, (int, float)) or not 0 <= duty <= 100:
                    errors.append(f"scenes[{i}] led entry duty_percent must be 0–100")
            for entry in s.get("relays", []):
                if not isinstance(entry, dict) or "id" not in entry:
                    errors.append(f"scenes[{i}] relays entry missing id")
                if entry.get("state") not in ("on", "off"):
                    errors.append(f"scenes[{i}] relay entry state must be 'on' or 'off'")

    def _valid_time(self, t):
        if not isinstance(t, str):
            return False
        low = t.strip().lower()
        if low in ("sunrise", "sunset"):
            return True
        try:
            parts = t.split(":")
            if len(parts) != 2:
                return False
            h, m = int(parts[0]), int(parts[1])
            return 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, IndexError):
            return False


# Module-level singleton
config_manager = ConfigManager()
