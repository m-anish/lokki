import asyncio
import socket
import json
import os
from core.config_manager import config_manager
from shared.system_status import system_status
from shared.simple_logger import Logger
import coordinator.api_handlers as api

log = Logger()

_PORT       = 80
_RECV_SIZE  = 512
_RECV_LOOPS = 32          # max read iterations per request
_BODY_MAX   = 8192        # max POST body bytes accepted
_STATIC_DIR = "/www"      # static web files root on the filesystem
_CHUNK_SIZE = 1024        # bytes per send chunk for static files

_MIME = {
    "html": "text/html; charset=utf-8",
    "js":   "application/javascript",
    "json": "application/json",
    "css":  "text/css",
    "ico":  "image/x-icon",
    "png":  "image/png",
    "svg":  "image/svg+xml",
}

_STATIC_PATHS = {
    "/index.html",
    "/config-builder.html",
}


class WebServer:

    def __init__(self):
        self.running = False
        self._server = None

    async def start_and_serve(self):
        try:
            self._server = socket.socket()
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("0.0.0.0", _PORT))
            self._server.listen(3)
            self._server.setblocking(False)
            self.running = True
            log.info(f"[WEB] Listening on port {_PORT}")
            system_status.set_connection_status(web_server=True)
        except Exception as e:
            log.error(f"[WEB] Failed to start: {e}")
            return

        while self.running:
            try:
                try:
                    conn, addr = self._server.accept()
                    asyncio.create_task(self._handle(conn, addr))
                except OSError:
                    pass
                await asyncio.sleep_ms(50)
            except Exception as e:
                log.error(f"[WEB] Accept error: {e}")
                await asyncio.sleep_ms(500)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def _handle(self, conn, addr):
        try:
            conn.setblocking(False)
            await asyncio.sleep_ms(10)

            raw = b""
            for _ in range(_RECV_LOOPS):
                try:
                    chunk = conn.recv(_RECV_SIZE)
                    if chunk:
                        raw += chunk
                    if b"\r\n\r\n" in raw:
                        break
                except OSError:
                    await asyncio.sleep_ms(20)

            method, path, headers, body = self._parse_request(raw)
            
            # If there's a Content-Length header, make sure we read the full body
            content_length = headers.get("content-length")
            if content_length:
                try:
                    expected_len = int(content_length)
                    while len(body) < expected_len and len(body) < _BODY_MAX:
                        try:
                            chunk = conn.recv(_RECV_SIZE)
                            if chunk:
                                body += chunk
                            else:
                                await asyncio.sleep_ms(20)
                        except OSError:
                            await asyncio.sleep_ms(20)
                except ValueError:
                    pass
            
            log.debug(f"[WEB] {method} {path} from {addr[0]}, body_len={len(body)}")

            # Static file serving (GET only)
            if method == "GET" and self._is_static(path):
                await self._serve_static(conn, path)
                return

            status, ctype, body_out = await self._route(method, path, headers, body)
            response = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {ctype}\r\n"
                "Connection: close\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type\r\n"
                "\r\n"
            ) + body_out
            await self._send_all(conn, response.encode())
        except Exception as e:
            log.error(f"[WEB] Handler error: {e}")
            import sys
            sys.print_exception(e)
            try:
                await self._send_all(
                    conn,
                    b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n",
                )
            except Exception:
                pass
        finally:
            conn.close()

    async def _send_all(self, conn, data):
        mv = memoryview(data)
        total = len(mv)
        sent = 0
        while sent < total:
            try:
                n = conn.send(mv[sent:])
                if n:
                    sent += n
                else:
                    await asyncio.sleep_ms(10)
            except OSError as e:
                if e.args and e.args[0] == 11:  # EAGAIN
                    await asyncio.sleep_ms(10)
                    continue
                raise

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def _is_static(self, path):
        if path in _STATIC_PATHS:
            return True
        if path.startswith("/vendor/"):
            return True
        return False

    async def _serve_static(self, conn, path):
        file_path = _STATIC_DIR + ("/index.html" if path == "/" else path)
        ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        ctype = _MIME.get(ext, "application/octet-stream")
        try:
            os.stat(file_path)
        except OSError:
            await self._send_all(
                conn,
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: text/plain\r\n"
                b"Connection: close\r\n\r\n"
                b"Not found",
            )
            return
        await self._send_all(
            conn,
            f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n"
            .encode(),
        )
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                await self._send_all(conn, chunk)
                await asyncio.sleep_ms(0)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _route(self, method, path, headers, body):
        # --- CORS preflight ---
        if method == "OPTIONS":
            return "204 No Content", "text/plain", ""

        # --- Static dashboard ---
        if path == "/" and method == "GET":
            return "200 OK", "text/html", self._dashboard_html()

        # --- Coordinator status (own unit) ---
        if path == "/api/status" and method == "GET":
            return self._json(api.handle_coordinator_status())

        if path == "/api/config" and method == "GET":
            return self._json(api.handle_full_config())

        if path == "/api/reboot" and method == "POST":
            return self._json(api.handle_reboot())

        # --- Fleet ---
        if path == "/api/fleet" and method == "GET":
            return self._json(api.handle_fleet_status())

        if path == "/api/sensors" and method == "GET":
            return self._json(api.handle_sensors())

        # --- Scenes ---
        if path == "/api/scenes" and method == "GET":
            return self._json(api.handle_list_scenes())

        if path.startswith("/api/scenes/") and method == "POST":
            scene_name = path[len("/api/scenes/"):]
            if scene_name.endswith("/apply"):
                scene_name = scene_name[:-len("/apply")]
            scene_name = self._url_decode(scene_name)
            parsed = self._parse_json_body(body)
            unit_ids = parsed.get("unit_ids") if parsed else None
            return self._json(api.handle_scene_apply(scene_name, unit_ids))

        if path == "/api/emergency-off" and method == "POST":
            return self._json(api.handle_emergency_off())

        # --- Per-unit endpoints  /api/units/<id>/... ---
        if path.startswith("/api/units/"):
            return await self._route_unit(method, path, body)

        return "404 Not Found", "application/json", '{"ok":false,"error":"not found"}'

    async def _route_unit(self, method, path, body):
        # parse /api/units/<id>[/sub]
        parts = path.split("/")          # ['', 'api', 'units', '<id>', ...]
        if len(parts) < 4:
            return "400 Bad Request", "application/json", '{"ok":false,"error":"bad path"}'
        try:
            unit_id = int(parts[3])
        except ValueError:
            return "400 Bad Request", "application/json", '{"ok":false,"error":"bad unit id"}'

        sub = parts[4] if len(parts) > 4 else ""

        if sub == "" and method == "GET":
            return self._json(api.handle_unit_status(unit_id))

        if sub == "config":
            if method == "GET":
                return self._json(api.handle_unit_config(unit_id))
            if method == "POST":
                cfg_str = body.decode("utf-8", "ignore") if body else ""
                result = await api.handle_config_push(unit_id, cfg_str)
                return self._json(result)

        if sub == "manual":
            if method == "POST":
                parsed = self._parse_json_body(body) or {}
                return self._json(api.handle_manual_override(unit_id, parsed))
            if method == "DELETE":
                return self._json(api.handle_manual_clear(unit_id))

        if sub == "status" and method == "POST":
            return self._json(api.handle_request_status(unit_id))

        if sub == "scenes" and method == "GET":
            return self._json(api.handle_unit_scenes(unit_id))

        return "404 Not Found", "application/json", '{"ok":false,"error":"not found"}'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _url_decode(self, s):
        res = []
        i = 0
        while i < len(s):
            if s[i] == '%' and i + 2 < len(s):
                try:
                    res.append(chr(int(s[i+1:i+3], 16)))
                    i += 3
                    continue
                except ValueError:
                    pass
            elif s[i] == '+':
                res.append(' ')
                i += 1
                continue
            res.append(s[i])
            i += 1
        return ''.join(res)

    def _json(self, result):
        status_code = result.pop("_status", 200) if isinstance(result, dict) else 200
        body = json.dumps(result)
        status_str = f"{status_code} OK" if status_code == 200 else f"{status_code} Error"
        return status_str, "application/json", body

    def _parse_request(self, raw):
        try:
            header_end = raw.find(b"\r\n\r\n")
            header_raw = raw[:header_end] if header_end >= 0 else raw
            body       = raw[header_end + 4:] if header_end >= 0 else b""
            lines      = header_raw.split(b"\r\n")
            
            # Handle empty or malformed requests
            if not lines or not lines[0]:
                return "GET", "/", {}, b""
            
            parts      = lines[0].decode().split(" ")
            method     = parts[0].upper() if len(parts) > 0 else "GET"
            
            # Fix: Add bounds check before accessing parts[1]
            if len(parts) > 1 and parts[1]:
                path_parts = parts[1].split("?")
                path = path_parts[0] if path_parts and path_parts[0] else "/"
            else:
                path = "/"
            
            headers    = {}
            for line in lines[1:]:
                if b":" in line:
                    k, _, v = line.partition(b":")
                    headers[k.strip().lower().decode()] = v.strip().decode()
            return method, path, headers, body[:_BODY_MAX]
        except Exception:
            return "GET", "/", {}, b""

    def _parse_json_body(self, body):
        try:
            return json.loads(body.decode("utf-8", "ignore")) if body else None
        except Exception:
            return None

    def _dashboard_html(self):
        try:
            unit_name_raw = config_manager.unit_name
            role_raw = config_manager.role
            unit_id_raw = config_manager.unit_id
            uptime_raw = system_status.get_uptime_string()
            
            log.debug(f"[WEB] Dashboard vars: name={unit_name_raw} ({type(unit_name_raw)}), role={role_raw} ({type(role_raw)}), id={unit_id_raw} ({type(unit_id_raw)}), uptime={uptime_raw} ({type(uptime_raw)})")
            
            unit_name = str(unit_name_raw or "Lokki")
            role      = str(role_raw or "coordinator")
            unit_id   = int(unit_id_raw or 0)
            uptime    = str(uptime_raw or "0s")
        except Exception as e:
            log.error(f"[WEB] Error getting dashboard vars: {e}")
            import sys
            sys.print_exception(e)
            unit_name = "Lokki"
            role = "coordinator"
            unit_id = 0
            uptime = "0s"
        
        # Log final values before using them
        log.info(f"[WEB] Final dashboard vars: name='{unit_name}' (type={type(unit_name).__name__}), id={unit_id} (type={type(unit_id).__name__}), uptime='{uptime}' (type={type(uptime).__name__})")
        
        try:
            # Build title separately to isolate any f-string issues
            title = "Lokki - " + str(unit_name)
            html = (
            "<!DOCTYPE html><html><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + title + "</title>"
            "<meta name='color-scheme' content='light dark'>"
            "<style>"
            "*, *::before, *::after{box-sizing:border-box}"
            ":root{--brand:#4f46e5;--brand-d:#3730a3;--bg:#f8fafc;--card:#ffffff;"
            "--border:#e2e8f0;--text:#0f172a;--muted:#64748b;--radius:10px;--green:#059669}"
            "body{margin:0;background:var(--bg);color:var(--text);"
            "font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "font-size:15px;line-height:1.5}"
            "nav{background:var(--card);border-bottom:1px solid var(--border);"
            "padding:0 24px;display:flex;align-items:center;gap:12px;height:56px}"
            ".nav-logo{font-size:1.1em;font-weight:700;color:var(--brand);"
            "letter-spacing:-.02em;text-decoration:none}"
            ".nav-logo span{color:var(--text);font-weight:400}"
            ".hero{background:linear-gradient(135deg,#312e81 0%,#4f46e5 60%,#7c3aed 100%);"
            "color:#fff;text-align:center;padding:40px 24px 36px}"
            ".hero h1{margin:0 0 10px;font-size:clamp(1.4em,4vw,2em);"
            "font-weight:800;letter-spacing:-.03em}"
            ".hero p{margin:0 auto;max-width:500px;font-size:.95em;opacity:.85}"
            "main{max-width:1000px;margin:0 auto;padding:24px 20px 60px}"
            ".unit-card{background:var(--card);border:1px solid var(--border);"
            "border-radius:var(--radius);margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.05)}"
            ".unit-head{display:flex;align-items:center;gap:12px;padding:16px 20px;"
            "border-bottom:1px solid var(--border)}"
            ".unit-name{font-size:1.1em;font-weight:700;flex:1}"
            ".unit-role{font-size:.75em;padding:3px 10px;border-radius:12px;"
            "background:#ede9fe;color:#5b21b6;font-weight:600;text-transform:uppercase}"
            ".unit-body{padding:16px 20px}"
            ".status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}"
            ".status-section{display:flex;flex-direction:column;gap:8px}"
            ".status-label{font-size:.75em;font-weight:600;color:var(--muted);"
            "text-transform:uppercase;letter-spacing:.05em}"
            ".led-indicators{display:flex;gap:6px;flex-wrap:wrap}"
            ".bswatch{width:28px;height:28px;border-radius:50%;border:1px solid #475569;"
            "display:inline-flex;align-items:center;justify-content:center;transition:all .2s;"
            "font-size:.65em;font-weight:700;color:#1e293b;text-shadow:0 0 2px rgba(255,255,255,0.8)}"
            ".relay-indicators{display:flex;gap:8px}"
            ".relay-ind{width:32px;height:20px;border-radius:10px;border:1px solid #475569;"
            "display:inline-flex;align-items:center;justify-content:center;font-size:.7em;"
            "font-weight:700;transition:all .2s}"
            ".relay-ind.on{background:#10b981;color:#fff;border-color:#10b981}"
            ".relay-ind.off{background:#e5e7eb;color:#6b7280;border-color:#9ca3af}"
            ".sensor-data{display:flex;gap:16px;flex-wrap:wrap;font-size:.9em}"
            ".sensor-item{display:flex;align-items:center;gap:6px}"
            ".sensor-value{font-weight:700;color:var(--text)}"
            ".conn-status{display:flex;gap:12px;font-size:.85em}"
            ".conn-item{display:flex;align-items:center;gap:4px}"
            ".conn-icon{width:8px;height:8px;border-radius:50%}"
            ".conn-icon.ok{background:#10b981}"
            ".conn-icon.err{background:#ef4444}"
            ".online{color:var(--green)}.offline{color:#dc2626}"
            ".btn{display:inline-block;padding:.5em 1em;border:1px solid var(--border);"
            "border-radius:6px;cursor:pointer;background:var(--card);font-size:.85em;"
            "font-weight:600;color:var(--text);transition:all .2s}"
            ".btn:hover{background:var(--brand);color:#fff;border-color:var(--brand)}"
            ".btn-primary{background:var(--brand);color:#fff;border-color:var(--brand)}"
            ".btn-primary:hover{background:var(--brand-d)}"
            ".modal{display:none;position:fixed;z-index:1000;left:0;top:0;width:100%;height:100%;"
            "background:rgba(0,0,0,0.5);overflow:auto}"
            ".modal-content{background:#fff;margin:5% auto;padding:20px;border-radius:8px;"
            "max-width:500px;box-shadow:0 4px 6px rgba(0,0,0,0.3)}"
            ".modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}"
            ".modal-header h2{margin:0;font-size:1.2em}"
            ".close{cursor:pointer;font-size:1.5em;font-weight:bold;color:#888}"
            ".close:hover{color:#000}"
            ".control-group{margin:15px 0;padding:10px;background:#f9f9f9;border-radius:4px}"
            ".control-group h3{margin:0 0 10px 0;font-size:1em;color:#555}"
            ".channel-control,.relay-control{margin:8px 0;padding:8px;background:#fff;border-radius:3px}"
            ".channel-control label{display:block;margin-bottom:5px;font-weight:bold;font-size:0.9em}"
            ".slider-container{display:flex;align-items:center;gap:10px}"
            ".slider{flex:1;height:6px;-webkit-appearance:none;appearance:none;background:#ddd;border-radius:3px;outline:none}"
            ".slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:18px;height:18px;"
            "background:#4CAF50;cursor:pointer;border-radius:50%}"
            ".slider::-moz-range-thumb{width:18px;height:18px;background:#4CAF50;cursor:pointer;border-radius:50%}"
            ".slider-value{min-width:45px;text-align:right;font-weight:bold;color:#333}"
            ".relay-buttons{display:flex;gap:5px}"
            ".relay-buttons .btn{flex:1;padding:5px 10px;font-size:0.85em}"
            ".relay-buttons .btn.active{background:#4CAF50;color:#fff;border-color:#4CAF50}"
            ".relay-toggle{position:relative;display:inline-block;width:50px;height:26px}"
            ".relay-toggle input{opacity:0;width:0;height:0}"
            ".toggle-slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;"
            "background:#ccc;border-radius:26px;transition:.3s}"
            ".toggle-slider:before{position:absolute;content:'';height:18px;width:18px;left:4px;bottom:4px;"
            "background:white;border-radius:50%;transition:.3s}"
            "input:checked+.toggle-slider{background:#10b981}"
            "input:checked+.toggle-slider:before{transform:translateX(24px)}"
            ".options{margin:15px 0;padding:10px;background:#f0f0f0;border-radius:4px}"
            ".options label{display:block;margin-bottom:10px;font-size:0.9em}"
            ".options input[type=number]{width:60px;padding:3px;margin-left:5px}"
            ".fade-slider-container{display:flex;align-items:center;gap:10px;margin-top:5px}"
            ".fade-slider{flex:1}"
            ".modal-actions{display:flex;gap:10px;margin-top:20px}"
            ".modal-actions .btn{flex:1;padding:10px;font-weight:bold}"
            ".preset-buttons{display:flex;gap:5px;margin-bottom:10px;flex-wrap:wrap}"
            ".preset-buttons .btn{flex:1;min-width:70px;font-size:0.85em}"
            "@media(max-width:768px){"
            ".status-grid{grid-template-columns:1fr}"
            ".unit-head{flex-wrap:wrap}"
            "}"
            "@media(prefers-color-scheme:dark){"
            ":root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#f1f5f9;"
            "--muted:#94a3b8;--brand:#818cf8;--brand-d:#6366f1;--green:#34d399}"
            "body{background:var(--bg);color:var(--text)}"
            "nav{background:var(--card);border-bottom-color:var(--border)}"
            ".unit-card{background:var(--card);border-color:var(--border)}"
            ".unit-head{border-bottom-color:var(--border)}"
            ".btn{background:#334155;border-color:#475569;color:var(--text)}"
            ".btn:hover{background:#475569}"
            ".modal-content{background:var(--card);color:var(--text)}"
            ".control-group{background:#1a2d45}"
            ".channel-control,.relay-control{background:#0f172a}"
            ".options{background:#1a2d45}"
            ".slider{background:#475569}"
            "}"
            "</style></head><body>"
            "<nav>"
            "<a href='/' class='nav-logo'>Lokki <span>- " + unit_name + "</span></a>"
            "<div style='margin-left:auto;display:flex;gap:16px;font-size:.85em'>"
            "<span id='currentTime' style='color:var(--muted)'></span>"
            "<span id='uptime' style='color:var(--muted)'>Uptime: " + uptime + "</span>"
            "</div>"
            "</nav>"
            "<div class='hero'>"
            "<h1>" + unit_name + "</h1>"
            "<p>Coordinator - Unit ID " + str(unit_id) + "</p>"
            "<div id='connStatus' style='margin-top:12px;font-size:.85em;opacity:.8'></div>"
            "</div>"
            "<main>"
            "<h2 style='font-size:1.2em;margin:0 0 16px;font-weight:700'>Fleet Status</h2>"
            "<div id='fleet'>Loading…</div>"
            "<div style='margin-top:40px;padding-top:20px;border-top:1px solid var(--border);font-size:.85em;color:var(--muted);display:flex;justify-content:space-between;align-items:center'>"
            "<div>"
            "<a href='/api/config' download='config.json' style='color:var(--brand)'>📥 Export Config</a> &middot; "
            "<a href='/index.html' style='color:var(--brand)'>📄 Documentation</a> &middot; "
            "<a href='/config-builder.html' style='color:var(--brand)'>⚙️ Config Builder</a> &middot; "
            "<button class='btn' onclick='rebootDevice()' style='color:#dc2626;border-color:#fca5a5'>🔄 Reboot</button>"
            "</div>"
            "<button class='btn' onclick='emergencyOff()' style='color:#dc2626;border-color:#fca5a5;font-size:.9em'>⚠️ Emergency Off</button>"
            "</div>"
            "</main>"
            "<!-- Manual Override Modal -->"
            "<div id='controlModal' class='modal'>"
            "<div class='modal-content'>"
            "<div class='modal-header'>"
            "<h2 id='modalTitle'>Manual Override</h2>"
            "<span class='close' onclick='closeModal()'>&times;</span>"
            "</div>"
            "<div id='modalBody'>Loading...</div>"
            "</div></div>"
            "<script>"
            "async function load(){"
            " const f=await fetch('/api/fleet').then(r=>r.json());"
            " const units=f.data||{};"
            " let h='';"
            " for(const[id,u] of Object.entries(units)){"
            "  const uptime=u.uptime||0;"
            "  const hrs=Math.floor(uptime/3600);"
            "  const mins=Math.floor((uptime%3600)/60);"
            "  const up=u.online?`${hrs}h ${mins}m`:'Offline';"
            "  const name=id==0?'" + unit_name + "':`Unit ${id}`;"
            "  const roleLabel=id==0?'Coordinator':'Leaf';"
            "  const statusCls=u.online?'online':'offline';"
            "  h+=`<div class=\"unit-card\">`;"
            "  h+=`<div class=\"unit-head\">`;"
            "  h+=`<span class=\"unit-name\">${name}</span>`;"
            "  h+=`<span class=\"unit-role\">${roleLabel}</span>`;"
            "  h+=`<div style=\"margin-left:auto;display:flex;gap:8px\">`;"
            "  h+=`<button class=\"btn\" onclick=\"openControlModal(${id},'${name}')\">Control</button>`;"
            "  h+=`<button class=\"btn\" onclick=\"reqStatus(${id})\">Refresh</button>`;"
            "  h+=`</div></div>`;"
            "  h+=`<div class=\"unit-body\"><div class=\"status-grid\">`;"
            "  h+=`<div class=\"status-section\"><div class=\"status-label\">LED Channels</div>`;"
            "  h+=`<div class=\"led-indicators\">`;"
            "  const ch=u.ch||[];"
            "  ch.forEach((v,i)=>{h+=`<span class=\"bswatch\" style=\"${swatchStyle(v)}\" title=\"ch${i+1}: ${v}%\">${i+1}</span>`;});"
            "  h+=`</div></div>`;"
            "  h+=`<div class=\"status-section\"><div class=\"status-label\">Relays</div>`;"
            "  h+=`<div class=\"relay-indicators\">`;"
            "  const rl=u.rl||[];"
            "  rl.forEach((v,i)=>{const cls=v?'on':'off';h+=`<span class=\"relay-ind ${cls}\" title=\"rly${i+1}: ${v?'ON':'OFF'}\">${v?'ON':'OFF'}</span>`;});"
            "  h+=`</div></div>`;"
            "  h+=`<div class=\"status-section\"><div class=\"status-label\">Sensors</div>`;"
            "  h+=`<div class=\"sensor-data\">`;"
            "  if(u.ldr!=null){"
            "   const ldrIcon=u.ldr>70?'☀️':u.ldr>40?'🌤️':u.ldr>10?'⛅':'🌙';"
            "   const ldrLabel=u.ldr>70?'Bright':u.ldr>40?'Moderate':u.ldr>10?'Dim':'Dark';"
            "   h+=`<div class=\"sensor-item\">${ldrIcon} Light: <span class=\"sensor-value\">${u.ldr}%</span> <span style=\"font-size:.85em;color:var(--muted)\">(${ldrLabel})</span></div>`;"
            "  }"
            "  h+=`</div></div>`;"
            "  h+=`<div class=\"status-section\"><div class=\"status-label\">System</div>`;"
            "  h+=`<div style=\"font-size:.9em\">`;"
            "  h+=`<div>Uptime: <b>${up}</b></div>`;"
            "  h+=`<div>Errors: <b>${u.err||0}</b></div>`;"
            "  h+=`</div></div>`;"
            "  h+=`</div></div></div>`;"
            " }"
            " if(!Object.keys(units).length) h='<p style=\"color:var(--muted)\">No units detected</p>';"
            " document.getElementById('fleet').innerHTML=h;"
            "}"
            "async function reqStatus(id){"
            " await fetch(`/api/units/${id}/status`,{method:'POST'});"
            " setTimeout(load,2000);"
            "}"
            "let currentUnitId=null;"
            "async function openControlModal(id,name){"
            " currentUnitId=id;"
            " document.getElementById('modalTitle').textContent='Control — '+name;"
            " document.getElementById('modalBody').innerHTML='<p style=\"color:var(--muted)\">Loading…</p>';"
            " document.getElementById('controlModal').style.display='block';"
            " const cfgUrl=id==0?'/api/config':'/api/units/'+id+'/config';"
            " const cfg=await fetch(cfgUrl).then(r=>r.json());"
            " const config=cfg.data||{};"
            " const channels=config.led_channels||[];"
            " const relays=config.relays||[];"
            " const scRes=await fetch('/api/units/'+id+'/scenes').then(r=>r.json());"
            " const unitScenes=scRes.data||[];"
            " if(id!=0)fetch('/api/units/'+id+'/status',{method:'POST'});"
            " let html='';"
            " if(unitScenes.length){"
            "  html+='<div class=\"control-group\"><h3>Scene Presets</h3>';"
            "  html+='<div class=\"preset-buttons\">';"
            "  unitScenes.forEach(function(s){"
            "   html+='<button class=\"btn scene-btn\" data-scene=\"'+s+'\">'+s+'</button> ';"
            "  });"
            "  html+='</div></div>';"
            " }"
            " html+='<div class=\"preset-buttons\">';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(100)\">All 100%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(50)\">All 50%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(25)\">All 25%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(0)\">All Off</button>';"
            " html+='</div>';"
            " if(channels.length){"
            "  html+='<div class=\"control-group\"><h3>LED Channels</h3>';"
            "  channels.forEach(ch=>{"
            "   const val=ch.default_duty_percent||0;"
            "   html+=`<div class=\"channel-control\">`;"
            "   html+=`<label>${ch.name} (${ch.id})</label>`;"
            "   html+=`<div class=\"slider-container\">`;"
            "   html+=`<input type=\"range\" class=\"slider\" id=\"ch_${ch.id}\" min=\"0\" max=\"100\" value=\"${val}\" oninput=\"updateSlider('${ch.id}',this.value)\">`;"
            "   html+=`<span class=\"bswatch\" id=\"sw_${ch.id}\" style=\"${swatchStyle(val)}\" title=\"Perceived brightness\"></span>`;"
            "   html+=`<span class=\"slider-value\" id=\"val_${ch.id}\">${val}%</span>`;"
            "   html+=`</div></div>`;"
            "  });"
            "  html+='</div>';"
            " }"
            " if(relays.length){"
            "  html+='<div class=\"control-group\"><h3>Relays</h3>';"
            "  relays.forEach(r=>{"
            "   const defaultState=r.default_state||'off';"
            "   const checked=defaultState==='on'?'checked':'';"
            "   html+=`<div class=\"relay-control\">`;"
            "   html+=`<label>${r.name} (${r.id})</label>`;"
            "   html+=`<label class=\"relay-toggle\">`;"
            "   html+=`<input type=\"checkbox\" id=\"rly_${r.id}\" ${checked}>`;"
            "   html+=`<span class=\"toggle-slider\"></span>`;"
            "   html+=`</label></div>`;"
            "  });"
            "  html+='</div>';"
            " }"
            " html+='<div class=\"options\">';"
            " html+='<label style=\"display:flex;align-items:center;gap:8px\">';"
            " html+='<input type=\"checkbox\" id=\"enableRevert\" checked onchange=\"toggleRevert()\">';"
            " html+='<span>Enable Auto-Revert</span></label>';"
            " html+='<div id=\"revertOptions\" style=\"margin-top:8px\">';"
            " html+='<label>Revert after: <input type=\"number\" id=\"revertTime\" value=\"60\" min=\"1\" max=\"3600\" style=\"width:70px\"> seconds</label>';"
            " html+='</div>';"
            " html+='<label style=\"margin-top:10px;display:block\">Fade Duration:';"
            " html+='<select id=\"fadeDuration\" style=\"width:100%;padding:5px;margin-top:5px\">';"
            " html+='<option value=\"1\">1 second</option>';"
            " html+='<option value=\"5\">5 seconds</option>';"
            " html+='<option value=\"15\">15 seconds</option>';"
            " html+='<option value=\"30\">30 seconds</option>';"
            " html+='<option value=\"60\">1 minute</option>';"
            " html+='<option value=\"300\">5 minutes</option>';"
            " html+='<option value=\"900\">15 minutes</option>';"
            " html+='<option value=\"1800\">30 minutes</option>';"
            " html+='</select></label>';"
            " html+='</div>';"
            " html+='<div class=\"modal-actions\">';"
            " html+='<button class=\"btn\" onclick=\"applyOverride()\">Apply Override</button>';"
            " html+='<button class=\"btn\" onclick=\"clearOverride()\">Clear All</button>';"
            " html+='<button class=\"btn\" onclick=\"closeModal()\">Close</button>';"
            " html+='</div>';"
            " document.getElementById('modalBody').innerHTML=html;"
            " document.querySelectorAll('.scene-btn').forEach(function(b){"
            "  b.onclick=function(){loadScenePreset(b.dataset.scene);}"
            " });"
            "}"
            "async function loadScenePreset(name){"
            " try{"
            "  const cfgUrl=currentUnitId==0?'/api/config':'/api/units/'+currentUnitId+'/config';"
            "  const cfg=await fetch(cfgUrl).then(r=>r.json());"
            "  if(!cfg.ok){console.error('Config fetch failed');return;}"
            "  const scenes=cfg.data.scenes||[];"
            "  console.log('Available scenes:',scenes);"
            "  const scene=scenes.find(s=>s.name===name);"
            "  if(!scene){console.error('Scene not found:',name);return;}"
            "  console.log('Loading scene:',scene);"
            "  (scene.led_channels||[]).forEach(ch=>{"
            "   console.log('Setting channel',ch.id,'to',ch.duty_percent);"
            "   const slider=document.getElementById('ch_'+ch.id);"
            "   if(slider){"
            "    slider.value=ch.duty_percent||0;"
            "    updateSlider(ch.id,slider.value);"
            "   }"
            "  });"
            "  (scene.relays||[]).forEach(r=>{"
            "   console.log('Setting relay',r.id,'to',r.state);"
            "   const toggle=document.getElementById('rly_'+r.id);"
            "   if(toggle)toggle.checked=(r.state==='on');"
            "  });"
            " }catch(e){console.error('Failed to load scene:',e);}"
            "}"
            "function toggleRevert(){"
            " const enabled=document.getElementById('enableRevert').checked;"
            " document.getElementById('revertOptions').style.display=enabled?'block':'none';"
            "}"
            "async function emergencyOff(){"
            " if(!confirm('Turn off ALL LEDs and relays on ALL units?')) return;"
            " try{"
            "  const r=await fetch('/api/emergency-off',{method:'POST'});"
            "  const j=await r.json();"
            "  if(!j.ok)alert('Emergency off error: '+j.error);"
            "  setTimeout(load,500);"
            " }catch(e){alert('Emergency off failed: '+e);}"
            "}"
            "function closeModal(){"
            " document.getElementById('controlModal').style.display='none';"
            "}"
            "function swatchStyle(pct){"
            " if(pct===0)return 'background:#1e293b;box-shadow:none';"
            " const r=Math.round(100+pct*1.55);"
            " const g=Math.round(80+pct*1.4);"
            " const b=Math.round(30+pct*0.7);"
            " const glow=Math.round(pct/5);"
            " return `background:rgb(${r},${g},${b});box-shadow:0 0 ${glow}px rgba(255,200,50,0.6)`;"
            "}"
            "function updateSlider(id,val){"
            " document.getElementById(`val_${id}`).textContent=val+'%';"
            " const sw=document.getElementById(`sw_${id}`);"
            " if(sw)sw.setAttribute('style',swatchStyle(parseInt(val)));"
            "}"
            "function applyPreset(val){"
            " document.querySelectorAll('.slider').forEach(s=>{s.value=val;updateSlider(s.id.substring(3),val);});"
            "}"
            "function updateFadeLabel(val){"
            " document.getElementById('fadeLabel').textContent=parseFloat(val).toFixed(1)+'s';"
            "}"
            "async function applyOverride(){"
            " try{"
            "  const ch=[];"
            "  document.querySelectorAll('.slider').forEach(s=>{"
            "   const id=s.id.substring(3);"
            "   ch.push([id,parseInt(s.value)]);"
            "  });"
            "  const rl=[];"
            "  document.querySelectorAll('.relay-toggle input').forEach(toggle=>{"
            "   const id=toggle.id.substring(4);"
            "   rl.push([id,toggle.checked?1:0]);"
            "  });"
            "  const enableRevert=document.getElementById('enableRevert').checked;"
            "  const revert=enableRevert?(parseInt(document.getElementById('revertTime').value)||0):0;"
            "  const fadeSec=parseInt(document.getElementById('fadeDuration').value)||1;"
            "  const fadeMs=Math.round(fadeSec*1000);"
            "  const payload={ch,rl,revert_s:revert,fade_ms:fadeMs};"
            "  console.log('Sending payload:',payload);"
            "  const resp=await fetch(`/api/units/${currentUnitId}/manual`,{"
            "   method:'POST',"
            "   headers:{'Content-Type':'application/json'},"
            "   body:JSON.stringify(payload)"
            "  });"
            "  const contentType=resp.headers.get('content-type');"
            "  if(!contentType||!contentType.includes('application/json')){"
            "   const text=await resp.text();"
            "   console.error('Server returned non-JSON response:',text.substring(0,200));"
            "   alert('Server error - check console for details');"
            "   return;"
            "  }"
            "  const result=await resp.json();"
            "  if(!result.ok)alert('Error: '+result.error);"
            "  closeModal();"
            "  setTimeout(load,500);"
            " }catch(e){console.error('Apply override error:',e);alert('Failed to apply override: '+e);}"
            "}"
            "async function clearOverride(){"
            " if(!confirm('Clear all manual overrides?')) return;"
            " await fetch(`/api/units/${currentUnitId}/manual`,{method:'DELETE'});"
            " closeModal();"
            " load();"
            "}"
            "async function rebootDevice(){"
            " if(!confirm('Reboot the coordinator? This will disconnect all clients briefly.')) return;"
            " try{"
            "  await fetch('/api/reboot',{method:'POST'});"
            "  alert('Rebooting... The page will reload in 10 seconds.');"
            "  setTimeout(()=>{window.location.reload();},10000);"
            " }catch(e){alert('Reboot request failed: '+e);}"
            "}"
            "function updateClock(){"
            " const now=new Date();"
            " const time=now.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});"
            " document.getElementById('currentTime').textContent=time;"
            "}"
            "async function updateUptime(){"
            " try{"
            "  const s=await fetch('/api/status').then(r=>r.json());"
            "  if(s.ok&&s.data.uptime_str)document.getElementById('uptime').textContent='Uptime: '+s.data.uptime_str;"
            "  if(s.ok&&s.data.connections){"
            "   const c=s.data.connections;"
            "   let cs='';"
            "   if(c.wifi)cs+='📶 WiFi ';"
            "   if(c.lora)cs+='📡 LoRa ';"
            "   if(c.mqtt)cs+='🔔 MQTT ';"
            "   if(!cs)cs='⚠️ No connections';"
            "   document.getElementById('connStatus').textContent=cs.trim();"
            "  }"
            " }catch(e){}"
            "}"
            "window.onclick=e=>{if(e.target.id==='controlModal')closeModal();};"
            "updateClock();setInterval(updateClock,1000);"
            "load();setInterval(load,15000);"
            "updateUptime();setInterval(updateUptime,60000);"
            "</script>"
            "</body></html>"
            )
            return html
        except Exception as e:
            log.error(f"[WEB] Error building dashboard HTML: {e}")
            import sys
            sys.print_exception(e)
            return "<html><body><h1>Error loading dashboard</h1><p>Please check logs</p></body></html>"

    def stop(self):
        self.running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass


web_server = WebServer()
