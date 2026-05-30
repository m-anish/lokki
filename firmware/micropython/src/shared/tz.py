"""Timezone + DST helpers.

The base offset lives in `config.timezone.utc_offset_hours`. For DST-
observing regions, an optional `config.timezone.dst` block describes
the seasonal flip:

    {
      "offset_hours": 1.0,       # most regions; AU's Lord Howe Island is 0.5
      "start": { "month": 3,  "week_of_month": 2,  "day_of_week": 0, "hour": 2 },
      "end":   { "month": 11, "week_of_month": 1,  "day_of_week": 0, "hour": 2 }
    }

Conventions:
  - day_of_week: 0 = Sunday, 6 = Saturday (matches the way DST rules
    are written in most reference material — "second Sunday in March").
  - week_of_month: 1..4 for the nth occurrence of that weekday;
    -1 for the LAST occurrence (e.g. "last Sunday in October" → -1).
    Anything else falls back to 1.
  - hour: local wall-clock hour the transition happens at (typically 2).
  - Northern-hemisphere rules have start.month < end.month (DST is
    active "in between"); southern-hemisphere have start.month >
    end.month (DST is active "outside the gap"). `in_dst()` handles
    both by checking which side of each transition we're on.

If `dst` is missing or malformed, callers fall back to the base
offset and behaviour is identical to the pre-DST firmware.

Module is pure-functional, no I/O, no globals. Caller passes a date
tuple in (already-local) time; we just compute "is this moment in
DST?" given the rule.
"""


def _is_leap(year):
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _days_in_month(year, month):
    if month == 2 and _is_leap(year):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def _weekday(year, month, day):
    """Sunday=0, Saturday=6. Zeller's congruence, simplified.
    Avoids a `time.mktime` round-trip so it works pre-time-sync."""
    if month < 3:
        month += 12
        year -= 1
    h = (day
         + (13 * (month + 1)) // 5
         + year
         + year // 4
         - year // 100
         + year // 400) % 7
    # Zeller's h: 0=Sat, 1=Sun, ..., 6=Fri. Remap to 0=Sun..6=Sat.
    return (h + 6) % 7


def _nth_weekday_of_month(year, month, n, dow):
    """Day-of-month for the nth occurrence of weekday `dow` (0=Sun, 6=Sat)
    in `month` of `year`. n=1..4 for nth, n=-1 for last. Returns None
    if `month` doesn't actually contain that many of the requested
    weekday (n=5 in a 28-day Feb), so the caller can fall back.
    """
    if n is None:
        n = 1
    first_dow = _weekday(year, month, 1)
    # First occurrence of `dow` in this month.
    first_day = 1 + ((dow - first_dow) % 7)
    if n >= 1:
        candidate = first_day + (n - 1) * 7
        if candidate > _days_in_month(year, month):
            return None
        return candidate
    # n == -1 (or any negative) → "last" — start from end of month.
    dim = _days_in_month(year, month)
    last_dow = _weekday(year, month, dim)
    last_day = dim - ((last_dow - dow) % 7)
    return last_day


def _rule_transition_minute(year, rule):
    """Return the local minute-of-year at which a transition fires,
    given the rule dict and the current year. None on malformed rule."""
    try:
        month = int(rule["month"])
        n     = int(rule.get("week_of_month", 1))
        dow   = int(rule.get("day_of_week", 0))
        hour  = int(rule.get("hour", 2))
    except (KeyError, TypeError, ValueError):
        return None
    if not (1 <= month <= 12):
        return None
    day = _nth_weekday_of_month(year, month, n, dow)
    if day is None:
        # Fall back to the 1st occurrence if "last" / "n=5" didn't resolve.
        day = _nth_weekday_of_month(year, month, 1, dow)
        if day is None:
            return None
    return _minute_of_year(year, month, day, hour, 0)


def _minute_of_year(year, month, day, hour, minute):
    """Linear minute-since-Jan-1-00:00 for `year`."""
    m = 0
    for mm in range(1, month):
        m += _days_in_month(year, mm) * 1440
    m += (day - 1) * 1440
    m += hour * 60 + minute
    return m


def in_dst(now_tuple, dst_rule):
    """Is the given LOCAL time inside the DST period defined by the rule?

    now_tuple: 8- or 9-tuple from time.localtime() — at minimum
               (year, month, day, hour, minute, ...).
    dst_rule:  the dict from config.timezone.dst, or None/falsy.

    Returns False if no rule, malformed rule, or the moment isn't in DST.
    """
    if not dst_rule or not isinstance(dst_rule, dict):
        return False
    start_rule = dst_rule.get("start")
    end_rule   = dst_rule.get("end")
    if not (start_rule and end_rule):
        return False
    try:
        year, month, day, hour, minute = (
            now_tuple[0], now_tuple[1], now_tuple[2],
            now_tuple[3], now_tuple[4],
        )
    except (IndexError, TypeError):
        return False

    start_min = _rule_transition_minute(year, start_rule)
    end_min   = _rule_transition_minute(year, end_rule)
    if start_min is None or end_min is None:
        return False
    now_min = _minute_of_year(year, month, day, hour, minute)

    if start_min < end_min:
        # Northern hemisphere — DST is active between start and end.
        return start_min <= now_min < end_min
    # Southern hemisphere — DST wraps the year boundary, active
    # OUTSIDE the gap between end (in autumn) and start (in spring).
    return now_min >= start_min or now_min < end_min


def effective_offset_hours(timezone_cfg, now_tuple):
    """Return the local offset from UTC in hours for `now_tuple`,
    accounting for DST if `timezone_cfg.dst` is present.

    timezone_cfg: the dict from config_manager.get("timezone").
    now_tuple:    time.localtime() output (LOCAL wall-clock).

    Defensive: returns the base offset (or 0 if even that is missing)
    on any problem with the DST rule, so a malformed config can't
    drift the clock far enough to break scheduling outright.
    """
    if not isinstance(timezone_cfg, dict):
        return 0.0
    try:
        base = float(timezone_cfg.get("utc_offset_hours", 0))
    except (TypeError, ValueError):
        base = 0.0
    dst_rule = timezone_cfg.get("dst")
    if not dst_rule:
        return base
    try:
        if in_dst(now_tuple, dst_rule):
            try:
                return base + float(dst_rule.get("offset_hours", 1.0))
            except (TypeError, ValueError):
                return base + 1.0
    except Exception:
        return base
    return base
