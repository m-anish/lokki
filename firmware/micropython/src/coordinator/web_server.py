import asyncio
import socket
import json
from core.config_manager import config_manager
from shared.system_status import system_status
from shared.simple_logger import Logger

log = Logger()

_PORT = 80


class WebServer:
    """
    Minimal async HTTP server for Phase 1.
    Serves basic JSON status endpoints.
    Full fleet management API added in Phase 3.
    """

    def __init__(self):
        self.running = False
        self._server = None

    async def start_and_serve(self):
        try:
            self._server = socket.socket()
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("0.0.0.0", _PORT))
            self._server.listen(2)
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

    async def _handle(self, conn, addr):
        try:
            conn.setblocking(False)
            await asyncio.sleep_ms(10)
            request = b""
            for _ in range(20):
                try:
                    chunk = conn.recv(256)
                    request += chunk
                    if b"\r\n\r\n" in request:
                        break
                except OSError:
                    await asyncio.sleep_ms(20)

            path = self._parse_path(request)
            body, ctype = self._route(path)
            response = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: {ctype}\r\n"
                "Connection: close\r\n\r\n"
            ) + body
            conn.send(response.encode())
        except Exception as e:
            log.error(f"[WEB] Handler error: {e}")
        finally:
            conn.close()

    def _parse_path(self, raw):
        try:
            line = raw.split(b"\r\n")[0].decode()
            return line.split(" ")[1].split("?")[0]
        except Exception:
            return "/"

    def _route(self, path):
        if path == "/api/status":
            return json.dumps(system_status.get_status_dict()), "application/json"
        if path == "/api/config":
            cfg = config_manager.get
            return json.dumps({
                "version": config_manager.version,
                "role": config_manager.role,
                "unit_id": config_manager.unit_id,
                "unit_name": config_manager.unit_name,
            }), "application/json"
        # Default: minimal HTML landing page
        body = (
            f"<html><body>"
            f"<h2>Lokki — {config_manager.unit_name}</h2>"
            f"<p>Role: {config_manager.role} | Unit ID: {config_manager.unit_id}</p>"
            f"<p>Uptime: {system_status.get_uptime_string()}</p>"
            f"<p><a href='/api/status'>Status JSON</a></p>"
            f"</body></html>"
        )
        return body, "text/html"

    def stop(self):
        self.running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass


web_server = WebServer()
