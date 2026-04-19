import asyncio
import socket
import json
from core.config_manager import config_manager
from shared.system_status import system_status
from shared.simple_logger import Logger
import coordinator.api_handlers as api

log = Logger()

_PORT       = 80
_RECV_SIZE  = 512
_RECV_LOOPS = 32          # max read iterations per request
_BODY_MAX   = 8192        # max POST body bytes accepted


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
            log.debug(f"[WEB] {method} {path} from {addr[0]}")

            status, ctype, body_out = await self._route(method, path, headers, body)
            response = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {ctype}\r\n"
                "Connection: close\r\n\r\n"
            ) + body_out
            conn.send(response.encode())
        except Exception as e:
            log.error(f"[WEB] Handler error: {e}")
            try:
                conn.send(b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _route(self, method, path, headers, body):
        # --- Static dashboard ---
        if path == "/" and method == "GET":
            return "200 OK", "text/html", self._dashboard_html()

        # --- Coordinator status (own unit) ---
        if path == "/api/status" and method == "GET":
            return self._json(api.handle_coordinator_status())

        if path == "/api/config" and method == "GET":
            return self._json(api.handle_unit_config(0))

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
            parsed = self._parse_json_body(body)
            unit_ids = parsed.get("unit_ids") if parsed else None
            return self._json(api.handle_scene_apply(scene_name, unit_ids))

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

        return "404 Not Found", "application/json", '{"ok":false,"error":"not found"}'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
            parts      = lines[0].decode().split(" ")
            method     = parts[0].upper() if len(parts) > 0 else "GET"
            path       = parts[1].split("?")[0] if len(parts) > 1 else "/"
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
        unit_name = config_manager.unit_name
        role      = config_manager.role
        unit_id   = config_manager.unit_id
        uptime    = system_status.get_uptime_string()
        return (
            "<!DOCTYPE html><html><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Lokki</title>"
            "<style>"
            "body{font-family:sans-serif;max-width:800px;margin:2em auto;padding:0 1em}"
            "h1{font-size:1.4em}table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #ccc;padding:.4em .6em;text-align:left}"
            "th{background:#f4f4f4}.online{color:green}.offline{color:#c00}"
            ".btn{display:inline-block;padding:.3em .8em;border:1px solid #888;"
            "border-radius:3px;cursor:pointer;background:#f0f0f0;font-size:.9em}"
            "</style></head><body>"
            f"<h1>Lokki — {unit_name}</h1>"
            f"<p>Role: <b>{role}</b> &nbsp;|&nbsp; Unit ID: <b>{unit_id}</b>"
            f" &nbsp;|&nbsp; Uptime: {uptime}</p>"
            "<hr>"
            "<h2>Fleet</h2>"
            "<div id='fleet'>Loading…</div>"
            "<hr>"
            "<h2>Scenes</h2>"
            "<div id='scenes'>Loading…</div>"
            "<hr>"
            "<p style='font-size:.8em;color:#999'>"
            "<a href='/api/status'>Status JSON</a> &middot; "
            "<a href='/api/fleet'>Fleet JSON</a> &middot; "
            "<a href='/api/scenes'>Scenes JSON</a></p>"
            "<script>"
            "async function load(){"
            " const f=await fetch('/api/fleet').then(r=>r.json());"
            " const units=f.data||{};"
            " let h='<table><tr><th>ID</th><th>Name</th><th>Status</th>"
            "<th>Uptime</th><th>LDR</th><th>Errors</th><th>Action</th></tr>';"
            " for(const[id,u] of Object.entries(units)){"
            "  const cls=u.online?'online':'offline';"
            "  const st=u.online?'Online':'Offline';"
            "  const up=u.online?`${Math.floor(u.uptime/3600)}h ${Math.floor((u.uptime%3600)/60)}m`:'—';"
            "  h+=`<tr><td>${id}</td><td>Unit ${id}</td>"
            "<td class='${cls}'>${st}</td><td>${up}</td>"
            "<td>${u.ldr!=null?u.ldr+'%':'—'}</td><td>${u.err}</td>"
            "<td><button class='btn' onclick='reqStatus(${id})'>Refresh</button></td></tr>`;"
            " }"
            " if(!Object.keys(units).length) h+='<tr><td colspan=7>No peers configured</td></tr>';"
            " h+='</table>';"
            " document.getElementById('fleet').innerHTML=h;"
            " const sc=await fetch('/api/scenes').then(r=>r.json());"
            " const scenes=sc.data||[];"
            " let sh=scenes.map(s=>"
            "  `<button class='btn' onclick='applyScene(\"${s}\")'>${s}</button> `"
            " ).join('')||'No scenes defined';"
            " document.getElementById('scenes').innerHTML=sh;"
            "}"
            "async function reqStatus(id){"
            " await fetch(`/api/units/${id}/status`,{method:'POST'});"
            " setTimeout(load,2000);"
            "}"
            "async function applyScene(name){"
            " if(!confirm(`Apply scene '${name}' to all units?`)) return;"
            " await fetch(`/api/scenes/${name}/apply`,{method:'POST',"
            "  headers:{'Content-Type':'application/json'},body:'{}'});"
            " load();"
            "}"
            "load();setInterval(load,15000);"
            "</script>"
            "</body></html>"
        )

    def stop(self):
        self.running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass


web_server = WebServer()
