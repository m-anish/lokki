#!/usr/bin/env python3
"""One-time E220-900T22D provisioning GUI (host-side).

Talks through the Pico — not directly to the LoRa module via a USB-to-TTL
adapter. The Pico runs `tests/pico_e220_bridge.py` as its main.py, which
holds the E220 in CONFIG mode (M0=M1=HIGH) and offers a tiny line-based
RPC over the USB CDC port:

    PING                       -> OK
    READ                       -> OK <16 hex chars>
    WRITE_VOL <16 hex chars>   -> OK <16 hex chars>
    WRITE_NV  <16 hex chars>   -> OK <16 hex chars>

Workflow:
  1. Flash a Pico with the bridge:    utils/flash_test.sh --script=bridge
  2. Plug it in (the soldered LoRa module is already on the same board).
  3. Run this GUI, pick the Pico's USB CDC port (e.g. /dev/tty.usbmodem*).
  4. Read current config, edit fields, write (volatile or NVRAM).
  5. After NVRAM write, power-cycle the Pico to apply.

Dependencies: pip install pyserial
"""

import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("This tool needs pyserial. Install with: pip install pyserial", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------
# E220 register encoding / decoding
# ----------------------------------------------------------------------

_BAUD_BITS   = {"1200": 0b000, "2400": 0b001, "4800": 0b010, "9600": 0b011,
                "19200": 0b100, "38400": 0b101, "57600": 0b110, "115200": 0b111}
_PARITY_BITS = {"8N1": 0b00, "8O1": 0b01, "8E1": 0b10}
_AIR_BITS    = {"2.4k": 0b010, "4.8k": 0b011, "9.6k": 0b100,
                "19.2k": 0b101, "38.4k": 0b110, "62.5k": 0b111}
_SUBPKT_BITS = {"200B": 0b00, "128B": 0b01, "64B": 0b10, "32B": 0b11}
_POWER_BITS  = {"22 dBm": 0b00, "17 dBm": 0b01, "13 dBm": 0b10, "10 dBm": 0b11}
_TX_METHOD   = {"Transparent": 0, "Fixed-point": 1}


def build_reg0(baud, parity, air):
    return (_BAUD_BITS[baud] << 5) | (_PARITY_BITS[parity] << 3) | _AIR_BITS[air]

def build_reg1(subpkt, rssi_ambient, power):
    return (_SUBPKT_BITS[subpkt] << 6) | ((1 if rssi_ambient else 0) << 5) | _POWER_BITS[power]

def build_reg3(rssi_byte, tx_method, lbt, wor_idx=0):
    b = (1 if rssi_byte else 0) << 7
    b |= _TX_METHOD[tx_method] << 6
    b |= (1 if lbt else 0) << 4
    b |= (wor_idx & 0x07)
    return b

def parse_reg0(b):
    bb = (b >> 5) & 0b111; pp = (b >> 3) & 0b11; aa = b & 0b111
    baud   = next((k for k, v in _BAUD_BITS.items()   if v == bb), "?")
    parity = next((k for k, v in _PARITY_BITS.items() if v == pp), "?")
    air    = next((k for k, v in _AIR_BITS.items()    if v == aa), "?")
    return baud, parity, air

def parse_reg1(b):
    sb = (b >> 6) & 0b11; rb = bool((b >> 5) & 1); pb = b & 0b11
    subpkt = next((k for k, v in _SUBPKT_BITS.items() if v == sb), "?")
    power  = next((k for k, v in _POWER_BITS.items()  if v == pb), "?")
    return subpkt, rb, power

def parse_reg3(b):
    return bool((b >> 7) & 1), bool((b >> 6) & 1), bool((b >> 4) & 1), b & 0b111


# ----------------------------------------------------------------------
# Pico bridge RPC
# ----------------------------------------------------------------------

class BridgeRPC:
    def __init__(self, port):
        # USB CDC ignores baud rate; pick anything reasonable.
        self.ser = serial.Serial(port, baudrate=115200, timeout=2.0)
        self._wait_for_banner()

    def _wait_for_banner(self):
        # On first connect, the Pico may still be booting. Look for the
        # "BRIDGE READY" line within a few seconds.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="ignore").strip()
            if "BRIDGE READY" in line:
                return
        # No banner — bridge maybe already past it. Try a PING.
        if self._call("PING") != "OK":
            raise RuntimeError(
                "No 'BRIDGE READY' banner and PING failed. Is the Pico "
                "running tests/pico_e220_bridge.py? Power-cycle and retry."
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
            raise RuntimeError("Bridge reply: " + r)
        return bytes.fromhex(r[3:])

    def write(self, payload_8, persist=False):
        cmd = "WRITE_NV " if persist else "WRITE_VOL "
        r = self._call(cmd + payload_8.hex(), timeout=4.0)
        if not r.startswith("OK "):
            raise RuntimeError("Bridge reply: " + r)
        return bytes.fromhex(r[3:])

    def close(self):
        try: self._call("QUIT", timeout=0.5)
        except Exception: pass
        try: self.ser.close()
        except Exception: pass


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

def list_serial_ports():
    return [p.device for p in list_ports.comports()]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lokki — E220 Provisioner (via Pico bridge)")
        self.geometry("680x720")
        self.bridge = None
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- Connection ---
        conn = ttk.LabelFrame(self, text="Connection (Pico USB CDC, running pico_e220_bridge.py)")
        conn.pack(fill="x", padx=10, pady=8)
        ttk.Label(conn, text="Serial port:").grid(row=0, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var,
                                        values=list_serial_ports(), width=44)
        self.port_combo.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(conn, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self._toggle_connection)
        self.connect_btn.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(conn, text="Flash bridge first:  utils/flash_test.sh --script=bridge",
                  foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        conn.columnconfigure(1, weight=1)

        # --- Module identity ---
        ident = ttk.LabelFrame(self, text="Module Identity (registers 0x00 / 0x01)")
        ident.pack(fill="x", padx=10, pady=8)
        ttk.Label(ident, text="ADDH:").grid(row=0, column=0, sticky="w", **pad)
        self.addh_var = tk.StringVar(value="0x00")
        ttk.Entry(ident, textvariable=self.addh_var, width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(ident, text="ADDL:").grid(row=0, column=2, sticky="w", **pad)
        self.addl_var = tk.StringVar(value="0x00")
        ttk.Entry(ident, textvariable=self.addl_var, width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(ident, text="(0xFFFF = broadcast/monitor; no address filtering)",
                  foreground="#666").grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        # --- REG0 ---
        sped = ttk.LabelFrame(self, text="REG0 — UART / Parity / Air rate (register 0x02)")
        sped.pack(fill="x", padx=10, pady=8)
        self.baud_var   = tk.StringVar(value="9600")
        self.parity_var = tk.StringVar(value="8N1")
        self.air_var    = tk.StringVar(value="2.4k")
        ttk.Label(sped, text="UART baud:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(sped, textvariable=self.baud_var, values=list(_BAUD_BITS),
                     state="readonly", width=10).grid(row=0, column=1, **pad)
        ttk.Label(sped, text="Parity:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Combobox(sped, textvariable=self.parity_var, values=list(_PARITY_BITS),
                     state="readonly", width=10).grid(row=0, column=3, **pad)
        ttk.Label(sped, text="Air rate:").grid(row=0, column=4, sticky="w", **pad)
        ttk.Combobox(sped, textvariable=self.air_var, values=list(_AIR_BITS),
                     state="readonly", width=10).grid(row=0, column=5, **pad)

        # --- REG1 ---
        opt = ttk.LabelFrame(self, text="REG1 — Sub-packet / RSSI ambient / TX power (register 0x03)")
        opt.pack(fill="x", padx=10, pady=8)
        self.subpkt_var       = tk.StringVar(value="200B")
        self.rssi_ambient_var = tk.BooleanVar(value=False)
        self.power_var        = tk.StringVar(value="22 dBm")
        ttk.Label(opt, text="Sub-packet:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(opt, textvariable=self.subpkt_var, values=list(_SUBPKT_BITS),
                     state="readonly", width=10).grid(row=0, column=1, **pad)
        ttk.Label(opt, text="TX power:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Combobox(opt, textvariable=self.power_var, values=list(_POWER_BITS),
                     state="readonly", width=10).grid(row=0, column=3, **pad)
        ttk.Checkbutton(opt, text="RSSI ambient noise enable",
                        variable=self.rssi_ambient_var).grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        # --- REG2 ---
        ch = ttk.LabelFrame(self, text="REG2 — Channel (register 0x04)")
        ch.pack(fill="x", padx=10, pady=8)
        self.chan_var = tk.IntVar(value=18)
        ttk.Label(ch, text="Channel (0..80):").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(ch, from_=0, to=80, textvariable=self.chan_var, width=8).grid(row=0, column=1, **pad)
        self.freq_label = ttk.Label(ch, text="Frequency: 868.125 MHz")
        self.freq_label.grid(row=0, column=2, sticky="w", **pad)
        self.chan_var.trace_add("write", lambda *_: self._update_freq_label())

        # --- REG3 ---
        tm = ttk.LabelFrame(self, text="REG3 — Transmission flags (register 0x05)")
        tm.pack(fill="x", padx=10, pady=8)
        self.tx_method_var = tk.StringVar(value="Transparent")
        self.rssi_byte_var = tk.BooleanVar(value=False)
        self.lbt_var       = tk.BooleanVar(value=False)
        ttk.Label(tm, text="Transmission method:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(tm, textvariable=self.tx_method_var, values=list(_TX_METHOD),
                     state="readonly", width=14).grid(row=0, column=1, **pad)
        ttk.Checkbutton(tm, text="Append per-packet RSSI byte on receive",
                        variable=self.rssi_byte_var).grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(tm, text="Listen Before Talk (LBT)",
                        variable=self.lbt_var).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        # --- Actions ---
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=10, pady=8)
        ttk.Button(actions, text="Read current config",
                   command=self._read_config).pack(side="left", **pad)
        ttk.Button(actions, text="Write (volatile, 0xC2)",
                   command=lambda: self._write_config(persist=False)).pack(side="left", **pad)
        ttk.Button(actions, text="Write & PERSIST (0xC0, NVRAM)",
                   command=lambda: self._write_config(persist=True)).pack(side="left", **pad)

        # --- Log ---
        log = ttk.LabelFrame(self, text="Log")
        log.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = tk.Text(log, height=10, font=("Menlo", 11))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        self._log("Flash the bridge:  utils/flash_test.sh --script=bridge")
        self._log("Then pick the Pico's USB serial port and click Connect.")

    # ------------------------------------------------------------------
    def _log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _refresh_ports(self):
        self.port_combo["values"] = list_serial_ports()

    def _update_freq_label(self):
        try:
            self.freq_label.config(text=f"Frequency: {850.125 + int(self.chan_var.get()):.3f} MHz")
        except Exception:
            pass

    def _toggle_connection(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None
            self.connect_btn.config(text="Connect")
            self._log("Disconnected.")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("No port", "Pick the Pico's USB serial port first.")
            return
        try:
            self.bridge = BridgeRPC(port)
            self.connect_btn.config(text="Disconnect")
            self._log(f"Connected to bridge on {port}.")
        except Exception as e:
            messagebox.showerror("Connect failed", str(e))
            self.bridge = None

    def _need_bridge(self):
        if not self.bridge:
            messagebox.showerror("Not connected", "Connect to the Pico bridge first.")
            return False
        return True

    def _read_config(self):
        if not self._need_bridge(): return
        try:
            regs = self.bridge.read()
        except Exception as e:
            self._log(f"READ FAILED: {e}")
            return
        addh, addl, reg0, reg1, chan, reg3, crypt_h, crypt_l = regs
        baud, par, air = parse_reg0(reg0)
        subpkt, rssi_amb, power = parse_reg1(reg1)
        rssi_byte, fixed, lbt, _ = parse_reg3(reg3)
        self._log(f"READ OK: ADDH={addh:#04x} ADDL={addl:#04x} CHAN={chan} (~{850.125+chan:.3f} MHz)")
        self._log(f"        REG0=0x{reg0:02x} -> baud={baud}, parity={par}, air={air}")
        self._log(f"        REG1=0x{reg1:02x} -> subpkt={subpkt}, ambient_rssi={'on' if rssi_amb else 'off'}, power={power}")
        self._log(f"        REG3=0x{reg3:02x} -> {'fixed-point' if fixed else 'transparent'}, "
                  f"rssi_byte={'on' if rssi_byte else 'off'}, lbt={'on' if lbt else 'off'}")
        self._log(f"        CRYPT_H/CRYPT_L: 0x{crypt_h:02x} 0x{crypt_l:02x}  (write-only — read returns 0)")
        # Reflect into form
        self.addh_var.set(f"0x{addh:02x}")
        self.addl_var.set(f"0x{addl:02x}")
        self.baud_var.set(baud); self.parity_var.set(par); self.air_var.set(air)
        self.subpkt_var.set(subpkt); self.rssi_ambient_var.set(rssi_amb); self.power_var.set(power)
        self.chan_var.set(chan)
        self.tx_method_var.set("Fixed-point" if fixed else "Transparent")
        self.rssi_byte_var.set(rssi_byte); self.lbt_var.set(lbt)

    def _parse_byte(self, s):
        s = s.strip().lower()
        return int(s, 16) if s.startswith("0x") else int(s, 0)

    def _write_config(self, persist):
        if not self._need_bridge(): return
        try:
            addh = self._parse_byte(self.addh_var.get())
            addl = self._parse_byte(self.addl_var.get())
            chan = int(self.chan_var.get())
            if not 0 <= addh <= 0xFF or not 0 <= addl <= 0xFF:
                raise ValueError("ADDH/ADDL must be 0..0xFF")
            if not 0 <= chan <= 80:
                raise ValueError("Channel must be 0..80")
            reg0 = build_reg0(self.baud_var.get(), self.parity_var.get(), self.air_var.get())
            reg1 = build_reg1(self.subpkt_var.get(), self.rssi_ambient_var.get(), self.power_var.get())
            reg3 = build_reg3(self.rssi_byte_var.get(), self.tx_method_var.get(), self.lbt_var.get())
        except Exception as e:
            messagebox.showerror("Bad input", str(e))
            return

        payload = bytes([addh, addl, reg0, reg1, chan, reg3, 0x00, 0x00])
        kind = "PERSIST (NVRAM, 0xC0)" if persist else "VOLATILE (RAM, 0xC2)"
        if persist and not messagebox.askyesno(
                "Persist to flash?",
                "Writing to NVRAM (0xC0) wears flash and persists across power cycles.\n\n"
                "After this write you must POWER-CYCLE the Pico for the new config to be cleanly applied "
                "to the radio's running state. Proceed?"):
            return
        try:
            after = self.bridge.write(payload, persist=persist)
        except Exception as e:
            self._log(f"WRITE FAILED ({kind}): {e}")
            return
        self._log(f"WRITE OK ({kind}): payload={payload.hex()}  echoed={after.hex()}")
        if persist:
            self._log("→ Now POWER-CYCLE the Pico (full power off) before installing for runtime.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
