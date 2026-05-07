# ----------------------------------------------------------------------
# E220 provisioning bridge.
#
# Runs on the Pico. Holds the LoRa module in CONFIG mode (M0=M1=HIGH),
# opens UART0 to the module at 9600 8N1 (the only baud the module
# accepts in CONFIG mode), then offers a tiny line-based RPC over USB
# CDC for the host-side provisioning GUI.
#
# Commands accepted on stdin (one per line, ASCII):
#
#   PING                         -> "OK"
#   READ                         -> "OK <16 hex chars>"  (8 register bytes)
#   WRITE_VOL <16 hex chars>     -> "OK <16 hex chars>"  (echoed register state)
#   WRITE_NV  <16 hex chars>     -> "OK <16 hex chars>"  (writes to NVRAM)
#
# Replies on stdout. Errors come back as "ERR <message>".
#
# The 8 register bytes are: ADDH, ADDL, REG0, REG1, CHAN, REG3, CRYPT_H, CRYPT_L.
# ----------------------------------------------------------------------

import sys
import time
from machine import UART, Pin


# Wiring — same as the rest of the test rig
UART_ID = 0
TX_PIN  = 0
RX_PIN  = 1
M0_PIN  = 2
M1_PIN  = 3
AUX_PIN = 4


# ----------------------------------------------------------------------
# Bring the module into CONFIG mode
# ----------------------------------------------------------------------
m0  = Pin(M0_PIN, Pin.OUT, value=1)
m1  = Pin(M1_PIN, Pin.OUT, value=1)
aux = Pin(AUX_PIN, Pin.IN)

# CONFIG mode is always 9600 8N1 regardless of REG0
uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))

# Let the module finish entering CONFIG mode + drain any boot-time bytes
time.sleep_ms(400)
for _ in range(3):
    uart.read()
    time.sleep_ms(30)


# ----------------------------------------------------------------------
# Module I/O primitives
# ----------------------------------------------------------------------

def _drain():
    while uart.read():
        time.sleep_ms(10)


def _send_command(frame, expected_len, timeout_ms=1500):
    """Write `frame` to the module, then poll the UART for up to
    `expected_len` bytes back, with a hard timeout. Returns the bytes
    received (may be shorter than expected_len on timeout)."""
    _drain()
    uart.write(frame)
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    buf = b""
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        chunk = uart.read()
        if chunk:
            buf += chunk
            if len(buf) >= expected_len:
                break
        time.sleep_ms(20)
    return buf


def cmd_read():
    reply = _send_command(b"\xC1\x00\x08", expected_len=11)
    if not reply:
        return "ERR no_reply"
    if reply[:3] == b"\xff\xff\xff":
        return "ERR wrong_format"
    if len(reply) < 11 or reply[0] not in (0xC0, 0xC1, 0xC2):
        return "ERR bad_reply " + reply.hex()
    return "OK " + reply[3:11].hex()


def cmd_write(persist, payload_hex):
    try:
        payload = bytes.fromhex(payload_hex)
    except Exception:
        return "ERR bad_hex"
    if len(payload) != 8:
        return "ERR need_8_bytes_got_" + str(len(payload))
    cmd_byte = 0xC0 if persist else 0xC2
    frame = bytes([cmd_byte, 0x00, 0x08]) + payload
    reply = _send_command(frame, expected_len=11)
    if not reply:
        return "ERR no_reply"
    if reply[:3] == b"\xff\xff\xff":
        return "ERR wrong_format"
    if len(reply) < 11 or reply[0] not in (0xC0, 0xC1, 0xC2):
        return "ERR bad_reply " + reply.hex()
    return "OK " + reply[3:11].hex()


# ----------------------------------------------------------------------
# Banner + RPC loop
# ----------------------------------------------------------------------

# The host-side tool waits for this exact line to know the bridge is up.
print("BRIDGE READY  AUX={}".format(aux.value()))

while True:
    try:
        line = sys.stdin.readline()
    except Exception as e:
        print("ERR read_failed " + str(e))
        continue
    if not line:
        continue
    line = line.strip()
    if not line:
        continue

    if line == "PING":
        print("OK")
    elif line == "READ":
        print(cmd_read())
    elif line.startswith("WRITE_VOL "):
        print(cmd_write(False, line[len("WRITE_VOL "):]))
    elif line.startswith("WRITE_NV "):
        print(cmd_write(True, line[len("WRITE_NV "):]))
    elif line == "QUIT":
        print("OK bye")
        break
    else:
        print("ERR unknown_cmd " + line)
