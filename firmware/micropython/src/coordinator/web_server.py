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
                "Connection: close\r\n\r\n"
            ) + body_out
            await self._send_all(conn, response.encode())
        except Exception as e:
            log.error(f"[WEB] Handler error: {e}")
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
            "body{{font-family:sans-serif;max-width:800px;margin:2em auto;padding:0 1em}}"
            "h1{{font-size:1.4em}}table{{border-collapse:collapse;width:100%}}"
            "td,th{{border:1px solid #ccc;padding:.4em .6em;text-align:left}}"
            "th{{background:#f4f4f4}}.online{{color:green}}.offline{{color:#c00}}"
            ".btn{{display:inline-block;padding:.3em .8em;border:1px solid #888;"
            "border-radius:3px;cursor:pointer;background:#f0f0f0;font-size:.9em}}"
            ".btn:hover{{background:#e0e0e0}}"
            ".modal{{display:none;position:fixed;z-index:1000;left:0;top:0;width:100%;height:100%;"
            "background:rgba(0,0,0,0.5);overflow:auto}}"
            ".modal-content{{background:#fff;margin:5% auto;padding:20px;border-radius:8px;"
            "max-width:500px;box-shadow:0 4px 6px rgba(0,0,0,0.3)}}"
            ".modal-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}}"
            ".modal-header h2{{margin:0;font-size:1.2em}}"
            ".close{{cursor:pointer;font-size:1.5em;font-weight:bold;color:#888}}"
            ".close:hover{{color:#000}}"
            ".control-group{{margin:15px 0;padding:10px;background:#f9f9f9;border-radius:4px}}"
            ".control-group h3{{margin:0 0 10px 0;font-size:1em;color:#555}}"
            ".channel-control,.relay-control{{margin:8px 0;padding:8px;background:#fff;border-radius:3px}}"
            ".channel-control label{{display:block;margin-bottom:5px;font-weight:bold;font-size:0.9em}}"
            ".slider-container{{display:flex;align-items:center;gap:10px}}"
            ".slider{{flex:1;height:6px;-webkit-appearance:none;appearance:none;background:#ddd;border-radius:3px;outline:none}}"
            ".slider::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:18px;height:18px;"
            "background:#4CAF50;cursor:pointer;border-radius:50%}}"
            ".slider::-moz-range-thumb{{width:18px;height:18px;background:#4CAF50;cursor:pointer;border-radius:50%}}"
            ".slider-value{{min-width:45px;text-align:right;font-weight:bold;color:#333}}"
            ".relay-buttons{{display:flex;gap:5px}}"
            ".relay-buttons .btn{{flex:1;padding:5px 10px;font-size:0.85em}}"
            ".relay-buttons .btn.active{{background:#4CAF50;color:#fff;border-color:#4CAF50}}"
            ".options{{margin:15px 0;padding:10px;background:#f0f0f0;border-radius:4px}}"
            ".options label{{display:inline-block;margin-right:10px;font-size:0.9em}}"
            ".options input{{width:60px;padding:3px;margin-left:5px}}"
            ".modal-actions{{display:flex;gap:10px;margin-top:20px}}"
            ".modal-actions .btn{{flex:1;padding:10px;font-weight:bold}}"
            ".preset-buttons{{display:flex;gap:5px;margin-bottom:10px;flex-wrap:wrap}}"
            ".preset-buttons .btn{{flex:1;min-width:70px;font-size:0.85em}}"
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
            "<a href='/api/scenes'>Scenes JSON</a> &middot; "
            "<a href='/config-builder.html'>Config Builder</a></p>"
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
            "async function load(){{"
            " const f=await fetch('/api/fleet').then(r=>r.json());"
            " const units=f.data||{{}};"
            " let h='<table><tr><th>ID</th><th>Name</th><th>Status</th>"
            "<th>Uptime</th><th>LDR</th><th>Errors</th><th>Action</th></tr>';"
            " for(const[id,u] of Object.entries(units)){{"
            "  const cls=u.online?'online':'offline';"
            "  const st=u.online?'Online':'Offline';"
            "  const uptime=u.uptime||0;"
            "  const hrs=Math.floor(uptime/3600);"
            "  const mins=Math.floor((uptime%3600)/60);"
            "  const up=u.online?`${{hrs}}h ${{mins}}m`:'—';"
            f"  const name=id==0?'{unit_name} (Coordinator)':`Unit ${{id}}`;"
            "  h+=`<tr><td>${{id}}</td><td>${{name}}</td>"
            "<td class='${{cls}}'>${{st}}</td><td>${{up}}</td>"
            "<td>${{u.ldr!=null?u.ldr+'%':'—'}}</td><td>${{u.err}}</td>"
            "<td><button class='btn' onclick='openControlModal(${{id}},\"${{name}}\")'>Control</button> "
            "<button class='btn' onclick='reqStatus(${{id}})'>Refresh</button></td></tr>`;"
            " }}"
            " if(!Object.keys(units).length) h+='<tr><td colspan=7>No units</td></tr>';"
            " h+='</table>';"
            " document.getElementById('fleet').innerHTML=h;"
            " const sc=await fetch('/api/scenes').then(r=>r.json());"
            " const scenes=sc.data||[];"
            " let sh=scenes.map(s=>"
            "  `<button class='btn' onclick='applyScene(\"${{s}}\")'>${{s}}</button> `"
            " ).join('')||'No scenes defined';"
            " document.getElementById('scenes').innerHTML=sh;"
            "}}"
            "async function reqStatus(id){{"
            " await fetch(`/api/units/${{id}}/status`,{{method:'POST'}});"
            " setTimeout(load,2000);"
            "}}"
            "async function applyScene(name){{"
            " if(!confirm(`Apply scene '${{name}}' to all units?`)) return;"
            " await fetch(`/api/scenes/${{name}}/apply`,{{method:'POST',"
            "  headers:{{'Content-Type':'application/json'}},body:'{{}}'}});"
            " load();"
            "}}"
            "let currentUnitId=null;"
            "async function openControlModal(id,name){{"
            " currentUnitId=id;"
            " document.getElementById('modalTitle').textContent=`Control - ${{name}}`;"
            " document.getElementById('controlModal').style.display='block';"
            " const cfgUrl=id==0?'/api/config':`/api/units/${{id}}/config`;"
            " const cfg=await fetch(cfgUrl).then(r=>r.json());"
            " const config=cfg.data||{{}};"
            " const channels=config.led_channels||[];"
            " const relays=config.relays||[];"
            " let html='<div class=\"preset-buttons\">';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(100)\">All 100%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(75)\">All 75%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(50)\">All 50%</button>';"
            " html+='<button class=\"btn\" onclick=\"applyPreset(0)\">All Off</button>';"
            " html+='</div>';"
            " if(channels.length){{"
            "  html+='<div class=\"control-group\"><h3>LED Channels</h3>';"
            "  channels.forEach(ch=>{{"
            "   const val=ch.default_duty_percent||0;"
            "   html+=`<div class=\"channel-control\">`;"
            "   html+=`<label>${{ch.name}} (${{ch.id}})</label>`;"
            "   html+=`<div class=\"slider-container\">`;"
            "   html+=`<input type=\"range\" class=\"slider\" id=\"ch_${{ch.id}}\" min=\"0\" max=\"100\" value=\"${{val}}\" oninput=\"updateSlider('${{ch.id}}',this.value)\">`;"
            "   html+=`<span class=\"slider-value\" id=\"val_${{ch.id}}\">${{val}}%</span>`;"
            "   html+=`</div></div>`;"
            "  }});"
            "  html+='</div>';"
            " }}"
            " if(relays.length){{"
            "  html+='<div class=\"control-group\"><h3>Relays</h3>';"
            "  relays.forEach(r=>{{"
            "   const defaultState=r.default_state||'off';"
            "   const onCls=defaultState==='on'?'active':'';"
            "   const offCls=defaultState==='off'?'active':'';"
            "   html+=`<div class=\"relay-control\">`;"
            "   html+=`<label>${{r.name}} (${{r.id}})</label>`;"
            "   html+=`<div class=\"relay-buttons\">`;"
            "   html+=`<button class=\"btn ${{onCls}}\" onclick=\"setRelay('${{r.id}}',1)\">ON</button>`;"
            "   html+=`<button class=\"btn ${{offCls}}\" onclick=\"setRelay('${{r.id}}',0)\">OFF</button>`;"
            "   html+=`</div></div>`;"
            "  }});"
            "  html+='</div>';"
            " }}"
            " html+='<div class=\"options\">';"
            " html+='<label>Auto-revert: <input type=\"number\" id=\"revertTime\" value=\"60\" min=\"0\" max=\"3600\"> sec</label>';"
            " html+='<label>Fade: <input type=\"number\" id=\"fadeTime\" value=\"1000\" min=\"0\" max=\"10000\"> ms</label>';"
            " html+='</div>';"
            " html+='<div class=\"modal-actions\">';"
            " html+='<button class=\"btn\" onclick=\"applyOverride()\">Apply Override</button>';"
            " html+='<button class=\"btn\" onclick=\"clearOverride()\">Clear All</button>';"
            " html+='<button class=\"btn\" onclick=\"closeModal()\">Close</button>';"
            " html+='</div>';"
            " document.getElementById('modalBody').innerHTML=html;"
            "}}"
            "function closeModal(){{"
            " document.getElementById('controlModal').style.display='none';"
            "}}"
            "function updateSlider(id,val){{"
            " document.getElementById(`val_${{id}}`).textContent=val+'%';"
            "}}"
            "function applyPreset(val){{"
            " document.querySelectorAll('.slider').forEach(s=>{{s.value=val;updateSlider(s.id.substring(3),val);}});"
            "}}"
            "function setRelay(id,state){{"
            " const btns=event.target.parentElement.querySelectorAll('.btn');"
            " btns.forEach(b=>b.classList.remove('active'));"
            " event.target.classList.add('active');"
            "}}"
            "async function applyOverride(){{"
            " try{{"
            "  const ch=[];"
            "  document.querySelectorAll('.slider').forEach(s=>{{"
            "   const id=s.id.substring(3);"
            "   ch.push([id,parseInt(s.value)]);"
            "  }});"
            "  const rl=[];"
            "  document.querySelectorAll('.relay-control').forEach(rc=>{{"
            "   const match=rc.querySelector('label').textContent.match(/\\((.+)\\)/);"
            "   if(!match)return;"
            "   const id=match[1];"
            "   const active=rc.querySelector('.btn.active');"
            "   if(active) rl.push([id,active.textContent==='ON'?1:0]);"
            "  }});"
            "  const revert=parseInt(document.getElementById('revertTime').value)||0;"
            "  const fade=parseInt(document.getElementById('fadeTime').value)||0;"
            "  const payload={{ch,rl,revert_s:revert,fade_ms:fade}};"
            "  console.log('Sending payload:',payload);"
            "  const resp=await fetch(`/api/units/${{currentUnitId}}/manual`,{{"
            "   method:'POST',"
            "   headers:{{'Content-Type':'application/json'}},"
            "   body:JSON.stringify(payload)"
            "  }});"
            "  const result=await resp.json();"
            "  if(!result.ok)alert('Error: '+result.error);"
            "  closeModal();"
            "  setTimeout(load,500);"
            " }}catch(e){{console.error(e);alert('Failed to apply override: '+e);}}"
            "}}"
            "async function clearOverride(){{"
            " if(!confirm('Clear all manual overrides?')) return;"
            " await fetch(`/api/units/${{currentUnitId}}/manual`,{{method:'DELETE'}});"
            " closeModal();"
            " load();"
            "}}"
            "window.onclick=e=>{{if(e.target.id==='controlModal')closeModal();}};"
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
