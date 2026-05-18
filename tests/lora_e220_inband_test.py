# ----------------------------------------------------------------------
# E220 in-band runtime-configuration test harness
#
# Mirrors the xreef library's C++ flow byte-for-byte (LoRa_E220::setMode,
# setConfiguration, getConfiguration, writeProgramCommand, waitCompleteResponse)
# but in MicroPython, with the explicit goal of finding out whether
# runtime register writes really do wedge the E220's RX path on our
# hardware, or whether that earlier diagnosis was a fluke of how the old
# transport sequenced UART and AUX.
#
# How to use:
#   1.  Edit OP below to pick READ / WRITE / TX / RX / WEDGE_STRESS.
#   2.  Edit the WR_* constants if doing WRITE — these are the registers
#       that will be programmed into the module (RAM or NVRAM, per PERSIST).
#   3.  Flash this single file to a Pico that has an E220 wired up.
#       No project-module imports — runs on a freshly-flashed Pico.
#   4.  Open the REPL and watch.
#
# Pass criteria for "in-band is safe enough to integrate":
#   * READ runs ≥ 100 cycles with no AUX timeouts.
#   * WRITE then READ echoes back identical 8 register bytes.
#   * TX → RX between two Picos delivers ≥ 99 % of MSG_COUNT frames.
#   * WEDGE_STRESS completes all cycles without RX going silent for more
#     than two consecutive frames.
#
# RSSI bytes (REG3 bit 7 = 1) are NOT stripped — they are decoded and
# printed alongside the payload so we can confirm both that the byte is
# being appended by the module AND that it matches measured link quality.
# ----------------------------------------------------------------------

import time
from machine import Pin, UART
try:
    import neopixel
except Exception:
    neopixel = None


# ============================================================
# What to do this run
# ============================================================
OP = "READ"        # READ | WRITE | TX | RX | WEDGE_STRESS | RWR | RR | RWT_LOOP


# ============================================================
# Hardware pins (match the leaf's lora_* in config.json)
# ============================================================
UART_ID  = 0
TX_PIN   = 0
RX_PIN   = 1
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4

# WS2812 status LED — matches the leaf's status_led_pin / led_color_order.
# Flashed red on TX, green on RX. Set LED_PIN to None to disable.
LED_PIN   = 5
LED_ORDER = "RGB"   # the abstract color order the user sees; hw is GRB
LED_FLASH_MS = 60

# UART baud while in NORMAL mode. The module forces 9600 in CONFIG
# regardless of REG0 baud bits, so the script always switches the UART
# baud around mode changes.
DATA_BAUD = 9600


# ============================================================
# WRITE op — register payload to program
# ============================================================
# 16-bit module address. 0xFFFF is the broadcast / accept-everything
# address — set both Picos here to avoid futzing with per-Pico
# addressing while we're still validating that runtime config works at
# all. Per the datasheet transparent mode shouldn't filter by ADDR, but
# empirically these modules do, so matching addresses is the simplest
# fix.
WR_ADDH = 0xFF
WR_ADDL = 0xFF

# REG0 — UART, parity, air rate
WR_BAUD_BITS   = 0b011    # 0b011 = 9600. See datasheet table 6.1.
WR_PARITY_BITS = 0b00     # 8N1
WR_AIR_BITS    = 0b010    # 0b010 = 2.4 kbps

# REG1 — sub-packet, ambient RSSI, TX power
WR_SUBPKT_BITS    = 0b00  # 200 B
WR_RSSI_AMBIENT   = False # ambient noise reporting (separate from per-frame RSSI byte)
WR_TX_POWER_BITS  = 0b00  # 22 dBm

# REG4 — channel (the actual register at addr 0x04; we call this CHAN
# because the datasheet does). 73 is the project-wide fleet default.
WR_CHANNEL = 73

# REG5 — RSSI byte, fixed/transparent mode, LBT, WOR cycle.
# Defaults below match the production firmware so this script doubles
# as a "factory reset to known-good defaults" tool — write with
# PERSIST=True and the module will boot cleanly even if the runtime
# lora_config.apply_from_config path is broken or absent.
WR_RSSI_BYTE  = True      # production forces this on; recv() strips trailing byte
WR_FIXED_MODE = True      # production uses FIXED for hardware-directed addressing
WR_LBT        = False     # default OFF — LBT silently drops frames if channel
                          # never becomes quiet within timeout, hurting reliability
