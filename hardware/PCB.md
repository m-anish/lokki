# Lokki PCB — Rev0

2-layer PCB. Schematic designed in EasyEDA (2025-08-23). KiCad files are being rebuilt from the EasyEDA source — see `hardware/kicad/`.

For the complete component list, GPIO map, and hardware details see [docs/architecture.md](../docs/architecture.md).

---

## Summary

| Component | Qty | Notes |
|-----------|-----|-------|
| Raspberry Pi Pico 2 / Pico 2 W | 1 | RP2350, pin-compatible. Use Pico 2 W for coordinator, Pico 2 for leaves. |
| PT4115 LED driver | 8 | Constant-current, PWM dimming, screw terminals for LED+/LED- |
| IRLML2502 MOSFET + relay | 2 | SPDT relay with 1N4001 flyback diode |
| LM2596 buck module | 1 | +30VDC → +5V, reverse polarity protection |
| DS3231 RTC module | 1 | I2C, battery-backed |
| E220-900T22D | 1 | LoRa radio, ~868MHz, UART |
| WS2812 addressable LED | 1 | Status indicator |
| PIR input headers (RJ45) | 4 | Zener + 100nF + 10k + 470R per input |
| I2C expansion header (RJ45) | 1 | For optional BME280/BH1750/SCD40 sensors |
| LDR + 10k divider | 1 | Ambient light sensing on ADC |
| Reset button | 1 | GP12, pulled up to 3V3 |
| M3 mounting holes | 4 | 6mm from corners |

## Power

- Input: up to +30VDC (single screw terminal)
- Regulated: +5V via LM2596 module
- Logic: 3.3V from Pico 2 / Pico 2 W onboard regulator
