import asyncio
import socket
import json
import os
import gc
import binascii
from shared.system_status import system_status
from shared.simple_logger import Logger
from core.config_manager import config_manager
import coordinator.api_handlers as api

log = Logger()

_PORT       = 80
_RECV_SIZE  = 512
_RECV_LOOPS = 32          # max read iterations per request
# Leaf configs prettified are routinely ~10–14 KB once scenes/time_windows grow.
# Cap is intentionally generous; the receive loop early-exits at content_length
# so a typical request still allocates only what it actually needs.
_BODY_MAX   = 16384
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
            # Proactive collect BEFORE we start allocating per-request
            # buffers. Big responses (e.g. /api/events with a full log
            # ring, /api/fleet with many leaves, a config push body)
            # need a 10-15 KB contiguous block which is the first
            # thing to disappear when the heap fragments. The
            # finally-block collect happens after the response is
            # already built/sent, so it can't help the request that's
            # currently trying to allocate. This one can.
            gc.collect()
            conn.setblocking(False)
            await asyncio.sleep_ms(10)

            # bytearray avoids the repeated re-allocation that `bytes += chunk`
            # forces: each += copies the whole buffer to a new bytes object,
            # then GC frees the old one, fragmenting the heap fast on a Pico.
            raw = bytearray()
            for _ in range(_RECV_LOOPS):
                try:
                    chunk = conn.recv(_RECV_SIZE)
                    if chunk:
                        raw.extend(chunk)
                    if b"\r\n\r\n" in raw:
                        break
                except OSError:
                    await asyncio.sleep_ms(20)

            method, path, headers, body = self._parse_request(bytes(raw))
            # Free the raw buffer now that headers + initial body have been
            # extracted into their own objects. Big wins on POSTs where `raw`
            # is several KB.
            raw = None
            if not method or not path:
                return

            # If there's a Content-Length header, make sure we read the full body
            content_length = headers.get("content-length")
            if content_length:
                try:
                    expected_len = int(content_length)
                except ValueError:
                    expected_len = 0
                if expected_len:
                    # Pre-empt fragmentation right before a sizable body read.
                    # gc.collect() is ~tens of ms; we only pay it on bodied
                    # requests, not every GET.
                    if expected_len > 1024:
                        gc.collect()
                    body = bytearray(body)
                    while len(body) < expected_len and len(body) < _BODY_MAX:
                        try:
                            chunk = conn.recv(_RECV_SIZE)
                            if chunk:
                                body.extend(chunk)
                            else:
                                await asyncio.sleep_ms(20)
                        except OSError:
                            await asyncio.sleep_ms(20)
                    body = bytes(body)
            
            log.debug(f"[WEB] {method} {path} from {addr[0]}, body_len={len(body)}")

            # HTTP Basic auth gate. When `dashboard.auth_password` is
            # set in config.json, every request — static + API — must
            # carry a matching Authorization header. CORS preflights
            # (OPTIONS) are exempt so browsers can do the auth dance
            # without being challenged twice. When auth_password is
            # absent / empty, this is a no-op (current behaviour).
            if method != "OPTIONS" and not self._check_auth(headers):
                await self._send_all(
                    conn,
                    b"HTTP/1.1 401 Unauthorized\r\n"
                    b"WWW-Authenticate: Basic realm=\"Lokki\"\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Authentication required",
                )
                return

            # Static file serving (GET only)
            if method == "GET" and self._is_static(path):
                await self._serve_static(conn, path)
                return

            status, ctype, body_out = await self._route(method, path, headers, body)
            # Send the response in pieces to keep peak contiguous-heap
            # demand as small as the inherent body size — no string
            # concatenation, no whole-body .encode() doubling. The
            # previous "headers + body, .encode() the lot" pattern
            # briefly held three ~10 KB allocations simultaneously
            # (body_out str + response str + response.encode() bytes)
            # which OOM'd on /api/events under sustained dashboard
            # polling once the heap fragmented enough.
            header_bytes = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {ctype}\r\n"
                "Connection: close\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: GET, POST, PATCH, DELETE, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type\r\n"
                "\r\n"
            ).encode()
            await self._send_all(conn, header_bytes)
            header_bytes = None    # release reference before allocating body chunks

            if isinstance(body_out, str):
                # Encode + send in slices so we never hold the whole
                # encoded body as a single bytes allocation. Peak
                # demand: body_out str (already in heap from json.dumps)
                # + one 1 KB slice + its encoded form.
                _CHUNK = 1024
                i = 0
                n = len(body_out)
                while i < n:
                    chunk = body_out[i:i + _CHUNK].encode()
                    await self._send_all(conn, chunk)
                    chunk = None
                    i += _CHUNK
            elif body_out:
                # Already bytes — send as-is.
                await self._send_all(conn, body_out)
        except Exception as e:
            # OSError EIO (errno 5) and ECONNRESET (errno 104) during
            # send are *recoverable* network-side conditions — the peer
            # browser will retry the GET automatically and the next
            # request usually succeeds. They are not bugs in our
            # request handler, and they should not trip the dashboard's
            # bell badge or flood the log at ERROR severity. Other
            # exceptions (MemoryError, KeyError, programming bugs in a
            # route handler) DO indicate something we should look at,
            # so those still log at ERROR.
            errno = getattr(e, "args", [None])[0] if isinstance(e, OSError) else None
            if errno in (5, 104):
                log.warn(f"[WEB] Connection dropped mid-response ({e}); peer will retry")
            else:
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
            # Reclaim heap fragments left behind by request buffers + response
            # encoding before the next connection grabs the dispatch loop.
            gc.collect()

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
    # HTTP Basic auth
    # ------------------------------------------------------------------
    # Threat model: LAN-only. Password is configured in cleartext in
    # config.json (mirrored to `dashboard.auth_password`), and travels
    # in cleartext over plain HTTP, because the Pico isn't a good
    # place to terminate TLS. Anyone with WiFi access to the LAN can
    # observe it. The relay design (docs/relay-design.md) is what
    # provides real TLS+auth for public exposure; this is the
    # "protect the dashboard from casual LAN users" knob.

    def _check_auth(self, headers):
        """Returns True when auth is OK (no password configured, or
        the request's Authorization header matches). False when a
        password is configured and the header is missing/wrong."""
        try:
            cfg = config_manager.get("dashboard") or {}
        except Exception:
            cfg = {}
        password = (cfg.get("auth_password") or "").strip()
        if not password:
            return True   # auth disabled — current shipping behaviour
        expected_user = (cfg.get("auth_username") or "admin").strip()
        auth = headers.get("authorization") or ""
        if not auth.lower().startswith("basic "):
            return False
        try:
            decoded = binascii.a2b_base64(auth[6:].strip()).decode("utf-8")
        except Exception:
            return False
        # Colon-split; password may itself contain colons (split only on
        # the first one). `partition` is the safe MicroPython-compatible
        # way to do that.
        user, sep, pwd = decoded.partition(":")
        if not sep:
            return False
        return user == expected_user and pwd == password

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

        # Split off the query string so per-route matching is clean. Handlers
        # that need query params receive `query` as a {k: v} dict.
        query = {}
        qpos = path.find("?")
        if qpos >= 0:
            query = self._parse_query(path[qpos + 1:])
            path = path[:qpos]

        # GET / is handled by _is_static / _serve_static before we get here.

        # --- Coordinator status (own unit) ---
        if path == "/api/status" and method == "GET":
            return self._json(api.handle_coordinator_status())

        if path == "/api/config" and method == "GET":
            return self._json(api.handle_full_config())

        if path == "/api/config-progress" and method == "GET":
            return self._json(api.handle_config_progress())

        if path == "/api/config/validate" and method == "POST":
            parsed = self._parse_json_body(body) or {}
            return self._json(api.handle_config_validate(parsed))

        if path == "/api/events" and method == "GET":
            return self._json(api.handle_events(query))

        if path == "/api/reboot" and method == "POST":
            return self._json(api.handle_reboot())

        if path == "/api/time-sync" and method == "POST":
            parsed = self._parse_json_body(body) or {}
            return self._json(api.handle_time_sync(parsed))

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

        # --- Unclaimed-leaf onboarding (claim wizard) ---
        # /api/unclaimed/<chip_uid>/blink  → POST: flash the leaf's status LED
        # /api/unclaimed/<chip_uid>/claim  → POST: push a blank-slate config so
        #                                    the leaf reboots into a real unit_id
        if path.startswith("/api/unclaimed/"):
            return await self._route_unclaimed(method, path, body)

        # --- Per-unit endpoints  /api/units/<id>/... ---
        if path.startswith("/api/units/"):
            return await self._route_unit(method, path, body)

        return "404 Not Found", "application/json", '{"ok":false,"error":"not found"}'

    async def _route_unclaimed(self, method, path, body):
        # parse /api/unclaimed/<chip_uid>/<sub>
        parts = path.split("/")  # ['', 'api', 'unclaimed', '<uid>', '<sub>']
        if len(parts) < 5:
            return "400 Bad Request", "application/json", '{"ok":false,"error":"bad path"}'
        chip_uid = parts[3]
        sub      = parts[4]

        if sub == "blink" and method == "POST":
            return self._json(api.handle_unclaimed_blink(chip_uid))

        if sub == "claim" and method == "POST":
            parsed = self._parse_json_body(body) or {}
            return self._json(await api.handle_unclaimed_claim(chip_uid, parsed))

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
            if method == "PATCH":
                # Incremental config update. Body is {path, value}.
                # Coord validates the merged result locally, picks
                # CFG_PATCH (one LoRa packet) vs chunked CFG_START
                # with target_path based on encoded payload size, and
                # updates the cached leaf config on success.
                parsed = self._parse_json_body(body) or {}
                return self._json(await api.handle_config_patch(unit_id, parsed))

        if sub == "manual":
            if method == "POST":
                parsed = self._parse_json_body(body) or {}
                return self._json(await api.handle_manual_override(unit_id, parsed))
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

    def _parse_query(self, qs):
        """Tiny query-string parser: a=1&b=hello%20world → {'a': '1', 'b': 'hello world'}."""
        out = {}
        if not qs:
            return out
        for part in qs.split("&"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
            else:
                k, v = part, ""
            out[self._url_decode(k)] = self._url_decode(v)
        return out

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
        if not raw:
            return None, None, {}, b""
        try:
            header_end = raw.find(b"\r\n\r\n")
            header_raw = raw[:header_end] if header_end >= 0 else raw
            body       = raw[header_end + 4:] if header_end >= 0 else b""
            lines      = header_raw.split(b"\r\n")
            
            # Handle empty or malformed requests
            if not lines or not lines[0]:
                return None, None, {}, b""
            
            parts      = lines[0].decode().split(" ")
            method     = parts[0].upper() if len(parts) > 0 else None
            
            # Fix: Add bounds check before accessing parts[1]
            if len(parts) > 1 and parts[1]:
                path_parts = parts[1].split("?")
                path = path_parts[0] if path_parts and path_parts[0] else None
            else:
                path = None
            
            if not method or not path:
                return None, None, {}, b""
            
            headers    = {}
            for line in lines[1:]:
                if b":" in line:
                    k, _, v = line.partition(b":")
                    headers[k.strip().lower().decode()] = v.strip().decode()
            return method, path, headers, body[:_BODY_MAX]
        except Exception:
            return None, None, {}, b""

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
