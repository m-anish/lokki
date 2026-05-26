"""Tiny mDNS A-record responder for the coordinator.

`network.hostname(...)` works on MicroPython builds that compiled lwIP
with `LWIP_MDNS_RESPONDER`; not all RP2 builds do. When the built-in
responder is missing, `lokki.local` doesn't resolve on the LAN even
though the DHCP hostname is set. This module is the Python-side
fallback: join the mDNS multicast group, watch for A queries for
`<hostname>.local`, answer with our STA IP.

Scope is deliberately minimal:
  - Only A records (IPv4). AAAA / PTR / SRV / TXT are out of scope.
  - Multicast responses only. No unicast-response handling.
  - No probing / conflict detection / goodbye. We assume one coord per
    LAN; two coords advertising the same name will cause the LAN
    resolver to see duplicate answers, not a hard failure.
  - Gratuitous announcement at startup so clients with stale cache
    entries refresh.

RAM: ~6 KB steady-state (one UDP socket + 512 B recv buffer).
"""
import asyncio
import socket
import struct

from shared.simple_logger import Logger

log = Logger()

_MDNS_ADDR = "224.0.0.251"
_MDNS_PORT = 5353
_TTL_S     = 120

_QTYPE_A   = 0x0001
_QTYPE_ANY = 0x00FF

# socket.IPPROTO_IP isn't exposed on every MicroPython build (the RP2
# Pico W port omits it in some firmwares — same family of constants
# that's missing IPPROTO_UDP). Fall back to the numeric value (0)
# which is the IP-level options selector regardless.
try:
    _IPPROTO_IP = socket.IPPROTO_IP
except AttributeError:
    _IPPROTO_IP = 0


def _parse_question(packet, offset):
    """Parse one DNS question starting at `offset` in `packet`.

    Returns (name, qtype, qclass, new_offset) or None on malformed
    input. Name comes back as lowercase dotted string. Compression
    pointers in the question section are rare; if we see one we bail
    rather than implement full pointer chasing.
    """
    labels = []
    while True:
        if offset >= len(packet):
            return None
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0:
            return None
        offset += 1
        if offset + length > len(packet):
            return None
        try:
            labels.append(packet[offset:offset + length].decode("utf-8", "ignore"))
        except Exception:
            return None
        offset += length
    if offset + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack(">HH", packet[offset:offset + 4])
    return (".".join(labels).lower(), qtype, qclass, offset + 4)


def _encode_name(name):
    out = bytearray()
    for part in name.split("."):
        if not part:
            continue
        out.append(len(part))
        out.extend(part.encode())
    out.append(0)
    return bytes(out)


def _build_response(fqdn, ip):
    """Build an mDNS unsolicited / response packet announcing
    `fqdn` → `ip`. Always multicast; the cache-flush bit in class
    tells clients to drop any prior cached answer for this name."""
    header = struct.pack(
        ">HHHHHH",
        0,           # transaction ID — must be 0 for mDNS responses
        0x8400,      # flags: response, authoritative answer
        0,           # qdcount
        1,           # ancount
        0, 0,        # nscount, arcount
    )
    answer = _encode_name(fqdn)
    answer += struct.pack(
        ">HHIH",
        _QTYPE_A,    # TYPE
        0x8001,      # CLASS = IN with cache-flush bit
        _TTL_S,
        4,           # RDLENGTH (IPv4)
    )
    answer += socket.inet_aton(ip)
    return header + answer


