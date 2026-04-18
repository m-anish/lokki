"""
Shared RTC and I2C instances for use across modules.

This module provides shared instances of I2C bus and DS3231 RTC to avoid
creating multiple instances which could cause conflicts.
"""

from machine import I2C, Pin
import urtc
from lib.config_manager import RTC_I2C_SDA_PIN, RTC_I2C_SCL_PIN

# Initialize shared I2C bus for RTC
i2c = I2C(0, scl=Pin(RTC_I2C_SCL_PIN), sda=Pin(RTC_I2C_SDA_PIN))

# Create shared DS3231 instance from urtc library
rtc = urtc.DS3231(i2c)