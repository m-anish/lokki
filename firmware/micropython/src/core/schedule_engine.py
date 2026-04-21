from hardware.rtc_module import get_current_time
from shared.sun_times import get_sunrise_sunset


class ScheduleEngine:

    def __init__(self):
        self._led_channels = []
        self._relays = []
        self._cached_date = None
        self._cached_rise = None
        self._cached_set = None

    def init_from_config(self, led_channels_cfg, relays_cfg):
        self._led_channels = led_channels_cfg
        self._relays = relays_cfg

    def get_desired_state(self):
        now = get_current_time()
        current_minutes = now[3] * 60 + now[4]
        rise_str, set_str = self._get_rise_set(now)

        state = {}

        for ch in self._led_channels:
            if not ch.get("enabled", False):
                continue
            cid = ch["id"]
            result = self._match_window(
                ch.get("time_windows", []),
                current_minutes, rise_str, set_str,
            )
            if result:
                state[cid] = {
                    "duty_percent": result.get("duty_percent", 0),
                    "fade_ms": result.get("fade_ms", 0),
                }
            else:
                state[cid] = {
                    "duty_percent": ch.get("default_duty_percent", 0),
                    "fade_ms": 0,
                }

        for r in self._relays:
            if not r.get("enabled", False):
                continue
            rid = r["id"]
            result = self._match_relay_window(
                r.get("time_windows", []),
                current_minutes, rise_str, set_str,
            )
            if result:
                state[rid] = {"state": result.get("state", "off")}
            else:
                state[rid] = {"state": r.get("default_state", "off")}

        return state

    # ------------------------------------------------------------------

    def _get_rise_set(self, now):
        date = (now[0], now[1], now[2])
        if self._cached_date != date:
            try:
                rh, rm, sh, sm = get_sunrise_sunset(now[1], now[2])
                self._cached_rise = f"{rh:02d}:{rm:02d}"
                self._cached_set  = f"{sh:02d}:{sm:02d}"
            except Exception:
                self._cached_rise = "06:30"
                self._cached_set  = "18:30"
            self._cached_date = date
        return self._cached_rise, self._cached_set

    def _resolve(self, time_str, rise_str, set_str):
        if not isinstance(time_str, str):
            return None
        low = time_str.strip().lower()
        if low == "sunrise":
            return rise_str
        if low == "sunset":
            return set_str
        return time_str

    def _window_active(self, start_str, end_str, current_minutes):
        try:
            sp = start_str.split(":")
            ep = end_str.split(":")
            s = int(sp[0]) * 60 + int(sp[1])
            e = int(ep[0]) * 60 + int(ep[1])
            if s > e:   # overnight
                return current_minutes >= s or current_minutes < e
            return s <= current_minutes < e
        except Exception:
            return False

    def _match_window(self, windows, current_minutes, rise_str, set_str):
        for w in windows:
            start = self._resolve(w.get("start"), rise_str, set_str)
            end   = self._resolve(w.get("end"),   rise_str, set_str)
            if start and end and self._window_active(start, end, current_minutes):
                return w
        return None

    def _match_relay_window(self, windows, current_minutes, rise_str, set_str):
        return self._match_window(windows, current_minutes, rise_str, set_str)


schedule_engine = ScheduleEngine()
