import asyncio
import socket
import json
import os
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
    "/dashboard.html",
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
        if path == "/" or path in _STATIC_PATHS:
            return True
        if path.startswith("/vendor/"):
            return True
        return False

    async def _serve_static(self, conn, path):
        # GET / serves the dashboard. All dynamic content (unit_name, fleet, etc.)
        # comes from API calls — the page itself is a static file in /www/.
        file_path = _STATIC_DIR + ("/dashboard.html" if path == "/" else path)
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

        # GET / is handled by _is_static / _serve_static before we get here.

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

    def stop(self):
        self.running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass


web_server = WebServer()