class MDNSResponder:

    def __init__(self):
        self.hostname = "lokki"
        self.fqdn     = "lokki.local"
        self._sock    = None
        self._ip      = None

    def init(self, hostname, ip):
        """Bind UDP/5353, join the mDNS multicast group, stash the
        announce data. Returns True iff the socket is live and we're
        ready to respond.

        EADDRINUSE on 5353 means lwIP's own mDNS responder is already
        listening on the port (network.hostname() activates it on
        builds that compiled LWIP_MDNS_RESPONDER). We try SO_REUSEPORT
        first to coexist (duplicate mDNS answers are harmless; the OS
        resolver dedupes); if that's not supported either, log it as a
        non-error and let lwIP do the work.
        """
        self.hostname = (hostname or "lokki").lower().rstrip(".")
        self.fqdn     = self.hostname + ".local"
        self._ip      = ip

        # AF_INET + SOCK_DGRAM defaults to UDP on every MicroPython
        # build; passing IPPROTO_UDP explicitly fails on RP2 because
        # that constant isn't exposed.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            # Not all MicroPython builds expose REUSEADDR. Non-fatal.
            pass
        # SO_REUSEPORT lets us share UDP/5353 with lwIP's built-in
        # responder when present. MicroPython exposes the symbol on
        # some builds; on the others we fall through to the numeric
        # value (15 on Linux, which is what lwIP-on-MicroPython
        # follows). If both are unsupported the bind below will
        # EADDRINUSE and we handle it gracefully there.
        try:
            so_reuseport = getattr(socket, "SO_REUSEPORT", 15)
            sock.setsockopt(socket.SOL_SOCKET, so_reuseport, 1)
        except Exception:
            pass
        try:
            sock.bind(("", _MDNS_PORT))
        except OSError as e:
            errno = e.args[0] if e.args else None
            if errno == 98 or "EADDRINUSE" in str(e):
                log.info(
                    "[MDNS] UDP/5353 already bound — lwIP's built-in "
                    "responder is handling mDNS; Python responder standing down"
                )
            else:
                log.error(f"[MDNS] Bind UDP/5353 failed: {e}")
            try: sock.close()
            except Exception: pass
            return False
        except Exception as e:
            log.error(f"[MDNS] Bind UDP/5353 failed: {e}")
            try: sock.close()
            except Exception: pass
            return False
        try:
            mreq = socket.inet_aton(_MDNS_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(_IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            log.error(f"[MDNS] Multicast group join failed (lwIP w/o IGMP?): {e}")
            try: sock.close()
            except Exception: pass
            return False
        sock.setblocking(False)
        self._sock = sock
        log.info(f"[MDNS] Responder up; advertising {self.fqdn} → {ip}")
        return True

    def _send_announce(self):
        try:
            packet = _build_response(self.fqdn, self._ip)
            self._sock.sendto(packet, (_MDNS_ADDR, _MDNS_PORT))
        except Exception as e:
            log.warn(f"[MDNS] Announce send failed: {e}")

    async def run(self):
        """Async task. Sends a gratuitous announcement at startup, then
        loops responding to A queries for our FQDN."""
        if self._sock is None:
            return
        # Gratuitous announce so clients with stale cache entries
        # update immediately rather than waiting until their TTL
        # expires. Also helps phones that didn't see our DHCP lease
        # event discover us on connect.
        self._send_announce()

        while True:
            try:
                try:
                    data, _addr = self._sock.recvfrom(512)
                except OSError:
                    await asyncio.sleep_ms(50)
                    continue

                if len(data) < 12:
                    continue
                # mDNS header
                _txid, flags, qdcount, _ancount, _nscount, _arcount = struct.unpack(
                    ">HHHHHH", data[:12]
                )
                # Top flag bit set = this is a response. Ignore — we
                # only act on incoming queries.
                if flags & 0x8000:
                    continue

                offset = 12
                matched = False
                for _ in range(qdcount):
                    q = _parse_question(data, offset)
                    if q is None:
                        break
                    name, qtype, _qclass, offset = q
                    if name == self.fqdn and qtype in (_QTYPE_A, _QTYPE_ANY):
                        matched = True
                        break

                if matched:
                    self._send_announce()
            except Exception as e:
                # Never let one bad packet kill the responder task.
                log.warn(f"[MDNS] Loop error (continuing): {e}")
            await asyncio.sleep_ms(20)


mdns_responder = MDNSResponder()
