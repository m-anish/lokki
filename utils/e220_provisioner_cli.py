#!/usr/bin/env python3
"""E220-900T22D provisioner — CLI flavour (no tkinter required).

Same job as utils/e220_provisioner.py: drive the Pico-side bridge
(tests/pico_e220_bridge.py) over USB-CDC to read or write the module's
configuration registers.

Examples:
    # 1. Find the Pico's USB serial port:
    python3 utils/e220_provisioner_cli.py --list

    # 2. Read the current config:
    python3 utils/e220_provisioner_cli.py --port /dev/tty.usbmodem1101 read

    # 3. Set ADDL=1, channel 18, transparent, per-packet RSSI byte ON,
    #    and persist to NVRAM:
    python3 utils/e220_provisioner_cli.py --port /dev/tty.usbmodem1101 \\
        write --addl 1 --channel 18 --rssi-byte --persist

    # 4. After a persist, power-cycle the Pico for clean re-init.

Dependencies:
    pip install pyserial
"""

import argparse
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("This tool needs pyserial. Install with: pip install pyserial", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------
# E220 register encoding (mirrors the GUI version)
# ----------------------------------------------------------------------
_BAUD   = {"1200": 0b000, "2400": 0b001, "4800": 0b010, "9600": 0b011,
           "19200": 0b100, "38400": 0b101, "57600": 0b110, "115200": 0b111}
_PARITY = {"8N1": 0b00, "8O1": 0b01, "8E1": 0b10}
_AIR    = {"2.4k": 0b010, "4.8k": 0b011, "9.6k": 0b100,
           "19.2k": 0b101, "38.4k": 0b110, "62.5k": 0b111}
_SUBPKT = {"200": 0b00, "128": 0b01, "64": 0b10, "32": 0b11}
_POWER  = {"22": 0b00, "17": 0b01, "13": 0b10, "10": 0b11}
_TX_METHOD = {"transparent": 0, "fixed-point": 1}


def encode_regs(addh, addl, baud, parity, air, subpkt, ambient_rssi, power,
                channel, tx_method, rssi_byte, lbt):
    reg0 = (_BAUD[baud] << 5) | (_PARITY[parity] << 3) | _AIR[air]
    reg1 = (_SUBPKT[subpkt] << 6) | ((1 if ambient_rssi else 0) << 5) | _POWER[power]
    reg3 = ((1 if rssi_byte else 0) << 7) | (_TX_METHOD[tx_method] << 6) \
           | ((1 if lbt else 0) << 4)
    return bytes([addh, addl, reg0, reg1, channel, reg3, 0x00, 0x00])


def decode_regs(b):
    if len(b) != 8:
        raise ValueError("Need 8 register bytes, got " + str(len(b)))
    addh, addl, reg0, reg1, chan, reg3, crypt_h, crypt_l = b
    baud_bits = (reg0 >> 5) & 0b111
    par_bits  = (reg0 >> 3) & 0b11
    air_bits  = reg0 & 0b111
    sub_bits  = (reg1 >> 6) & 0b11
    rssi_amb  = bool((reg1 >> 5) & 1)
    pwr_bits  = reg1 & 0b11
    rssi_byte = bool((reg3 >> 7) & 1)
    fixed     = bool((reg3 >> 6) & 1)
    lbt       = bool((reg3 >> 4) & 1)
    def find(d, k):
        return next((kk for kk, vv in d.items() if vv == k), "?")
    return {
        "addh": addh, "addl": addl,
        "reg0": reg0, "reg1": reg1, "channel": chan, "reg3": reg3,
        "crypt_h": crypt_h, "crypt_l": crypt_l,
        "baud":   find(_BAUD,   baud_bits),
        "parity": find(_PARITY, par_bits),
        "air":    find(_AIR,    air_bits),
        "subpkt": find(_SUBPKT, sub_bits),
        "ambient_rssi": rssi_amb,
        "power":  find(_POWER, pwr_bits),
        "tx_method": "fixed-point" if fixed else "transparent",
        "rssi_byte": rssi_byte,
        "lbt": lbt,
    }


# ----------------------------------------------------------------------
# Pico bridge RPC
# ----------------------------------------------------------------------

class BridgeRPC:
    def __init__(self, port):
        self.ser = serial.Serial(port, baudrate=115200, timeout=2.0)
        self._wait_for_banner()

    def _wait_for_banner(self):
        deadline = time.time() + 4.0
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="ignore").strip()
            if "BRIDGE READY" in line:
                return
        if self._call("PING") != "OK":
            raise RuntimeError(
                "No 'BRIDGE READY' banner and PING failed. "
                "Is the Pico running tests/pico_e220_bridge.py? Power-cycle and retry."
            )

    def _call(self, line, timeout=3.0):
        self.ser.reset_input_buffer()
        self.ser.write((line + "\n").encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            reply = self.ser.readline().decode(errors="ignore").strip()
            if reply:
                return reply
        return ""

    def read(self):
        r = self._call("READ")
        if not r.startswith("OK "):
            raise RuntimeError("Bridge: " + r)
        return bytes.fromhex(r[3:])

    def write(self, payload8, persist=False):
        cmd = ("WRITE_NV " if persist else "WRITE_VOL ") + payload8.hex()
        r = self._call(cmd, timeout=4.0)
        if not r.startswith("OK "):
            raise RuntimeError("Bridge: " + r)
        return bytes.fromhex(r[3:])

    def close(self):
        try: self._call("QUIT", timeout=0.5)
        except Exception: pass
        self.ser.close()


# ----------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------

def print_decoded(d):
    print(f"  ADDH=0x{d['addh']:02x}  ADDL=0x{d['addl']:02x}")
    print(f"  Channel={d['channel']}  (~{850.125 + d['channel']:.3f} MHz)")
    print(f"  REG0=0x{d['reg0']:02x}  baud={d['baud']}  parity={d['parity']}  air={d['air']}")
    print(f"  REG1=0x{d['reg1']:02x}  subpkt={d['subpkt']}B  ambient_rssi={'on' if d['ambient_rssi'] else 'off'}  power={d['power']} dBm")
    print(f"  REG3=0x{d['reg3']:02x}  tx={d['tx_method']}  rssi_byte={'on' if d['rssi_byte'] else 'off'}  lbt={'on' if d['lbt'] else 'off'}")
    print(f"  CRYPT_H=0x{d['crypt_h']:02x}  CRYPT_L=0x{d['crypt_l']:02x}  (write-only — read returns 0)")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def parse_byte(s):
    s = s.strip().lower()
    return int(s, 16) if s.startswith("0x") else int(s, 0)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="Pico's USB serial port (e.g. /dev/tty.usbmodem1101)")
    p.add_argument("--list", action="store_true",
                   help="List available serial ports and exit")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("read", help="Read and pretty-print the module's current config")

    w = sub.add_parser("write", help="Write a new config (volatile by default)")
    w.add_argument("--addh",      type=parse_byte, default=0x00, help="ADDH (0x00..0xFF)")
    w.add_argument("--addl",      type=parse_byte, default=0x00, help="ADDL (0x00..0xFF)")
    w.add_argument("--channel",   type=int,        default=18,    help="0..80 (freq=850.125+CH)")
    w.add_argument("--baud",      choices=list(_BAUD),    default="9600")
    w.add_argument("--parity",    choices=list(_PARITY),  default="8N1")
    w.add_argument("--air",       choices=list(_AIR),     default="2.4k")
    w.add_argument("--subpkt",    choices=list(_SUBPKT),  default="200",
                   help="Sub-packet size in bytes")
    w.add_argument("--power",     choices=list(_POWER),   default="22",
                   help="TX power in dBm")
    w.add_argument("--ambient-rssi", action="store_true",
                   help="Enable ambient RSSI noise reading (REG1 bit 5)")
    w.add_argument("--tx-method", choices=list(_TX_METHOD), default="transparent")
    w.add_argument("--rssi-byte", action="store_true",
                   help="Append per-packet RSSI byte on receive (REG3 bit 7)")
    w.add_argument("--lbt", action="store_true",
                   help="Enable Listen Before Talk")
    w.add_argument("--persist", action="store_true",
                   help="Use 0xC0 (NVRAM persist) — survives power cycles. "
                        "Default is 0xC2 (volatile, RAM only).")

    args = p.parse_args()

    if args.list:
        for port in list_ports.comports():
            desc = port.description or ""
            print(f"  {port.device:40s}  {desc}")
        return

    if not args.port:
        p.error("--port is required (or use --list to see candidates)")
    if not args.cmd:
        p.error("subcommand 'read' or 'write' is required")

    bridge = BridgeRPC(args.port)
    try:
        if args.cmd == "read":
            regs = bridge.read()
            print(f"Raw register bytes: {regs.hex()}")
            print_decoded(decode_regs(regs))

        elif args.cmd == "write":
            if not 0 <= args.addh <= 0xFF or not 0 <= args.addl <= 0xFF:
                sys.exit("ADDH/ADDL must be 0..0xFF")
            if not 0 <= args.channel <= 80:
                sys.exit("Channel must be 0..80")
            payload = encode_regs(
                args.addh, args.addl,
                args.baud, args.parity, args.air,
                args.subpkt, args.ambient_rssi, args.power,
                args.channel, args.tx_method, args.rssi_byte, args.lbt,
            )
            kind = "NVRAM (0xC0, persist)" if args.persist else "RAM (0xC2, volatile)"
            print(f"Writing {kind}: payload={payload.hex()}")
            after = bridge.write(payload, persist=args.persist)
            print(f"Echoed:                  {after.hex()}")
            print()
            print("After-write decoded:")
            print_decoded(decode_regs(after))
            if args.persist:
                print()
                print("→ POWER-CYCLE the Pico (full power off) before deploying.")
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