WR_WOR_BITS   = 0b011     # 2000 ms WOR period (only used if WOR mode entered)

# Encryption key — fleet-wide default. MUST match across every unit.
WR_CRYPT_H = 0x07
WR_CRYPT_L = 0x93

# 0xC0 → save to NVRAM (survives power cycles, costs flash writes).
# 0xC2 → RAM only (volatile, safe to spam during testing).
PERSIST = False


# ============================================================
# TX op
# ============================================================
# Used only when the current register state has FIXED mode on. In FIXED
# mode every UART frame is prefixed by [DESTH, DESTL, DESTCHAN]; the
# module strips that header before TX, and on the RX side those bytes
# never appear. Set 0xFFFF for broadcast.
TX_DEST_H    = 0x00
TX_DEST_L    = 0x02       # send to the OTHER Pico
TX_DEST_CHAN = 18

TX_PAYLOAD       = "ping from %d"      # %d filled with sequence number
TX_COUNT         = 200
TX_INTERVAL_MS   = 1000


# ============================================================
# RX op
# ============================================================
RX_DURATION_S = 0          # 0 = listen forever (Ctrl-C to stop)
# Whether to treat the trailing byte of each received frame as RSSI. Set
# True if the module was last programmed with WR_RSSI_BYTE = True. If
# False the trailing byte is treated as payload data.
RX_RSSI_BYTE_ENABLED = True


# ============================================================
# WEDGE_STRESS op
# ============================================================
# Interleave config writes with TX and RX so we can prove (or disprove)
# the original failure mode: "register write at runtime silently wedges
# RX path until power cycle."
WS_CYCLES         = 50
WS_TX_PER_CYCLE   = 10
WS_RX_LISTEN_S    = 5
WS_INTER_DELAY_MS = 100

# ============================================================
# RWT_LOOP op (Read - Write - Transmit, in a loop)
# ============================================================
# End-to-end validation: do RF photons actually reach a peer Pico after
# each register WRITE? Pair this Pico with another running OP="RX".
# If frames keep arriving on the RX side, in-band config is safe.
# If frames stop after cycle 1, the WRITE is wedging the RF path even
# though manual READs still work afterwards.
RWT_CYCLES        = 50
RWT_DELAY_MS      = 1000  # between cycles
RWT_INTRA_DELAY_MS = 100  # between R/W/T within a cycle


# ============================================================
# Module-level constants — DO NOT edit unless following the datasheet
# ============================================================
CMD_WRITE_NVRAM = 0xC0
CMD_READ_REGS   = 0xC1
CMD_WRITE_RAM   = 0xC2
REG_ADDR_CFG    = 0x00
PL_CONFIG       = 0x08
RETURNED_CMD    = 0xC1   # echo from module on any successful read or write
WRONG_FORMAT    = 0xFF

CONFIG_BAUD = 9600   # forced by the module while in PROGRAM mode


# ============================================================
# Pin / UART setup
# ============================================================
m0  = Pin(M0_PIN,  Pin.OUT, value=0)
m1  = Pin(M1_PIN,  Pin.OUT, value=0)
aux = Pin(AUX_PIN, Pin.IN)
uart = UART(UART_ID, baudrate=DATA_BAUD, tx=Pin(TX_PIN), rx=Pin(RX_PIN))

# ============================================================
# LED helper (red on TX, green on RX)
# ============================================================
_np = None
if neopixel is not None and LED_PIN is not None:
    try:
        _np = neopixel.NeoPixel(Pin(LED_PIN), 1)
    except Exception:
        _np = None


def _led_set(r, g, b):
    if _np is None:
        return
    # WS2812 hardware order is GRB. If the user thinks in "RGB", we
    # swap; if they've set LED_ORDER="GRB" we trust them.
    if LED_ORDER == "RGB":
        _np[0] = (g, r, b)
    else:
        _np[0] = (r, g, b)
    _np.write()


def _led_blink(r, g, b, ms=None):
    if _np is None:
        return
    _led_set(r, g, b)
    time.sleep_ms(ms if ms is not None else LED_FLASH_MS)
    _led_set(0, 0, 0)


# ============================================================
# Low-level helpers — these mirror xreef's set_mode / waitCompleteResponse
# ============================================================

