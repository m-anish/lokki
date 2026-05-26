import time
from hardware.rtc_shared import rtc, i2c
from shared.simple_logger import Logger

log = Logger()

# DS3231 die-temperature registers. The chip uses this internally to
# compensate its TCXO; it's a free die-temp reading off the same I2C
# bus, ±3 °C absolute accuracy, ~5–10 °C above ambient in a sealed
# enclosure. Updated by the chip every 64 s, so polling faster is a
# no-op.
_DS3231_ADDR     = 0x68
_REG_TEMP_MSB    = 0x11

# Throttle for repeated DS3231 read failures. The schedule task polls
# this function every ~500 ms, so one bad I2C read becomes ~120 log
# lines per minute without throttling. Five minutes between warnings
# is enough that the operator notices once on the dashboard but the
# log doesn't drown in it.
_RTC_WARN_THROTTLE_S = 300
_rtc_last_warn_s     = 0


def get_current_time():
    """Return (year, month, day, hour, minute, second, weekday).

    Primary source is the DS3231 over I2C. On I/O failure (flat backup
    battery, intermittent bus, ribbon-cable contention) we fall back
    to the MCU's internal clock — `time.localtime()` — which is kept
    in sync by NTP on the coord and by the LoRa TS broadcast on
    leaves. This keeps the schedule engine running through transient
    RTC failures instead of throwing on every tick. Repeated failures
    are logged at most once per _RTC_WARN_THROTTLE_S so a hardware
    fault doesn't drown the event bus.
    """
    global _rtc_last_warn_s
    try:
        dt = rtc.datetime()
        return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.weekday)
    except Exception as e:
        now = time.time()
        if now - _rtc_last_warn_s > _RTC_WARN_THROTTLE_S:
            log.warn(f"[RTC] DS3231 read failed ({e}); falling back to MCU clock")
            _rtc_last_warn_s = now
        lt = time.localtime()
        # localtime returns (Y, M, D, h, m, s, weekday, yearday).
        # Slice to match the DS3231 tuple shape; schedule_engine only
        # reads index 3 (hour) and 4 (minute) anyway, so weekday-
        # convention differences between urtc and time.localtime are
        # harmless in practice.
        return lt[:7]


def get_rtc_temp_c():
    """Read the DS3231's on-chip temperature sensor. Returns float
    °C (in 0.25 °C steps) or None on I2C failure.

    NOT room temperature — this is the die temp the chip uses for
    TCXO compensation. Typically reads 5–10 °C above ambient in a
    sealed enclosure. ±3 °C absolute accuracy, excellent stability,
    so it's most useful for trend detection ("the unit is heating
    up") rather than absolute readings. Free-of-charge — we already
    have an I2C bus to this chip.
    """
    try:
        raw = i2c.readfrom_mem(_DS3231_ADDR, _REG_TEMP_MSB, 2)
    except Exception:
        return None
    msb = raw[0]
    if msb & 0x80:
        msb -= 256
    frac = ((raw[1] >> 6) & 0x03) * 0.25
    return msb + frac
