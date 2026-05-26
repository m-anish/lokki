"""Interactive I2C / DS3231 RTC helper.

Standalone tool — does not import anything from the Lokki firmware
modules so it keeps working even when src/ is half-bricked. Reads
i2c_sda_pin / i2c_scl_pin / i2c_freq_hz from /config.json (so it
matches the running fleet configuration), or accepts defaults if
config.json is missing.

Run via mpremote without flashing:

    mpremote run firmware/micropython/tools/i2c_helper.py

…or after flashing with update.sh (which ships this file to /tools/
on the device):

    mpremote exec "exec(open('/tools/i2c_helper.py').read())"

What it does (interactive menu):
  1. Scan I2C bus — list every addr that ACKs, naming known chips
     (DS3231 @ 0x68, BME280 @ 0x76/0x77, BH1750 @ 0x23/0x5C, SCD40
     @ 0x62, etc.). Useful to confirm whether wiring is alive and
     whether the DS3231 is responding at all.
  2. Read DS3231 — pull the 7 time registers, print decoded
     date/time + raw bytes, plus the status register's OSF (oscillator
     stop flag) and BBSQW bits so you can tell whether the backup
     battery has been drained.
  3. Read DS3231 temperature — DS3231 has an on-chip temp sensor
     (registers 0x11/0x12). Handy heat-related supply diagnostic.
  4. Write DS3231 — set the time. Either from the host (mpremote
     pipes in the host's current time as a tuple) or by typing each
     field at the prompt. Clears OSF after a successful write.
  5. Re-scan / re-read in a loop — useful when chasing intermittent
     EIO; you'll see the bus drop in and out across iterations.
  6. Exit.

Ctrl-C bails cleanly. Failures print the exception and return to the
menu so a single bus glitch doesn't kill the session.
"""
import sys
import time
import json
from machine import I2C, Pin


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_hw_cfg():
    """Read i2c pins + freq from /config.json. Falls back to defaults
    if the file is missing/unreadable so this tool still works on a
    fresh / safe-mode device."""
    defaults = {"sda": 20, "scl": 21, "freq": 100000, "id": 0}
    try:
        with open("/config.json") as f:
            cfg = json.load(f)
        hw = cfg.get("hardware", {}) or {}
        return {
            "sda":  hw.get("i2c_sda_pin",   defaults["sda"]),
            "scl":  hw.get("i2c_scl_pin",   defaults["scl"]),
            "freq": hw.get("i2c_freq_hz",   defaults["freq"]),
            "id":   hw.get("i2c_id",        defaults["id"]),
        }
    except Exception as e:
        print(f"[i2c-helper] couldn't read /config.json ({e}); using defaults")
        return defaults


def _make_bus(hw):
    return I2C(hw["id"], scl=Pin(hw["scl"]), sda=Pin(hw["sda"]), freq=hw["freq"])


# ---------------------------------------------------------------------------
# Known device fingerprints — for friendlier scan output
# ---------------------------------------------------------------------------

_KNOWN_DEVICES = {
    0x23: "BH1750 ambient light (alt addr)",
    0x5C: "BH1750 ambient light",
    0x57: "AT24C32 EEPROM (on DS3231 modules)",
    0x62: "SCD40/SCD41 CO2",
    0x68: "DS3231 RTC",
    0x76: "BME280 / BMP280 temp+humidity (alt)",
    0x77: "BME280 / BMP280 temp+humidity",
}


# ---------------------------------------------------------------------------
# Bus scan
# ---------------------------------------------------------------------------

def cmd_scan(bus):
    try:
        addrs = bus.scan()
    except Exception as e:
        print(f"[scan] bus.scan() raised: {e}")
        return
    if not addrs:
        print("[scan] no devices responded — check wiring, pull-ups, power")
        return
    print(f"[scan] {len(addrs)} device(s) found:")
    for a in addrs:
        name = _KNOWN_DEVICES.get(a, "?")
        print(f"  0x{a:02X}  ({name})")


# ---------------------------------------------------------------------------
# DS3231 read
# ---------------------------------------------------------------------------

