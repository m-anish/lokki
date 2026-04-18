"""
Simple logging module for MicroPython with log levels and timestamps.

Logs messages with levels: FATAL, ERROR, WARN, INFO, DEBUG.

The logging level is taken from the config module by default.

Timestamps are formatted as:
<Weekday> <DD> <Mon> <YYYY> - hh:mm:ss <TIMEZONE_NAME>(UTC±offset),
e.g. <Mon 18 Aug 2025 - 15:55:10 IST(UTC+5:30)>
"""

from lib.config_manager import LOG_LEVEL, TIMEZONE_NAME, TIMEZONE_OFFSET
from lib.rtc_shared import rtc

class Logger:
    """
    A simple logger supporting multiple severity levels and timestamps.

    Levels:
        FATAL (0) - Critical errors causing shutdown
        ERROR (1) - Errors preventing function
        WARN  (2) - Warnings of potential issues
        INFO  (3) - General informational messages
        DEBUG (4) - Detailed debugging information

    Args:
        level (str): Minimum logging level to output. Default is from config.

    Methods:
        fatal(msg), error(msg), warn(msg), info(msg), debug(msg): Log messages
        at corresponding levels.
    """

    LEVELS = {'FATAL': 0, 'ERROR': 1, 'WARN': 2, 'INFO': 3, 'DEBUG': 4}

    WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def __init__(self, level=None):
        self.level = self.LEVELS.get(level or LOG_LEVEL, 3)
        self.rtc = rtc

    def _format_offset(self):
        """
        Format the timezone offset (float hours) to a string like '+5:30' or
        '-4:00'.
        """
        total_minutes = int(abs(TIMEZONE_OFFSET) * 60)
        sign = '+' if TIMEZONE_OFFSET >= 0 else '-'
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{sign}{hours}:{minutes:02d}"

    def _timestamp(self):
        """
        Return current timestamp string formatted as:
        <Weekday> <DD> <Mon> <YYYY> - hh:mm:ss <TIMEZONE_NAME>(UTC±offset)

        Obtains time from DS3231 RTC to ensure local time with offset.

        e.g. <Mon 18 Aug 2025 - 15:55:10 IST(UTC+5:30)>
        """
        dt = self.rtc.datetime()
        year = dt.year
        month = dt.month
        day = dt.day
        hour = dt.hour
        minute = dt.minute
        second = dt.second
        # urtc weekday: Monday=1,...Sunday=7; map to 0-based
        weekday_idx = dt.weekday - 1

        weekday_str = self.WEEKDAYS[weekday_idx if 0 <= weekday_idx < 7 else 0]
        month_str = self.MONTHS[month-1] if 1 <= month <= 12 else "Jan"

        offset_str = self._format_offset()

        return "<{} {:02d} {} {:04d} - {:02d}:{:02d}:{:02d} {}(UTC{})>".format(
            weekday_str, day, month_str, year, hour, minute, second,
            TIMEZONE_NAME, offset_str)

    def log(self, level, msg):
        """
        Log a message if the level is at or above the configured threshold.

        Args:
            level (str): Logging level of the message.
            msg (str): The message to log.
        """
        lvl_value = self.LEVELS.get(level, 3)
        if lvl_value <= self.level:
            print("{} {}: {}".format(self._timestamp(), level, msg))

    def fatal(self, msg):
        """Log message with FATAL level."""
        self.log('FATAL', msg)

    def error(self, msg):
        """Log message with ERROR level."""
        self.log('ERROR', msg)

    def warn(self, msg):
        """Log message with WARN level."""
        self.log('WARN', msg)

    def info(self, msg):
        """Log message with INFO level."""
        self.log('INFO', msg)

    def debug(self, msg):
        """Log message with DEBUG level."""
        self.log('DEBUG', msg)
