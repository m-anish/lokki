"""On-device sunrise/sunset computation from (lat, lon, day-of-year, tz).

Implements NOAA's solar position algorithm — same equations used by the
US National Weather Service's online calculator. Accurate to within a
minute for typical mid-latitude locations. Cheap enough for MicroPython
that schedule_engine can call it once per day per unit at the cost of a
few dozen float ops.

Why on-device instead of a flashed sun_times.json file:
  - Lat/lon doesn't change for a deployed venue, but day-of-year does.
    Recomputing daily from (lat, lon) is more accurate than weekly
    JSON buckets, and the JSON would need an annual refresh otherwise.
  - Removes one file from flash, one operational step at install.
  - The dashboard can edit `config.location.lat/lon` like any other
    field; CFG_PATCH propagates to leaves; each leaf computes locally.

Algorithm reference:
  Spencer, J.W. (1971). Fourier series representation of the position
  of the sun. Search, 2(5), 172.
  Plus NOAA's refraction + zenith corrections.

Pure functional — no global state, no I/O. Caller decides when to call
(typically once per day from the schedule engine's `_get_rise_set`
cache).
"""

import math


# Solar zenith for "official" sunrise/sunset (90° + atmospheric
# refraction). NOAA uses 90.833° — the centre of the sun's disc when
# the upper limb is on the horizon, with average refraction.
_ZENITH_DEG = 90.833


def _day_of_year(year, month, day):
    """1-based day-of-year. Handles leap years."""
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        days_in_month[1] = 29
    doy = day
    for m in range(month - 1):
        doy += days_in_month[m]
    return doy


def compute(year, month, day, lat, lon, tz_offset_hours):
    """Compute sunrise/sunset for a given date and location.

    Args:
        year:               4-digit year (e.g. 2026)
        month:              1-12
        day:                1-31
        lat:                degrees, positive north
        lon:                degrees, positive east
        tz_offset_hours:    local time offset from UTC (e.g. 5.5 for IST)

    Returns:
        (rise_h, rise_m, set_h, set_m) tuple of ints, all in LOCAL time.
        Returns None if the sun doesn't rise/set on this date at this
        latitude (polar night / midnight sun) — caller should fall back
        to a sensible default.

    Float precision is plenty for minute-level scheduling. Don't expect
    second accuracy.
    """
    doy = _day_of_year(year, month, day)

    # Fractional year in radians. The +0.5 centres the calc on solar
    # noon rather than midnight; matches NOAA's online calculator
    # output to <1 minute for typical use.
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + 0.5)

    # Equation of time, in minutes. Apparent solar time vs mean solar
    # time — peaks at ±16 minutes through the year.
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )

    # Solar declination, in radians.
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.001480 * math.sin(3.0 * gamma)
    )

    lat_rad = math.radians(lat)
    zenith_rad = math.radians(_ZENITH_DEG)

    # Hour angle, in radians.
    # cos(ha) = (cos(zenith) - sin(lat)*sin(decl)) / (cos(lat)*cos(decl))
    # acos's argument can exceed [-1, 1] near the poles → no sunrise
    # or no sunset on this date.
    cos_ha_num = math.cos(zenith_rad) - math.sin(lat_rad) * math.sin(decl)
    cos_ha_den = math.cos(lat_rad) * math.cos(decl)
    if cos_ha_den == 0:
        return None
    cos_ha = cos_ha_num / cos_ha_den
    if cos_ha > 1.0 or cos_ha < -1.0:
        # Polar night (sun never rises) or midnight sun (never sets).
        return None
    ha = math.degrees(math.acos(cos_ha))

    # Sunrise/sunset in UTC minutes-since-midnight. The formula:
    #   720 = solar noon at lon=0 (12:00 UTC)
    #   4 minutes per degree of longitude (24*60/360)
    # minus the eqtime correction, ± the hour angle.
    sunrise_utc_min = 720.0 - 4.0 * (lon + ha) - eqtime
    sunset_utc_min  = 720.0 - 4.0 * (lon - ha) - eqtime

    # Apply timezone, then wrap to a 0..1440 day.
    sunrise_local = sunrise_utc_min + tz_offset_hours * 60.0
    sunset_local  = sunset_utc_min  + tz_offset_hours * 60.0
    sunrise_local = sunrise_local % (24 * 60)
    sunset_local  = sunset_local  % (24 * 60)

    rise_h = int(sunrise_local // 60)
    rise_m = int(sunrise_local % 60)
    set_h  = int(sunset_local  // 60)
    set_m  = int(sunset_local  % 60)
    return rise_h, rise_m, set_h, set_m