_DS3231_ADDR  = 0x68
_REG_TIME     = 0x00
_REG_TEMP     = 0x11
_REG_STATUS   = 0x0F
_REG_CONTROL  = 0x0E


def _bcd2int(b):
    return ((b >> 4) * 10) + (b & 0x0F)


def _int2bcd(n):
    return ((n // 10) << 4) | (n % 10)


def cmd_read_time(bus):
    try:
        raw = bus.readfrom_mem(_DS3231_ADDR, _REG_TIME, 7)
    except Exception as e:
        print(f"[read-time] I2C read failed: {e}")
        return
    sec    = _bcd2int(raw[0] & 0x7F)
    minute = _bcd2int(raw[1] & 0x7F)
    hour   = _bcd2int(raw[2] & 0x3F)   # ignoring 12/24 flag — 24h assumed
    wday   = raw[3] & 0x07              # 1..7, DS3231 spec
    day    = _bcd2int(raw[4] & 0x3F)
    month  = _bcd2int(raw[5] & 0x1F)
    year   = _bcd2int(raw[6]) + 2000

    print(f"[read-time] {year:04d}-{month:02d}-{day:02d} "
          f"{hour:02d}:{minute:02d}:{sec:02d}  weekday={wday}")
    print(f"[read-time] raw 7 bytes: {' '.join('%02X' % b for b in raw)}")

    # Status register — bit 7 OSF tells you the oscillator has stopped
    # at least once since you last cleared it (typically: backup
    # battery flat OR first power-up). Bit 3 EN32kHz is informational.
    try:
        status = bus.readfrom_mem(_DS3231_ADDR, _REG_STATUS, 1)[0]
        osf  = (status >> 7) & 1
        bsy  = (status >> 2) & 1
        a2f  = (status >> 1) & 1
        a1f  =  status        & 1
        print(f"[read-time] status 0x{status:02X}  "
              f"OSF={osf}  BUSY={bsy}  A2F={a2f}  A1F={a1f}")
        if osf:
            print("[read-time] ⚠ OSF=1 — oscillator stopped at some point. "
                  "Battery likely flat. After setting time, clear OSF.")
    except Exception as e:
        print(f"[read-time] could not read status reg: {e}")


def cmd_read_temp(bus):
    try:
        raw = bus.readfrom_mem(_DS3231_ADDR, _REG_TEMP, 2)
    except Exception as e:
        print(f"[temp] I2C read failed: {e}")
        return
    # MSB is signed integer degrees C; LSB top 2 bits are 0.25 C steps
    msb = raw[0]
    if msb & 0x80:
        msb -= 256
    frac = ((raw[1] >> 6) & 0x03) * 0.25
    temp = msb + frac
    print(f"[temp] DS3231 die temperature: {temp:.2f} °C")


# ---------------------------------------------------------------------------
# DS3231 write
# ---------------------------------------------------------------------------

def _prompt_int(label, lo, hi, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            s = input(f"  {label} ({lo}-{hi}){suffix}: ").strip()
        except EOFError:
            return default
        if not s and default is not None:
            return default
        try:
            n = int(s)
            if lo <= n <= hi:
                return n
            print(f"  out of range {lo}..{hi}")
        except ValueError:
            print("  please enter an integer")


def cmd_write_time(bus, source="prompt"):
    """source = "prompt" or "host". host uses time.localtime() of
    whatever clock the MCU has — useful right after NTP set it."""
    if source == "host":
        try:
            lt = time.localtime()
            year, month, day = lt[0], lt[1], lt[2]
            hour, minute, sec = lt[3], lt[4], lt[5]
            wday = (lt[6] % 7) + 1   # DS3231 wants 1..7
        except Exception as e:
            print(f"[write-time] couldn't read host time: {e}")
            return
        print(f"[write-time] using MCU clock: {year}-{month:02d}-{day:02d} "
              f"{hour:02d}:{minute:02d}:{sec:02d} wday={wday}")
    else:
        # Pre-fill with the current MCU clock if it's sane, else
        # blank-pre-fill. Operator can override every field.
        try:
            lt = time.localtime()
            default_year, default_month, default_day = lt[0], lt[1], lt[2]
            default_hour, default_min, default_sec   = lt[3], lt[4], lt[5]
            default_wday = (lt[6] % 7) + 1
            if default_year < 2024:
                default_year = 2026
        except Exception:
            default_year, default_month, default_day = 2026, 1, 1
            default_hour, default_min, default_sec   = 0, 0, 0
            default_wday = 1
        print("Enter target time (press Enter to accept default):")
        year   = _prompt_int("year",   2024, 2099, default_year)
        month  = _prompt_int("month",     1,   12, default_month)
        day    = _prompt_int("day",       1,   31, default_day)
        hour   = _prompt_int("hour",      0,   23, default_hour)
        minute = _prompt_int("minute",    0,   59, default_min)
        sec    = _prompt_int("second",    0,   59, default_sec)
        wday   = _prompt_int("weekday (1=Mon)", 1, 7, default_wday)

    buf = bytes([
        _int2bcd(sec),
        _int2bcd(minute),
        _int2bcd(hour),
        wday & 0x07,
        _int2bcd(day),
        _int2bcd(month),
        _int2bcd(year - 2000),
    ])
    try:
        bus.writeto_mem(_DS3231_ADDR, _REG_TIME, buf)
    except Exception as e:
        print(f"[write-time] I2C write failed: {e}")
        return
    print("[write-time] wrote 7 time bytes")
    # Clear OSF so the warning goes away after a successful set.
    try:
        status = bus.readfrom_mem(_DS3231_ADDR, _REG_STATUS, 1)[0]
        if status & 0x80:
            bus.writeto_mem(_DS3231_ADDR, _REG_STATUS, bytes([status & 0x7F]))
            print("[write-time] cleared OSF (oscillator-stop flag)")
    except Exception as e:
        print(f"[write-time] could not clear OSF: {e}")
    # Read back as a sanity check
    cmd_read_time(bus)


# ---------------------------------------------------------------------------
# Soak loop — useful for intermittent-failure diagnosis
# ---------------------------------------------------------------------------

def cmd_soak(bus, n=20, sleep_ms=500):
    print(f"[soak] {n} iterations, {sleep_ms} ms apart — Ctrl-C to abort early")
    fails = 0
    for i in range(1, n + 1):
        try:
            bus.scan()
            bus.readfrom_mem(_DS3231_ADDR, _REG_TIME, 7)
            print(f"  {i:3d}/{n}  OK")
        except KeyboardInterrupt:
            print("[soak] aborted")
            return
        except Exception as e:
            fails += 1
            print(f"  {i:3d}/{n}  FAIL  {e}")
        time.sleep_ms(sleep_ms)
    print(f"[soak] done. {fails}/{n} failures ({100.0*fails/n:.1f}%)")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

_MENU = """
=== Lokki I2C / DS3231 helper ===
  1) Scan I2C bus
  2) Read DS3231 time + status
  3) Read DS3231 temperature
  4) Write DS3231 time — interactive
  5) Write DS3231 time — from MCU clock (host-set if you NTPed first)
  6) Soak test — 20 reads, see how many fail
  7) Re-init bus (after changing pull-ups / wiring while tool is running)
  q) Quit
""".strip()


def main():
    hw = _load_hw_cfg()
    print(f"[i2c-helper] bus id={hw['id']}  sda=GP{hw['sda']}  scl=GP{hw['scl']}  freq={hw['freq']} Hz")
    bus = _make_bus(hw)

    while True:
        print(_MENU)
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "1":
            cmd_scan(bus)
        elif choice == "2":
            cmd_read_time(bus)
        elif choice == "3":
            cmd_read_temp(bus)
        elif choice == "4":
            cmd_write_time(bus, source="prompt")
        elif choice == "5":
            cmd_write_time(bus, source="host")
        elif choice == "6":
            cmd_soak(bus)
        elif choice == "7":
            hw = _load_hw_cfg()
            bus = _make_bus(hw)
            print(f"[i2c-helper] re-init: id={hw['id']}  sda=GP{hw['sda']}  scl=GP{hw['scl']}  freq={hw['freq']} Hz")
        elif choice in ("q", "quit", "exit"):
            return
        else:
            print("(unknown choice)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
