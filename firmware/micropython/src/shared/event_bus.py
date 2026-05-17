"""In-memory event bus + ring buffer for dashboard observability.

The coordinator's Logger tees every log line here, and the LoRa ERR handler
pushes WARN/ERROR lines forwarded by leaves. The web layer reads events out
of this buffer to power the Logs view and the notification bell.

Design notes:
  - Single buffer, oldest-evicted-first, capped by entries (not bytes).
  - Each event has a monotonic `seq`, so the dashboard can ask "give me
    everything newer than N" without timestamps colliding across reboots.
  - The bus is import-time-safe: it has no firmware dependencies, so it can
    be imported by simple_logger without circular import concerns.
  - RAM cost: ~100 bytes per event × buffer_size. Default 100 events ≈ 10 KB.
    Tune via system.log_buffer_size in config.json.
"""

_DEFAULT_SIZE = 100
_MIN_SIZE     = 20
_MAX_SIZE     = 500


class EventBus:

    LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")

    def __init__(self):
        self._size   = _DEFAULT_SIZE
        self._buf    = []     # list used as a ring; we keep it bounded manually
        self._seq    = 0      # monotonic, never reset within a boot
        self._unit_id = 0     # which unit this bus belongs to (0 = coordinator)
        self._drops  = 0      # for diagnostics: number of events evicted

    def set_size(self, n):
        try:
            n = int(n)
        except Exception:
            return
        n = max(_MIN_SIZE, min(_MAX_SIZE, n))
        if n == self._size:
            return
        self._size = n
        # Trim if shrinking.
        if len(self._buf) > n:
            self._drops += (len(self._buf) - n)
            self._buf = self._buf[-n:]

    def set_unit_id(self, uid):
        try:
            self._unit_id = int(uid)
        except Exception:
            pass

    def push(self, level, msg, src=None, tag=None, ts=None):
        """Append an event. `src` defaults to this unit's id; `tag` is optional
        free-form (e.g. 'lora', 'config'). `ts` defaults to current epoch."""
        if level not in self.LEVELS:
            level = "INFO"
        self._seq += 1
        if ts is None:
            try:
                import time
                ts = int(time.time())
            except Exception:
                ts = 0
        evt = {
            "seq":   self._seq,
            "ts":    ts,
            "src":   self._unit_id if src is None else src,
            "level": level,
            "msg":   str(msg),
        }
        if tag:
            evt["tag"] = tag
        self._buf.append(evt)
        # Bounded ring: drop oldest if over capacity.
        if len(self._buf) > self._size:
            # MicroPython lists are O(n) on pop(0); accept that — bus pushes
            # are infrequent compared to LoRa traffic.
            self._buf.pop(0)
            self._drops += 1

    def events_since(self, since_seq=0, level=None, src=None, limit=200):
        """Return events with seq > since_seq matching optional filters.
        `level` is a minimum severity (e.g. 'WARN' → WARN, ERROR, FATAL)."""
        min_level_idx = 0
        if level is not None:
            try:
                min_level_idx = self.LEVELS.index(level)
            except ValueError:
                min_level_idx = 0
        try:
            since_seq = int(since_seq)
        except Exception:
            since_seq = 0

        out = []
        for evt in self._buf:
            if evt["seq"] <= since_seq:
                continue
            if min_level_idx and self.LEVELS.index(evt["level"]) < min_level_idx:
                continue
            if src is not None and evt["src"] != src:
                continue
            out.append(evt)
            if len(out) >= limit:
                break
        return out

    def stats(self):
        return {
            "size":     self._size,
            "count":    len(self._buf),
            "last_seq": self._seq,
            "drops":    self._drops,
            "min_size": _MIN_SIZE,
            "max_size": _MAX_SIZE,
        }


event_bus = EventBus()