def _managed_delay(ms):
    """xreef's busy-wait equivalent. We use time.sleep_ms which is fine
    in a test context; in async firmware we'd use asyncio.sleep_ms."""
    time.sleep_ms(ms)


def wait_aux_high(timeout_ms=1000):
    """Block until AUX pin goes HIGH or timeout. Returns True on AUX
    HIGH, False on timeout. xreef calls this waitCompleteResponse and
    uses it as the universal 'module is done' semaphore."""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while aux.value() == 0:
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
    _managed_delay(20)  # xreef's 20 ms tail
    return True


def set_mode(mode):
    """Drive M0/M1 to put the module in NORMAL/PROGRAM/WOR-TX/WOR-RX.
    Mirrors xreef::setMode exactly: 40 ms pre, set pins, 40 ms post,
    wait AUX HIGH. Returns True if AUX confirmed within 1 s."""
    _managed_delay(40)
    if mode == "NORMAL":
        m0.value(0); m1.value(0)
    elif mode == "WOR_TX":
        m0.value(1); m1.value(0)
    elif mode == "WOR_RX":
        m0.value(0); m1.value(1)
    elif mode == "PROGRAM":
        m0.value(1); m1.value(1)
    else:
        raise ValueError("bad mode: " + mode)
    _managed_delay(40)
    return wait_aux_high(1000)


def _drain_uart():
    """xreef's cleanUARTbuffer equivalent. Discards anything sitting in
    the RX buffer so a previous reply doesn't get mistaken for our next
    response."""
    n = uart.any()
    if n:
        uart.read(n)


_current_baud = DATA_BAUD

def _switch_uart_baud(baud):
    """The E220's UART baud is fixed at 9600 in PROGRAM mode but follows
    REG0 in NORMAL. If the two bauds differ we have to re-init the UART
    around mode transitions; if they're the same (e.g. running NORMAL at
    9600 too), the deinit/reinit dance is *worse than useless*: it
    leaves the RP2350's UART peripheral in a transient state that the
    next register-read times out against.
    Empirically: WRITE → TX → deinit/reinit-at-same-baud → READ fails.
    Same flow without the deinit/reinit works fine."""
    global uart, _current_baud
    if baud == _current_baud:
        return                                  # nothing to do — preserve UART state
    uart.deinit()
    uart = UART(UART_ID, baudrate=baud, tx=Pin(TX_PIN), rx=Pin(RX_PIN))
    _current_baud = baud
    _managed_delay(50)                          # let the peripheral settle


# ============================================================
# Register encode/decode — same byte layout as utils/e220_provisioner_cli.py
# ============================================================

def build_write_frame(persist):
    """Build the 11-byte write frame:
      [cmd, REG_ADDR_CFG, PL_CONFIG,
       ADDH, ADDL, REG0, REG1, CHAN, REG3, CRYPT_H, CRYPT_L]
    Mirrors xreef::sendStruct over the Configuration struct."""
    reg0 = (WR_BAUD_BITS << 5) | (WR_PARITY_BITS << 3) | WR_AIR_BITS
    reg1 = (WR_SUBPKT_BITS << 6) | ((1 if WR_RSSI_AMBIENT else 0) << 5) | WR_TX_POWER_BITS
    reg3 = (((1 if WR_RSSI_BYTE else 0) << 7) |
            ((1 if WR_FIXED_MODE else 0) << 6) |
            ((1 if WR_LBT       else 0) << 4) |
            WR_WOR_BITS)
    cmd = CMD_WRITE_NVRAM if persist else CMD_WRITE_RAM
    return bytes([cmd, REG_ADDR_CFG, PL_CONFIG,
                  WR_ADDH, WR_ADDL, reg0, reg1, WR_CHANNEL, reg3, WR_CRYPT_H, WR_CRYPT_L])


def decode_regs(eight_bytes):
    """Mirror utils/e220_provisioner_cli.py for symmetry."""
    if len(eight_bytes) != 8:
        return {"error": "expected 8 register bytes, got %d" % len(eight_bytes)}
    addh, addl, reg0, reg1, chan, reg3, ch, cl = eight_bytes
    return {
        "addr":          "%02X%02X" % (addh, addl),
        "channel":       chan,
        "baud_bits":     (reg0 >> 5) & 0b111,
        "parity_bits":   (reg0 >> 3) & 0b11,
        "air_bits":       reg0       & 0b111,
        "subpkt_bits":   (reg1 >> 6) & 0b11,
        "rssi_ambient":  bool((reg1 >> 5) & 1),
        "tx_pwr_bits":    reg1       & 0b11,
        "rssi_byte":     bool((reg3 >> 7) & 1),
        "fixed_mode":    bool((reg3 >> 6) & 1),
        "lbt":           bool((reg3 >> 4) & 1),
        "crypt":         "%02X%02X" % (ch, cl),
        "raw":           " ".join("%02X" % b for b in eight_bytes),
    }


