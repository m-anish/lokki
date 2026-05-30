---
name: Test scenario failure
about: A scenario from tests/SCENARIOS.md didn't behave as expected on real hardware.
title: "[TEST] <scenario id> — <one-line summary>"
labels: bug, test-failure
---

<!--
Use this template when running through tests/SCENARIOS.md catches a real
deviation between expected and observed behaviour. For new bugs not yet
codified as scenarios, use a normal bug issue and add the scenario to
SCENARIOS.md as part of the fix.
-->

## Scenario
Link or quote: e.g. `tests/SCENARIOS.md` → **2.3 Mid-runtime WiFi drop → recovery**.

## Hardware
- Role: coord / leaf / both
- Unit IDs involved:
- PCB rev / hand-wired:
- DS3231 battery present? (yes / no / not applicable)
- LoRa module wiring sanity-check passed? (yes / no)

## Firmware
- Commit on `main` at flash time: `git rev-parse HEAD` →
- Flash command used: `./utils/update.sh ...`
- Any local uncommitted changes? (yes / no — if yes, paste `git diff --stat`)

## Setup state
What was the config when you started? Anything material that differs from
the scenario's stated Setup?

## Steps actually performed
1.
2.
3.

## Expected
Quote the scenario's **Expected** block.

## Observed
What actually happened. Be precise — LED colour + pattern, log lines
verbatim (paste in a code block), dashboard state. Screenshots welcome.

```
<paste REPL output / coord logs here>
```

## Reproducibility
- Reliable repro? (every time / sometimes / once)
- Resolves on its own after waiting? (yes / no / time:)
- Reboot recovers? (yes / no)

## Notes
Anything else — guesses at root cause, related commits, RF environment,
recent infra changes, etc. Optional.

<!--
Once filed, link this issue back from the scenario's `Last verified`
line in SCENARIOS.md (e.g. "verified-failing in #123 on 2026-05-30").
When the fix lands, update the line to "fixed in <commit>, regression
guard added."
-->
