try:
    from core.config_manager import config_manager as _cm
    _sys = _cm.get("system")
    _tz  = _cm.get("timezone")
    _LOG_LEVEL      = _sys.get("log_level", "INFO")
    _TIMEZONE_NAME  = _tz.get("name", "UTC")
    _TIMEZONE_OFFSET = _tz.get("utc_offset_hours", 0.0)
except Exception:
    _LOG_LEVEL, _TIMEZONE_NAME, _TIMEZONE_OFFSET = "INFO", "UTC", 0.0

try:
    from hardware.rtc_shared import rtc as _rtc
except Exception:
    _rtc = None

# event_bus has no firmware deps, so this import is safe even at early boot.
# The Logger tees every line into it so the dashboard's Logs view and the
# notification system can render coordinator activity without us sprinkling
# bus.push() calls across the codebase.
try:
    from shared.event_bus import event_bus as _bus
except Exception:
    _bus = None


class Logger:

    LEVELS   = {"FATAL": 0, "ERROR": 1, "WARN": 2, "INFO": 3, "DEBUG": 4}
    WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    MONTHS   = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def __init__(self, level=None):
        self.level = self.LEVELS.get(level or _LOG_LEVEL, 3)

    def _offset_str(self):
        total = int(abs(_TIMEZONE_OFFSET) * 60)
        sign  = "+" if _TIMEZONE_OFFSET >= 0 else "-"
        return f"{sign}{total // 60}:{total % 60:02d}"

    def _timestamp(self):
        # Prefer the DS3231 when it's healthy (it survives power cycles
        # via its battery), but fall back to the MCU's internal clock
        # when the I2C read fails. NTP on the coord and the LoRa TS
        # broadcast on leaves keep `time.localtime()` accurate, so log
        # timestamps stay readable through a flaky RTC.
        if _rtc is not None:
            try:
                dt = _rtc.datetime()
                wd = self.WEEKDAYS[(dt.weekday - 1) % 7]
                mo = self.MONTHS[dt.month - 1]
                return "<{} {:02d} {} {:04d} - {:02d}:{:02d}:{:02d} {}(UTC{})>".format(
                    wd, dt.day, mo, dt.year,
                    dt.hour, dt.minute, dt.second,
                    _TIMEZONE_NAME, self._offset_str()
                )
            except Exception:
                pass
        try:
            import time
            lt = time.localtime()
            wd = self.WEEKDAYS[lt[6] % 7]
            mo = self.MONTHS[lt[1] - 1]
            return "<{} {:02d} {} {:04d} - {:02d}:{:02d}:{:02d} {}(UTC{})>".format(
                wd, lt[2], mo, lt[0],
                lt[3], lt[4], lt[5],
                _TIMEZONE_NAME, self._offset_str()
            )
        except Exception:
            return "<no-rtc>"

    def _log(self, level, msg, tag=None):
        if self.LEVELS.get(level, 3) <= self.level:
            print(self._timestamp(), level + ":", msg)
            if _bus is not None:
                try:
                    _bus.push(level, msg, tag=tag)
                except Exception:
                    # Bus failures must never break logging itself.
                    pass

    def fatal(self, m): self._log("FATAL", m)
    def error(self, m): self._log("ERROR", m)
    def warn(self,  m): self._log("WARN",  m)
    def info(self,  m): self._log("INFO",  m)
    def debug(self, m): self._log("DEBUG", m)

    def activity(self, m):
        """User-action / fleet-state-change events that should appear
        in the dashboard's Activity view (UX-4.2). Logged at INFO and
        tagged so the dashboard can filter for "what changed today?"
        Cost on Pico: one short-string tag per event in the ring
        (~10 bytes). Reserved for endpoints that represent an
        operator intent or a state-changing outcome — manual
        overrides, config pushes, scene applies, claims, reboots,
        time-sync overrides. Don't tag every INFO line."""
        self._log("INFO", m, tag="activity")
