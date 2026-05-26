from machine import I2C, Pin
from hardware.urtc import DS3231
from core.config_manager import config_manager

hw = config_manager.get("hardware")
i2c = I2C(
    0,
    scl=Pin(hw.get("i2c_scl_pin", 21)),
    sda=Pin(hw.get("i2c_sda_pin", 20)),
    freq=hw.get("i2c_freq_hz", 400000),
)
rtc = DS3231(i2c)
