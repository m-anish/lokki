"""
PWM controller for LED intensity or other output on specified GPIO pins.

Controls PWM frequency and duty cycle with debug logging for multiple pins.
"""

from machine import Pin, PWM
from lib.config_manager import PWM_FREQUENCY, config_manager
from simple_logger import Logger


log = Logger()


class PWMController:
    """
    Single PWM controller class for individual pin control.
    Args:
        freq (int): PWM frequency in Hz.
        pin (int): GPIO pin number for PWM output.
        name (str): Human-readable name for this PWM controller.

    Methods:
        set_freq(freq) - Set PWM frequency.
        set_duty_percent(percent) - Set PWM duty cycle in %.
        deinit() - Deinitialize PWM.
    """
    def __init__(self, freq=PWM_FREQUENCY, pin=16, name="PWM"):
        self.pin = pin
        self.name = name
        self.pwm = PWM(Pin(pin))
        self.freq = freq
        self.pwm.freq(self.freq)
        self.current_duty = 0
        self.set_duty_percent(0)
        log.info(f"[PWM] {self.name} controller initialized at {self.freq} Hz on GPIO{pin}")

    def set_freq(self, freq):
        self.freq = freq
        self.pwm.freq(self.freq)
        log.info(f"[PWM] {self.name} frequency set to {freq} Hz")

    def set_duty_percent(self, percent):
        duty_value = int(percent * 65535 / 100)
        self.pwm.duty_u16(duty_value)
        self.current_duty = percent
        # Reduce logging to save memory
        # log.debug(f"[PWM] {self.name} duty cycle set to {percent}% (duty_u16={duty_value})")

    def get_duty_percent(self):
        return self.current_duty

    def deinit(self):
        self.pwm.deinit()
        log.info(f"[PWM] {self.name} controller deinitialized")


class MultiPWMManager:
    """
    Manager for multiple PWM controllers based on configuration.
    
    Automatically creates and manages PWM controllers for all enabled pins
    from the configuration.
    """
    
    def __init__(self):
        self.controllers = {}
        self.pwm_frequency = PWM_FREQUENCY
        self.initialize_from_config()
    
    def initialize_from_config(self):
        """
        Initialize PWM controllers based on current configuration.
        """
        # Clean up existing controllers
        self.deinit_all()
        
        config = config_manager.get_config_dict()
        self.pwm_frequency = config.get('hardware', {}).get('pwm_frequency', 1000)
        pwm_pins = config.get('pwm_pins', {})
        
        enabled_count = 0
        for pin_key, pin_config in pwm_pins.items():
            # Skip comment fields
            if pin_key.startswith('_'):
                continue
                
            if not pin_config.get('enabled', False):
                continue
                
            gpio_pin = pin_config.get('gpio_pin')
            pin_name = pin_config.get('name', f'Pin {gpio_pin}')
            
            # Validate gpio_pin
            if gpio_pin is None:
                log.error(f"[PWM_MGR] No gpio_pin specified for {pin_key}")
                continue
                
            try:
                controller = PWMController(
                    freq=self.pwm_frequency,
                    pin=gpio_pin,
                    name=pin_name
                )
                self.controllers[pin_key] = controller
                enabled_count += 1
                log.info(f"[PWM_MGR] Initialized controller for {pin_name} on GPIO{gpio_pin}")
                
            except Exception as e:
                log.error(f"[PWM_MGR] Failed to initialize PWM on GPIO{gpio_pin}: {e}")
        
        log.info(f"[PWM_MGR] Initialized {enabled_count} PWM controllers")
    
    def set_pin_duty_percent(self, pin_key, percent):
        """
        Set duty cycle for a specific pin.
        
        Args:
            pin_key (str): Pin configuration key (e.g., 'pin_16')
            percent (int): Duty cycle percentage (0-100)
        """
        if pin_key in self.controllers:
            self.controllers[pin_key].set_duty_percent(percent)
        else:
            log.warn(f"[PWM_MGR] Pin key {pin_key} not found in controllers")
    
    def get_pin_duty_percent(self, pin_key):
        """
        Get current duty cycle for a specific pin.
        
        Args:
            pin_key (str): Pin configuration key
            
        Returns:
            int: Current duty cycle percentage
        """
        if pin_key in self.controllers:
            return self.controllers[pin_key].get_duty_percent()
        return 0
    
    def set_all_pins_duty_percent(self, percent):
        """
        Set duty cycle for all enabled pins.
        
        Args:
            percent (int): Duty cycle percentage (0-100)
        """
        for pin_key, controller in self.controllers.items():
            controller.set_duty_percent(percent)
        log.debug(f"[PWM_MGR] Set all pins to {percent}%")
    
    def get_enabled_pins(self):
        """
        Get list of currently enabled pin keys.
        
        Returns:
            list: List of pin configuration keys
        """
        return list(self.controllers.keys())
    
    def get_pin_status(self):
        """
        Get status of all enabled pins.
        
        Returns:
            dict: {pin_key: {'name': str, 'gpio_pin': int, 'duty_percent': int}}
        """
        status = {}
        config = config_manager.get_config_dict()
        pwm_pins = config.get('pwm_pins', {})
        
        for pin_key, controller in self.controllers.items():
            pin_config = pwm_pins.get(pin_key, {})
            status[pin_key] = {
                'name': pin_config.get('name', 'Unknown'),
                'gpio_pin': pin_config.get('gpio_pin', 0),
                'duty_percent': controller.get_duty_percent()
            }
        
        return status
    
    def deinit_all(self):
        """
        Deinitialize all PWM controllers.
        """
        for controller in self.controllers.values():
            try:
                controller.deinit()
            except Exception as e:
                log.error(f"[PWM_MGR] Error deinitializing controller: {e}")
        
        self.controllers.clear()
        log.info("[PWM_MGR] All PWM controllers deinitialized")
    
    def reload_config(self):
        """
        Reload configuration and reinitialize controllers.
        """
        log.info("[PWM_MGR] Reloading PWM configuration")
        self.initialize_from_config()


# Global multi-PWM manager instance
multi_pwm = MultiPWMManager()