# ============================================================
# Operations
# ============================================================

def op_read():
    """Single READ cycle: PROGRAM → emit [0xC1, 0x00, 0x08] → expect
    [0xC1, 0x00, 0x08, ...8 reg bytes] → NORMAL → decode + print.
    No data path touched."""
    print("[READ] Entering PROGRAM mode")
    # CRITICAL: wait until the module is idle (AUX HIGH) BEFORE we touch
    # the UART baud or M0/M1. If we tear down the UART while the module
    # is still flushing a previous TX, the in-flight byte gets cut and
    # the module enters a confused state where the very next register
    # read returns nothing. (Confirmed empirically with WEDGE_STRESS.)
    if not wait_aux_high(2000):
        print("[READ] Module still busy (AUX LOW > 2s) — aborting"); return False
    _switch_uart_baud(CONFIG_BAUD)
    if not set_mode("PROGRAM"):
        print("[READ] AUX did not go HIGH on PROGRAM entry — aborting"); return False
    _drain_uart()

    uart.write(bytes([CMD_READ_REGS, REG_ADDR_CFG, PL_CONFIG]))
    _managed_delay(200)   # leave time for module to assemble reply
    reply = uart.read()
    if reply is None or len(reply) < 11:
        print("[READ] Short reply (got %r)" % reply)
        set_mode("NORMAL"); _switch_uart_baud(DATA_BAUD); return False

    if reply[0] not in (CMD_WRITE_NVRAM, CMD_WRITE_RAM, RETURNED_CMD):
        print("[READ] Unexpected reply header: %02X" % reply[0])
    print("[READ] Reply 11 bytes:", " ".join("%02X" % b for b in reply[:11]))
    decoded = decode_regs(reply[3:11])
    for k, v in decoded.items():
        print("  %-13s %s" % (k, v))

    set_mode("NORMAL")
    _switch_uart_baud(DATA_BAUD)
    return True


def op_write():
    """Single WRITE cycle: PROGRAM → emit 11-byte write frame → expect
    11-byte echo with cmd swapped to 0xC1 → NORMAL → verify echo
    matches what we sent."""
    print("[WRITE] Entering PROGRAM mode (persist=%s)" % PERSIST)
    # Same front-edge AUX guard as op_read — see comment there.
    if not wait_aux_high(2000):
        print("[WRITE] Module still busy (AUX LOW > 2s) — aborting"); return False
    _switch_uart_baud(CONFIG_BAUD)
    if not set_mode("PROGRAM"):
        print("[WRITE] AUX did not go HIGH on PROGRAM entry — aborting"); return False
    _drain_uart()

    frame = build_write_frame(PERSIST)
    print("[WRITE] TX 11 bytes:", " ".join("%02X" % b for b in frame))
    uart.write(frame)
    _managed_delay(300)   # writes take a bit longer than reads
    reply = uart.read()
    if reply is None or len(reply) < 11:
        print("[WRITE] Short reply (got %r) — module probably rejected the frame" % reply)
        set_mode("NORMAL"); _switch_uart_baud(DATA_BAUD); return False

    print("[WRITE] RX 11 bytes:", " ".join("%02X" % b for b in reply[:11]))
    if reply[0] != RETURNED_CMD:
        print("[WRITE] WARNING: reply cmd byte is %02X (expected %02X)" % (reply[0], RETURNED_CMD))
    if reply[3:11] != frame[3:11]:
        print("[WRITE] MISMATCH: echo register bytes differ from what we sent")
    else:
        print("[WRITE] OK: echo register bytes match")
    decoded = decode_regs(reply[3:11])
    for k, v in decoded.items():
        print("  %-13s %s" % (k, v))

    set_mode("NORMAL")
    _switch_uart_baud(DATA_BAUD)
    return True


