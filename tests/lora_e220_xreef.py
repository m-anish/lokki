# ----------------------------------------------------------------------
# Test using xreef's EByte_LoRa_E220_micropython_library directly.
#
# Layout: tests/xreef/{lora_e220.py, lora_e220_constants.py,
# lora_e220_operation_constant.py} are the library files (verbatim).
# This script is the test harness, modeled after the upstream
# set_configuration.py + send_transparent_string_message.py examples.
#
# Goal: prove or refute the theory that *their* config-write flow
# leaves the radio in a healthy RX state after writing, while ours
# doesn't. Same hardware (Pico 2 + E220-900T22D), same wiring, same
# desired config (channel 18, transparent, addr=UNIT_ID).
# ----------------------------------------------------------------------

import sys
import time
import neopixel
from machine import Pin, UART

# Make the bundled xreef package importable.
sys.path.insert(0, "/xreef")

from xreef.lora_e220 import LoRaE220, Configuration
from xreef.lora_e220_constants import (
    AirDataRate, UARTBaudRate, UARTParity,
    SubPacketSetting, RssiAmbientNoiseEnable, TransmissionPower22,
    FixedTransmission, WorPeriod, LbtEnableByte, RssiEnableByte,
)
from xreef.lora_e220_operation_constant import ResponseStatusCode


# ============================================================
# CONFIG
# ============================================================
UNIT_ID  = 0          # patched by flash_test.sh

UART_ID  = 0
TX_PIN   = 0
RX_PIN   = 1
M0_PIN   = 2
M1_PIN   = 3
AUX_PIN  = 4
LED_PIN  = 5

LED_ORDER = "RGB"
PING_INTERVAL_MS = 2500


# ============================================================
# WS2812
# ============================================================
_np = neopixel.NeoPixel(Pin(LED_PIN), 1)
def led(r, g, b):
    if LED_ORDER == "RGB":
        _np[0] = (g, r, b)
    else:
        _np[0] = (r, g, b)
    _np.write()


# ============================================================
# Bring up E220 via xreef's library
# ============================================================
led(0, 0, 40)             # blue: startup

uart = UART(UART_ID, baudrate=9600, tx=Pin(TX_PIN), rx=Pin(RX_PIN))
lora = LoRaE220("900T22D", uart, aux_pin=AUX_PIN, m0_pin=M0_PIN, m1_pin=M1_PIN)

print("[XREEF] begin()...")
code = lora.begin()
print("[XREEF] begin code:", ResponseStatusCode.get_description(code))

# Read whatever config the modules have right now.
print("[XREEF] get_configuration()...")
code, cfg = lora.get_configuration()
print("[XREEF] get_configuration code:", ResponseStatusCode.get_description(code))
if cfg is not None:
    print("[XREEF] BEFORE: ADDH=0x{:02x} ADDL=0x{:02x} CHAN={}  air_rate={}  baud={}  fixed={}  rssi_byte={}".format(
        cfg.ADDH, cfg.ADDL, cfg.CHAN,
        cfg.SPED.airDataRate, cfg.SPED.uartBaudRate,
        cfg.SPED.uartParity,
        cfg.TRANSMISSION_MODE.fixedTransmission,
        cfg.TRANSMISSION_MODE.enableRSSI,
    ))

# Build the desired config (transparent, channel 18, addr=UNIT_ID, no encryption)
desired = Configuration("900T22D")
desired.ADDH = 0x00
desired.ADDL = UNIT_ID & 0xFF
desired.CHAN = 18                                                   # 868.125 MHz
desired.SPED.uartBaudRate = UARTBaudRate.BPS_9600
desired.SPED.uartParity   = UARTParity.MODE_00_8N1
desired.SPED.airDataRate  = AirDataRate.AIR_DATA_RATE_010_24
desired.OPTION.subPacketSetting = SubPacketSetting.SPS_200_00
desired.OPTION.RSSIAmbientNoise = RssiAmbientNoiseEnable.RSSI_AMBIENT_NOISE_DISABLED
desired.OPTION.transmissionPower = TransmissionPower22.POWER_22
desired.TRANSMISSION_MODE.fixedTransmission = FixedTransmission.TRANSPARENT_TRANSMISSION
desired.TRANSMISSION_MODE.enableLBT  = LbtEnableByte.LBT_DISABLED
desired.TRANSMISSION_MODE.enableRSSI = RssiEnableByte.RSSI_DISABLED
desired.TRANSMISSION_MODE.WORPeriod  = WorPeriod.WOR_500_000
desired.CRYPT.CRYPT_H = 0
desired.CRYPT.CRYPT_L = 0

print("[XREEF] set_configuration(volatile)...")
# permanentConfiguration=False → uses 0xC2 (volatile). Avoids touching flash.
code, after = lora.set_configuration(desired, permanentConfiguration=False)
print("[XREEF] set_configuration code:", ResponseStatusCode.get_description(code))
if after is not None:
    print("[XREEF] AFTER : ADDH=0x{:02x} ADDL=0x{:02x} CHAN={}  fixed={}  rssi_byte={}".format(
        after.ADDH, after.ADDL, after.CHAN,
        after.TRANSMISSION_MODE.fixedTransmission,
        after.TRANSMISSION_MODE.enableRSSI,
    ))

led(20, 20, 0)            # idle yellow


# ============================================================
# Bidirectional PING/RX loop
# ============================================================

print("[XREEF] Test loop running. unit_id={}".format(UNIT_ID))
counter = 0
last_send_ms = time.ticks_ms()

while True:
    if uart.any():
        data = uart.read()
        if data:
            try:
                text = data.decode("utf-8", "ignore").strip()
            except Exception:
                text = repr(data)
            if text:
                print("[RX] {!r}".format(text))
                led(40, 0, 0)
                time.sleep_ms(150)
                led(20, 20, 0)

    if time.ticks_diff(time.ticks_ms(), last_send_ms) >= PING_INTERVAL_MS:
        msg = "Hello {} from unit {}".format(counter, UNIT_ID)
        uart.write(msg + "\n")
        print("[TX] {!r}".format(msg))
        led(0, 40, 0)
        time.sleep_ms(100)
        led(20, 20, 0)
        counter += 1
        last_send_ms = time.ticks_ms()

    time.sleep_ms(20)
