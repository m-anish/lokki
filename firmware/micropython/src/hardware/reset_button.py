"""Physical reset button watcher.

Polls the configured GPIO (hardware.reset_btn_pin) and triggers a
machine.soft_reset() when the button is held for HOLD_MS. Same effect
as Thonny's Stop/Restart Backend, but available without USB — handy
during debugging when LoRa init is flaky and you want to retry without
pulling power.

Hardware assumption: button to GND, Pico's internal pull-up keeps the
line HIGH at rest. Pressing the button pulls it LOW.
"""

import asyncio
import machine
from machine import Pin
from shared.simple_logger import Logger

log = Logger()

_POLL_MS = 50    # how often we sample
_HOLD_MS = 200   # how long the button must stay LOW to count as a press;
                 # filters out brush/glitch events that would otherwise
                 # reset the unit on stray noise


async def run(pin_num):
    """Watch the button on `pin_num`. Runs forever; cancel the task to stop."""
    btn = Pin(pin_num, Pin.IN, Pin.PULL_UP)
    log.info(f"[RESET_BTN] Watching GP{pin_num} for soft-reset (hold ≥ {_HOLD_MS} ms)")
    held = 0
    while True:
        if btn.value() == 0:
            held += _POLL_MS
            if held >= _HOLD_MS:
                log.warn(f"[RESET_BTN] Held {held} ms — issuing machine.soft_reset()")
                # Brief sleep so the warn line propagates over the event
                # bus / USB before we tear the VM down.
                await asyncio.sleep_ms(100)
                machine.soft_reset()
                return        # unreachable
        else:
            held = 0
        await asyncio.sleep_ms(_POLL_MS)
