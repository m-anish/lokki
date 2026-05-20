"""Physical reset-button watcher.

Two gestures on the same GPIO:

  short press  (≥ 200 ms LOW, then released before 5 s):
                machine.soft_reset()
                Equivalent to Thonny's Stop/Restart Backend. Used for
                debugging — quick way to retry a flaky cold-boot LoRa
                init without unplugging power.

  long press   (held LOW for ≥ 5 s):
                factory_reset_unclaimed()  →  machine.reset()
                Overwrites /config.json with the "unclaimed leaf" stub
                (unit_id = 99) while preserving the LoRa channel/crypt
                so the leaf can still talk to the rest of the fleet.
                The leaf comes back online as 99 and appears on the
                dashboard as "New device — set me up".

Hold-time visual feedback (status LED states, drives operator clarity
during the irreversible-ish action):

      0 –  200 ms     no change (debounce)
    200 – 2000 ms     reset_armed   amber solid  — release for soft_reset
   2000 – 5000 ms     reset_warning red blink    — keep holding to commit
       5000 ms+       factory reset commits immediately

Refuses long-press on the coordinator: factory-resetting the coord
would orphan the fleet, no one wants that pressed by accident.
"""

import asyncio
import machine
from machine import Pin
from core.config_manager import config_manager
from hardware.status_led import status_led
from shared.simple_logger import Logger

log = Logger()

_POLL_MS = 50

_DEBOUNCE_MS    = 200    # below this: ignored
_ARMED_MS       = 200    # above this: LED shows amber
_WARNING_MS     = 2000   # above this: LED shows red-blink warning
_FACTORY_MS     = 5000   # at this point: commit factory reset (don't wait for release)


async def run(pin_num):
    """Watch the button on `pin_num`. Runs forever; cancel the task to stop."""
    btn = Pin(pin_num, Pin.IN, Pin.PULL_UP)
    role = config_manager.role
    log.info(f"[RESET_BTN] Watching GP{pin_num} — short press: soft_reset, "
             f"long press (≥{_FACTORY_MS} ms): factory reset to unclaimed")

    held = 0
    state = "idle"        # "idle" | "armed" | "warning" | "committing"
    prev_led_state = None

    while True:
        if btn.value() == 0:
            held += _POLL_MS

            # Cross the debounce threshold → arm.
            if state == "idle" and held >= _DEBOUNCE_MS:
                state = "armed"
                prev_led_state = status_led.state_name
                # Take exclusive ownership of the LED for the
                # duration of the hold. Without the lock,
                # leaf_status_task (fires every 2 s) or
                # fleet_timeout_task (every 10 s) or an HB flash
                # would override our amber/red-blink sequence and
                # the operator wouldn't see clean hold-time feedback.
                status_led.lock(True)
                status_led.set_state("reset_armed", force=True)
                log.debug(f"[RESET_BTN] Armed at {held} ms — release for soft_reset")

            # Cross the warning threshold → escalate visual.
            if state == "armed" and held >= _WARNING_MS:
                state = "warning"
                status_led.set_state("reset_warning", force=True)
                log.warn(f"[RESET_BTN] Held {held} ms — keep holding to factory-reset")

            # Cross the factory-reset threshold → commit immediately,
            # without waiting for release. The operator has held this
            # long enough to mean it. Coord units refuse: a factory
            # reset on the coord would orphan the whole fleet.
            if state == "warning" and held >= _FACTORY_MS:
                state = "committing"
                if role == "coordinator":
                    log.error("[RESET_BTN] Long-press detected on coordinator — refusing "
                              "(factory-reset would orphan the fleet)")
                    status_led.lock(False)
                    if prev_led_state:
                        status_led.set_state(prev_led_state)
                    # Treat as no-op; wait for release before allowing next gesture.
                else:
                    log.warn("[RESET_BTN] Committing factory reset to unclaimed-leaf state")
                    try:
                        config_manager.factory_reset_unclaimed()
                    except Exception as e:
                        log.error(f"[RESET_BTN] factory_reset_unclaimed failed: {e}")
                        status_led.lock(False)
                        if prev_led_state:
                            status_led.set_state(prev_led_state)
                        # Give up — wait for release.
                    else:
                        await asyncio.sleep_ms(100)   # let log line propagate
                        machine.reset()               # does not return
                        return
        else:
            # Released. Decide what to do based on how long it was held.
            if state == "armed":
                # Short press — release in the soft_reset window.
                log.warn(f"[RESET_BTN] Released after {held} ms — issuing soft_reset")
                status_led.lock(False)
                if prev_led_state:
                    status_led.set_state(prev_led_state)
                await asyncio.sleep_ms(100)
                machine.soft_reset()
                return
            elif state == "warning":
                # Released between 2 s and 5 s — operator backed out
                # before the factory-reset commit. We still soft_reset
                # because they clearly wanted *some* kind of restart,
                # and it's the safer of the two.
                log.warn(f"[RESET_BTN] Released after {held} ms in warning state — "
                         "soft_reset (factory-reset aborted before 5 s)")
                status_led.lock(False)
                if prev_led_state:
                    status_led.set_state(prev_led_state)
                await asyncio.sleep_ms(100)
                machine.soft_reset()
                return
            elif state == "committing":
                # Coord refusal path — restore LED, reset counters.
                status_led.lock(False)
                if prev_led_state:
                    status_led.set_state(prev_led_state)

            # Reset state for the next gesture.
            held = 0
            state = "idle"
            prev_led_state = None

        await asyncio.sleep_ms(_POLL_MS)
