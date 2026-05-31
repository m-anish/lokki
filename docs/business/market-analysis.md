# Lokki — Market Analysis, Costing & Pricing

> **Status** · internal business analysis (not engineering documentation)
> **Date** · 2026-05-31
> **FX** · USD converted at **₹95 / $1**
> **Basis** · Rev0.1 schematic + PCB, LCSC / India bulk component pricing, and a
> deep-and-wide competitor sweep (India-first, global context).
>
> All figures are back-of-envelope estimates for **positioning**, not a
> manufacturing quote. Refresh before any external quote or launch.

---

## Contents

1. [Price list (the answer)](#1-price-list-the-answer)
2. [What the hardware is](#2-what-the-hardware-is)
3. [Material cost estimates](#3-material-cost-estimates)
4. [Pricing rationale](#4-pricing-rationale)
5. [Competitor landscape](#5-competitor-landscape)
6. [Feature comparison](#6-feature-comparison)
7. [Honest weaknesses](#7-honest-weaknesses)
8. [Sources](#8-sources)

---

## 1. Price list (the answer)

| SKU | What it is | Material cost | Loaded cost | **Sell price** | Margin multiple |
|---|---|--:|--:|--:|:--:|
| **Box** | The controller (PCB + PSU + enclosure) | ~₹3,150 | ~₹5,000 | **₹12,900** | ~2.6× |
| **LED module** | 3 W downlight, CH812 series connectors | ~₹80 | ~₹120 | **₹349** | ~4.3× |
| **PIR module** | Motion sensor, weatherproof, on RJ11 cable | ~₹205 *(5 m)* | ~₹330 | **from ₹575** | ~1.7× |

**PIR is priced as a fixed head + per-metre cable** so the number scales honestly:

| Component | Sell price |
|---|--:|
| PIR head (sensor + carrier + jack + weatherproof case + gland) | ₹499 |
| Unshielded RJ11 cable (Fedus-class) | +₹19 / m |
| → starting price at 4 m | **₹575** |
| → typical 5 m run | ₹599 |
| → 6 m run | ₹619 |
| → optional true IP-rated sealed variant | +₹149 |

**Headline positioning** — *lowest cost per controlled light point, with zero
recurring fee, ever.* One Box dims **up to 56 light points** (8 channels × up to
7 LEDs); the nearest wireless incumbent (Casambi) costs **~₹2,375 per channel for
the node alone**.

---

## 2. What the hardware is

Read from the Rev0.1 EasyEDA schematic + PCB. Same PCB, same firmware on every
unit; role (coordinator vs leaf) is set in `config.json` at boot.

### 2.1 The Box (controller)

One 2-layer PCB (`Lokki Rev0.1`) + a certified PSU in a 3D-printed case.

| Block | Parts on board |
|---|---|
| Brain | 1× Raspberry Pi Pico 2 W (RP2350) |
| LED drive | 8× constant-current drivers (PT4115-class), each with LED+/LED− screw terminal |
| Switched loads | 2× SPDT relays + 2× IRLML2502 MOSFET drivers + 2× 1N4001 flyback diodes |
| Radio | 1× E220-900T22D LoRa (~868 MHz, UART) + antenna |
| Power | 1× LM2596 buck module (24 V → 5 V) + 220 µF bulk cap |
| Time | 1× DS3231 RTC module (battery-backed) |
| Sensing | 4× PIR inputs (RJ jacks, zener+RC protected) · 1× LDR divider · 1× I2C expansion (RJ) |
| Status / UI | 1× WS2812 status LED · 1× reset switch |
| I/O | DC-in terminal · 8× LED-out terminals · 2× relay terminals |

### 2.2 The shipped kit (a moat, not just a board)

Observed from the review-build photo, the Box ships as a finished, installer-ready
kit — meaningfully more than a bare PCB:

- Branded 3D-printed enclosure with a mains rocker switch, status indicator, and embossed "Lokki".
- **Wago-style lever / push-in quick connectors** on every lead — no soldering to install.
- A bundled **IEC mains lead**.
- **Install tooling** in the box (wire stripper, tape).

Incumbents ship a node and an integrator's invoice. Lokki ships a ready-to-mount
kit. That finish is part of why the Box carries a 2.6× markup.

---

## 3. Material cost estimates

> LCSC / India bulk pricing. USD parts at ₹95 / $1.

### 3.1 Box — bill of materials

| Item | Qty | Unit ₹ | ₹ |
|---|--:|--:|--:|
| Pico 2 W (RP2350) | 1 | 650 | 650 |
| LoRa E220-900T22D *(LCSC ~$4.19 → ₹400)* | 1 | 400 | 400 |
| LED driver module (PT4115-class) | 8 | 35 | 280 |
| DS3231 RTC module | 1 | 130 | 130 |
| LM2596 buck module | 1 | 50 | 50 |
| Relays (SPDT) | 2 | 35 | 70 |
| Screw terminals (DC / LED / relay) | ~11 | 6 | 66 |
| RJ jacks (4× PIR + 1× I2C) | 5 | 12 | 60 |
| PCB, bare (JLCPCB small batch) | 1 | 80 | 80 |
| MOSFETs, diodes, zeners, WS2812 | — | — | 25 |
| Caps, resistors, LDR, switch, headers | — | — | 60 |
| **PCBA subtotal** | | | **≈ 1,870** |
| Mean Well LRS-150-24 (genuine, India) | 1 | 900 | 900 |
| 3D-printed enclosure (filament ~200 g) | 1 | 250 | 250 |
| Antenna (SMA whip) + wiring + M3 fasteners | — | — | 120 |
| **Box material total** | | | **≈ ₹3,150** |

> **Material floor ≈ ₹3,150 → loaded build cost ≈ ₹5,000.** The gap is SMT
> assembly + stencil amortization, hand-soldering the through-hole/terminals,
> enclosure **print time** (not just filament), functional test, antenna/cabling,
> and the low-volume procurement premium. If the Wago connectors + tooling ship
> in **every** production unit, the floor rises ~₹300–500, making ₹5,000 slightly
> conservative. We use **₹5,000** as the costing basis.

### 3.2 LED module — bill of materials

A real **downlight**: a 3 W LED on an aluminium heatsink with lens/reflector,
CH812 quick connectors for series in/out, in a 3D-printed enclosure. Mounted
heatsink-up as a downlight — fins-up favours natural convection, so **no thermal
derating or larger heatsink needed**.

| Item | ₹ |
|---|--:|
| 3 W LED chip on aluminium MCPCB / heatsink | 18 |
| 2× CH812 quick connectors (in + out for series) | 30 |
| 3D-printed enclosure (filament ~25 g) | 30 |
| Wiring | 5 |
| **LED module material total** | **≈ ₹80–85** |

> Confirms the ₹80 figure. Because it's a finished downlight (not a bare
> star-PCB), ₹80 sits at the *top* of the plausible range — so ₹349 holds with
> headroom (₹399 defensible).

### 3.3 PIR module — bill of materials

An HC-SR501 on a small PCB carrier with an RJ11 jack, in a weatherproof
3D-printed case, on an **unshielded RJ11 cable** (Fedus-class — fine for
low-speed digital over short runs). Cost scales with **cable length**.

| Item | ₹ *(5 m base)* |
|---|--:|
| HC-SR501 sensor | 55 |
| RJ11 jack (6P) | 12 |
| PCB carrier (tiny 2-layer) | 20 |
| Unshielded RJ11 cable, 5 m @ ~₹10/m (Fedus-class) | 50 |
| Weatherproof 3D case (~30 g) | 35 |
| Gasket + cable gland / strain-relief | 18 |
| Fasteners + internal wiring | 15 |
| **PIR module material total (5 m)** | **≈ ₹205** |

> Loaded build cost (5 m) ≈ ₹330–350. Cable is near-pass-through in pricing
> (+₹19/m) so length never becomes a purchase objection — important for
> campus-scale path / pond / corridor runs.

---

## 4. Pricing rationale

### 4.1 Three anchors

1. **Cost floor (cost-plus)** — Box ₹5,000 · LED ₹80 · PIR ₹205 (5 m). Needs
   margin for R&D, low-volume procurement, and support.
2. **Value ceiling (what the job costs today)** — a comparable *zone* of dimmed,
   scheduled, motion-aware control costs **₹30,000 – ₹1,00,000+** via Casambi /
   Lutron / DALI / GRMS (nodes + drivers + hub + installer). Indian GRMS rooms
   run **₹7,000 – ₹54,000 / room**.
3. **The killer metric — cost per controlled light point.** One Box drives
   **8 channels × up to 7 LEDs = up to 56 points**. Casambi is **~₹2,375 /
   channel** for the node alone.

### 4.2 Per-SKU logic

| SKU | Markup | Why this markup |
|---|:--:|---|
| **Box** | ~2.6× | The brain; carries the value. ~₹1,600 / channel including PSU, sensors, relays, and dashboard — already under Casambi's per-channel node-only price. Headroom to ₹13,900 given the kit finish. |
| **LED module** | ~4.3× | Scalable consumable with high attach rate. A real downlight, not a star-PCB → ₹399 defensible. |
| **PIR module** | ~1.7× | An accessory, not a differentiator. Price for **attach**, not margin, so nobody skips motion sensing to save money. Margin lives in the head; cable is near pass-through. |

### 4.3 Posture

**Value-disruptor with a fair margin.** Price far under incumbents so the value
gap sells itself, at a markup that funds support and reflects the engineering —
not a DIY race-to-the-bottom. Over five years, no-subscription crushes
Interact / GRMS cloud TCO. Public message: **"lowest cost per controlled light
point, and zero recurring fee — ever."**

---

## 5. Competitor landscape

> Deep-and-wide sweep: India-first, then global incumbents, then the LoRa niche,
> then DIY. USD figures at ₹95 / $1.

### 5.1 Indian smart-lighting / GRMS (home turf — hospitality / wellness)

| Player | What they do | Notes |
|---|---|---|
| **BuildTrack** | DALI + scene keypads, app/voice, hotel automation | Wired / WiFi GRMS |
| **SmartNode** (Gujarat) | Home & hotel automation; curtains + lighting | WiFi, app/voice |
| **HDL / AUTOMAT / Navkar / Foxdomotics** | KNX-class GRMS integrators | Premium, installer-led |
| **Smart G4** | Packaged hotel kits | **$75–570 / room** ≈ ₹7,100 – ₹54,000 |
| **Wipro / Havells / Syska / Bajaj / Crompton / Signify** | Fixtures + some connected/cloud lighting | India lighting market ≈ **$5.12 B**, growing |

*Pattern: app/cloud-leaning, installer-commissioned, room/building-scale, closed.*

### 5.2 Global lighting-control incumbents

| System | Tech | Price signal |
|---|---|---|
| **Casambi** | Bluetooth mesh, no gateway | Short range (BT). CBU-PWM4 ≈ **$100 ≈ ₹9,500** (4 ch → ₹2,375/ch); CBU-ASD ≈ **$136 ≈ ₹12,900**. App/cloud commissioning. |
| **Lutron** (Caséta / Vive / Athena) | RF wireless, short range | **$54–200/device ≈ ₹5,100 – ₹19,000** + hub + installer; commercial tiers lean cloud/analytics. |
| **DALI** (IEC 62386, open wired) | Wired bus | Per-driver / per-fixture + a controller; robust but **wired**. |
| **KNX** | Whole-building wired | Projects **€25k+**; integrator-only. |
| **Crestron / Control4 / Signify Interact** | Premium / cloud | Integrator-led or **subscription** (Interact is cloud). |

### 5.3 LoRa lighting — the niche Lokki overlaps

| Product | What it is |
|---|---|
| **Milesight WS558** | LoRaWAN 8-circuit controller — **on/off + timed only**, gateway-oriented |
| **Gebosun LoRa-MESH / Fonda / inteliLIGHT / Tvilight** | LoRa(WAN) **street / municipal** lighting; mesh, local RTC, work offline |

*Pattern: LoRa lighting exists almost exclusively at **street-/city-scale**,
on/off-grade, gateway/concentrator-based.* **Nobody is doing venue-scale, dimmed,
sensor-driven, gateway-free LoRa coordination for wellness interiors** — Lokki's
white space.

### 5.4 DIY / prosumer local

**Shelly, Sonoff (Tasmota), Athom, Loxone, Home Assistant** — local, cheap,
no-sub, open-ish, but short-range WiFi/Zigbee, no built-in long-range
coordination, and *you* must be the integrator.

---

## 6. Feature comparison

✅ yes / strong · ⚠️ partial / depends · ❌ no

| Capability | **Lokki** | Casambi (BT mesh) | Lutron (wireless) | DALI / KNX (wired) | Indian GRMS | DIY (Shelly/HA) |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Fully local — no cloud needed | ✅ | ⚠️ | ⚠️ | ✅ | ❌ | ✅ |
| No subscription / recurring fee | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ |
| Long-range wireless (sub-GHz LoRa ~1 km) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| No paid hub/gateway required | ✅ | ✅ | ❌ | ❌ | ❌ | ⚠️ |
| Schedule survives network/coordinator loss | ✅ | ⚠️ | ⚠️ | ✅ | ❌ | ⚠️ |
| On-device sunrise/sunset (astronomical) timing | ✅ | ⚠️ | ✅ | ⚠️ | ⚠️ | ✅ |
| Dimmed channels per controller | ✅ **8** | ⚠️ 4 | ❌ 1–2 | ✅ addressable | ⚠️ | ⚠️ |
| Built-in motion + ambient light per controller | ✅ 4 PIR + LDR | ⚠️ sep. nodes | ⚠️ sep. sensors | ⚠️ | ✅ | ⚠️ |
| Relays for non-dim loads | ✅ 2 | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ |
| Open source / open hardware | ✅ GPL-3.0 | ❌ | ❌ | ⚠️ | ❌ | ✅ |
| Self-commission (no certified dealer) | ✅ | ⚠️ | ❌ | ❌ | ❌ | ✅ |
| No app to install (web dashboard) | ✅ | ❌ | ❌ | ⚠️ | ⚠️ | ⚠️ |
| Relative cost per controlled point | **₹ lowest** | ₹₹₹ | ₹₹₹₹ | ₹₹₹₹ | ₹₹₹ | ₹₹ |

**Takeaway** — Lokki is the only option that is *long-range, gateway-free, fully
local, open, and venue-scale* at once, and it's the cheapest per controlled light
point.

> The public site renders this matrix in the brand's restrained voice: no brand
> names (honest category buckets), and the site's own dot marks (● full · ◐
> partial · – none) instead of ✅/❌. See the "How it compares" section in
> `site/index.html`.

---

## 7. Honest weaknesses

Already reflected in the site's "What it can't do" section:

- **Indoor-only enclosure** — outdoor fixtures yes, outdoor electronics no.
- **Campus-scale max** — one coordinator + up to eight leaves (9 boxes). Not city-scale.
- **Augments daylight; doesn't replace it** — a windowless room stays windowless.
- **Lab-testing stage** — the board is not yet formally certified (the PSU is).

---

## 8. Sources

- [LCSC — E220-900T22D](https://lcsc.com/product-detail/C970287.html)
- [Casambi CBU-PWM4 (~$99.89)](https://www.aspectled.com/casambi-bluetooth-4-channel-pwm-controller)
- [Casambi CBU-ASD (~$136)](https://www.wired4signsusa.com/products/casambi-light-control-cbu-asd-ip65)
- [Lutron Caséta pricing](https://www.tomsguide.com/us/lutron-caseta-smart-lighting-dimmer-kit,review-4885.html)
- [Mean Well LRS-150-24 — robu.in](https://robu.in/product/mean-well-lrs-150-24-24v-6-5a-156w-smps/)
- [DS3231 / HC-SR501 / LM2596 — India retail (robu.in, robocraze, sunrom)](https://robu.in/)
- [Milesight WS558 — LoRaWAN 8-ch controller](https://www.milesight.com/iot/product/lorawan-sensor/ws558)
- [Gebosun LoRa-MESH street lighting](https://www.smartbrighten.com/test-t-hot-featured-product/)
- [BuildTrack — hotel automation](https://www.buildtrack.in/hotel-automation)
- [SmartNode — home/hotel automation](https://smartnode.in/)
- [India smart-lighting market — IMARC](https://www.imarcgroup.com/top-smart-lighting-companies-india)
- [Commercial lighting control comparison (2025) — XHLUX](https://www.xhlux.com/the-5-best-commercial-lighting-control-systems-2025/)

---

*Positioning estimates. Refresh before any external quote or launch.*
