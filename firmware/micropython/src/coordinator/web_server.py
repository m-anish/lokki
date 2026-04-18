"""
Simple async web server for PagodaLightPico.

Provides basic web interface for system status without complex endpoints.
"""

import asyncio
import socket
import json
import os
from simple_logger import Logger
from lib import config_manager as config
from lib.system_status import system_status
from lib.pwm_control import multi_pwm
import rtc_module
import time
import machine

log = Logger()

class AsyncWebServer:
    """
    Simple async web server for PagodaLightPico.
    Minimal memory footprint with basic functionality.
    """
    
    def __init__(self, port=80):
        self.port = port
        self.running = False
        self.server_socket = None
        self._cleanup_task_handle = None
    
    async def start(self):
        """Start the web server."""
        try:
            log.info(f"[WEB] Starting async web server on port {self.port}")
            # Create server socket
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.setblocking(False)
            self.server_socket.bind(('0.0.0.0', self.port))
            self.server_socket.listen(1)
            
            self.running = True
            # Cleanup abandoned uploads on startup
            try:
                self._cleanup_stale_uploads()
            except Exception as _e:
                log.debug("[WEB] Cleanup on start skipped: " + str(_e))

            # Start background cleanup task
            try:
                self._cleanup_task_handle = asyncio.create_task(self._cleanup_task())
            except Exception:
                self._cleanup_task_handle = None
            log.info(f"[WEB] Async web server started on port {self.port}")
            return True
            
        except Exception as e:
            log.error(f"[WEB] Failed to start web server: {e}")
            self.running = False
            return False
    
    def stop(self):
        """Stop the web server."""
        self.running = False
        # Best-effort cancel/let background task exit
        try:
            if self._cleanup_task_handle is not None:
                # Task loop checks self.running; it will exit shortly.
                self._cleanup_task_handle = None
        except Exception:
            pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        log.info("[WEB] Web server stopped")

    def _cleanup_stale_uploads(self, max_age_seconds=15*60):
        """Delete temp upload files older than max_age_seconds."""
        try:
            now = time.time()
        except Exception:
            # If time not available, skip cleanup
            return

        def _maybe_remove(path):
            try:
                st = os.stat(path)
            except Exception:
                return
            try:
                # MicroPython: mtime typically at index 8
                mtime = st[8] if len(st) > 8 else None
            except Exception:
                mtime = None
            if mtime is None:
                return
            try:
                age = now - mtime
                if age > max_age_seconds:
                    try:
                        os.remove(path)
                        log.info(f"[WEB] Removed stale temp upload: {path}")
                    except Exception as e:
                        log.debug(f"[WEB] Failed to remove stale temp '{path}': {e}")
            except Exception:
                pass

        # Check known temp files
        try:
            _maybe_remove(self._tmp_config_path())
        except Exception:
            pass
        try:
            _maybe_remove(self._tmp_sun_times_path())
        except Exception:
            pass

    async def _cleanup_task(self):
        """Periodic cleanup of abandoned uploads."""
        try:
            while self.running:
                try:
                    self._cleanup_stale_uploads()
                except Exception as e:
                    log.debug(f"[WEB] Cleanup task error: {e}")
                # Sleep 10 minutes between checks
                for _ in range(600):
                    if not self.running:
                        break
                    await asyncio.sleep(1)
        except Exception:
            pass
    
    async def serve_forever(self):
        """Main server loop."""
        if not self.running:
            return
            
        log.info("[WEB] Starting server loop")
        
        while self.running:
            try:
                # Accept connections with timeout
                try:
                    client_socket, addr = self.server_socket.accept()
                    client_socket.setblocking(False)
                    log.debug(f"[WEB] Connection from {addr}")
                    
                    # Handle client in separate task
                    asyncio.create_task(self.handle_client(client_socket, addr))
                    
                except OSError:
                    # No connection available, yield control
                    await asyncio.sleep(config.SERVER_IDLE_SLEEP_MS / 1000.0)
                    continue
                    
            except Exception as e:
                log.error(f"[WEB] Server loop error: {e}")
                await asyncio.sleep(1)
    
    async def handle_client(self, client_socket, addr):
        """Handle a client connection."""
        try:
            # Read request with timeout
            request_data = b""
            start_time = time.time()
            
            # First read until headers are complete (\r\n\r\n)
            headers_end = -1
            while time.time() - start_time < 5:  # 5 second timeout
                try:
                    chunk = client_socket.recv(1024)
                    if not chunk:
                        break
                    request_data += chunk
                    if b'\r\n\r\n' in request_data:
                        headers_end = request_data.find(b'\r\n\r\n')
                        break
                except OSError:
                    await asyncio.sleep(config.CLIENT_READ_SLEEP_MS / 1000.0)
                    continue
            
            if not request_data:
                return
            
            # If we have headers, check for Content-Length and read the body fully
            if headers_end != -1:
                headers_bytes = request_data[:headers_end]
                # Decode headers permissively
                try:
                    headers_text = headers_bytes.decode('utf-8')
                except:
                    headers_text = headers_bytes.decode('latin-1')

                content_length = 0
                for hline in headers_text.split('\r\n'):
                    if hline.lower().startswith('content-length:'):
                        try:
                            content_length = int(hline.split(':', 1)[1].strip())
                        except:
                            content_length = 0
                        break

                total_expected = headers_end + 4 + content_length
                # Read remaining body if any
                while len(request_data) < total_expected and (time.time() - start_time) < 5:
                    try:
                        chunk = client_socket.recv(1024)
                        if not chunk:
                            break
                        request_data += chunk
                    except OSError:
                        await asyncio.sleep(config.CLIENT_READ_SLEEP_MS / 1000.0)
                        continue

            # Parse request (headers + body)
            try:
                request_str = request_data.decode('utf-8')
            except:
                # If decode fails, try with latin-1 which accepts all byte values
                request_str = request_data.decode('latin-1')
            lines = request_str.split('\r\n')
            if not lines:
                return
                
            request_line = lines[0]
            parts = request_line.split(' ')
            if len(parts) < 2:
                return
                
            method = parts[0]
            path = parts[1]
            # Extract headers text and body bytes for binary-safe handlers
            headers_text = ''
            body_bytes = b''
            if headers_end != -1:
                try:
                    headers_text = request_data[:headers_end].decode('utf-8')
                except:
                    headers_text = request_data[:headers_end].decode('latin-1')
                body_bytes = request_data[headers_end+4: headers_end+4+content_length]
            
            log.debug(f"[WEB] {method} {path} from {addr}")
            
            # Generate response
            if path == '/':
                # Stream the main page directly to the client to minimize memory usage
                await self.stream_main_page(client_socket)
                response = None
            elif path == '/status':
                response = self.generate_status_json()
            elif path == '/download-config':
                response = self.generate_config_download()
            elif path == '/download-sun-times':
                response = self.generate_sun_times_download()
            elif path == '/upload-config-begin' and method == 'POST':
                response = self.handle_config_upload_begin()
            elif path == '/upload-config-chunk' and method == 'POST':
                response = self.handle_config_upload_chunk(body_bytes, headers_text)
            elif path == '/upload-config-finalize' and method == 'POST':
                response = await self.handle_config_upload_finalize(request_str)
            elif path == '/upload-config':
                if method == 'GET':
                    # Stream the chunked-upload page to minimize memory usage
                    await self.stream_upload_page_chunked(client_socket)
                    response = None
                elif method == 'POST':
                    response = await self.handle_config_upload(request_str)
                else:
                    response = self.generate_404()
            elif path == '/upload-sun-times-begin' and method == 'POST':
                response = self.handle_sun_times_upload_begin()
            elif path == '/upload-sun-times-chunk' and method == 'POST':
                response = self.handle_sun_times_upload_chunk(body_bytes, headers_text)
            elif path == '/upload-sun-times-finalize' and method == 'POST':
                response = await self.handle_sun_times_upload_finalize()
            elif path == '/upload-sun-times':
                if method == 'GET':
                    # Stream the sun times upload page to minimize memory usage
                    await self.stream_upload_sun_times_page_chunked(client_socket)
                    response = None
                elif method == 'POST':
                    # Legacy multipart handler
                    response = await self.handle_sun_times_upload(request_str)
                else:
                    response = self.generate_404()
            elif path == '/restart':
                # Show restart page and schedule hard reset
                response = self.generate_restart_page()
            else:
                response = self.generate_404()
            
            # Send response (ensure full bytes are sent)
            try:
                if response is not None:
                    if isinstance(response, str):
                        response_bytes = response.encode('utf-8')
                    else:
                        response_bytes = response
                    total_sent = 0
                    while total_sent < len(response_bytes):
                        try:
                            sent = client_socket.send(response_bytes[total_sent:])
                            if sent is None:
                                sent = 0
                            if sent <= 0:
                                break
                            total_sent += sent
                        except OSError:
                            # Briefly yield and retry
                            await asyncio.sleep(0.01)
                            continue
            except Exception:
                pass  # Client disconnected or other send error
                
        except Exception as e:
            log.error(f"[WEB] Client handling error: {e}")
        finally:
            try:
                client_socket.close()
            except:
                pass
    
    def generate_main_page(self):
        """Generate simple main page."""
        try:
            current_time = rtc_module.get_current_time()
            time_str = f"{current_time[3]:02d}:{current_time[4]:02d}:{current_time[5]:02d}"
            date_str = f"{current_time[2]:02d}/{current_time[1]:02d}/{current_time[0]}"

            status = system_status.get_status_dict()
            
            # Get PWM controller status and full config for including disabled controllers
            pwm_status = multi_pwm.get_pin_status()
            config_dict = config.config_manager.get_config_dict()
            # Current config version for display
            current_config_version = str(config_dict.get('version', '')).strip() or 'unknown'
            # Read location from sun_times.json (if available)
            try:
                with open('sun_times.json', 'r') as f:
                    _st = json.loads(f.read())
                ui_location = str(_st.get('location', 'Unknown')).strip() or 'Unknown'
            except Exception:
                ui_location = 'Unknown'

            # Build Controllers table HTML (include disabled pins too)
            pwm_table_rows = ""
            pwm_pins_cfg = config_dict.get('pwm_pins', {})
            for pin_key, pin_cfg in pwm_pins_cfg.items():
                if str(pin_key).startswith('_'):
                    continue

                enabled = pin_cfg.get('enabled', False)
                # Use live status if available; otherwise, default values
                pin_live = pwm_status.get(pin_key, {
                    'name': pin_cfg.get('name', pin_key),
                    'gpio_pin': pin_cfg.get('gpio_pin', 0),
                    'duty_percent': 0
                })

                # Get current window info from system status
                current_window = "None"
                window_time = "N/A"

                status_pins = status.get('pins', {})
                if pin_key in status_pins:
                    status_pin = status_pins[pin_key]
                    current_window = status_pin.get('window_display', 'None')
                    start_time = status_pin.get('window_start', 'N/A')
                    end_time = status_pin.get('window_end', 'N/A')
                    if start_time != 'N/A' and end_time != 'N/A':
                        window_time = f"{start_time} - {end_time}"

                duty_percent = pin_live.get('duty_percent', 0) if enabled else 0

                if not enabled:
                    active_status = "Inactive"
                    status_class = "disabled"
                else:
                    active_status = "Active" if duty_percent > 0 else "Inactive"
                    status_class = "active" if duty_percent > 0 else "inactive"

                pwm_table_rows += f"""
                <tr class="{status_class}">
                    <td>{pin_live.get('name', pin_cfg.get('name', pin_key))}</td>
                    <td>GPIO {pin_live.get('gpio_pin', pin_cfg.get('gpio_pin', ''))}</td>
                    <td>{active_status}</td>
                    <td>{current_window}</td>
                    <td>{window_time}</td>
                    <td>{duty_percent}%</td>
                </tr>"""

            if not pwm_table_rows:
                pwm_table_rows = '<tr><td colspan="6" style="text-align: center; color: #666;">No controllers configured</td></tr>'
            
            # Determine MQTT status and styling
            mqtt_enabled = config_dict.get('notifications', {}).get('enabled', False)
            mqtt_connected = status.get('connections', {}).get('mqtt', False)
            
            if not mqtt_enabled:
                mqtt_status = "Disabled"
                mqtt_class = "disabled"
            elif mqtt_connected:
                mqtt_status = "Connected"
                mqtt_class = "online"
            else:
                mqtt_status = "Offline"
                mqtt_class = "offline"

            html = f"""<!DOCTYPE html>
    <html>
    <head>
        <title>{config.WEB_TITLE}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, 'Noto Sans', 'Liberation Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji'; margin: 20px; background: #f5f5f5; }}
            .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
            h1 {{ color: #2c3e50; text-align: center; }}
            h2 {{ color: #34495e; margin-top: 30px; }}
            .status {{ padding: 10px; margin: 10px 0; border-radius: 5px; }}
            .online {{ background: #d4edda; border-left: 4px solid #28a745; }}
            .offline {{ background: #f8d7da; border-left: 4px solid #dc3545; }}
            .disabled {{ background: #fff3cd; border-left: 4px solid #ffc107; }}
            .time {{ font-size: 24px; text-align: center; margin: 20px 0; color: #2c3e50; }}
            .pwm-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            .pwm-table th, .pwm-table td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }}
            .pwm-table th {{ background-color: #f8f9fa; font-weight: bold; }}
            .pwm-table tr.active {{ background-color: #d4edda; }}
            .pwm-table tr.inactive {{ background-color: #f8f9fa; }}
            .pwm-table tr.disabled {{ background-color: #ffe0b2; }}
            .table-responsive {{ width: 100%; position: relative; }}
            .table-scroll {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
            .table-responsive::after {{
                content: "";
                position: absolute;
                top: 0; right: 0;
                width: 24px; height: 100%;
                pointer-events: none;
                background: linear-gradient(to left, rgba(255,255,255,1), rgba(255,255,255,0));
                opacity: 0; transition: opacity 0.15s linear; z-index: 1;
            }}
            .table-responsive::before {{
                content: "";
                position: absolute;
                top: 0; left: 0;
                width: 24px; height: 100%;
                pointer-events: none;
                background: linear-gradient(to right, rgba(255,255,255,1), rgba(255,255,255,0));
                opacity: 0; transition: opacity 0.15s linear; z-index: 1;
            }}
            .table-responsive.has-right::after {{ opacity: 1; }}
            .table-responsive.has-left::before {{ opacity: 1; }}
            /* Ensure table can scroll horizontally on small screens */
            .pwm-table {{ min-width: 560px; }}
            .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; }}
            .footer a {{ color: #007bff; text-decoration: none; }}
            .footer a:hover {{ text-decoration: underline; }}
            .refresh-info {{ font-size: 11px; color: #999; margin-top: 10px; }}
            .footer-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 16px; align-items: start; padding: 0; margin: 8px 0 0 0; }}
            .footer-grid .col {{ display: flex; flex-direction: column; gap: 6px; }}
            .footer .col-title {{ font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.03em; }}
            .version {{ background: #e9ecef; border-left: 4px solid #6c757d; }}
            .location {{ background: #e7f3ff; border-left: 4px solid #0d6efd; }}
            /* Mobile tweaks */
            @media (max-width: 480px) {{
                .container {{ padding: 12px; }}
                .pwm-table th, .pwm-table td {{ padding: 6px 8px; font-size: 12px; }}
            }}
            /* Very small screens: hide Pin and Window Time columns */
            @media (max-width: 420px) {{
                .pwm-table th:nth-child(2), .pwm-table td:nth-child(2),
                .pwm-table th:nth-child(5), .pwm-table td:nth-child(5) {{
                    display: none;
                }}
            }}
        </style>
        <script>
            let clockInterval;
            let refreshInterval;
            let countdownInterval;
            
            // Clock updating every second
            function startClock(h, m, s) {{
                const timeEl = document.getElementById('time');
                function pad(n) {{ return (n < 10 ? '0' : '') + n; }}
                
                function tick() {{
                    s += 1;
                    if (s >= 60) {{ s = 0; m += 1; }}
                    if (m >= 60) {{ m = 0; h = (h + 1) % 24; }}
                    timeEl.textContent = pad(h) + ':' + pad(m) + ':' + pad(s);
                }}
                
                // Start the clock immediately and then every second
                tick();
                clockInterval = setInterval(tick, 1000);
            }}
            
            // Page refresh functionality
            function startPageRefresh() {{
                let secondsLeft = 180;
                function setText() {{
                    const el = document.getElementById('refresh-countdown');
                    if (el) {{ el.textContent = `Next refresh in ${secondsLeft} seconds`; return true; }}
                    return false;
                }}
                // Initial text; if element isn't in DOM yet (streaming), retry until it appears
                if (!setText()) {{
                    const waitId = setInterval(function() {{
                        if (setText()) clearInterval(waitId);
                    }}, 200);
                }}
                function updateCountdown() {{
                    secondsLeft--;
                    if (secondsLeft <= 0) {{ location.reload(); return; }}
                    setText();
                }}
                countdownInterval = setInterval(updateCountdown, 1000);
                refreshInterval = setTimeout(function(){{ location.reload(); }}, 180000);
            }}
            
            // Cleanup intervals on page unload
            window.addEventListener('beforeunload', function() {{
                if (clockInterval) clearInterval(clockInterval);
                if (refreshInterval) clearTimeout(refreshInterval);
                if (countdownInterval) clearInterval(countdownInterval);
            }});
            // Horizontal scroll fades
            function initTableFades() {{
                const wrap = document.querySelector('.table-responsive');
                if (!wrap) return;
                const scroller = wrap.querySelector('.table-scroll') || wrap;
                function update() {{
                    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
                    if (maxScroll <= 0) {{ wrap.classList.remove('has-left','has-right'); return; }}
                    wrap.classList.toggle('has-left', scroller.scrollLeft > 0);
                    wrap.classList.toggle('has-right', scroller.scrollLeft < maxScroll - 1);
                }}
                scroller.addEventListener('scroll', update, {{ passive: true }});
                setTimeout(update, 0);
            }}
        </script>
    </head>
    <body onload="startClock({current_time[3]}, {current_time[4]}, {current_time[5]}); startPageRefresh(); initTableFades();">
        <div class="container">
            <h1>{config.WEB_TITLE}</h1>
            <div class="time">🕒 <span id="time">{time_str}</span><br><small>{date_str}</small></div>

            <div class="status version">
                <strong>🏷️ Config version:</strong> {current_config_version}
            </div>
            <div class="status location">
                <strong>📍 Location:</strong> {ui_location}
            </div>

            <div class="status {'online' if status.get('connections', {}).get('wifi', False) else 'offline'}">
                <strong>📶 WiFi:</strong> {config_dict.get('wifi', {}).get('ssid', 'Unknown')}, {status.get('network', {}).get('ip', 'N/A')}
            </div>

            <div class="status {mqtt_class}">
                <strong>🔌 MQTT:</strong> {mqtt_status}
            </div>

            <h2>🎛️ Controllers</h2>
            <div class="table-responsive">
            <div class="table-scroll">
            <table class="pwm-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Pin</th>
                        <th>Status</th>
                        <th>Current Window</th>
                        <th>Window Time</th>
                        <th>Duty Cycle</th>
                    </tr>
                </thead>
                <tbody>
                    {pwm_table_rows}
                </tbody>
            </table>
            </div>
            <div class="table-scroll">
                <div class="table-responsive">
                    <table class="pwm-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Pin</th>
                                <th>Status</th>
                                <th>Current Window</th>
                                <th>Window Time</th>
                                <th>Duty Cycle</th>
                            </tr>
                        </thead>
                        <tbody>
                            {pwm_table_rows}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

            <div class="footer">
                <div class="footer-grid">
                    <div class="col">
                        <a href="/status">📄 Status (JSON)</a>
{{ ... }}
```
                    </div>
                    <div class="col">
                        <a href="/upload-config">⬆️ Upload Config</a>
                        <a href="/upload-sun-times">⬆️ Upload Sun Times</a>
                    </div>
                    <div class="col">
                        <a href="/download-config">⬇️ Download Config</a>
                        <a href="/download-sun-times">⬇️ Download Sun Times</a>
                    </div>
                    <div class="col">
                        <a href="/restart">🔄 Restart Device</a>
                    </div>
                </div>
                <div style="margin-top:8px;font-size:12px;color:#666;">
                    <small>
                        <a href="https://github.com/m-anish/PagodaLightPico" target="_blank" rel="noopener">PagodaLightPico</a>
                    </small>
                </div>
                <div class="refresh-info" id="refresh-countdown"></div>
            </div>
        </div>
    </body>
    </html>"""

            # Ensure Content-Length matches bytes actually sent
            body_bytes = html.encode('utf-8')
            headers = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
            return headers + html

        except Exception as e:
            log.error(f"[WEB] Error generating main page: {e}")
            return self.generate_500()

    async def _awrite(self, sock, data_bytes):
        """Asynchronously write all bytes to the socket in small chunks."""
        try:
            total_sent = 0
            ln = len(data_bytes)
            while total_sent < ln:
                try:
                    sent = sock.send(data_bytes[total_sent:])
                    if sent is None:
                        sent = 0
                    if sent <= 0:
                        # Yield and retry
                        await asyncio.sleep(0.005)
                        continue
                    total_sent += sent
                except OSError:
                    await asyncio.sleep(0.005)
                    continue
        except Exception:
            pass

    async def stream_main_page(self, client_socket):
        """Stream the main page in small chunks without computing Content-Length."""
        try:
            current_time = rtc_module.get_current_time()
            time_str = f"{current_time[3]:02d}:{current_time[4]:02d}:{current_time[5]:02d}"
            date_str = f"{current_time[2]:02d}/{current_time[1]:02d}/{current_time[0]}"

            status = system_status.get_status_dict()
            pwm_status = multi_pwm.get_pin_status()
            config_dict = config.config_manager.get_config_dict()
            current_config_version = str(config_dict.get('version', '')).strip() or 'unknown'
            # UI location from sun_times.json
            try:
                with open('sun_times.json', 'r') as f:
                    _st = json.loads(f.read())
                ui_location = str(_st.get('location', 'Unknown')).strip() or 'Unknown'
            except Exception:
                ui_location = 'Unknown'

            mqtt_enabled = config_dict.get('notifications', {}).get('enabled', False)
            mqtt_connected = status.get('connections', {}).get('mqtt', False)
            if not mqtt_enabled:
                mqtt_status = "Disabled"
                mqtt_class = "disabled"
            elif mqtt_connected:
                mqtt_status = "Connected"
                mqtt_class = "online"
            else:
                mqtt_status = "Offline"
                mqtt_class = "offline"

            # Headers (no Content-Length)
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Connection: close\r\n\r\n"
            )
            await self._awrite(client_socket, headers.encode('utf-8'))

            # Head start + styles (small chunks)
            await self._awrite(client_socket, b"<!DOCTYPE html><html><head>")
            await self._awrite(client_socket, f"<title>{config.WEB_TITLE}</title>".encode('utf-8'))
            await self._awrite(client_socket, b"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")

            style = (
                "<style>"
                "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, 'Noto Sans', 'Liberation Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji'; margin: 20px; background: #f5f5f5; }"
                ".container { max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }"
                "h1 { color: #2c3e50; text-align: center; }"
                "h2 { color: #34495e; margin-top: 30px; }"
                ".status { padding: 10px; margin: 10px 0; border-radius: 5px; }"
                ".online { background: #d4edda; border-left: 4px solid #28a745; }"
                ".offline { background: #f8d7da; border-left: 4px solid #dc3545; }"
                ".disabled { background: #fff3cd; border-left: 4px solid #ffc107; }"
                ".time { font-size: 24px; text-align: center; margin: 20px 0; color: #2c3e50; }"
                ".pwm-table { width: 100%; border-collapse: collapse; margin: 20px 0; }"
                ".pwm-table th, .pwm-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }"
                ".pwm-table th { background-color: #f8f9fa; font-weight: bold; }"
                ".pwm-table tr.active { background-color: #d4edda; }"
                ".pwm-table tr.inactive { background-color: #f8f9fa; }"
                ".pwm-table tr.disabled { background-color: #ffe0b2; }"
                ".table-responsive { width: 100%; position: relative; }"
                ".table-scroll { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }"
                ".table-responsive::after{content:'';position:absolute;top:0;right:0;width:36px;height:100%;pointer-events:none;background:linear-gradient(to left, rgba(255,255,255,1), rgba(255,255,255,0));opacity:0;transition:opacity 0.15s linear;z-index:1;}"
                ".table-responsive::before{content:'';position:absolute;top:0;left:0;width:36px;height:100%;pointer-events:none;background:linear-gradient(to right, rgba(255,255,255,1), rgba(255,255,255,0));opacity:0;transition:opacity 0.15s linear;z-index:1;}"
                ".table-responsive.has-right::after{opacity:1;}"
                ".table-responsive.has-left::before{opacity:1;}"
                ".pwm-table { min-width: 560px; }"
                ".footer { text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; }"
                ".footer a { color: #007bff; text-decoration: none; }"
                ".footer a:hover { text-decoration: underline; }"
                ".refresh-info { font-size: 11px; color: #999; margin-top: 10px; }"
                ".footer-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 16px; align-items: start; padding: 0; margin: 8px 0 0 0; }"
                ".footer-grid .col { display: flex; flex-direction: column; gap: 6px; }"
                ".footer .col-title { font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.03em; }"
                ".version { background: #e9ecef; border-left: 4px solid #6c757d; }"
                ".location { background: #e7f3ff; border-left: 4px solid #0d6efd; }"
                "@media (max-width: 480px){.container{padding:12px;}.pwm-table th,.pwm-table td{padding:6px 8px;font-size:12px;}}"
                "@media (max-width: 420px){.pwm-table th:nth-child(2),.pwm-table td:nth-child(2),.pwm-table th:nth-child(5),.pwm-table td:nth-child(5){display:none;}}"
                "</style>"
            )
            await self._awrite(client_socket, style.encode('utf-8'))

            # Script block
            script = (
                "<script>let clockInterval;let refreshInterval;let countdownInterval;"
                "function startClock(h,m,s){const timeEl=document.getElementById('time');function pad(n){return(n<10?'0':'')+n;}"
                "function tick(){s+=1;if(s>=60){s=0;m+=1;}if(m>=60){m=0;h=(h+1)%24;}timeEl.textContent=pad(h)+':'+pad(m)+':'+pad(s);}" 
                "tick();clockInterval=setInterval(tick,1000);}" 
                "function startPageRefresh(){let s=180;function setT(){const e=document.getElementById('refresh-countdown');if(e){e.textContent='Next refresh in '+s+' seconds';return true}return false}" 
                "if(!setT()){const w=setInterval(function(){if(setT())clearInterval(w)},200)}function upd(){s--;if(s<=0){location.reload();return}setT()}" 
                "countdownInterval=setInterval(upd,1000);refreshInterval=setTimeout(function(){location.reload()},180000);}" 
                "window.addEventListener('beforeunload',function(){if(clockInterval)clearInterval(clockInterval);if(refreshInterval)clearTimeout(refreshInterval);if(countdownInterval)clearInterval(countdownInterval);});" 
                "function initTableFades(){const wrap=document.querySelector('.table-responsive');if(!wrap)return;const scroller=wrap.querySelector('.table-scroll')||wrap;function upd(){const max=scroller.scrollWidth-scroller.clientWidth;if(max<=0){wrap.classList.remove('has-left','has-right');return;}wrap.classList.toggle('has-left',scroller.scrollLeft>0);wrap.classList.toggle('has-right',scroller.scrollLeft<max-1);}scroller.addEventListener('scroll',upd,{passive:true});setTimeout(upd,0);}" 
                "</script>"
            )
            await self._awrite(client_socket, script.encode('utf-8'))
            await self._awrite(client_socket, b"</head>")

            # Body start
            await self._awrite(client_socket, f"<body onload=\"startClock({current_time[3]}, {current_time[4]}, {current_time[5]}); startPageRefresh(); initTableFades();\"><div class=\"container\">".encode('utf-8'))
            await self._awrite(client_socket, f"<h1>{config.WEB_TITLE}</h1>".encode('utf-8'))
            await self._awrite(client_socket, f"<div class=\"time\">🕒 <span id=\"time\">{time_str}</span><br><small>{date_str}</small></div>".encode('utf-8'))

            # Version
            await self._awrite(client_socket, f"<div class=\"status version\"><strong>🏷️ Config version:</strong> {current_config_version}</div>".encode('utf-8'))
            # Location
            await self._awrite(client_socket, f"<div class=\"status location\"><strong>📍 Location:</strong> {ui_location}</div>".encode('utf-8'))

            # WiFi
            wifi_class = 'online' if status.get('connections', {}).get('wifi', False) else 'offline'
            wifi_ssid = config_dict.get('wifi', {}).get('ssid', 'Unknown')
            wifi_ip = status.get('network', {}).get('ip', 'N/A')
            await self._awrite(client_socket, f"<div class=\"status {wifi_class}\"><strong>📶 WiFi:</strong> {wifi_ssid}, {wifi_ip}</div>".encode('utf-8'))

            # MQTT
            await self._awrite(client_socket, f"<div class=\"status {mqtt_class}\"><strong>🔌 MQTT:</strong> {mqtt_status}</div>".encode('utf-8'))

            # Controllers table header
            await self._awrite(client_socket, "<h2>🎛️ Controllers</h2>".encode('utf-8'))
            await self._awrite(client_socket, "<div class=\"table-responsive\"><div class=\"table-scroll\">".encode('utf-8'))
            await self._awrite(client_socket, (
                "<table class=\"pwm-table\"><thead><tr>"
                "<th>Name</th><th>Pin</th><th>Status</th><th>Current Window</th><th>Window Time</th><th>Duty Cycle</th>"
                "</tr></thead><tbody>"
            ).encode('utf-8'))

            # Populate rows from config (include disabled)
            pwm_pins_cfg = config_dict.get('pwm_pins', {})
            rows_written = 0
            for pin_key, pin_cfg in pwm_pins_cfg.items():
                if str(pin_key).startswith('_'):
                    continue
                enabled = pin_cfg.get('enabled', False)
                pin_live = pwm_status.get(pin_key, {
                    'name': pin_cfg.get('name', pin_key),
                    'gpio_pin': pin_cfg.get('gpio_pin', 0),
                    'duty_percent': 0
                })
                current_window = "None"
                window_time = "N/A"
                status_pins = status.get('pins', {})
                if pin_key in status_pins:
                    status_pin = status_pins[pin_key]
                    current_window = status_pin.get('window_display', 'None')
                    start_time = status_pin.get('window_start', 'N/A')
                    end_time = status_pin.get('window_end', 'N/A')
                    if start_time != 'N/A' and end_time != 'N/A':
                        window_time = f"{start_time} - {end_time}"

                duty_percent = pin_live.get('duty_percent', 0) if enabled else 0
                if not enabled:
                    active_status = "Inactive"
                    status_class = "disabled"
                else:
                    active_status = "Active" if duty_percent > 0 else "Inactive"
                    status_class = "active" if duty_percent > 0 else "inactive"

                row = (
                    f"<tr class=\"{status_class}\">"
                    f"<td>{pin_live.get('name', pin_cfg.get('name', pin_key))}</td>"
                    f"<td>GPIO {pin_live.get('gpio_pin', pin_cfg.get('gpio_pin', ''))}</td>"
                    f"<td>{active_status}</td>"
                    f"<td>{current_window}</td>"
                    f"<td>{window_time}</td>"
                    f"<td>{duty_percent}%</td>"
                    "</tr>"
                )
                await self._awrite(client_socket, row.encode('utf-8'))
                rows_written += 1

            if rows_written == 0:
                await self._awrite(client_socket, b"<tr><td colspan=\"6\" style=\"text-align: center; color: #666;\">No controllers configured</td></tr>")

            # Close table
            await self._awrite(client_socket, b"</tbody></table>")
            await self._awrite(client_socket, b"</div>")
            await self._awrite(client_socket, b"</div>")

            # Footer
            footer_top = (
                "<div class=\"footer\"><div class=\"footer-grid\">"
                "<div class=\"col\"><a href=\"/status\">📄 Status (JSON)</a></div>"
                "<div class=\"col\"><a href=\"/upload-config\">⬆️ Upload Config</a><a href=\"/upload-sun-times\">⬆️ Upload Sun Times</a></div>"
                "<div class=\"col\"><a href=\"/download-config\">⬇️ Download Config</a><a href=\"/download-sun-times\">⬇️ Download Sun Times</a></div>"
                "<div class=\"col\"><a href=\"/restart\">🔄 Restart Device</a></div>"
                "</div>"
            )
            await self._awrite(client_socket, footer_top.encode('utf-8'))
            await self._awrite(client_socket, (
                "<div style=\"margin-top:8px;font-size:12px;color:#666;\"><small><a href=\"https://github.com/m-anish/PagodaLightPico\" target=\"_blank\" rel=\"noopener\">PagodaLightPico</a></small></div>"
                "<div class=\"refresh-info\" id=\"refresh-countdown\"></div>"
                "</div>"
            ).encode('utf-8'))

            # Close body/html
            await self._awrite(client_socket, b"</div></body></html>")
        except Exception as e:
            log.error(f"[WEB] Error streaming main page: {e}")
            # Best-effort minimal error page so the browser shows something
            try:
                await self._awrite(client_socket, b"HTTP/1.1 500 Internal Server Error\r\n")
                await self._awrite(client_socket, b"Content-Type: text/html; charset=utf-8\r\n")
                await self._awrite(client_socket, b"Connection: close\r\n\r\n")
                await self._awrite(client_socket, b"<html><body><h1>Server Error</h1><p>Failed to render homepage.</p></body></html>")
            except Exception:
                pass
    
    async def stream_upload_page_chunked(self, client_socket):
        """Stream the config upload page (chunked upload JS) to minimize RAM usage."""
        try:
            # Send headers without Content-Length
            await self._awrite(client_socket, b"HTTP/1.1 200 OK\r\n")
            await self._awrite(client_socket, b"Content-Type: text/html; charset=utf-8\r\n")
            await self._awrite(client_socket, b"Connection: close\r\n\r\n")

            # Head start
            await self._awrite(client_socket, f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Config - {config.WEB_TITLE}</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        h1 {{ color: #2c3e50; text-align: center; }}
        .form-group {{ margin: 20px 0; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input[type=\"file\"] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }}
        .btn {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }}
        .btn:hover {{ background: #0056b3; }}
        .btn-secondary {{ background: #6c757d; }}
        .btn-secondary:hover {{ background: #545b62; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .footer {{ text-align: center; margin-top: 30px; }}
        .error {{ background: #f8d7da; border: 1px solid #f5c6cb; padding: 10px; border-radius: 4px; margin: 10px 0; color: #721c24; }}
        .ok {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 10px; border-radius: 4px; margin: 10px 0; color: #155724; }}
    </style>
    <script>
    // Minimal JS to reduce memory footprint
    (function() {{
        const CHUNK_SIZE = 1024; // smaller chunks to lower memory spikes
        function ge(id) {{ return document.getElementById(id); }}
        function setStatus(t) {{ ge('status').textContent = t; }}
        async function post(url, opts) {{
            const r = await fetch(url, opts || {{ method: 'POST' }});
            if (!r.ok) throw new Error(url + ' failed');
            return r;
        }}
        window.addEventListener('load', function() {{
            const form = ge('uploadForm');
            form.addEventListener('submit', async function(e) {{
                e.preventDefault();
                const f = ge('configFile').files[0];
                if (!f) return;
                ge('result').innerHTML = '';
                setStatus('Starting upload...');
                try {{
                    await post('/upload-config-begin');
                    let off = 0;
                    while (off < f.size) {{
                        const chunk = f.slice(off, Math.min(off + CHUNK_SIZE, f.size));
                        const buf = await chunk.arrayBuffer();
                        await post('/upload-config-chunk', {{ method: 'POST', headers: {{ 'Content-Type': 'application/octet-stream' }}, body: buf }});
                        off += CHUNK_SIZE;
                        setStatus(`Uploaded ${{Math.min(off, f.size)}} / ${{f.size}} bytes`);
                    }}
                    const resp = await post('/upload-config-finalize');
                    const text = await resp.text();
                    document.open(); document.write(text); document.close();
                }} catch (err) {{
                    setStatus('Error: ' + err.message);
                    ge('result').innerHTML = '<div class="error">Upload failed: ' + err.message + '</div>';
                }}
            }});
        }});
    }})();
    </script>
</head>
<body>
<div class=\"container\">""".encode('utf-8'))

            # Body content in small chunks
            await self._awrite(client_socket, b"<h1>Upload Configuration</h1>")
            await self._awrite(client_socket, b"<div class=\"warning\"><strong>Warning:</strong> Uploading a new configuration will replace the current settings and trigger a restart. Make sure your configuration is valid.</div>")
            await self._awrite(client_socket, b"<form id=\"uploadForm\">")
            await self._awrite(client_socket, b"<div class=\"form-group\"><label for=\"configFile\">Select config.json file:</label><input type=\"file\" id=\"configFile\" name=\"config\" accept=\".json\" required></div>")
            await self._awrite(client_socket, b"<div class=\"form-group\"><button type=\"submit\" class=\"btn\">Upload and Apply</button> <a href=\"/\" class=\"btn btn-secondary\" style=\"text-decoration: none; margin-left: 10px;\">Cancel</a></div>")
            await self._awrite(client_socket, b"<div id=\"status\"></div><div id=\"result\"></div>")
            await self._awrite(client_socket, b"</form>")
            await self._awrite(client_socket, b"<div class=\"footer\"><p><a href=\"/download-config\">Download Current Config</a> | <a href=\"/\">Back to Home</a></p></div>")
            await self._awrite(client_socket, b"</div></body></html>")
        except Exception as e:
            log.error(f"[WEB] Error streaming upload page: {e}")

    def generate_status_json(self):
        """Generate JSON status response."""
        try:
            import gc
            current_time = rtc_module.get_current_time()
            status = system_status.get_status_dict()
            
            # Sample memory
            try:
                gc.collect()
                mem_free = gc.mem_free()
                mem_alloc = gc.mem_alloc() if hasattr(gc, 'mem_alloc') else None
            except Exception:
                mem_free = None
                mem_alloc = None

            data = {
                'timestamp': time.time(),
                'current_time': {
                    'hour': current_time[3],
                    'minute': current_time[4],
                    'second': current_time[5],
                    'day': current_time[2],
                    'month': current_time[1],
                    'year': current_time[0]
                },
                'memory': {
                    'free': mem_free,
                    'alloc': mem_alloc
                }
            }
            # Merge the status dictionary into data
            data.update(status)
            
            json_str = json.dumps(data)
            body = json_str.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + json_str
            return response
            
        except Exception as e:
            log.error(f"[WEB] Error generating status JSON: {e}")
            return self.generate_500()
    
    def generate_404(self):
        """Generate 404 response."""
        html = """<!DOCTYPE html>
<html>
<head><title>404 Not Found</title></head>
<body>
    <h1>404 - Page Not Found</h1>
    <p><a href="/">Back to Home</a></p>
</body>
</html>"""
        body = html.encode('utf-8')
        response = (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ) + html
        return response
    
    def generate_500(self):
        """Generate 500 response."""
        html = """<!DOCTYPE html>
<html>
<head><title>500 Server Error</title></head>
<body>
    <h1>500 - Server Error</h1>
    <p><a href="/">Back to Home</a></p>
</body>
</html>"""
        body = html.encode('utf-8')
        response = (
            "HTTP/1.1 500 Internal Server Error\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ) + html
        return response

    def generate_restart_page(self):
        """Generate a page that informs the user of an imminent restart and triggers it."""
        try:
            # Schedule the hard reset with a short delay so the response can flush
            asyncio.create_task(self.soft_reboot_delayed())

            html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Restarting - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="8;url=/">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .success {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 4px; margin: 20px 0; color: #155724; }}
    </style>
    <script>
        // Simple countdown display
        let seconds = 5;
        function tick() {{
            const el = document.getElementById('count');
            if (!el) return;
            el.textContent = seconds;
            seconds--;
            if (seconds >= 0) setTimeout(tick, 1000);
        }}
        window.onload = tick;
    </script>
</head>
<body>
    <div class="container">
        <h1>Restarting Device</h1>
        <div class="success">
            <p>The device will restart in <strong id="count">5</strong> seconds.</p>
            <p>You will be redirected to the home page automatically after restart.</p>
        </div>
        <p><a href="/">Return to Home</a></p>
    </div>
</body>
</html>"""
            body = html.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + html
            return response
        except Exception as e:
            log.error(f"[WEB] Error generating restart page: {e}")
            return self.generate_500()
    
    def generate_config_download(self):
        """Generate config.json download response."""
        try:
            # Read current config file
            with open('config.json', 'r') as f:
                config_content = f.read()
            
            # Generate download response with proper headers
            body = config_content.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Content-Disposition: attachment; filename=\"config.json\"\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + config_content
            return response
            
        except Exception as e:
            log.error(f"[WEB] Error generating config download: {e}")
            return self.generate_500()

    def generate_sun_times_download(self):
        """Generate sun_times.json download response."""
        try:
            # Read current sun_times file
            with open('sun_times.json', 'r') as f:
                sun_times_content = f.read()

            # Generate download response with proper headers
            body = sun_times_content.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Content-Disposition: attachment; filename=\"sun_times.json\"\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + sun_times_content
            return response

        except Exception as e:
            log.error(f"[WEB] Error generating sun_times download: {e}")
            return self.generate_500()
    
    def generate_upload_page(self):
        """Generate config upload page."""
        try:
            html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Config - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        h1 {{ color: #2c3e50; text-align: center; }}
        .form-group {{ margin: 20px 0; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input[type="file"] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }}
        .btn {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }}
        .btn:hover {{ background: #0056b3; }}
        .btn-secondary {{ background: #6c757d; }}
        .btn-secondary:hover {{ background: #545b62; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .footer {{ text-align: center; margin-top: 30px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Upload Configuration</h1>
        
        <div class="warning">
            <strong>Warning:</strong> Uploading a new configuration will replace the current settings and trigger a restart. 
            Make sure your configuration is valid to avoid system issues.
        </div>
        
        <form method="POST" enctype="multipart/form-data">
            <div class="form-group">
                <label for="config-file">Select config.json file:</label>
                <input type="file" id="config-file" name="config" accept=".json" required>
            </div>
            
            <div class="form-group">
                <button type="submit" class="btn">Upload and Apply</button>
                <a href="/" class="btn btn-secondary" style="text-decoration: none; margin-left: 10px;">Cancel</a>
            </div>
        </form>
        
        <div class="footer">
            <p><a href="/download-config">Download Current Config</a> | <a href="/">Back to Home</a></p>
        </div>
    </div>
</body>
</html>"""
            
            body = html.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + html
            return response
            
        except Exception as e:
            log.error(f"[WEB] Error generating upload page: {e}")
            return self.generate_500()
    
    def generate_upload_page_chunked(self):
        """Generate config upload page with chunked upload support."""
        try:
            html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Config - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        h1 {{ color: #2c3e50; text-align: center; }}
        .form-group {{ margin: 20px 0; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input[type="file"] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }}
        .btn {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }}
        .btn:hover {{ background: #0056b3; }}
        .btn-secondary {{ background: #6c757d; }}
        .btn-secondary:hover {{ background: #545b62; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .footer {{ text-align: center; margin-top: 30px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Upload Configuration</h1>
        
        <div class="warning">
            <strong>Warning:</strong> Uploading a new configuration will replace the current settings and trigger a restart. 
            Make sure your configuration is valid to avoid system issues.
        </div>
        
        <form id="uploadForm">
            <div class="form-group">
                <label for="configFile">Select config.json file:</label>
                <input type="file" id="configFile" name="config" accept=".json" required>
            </div>
            
            <div class="form-group">
                <button type="submit" class="btn">Upload and Apply</button>
                <a href="/" class="btn btn-secondary" style="text-decoration: none; margin-left: 10px;">Cancel</a>
            </div>
            <div id="progress" style="display: none; margin: 10px 0;">
                <div id="bar" style="background-color: #007bff; height: 10px; width: 0%;"></div>
                <span id="percent">0%</span>
            </div>
            <div id="status"></div>
            <div id="result"></div>
        </form>
        
        <div class="footer">
            <p><a href="/download-config">Download Current Config</a> | <a href="/">Back to Home</a></p>
        </div>
    </div>
    <script>
    (function() {{
        const form = document.getElementById('uploadForm');
        const fileInput = document.getElementById('configFile');
        const progress = document.getElementById('progress');
        const statusEl = document.getElementById('status');
        const resultEl = document.getElementById('result');
        const bar = document.getElementById('bar');
        const percentEl = document.getElementById('percent');
        const CHUNK_SIZE = 4096;

        function setStatus(txt) {{ statusEl.textContent = txt; }}
        function setProgress(done, total) {{
            const pct = total ? Math.floor(done * 100 / total) : 0;
            progress.style.display = 'block';
            bar.style.width = pct + '%';
            percentEl.textContent = pct + '%';
        }}

        form.addEventListener('submit', async function(e) {{
            e.preventDefault();
            const file = fileInput.files[0];
            if (!file) {{ return; }}
            resultEl.innerHTML = '';
            setStatus('Starting upload...');
            setProgress(0, 100);
            try {{
                // Begin
                let resp = await fetch('/upload-config-begin', {{ method: 'POST' }});
                if (!resp.ok) throw new Error('Failed to begin upload');

                // Send chunks
                let offset = 0;
                while (offset < file.size) {{
                    const chunk = file.slice(offset, Math.min(offset + CHUNK_SIZE, file.size));
                    const buf = await chunk.arrayBuffer();
                    resp = await fetch('/upload-config-chunk', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/octet-stream' }},
                        body: buf
                    }});
                    if (!resp.ok) throw new Error('Chunk upload failed at offset ' + offset);
                    offset += CHUNK_SIZE;
                    setStatus(`Uploaded ${{Math.min(offset, file.size)}} / ${{file.size}} bytes`);
                    setProgress(Math.min(offset, file.size), file.size);
                }}

                // Finalize
                resp = await fetch('/upload-config-finalize', {{ method: 'POST' }});
                if (!resp.ok) throw new Error('Finalize failed');
                const text = await resp.text();
                // Server returns an HTML success page (restart page). Replace document.
                document.open(); document.write(text); document.close();
            }} catch (err) {{
                setStatus('Error: ' + err.message);
                resultEl.innerHTML = '<div class=\"error\">Upload failed: ' + err.message + '</div>';
            }}
        }});
    }})();
    </script>
    </html>"""
            
            body = html.encode('utf-8')
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + html
            return response
            
        except Exception as e:
            log.error(f"[WEB] Error generating upload page: {e}")
            return self.generate_500()

    def _tmp_config_path(self):
        return 'config.json.upload'

    def _json_response(self, status_code, obj):
        try:
            s = json.dumps(obj)
        except Exception:
            s = '{}'
        body = s.encode('utf-8')
        return (
            f"HTTP/1.1 {status_code} OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ) + s

    def handle_config_upload_begin(self):
        """Begin chunked upload: create/truncate temp file."""
        try:
            # Remove any previous temp
            try:
                os.remove(self._tmp_config_path())
            except Exception:
                pass
            with open(self._tmp_config_path(), 'wb') as f:
                pass  # just create/truncate
            return self._json_response(200, { 'ok': True })
        except Exception as e:
            log.error(f"[WEB] upload-begin error: {e}")
            return self._json_response(500, { 'ok': False, 'error': 'begin failed' })

    def handle_config_upload_chunk(self, body_bytes, headers_text):
        """Append a binary chunk to temp file."""
        try:
            # Basic safety: ensure temp exists
            with open(self._tmp_config_path(), 'ab') as f:
                f.write(body_bytes)
            # Report current size
            size = 0
            try:
                stat = os.stat(self._tmp_config_path())
                size = stat[6] if isinstance(stat, tuple) else stat.st_size
            except Exception:
                pass
            return self._json_response(200, { 'ok': True, 'size': size })
        except Exception as e:
            log.error(f"[WEB] upload-chunk error: {e}")
            return self._json_response(500, { 'ok': False, 'error': 'chunk failed' })

    async def handle_config_upload_finalize(self, request_str):
        """Validate temp config and replace current config.json; then show restart page."""
        try:
            # Read uploaded file
            with open(self._tmp_config_path(), 'r') as f:
                uploaded_text = f.read()
            # Parse JSON to ensure validity
            uploaded_json = json.loads(uploaded_text)

            # Version compatibility check
            expected_prefix = self._expected_version_prefix()
            up_ver = str(uploaded_json.get('version', '')).strip()
            if not up_ver:
                raise ValueError("Uploaded config missing 'version'")
            if not self._version_compatible(up_ver, expected_prefix):
                raise ValueError(f"Version {up_ver} not compatible with required {expected_prefix}.*")

            # Backup current config and replace atomically
            try:
                if os.stat('config.json'):
                    try:
                        os.remove('config.json.backup')
                    except Exception:
                        pass
                    os.rename('config.json', 'config.json.backup')
            except Exception:
                pass

            # Move temp into place
            try:
                # Write validated JSON to ensure formatting
                with open('config.json', 'w') as f:
                    f.write(uploaded_text)
            except Exception as e:
                # restore backup if write failed
                try:
                    os.rename('config.json.backup', 'config.json')
                except Exception:
                    pass
                raise e
            finally:
                try:
                    os.remove(self._tmp_config_path())
                except Exception:
                    pass

            # Return restart page and schedule reset
            return self.generate_restart_page()
        except Exception as e:
            log.error(f"[WEB] upload-finalize error: {e}")
            try:
                os.remove(self._tmp_config_path())
            except Exception:
                pass
            return self.generate_upload_error(f"Finalize failed: {e}")
    
    async def handle_config_upload(self, request_str):
        """Handle config file upload and validation."""
        try:
            # Parse multipart form data (simplified and robust)
            lines = request_str.split('\r\n')

            # Find boundary from headers
            boundary = None
            for line in lines:
                if line.startswith('Content-Type: multipart/form-data'):
                    parts = line.split('boundary=')
                    if len(parts) > 1:
                        boundary = '--' + parts[1].strip()
                        break

            if not boundary:
                return self.generate_upload_error("Invalid multipart data")

            # Split parts by boundary and extract the part with a filename
            file_content = None
            parts = request_str.split(boundary)
            for part in parts:
                if 'Content-Disposition: form-data' in part and 'filename=' in part:
                    # Separate headers and body of this part
                    if '\r\n\r\n' in part:
                        body = part.split('\r\n\r\n', 1)[1]
                        # Trim the trailing CRLF and any ending markers
                        body = body.strip('\r\n')
                        # Exclude potential closing boundary markers that may be concatenated
                        if body.endswith('--'):
                            body = body[:-2]
                        file_content = body.strip()
                        break

            if not file_content:
                return self.generate_upload_error("No file content found")
            
            # Validate JSON
            try:
                config_data = json.loads(file_content)
            except json.JSONDecodeError as e:
                return self.generate_upload_error(f"Invalid JSON format: {e}")
            
            # Version compatibility check (require matching major.minor against current running config)
            try:
                uploaded_ver = str(config_data.get('version', '')).strip()
                if not uploaded_ver:
                    return self.generate_upload_error("Missing 'version' in config.json. Please use sample config for your release.")
                expected_prefix = self._expected_version_prefix()
                if not self._version_compatible(uploaded_ver, expected_prefix):
                    return self.generate_upload_error(f"Incompatible config version '{uploaded_ver}'. Expected {expected_prefix}.x (to match device config)")
            except Exception as e:
                return self.generate_upload_error(f"Version check failed: {e}")
            
            # Backup current config
            try:
                os.rename('config.json', 'config.json.backup')
            except:
                pass  # Backup failed, continue anyway
            
            # Save new config
            try:
                with open('config.json', 'w') as f:
                    f.write(file_content)
                
                # Validate the new config using config manager
                config.config_manager.reload()
                
                # If we get here, config is valid
                log.info("[WEB] New configuration uploaded and validated successfully")
                
                # Generate success response with auto-reboot
                html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Success - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="8;url=/">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .success {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 4px; margin: 20px 0; color: #155724; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Configuration Updated Successfully</h1>
        <div class="success">
            <p>The new configuration has been uploaded and validated. The system will restart in a few seconds.</p>
            <p>You will be redirected to the home page automatically.</p>
        </div>
        <p><a href="/">Return to Home</a></p>
    </div>
</body>
</html>"""
                
                # Schedule soft reboot after response is sent
                asyncio.create_task(self.soft_reboot_delayed())
                
                body = html.encode('utf-8')
                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ) + html
                return response
                
            except Exception as e:
                # Restore backup if validation failed
                try:
                    os.rename('config.json.backup', 'config.json')
                except:
                    pass
                return self.generate_upload_error(f"Configuration validation failed: {e}")
                
        except Exception as e:
            log.error(f"[WEB] Error handling config upload: {e}")
            return self.generate_upload_error(f"Upload processing failed: {e}")
    
    def generate_upload_error(self, error_msg):
        """Generate upload error page."""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Error - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        .error {{ background: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; border-radius: 4px; margin: 20px 0; color: #721c24; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Upload Failed</h1>
        <div class="error">
            <p><strong>Error:</strong> {error_msg}</p>
        </div>
        <p><a href="/upload-config">Try Again</a> | <a href="/">Back to Home</a></p>
    </div>
</body>
</html>"""
        
        body = html.encode('utf-8')
        response = (
            "HTTP/1.1 400 Bad Request\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ) + html
        return response
    
    async def stream_upload_sun_times_page_chunked(self, client_socket):
        """Stream the sun_times upload page with chunked JS upload to reduce RAM."""
        try:
            await self._awrite(client_socket, b"HTTP/1.1 200 OK\r\n")
            await self._awrite(client_socket, b"Content-Type: text/html; charset=utf-8\r\n")
            await self._awrite(client_socket, b"Connection: close\r\n\r\n")

            await self._awrite(client_socket, f"""<!DOCTYPE html>
<html>
<head>
    <title>Upload Sun Times - {config.WEB_TITLE}</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        h1 {{ color: #2c3e50; text-align: center; }}
        .form-group {{ margin: 20px 0; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input[type=\"file\"] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }}
        .btn {{ background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }}
        .btn:hover {{ background: #0056b3; }}
        .btn-secondary {{ background: #6c757d; }}
        .btn-secondary:hover {{ background: #545b62; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .footer {{ text-align: center; margin-top: 30px; }}
        .error {{ background: #f8d7da; border: 1px solid #f5c6cb; padding: 10px; border-radius: 4px; margin: 10px 0; color: #721c24; }}
        .ok {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 10px; border-radius: 4px; margin: 10px 0; color: #155724; }}
    </style>
    <script>
    (function() {{
        const CHUNK_SIZE = 1024;
        function ge(id) {{ return document.getElementById(id); }}
        function setStatus(t) {{ ge('status').textContent = t; }}
        async function post(url, opts) {{
            const r = await fetch(url, opts || {{ method: 'POST' }});
            if (!r.ok) throw new Error(url + ' failed');
            return r;
        }}
        window.addEventListener('load', function() {{
            const form = ge('uploadForm');
            form.addEventListener('submit', async function(e) {{
                e.preventDefault();
                const f = ge('sunFile').files[0];
                if (!f) return;
                ge('result').innerHTML = '';
                setStatus('Starting upload...');
                try {{
                    await post('/upload-sun-times-begin');
                    let off = 0;
                    while (off < f.size) {{
                        const chunk = f.slice(off, Math.min(off + CHUNK_SIZE, f.size));
                        const buf = await chunk.arrayBuffer();
                        await post('/upload-sun-times-chunk', {{ method: 'POST', headers: {{ 'Content-Type': 'application/octet-stream' }}, body: buf }});
                        off += CHUNK_SIZE;
                        setStatus(`Uploaded ${{Math.min(off, f.size)}} / ${{f.size}} bytes`);
                    }}
                    const resp = await post('/upload-sun-times-finalize');
                    const text = await resp.text();
                    document.open(); document.write(text); document.close();
                }} catch (err) {{
                    setStatus('Error: ' + err.message);
                    ge('result').innerHTML = '<div class=\"error\">Upload failed: ' + err.message + '</div>';
                }}
            }});
        }});
    }})();
    </script>
</head>
<body>
<div class=\"container\">""".encode('utf-8'))

            await self._awrite(client_socket, b"<h1>Upload Sun Times</h1>")
            await self._awrite(client_socket, b"<div class=\"warning\"><strong>Note:</strong> Uploading a new sun_times.json replaces the current sunrise/sunset schedule without restarting.</div>")
            await self._awrite(client_socket, b"<form id=\"uploadForm\">")
            await self._awrite(client_socket, b"<div class=\"form-group\"><label for=\"sunFile\">Select sun_times.json file:</label><input type=\"file\" id=\"sunFile\" name=\"sun_times\" accept=\".json\" required></div>")
            await self._awrite(client_socket, b"<div class=\"form-group\"><button type=\"submit\" class=\"btn\">Upload</button> <a href=\"/\" class=\"btn btn-secondary\" style=\"text-decoration: none; margin-left: 10px;\">Cancel</a></div>")
            await self._awrite(client_socket, b"<div id=\"status\"></div><div id=\"result\"></div>")
            await self._awrite(client_socket, b"</form>")
            await self._awrite(client_socket, b"<div class=\"footer\"><p><a href=\"/download-sun-times\">Download Current Sun Times</a> | <a href=\"/\">Back to Home</a></p></div>")
            await self._awrite(client_socket, b"</div></body></html>")
        except Exception as e:
            log.error(f"[WEB] Error streaming sun times upload page: {e}")

    def _tmp_sun_times_path(self):
        return 'sun_times.json.upload'

    def handle_sun_times_upload_begin(self):
        """Begin chunked upload for sun_times.json."""
        try:
            try:
                os.remove(self._tmp_sun_times_path())
            except Exception:
                pass
            with open(self._tmp_sun_times_path(), 'wb') as f:
                pass
            return self._json_response(200, { 'ok': True })
        except Exception as e:
            log.error(f"[WEB] sun-begin error: {e}")
            return self._json_response(500, { 'ok': False, 'error': 'begin failed' })

    def handle_sun_times_upload_chunk(self, body_bytes, headers_text):
        """Append a chunk to temporary sun_times upload file."""
        try:
            with open(self._tmp_sun_times_path(), 'ab') as f:
                f.write(body_bytes)
            size = os.stat(self._tmp_sun_times_path())[6]
            return self._json_response(200, { 'ok': True, 'size': size })
        except Exception as e:
            log.error(f"[WEB] sun-chunk error: {e}")
            return self._json_response(500, { 'ok': False, 'error': 'chunk failed' })

    async def handle_sun_times_upload_finalize(self):
        """Validate temp sun_times and replace file atomically."""
        try:
            # Read uploaded file
            with open(self._tmp_sun_times_path(), 'r') as f:
                uploaded_text = f.read()
            data = json.loads(uploaded_text)

            # Validate structure using existing helper
            if not self.validate_sun_times_structure(data):
                raise ValueError('Invalid sun_times.json structure')

            # Backup current file if exists
            try:
                if os.stat('sun_times.json'):
                    try:
                        os.remove('sun_times.json.backup')
                    except Exception:
                        pass
                    os.rename('sun_times.json', 'sun_times.json.backup')
            except Exception:
                pass

            # Write to temp and replace atomically
            try:
                with open('sun_times.json.new', 'w') as nf:
                    nf.write(uploaded_text)
                try:
                    os.remove('sun_times.json')
                except Exception:
                    pass
                os.rename('sun_times.json.new', 'sun_times.json')
                try:
                    os.remove('sun_times.json.backup')
                except Exception:
                    pass
            except Exception as e:
                # Restore backup
                try:
                    os.rename('sun_times.json.backup', 'sun_times.json')
                except Exception:
                    pass
                raise e
            finally:
                try:
                    os.remove(self._tmp_sun_times_path())
                except Exception:
                    pass

            # Success page (no restart needed)
            html = """<!DOCTYPE html>
<html><head><title>Sun Times Updated</title><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head>
<body><div class=\"container\"><h1>Sun Times Updated</h1><p>sun_times.json has been updated successfully.</p><p><a href=\"/\">Back to Home</a></p></div></body></html>"""
            body = html.encode('utf-8')
            return (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + html
        except Exception as e:
            log.error(f"[WEB] sun-finalize error: {e}")
            try:
                os.remove(self._tmp_sun_times_path())
            except Exception:
                pass
            err = f"Finalize failed: {e}"
            html = f"""<!DOCTYPE html>
<html><head><title>Sun Times Upload Error</title><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head>
<body><div class=\"container\"><h1>Upload Failed</h1><p><strong>Error:</strong> {err}</p><p><a href=\"/upload-sun-times\">Try Again</a> | <a href=\"/\">Home</a></p></div></body></html>"""
            body = html.encode('utf-8')
            return (
                "HTTP/1.1 400 Bad Request\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ) + html
    
    async def handle_sun_times_upload(self, request_str):
        """Handle sun_times.json file upload and validation."""
        try:
            # Parse multipart form data (simplified and robust)
            lines = request_str.split('\r\n')

            # Find boundary from headers
            boundary = None
            for line in lines:
                if line.startswith('Content-Type: multipart/form-data'):
                    parts = line.split('boundary=')
                    if len(parts) > 1:
                        boundary = '--' + parts[1].strip()
                        break

            if not boundary:
                return self.generate_sun_times_upload_error("Invalid multipart data")

            # Split parts by boundary and extract the part with a filename
            file_content = None
            parts = request_str.split(boundary)
            for part in parts:
                if 'Content-Disposition: form-data' in part and 'filename=' in part:
                    if '\r\n\r\n' in part:
                        body = part.split('\r\n\r\n', 1)[1]
                        body = body.strip('\r\n')
                        if body.endswith('--'):
                            body = body[:-2]
                        file_content = body.strip()
                        break

            if not file_content:
                return self.generate_sun_times_upload_error("No file content found")
            
            # Validate JSON
            try:
                sun_times_data = json.loads(file_content)
            except json.JSONDecodeError as e:
                return self.generate_sun_times_upload_error(f"Invalid JSON format: {e}")
            
            # Basic validation of sun_times structure
            if not self.validate_sun_times_structure(sun_times_data):
                return self.generate_sun_times_upload_error("Invalid sun_times.json structure. Expected format with 'location', 'lat', 'lon', and 'days' fields.")
            
            # Version compatibility check (require matching major.minor against current running config)
            try:
                uploaded_ver = str(sun_times_data.get('version', '')).strip()
                if not uploaded_ver:
                    return self.generate_sun_times_upload_error("Missing 'version' in sun_times.json. Please use sample file for your release.")
                expected_prefix = self._expected_version_prefix()
                if not self._version_compatible(uploaded_ver, expected_prefix):
                    return self.generate_sun_times_upload_error(f"Incompatible sun_times version '{uploaded_ver}'. Expected {expected_prefix}.x (to match device config)")
            except Exception as e:
                return self.generate_sun_times_upload_error(f"Version check failed: {e}")
            
            # Backup current sun_times
            try:
                os.rename('sun_times.json', 'sun_times.json.backup')
            except:
                pass  # Backup failed, continue anyway
            
            # Save new sun_times
            try:
                with open('sun_times.json', 'w') as f:
                    f.write(file_content)
                
                log.info("[WEB] New sun_times.json uploaded successfully")
                
                # Generate success response with auto-reboot
                html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Sun Times Upload Success - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="8;url=/">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .success {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 4px; margin: 20px 0; color: #155724; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Sun Times Data Updated Successfully</h1>
        <div class="success">
            <p>The new sun times data has been uploaded successfully. The system will restart in a few seconds.</p>
            <p>You will be redirected to the home page automatically.</p>
        </div>
        <p><a href="/">Return to Home</a></p>
    </div>
</body>
</html>"""
                
                # Schedule soft reboot after response is sent
                asyncio.create_task(self.soft_reboot_delayed())
                
                response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(html)}\r\nConnection: close\r\n\r\n{html}"
                return response
                
            except Exception as e:
                # Restore backup if save failed
                try:
                    os.rename('sun_times.json.backup', 'sun_times.json')
                except:
                    pass
                return self.generate_sun_times_upload_error(f"Failed to save sun times data: {e}")
                
        except Exception as e:
            log.error(f"[WEB] Error handling sun times upload: {e}")
            return self.generate_sun_times_upload_error(f"Upload processing failed: {e}")

    def _major_minor(self, ver_str):
        """Return 'major.minor' part of a semantic version string like '0.2.0' -> '0.2'."""
        try:
            parts = str(ver_str).split('.')
            if len(parts) < 2:
                return ver_str
            return parts[0] + '.' + parts[1]
        except Exception:
            return ver_str

    def _version_compatible(self, uploaded_ver, expected_prefix):
        """Check if uploaded version matches expected major.minor prefix."""
        return self._major_minor(uploaded_ver) == expected_prefix

    def _expected_version_prefix(self):
        """Return required major.minor from current running config.json's version.
        Raises ValueError if the running config has no valid major.minor version.
        """
        try:
            current_ver = str(config.config_manager.get_config_dict().get('version', '')).strip()
            if not current_ver:
                raise ValueError("Running config has no 'version'.")
            mm = self._major_minor(current_ver)
            if "." not in mm:
                raise ValueError(f"Running config version '{current_ver}' is invalid; expected major.minor.patch like '0.2.0'.")
            return mm
        except Exception as e:
            # Re-raise to force callers to handle as hard error
            raise e
    
    def validate_sun_times_structure(self, data):
        """Validate basic structure of sun_times.json data."""
        try:
            # Check required top-level fields
            required_fields = ['location', 'lat', 'lon', 'days']
            for field in required_fields:
                if field not in data:
                    return False
            
            # Check that days is a dict
            if not isinstance(data['days'], dict):
                return False
            
            # Check that lat/lon are numbers
            if not isinstance(data['lat'], (int, float)) or not isinstance(data['lon'], (int, float)):
                return False
            
            # Check a few day entries have proper format
            for date_key, day_data in list(data['days'].items())[:3]:  # Check first 3 entries
                if not isinstance(day_data, dict):
                    return False
                if 'rise' not in day_data or 'set' not in day_data:
                    return False
                # Basic time format check (HH:MM)
                rise_time = day_data['rise']
                set_time = day_data['set']
                if not (isinstance(rise_time, str) and ':' in rise_time):
                    return False
                if not (isinstance(set_time, str) and ':' in set_time):
                    return False
            
            return True
            
        except Exception:
            return False
    
    def generate_sun_times_upload_error(self, error_msg):
        """Generate sun times upload error page."""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Sun Times Upload Error - {config.WEB_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
        .error {{ background: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; border-radius: 4px; margin: 20px 0; color: #721c24; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Sun Times Upload Failed</h1>
        <div class="error">
            <p><strong>Error:</strong> {error_msg}</p>
        </div>
        <p><a href="/upload-sun-times">Try Again</a> | <a href="/">Back to Home</a></p>
    </div>
</body>
</html>"""
        
        response = f"HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\nContent-Length: {len(html)}\r\nConnection: close\r\n\r\n{html}"
        return response

    async def soft_reboot_delayed(self):
        """Perform hard reset after a short delay."""
        try:
            # Wait a bit longer to ensure the HTTP response is flushed to client
            await asyncio.sleep(5)
            log.info("[WEB] Performing hard reset after file update")
            # Hard reset is more reliable to clear all state (sockets, tasks)
            machine.reset()
        except Exception as e:
            log.error(f"[WEB] Error during hard reset: {e}")

# Global web server instance
web_server = AsyncWebServer()