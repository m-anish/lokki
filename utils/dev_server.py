#!/usr/bin/env python3
"""Throwaway dev server for previewing the dashboard locally.

Serves web/app/* and stubs out the /api/* endpoints with canned
responses derived from the sample config. No firmware, no LoRa.
Just enough to render the UI and iterate on visual changes
(e.g. UX-3 schedule strip mockup) without flashing a Pico.

    python3 utils/dev_server.py
    open http://localhost:8088/dashboard.html

Stop with Ctrl-C. This script is intentionally not wired into
update.sh — it's a local-only preview, never goes on device.
"""
import json
import time
import os
import http.server
import socketserver
from urllib.parse import urlparse

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WWW_DIR   = os.path.join(ROOT, "web", "app")
SAMPLE    = os.path.join(ROOT, "firmware", "micropython", "src",
                         "config", "samples", "config.json.sample")
PORT      = 8088
BOOT_TIME = time.time()

with open(SAMPLE) as f:
    COORD_CFG = json.load(f)

# Build a second, slightly different config for leaf 1 so the demo
# isn't just the coord twice.
LEAF1_CFG = json.loads(json.dumps(COORD_CFG))
LEAF1_CFG["system"]["role"]      = "leaf"
LEAF1_CFG["system"]["unit_id"]   = 1
LEAF1_CFG["system"]["unit_name"] = "Garden Leaf"
# Add a cross-midnight window so the operator can see split rendering.
LEAF1_CFG["led_channels"][1]["time_windows"] = [
    {"start": "22:00", "end": "05:00", "duty_percent": 35, "fade_ms": 8000},
    {"start": "sunrise", "end": "sunset", "duty_percent": 0},
]
# Give relay 2 some windows so the relay strip has something to show.
LEAF1_CFG["relays"][1]["enabled"] = True
LEAF1_CFG["relays"][1]["time_windows"] = [
    {"start": "06:00", "end": "09:00", "state": "on"},
    {"start": "17:30", "end": "23:00", "state": "on"},
]

CONFIGS = {0: COORD_CFG, 1: LEAF1_CFG}


def _positional_names(items, slots, prefix):
    out = [f"{prefix}{i+1}" for i in range(slots)]
    for x in (items or []):
        idx = x.get("id", 0) - 1
        if 0 <= idx < slots:
            out[idx] = x.get("name") or out[idx]
    return out


def _positional_enabled(items, slots):
    out = [False] * slots
    for x in (items or []):
        idx = x.get("id", 0) - 1
        if 0 <= idx < slots:
            out[idx] = bool(x.get("enabled"))
    return out


def _fleet_entry(uid, cfg):
    now = time.time()
    return {
        "name":            cfg["system"]["unit_name"],
        "online":          True,
        "last_seen_ago_s": 0 if uid == 0 else 4,
        "uptime":          int(now - BOOT_TIME) + (0 if uid == 0 else 3600),
        "ch":              [0, 40, 0, 0, 0, 0, 0, 0],
        "rl":              [0, 1],
        "pir":             [0, 0, 0, 0],
        "ldr":             62,
        "rtc_t":           29.5,
        "err":             0,
        "rssi":            None if uid == 0 else -78,
        "ch_names":   _positional_names(cfg.get("led_channels"), 8, "ch"),
        "ch_enabled": _positional_enabled(cfg.get("led_channels"), 8),
        "rl_names":   _positional_names(cfg.get("relays"),       2, "rl"),
        "rl_enabled": _positional_enabled(cfg.get("relays"),       2),
        "pir_names":  _positional_names(cfg.get("pir"),          4, "pir"),
        "pir_enabled":_positional_enabled(cfg.get("pir"),          4),
    }


def _ok(data):
    return 200, {"ok": True, "data": data}


def _route(path, method):
    """Returns (status, body_dict) or None if not an /api/ route."""
    p = urlparse(path).path
    if not p.startswith("/api/"):
        return None

    if p == "/api/status":
        # Mirror the firmware's status shape including the new AP
        # fields so the dashboard can render the AP-mode banner. To
        # preview AP fallback locally: set DEV_AP_MODE=1 in the env.
        ap_active = os.environ.get("DEV_AP_MODE") == "1"
        return _ok({
            "unit_name":   COORD_CFG["system"]["unit_name"],
            "time_synced": True,
            "uptime":      f"{int(time.time() - BOOT_TIME)}s",
            "connections": {
                "wifi":      not ap_active,
                "lora":      True,
                "mqtt":      False,
                "ap_active": ap_active,
                "ap_ip":     "192.168.4.1" if ap_active else None,
            },
        })

    if p == "/api/fleet":
        return _ok({
            "fleet":     {str(uid): _fleet_entry(uid, cfg) for uid, cfg in CONFIGS.items()},
            "unclaimed": {},
        })

    if p == "/api/config":
        return _ok(COORD_CFG)

    if p == "/api/events":
        return _ok({"events": [], "cursor": 0})

    if p.startswith("/api/units/"):
        # /api/units/N/{config|scenes|status|manual}
        parts = p.split("/")
        try:
            uid = int(parts[3])
        except (IndexError, ValueError):
            return 404, {"ok": False, "error": "bad unit id"}
        tail = parts[4] if len(parts) > 4 else ""

        if tail == "config":
            cfg = CONFIGS.get(uid)
            if cfg is None:
                return _ok({"source": "none"})
            out = dict(cfg)
            out["source"] = "live" if uid == 0 else "cached"
            return _ok(out)

        if tail == "scenes":
            cfg = CONFIGS.get(uid, COORD_CFG)
            return _ok([s["name"] for s in cfg.get("scenes", [])])

        # Stub anything else as "ok" so the dashboard doesn't error.
        return _ok({"stubbed": True})

    return 404, {"ok": False, "error": f"no stub for {p}"}


class DevHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kw):
        super().__init__(*args, directory=WWW_DIR, **kw)

    def _serve_api(self):
        result = _route(self.path, self.command)
        if result is None:
            return False
        status, body = result
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return True

    def do_GET(self):
        if self._serve_api():
            return
        # Redirect "/" to "/dashboard.html" so the user doesn't have to type it.
        if self.path == "/" or self.path == "":
            self.path = "/dashboard.html"
        return super().do_GET()

    def do_POST(self):
        if self._serve_api():
            return
        self.send_error(404)

    def do_PATCH(self):
        if self._serve_api():
            return
        self.send_error(404)

    def do_DELETE(self):
        if self._serve_api():
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        # Quieter logs — drop the noisy default per-request lines unless DEV_DEBUG=1.
        if os.environ.get("DEV_DEBUG"):
            super().log_message(fmt, *args)


def main():
    with socketserver.TCPServer(("127.0.0.1", PORT), DevHandler) as srv:
        srv.allow_reuse_address = True
        print(f"Dev server: http://localhost:{PORT}/dashboard.html")
        print("Stop with Ctrl-C.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nBye.")


if __name__ == "__main__":
    main()
