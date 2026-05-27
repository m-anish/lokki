import json
import os
import gc


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
        self.safe_mode_reason = None
        try:
            self.load()
        except SafeModeError as e:
            # Capture rather than raise so module import doesn't fail.
            # main.py inspects safe_mode_reason at boot and enters safe_mode().
            self.safe_mode_reason = str(e)
            _log.error(f"Config load failed, will enter safe mode: {e}")

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

    def replace(self, new_config_str):
        try:
            candidate = json.loads(new_config_str)
        except Exception as e:
            raise ValueError(f"Invalid JSON: {e}")
        old = self._config
        self._config = candidate
        self._normalize_scenes()
        try:
            self._validate()
        except Exception:
            self._config = old
            raise
        self._atomic_write(self._config)

    # ------------------------------------------------------------------
    # Factory-reset to unclaimed leaf (unit_id = 99)
    # ------------------------------------------------------------------
    # Used by the long-press reset-button flow on leaves. Preserves the
    # fleet-wide LoRa, hardware, timezone, and (vestigial on leaves) wifi
    # sections so the freshly-reset leaf can still talk to the rest of the
    # fleet on the same channel/crypt — otherwise it'd be orphaned with
    # default LoRa settings and the operator would need a USB cable to
    # recover. Replaces system + everything user-configurable with
    # the unclaimed defaults.
    # ------------------------------------------------------------------

    _UNCLAIMED_SYSTEM = {
        "role":                   "leaf",
        "unit_id":                99,
        "unit_name":              "Unclaimed Leaf",
        "log_level":              "INFO",
        "log_buffer_size":        100,
        "heartbeat_interval_s":   30,
        "heartbeat_timeout_s":    120,
        "pwm_update_interval_ms": 500,
    }

    _UNCLAIMED_LED_CHANNELS = [
        {"id": i, "name": f"Channel {i}", "gpio_pin": pin,
         "enabled": False, "default_duty_percent": 0, "time_windows": []}
        for i, pin in zip(range(1, 9), (16, 17, 18, 19, 22, 15, 14, 13))
    ]

    def factory_reset_unclaimed(self):
        """Overwrite /config.json with a default 'unclaimed leaf' config.
        Preserves the lora/hardware/timezone/wifi sections from the
        current config so the leaf stays on the fleet's channel and key.
        Caller is responsible for calling machine.reset() afterwards."""
        old = self._config or {}
        new = {
            "version":       old.get("version", "1.0"),
            "system":        dict(self._UNCLAIMED_SYSTEM),
            "wifi":          old.get("wifi", {"ssid": "N/A", "password": ""}),
            "lora":          old.get("lora", {}),
            "timezone":      old.get("timezone", {"name": "UTC", "utc_offset_hours": 0}),
            "hardware":      old.get("hardware", {}),
            "ldr":           {"enabled": False, "smoothing_window_s": 60, "cap_rules": []},
            "pir":           [],
            "relays":        [],
            "led_channels":  [dict(ch) for ch in self._UNCLAIMED_LED_CHANNELS],
            "scenes":        [],
            "notifications": {"mqtt_enabled": False},
        }
        # We write directly without going through replace() / _validate(),
        # because the validator allows id=99 only for the "unclaimed"
        # role — and we're trusting our own defaults here. _atomic_write
        # still gives us power-loss safety via the tmp + rename dance.
        self._atomic_write(new)

    def _atomic_write(self, cfg):
        """Write config via tmp + rename to survive power loss mid-write.

        On a leaf with a fragmented heap (lots of small allocs from LoRa
        chunk reassembly + event bus + handlers), the old code path —
        prettify_json builds the entire formatted string in RAM, then
        f.write(...) takes another contiguous block — could fail to find a
        ~16 KB contiguous run and OOM with "memory allocation failed,
        allocating 16384 bytes". We now (1) gc.collect() right before
        touching disk so the longest free runs are available, and (2)
        stream-write the prettified output one token at a time, so the
        peak alloc is one short string instead of the full file."""
        gc.collect()
        tmp = self.config_file + ".tmp"
        with open(tmp, "w") as f:
            self._write_pretty(f, cfg)
        # Best-effort atomic swap. If rename isn't atomic on this VFS,
        # we still get a tmp file as fallback for manual recovery.
        try:
            os.remove(self.config_file)
        except OSError:
            pass
        os.rename(tmp, self.config_file)

    def _write_pretty(self, f, data):
        """Stream prettified JSON for `data` directly to file `f` without
        materializing the whole output in RAM. The `compact = json.dumps(data)`
        intermediate still costs one contiguous block (~13 KB for a leaf
        config) but that's the smallest we can do without a streaming JSON
        encoder, and crucially we no longer need a *second* contiguous block
        for the prettified copy."""
        compact = json.dumps(data)
        indent = 0
        in_string = False
        i = 0
        n = len(compact)
        while i < n:
            c = compact[i]
            if c == '"' and (i == 0 or compact[i-1] != '\\'):
                in_string = not in_string
                f.write(c)
            elif not in_string:
                if c == '{' or c == '[':
                    indent += 2
                    f.write(c)
                    f.write('\n')
                    f.write(' ' * indent)
                elif c == '}' or c == ']':
                    indent -= 2
                    f.write('\n')
                    f.write(' ' * indent)
                    f.write(c)
                elif c == ',':
                    f.write(c)
                    f.write('\n')
                    f.write(' ' * indent)
                elif c == ':':
                    f.write(c)
                    f.write(' ')
                else:
                    f.write(c)
            else:
                f.write(c)
            i += 1

    def prettify_json(self, data):
        """Backwards-compatible wrapper that returns the prettified string.
        Used by the web layer (/api/config) where the caller needs the bytes
        in hand to send over HTTP. On RAM-constrained leaves, prefer
        _write_pretty(f, data) which streams directly to disk."""
        compact = json.dumps(data)
        indent = 0
        in_string = False
        out = []
        i = 0
        n = len(compact)
        while i < n:
            c = compact[i]
            if c == '"' and (i == 0 or compact[i-1] != '\\'):
                in_string = not in_string
                out.append(c)
            elif not in_string:
                if c == '{' or c == '[':
                    indent += 2
                    out.append(c + '\n' + ' ' * indent)
                elif c == '}' or c == ']':
                    indent -= 2
                    out.append('\n' + ' ' * indent + c)
                elif c == ',':
                    out.append(c + '\n' + ' ' * indent)
                elif c == ':':
                    out.append(c + ' ')
                else:
                    out.append(c)
            else:
                out.append(c)
            i += 1
        return "".join(out)

    def _normalize_scenes(self):
        """Remove duplicate channel/relay entries within each scene (keep first)."""
        for scene in self._config.get("scenes", []):
            for key in ("led_channels", "relays"):
                entries = scene.get(key, [])
                seen = set()
                deduped = []
                for entry in entries:
                    eid = entry.get("id")
                    if eid is not None and eid not in seen:
                        seen.add(eid)
                        deduped.append(entry)
                scene[key] = deduped

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
    # Validation — JSON-Schema-driven + semantic-checks layer
    # ------------------------------------------------------------------
    # The authoritative type/range/enum spec lives in
    # `web/app/config.schema.json` (mirrored to `/config.schema.json`
    # on the device at flash time by update.sh). Everything that JSON
    # Schema can express runs through `schema_validator`; cross-field
    # / positional / uniqueness invariants run through
    # `semantic_checks` (e.g. led_channels[i].id == i+1, heartbeat_
    # timeout >= interval, scene-name uniqueness, role/unit_id
    # consistency). One special case stays here in Python: a major-
    # version mismatch raises SafeModeError (not a regular validation
    # error — the device can't safely apply a config from a different
    # major).

    _SCHEMA_PATH = "/config.schema.json"

    @classmethod
    def _load_schema(cls):
        """Read + parse the schema fresh each call. We deliberately do
        NOT cache the parsed dict in memory: the schema is ~5 KB and
        the resident dict pins enough heap to push the web server's
        large-response path (e.g. /api/events with a full log buffer)
        into the "free bytes plenty, contiguous block impossible"
        fragmentation zone on the Pico. Validation is rare (boot +
        each config replace + each /api/config/validate POST), so
        re-reading from flash is the right tradeoff. ~5 ms per call."""
        try:
            with open(cls._SCHEMA_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            # If the schema file is missing or broken, that's a
            # genuine emergency — every config flowing through us
            # depends on it. Refuse to validate rather than
            # silently accept everything.
            raise SafeModeError(f"config.schema.json unreadable: {e}")

    def _validate(self):
        # Version first — SafeModeError special case lives here because
        # a major-version mismatch is structurally unrecoverable.
        v = self._config.get("version", "")
        if not isinstance(v, str) or not v:
            raise ValueError("Config invalid: version string required")
        parts = v.split(".")
        if len(parts) < 2:
            raise ValueError(f"Config invalid: version must be major.minor, got: {v}")
        if parts[0] != _MAJOR_VERSION:
            raise SafeModeError(
                f"Config major version mismatch: expected {_MAJOR_VERSION}, got {parts[0]}"
            )

        # Schema-level validation first — types, ranges, enums,
        # required-property presence. Catches the vast majority of
        # errors with operator-friendly messages.
        from core import schema_validator
        schema = self._load_schema()
        errors = schema_validator.validate(self._config, schema)
        del schema             # free the ~5 KB parsed schema dict ASAP

        # Semantic / cross-field invariants the schema can't express.
        # Run AFTER schema validation so these checks can assume
        # basic types are already correct (they're defensive about
        # type mismatches anyway, but the error messages are less
        # noisy that way).
        from core import semantic_checks
        errors.extend(semantic_checks.check(self._config))

        if errors:
            raise ValueError("Config invalid: " + "; ".join(errors))

        # Schema parse + validation temporary allocations leave the
        # heap fragmented. Collect now so subsequent boot work (LoRa
        # buffers, WiFi connect, web server startup) sees a clean
        # heap rather than tripping on the leftovers.
        gc.collect()

    @classmethod
    def validate_candidate(cls, candidate):
        """Validate a config dict without applying it. Returns
        (ok, errors) where errors is a list of strings (empty when
        ok). Used by `POST /api/config/validate` so the dashboard
        can show errors inline before a save round-trip."""
        # Version pre-check — translates the SafeModeError special
        # case into a regular error in the return value so the API
        # caller gets a uniform error list.
        v = candidate.get("version") if isinstance(candidate, dict) else None
        if not isinstance(v, str) or not v:
            return False, ["version string required"]
        parts = v.split(".")
        if len(parts) < 2:
            return False, [f"version must be major.minor, got: {v}"]
        if parts[0] != _MAJOR_VERSION:
            return False, [
                f"version major mismatch: expected {_MAJOR_VERSION}, got {parts[0]}"
            ]
        try:
            from core import schema_validator, semantic_checks
            schema = cls._load_schema()
            errors = schema_validator.validate(candidate, schema)
            del schema         # free ~5 KB before semantic_checks runs
            errors.extend(semantic_checks.check(candidate))
        except SafeModeError as e:
            # Schema file missing or broken — surface to the API
            # caller rather than crashing the request handler.
            return False, [str(e)]
        gc.collect()           # validate calls happen mid-request; clear temps
        return (len(errors) == 0), errors


# Module-level singleton
config_manager = ConfigManager()