def _tx_one(seq):
    """Build and TX a single payload frame. Prefixes destination header
    if WR_FIXED_MODE is on so the module's fixed-mode addressing kicks in.
    Note: this trusts the *current* WR_FIXED_MODE value — i.e. assumes
    the module has been programmed accordingly. If you flip WR_FIXED_MODE
    here without re-running WRITE, transmission will fail or broadcast."""
    body = (TX_PAYLOAD % seq).encode()
    if WR_FIXED_MODE:
        head = bytes([TX_DEST_H, TX_DEST_L, TX_DEST_CHAN])
        frame = head + body
    else:
        frame = body
    uart.write(frame)
    _led_blink(255, 0, 0)   # red flash on send


def op_tx():
    """Send TX_COUNT frames at TX_INTERVAL_MS spacing. The module must
    already be in NORMAL mode with the desired transmission policy
    programmed (transparent vs fixed) — this op never touches the
    config registers."""
    if not set_mode("NORMAL"):
        print("[TX] AUX did not go HIGH on NORMAL entry — aborting"); return
    print("[TX] mode=%s, sending %d frames at %d ms intervals"
          % ("FIXED" if WR_FIXED_MODE else "TRANSPARENT", TX_COUNT, TX_INTERVAL_MS))
    for i in range(1, TX_COUNT + 1):
        _tx_one(i)
        if (i % 10) == 0:
            print("[TX] %d sent" % i)
        time.sleep_ms(TX_INTERVAL_MS)
    print("[TX] done")


def _on_rx_frame():
    """Hook for RX visual feedback — green flash."""
    _led_blink(0, 255, 0)


def _print_rx_frame(raw):
    """Pretty-print a received frame. If WR_RSSI_BYTE was programmed in
    the module's NVRAM, the trailing byte is the per-frame RSSI in the
    'unsigned offset' form `RSSI_dBm = -(256 - byte)`. We DO NOT strip
    it — we always print the raw bytes, and if RSSI mode is on we also
    decode and print the dBm value."""
    hex_bytes = " ".join("%02X" % b for b in raw)
    try:
        ascii_view = raw.decode("utf-8")
    except Exception:
        ascii_view = "<non-utf8>"

    if RX_RSSI_BYTE_ENABLED and len(raw) >= 1:
        rssi_byte = raw[-1]
        rssi_dbm = -(256 - rssi_byte)
        body = raw[:-1]
        try:
            body_ascii = body.decode("utf-8")
        except Exception:
            body_ascii = "<non-utf8>"
        print("[RX %dB] raw=%s | payload=%r | RSSI=%d dBm (raw byte 0x%02X)"
              % (len(raw), hex_bytes, body_ascii, rssi_dbm, rssi_byte))
    else:
        print("[RX %dB] raw=%s | payload=%r" % (len(raw), hex_bytes, ascii_view))


def op_rx():
    """Listen for incoming frames. We poll uart.any() and wait for AUX
    HIGH before reading, mirroring lora_transport.py's recv() logic but
    without the unconditional last-byte strip — the RSSI byte is logged
    instead of consumed."""
    if not set_mode("NORMAL"):
        print("[RX] AUX did not go HIGH on NORMAL entry — aborting"); return
    print("[RX] Listening (duration=%ds, 0=forever). RSSI_byte_mode=%s"
          % (RX_DURATION_S, RX_RSSI_BYTE_ENABLED))
    start = time.ticks_ms()
    count = 0
    while True:
        if RX_DURATION_S and time.ticks_diff(time.ticks_ms(), start) > RX_DURATION_S * 1000:
            break
        if uart.any():
            # AUX goes LOW while module is forwarding data to UART; wait
            # for it to come back HIGH so we have a complete frame, not
            # half a packet.
            wait_aux_high(2000)
            raw = uart.read()
            if raw:
                count += 1
                _on_rx_frame()
                _print_rx_frame(raw)
        time.sleep_ms(20)
    print("[RX] done, %d frames received" % count)


