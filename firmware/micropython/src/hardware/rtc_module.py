"""
RTC module using the uRTC library's DS3231 driver.

Provides current date and time from the DS3231 real-time clock over I2C.

Note:
- Timezone offset is NOT applied here to avoid double offsetting,
  since the DS3231 RTC stores local time adjusted during write.
- Returns the raw datetime from the DS3231.

The time tuple format used is DateTimeTuple(year, month, day, weekday, hour,
minute, second, millisecond).

Requires: micropython-urtc library with DS3231 support.
"""

from simple_logger import Logger
from lib.rtc_shared import rtc

log = Logger()


def get_current_time():
    """
    Get current date and time directly from the DS3231 RTC without offset.

    Returns:
        tuple: (year, month, day, hour, minute, second, weekday)
            where weekday is 1=Monday ... 7=Sunday
    """
    # DateTimeTuple(year, month, day, weekday, hour, minute, second,
    #               millisecond)
    dt = rtc.datetime()

    year = dt.year
    month = dt.month
    day = dt.day
    hour = dt.hour
    minute = dt.minute
    second = dt.second
    weekday = dt.weekday

    log.debug("[RTC] Current time read (no offset): "
              "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} Weekday: {}"
              .format(year, month, day, hour, minute, second, weekday))

    return (year, month, day, hour, minute, second, weekday)
