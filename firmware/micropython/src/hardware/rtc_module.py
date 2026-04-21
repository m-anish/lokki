from hardware.rtc_shared import rtc


def get_current_time():
    """Return (year, month, day, hour, minute, second, weekday)."""
    dt = rtc.datetime()
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.weekday)