def op_wedge_stress():
    """The big one. WS_CYCLES iterations of:
        1. WRITE (volatile so we don't burn flash)
        2. TX a few frames
        3. READ (no writes, just mode-bouncing)
        4. RX listen for WS_RX_LISTEN_S
    On each cycle, count how many frames we receive in step 4 (if a
    paired Pico is TXing on the other end). The smoking-gun symptom is
    'frames stop arriving after the first WRITE in step 1 and never
    return until power cycle.' If we run WS_CYCLES = 50 with no RX
    deficit, the in-band approach is safe to integrate."""
    print("[STRESS] Running %d cycles of write-tx-read-rx" % WS_CYCLES)
    rx_log = []  # rx count per cycle, for post-run analysis
    for cycle in range(1, WS_CYCLES + 1):
        print("\n[STRESS] === Cycle %d/%d ===" % (cycle, WS_CYCLES))
        if not op_write():
            print("[STRESS] WRITE failed at cycle %d — aborting" % cycle); return
        time.sleep_ms(WS_INTER_DELAY_MS)

        if not set_mode("NORMAL"):
            print("[STRESS] Mode change to NORMAL failed at cycle %d" % cycle); return
        for i in range(WS_TX_PER_CYCLE):
            _tx_one(cycle * 1000 + i)
            time.sleep_ms(150)
        # Wait for the final TX frame to actually leave the air before we
        # disturb the module again. uart.write() returns when the Pico is
        # done shoving bytes into the E220's UART; the module then needs
        # time to RF-transmit them. AUX goes LOW during TX and back HIGH
        # when done — that's our "fully drained" signal.
        wait_aux_high(3000)
        time.sleep_ms(WS_INTER_DELAY_MS)

        if not op_read():
            print("[STRESS] READ failed at cycle %d — likely wedged" % cycle); return
        time.sleep_ms(WS_INTER_DELAY_MS)

        # RX window — count frames. If RX has wedged this will be 0.
        if not set_mode("NORMAL"):
            print("[STRESS] Mode change to NORMAL failed before RX at cycle %d" % cycle); return
        rx_start = time.ticks_ms()
        rx_count = 0
        while time.ticks_diff(time.ticks_ms(), rx_start) < WS_RX_LISTEN_S * 1000:
            if uart.any():
                wait_aux_high(2000)
                raw = uart.read()
                if raw:
                    rx_count += 1
                    _on_rx_frame()
            time.sleep_ms(20)
        rx_log.append(rx_count)
        print("[STRESS] Cycle %d: rx=%d in %ds" % (cycle, rx_count, WS_RX_LISTEN_S))

    print("\n[STRESS] Summary: %d cycles" % WS_CYCLES)
    print("[STRESS] RX per cycle:", rx_log)
    zero_run = max_run = 0
    for n in rx_log:
        if n == 0:
            zero_run += 1
            max_run = max(max_run, zero_run)
        else:
            zero_run = 0
    print("[STRESS] Longest consecutive RX-zero run: %d" % max_run)
    if max_run >= 3:
        print("[STRESS] FAIL: RX appears to wedge after register writes")
    else:
        print("[STRESS] PASS: RX survives register writes")


def op_rr():
    """Two READs back-to-back with NORMAL mode in between. Strips
    everything else away — no TX, no register write. If this fails on
    the second READ, we know the issue is consecutive PROGRAM-mode ops
    in the same boot, not anything to do with TX or with mutating
    registers."""
    print("\n[RR] === First READ ===")
    if not op_read():
        print("[RR] First READ failed — module probably wasn't ready at boot."); return
    print("\n[RR] === Settle in NORMAL for 500 ms ===")
    if not set_mode("NORMAL"):
        print("[RR] Couldn't return to NORMAL between reads."); return
    time.sleep_ms(500)
    print("\n[RR] === Second READ ===")
    if not op_read():
        print("\n[RR] FAIL: second consecutive READ returned no data.")
        print("[RR] This proves the issue is consecutive PROGRAM-mode ops,")
        print("[RR] not anything to do with writes or transparent-mode TX.")
        return
    print("\n[RR] PASS: two READs in the same boot both succeeded.")


