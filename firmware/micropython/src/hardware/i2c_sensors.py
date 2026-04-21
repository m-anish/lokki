import time
from shared.simple_logger import Logger

log = Logger()

# I2C addresses to probe
_ADDR_BME280  = (0x76, 0x77)
_ADDR_BME680  = (0x76, 0x77)
_ADDR_SHT31   = (0x44, 0x45)
_ADDR_BH1750  = (0x23, 0x5C)
_ADDR_SCD40   = (0x62,)

_POLL_INTERVAL_S = 60


def _i2c_scan(i2c):
    try:
        return set(i2c.scan())
    except Exception:
        return set()


# ------------------------------------------------------------------
# Minimal register-based drivers (no external libraries)
# ------------------------------------------------------------------

class _BME280:
    def __init__(self, i2c, addr):
        self._i2c   = i2c
        self._addr  = addr
        self._cal   = self._read_calibration()
        self._write(0xF4, 0x27)   # osrs_t=001, osrs_p=001, mode=normal
        self._write(0xF2, 0x01)   # osrs_h=001

    def _write(self, reg, val):
        self._i2c.writeto_mem(self._addr, reg, bytes([val]))

    def _read(self, reg, n):
        return self._i2c.readfrom_mem(self._addr, reg, n)

    def _read_calibration(self):
        c = self._read(0x88, 24)
        h = self._read(0xA1, 1) + self._read(0xE1, 7)
        from struct import unpack_from
        T = unpack_from('<HhH', c, 0)
        P = unpack_from('<HhhhhhhhhH', c, 6)
        H = [h[0],
             unpack_from('<h', h, 1)[0],
             h[3],
             (unpack_from('<h', h, 4)[0] >> 4) | ((h[4] & 0x0F) << 4),
             unpack_from('<b', h, 5)[0],
             unpack_from('<b', h, 6)[0]]
        return T, P, H

    def read(self):
        data = self._read(0xF7, 8)
        adc_P = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        adc_T = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        adc_H = (data[6] << 8)  |  data[7]
        T, P, H = self._cal

        # Temperature
        v1 = (adc_T / 16384.0 - T[0] / 1024.0) * T[1]
        v2 = (adc_T / 131072.0 - T[0] / 8192.0) ** 2 * T[2]
        t_fine = v1 + v2
        temp_c = t_fine / 5120.0

        # Pressure
        v1 = t_fine / 2.0 - 64000.0
        v2 = v1 * v1 * P[5] / 32768.0
        v2 += v1 * P[4] * 2.0
        v2 = v2 / 4.0 + P[3] * 65536.0
        v1 = (P[2] * v1 * v1 / 524288.0 + P[1] * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * P[0]
        if v1 == 0:
            pressure_hpa = 0
        else:
            p = 1048576.0 - adc_P
            p = ((p - v2 / 4096.0) * 6250.0) / v1
            v1 = P[8] * p * p / 2147483648.0
            v2 = p * P[7] / 32768.0
            pressure_hpa = (p + (v1 + v2 + P[6]) / 16.0) / 100.0

        # Humidity
        x = t_fine - 76800.0
        if x == 0:
            humidity_pct = 0.0
        else:
            x = (adc_H - (H[3] * 64.0 + H[4] / 16384.0 * x)) * \
                (H[1] / 65536.0 * (1.0 + H[5] / 67108864.0 * x *
                 (1.0 + H[2] / 67108864.0 * x)))
            x *= 1.0 - H[0] * x / 524288.0
            humidity_pct = max(0.0, min(100.0, x))

        return {
            "temp_c":        round(temp_c, 2),
            "pressure_hpa":  round(pressure_hpa, 1),
            "humidity_pct":  round(humidity_pct, 1),
        }


class _SHT31:
    def __init__(self, i2c, addr):
        self._i2c  = i2c
        self._addr = addr

    def read(self):
        self._i2c.writeto(self._addr, b'\x24\x00')
        time.sleep_ms(20)
        data = self._i2c.readfrom(self._addr, 6)
        raw_t = (data[0] << 8) | data[1]
        raw_h = (data[3] << 8) | data[4]
        temp_c       = -45 + 175 * raw_t / 65535.0
        humidity_pct = 100 * raw_h / 65535.0
        return {
            "temp_c":       round(temp_c, 2),
            "humidity_pct": round(humidity_pct, 1),
        }


class _BH1750:
    _CMD_CONT_HIGH = 0x10

    def __init__(self, i2c, addr):
        self._i2c  = i2c
        self._addr = addr
        self._i2c.writeto(self._addr, bytes([self._CMD_CONT_HIGH]))
        time.sleep_ms(180)

    def read(self):
        data = self._i2c.readfrom(self._addr, 2)
        lux = ((data[0] << 8) | data[1]) / 1.2
        return {"lux": round(lux, 1)}


class _SCD40:
    def __init__(self, i2c, addr):
        self._i2c  = i2c
        self._addr = addr
        self._cmd(0x21B1)   # start_periodic_measurement
        time.sleep_ms(100)

    def _cmd(self, cmd):
        self._i2c.writeto(self._addr, bytes([cmd >> 8, cmd & 0xFF]))

    def read(self):
        self._cmd(0xEC05)   # read_measurement
        time.sleep_ms(1)
        data = self._i2c.readfrom(self._addr, 9)
        co2 = (data[0] << 8) | data[1]
        raw_t = (data[3] << 8) | data[4]
        raw_h = (data[6] << 8) | data[7]
        temp_c       = -45 + 175 * raw_t / 65535.0
        humidity_pct = 100 * raw_h / 65535.0
        return {
            "co2_ppm":      co2,
            "temp_c":       round(temp_c, 2),
            "humidity_pct": round(humidity_pct, 1),
        }


# ------------------------------------------------------------------
# Manager
# ------------------------------------------------------------------

class I2CSensors:

    def __init__(self):
        self._sensors  = {}    # {name: driver_instance}
        self._readings = {}
        self._ready    = False

    def init(self):
        try:
            from hardware.rtc_shared import i2c as shared_i2c
        except Exception as e:
            log.warn(f"[I2C_SENSORS] Cannot get shared I2C: {e}")
            return

        found = _i2c_scan(shared_i2c)
        log.info(f"[I2C_SENSORS] I2C bus scan: {[hex(a) for a in sorted(found)]}")

        for addr in _ADDR_BH1750:
            if addr in found:
                try:
                    self._sensors["bh1750"] = _BH1750(shared_i2c, addr)
                    log.info(f"[I2C_SENSORS] BH1750 at 0x{addr:02X}")
                except Exception as e:
                    log.warn(f"[I2C_SENSORS] BH1750 init failed: {e}")
                break

        for addr in _ADDR_SHT31:
            if addr in found:
                try:
                    self._sensors["sht31"] = _SHT31(shared_i2c, addr)
                    log.info(f"[I2C_SENSORS] SHT31 at 0x{addr:02X}")
                except Exception as e:
                    log.warn(f"[I2C_SENSORS] SHT31 init failed: {e}")
                break

        # BME280/BME680 share addresses — try BME680 first (superset)
        for addr in _ADDR_BME280:
            if addr in found and "sht31" not in self._sensors:
                # Distinguish BME680 (chip_id=0x61) vs BME280 (chip_id=0x60)
                try:
                    chip_id = shared_i2c.readfrom_mem(addr, 0xD0, 1)[0]
                    if chip_id == 0x61:
                        # BME680 — use same driver as BME280 for temp/press/hum
                        self._sensors["bme680"] = _BME280(shared_i2c, addr)
                        log.info(f"[I2C_SENSORS] BME680 at 0x{addr:02X}")
                    elif chip_id == 0x60:
                        self._sensors["bme280"] = _BME280(shared_i2c, addr)
                        log.info(f"[I2C_SENSORS] BME280 at 0x{addr:02X}")
                except Exception as e:
                    log.warn(f"[I2C_SENSORS] BME2x0 init failed at 0x{addr:02X}: {e}")
                break

        for addr in _ADDR_SCD40:
            if addr in found:
                try:
                    self._sensors["scd40"] = _SCD40(shared_i2c, addr)
                    log.info(f"[I2C_SENSORS] SCD40 at 0x{addr:02X}")
                except Exception as e:
                    log.warn(f"[I2C_SENSORS] SCD40 init failed: {e}")
                break

        if not self._sensors:
            log.info("[I2C_SENSORS] No expansion sensors detected")
        self._ready = True

    async def run(self):
        import asyncio
        while True:
            if self._sensors:
                self._poll()
            await asyncio.sleep(_POLL_INTERVAL_S)

    def _poll(self):
        readings = {}
        for name, driver in self._sensors.items():
            try:
                readings[name] = driver.read()
            except Exception as e:
                log.warn(f"[I2C_SENSORS] {name} read error: {e}")
        self._readings = readings

    def get_readings(self):
        return dict(self._readings)

    @property
    def has_sensors(self):
        return bool(self._sensors)


i2c_sensors = I2CSensors()
