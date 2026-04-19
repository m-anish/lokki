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
        if _rtc is None:
            return "<no-rtc>"
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
            return "<ts-err>"

    def _log(self, level, msg):
        if self.LEVELS.get(level, 3) <= self.level:
            print(self._timestamp(), level + ":", msg)

    def fatal(self, m): self._log("FATAL", m)
    def error(self, m): self._log("ERROR", m)
    def warn(self,  m): self._log("WARN",  m)
    def info(self,  m): self._log("INFO",  m)
    def debug(self, m): self._log("DEBUG", m)