def op_rwt_loop():
    """Read-Write-Transmit loop. Pair with another Pico in OP='RX'.

    Each cycle:
      1. READ current registers (confirm we can talk to the module).
      2. WRITE the same register payload back (volatile). This is the
         operation we want to prove safe — if it's going to wedge the
         RF path, we'll see no further TX frames arriving on the peer.
      3. Send one TX frame (numbered sequentially).
      4. Settle in NORMAL for RWT_DELAY_MS, then repeat.

    On the partner Pico, run OP='RX'. The pass criterion is: frames keep
    arriving on the partner across all RWT_CYCLES cycles. The fail mode
    is: frames stop arriving after cycle 1 (the first WRITE)."""
    print("[RWT] %d cycles of read-write-tx, %d ms between cycles" % (RWT_CYCLES, RWT_DELAY_MS))
    print("[RWT] Pair me with a second Pico running OP='RX' on the same channel.")
    print("[RWT] Watch the peer's REPL — frames stopping = WRITE wedged the RF path.")

    for cycle in range(1, RWT_CYCLES + 1):
        print("\n[RWT] === Cycle %d/%d ===" % (cycle, RWT_CYCLES))

        if not op_read():
            print("[RWT] FAIL at cycle %d: READ failed" % cycle); return
        time.sleep_ms(RWT_INTRA_DELAY_MS)

        if not op_write():
            print("[RWT] FAIL at cycle %d: WRITE failed" % cycle); return
        time.sleep_ms(RWT_INTRA_DELAY_MS)

        # TX one frame from NORMAL mode. op_write() left us in NORMAL.
        if not wait_aux_high(2000):
            print("[RWT] WARN: AUX still LOW before TX (cycle %d)" % cycle)
        _tx_one(cycle)
        print("[RWT] Cycle %d: TX'd frame seq=%d. Check peer's REPL." % (cycle, cycle))
        # Wait for the frame to actually leave the air before the next cycle.
        wait_aux_high(3000)
        time.sleep_ms(RWT_DELAY_MS)

    print("\n[RWT] All %d cycles completed on the TX side without local error." % RWT_CYCLES)
    print("[RWT] If the peer's REPL shows %d frames received, in-band config is safe." % RWT_CYCLES)


def op_rwr():
    """READ → WRITE (with WR_ADDL changed) → READ, NORMAL between each.
    Verifies the second READ reflects what WRITE just put in volatile.
    This is what you asked for: simplest test of back-to-back ops with a
    meaningful register change in the middle."""
    print("\n[RWR] === First READ (initial state) ===")
    if not op_read():
        print("[RWR] First READ failed."); return
    print("\n[RWR] === Settle in NORMAL for 500 ms ===")
    if not set_mode("NORMAL"):
        print("[RWR] Couldn't return to NORMAL after first READ."); return
    time.sleep_ms(500)
    print("\n[RWR] === WRITE (changing ADDL to 0x%02X) ===" % WR_ADDL)
    if not op_write():
        print("\n[RWR] FAIL: WRITE failed.")
        print("[RWR] Means a READ followed by a WRITE wedges the module.")
        return
    print("\n[RWR] === Settle in NORMAL for 500 ms ===")
    if not set_mode("NORMAL"):
        print("[RWR] Couldn't return to NORMAL after WRITE."); return
    time.sleep_ms(500)
    print("\n[RWR] === Second READ (should reflect the WRITE) ===")
    if not op_read():
        print("\n[RWR] FAIL: second READ failed after a successful WRITE.")
        print("[RWR] This is the 'WRITE wedges subsequent reads' shape.")
        return
    print("\n[RWR] PASS: READ → WRITE → READ all completed in one boot.")
    print("[RWR] The second READ above should show ADDL = 0x%02X." % WR_ADDL)


# ============================================================
# Entry
# ============================================================

def main():
    print("=" * 56)
    print("E220 in-band test, OP=%s" % OP)
    print("Hardware: UART%d tx=GP%d rx=GP%d M0=GP%d M1=GP%d AUX=GP%d"
          % (UART_ID, TX_PIN, RX_PIN, M0_PIN, M1_PIN, AUX_PIN))
    print("=" * 56)

    # Wait for AUX to settle HIGH at boot (module is ready when AUX HIGH).
    if not wait_aux_high(2000):
        print("[INIT] WARNING: AUX did not go HIGH at boot. Check wiring.")

    if   OP == "READ":          op_read()
    elif OP == "WRITE":         op_write()
    elif OP == "TX":            op_tx()
    elif OP == "RX":            op_rx()
    elif OP == "WEDGE_STRESS":  op_wedge_stress()
    elif OP == "RR":            op_rr()
    elif OP == "RWR":           op_rwr()
    elif OP == "RWT_LOOP":      op_rwt_loop()
    else:
        print("[ERR] Unknown OP %r. Pick READ | WRITE | TX | RX | WEDGE_STRESS | RR | RWR | RWT_LOOP." % OP)


main()
