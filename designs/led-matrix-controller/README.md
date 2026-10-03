# LED matrix controller — status

Design brief in `BRIEF.md`. Nothing laid out yet. Requirements are settled and
nothing under Open questions blocks the netlist any more — what remains there is
verification, converter-board dependencies and accepted trade-offs.

## What the board does

Negotiates USB PD, supplies a chained set of LED matrix modules over a
high-voltage bus, and runs an ESP32-C6 that drives the chain over Ethernet
(W5500) or USB. Brightness is capped in software to whatever PD actually
negotiated, so modules can be added freely within the power budget — the eFuse's
programmed ramp has no capacitance term, though the thermal regulation loop is
what actually bounds start-up — and the board divides the
available power between them.

## Environment

**The wall mounts on a soundwall for tekno. Sustained, high-amplitude vibration
is a given, not an edge case.** This gates part selection from here on rather
than being something to remember ad hoc.

Vibration attacks a connection two ways — the connector walking out of its
header, and the wire loosening in its termination. Screw terminals are
specifically bad at the second: screws back out, and industrial practice is
periodic re-torquing, which nobody is going to do on a soundwall.

It also puts anything with **mass on long leads** at risk of solder-joint
fatigue. Affected, and still to be revisited:

- the module's bulk electrolytic — a 220-330 uF radial cap is a lump of mass on
  two leads, and this is the classic vibration failure. Bond the body down, or
  move to SMD polymer or an MLCC bank
- converter board inductors — heavy and tall
- **board mounting**: standoffs at several points, not two corners. An
  unsupported span has a resonance and parts in the middle see amplified
  acceleration
- **cable strain relief**: a tie-down near each connector so cable movement does
  not load the contacts. Cheap, and it matters more than the connector choice

## Settled decisions

| Decision | Choice | Why |
|---|---|---|
| Bus voltage | **36 V** | highest EPR PDO the modules survive; XL1509 is 40 V operating / 45 V absolute |
| Power budget | **180 W** (36 V x 5 A) | EPR fixed PDO; matches the original brief |
| PD controller | **HUSB238A-BB001-QN16R** (C24833806) | I2C variant |
| PD mode | **I2C**, ADDR to GND = 0x42 | GPIO mode caps at 28 V/3.25 A = 91 W |
| PD front end | **p.16 Figure 6 topology** | 36 V exceeds the 33 V VBUS absolute max, so the chip sits behind an emitter follower. It carries **no power path**: VBUS feeds the bus directly and the channels switch downstream |
| Channel switching | **TPS16630** (C1849461) x2 | 60 V / 6 A eFuse with an integrated FET. Works from **4.5 V**, so the LED output follows the rails down instead of dying below a 12 V contract |
| External part ratings | **60 V class throughout** | the channel eFuse is 67 V absolute, which the TVS reaches only at 27 A of surge; the 60 V TPS54360B and SS36 are reached at 16.5 A, which is what sets the real limit. Still no room for a future 48 V/240 W bus, which would need every module respun anyway |
| Channels | **2**, 10 modules each | fps depends only on modules per channel; 10/ch = 90 fps |
| Target scale | **20 modules**, 80% perceived brightness | what 180 W supports before brightness falls off; 10 per channel |
| UART bridge | **none** | ESP32-C6 has native USB Serial/JTAG, and PD runs on CC not D+/D- |
| MCU | ESP32-C6-WROOM-1-N8 (C5366877) | from brief |
| Ethernet | **W5500 module** (W5500 Lite / WIZ850io class), soldered down | the brief said W5500; this design first read that as the bare chip. A finished module carries the PHY front end, the crystal and the magnetics, which retires two blocking open questions and the hardest routing on the board |
| Current sense | **INA226** (C49851), **low-side** | specified 0-36 V operating (40 V absolute), so on a 36 V bus there is no operating margin at all; the shunt goes in the ground return where common mode is ~0 V |
| Module connector | **Wago picoMAX 3.5** (2091 series), 4-pole: **+36V / LED_RTN / A / B** | 10 A, push-in spring, integrated locking latch. Spring force does not relax like a screw; the latch stops it walking out. Reichelt 2091-1424 board side, 2091-1104 cable side; not LCSC |
| Data link | **differential pair**, MAX3485 (C6395158) x2 | one per channel. Removes the single-ended distance limit and the level shifting at this end |
| Status-LED level shift | **74AHCT541** (C84548) | the SK6812 chain needs 5 V logic off a 3.3 V GPIO. Octal and only one channel used — oversized, see KB |

### Architecture

Split into power and control. One diagram carrying both always crosses itself,
because the MCU touches everything on the board.

**Power path** — left to right, the two channels parallel:

```mermaid
flowchart LR
    USBC["USB-C<br/>receptacle"]

    subgraph FE["PD front end"]
        direction TB
        RZ["MMBT5551 follower<br/>+ BZT52C27"]
        PD["HUSB238A<br/>PD sink, I2C mode"]
    end

    BUS(["36 V bus"])

    subgraph CH["output channels"]
        direction TB
        EF1["ch1 eFuse<br/>+ ramp"] --> CN1["picoMAX 4-pole<br/>+36V / LED_RTN / A / B"] --> M1["10 modules"]
        EF2["ch2 eFuse<br/>+ ramp"] --> CN2["picoMAX 4-pole<br/>+36V / LED_RTN / A / B"] --> M2["10 modules"]
    end

    RAILS["TPS54360B -> 5 V<br/>SY8089 -> 3.3 V"]
    SHUNT["low-side shunt"]
    GND(["GND"])

    USBC -->|"CC1 / CC2"| PD
    USBC -->|"VBUS 5-36 V"| RZ --> PD
    USBC --> RAILS
    USBC ==> BUS
    BUS ==> EF1
    BUS ==> EF2
    M1 -->|return| SHUNT
    M2 -->|return| SHUNT
    SHUNT --> GND
```

Thick edges carry the bus current; thin edges are control and sense. The shunt
sits in the **return** path, which is what makes low-side sensing work.

**Control and data:**

```mermaid
flowchart LR
    MCU["ESP32-C6-WROOM-1-N8"]

    PD["HUSB238A<br/>PD status + control"]
    INA["INA226<br/>low-side current"]
    ETH["W5500 module<br/>10/100 Ethernet"]
    USBD["USB-C D+/D-<br/>native USB Serial/JTAG"]

    subgraph OUTS["outputs"]
        direction TB
        RS485["2x MAX3485<br/>differential, 3.3 V"] -->|"A/B"| CN1["ch1 connector"]
        RS485 -->|"A/B"| CN2["ch2 connector"]
        BUF["HCT buffer<br/>3.3 V -> 5 V"]
        BUF -->|status| LEDS["8x SK6812-EC20"]
    end

    EF1["ch1 eFuse"]
    EF2["ch2 eFuse"]

    MCU <-->|I2C| PD
    MCU <-->|I2C| INA
    MCU <-->|SPI| ETH
    MCU <-->|"D+/D-"| USBD
    MCU -->|"PARLIO, 2 lanes"| RS485
    MCU --> BUF
    MCU -->|enable| EF1
    MCU -->|enable| EF2
```

Both I2C devices share one bus. The buffer drives only the status chain — the
data outputs are differential and need no level shifting here — so one channel
of eight is used.

### Why not 240 W

48 V is supported by the PD chip (p.16 Figure 6) but **destroys the modules** - the XL1509
is 40 V operating, 45 V absolute. 240 W would need every module respun onto a 60 V-class
buck. 48 V would want 100 V throughout, and **nothing on this board is 100 V where it
would have to be**. The bus-side parts are **60 V class** — the eFuse at 60 V with
a 67 V absolute, the TPS54360B, the SS36 — and that is what sets the limit. The only 100 V semiconductors fitted are D15/D16 on the *switched
outputs*, which is not the node a 48 V contract would stress. The 100 V figure in
the original argument belonged to the discrete NSS085N100S that the eFuse
replaced. The argument is stronger for it, not weaker. Since 48 V needs every module respun
anyway, the door is closed by the modules, not by this board.

### Why not 28 V

At 14 modules, 140 W and 180 W are nearly indistinguishable (83% vs 85% perceived) —
the 180 W case is thermally capped there, the 140 W case just barely power-limited at 67%. At 20 modules the gap is real: 71% vs 80%. 20 modules is the target,
so the extra front-end complexity is worth it.

## Power budget

180 W, less **5 W** for the controller, leaves **175 W** for modules. 5 W is the
figure every calculation and `figures.py` actually use; the thermal budget's
4.02 W is the computed total and the 1.0 W difference is deliberate headroom.
Perceived brightness applies a gamma of 2.2.

| Modules | Power | Perceived | fps (2 ch) | Binds on |
|---|---|---|---|---|
| 10 | 70% | 85% | 176 | thermal |
| 14 | 70% | 85% | 128 | thermal |
| 20 | 61% | 80% | 90 | power |
| 24 | 51% | 73% | 76 | power |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="figures/brightness-vs-modules-dark.svg">
  <img alt="Sustained brightness vs module count" src="figures/brightness-vs-modules.svg">
</picture>

Thermal and power cross over near 17 modules. Below that the modules are heat-limited
however much is negotiated; above it, software divides what is available.

## Cabling

**Terminology:** the link is called a **differential pair** throughout. The
drivers are RS-485 transceivers and follow RS-485 line levels, but none of the
RS-485 *connector* conventions apply — the pair runs over two poles of a picoMAX
alongside power and ground.

The converter is a **separate board from the LED matrix**: controller → wire →
per-module converter board on the back of the wall → LED matrix board. The
modules sit adjacent and chain board-to-board like a snake, so **only the
controller-to-first-module cable has any length.** Everything after it is a
short board-to-board link whose drop is negligible.

That single cable carries the whole channel: 2.43 A sustained when both are lit,
2.80 A when one is dark and the other is at the cap, 4.0 A on a
full-white flash.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="figures/cable-drop-vs-hop-length-dark.svg">
  <img alt="Drop in the controller-to-first-module cable" src="figures/cable-drop-vs-hop-length.svg">
</picture>

Cross-sections per **IEC 60228**, loop resistance 2ρ/A with ρ(Cu) = 0.0172
Ω·mm²/m at 20 °C:

Worked at **2.80 A**, which is the real per-channel worst case — one channel at
the 70% thermal cap while the other is dark, since PD caps the total and not the
split. The balanced 2.43 A would be 16% kinder and is not what sizes a cable.

| mm² | 5 m | 10 m | 15 m | max at 5% |
|---|---|---|---|---|
| 0.25 | 1.93 V | 3.85 V | 5.78 V | 4.7 m |
| 0.34 | 1.42 V | 2.83 V | 4.25 V | 6.4 m |
| **0.50** | **0.96 V** | **1.93 V** | 2.89 V | **9.3 m** |
| 0.75 | 0.64 V | 1.28 V | 1.93 V | 14.0 m |
| 1.00 | 0.48 V | 0.96 V | 1.44 V | 18.7 m |

The 5% is an efficiency choice rather than a functional limit — the converter
needs only 6.5 V in — so past it the recommendation changes and the feasibility
does not: **0.5 mm² up to 9 m, 0.75 mm² beyond**.

**Power is not the constraint.** The converter needs only 6.5 V in (5 V out plus
1.5 V dropout), so from 36 V there is 29.5 V of headroom — the cable would have
to be absurd before the converter stopped regulating. The 5% line above is an
efficiency choice, not a functional limit. At 0.5 mm² and 10 m the cable burns
**4.06 W** per channel at the balanced 2.43 A, and **5.39 W** at the 2.80 A worst case. That is the real cost.

**Data is no longer the limit.** Going differential removed it. A differential
pair is good for tens of metres at 800 kbps, well past anything power
allows, and it is far more tolerant of a room full of switching converters than
a single-ended line would have been.

So **power is now the only constraint: 0.5 mm², up to about 9 m** at 5% drop — 10.8 m on the balanced 2.43 A, 9.3 m at the 2.80 A one channel actually draws with the other dark.
That is a real gain over the ~5 m the single-ended link would have been held to.

**The design's reference run is 10 m on 0.5 mm².** The 11 m that the ESD and
edge-rate sections work at is deliberately the *longer* case, and therefore the
conservative one for EMC. It is not a limit: 0.75 mm² reaches 14.0 m.

Practice for the pair:

- **Twist A and B together.** The connector pinout is +36V / LED_RTN / A / B so the
  pair is adjacent and away from the power conductor.
- **120 Ω termination at the far end**, on the first converter board.
- Keep the pair away from the +36 V conductor and its converter switching noise.

Worth noting this is a dividend of the 36 V decision. At a 5 V bus a 1.6 V drop
would be 32% of the supply; at 36 V it is 4.5%.

## Status indication

Eight SK6812-EC20 in a chain on ONE GPIO, level-shifted through a 74AHCT541.

**Why all eight are RGB when only two need colour for their own job.** The
obvious saving is two addressable LEDs for the fault indicators plus six plain
ones for the bar, and it does not pay:

| | power | GPIOs | parts | cost |
|---|---|---|---|---|
| **8 addressable + 74AHCT541 (chosen)** | **0.120 W** | 1 | **19-20** | $1.11 |
| 8 plain 0805 + 74HC595 + 8 resistors | 0.053 W | 1 | 18 | **$0.15** |
| 2 addressable + 6 plain, driven directly | 0.070 W | 7 — none spare | 14 | — |

**GPIO count is a tie, not a win.** A 74HC595 shares SCLK and MOSI with the Ethernet module and needs only
a latch pin — exactly what the addressable chain costs — and it would replace the
74AHCT541, which exists only because SK6812 wants 5 V logic. The plain option is
genuinely buildable: YLED0805R (C19171391) at 130k stock and 0.8 ¢.

**Part count is a wash, counting each option's own support parts.** The real
counts are **19 or 20 against 18**: eight SK6812s, the 74AHCT541, eight
decoupling capacitors the datasheet calls essential, two series resistors and
the buffer's own decoupling, against eight LEDs, a 74HC595, eight
current-limiting resistors and one decoupling — a marginal edge to the shift
register. **So the decision rests entirely on colour**, and on a small run that
outweighs 96 ¢. **And colour has become load-bearing.** The bar
reads watts across 180 W, so on a 36 W contract one LED lights whether the wall
is dark or the supply is small; without colour there is no way to tell those
apart. That did not matter while 36 V was the only normal case — since the UVLO
change, 12, 20, 28 and 36 V all are.

Power is not the deciding factor: 67 mW is 1.7% of a budget sitting at 1.19×.

**A real simplification does exist and is not taken:** three addressable LEDs
showing two colours each would encode the same six power levels, so five parts
instead of eight at 0.075 W. It is rejected on readability — a bar is read at a
glance and a colour-coded bar is read twice.

- **6 as a power bar** - watts, not volts, at 30 W per LED across the 180 W budget.
  Watts is what the limiter reasons about and it reads at a glance. **Colour
  carries the negotiated voltage** — a requirement rather than an option since the
  UVLO change made 12/20/28/36 V all normal, and the reason the bar is RGB. (Actual PD PDOs are 5/9/12/15/20/28/36/48, but 48 V is
  never requested because it destroys modules, so a voltage bar would need 7.)
- **1 for power limiting**, **1 for thermal limiting**. RGB encodes history on a single
  LED rather than one per time window: bright red = limiting now, orange = within
  5 min, dim yellow = within 10 min, off = clean for 10+ min.

Selected: **SK6812-EC20** (C2909058), 2 × 2 mm top-view.

**The side-emitting alternative was evaluated and is the better part once the
enclosure is known.** SK6812D-EC3210R (C2890041) shines out the board edge, which
saves cutting a window in whatever face the operator looks at. But the enclosure
is not designed, so neither constraint can be weighed: a top-view part can sit
anywhere on the board and needs a window; a side-view part needs an unobstructed
in-plane path and gives edge visibility without one. Which is cheaper depends on
how the board sits in the box.

**While that is open, the part that constrains nothing is the right default.**

| | SK6812-EC20 (chosen) | SK6812D-EC3210R | SK6812MINI-E |
|---|---|---|---|
| area | 4.0 mm² | 3.52 mm² | 12.25 mm² |
| emission | top | side | top |
| placement | anywhere | needs line of sight | anywhere |
| price for eight | $0.89 | $0.90 | $0.65 |
| stock | 35k | 52k | 187k |

Electrically they are interchangeable: IDOUT 12 mA typ on all three, same supply
range, and all three need the level shifter (below).

**All three have different pin orders, and no two agree:**

| | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| SK6812MINI-E | GND | DIN | VDD | DOUT |
| SK6812D-EC3210R | GND | DOUT | DIN | VDD |
| **SK6812-EC20** | **VDD** | DOUT | GND | DIN |

A netlist written for any one of them puts VDD on a data pin of either other.
This is the clearest illustration in the project of why pin numbers are read and
never assumed.

**The level shifter stays whichever is chosen.** Every SK6812 in this family wants
VIH ≥ 0.6-0.65 × VDD, i.e. 3.0-3.25 V on a 5 V rail, and the ESP32-C6 guarantees
only V_OH ≥ 0.8 × VDD = **2.64 V**. The EC20's 0.6 × VDD is the most permissive of
the three and still out of reach. The 74AHCT541 is not a part any LED choice can
delete.

**The thermal indicator is a model, not a measurement.** There is no temperature sensor
on the modules - the controller infers thermal state from commanded brightness
integrated over time against the RthJA figures in the knowledge base. Real thermal data
would need a sensor on the module, i.e. a module revision.

## Software power limiting

The use case is flashing effects, not sustained brightness, which changes what
the limiter has to do.

**Predictive limiting is primary.** Compute a frame's draw before displaying it
and scale the frame if it would breach the negotiated current. Reacting to a
measurement is too late: a sudden all-white flash steps the bus by several amps,
and if that exceeds what PD negotiated the source can cut VBUS entirely, blacking
out the whole installation.

The model needs three terms:

    I_bus  =  (sum(channel values) x 20 mA / 255) x 5 V / (36 V x eta)   [LED term]
           +  N_ICs x 0.6 mA x 5 V / (36 V x eta)                        [quiescent floor]
           +  controller overhead

- **The sum is amperes at the module's 5 V rail**, and the contract is amperes at
  the **negotiated** bus voltage. The divisor is `V_negotiated × η / 5` — **5.4**
  at 36 V and 75% efficiency, but **3.0** at 20 V and **1.8** at 12 V. Hard-wiring
  36 V here would under-predict bus current by **3×** on a 12 V contract, which is
  the least forgiving one the board accepts. Dividing by η *raises* the bus current, so it belongs in the
  numerator of that ratio, not the denominator. Writing 9.6 (= 7.2/0.75) instead
  **under-predicts bus current by 44%**, in the one computation whose whole
  purpose is to stop the source cutting VBUS.
  Sanity check against this document's own numbers: 20 modules at full white is
  43.2 A at 5 V, and 43.2/5.4 = **8.0 A**, which is exactly the 2 × 4.0 A per
  channel stated in Cabling.
- **An all-black frame does not draw zero.** The WS2812D-F8 specifies no
  quiescent current; the sibling SK6812 gives 0.6 mA per IC and the family runs
  0.5-1 mA. At 720 ICs that is 0.36-0.72 A at 5 V, i.e. **2.4-4.8 W off the
  bus** — 1.3-2.7% of a budget this document already computes at 5.2 A against a
  5.0 A contract. The floor is not optional headroom; it is always present.
- η is the same optimistic 75% the whole power budget rests on, characterised at
  28 V rather than 36 V. The limiter inherits that error and should carry a
  margin for it rather than trusting the figure.

**The step is slower than "microseconds".** The modules' own converters and their
3.3 mF of bulk slew a full-white transition over roughly 100 µs, not
instantaneously — which is what makes a predictive limiter viable at all, and is
worth stating because the earlier wording argued the opposite way.

**And "61% brightness" is not 61% of the current, continuously.** The WS2812
dims by duty-cycling its constant-current sinks at roughly **400 Hz**, so at 61%
a channel draws its *full-white* 4.0 A for 61% of each 2.5 ms period and nothing
for the rest. What reaches the bus is set by a current divider, and it is a **phasor** divider
— adding the resistance to the reactance as scalars gives the wrong
fraction. The channel's 3.3 mF bank is **0.121 Ω** at
400 Hz; the path is cable, switch and sense resistance:

    bus share = |Z_C| / |R + Z_C| = 0.121 / sqrt(R² + 0.121²)

| cable | R (loop) | bus share | ripple seen |
|---|---|---|---|
| ~5 m, 0.5 mm² | 0.35 Ω | 32.7% | **+0.51 / −0.79 A** |
| 10 m, 0.5 mm² | 0.69 Ω | 17.3% | **+0.27 / −0.42 A** |

The two shares do not sum to one with the capacitor's — they are orthogonal
components, which is exactly what the scalar version got wrong. **The document's
own worked case is 10 m**, so +0.27 A is the figure that matches the rest of the
design; −0.79 A applies to a short cable and is the conservative end.

Every current figure in this document is a frame average, which is the right
basis for the thermal and contract budgets and the wrong one for the INA226's
alert threshold and for anything about PD source behaviour. **Whether the ripple
adds or averages across a chain depends on whether the modules' PWM phases stay
correlated** — they re-latch every frame, so they plausibly do — and nothing here
establishes which. Treat +0.27 / −0.42 A as the working figure at the 10 m reference run, and +0.5 / −0.8 A as the short-cable bound and the correlation as
unverified.

**The INA226 is the slow loop**: calibrating the model, noticing the module
count changed, and acting as a safety net. Not the first line of defence.

**Inrush is handled in hardware, not software** — each channel switch ramps into
its own module bank under power limit rather than switching into it. See
Channel switching and inrush.

## Cold-start sequence

**The rails are fed from VBUS directly.** There is no master pass FET — see
Channel switching and inrush. Wiring the rails behind any switch creates a circular deadlock that
would stop the board ever powering up:

> EN_N is pulled up internally and the HUSB238A is enabled by pulling it to GND
> (datasheet p.5). If EN_N sits on an MCU GPIO, that GPIO is high-Z until the MCU
> is powered. So the chip stays disabled, GATE never closes, the bus never comes
> up, the rails never come up, and the MCU never boots to pull EN_N low.

Feeding the rails pre-FET breaks it, and turns EN_N's internal pull-up from a
trap into the correct safe default — nothing negotiates until firmware is alive.
**That default rests on one unverified thing**, and it is the only assumption on
this board that could stop it booting at all: whether the chip still presents Rd
while powered-but-disabled. See *Assumptions*; if it does not, the fix is a
pull-down on `PD_EN_N` that enables the chip before the MCU, and the sequence
below changes at step 3.

1. Plug in. VBUS is at vSafe5V (5 V), both channel switches are off, no LED power.
2. The TPS54360B runs straight off that 5 V — this is exactly why ~100% duty
   pass-through was a selection criterion — giving **4.81 V**, and the SY8089 makes
   3.3 V from it.
3. The MCU boots. EN_N is still high (internal pull-up), so the PD chip is idle.
4. Firmware pulls EN_N low and the HUSB238A negotiates. **It is powered from
   VDD**, which step 2 already brought up. It cannot self-power from VBUS the way
   the datasheet's standalone application implies: behind the Figure 6 follower the VBUS pin
   sees only 3.91-4.11 V at vSafe5V, under the 4.5 V the chip needs when VDD is
   absent. D14 holds the pin at ~4.19 V and VDD does the supplying.
5. VBUS rises to 36 V. The rails ride through it; the TPS54360B is a 60 V part
   and simply leaves pass-through.
6. Past ~4.5 V each TPS16630 is above its own operating minimum, and firmware
   enables the channels by driving the enable GPIOs **high**. Both may be
   released together: the dV/dt-controlled inrush is 0.72 A per channel, so
   1.44 A against a 5.0 A contract.
7. Each eFuse turns on after **11.6 ms** (742 µs + 49.5 × C_dVdT in nF) and ramps
   its own 3.3 mF at a dV/dt-controlled **0.72 A**, taking **165 ms** as
   programmed — in practice the thermal regulation loop stretches it, see below.
   The bus is already static, so they ramp into a steady source. The multi-second insertion
   delay the LM5069 imposed is gone with it.

There is no master pass FET — see Channel switching and inrush. A PD
fault sheds the LED channels while the controller stays alive to report it, which
is the behaviour we want.

## Rail architecture

The board must run from a 5 V bus as well as 36 V, with software holding back
what it cannot power. That constrains the rails more than the 36 V case does.

**The controller follows them all the way; the modules do not.** The TPS16630
operates from **4.5 V**, so there is no PD level at which the board fails to power
up and switch its channels on — including a plain laptop port at vSafe5V. Earlier
revisions set this threshold at 28 V and then 8 V, each of which made that true of
the rails and false of the load.

What does *not* follow all the way down is the light. Each module regulates its
own 5 V with an **XL1509**, whose datasheet p.1 states a **minimum dropout of
1.5 V** and p.7 a saturation voltage of **1.2 V typ / 1.4 V max at 2 A**. The
module therefore needs about **6.5 V in** to hold 5 V out at load — which no 5 V
bus can give it. Below that the XL1509 sits at 100% duty as a pass-through and
the module rail follows the bus down, minus whatever the switch drops at the
current drawn. See Recovery and debug for what that leaves.

```mermaid
flowchart LR
    BUS["36 V bus<br/>(5-36 V)"] --> B1["TPS54360B<br/>C524806 · 4.5-60 V in"]
    B1 --> RAIL5["5 V rail"]
    RAIL5 --> B2["SY8089<br/>C479074"]
    B2 --> RAIL3["3.3 V rail"]
    RAIL5 --> LS["74AHCT541<br/>level shifter"]
    RAIL5 -->|"D14"| PDV["HUSB238A VBUS pin"]
    RAIL3 --> MCU["ESP32-C6"]
    RAIL3 --> ETH["W5500 module"]
    RAIL3 --> SENSE["INA226"]
    RAIL3 --> PD["HUSB238A VDD"]
```

**TPS54360B** (4.5-60 V in, 3.5 A, 60k stock). Chosen specifically because its automatic
BOOT recharge circuit allows "duty cycles approaching 100%", so "the maximum output
voltage is near the minimum input supply voltage" (datasheet p.10). An ordinary buck
cannot make 5 V from a 5 V bus; this one passes through.

Output across the input range at 0.7 A, from the datasheet's p.12 Eq. 1, inverted:

    V_OUT = 0.99·V_IN + 0.99·V_F − 0.99·R_DS(on)·I_OUT − V_F − R_dc·I_OUT

with R_dc = **0.0725 Ω** — the SPM6530T-100M's DCR — and R_DS(on) = **0.12 Ω**, which is the datasheet's own
choice for this case (§8.2: *"the BOOT-SW = 3 V curve in Figure 1 was used for
RDS(on) = 0.12 Ω because the device operates with low drop out"*).

**That 0.12 Ω is a typical, and the maximum that goes with it is not published.**
The p.5 electrical table gives 92 mΩ typ / **190 mΩ max** — but explicitly at
`VIN = 12 V, **BOOT-SW = 6 V**`, which is not the condition §8.2 just told us to
use. Figure 1 plots both drives: the 3 V curve runs **1.25-1.30×** the 6 V curve
at every junction temperature. Scaling the 6 V maximum by that ratio puts the
applicable worst case at **≈0.24 Ω**, and there the rail is **4.48 V** — about
20 mV **under** the 74AHCT541's 4.5 V minimum, with no fan fitted. The rail
crosses that floor at 0.212 Ω, so the 6 V-drive 190 mΩ figure (4.515 V) is the
only reading that clears it, and it is the wrong reading.

**What that means, and what it does not.** `P1-undervoltage` fires on this
design. It is not a reason to respin: the 74AHCT541 drives nothing but the status
chain, so a marginal rail during the few seconds before negotiation means the
indicators misbehave and not that the board fails to start — the same conclusion
*Rail architecture* reaches below. What it does retire is the idea that fan size
is the discriminator: the no-fan case is already under the floor at this corner,
so §Cooling's 50 mA bound is about the *typical* part, not the worst one. And the
1.25-1.30× ratio is read off a curve, not published — see Assumptions.

At the applicable 0.24 Ω corner the rail is 4.48 V and D14 then holds the VBUS
pin at **4.11 V**, which takes `VBUS_OK` to **+0.11 V** on typ and −0.29 V on max,
and leaves the under-voltage detector at **+0.11 V on the adjacent F1 band,
0.19 V short on the pessimistic F0 reading** — see the residual table. At the
non-applicable 190 mΩ reading the rail is 4.515 V and the pin 4.15 V; the 4.56 V
row is the typical, not the floor.

| bus | 5 V rail | 74AHCT541 needs 4.5-5.5 V |
|---|---|---|
| 4.75 V (USB-C low tolerance) | **4.56 V** | 0.06 V over the floor |
| 5.00 V | 4.81 V | ok |
| >= 5.25 V | 5.00 V | ok |

The rail is **4.56 V** at the bottom of vSafe5V, and D14 holds the HUSB238A VBUS
pin at **4.19 V**. Both numbers are sensitive to the inductor: this is the
SPM6530T-100M's 72 mΩ, and a 163 mΩ part would put the rail at 4.50 V, exactly on
the 74AHCT541's floor.

**Neither is fatal, and here is why.** The 74AHCT541 only drives the status LED
chain; a marginal rail during the few seconds of the 5 V phase means the
indicators may misbehave before negotiation, not that the board fails to start.
And 4.19 V still clears the VBUS pin's 3.15 V minimum by 1.04 V. What it does
erode is the margin against two 4.0 V thresholds — see Known electrical limits.

**The sag at low bus voltage matters locally, not across the cable.** It is
tempting to call it self-correcting — the modules' own XL1509 is also in
pass-through at 5 V, so module VDD falls too and WS2812 thresholds track with it.
That argument is void here: the controller's 5 V rail and the modules' 5 V rail
no longer share a signal path, only a differential pair at 3.3 V logic.

What the sag actually threatens is the **SK6812 status chain on this board**:
its VIH is 0.6 x VDD, so at a 4.56 V rail the threshold is 2.74 V and the
74AHCT541 drives it comfortably. The binding constraint is the buffer's own
4.5 V supply minimum, which the rail clears by 0.06 V on a **typical** part and
misses by about 20 mV at the applicable worst-case on-resistance — see the
dropout table.

**SY8089** (C479074, SOT-23-5, 100k stock) for 3.3 V rather than an LDO. At 4.81 V in,
3.3 V out, and the ESP32-C6's **382 mA** worst-case transmit peak, an LDO would burn ~0.6 W; a
synchronous buck burns under 0.1 W and avoids a thermal problem in a small package.

### Why two conversion stages

The board needs **both** rails: 3.3 V for the MCU, Ethernet module, INA226 and the
HUSB238A's VDD, and 5 V for the 74AHCT541, the SK6812 status chain and **D14**.

**The reason is the status chain, not the modules.** The WS2812's 0.7 x VDD =
3.5 V threshold is the tempting number here and it does not apply: this
controller drives no WS2812. It drives two MAX3485s at 3.3 V, and the
3.3 -> 5 V shift lives on the converter board, which the module record states
outright. The governing number is the **status chain's 0.6 x 5.0 = 3.00 V** on the
controller's own rail, against the ESP32-C6's *guaranteed* V_OH of 0.8 x VDD =
**2.64 V**. The GPIO does not clear it at all — not by a thin margin, by 360 mV
the wrong way. That is what justifies the buffer, and it holds even before the
rail's own sag is counted.

**What the 3.3 V rail actually carries**, which until now was a bare "~2.3 W":

| Load | mA | Note |
|---|---|---|
| ESP32-C6, TX peak | 382 | datasheet Table 13, 802.11b at 20.5 dBm, 100% duty — pessimistic as a continuous figure |
| Ethernet module | 152 | 0.50 W at 3.3 V |
| 2x MAX3485 | ~28 | 2 mA I_CC each **plus ~12 mA each of line current** — a driver with DE asserted pulls load current 100% of the time |
| HUSB238A VDD | 4.5 | active sink |
| INA226 + pull-ups | ~6 | |
| **total** | **~573 mA = 1.89 W** | inside the ~2.3 W the rail is sized for |

The transceivers' line current is the row that was missing: it appears in the
thermal budget as 0.06 W of *board heat*, and the split is worth stating because
"most of it is burned in the far-end termination" is the natural guess and is
wrong. 2 × (2 + 12) mA × 3.3 V is 92 mW total and the far-end
terminations take 2 × (12 mA)² × 120 Ω = **35 mW**, so 38% leaves the board and
about 60% stays, which is the 0.06 W row. It is a real continuous draw on
the SY8089 either way. Note the datasheet gives neither a loaded I_CC nor a V_OD
at 120 Ω, so this row is estimated and cannot be verified from it.

Cascading costs almost nothing. Two figures are in play and they are different
things: the rail **draws** 1.89 W by the table above and is **sized** for 2.3 W.
The penalty scales with whichever is used, so both are given:

| | efficiency | lost at the 1.89 W draw | at the 2.3 W sizing ceiling |
|---|---|---|---|
| cascade, 36 -> 5 -> 3.3 V | ~78% | 0.533 W | 0.649 W |
| hypothetical single 36 -> 3.3 V | ~80% | 0.473 W | 0.575 W |
| **penalty** | | **60 mW** | **74 mW** |

**60 mW at the real draw**, against saving a second wide-input buck. Earlier
versions of this passage gave 60, 66 and 74 mW from the same two efficiencies
and did not say which load they were taken at — which is how the same page came
to carry "1.89 W" and "the rail delivers ~2.3 W" eight lines apart. The
alternatives are all worse:

- **Two parallel wide-input bucks** (bus to 5 V, bus to 3.3 V) needs two
  TPS54360-class parts and two inductors, for that same 74 mW.
- **One bus-to-3.3 V buck plus a charge pump to 5 V** works — the level shifter
  draws only tens of mA — but adds a part and puts switching noise next to the
  data lines.
- **Eliminating the 5 V rail is not available.** It reads like the one real
  simplification — run the modules at 4.5 V so a 3.3 V GPIO could drive their
  WS2812s directly — but that belongs to a design where the controller drives the
  modules' LEDs, which this one stopped doing when the link went differential. Three things on **this** board need 5 V and no module revision
  touches any of them: the 74AHCT541, the SK6812 status chain it buffers, and
  **D14**, which must sit above the HUSB238A's VBUS pin to hold it at 4.19 V.

So the two stages are forced by the 5 V logic requirement, and the cascade is
the cheapest way to meet it.

## Protection architecture

**The eFuse question is resolved, and by an eFuse.** The TPS26630 was dropped
early because its integrated FET is 40 V (1.11x at 36 V). Its sibling the
**TPS16630** carries a **60 V** FET at 67 V absolute, which is more margin than
the TPS54360B and SS36 on the same node have, so the original objection does not
apply to it. One per channel now does the current limiting the bus never had.

What protects the load, in order of speed:

1. **TPS16630 current limit**, 5.56 A typ per channel. Autonomous, but see Known
   electrical limits: against a 5.0 A *total* contract the PD source acts first
   on a hard short, so "one chain sheds, the other keeps running" is **not
   reliably obtainable**
2. **TPS16630 OVP**, a hardware overvoltage cut-off at 40.68 V typ, needing no firmware
3. the PD source limiting at the negotiated level
4. HUSB238A FAULT into the interlock transistors, pulling both SHDN pins low on
   OTP or an adapter-capability fault. OVP does not survive the clamp topology
   above 28 V; UVP does, offset by one V_BE — see the follower section
5. software via the INA226, as a slow safety net

**What is still unprotected is the bus node upstream of the two controllers.**
The rails and the PD front end sit there with only the TVS
and the PD source's own limiting between them and a fault.

**Current sensing is low-side.** The shunt sits in the ground return, so
common-mode is near 0 V and the INA226's rating — 0-36 V operating, 40 V absolute,
so no operating margin on a 36 V bus — stops mattering. Forced by both 85 V parts (INA228, INA238) being at zero stock;
it turned out to be the better answer. Costs the ability to detect an output
short to ground, and lifts the load ground by 25 mV at 5 A through 5 mOhm.

**The Figure 6 "Voltage Regulator" is a source follower** — a BSS138 in the vendor reference, an NPN emitter follower here, for the reason worked below. Hynetek
publishes it with values at [hynetek.com/2730.html](https://www.hynetek.com/2730.html)
— it is not in the datasheet. Gate pulled up through 10 kΩ and clamped by a Zener, source feeding the VBUS pin; a 0 Ω link bypasses it for
builds at 28 V or below.

**We use 27 V (BZT52C27), one bin below Hynetek's published 28 V, and the 20 V
part this design carried for four revisions would have broken it.** The argument
for 20 V was that
it puts the VBUS pin at ~18.5 V, "comfortably inside its 3.15-29.4 V window",
where a 28-30 V part would sit at ~28.5 V against a 29.4 V recommended maximum.

The window is not 3.15-29.4 V at 36 V. The undervoltage table on **p.7** has
*three* bands, and only the third applies here:

| symbol | condition | falling threshold |
|---|---|---|
| vVBUV_F0 | 26 V > RDO > 10 V | 86% of the requested voltage |
| vVBUV_F1 | 10 V ≥ RDO > 5 V | 80% of the requested voltage |
| **vVBUV_F2** | **RDO ≥ 26 V** | **22.4 V, absolute** |

At a 36 V contract the disconnect threshold is a flat **22.4 V** and the clamped
pin sat at 18.5 V — **3.9 V below it, permanently**. Per the datasheet's UVP
text the part then *"moves out the Attached.SNK state"*, so the sequence is
negotiate 36 V, false disconnect, source reverts to vSafe5V, repeat. The board
could never have held its own headline PDO. Every corner failed: BZT52C20 is
19-21 V and the BSS138's V_GS at 800 µA is 0.8-1.6 V, so the pin landed at
17.4-20.2 V against a threshold that does not move.

**BZT52C27 (C173421, 25.65-28.35 V)** fixes the clamp. But fixing the clamp only
fixed `vVBUV_F2`, and that was the second mistake: **the other two bands break
the same way, and the clamp has nothing to do with them.**

### The follower's offset is fixed; the threshold is a percentage

Below 25.65 V the Zener never conducts, so the clamp is irrelevant and the pin is
simply the bus minus the follower's own drop. The disconnect threshold, however,
is a *percentage of the requested voltage*. A fixed offset is a bigger fraction
of a smaller number, so the lower the contract, the worse it gets. The pass
condition in the 86% band is

    0.95·V − V_offset ≥ 0.86·V      ⇒      V_offset ≤ 0.09·V

— 0.95 because a compliant fixed PDO may sit 5% low, the figure this document
uses everywhere. With the BSS138's 1.6 V worst-case V_GS that needs **V ≥ 17.8 V**,
so 9 V, 12 V and 15 V all false-disconnect, and 20 V survives by 0.2 V. Those are
exactly the contracts the degradation story is built on.

**Q3 becomes an NPN emitter follower (MMBT5551, C7420357) and R44 drops to
3.24 kΩ.** A silicon base-emitter junction is 0.6-0.8 V where a BSS138 gate is
0.8-1.6 V, and that halving is the whole fix. The lower R44 keeps the base
current's own IR drop out of the margin:

| PDO | band | threshold | pin at a −5% bus | margin |
|---|---|---|---|---|
| 9 V | F1, 80% | 7.20 V | 7.71 V | +0.51 V |
| **12 V** | F0, 86% | 10.32 V | 10.56 V | **+0.24 V** |
| 15 V | F0, 86% | 12.90 V | 13.41 V | +0.51 V |
| 20 V | F0, 86% | 17.20 V | 18.16 V | +0.96 V |
| 28 V | F2, flat | 22.40 V | 24.85 V | +2.45 V |
| 36 V | F2, flat | 22.40 V | 24.85 V | +2.45 V |

Worked at V_BE = 0.80 V, h_FE = **60**, R44 = 3.24 kΩ (a 43 mV base drop) and the BZT52C27's low corner. The 60 is
deliberately below the datasheet's 80 min at I_C = 1.0 mA, which is the row that
brackets this operating point; at 80 the 12 V margin is 0.25 V rather than 0.24 V,
so every figure in the table is the pessimistic one. **12 V is the pinch point** — it sits just inside the 86% band, where the required offset is at
its smallest — and 0.24 V of margin on a 10.32 V threshold is 2.3%. Carried in
Known electrical limits rather than claimed as solved.

At the top end the swap helps as well: the pin lands at 24.85-27.75 V against the
22.4 V threshold and the 29.4 V recommended maximum, with more room at both ends
than the MOSFET gave, because V_BE is smaller than V_GS. Worked at the binding
corner — a +5% bus against the lowest emitter the spread allows, 25.65 − 0.80 =
24.85 V — V_CE is **12.95 V** in normal operation and **39.65 V** during a 64.5 V
TVS clamp, against the part's **160 V**
V_CEO — where the BSS138's 50 V would have been only 1.3× that event.

**What is not datasheet-backed here is V_BE.** The MMBT5551 datasheet specifies
V_CEO, V_EBO and h_FE and gives **no V_BE(on)** at any current. The 0.6-0.8 V
used above is a silicon junction at 800 µA over −20 to +85 °C, which is physics
rather than a figure anyone published, and the 12 V margin rests on it directly.
It is recorded as `inferred` in the part record and wants a measurement.

Two things the NPN costs. Base current is 13 µA at h_FE 60 and R44 has to supply
it, so R44's value lands directly on the 12 V margin — and the pin current it
scales with is published **only at VBUS = 5 V** (p.6: `VDD=3.3 V & VBUS=5 V`).
That makes the margin sensitive to a number the datasheet does not give at the
contract where it matters, so R44 is sized to be insensitive rather than optimal:

| R44 | at 800 µA | at 1.8 mA | at 4.0 mA | at **4.5 mA** |
|---|---|---|---|---|
| 4.7 kΩ | 0.217 V | 0.139 V | −0.033 V | **−0.073 V** |
| **3.24 kΩ** | **0.237 V** | **0.183 V** | **0.064 V** | **0.037 V** |
| 2.2 kΩ | 0.251 V | 0.214 V | 0.133 V | 0.115 V |

**4.5 mA is the hard upper bound**, not 4.0 — the other VBUS current row reads
4 typ / 4.5 **max** at 29.4 V with VDD unpowered, and this document's own rule is
to take the binding end of a spread. At 3.24 kΩ the 12 V margin stays positive
across that whole range; at 4.7 kΩ it is negative over half of it.

The cost is real and worth stating plainly: **100 mW** of board heat against
69 mW, and R44's own dissipation goes **up** from 17 mW to 25 mW at the nominal
36 V contract, where 9 V stands across it — 28% of its 62.5 mW rating to 40%. At the corner this document works
everywhere else, a +5% bus against the Zener's low bin, R44 sees 37.8 − 25.65 =
12.15 V and **45.6 mW, 73%** of the rating at 70 °C, above which a thick-film
0402 derates further. The 50 V limit is never approached. During a 64.5 V TVS
clamp R44 sees 38.85 V and **466 mW, 7.5x rating** — survivable because the
event is microseconds and the part's thermal mass is not, but it is the same
corner Q3's V_CE is worked at and it belongs here too. Lowering a resistor with a
fixed voltage across it necessarily heats it more. 3.24 kΩ is where the margin stops depending on an unpublished
number without the resistor leaving comfortable derating; it is also a part
already in the BOM. And V_EBO is 6.0 V: at power-down D14 holds the pin at 4.19 V
while the bus collapses, so the junction sees up to 4.19 V reversed. Inside the
rating, and the datasheet characterises I_EBO at exactly 4.0 V, so it is a region
the part is specified in.

The tighter-binned C27 is chosen over the C27S
(25.1-28.9 V) because the extra 1.1 V of spread eats most of the high-side
margin. The 5 V case is untouched: the Zener does not conduct at a 5 V bus, so
the pin still comes from D14 at 4.19 V.

This replaces the earlier assumption of a resistor-plus-Zener shunt, and the
difference matters: a follower holds the pin near (V_base − V_BE) with low output
impedance, where a series resistor would drop voltage with load current.

#### The follower does not reach the thresholds at 5 V, and the fix is two parts

The follower costs one V_BE, and it costs it where there is least room. At the
bottom of vSafe5V — a **4.75 V** bus — the base sits 43 mV below the rail
(I_B × R44) and the emitter one V_BE below that, so the pin gets
**3.91 – 4.11 V**. Against a VBUS-pin minimum of **4.5 V** with VDD unpowered,
the chip would not reliably come up.

**First: VDD (pin 5) is an input supply, so tie it to 3V3.** The datasheet is
explicit — *"It is recommended to tie this pin to the single cell battery or a
3.3 V power rail. When the power is not available from this pin, the VBUS pin may
power the internal circuitry"* (p.4). That one connection changes two numbers at
once:

| With VDD = 3.3 V | With VDD unpowered |
|---|---|
| VBUS pin range **3.15 – 29.4 V** | 4.5 – 29.4 V |
| VBUS pin current **330 µA typ / 800 µA max** | 4.5 mA |

The requirement drops by 1.35 V and the current by **5.6x**, and 3.91 V now
clears it by 0.76 V. That alone would do — but it is not what decides, because
two *threshold* figures sit above the minimum and the follower misses both. Sequencing makes this safe: nothing has to happen until
firmware pulls EN_N low, and by then the 5 V and 3.3 V rails are both up, because
they are fed from the bus directly.

**Second: a Schottky from the 5 V rail to the VBUS pin.** At a 4.75 V bus the
rail sits at **4.56 V** typical (the dropout table), and an RB751V-40 drops
**0.37 V max at 1 mA** (p.2), so the pin is held at **4.19 V** — and the follower,
seeing its emitter above (V_base − V_BE), simply stops conducting. At the
converter's applicable worst-case on-resistance the rail is 4.48 V and the pin
**4.11 V**, which is the figure the residuals below have to be read at. Once the bus rises the
follower takes the pin to ~26.3 V and the Schottky is reverse-biased by 21.3 V,
well inside its 40 V rating. It does nothing at 36 V and everything at 5 V.

| | Follower alone | With D14 |
|---|---|---|
| Pin at a 4.75 V bus | 3.91 – 4.11 V | **4.19 V** typ, **4.11 V** at the R_DS corner |
| Against the 3.15 V minimum | +0.76 V | +1.04 V |
| Against the 4.0 V thresholds | **−0.09 V at the low corner** | +0.19 V typ, **+0.11 V** at the R_DS corner |

So D14 is not rescuing a part that fails its supply minimum — the follower clears
that by 0.76 V since VDD was tied to 3V3. It is buying the margin on the two 4.0 V
*threshold* figures, which the follower alone misses at its low corner.

**Two residuals at the bottom of vSafe5V, and they are tighter than they look.**
Both are 4.0 V thresholds against the **4.11 V** the Schottky delivers at the applicable R_DS corner (4.19 V on a typical part):

| Threshold | Value | Margin at 4.19 V |
|---|---|---|
| `VBUS_OK` rising, vVBPRS_R | **3.67 / 4.0 / 4.4 V** min/typ/max (p.7) | at the R_DS corner **+0.11 V** on typ, **−0.29 V** on max (a typical converter gives +0.19 / −0.21) |
| VBUS UV falling | **not specified at a 5 V RDO.** p.7 bands it 86% for 26 V > RDO > 10 V (F0) and 80% for 10 V ≥ RDO > 5 V (F1) — 5 V itself falls in no band. The **adjacent** band is F1 at 80%, which ends exactly at 5 V; F0 starts at 10 V, one band further up. At 80% the bound is **4.00 V**; at F0's 86% it would be 4.30 V | at the R_DS corner **+0.11 V** on the adjacent band, **−0.19 V** on the pessimistic one |

The first decides whether VBUS-present is seen; the second is the under-voltage
detector that, per the same datasheet's UVP section, *"moves out the Attached.SNK
state"*. Note that 3.67 V is the **minimum**, not the typical. The risk is easy
to argue away on the grounds that "negotiation runs on CC"; that is weaker than
it sounds, because USB Type-C gates
the `AttachWait.SNK → Attached.SNK` transition on VBUS detection, which is what
vVBPRS_R implements, and the UV detector was not mentioned at all.

**Honest position: two margins, both thin, and one of them unspecified.**
`VBUS_OK` rising clears by **+0.11 V** at the converter's applicable worst-case
on-resistance (+0.19 V on a typical one) and misses by 0.29 V on a max-threshold
part. The under-voltage detector publishes no band covering a 5 V RDO at
all; its **adjacent** band is F1 at 80%, giving a 4.00 V bound that the pin
clears by **+0.11 V** at the R_DS corner and +0.19 V on a typical converter —
numerically the same pair as `VBUS_OK` by coincidence, since vVBPRS_R typ is
4.0 V and 80% of 5 V is also 4.0 V. Reading the *next* band up instead (F0, 86%)
would put the bound at 4.30 V and the pin 0.19 V short, which is the pessimistic
case rather than the adjacent one. Both margins would move
0.35 V the right way if the 5 V rail were not at its own dropout floor. Bench-check it on the first board; if it
fails, the lever is the rail, not the diode — a lower-DCR inductor buys back
more than any Schottky swap can.

**Hynetek's >28 V gate drive is not built here, and this note survives only to
say why.** Figures 4, 5 and 6 all draw the *same* Zener gate network — the 28 V
one, not a 48 V feature — and above 28 V Hynetek replaces it with GATE pulled to
3V3 through 5.1 kΩ and level-shifted by a PMOS-then-NMOS pair into a high-side
P-FET.

That mattered while the HUSB238A's GATE pin drove the power path. **It no longer
does:** GATE is unconnected, the channels switch through their own controllers,
and none of this arrangement exists in the BOM. It also retires the open question
about a small-signal PMOS — the design needs no such part.

### No overvoltage protection above 28 V

Hynetek states it plainly: *"因为HUSB238A没有36V/48V OV保护"* — there is no
36 V/48 V OV protection. The clamp preserves the supply function and
VBUS-present detection, but **destroys OVP and the discharge path**. UVP is the
exception and the whole follower analysis below depends on it: the clamped pin
tracks the bus again once the Zener stops conducting, which is a **bus** figure
of 25.7–28.4 V across the clamp's spread, so vVBUV_F2's flat
22.4 V fires at a bus of roughly 23.2 V — the intended threshold plus one V_BE.
OVP genuinely cannot fire, because VBUS_OV is 120% of the requested voltage —
43.2 V on a 36 V contract — and the clamp never lets the pin near it. It is
also why 48 V is I²C-only while GPIO mode stops at 28 V.

Accepted, because it is inherent to the topology rather than a mistake — but it
changes what protects the bus. The **SMCJ40CA TVS is now the only fast
overvoltage protection**, with the 470 k/27 k ADC divider as a slow software
check. Those two were added for surge and brownout; they are now carrying OVP as
well.

Worth knowing this approach is the **outlier**. The common industry solution is
an active 0.42× ratiometric level shifter in a companion IC (TI TPD4S480,
Kinetic KTU1133, Fortune FA2218), which *preserves* measurement where Hynetek's
clamp does not. If a later revision wants real OVP back, that is the direction.

## Channel switching and inrush

**Four attempts at a discrete gate network, four failures.** The topology is
abandoned. Each channel now switches through a **TPS16630 eFuse** (C1849461),
which does ramp, current limit, overvoltage cut-off and fast turn-off in silicon
with an integrated FET.

### Why the discrete attempts kept failing

| Attempt | What it got right | What it broke |
|---|---|---|
| R_pu 100 k / R_pd 1 M | — | Vgs 3.27 V, FET never enhances |
| R_pu 10 M / R_pd 1 M / C_gd 1 uF | the divider | ramp on the wrong node; 180 s turn-off; dV/dt self-turn-on |
| Drop the pass FET, C_gd 3.3 uF | removed one FET | moved the same faults onto switches with 27x more load capacitance |
| C_gs + diode across R_pu | dV/dt immunity | the diode clamps Vgs so the FET never enhances; C_gs cannot set a ramp |

The fourth was written here as **settled**, which makes it the worst of the four.
Two independent errors, both found by review:

- **The bypass diode cannot work.** R_pu sits gate-to-source and carries current
  source-to-gate in *both* phases — when turning off (wanted) and when the
  pull-down drags the gate low (not wanted). A diode across it cannot tell those
  apart. Anode at the source, it forward-biases as soon as Vgs passes -0.6 V and
  clamps there, so the FET never reaches its -1.0 to -2.5 V threshold; anode at
  the gate, it is reverse-biased during turn-off and does nothing. The
  diode-across-a-resistor trick needs a *series* gate resistor to bypass, and
  this topology has none.
- **A gate-to-source capacitor cannot set a ramp.** The FET is in common source:
  the drain transition happens entirely inside the Miller plateau, where Vgs is
  constant by definition, so C_gs carries **zero** current while the drain slews.
  It adds turn-on *delay*, not dV/dt control. The drain rate would instead be set
  by the FET's own Crss against the net gate current — 326 uA into 98 pF is
  3.3 V/us, the full 36 V in about 11 us — with the current rise through a 35 S
  transconductance entirely uncontrolled.

The thread running through all four: **a discrete network has to deliver ramp,
current limit, turn-off and dV/dt immunity from one capacitor and two resistors,
and those requirements pull against each other.** No value set reconciles them.
That is what the integrated part exists for.

### The replacement

One **TPS16630** per channel: a 60 V, 6 A eFuse with an integrated 31 mΩ (headline) hot-swap
FET, adjustable current limit, adjustable overvoltage cut-off and a dV/dt pin
that sets the output slew rate directly.

```mermaid
flowchart LR
  VBUS[VBUS 4.5-60 V] ==> U["TPS16630<br/>integrated FET"]
  U ==> OUTN[channel bus]
  DIV["divider<br/>732k / 255k / 30.0k"] --> U
  RIL["R_ILIM 3.24k"] --> U
  CDV["C_dVdT 220 nF"] --> U
  MCU[ESP32-C6 GPIO] -->|"1k series"| U
  FLT[HUSB238A FAULT] --> QF["Q1 BSS138<br/>FAULT interlock"]
  QF --> U
  U -. PGOOD .-> MCU
```

**It replaced an LM5069 driving a discrete N-FET**, and the reason was the one
requirement the discrete arrangement could not meet: the LM5069 needs **8 V** to
operate, so the LED output was dead below a 12 V contract and dead on a laptop
port. The TPS16630 works from **4.5 V**.

| | LM5069 + NSS085N100S | TPS16630 |
|---|---|---|
| input range | 8-90 V | **4.5-60 V** |
| absolute max | 108 V | 67 V, 75 V for 10 ms |
| parts per channel | IC + FET + sense resistor | **IC only** |
| stock | 105 | 1141 |
| inrush | 1.05 A rising to a 5.5 A plateau | **constant 0.72 A** |
| two channels at once | 11 A — needed staggering | **1.44 A** |
| ramp vs module count | capacitance-limited at 12.7/channel | **bounded by the eFuse's 1.1 s thermal timeout, not by a fault timer** |
| switched-path dissipation | 0.35 W | 0.66 W |

The last row is the cost and it is real. It is the whole **switched path**, not
the FET alone: the conduction half is 45 mΩ max integrated against 9.5 mΩ
discrete plus a 10 mΩ shunt, which is 0.53 W against 0.23 W, and each column
carries its own controller's quiescent and divider draw on top — 0.13 W here,
0.12 W there. Thermal headroom falls from 1.29x to **1.19x**, on a board
whose thermal section already calls 45 °C ambient over budget.

**What it buys back is three of this document's own known limits.** The inrush is
set by dV/dt rather than by a power limit, so it is *constant* rather than rising
into the current limit — which means both channels can be released together, the
ramp no longer exceeds the PD contract, and the ramp duration no longer depends
on how much capacitance is hanging on it.

### Values, derived

Per channel, from the datasheet's own equations rather than from its worked
example — **back-solving the gain from the rounded example in §10.2.2.3 gives
33x and is wrong**. The datasheet specifies
`GAIN(dVdT) = 23.5 / 25 / 26 V/V` directly (p.7), and Equations 1 and 2 give it
independently as `1/(20.8e3 × 2 µA) = 24.0`.

    t(dVdT)   = 20.8e3 × V_IN × C_dVdT          (2)
    I(INRUSH) = C_OUT × V_IN / t(dVdT)          (1)

| Element | Value | Derivation |
|---|---|---|
| R_ILIM | **3.24 kΩ** 1% | the datasheet tabulates 3 kΩ → 6 A and 4.02 kΩ → 4.5 A, i.e. I·R ≈ 18 kΩ·A, so 3.24 kΩ gives **5.56 A**. Clears the 4.0 A white-flash peak by 1.29x at the low end of its ±7% spread |
| C_dVdT | **220 nF** | Eq. 2 gives t = 20.8e3 × 36 × 220 nF = **165 ms**; Eq. 1 gives inrush = 3.3 mF × 36 / 165 ms = **0.72 A**. Carrying I(dVdT) 1.775-2.225 µA and GAIN 23.5-26 V/V, the spread is **137-190 ms** and **0.63-0.87 A** before C_dVdT's own ±10% |
| UVLO/OVP string | **732 kΩ / 255 kΩ / 30.0 kΩ** 1% | IN → R_UV1 → **UVLO** → R_UV2 → **OVP** → R_UV3 → GND, against 1.2 V on both pins. UVLO is the *upper* tap. Gives **UVLO 4.28 V**, **OVP 40.68 V**, and draws 35.4 µA. The E192 values this string first carried (723 k / 249 k / 29.4 k) are not stocked in 0402 — see Bill of materials |
| C_IN | **220 nF** ≥100 V | p.6 Recommended Operating Conditions gives 0.1 µF as a **minimum**. A nominal 100 nF ±10% part meets it with zero margin — 90 nF at tolerance alone, less after X7R's tempco and DC-bias loss at 36 V. 220 nF lands near 150-170 nF effective. Same reasoning the TPS54360B row applies with its "≥3 µF effective after DC-bias derating" |
| PGOOD pull-up | **10 kΩ** to 3V3, shared | open drain; both channels wire-ORed onto one GPIO |
| C_OUT, **OUT → GND** | **220 nF** ≥100 V | p.6 Recommended Operating Conditions gives 0.1 µF as the **minimum** at IN, P_IN and OUT. C34/C35, oversized so the minimum survives tolerance and DC bias |
| Transient Schottky, **OUT → GND** | **MBRS3100T3G**, cathode to OUT | §11.1 p.28: interrupting current makes the output inductance drive OUT negative, against a −0.3 V absolute maximum. D15/D16. Note it **bounds** the spike rather than meeting the rating: V_F is 0.79 V at 3 A, so OUT still goes to about −0.8 V — an unbounded inductive excursion becomes a bounded sub-µs one, which is the normal reading of an absolute maximum |

**The inrush is constant, which is the whole point.** Equations 1 and 2 make the
charging current independent of bus voltage and of how far the ramp has
progressed. At **0.72 A** per channel, **both channels can be released
simultaneously** — 1.44 A against a 5.0 A contract — and the staggering the
previous design needed is gone.

**The ramp time does not depend on module count, at 25 °C.** Equation 2 has no
C_OUT term, so t is **165 ms** whether a channel carries 10 modules or 20 — until
the thermal regulation loop engages and overrides the slew, which it will at
higher ambients (see below); only the inrush
scales, to 1.44 A per channel at 6.6 mF. That removes the
12.7-modules-per-channel start-up ceiling the LM5069's fault timer imposed — the
binding constraint returns to power, where it belongs.

**Turn-on delay** is `742 µs + 49.5 × C_dVdT[nF]` = **11.6 ms** at 220 nF, which
is what replaces the LM5069's multi-second insertion timer.

**Ramping 3.3 mF is characterised, and the comparison is energy, not farads.**
TI publishes the part powering into **15 mF** (Figure 16) — but at V_IN = 24 V
and with dVdT *open*, so ½CV² = **4.32 J**. Ours is ½ × 3.3 mF × 36² = **2.14 J**,
i.e. **2.0×** inside TI's demonstrated case, not the 4× a capacitance comparison
suggests. At 20 modules *on a single channel* it is **4.28 J ≈ 1.0×** — equal to
the demonstration, not a quarter of it. Note that case is a 40-module wall, which
is outside the 180 W budget; it is carried as the growth bound, not the design
point.

**And the 165 ms ramp is never actually realised.** Equation 3 gives the inrush power:

    P_D(inrush) = 0.5 × V_IN × I_INRUSH = 0.5 × 36 × 0.72 = **13 W** per channel

for 165 ms, 26 W for the pair — six times the board's entire steady budget. The
datasheet is explicit that if this exceeds the Figure 13 power-versus-time
boundary, the **thermal regulation loop** takes over, overrides the programmed
slew and starts a `t(Treg_timeout)` of **1.1 s minimum** (1.25 typ); if the output has not come up by
then the FET turns off and MODE decides latch or retry.

Reading Figure 13 properly — the curves extracted rather than estimated — gives:

Figure 13 is a *per-device* boundary, so both rows below are one eFuse. The
module counts are therefore **per channel**, not wall totals — 10 per channel is
the 20-module design point.

| P_D per device | 0 °C | **25 °C** | 85 °C |
|---|---|---|---|
| 13 W (10 modules on the channel) | 475 ms | **120 ms** | 58 ms |
| 26 W (20 modules on the channel) | 68 ms | 38 ms | 14 ms |

**So the programmed 165 ms slew is never realised at any ambient this board will see.** At 25 °C the
boundary at 13 W is 120 ms, *below* the ramp. Two things make it tighter still: Figure 13 is time-to-*shutdown*
at T(TSD) = 165 °C, while the regulation loop engages at T(J_REG) = 145 °C typ and
136 °C minimum, so regulation starts earlier than the plotted time; and the
inrush tolerance band puts P_D at 11.3-15.7 W rather than exactly 13 W.

**The correct statement is that thermal regulation runs the ramp, not the dVdT
capacitor.** C_dVdT sets the *intended* slew and the loop overrides it whenever
the ramp would overheat the die, which is always here. The real bound is
`t(Treg_timeout)`, and the binding end of that spread is its **1.1 s minimum**
(1.25 typ, 1.5 max, p.8) — not the typical. The question is whether the output
comes up inside it.

On energy it does. Figure 13's 25 °C trace is the hardest line on that plot to
read — light grey over a dense log grid — and two careful extractions of it
differ by 20%, giving **5.9 to 7.0 W** at the timeout. That is **6.5 to 7.7 J**
against the **2.14 J** one channel needs at the 10-modules-per-channel design
point: **3.0 to 3.6x**, and the spread does not change the answer. At 85 °C the
curve is near 4 W, so about 4.5 J. The design point keeps roughly **2x** margin
even hot; the 20-per-channel growth case sits at 4.5 J against 4.28 J, i.e.
**1.05x**, and that case is already outside the power budget.

One caveat the datasheet attaches and this design cannot close: Figure 13 is
*"taken on VQFN device on EVM board"*, and the fitted part is the HTSSOP-20. Both
the package and the copper differ, in the unhelpful direction.

**Thresholds.** UVLO at 4.28 V sits just under the part's own 4.5 V minimum
operating voltage, so **the device's own floor is the binding one** and the LED
output follows the rails down to vSafe5V. Its hysteresis is 78 mV typ, so it
falls out at 4.00 V — still under that floor, which is why the hysteresis does
not need designing around.

**OVP cuts off at 40.68 V, and that number closes an accepted limit.** The
reference is **±2%** (1.176/1.2/1.224 V), not the ±10% of the LM5069 comparator
this design previously carried. With 1% resistors the trip lands between
**39.1 V and 42.3 V** — above the 37.8 V maximum bus by 1.3 V and below the
modules' 45 V absolute by 2.7 V. The window the old part could not fit into is
comfortable for this one.

**The release threshold is not comfortable, and closing the trip point did not
close it.** V(OVPF) is 1.09/1.122/1.15 V against a nominal divider ratio of 33.9, so the
channel re-enables at **37.0-39.0 V** on the comparator spread alone and
**36.2-39.7 V** once the 1% resistors are carried. A compliant source may sit at **37.8 V indefinitely** —
which is inside that band. A low-corner part that trips on an excursion would
then **not re-enable on a healthy bus**: it needs the bus below 36.2 V, which a
36 V contract never reaches.

**And cycling SHDN does not clear it**, which the datasheet's own latch-reset
sentence makes tempting to assume. OVP is not a latch: p.7 gives it a rising threshold V(OVPR) and a falling
one V(OVPF), so it is a hysteretic comparator that re-enables the FET on its own
once the OVP pin falls below V(OVPF) and *not before*. The SHDN-reset sentence in
the datasheet, and Table 1's latch/auto-retry choice, belong to the overload and
thermal fault class — a different mechanism. Toggling SHDN while the bus still
sits above the release point simply re-enters OVP shutdown within
`OVP_toff(dly)` = 11 µs.

**The recovery that does work is a PD renegotiation.** Firmware commands the
HUSB238A down to a lower fixed PDO — 20 V or 28 V — which drops the bus far below
even the low release corner, the eFuse re-enables, and firmware then renegotiates
back to 36 V. The bus returns to at most 37.8 V, which is under the *trip*
corner of 39.1 V, so the channel stays up. This costs no parts: the HUSB238A and
the I2C link it needs are already on the board for the initial negotiation.

Retuning the divider instead was worked and rejected. Clearing 37.8 V on the
release corner needs a divider ratio of at least 35.36 once the 1% resistors are
carried on both sides, which pushes the trip's
high corner to about 44.1 V — above the modules' XL1509 **40 V operating** rating,
though still inside its 45 V absolute. Trading a recoverable stall for an
overvoltage the modules are not rated to see is the wrong direction.

### Enable

**SHDN needs a path *up*, and that is the whole difficulty.** An open-drain
transistor on SHDN together with a pull-down on the same pin gives two paths to
ground and none to 3V3, so both channels sit in low-IQ shutdown permanently. The
only thing that can raise SHDN is its own internal source
(2.48-3.3 V open circuit, ≤10 µA), which against 100 kΩ settles near 0.77 V,
under the 0.8 V guaranteed-shutdown threshold.

**SHDN is a logic input and the GPIO drives it directly.** It accepts 0-5 V with
V(SHUTR) ≤ 2 V, so a 3.3 V GPIO is in range with margin.

| Part | Value | Why |
|---|---|---|
| SHDN pull-down | **10 kΩ** to GND | fail-safe off through MCU reset. Against the pin's own ≤10 µA source this holds 0.1 V, clear of the 0.8 V shutdown threshold — 100 kΩ would sit at 0.77 V and might not shut down at all |
| GPIO series | **1 kΩ** | lets the FAULT transistor override a driven-high GPIO without shorting it. With 1 kΩ into 10 kΩ, SHDN reaches **3.0 V** when the GPIO is high, above the 2 V turn-on threshold |
| Q1/Q2 | BSS138 drain on SHDN | FAULT asserted must pull SHDN below the 0.8 V threshold, which needs R_DS ≤ 330 Ω against the 909 Ω Thévenin source — easily met, though R_DS is not characterised at this gate drive. The GPIO then sources 3.2 mA through the 1 kΩ |

**The logic is not inverted.** GPIO high enables, GPIO low or high-Z disables, and
the pull-down makes reset the off state. The 2N7002s the old arrangement needed
in the enable path are gone entirely — the GPIO drives SHDN itself, and the only
transistors left on this net are the two FAULT interlocks.

### What is still open here

- **PGOOD wiring.** Both TPS16630 PGOOD pins are open-drain; wiring them together
  into one GPIO with a pull-up reports "both channels good" and costs the single
  spare pin. Which channel failed is then inferred from which one firmware
  enabled. The part also brings an **IMON** current-monitor output per channel,
  which would give the per-channel sensing the shared shunt cannot — unused for
  now because there are no spare ADC pins.
- **The low-side shunt wants Kelvin connection.** R1 is 5 mΩ carrying the full
  return current, so 0.125 W at the 5 A the limiter holds it to and 0.62 W at the
  11.1 A the two eFuses can pass — the **range** is sized for the second figure
  and the **dissipation** quoted at the first, because one is a fault lasting
  milliseconds and the other is continuous. Its sense traces are not a routing
  afterthought. (The eFuse
  has no sense resistor — this is the INA226's shunt, one part in the shared
  return, not a per-channel pair.)
- **Second-source risk is lower than it was.** C1849461 is TI silicon at 1141
  units, where the LM5069 it replaces was a Tokmas clone at 105. The TPS16632
  variant adds adjustable output power limiting but fixes the overvoltage clamp,
  and the binding figure is its **35.7 V minimum** (36.6 typ, 39 max, p.7) — i.e.
  the worst-case part clamps *below* the 36 V nominal bus, never mind the 37.8 V
  maximum. So the -30 is the right one here. Read the **minimum** of that spread,
  not the 39 V maximum: the maximum is the forgiving end and 3.3 V away from the
  number that decides it.
- **The OVP release threshold straddles the maximum bus.** The trip point is
  sound, the recovery point is not, and the lever is a firmware **PD
  renegotiation** down to 20 V or 28 V — *not* cycling SHDN, which cannot clear a
  hysteretic comparator, and not a divider change. See Channel switching and
  inrush.

## Why two channels

Checked rather than assumed. At the 20-module target:

| channels | modules/ch | fps | A/ch balanced | % of 10 A | A/ch worst case | % |
|---|---|---|---|---|---|---|
| **2** | **10** | **90** | **2.43 A** | **24%** | 2.80 A | 28% |
| 3 | 6.7 | 134 | 1.62 A | 16% | 1.87 A | 19% |
| 4 | 5 | 176 | 1.22 A | 12% | 1.40 A | 14% |

"Worst case" is one channel sustained at the 70% thermal cap while the other is
dark — PD limits the *total* to 5 A, not how it splits. Percentages are against
the picoMAX's 10 A rating; at two channels the connector has 3.6x margin and is
nowhere near binding.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="figures/current-vs-connector-rating-dark.svg">
  <img alt="Per-channel current against the connector rating" src="figures/current-vs-connector-rating.svg">
</picture>

Two is right because 180 W caps the wall near 20 modules anyway, so channels and
power budget run out together. 10 per channel is also the even physical grouping
wanted; three channels would mean 6.7 per chain for no benefit that is felt.

Two boundaries worth knowing rather than discovering:

- **An unbalanced load is no longer a connector problem.** PD caps the total at
  5 A but says nothing about the split, so one channel at the thermal cap while
  the other is dark draws 2.80 A — 28% of the picoMAX's 10 A. This was a real
  constraint at the old 3 A connector and is not one now.
- **Growth is no longer capped by start-up.** The LM5069 that preceded the
  TPS16630 had a fault timer that expired during the ramp above ~12.7 modules per
  channel, because t_start scaled with capacitance. Under dV/dt control the ramp
  is **165 ms as programmed** — though in practice the thermal regulation loop
runs the ramp rather than C_dVdT, see Channel switching and inrush; only the
inrush scales, and at the headline 20-module
  wall that is 0.72 A per channel — 10 modules and 3.3 mF each. The binding
  constraint is power again.

- **Frame rate past ~24 modules degrades too**, if the capacitance problem were
  solved: 24 -> 76 fps, 30 -> 61 fps (at the floor), 40 -> 46 fps (below it).
  Beyond ~24 is a board revision with 3-4
  channels, not a software change.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="figures/fps-vs-modules-per-channel-dark.svg">
  <img alt="Frame rate vs modules per channel" src="figures/fps-vs-modules-per-channel.svg">
</picture>

## Bill of materials

Active parts. Passives are listed below the table.

**The `$` column is the unit price at LCSC's lowest quantity break**, taken from
each part's knowledge-base record — so it carries that record's `retrieved`
date, which `./tools/stock.py --refresh` updates and `./tools/consistency.py`
checks the column against. One basis for the whole column, because mixing tiers
is how a BOM total becomes unreproducible: five cells here used to sit at
different breaks and three at no published break at all. Real quantities are
much cheaper — the eFuse is $2.80 at one and $1.78 at a hundred — so this
column is an upper bound, not an estimate of what a run costs.

The one cell that is **not** on that basis is J2/J3's 0.77, which is **€0.77**
from Reichelt: not an LCSC part, no quantity ladder. It is in the table because
a build needs the number, and it is called out here because the column heading
cannot carry two currencies.

| Ref | Part | LCSC | Qty | Role | $ |
|---|---|---|---|---|---|
| U1 | HUSB238A-BB001-QN16R | C24833806 | 1 | USB PD sink, I2C mode | 0.6572 |
| U2 | ESP32-C6-WROOM-1-N8 | C5366877 | 1 | MCU, native USB | 4.2495 |
| U3 | W5500 module, WIZ850io-class | **C134462, DO NOT FIT** | 1 | 10/100 Ethernet, **soldered down**. The C-number is in the netlist so EasyEDA resolves the symbol and footprint — it must **not** reach a PCBA order, where it buys the $22.89 WIZnet original instead of the €12.30 JOY-IT clone in the sourcing table below. See Ethernet module | — |
| U4 | TPS54360B | C524806 | 1 | bus to 5 V, ~100% duty | 0.7605 |
| U5 | SY8089A1AAC | C479074 | 1 | 5 V to 3.3 V | 0.0869 |
| U6 | INA226 | C49851 | 1 | low-side current sense | 0.7621 |
| U7,U8 | MAX3485 | C6395158 | 2 | differential line driver, one per channel | 0.3495 |
| U9 | 74AHCT541 | C84548 | 1 | status-chain level shift (oversized, see KB) | 0.224 |
| U10,U11 | TPS16630PWPR | C1849461 | 2 | 60 V / 6 A eFuse, integrated FET, one per channel | 2.8002 |
| Q1,Q2 | BSS138 | C7420339 | 2 | FAULT interlock, **one per channel** — logic-level, see Enable | 0.0266 |
| Q3 | **MMBT5551** | C7420357 | 1 | **emitter** follower feeding the VBUS pin — V_BE 0.6-0.8 V where a BSS138 gate is 0.8-1.6 V, which is what lets 9-15 V contracts hold. 160 V V_CEO | 0.0122 |
| D1 | **BZT52C27** | C173421 | 1 | clamps the Q3 follower base at 25.65-28.35 V — see the PD front end; a 20 V part here drops every 36 V contract | 0.0164 |
| D2 | SS36 | C2903825 | 1 | TPS54360B catch diode (required, p.26) | 0.0629 |
| D3-D6 | H5VL10B | C7420372 | 4 | ESD on USB-C D+/D- and CC1/CC2 | 0.0065 |
| D7-D9 | **SMCJ40CA** | C19077610 | 3 | **40 V TVS**, which stands off the 37.8 V a compliant PDO may hold indefinitely where a 36 V part does not. Its 0.66 Ω dynamic resistance keeps the node under the 60 V TPS54360B and SS36 to **16.5 A** of surge. The residual is J1 at 48 V, which no 37.8 V-standoff TVS can protect — see ESD and surge protection | 0.1242 |
| D10-D13 | SMAJ7.0CA | C19077529 | 4 | 7 V TVS on the A/B pair, 2 per output — clamps at **12 V**, under the MAX3485's ±15 V | 0.0428 |
| D14 | RB751V-40 | C7502691 | 1 | 5V rail → HUSB238A VBUS pin at vSafe5V | 0.0179 |
| D15,D16 | **MBRS3100T3G** | C12790 | 2 | **cathode on each channel output, anode to GND** — §11.1 wants a Schottky there to absorb the negative spike the output inductance generates when the eFuse interrupts. 100 V rather than the 60 V SS36 for **leakage**, not breakdown: these sit at 36 V reverse continuously, and the SS36 would run at 60% of its rating for 0.5 mA against this part's 50 µA | 0.3045 |
| L1 | SPM6530T-100M | C112288 | 1 | 10 µH, TPS54360B output, **3.8 A Isat, 72 mΩ DCR** | 0.2279 |
| L2 | ANR6028T2R2M | C7427146 | 1 | 2.2 uH, SY8089 output | 0.0676 |
| R1,R2 | see Passives | — | — | the shunt and the bleeder are listed with the other passives below | — |
| J1 | CX90B-16P (Hirose) | C3198004 | 1 | USB-C, **5 A / 48 V AC/DC**, USB 2.0 | 0.9868 |
| J2,J3 | Wago picoMAX 3.5 4-pole, angled | 2091-1424 | 2 | module output, see sourcing table | 0.77 |
| LED1-8 | SK6812-EC20 | C2909058 | 8 | status chain, 2 × 2 mm top-view | 0.1112 |
| SW1,SW2 | TS-1088-AR02016 | C720477 | 2 | **BOOT** (GPIO9 to GND) and **EN** (reset) — recovery only; programming is over the ESP32-C6's native USB on J1 | 0.0528 |
| J4 | PZ254V-11-02P | C492401 | 1 | **fan header, do not fit by default** — 1x2 2.54 mm across the 5 V rail. See Cooling | 0.0188 |
| TP1-TP3 | — | — | 3 | UART0 TX / RX / GND, bare pads. A footprint, not a part: the console is a fallback for when USB enumeration itself is what is broken | — |

**Not from LCSC/JLCPCB.** Two board items are hand-fitted — the Ethernet module
and the picoMAX headers — so the board is not fully JLCPCB-assemblable. The third
row below is the cable-side plug, which is not a board part at all and is listed
because a 20-module run needs twenty of them. Order numbers so the build is reproducible:

| Ref | Part | Source | Order no. | Price |
|---|---|---|---|---|
| U3 | JOY-IT SBC-USR-ES1, W5500 module | Reichelt | **DEBO SPI RJ45** (art. 305766) | €12.30 |
| J2,J3 | Wago picoMAX 3.5, pin header **angled**, 4-pole — board side | Reichelt | **WAGO 2091-1424** (art. 136753) | €0.77 |
| — | Wago picoMAX 3.5, **spring plug** 4-pole — cable side, 2 per module hop | Reichelt | **WAGO 2091-1104** (art. 136715) | — |

The straight-pin alternative is **2091-1524** (art. 136759, €3.10); angled is the
default because the cable then leaves parallel to a wall-mounted board rather
than standing off it. The spring plug is not a board part — it is counted here
because a 20-module run needs 20 of them and they are easy to forget when
ordering.

Passives. Every one is an LCSC part with a knowledge-base record, and every
C-number here has been checked against the **live** stock endpoint with
`./tools/stock.py`, not against a catalogue mirror — five of them were at zero
when this table first claimed otherwise — the importer resolves by `Supplier Part` alone, so a passive without a C-number cannot go into the netlist at all, and omitting it instead trips the decoupling and pull-up rules that check for it.

**Resistors** — 47 parts, 0402 1% unless stated. R16 is unused.

| Ref | Value | LCSC | Role |
|---|---|---|---|
| R1 | **5 mΩ 1% 3 W 2512** | C393074 | low-side current-sense shunt, shared return. **5 mΩ, not 10**: the INA226's ±81.92 mV range is 16.4 A here against 8.19 A at 10 mΩ, and this board's own full-white flash is 8.0 A — normal operation would have sat at 98% of full scale with the two eFuses able to pass 11.1 A beyond it. Halves the ground lift and the dissipation too |
| R2 | **10 kΩ 1206 250 mW** | C17902 | 36 V bus bleeder — 130 mW, which is why it is not an 0402 |
| R3,R4 | **3.24 kΩ 1% 0402** | C11457 | R_ILIM, one per eFuse → 5.56 A |
| R5,R6 | **732 kΩ 1% 0402** | C5159692 | R_UV1, upper leg of each eFuse divider |
| R7,R8 | **255 kΩ 1% 0402** | C270623 | R_UV2, between the UVLO and OVP taps |
| R9,R10 | **30.0 kΩ 1% 0402** | C2909347 | R_UV3, bottom leg — sets OVP |
| R11,R12 | **1 kΩ 1% 0402** | C2906864 | GPIO → SHDN series, lets FAULT override a driven-high GPIO |
| R13,R14 | **10 kΩ 1% 0402** | C25744 | SHDN pull-down — makes reset the off state |
| R15 | **10 kΩ 1% 0402** | C25744 | FAULT interlock gate pull-down, **one**, on the shared `PD_FAULT` net. A pull-*up* here latches both channels off. R16 was a second in parallel from reading "one per channel" off a net that is not per channel; the designator is left unused rather than renumbering everything after it |
| R17,R18 | **4.7 kΩ 1% 0402** | C25900 | I²C pull-ups, SDA and SCL |
| R19 | **10 kΩ 1% 0402** | C25744 | HUSB238A INT_N pull-up |
| R20 | **10 kΩ 1% 0402** | C25744 | shared PGOOD pull-up (both eFuses wire-ORed) |
| R21-R23 | **910 kΩ 1% 0402** | C25800 | HUSB238A ADDR, DEBUG_N, EN_HVDCP/OUT1 — the datasheet's 900 kΩ is not an E96 value |
| R24 | **100 kΩ 1% 0402** | C25741 | TPS54360B RT, ~1 MHz |
| R25 | **53.6 kΩ 1% 0402** | C53398 | TPS54360B feedback, upper |
| R26 | **10.2 kΩ 1% 0402** | C11660 | TPS54360B feedback, lower |
| R27 | **4.7 kΩ 1% 0402** | C25900 | TPS54360B COMP series |
| R28 | **100 kΩ 1% 0402** | C25741 | SY8089 feedback, upper |
| R29 | **22.1 kΩ 1% 0402** | C43473 | SY8089 feedback, lower |
| R30 | **100 kΩ 1% 0402** | C25741 | SY8089 EN pull-up — the datasheet forbids leaving it floating |
| R31 | **470 kΩ 1% 0402** | C25790 | bus-voltage ADC divider, upper |
| R32 | **27 kΩ 1% 0402** | C25771 | bus-voltage ADC divider, lower → 36 V reads 1.96 V |
| R33,R34 | **499 Ω 1% 0402** | C4125 | status-chain data series — **R34 on the 3.3 V side** (GPIO15 → buffer input), **R33 on the 5 V side** (buffer output → LED1 DIN). See Status indication |
| R35-R38 | **33 Ω 1% 0402** | C138002 | MAX3485 A/B series — slows the edge without disturbing the 120 Ω far-end termination |
| R39 | **10 kΩ 1% 0402** | C25744 | ESP32-C6 EN pull-up (with C17 forms the reset delay) |
| R40 | **10 kΩ 1% 0402** | C25744 | GPIO8 strapping pull-up |
| R41 | **10 kΩ 1% 0402** | C25744 | GPIO15 **pull-down** — the GPIO table asks for one, and it is the status chain's data line: a pull-up would hold DIN high through reset and the whole boot window. With factory eFuses GPIO15 is not read as a strap at all, so the LED idle state decides it |
| R42,R43 | **10 kΩ 1% 0402** | C25744 | MAX3485 DI pull-down, one per transceiver |
| R44 | **3.24 kΩ 1% 0402** | C11457 | MMBT5551 follower base pull-up. Small because the base current's own IR drop comes straight off the 12 V margin, and the pin current it scales with is **only specified at VBUS = 5 V** — see Assumptions. Same part as R_ILIM |
| R45 | **10 kΩ 1% 0402** | C25744 | INA226 ALERT pull-up — the pin is open-drain and cannot assert high without it. Stated as a requirement in Tie-offs for three revisions with no BOM line |
| R46 | **10 kΩ 1% 0402** | C25744 | W5500 module RSTn pull-down — holds the PHY in reset until firmware drives GPIO21, instead of leaving it floating through the boot window |
| R47 | **10 kΩ 1% 0402** | C25744 | W5500 module INTn pull-up — open-drain, same reasoning as the HUSB238A's INT_N |
| R48 | **0 Ω 0402, do not fit** | C17168 | follower bypass link. Fitted only on a build that never exceeds 28 V, where Q3 and D1 come out and VBUS connects straight through. **DNF** on this board |

**Capacitors** — 36 parts.

| Ref | Value | LCSC | Role |
|---|---|---|---|
| C1,C2 | **220 nF 50 V X7R 0603** | C64705 | C_dVdT, one per eFuse — sets the ramp |
| C5 | **100 nF 50 V X7R 0402** | C131394 | TPS54360B BOOT — required for operation |
| C6 | **33 nF 50 V X7R 0402** | C106862 | TPS54360B COMP series |
| C7 | **150 pF 50 V C0G 0402** | C1527 | TPS54360B COMP parallel |
| C8 | **4.7 µF 100 V X7R 1210** | C2840282 | TPS54360B input bulk — the part the "C_IN ≥3 µF effective after DC-bias derating" row means |
| C33 | **2.2 µF 100 V X7R 1210** | C153036 | 36 V bulk at the USB-C inlet. 2.2 rather than a second 4.7 because of the Type-C sink bypass limit — see Resolved |
| C3,C4,C34,C35 | **220 nF 100 V X7R 0805** | C513710 | C_IN and C_OUT at each eFuse, one of each per channel. p.6 gives 0.1 µF as a **minimum** at IN, P_IN *and* OUT; a nominal 100 nF ±10% part meets that with zero margin, so this is oversized deliberately |
| C9 | **220 nF 100 V X7R 0805** | C513710 | TPS54360B input decoupling — this one sits on the **36 V bus**, so it takes the same 100 V part as the eFuse inputs, not the 0402 |
| C14 | **100 nF 50 V X7R 0402** | C131394 | SY8089 input decoupling, on the 5 V rail |
| C10,C11 | **10 µF 50 V X5R 1206** | C7432781 | TPS54360B output |
| C12 | **10 µF 25 V X5R 0805** | C15850 | SY8089 input |
| C13 | **22 µF 25 V X5R 0805** | C45783 | SY8089 output — the p.1 selection table ticks 2.2 µH only at 22 µF and above, and L2 is 2.2 µH. 10 µF would be outside the vendor's endorsed L/C set, for a COT regulator whose stability depends on exactly that pair |
| C15 | **22 µF 25 V X5R 0805** | C45783 | ESP32-C6 local bulk — the 382 mA TX peak |
| C16,C19,C20 | **100 nF 50 V X7R 0402** | C131394 | ESP32-C6, HUSB238A and INA226 supply decoupling |
| C17,C18 | **1 µF 50 V X5R 0402** | C7472948 | ESP32-C6 EN delay, and HUSB238A VDD |
| C21,C22 | **100 nF 50 V X7R 0402** | C131394 | MAX3485 supply decoupling, one per transceiver |
| C23,C32 | **100 nF 50 V X7R 0402** | C131394 | 74AHCT541 and the Ethernet module's 3V3 feed |
| C24-C31 | **100 nF 50 V X7R 0402** | C131394 | one per SK6812 — the datasheet calls the inter-LED decoupling essential |
| C36 | **100 nF 50 V X7R 0402** | C131394 | bus-voltage ADC tap filter. Specified in prose for several revisions with no BOM line — it is what makes the 25.5 kΩ source impedance acceptable to the SAR, τ = 2.55 ms |

Not on this board: the **120 Ω** differential termination belongs at the far end of each chain, on the first converter board — see Cabling.

## Network protocol

**Custom UDP, not Art-Net.** Art-Net's 512-channel universe limit means 20 modules
(2160 bytes of pixel data) would be split across 5 universes and 30 modules across
7 — protocol bookkeeping that buys nothing here, because this is one controller
driving its own fixed set of chains, not a lighting desk addressing arbitrary
fixtures.

A plain UDP frame protocol is simpler and smaller:

| Modules | Pixel bytes | Packets at 1400 B MTU | packets/s **if run at 90 fps** |
|---|---|---|---|
| 10 | 1080 | 1 | 90 pkt/s |
| 20 | 2160 | 2 | 180 pkt/s |
| 30 | 3240 | 3 | 270 pkt/s |

Trivial *bandwidth* for a W5500 — 1.56 Mbit/s at the target — but **not trivial
buffering, and the default allocation drops half of every frame.** The W5500 has
16 kB of RX split **2 kB per socket by default** (§3.3 p.30, `Sn_RXBUF_SIZE`
p.52), and it prepends an 8-byte PACKET-INFO header per datagram. Packet 1 of a
20-module frame occupies 1088 B; packet 2 arrives about 92 µs later on a 100 Mbit
link, while draining packet 1 over SPI costs 109 µs at 80 MHz and 218 µs at
40 MHz before interrupt latency. With 960 B free the W5500 **discards a datagram
that does not fit**. The fix is **eight** register writes at init, not one: the W5500's 16 kB of RX
is shared, and the sum over all eight sockets must not exceed it. Setting
`Sn_RXBUF_SIZE = 8` on the pixel socket while sockets 1-7 keep their 2 kB default
allocates **22 kB** and aliases buffer memory. Write 8 for the pixel socket and
0 or 1 for the rest.

Keep each datagram under the ~1400 byte MTU rather than relying on IP
fragmentation, which is fragile over UDP and makes a single lost fragment cost
the whole frame.

Worth building in from the start: a **frame number and byte offset** in each
packet header, so the controller only displays once every packet of a frame has
arrived. Without it a dropped packet tears the image instead of just repeating
the previous frame.

**And a stale-frame watchdog, which is a safety requirement rather than a
polish item.** "Repeat the previous frame" is the right behaviour for one lost
packet and the wrong one for a lost link: a frozen full-white frame is 180 W of
sustained load with nobody watching. Blank the output after a bounded number of
missed frames — a few hundred milliseconds — and treat link-down as immediate
blank.

Two more things the protocol section owes an implementer: **addressing** (static,
DHCP or mDNS — this is a wall with no display, so a lost address is a site
visit), and the **MAC address source**. The W5500 has no EEPROM and no assigned
MAC; derive one from the ESP32-C6's eFuse base MAC rather than hard-coding a
literal that would collide on a multi-controller wall.

TouchDesigner drives it directly. If Art-Net is ever wanted it can be a software
translation layer; nothing in the hardware depends on the choice.

## ESD and surge protection

A rave means people plugging and unplugging connectors, and up to 11 m of cable
per channel acting as an antenna. Three different problems, three different
parts:

| Interface | Part | Qty | Why that one |
|---|---|---|---|
| USB-C D+/D-, CC1/CC2 | H5VL10B | 4 | 5 V class, and low capacitance matters on USB 2.0 full speed |
| USB-C VBUS, both output +36 V — **D7 to GND, D8/D9 to `LED_RTN`** | **SMCJ40CA** | 3 | **Vrwm 40 V**, so it is genuinely off at the PDO's +5%; Vbr 44.4-49.1 V, R_d 0.66 Ω. Stays under the 60 V TPS54360B and SS36 to 16.5 A of surge and under the eFuse's 67 V to 27 A. The residual is **J1 at 48 V**, which no 37.8 V-standoff TVS can protect. See below |
| Differential A and B, both outputs | **SMAJ7.0CA** | 4 | 7 V bidirectional, clamps at 12 V — under the MAX3485's ±15 V |

**The A/B pair needed its own part, not the 5 V one.** The RS-485 electrical
standard these drivers follow defines a
common-mode range of −7 V to +12 V, so a 5 V clamp would conduct during normal
operation and corrupt the bus. The fitted **SMAJ7.0CA** has a 7 V standoff, which
clears the 0.84-1.38 V of return IR drop this cable actually imposes — that
−7/+12 V figure is the transceiver's *capability*, not the excursion seen here,
because both ends share GND inside one shell. See Resolved. Bidirectional is
required because the lines swing both polarities.

**Ethernet needs nothing extra.** The module's RJ45 has integrated magnetics, so
the cable side is galvanically isolated, and Bob Smith termination is inside the
jack rather than something to add. Shield-to-GND bonding is the module's
arrangement, not ours.

**The standoff has to clear 37.8 V, and comparing headline clamping voltages is
how this was got wrong.** A compliant PD fixed PDO is ±5%, so a source may sit at
**37.8 V indefinitely**. Against a 36 V standoff that is *above* Vrwm, where
leakage is unspecified and strongly temperature-dependent — µA at 25 °C,
potentially mA at 85 °C — meaning standby draw and self-heating on three parts.
A 40 V standoff clears it outright, and `P1-overvoltage` fires on a 36 V part
here precisely because the knowledge base records Vrwm rather than Vc.

The objection to a 40 V part was its headline **64.5 V** clamp against the 36 V
part's 58.1 V. That comparison is invalid: **each figure is quoted at that
part's own rated surge** — 6.9 A for a 400 W SMA, 23.3 A for a 1.5 kW SMC. What
decides the board is the clamp at the current the node can actually deliver, and
that follows the dynamic resistance R_d = (Vc − Vbr) / Ipp:

| | SMAJ36CA (SMA, 400 W) | **SMCJ40CA (SMC, 1.5 kW)** |
|---|---|---|
| Vrwm | 36 V — **under the 37.8 V bus** | **40 V** |
| R_d | 2.01 Ω | **0.66 Ω** |
| clamp at 10 A | 64.3 V | **55.7 V** |
| reaches 60 V at | 7.8 A | **16.5 A** |
| reaches the eFuse's 67 V at | 11.3 A | **27.1 A** |

**Fitted: SMCJ40CA (C19077610).** It is the better part on every axis that
decides this board — it stands off the bus, and it stays under the 60 V
TPS54360B and SS36 to **16.5 A** where the SMA part crossed at 7.8 A. It costs
0.12 against 0.04, a larger SMC footprint on three placements, and a V_BR that
starts 4 V higher. It also has **more** stock, which is not why it was chosen but
is worth knowing.

**J1 is the part this does not fully rescue.** The CX90B-16P is rated **48 V
AC/DC**, and where the node reaches it depends on which breakdown corner the
part lands at: at the SMCJ40CA's **low** corner, 44.4 V, 48 V arrives at
**5.4 A** of surge; at its **high** corner, 49.1 V, the part does not conduct
until the node is already past J1's rating. (The retired SMA part reached 48 V
at 1.9 A, because its R_d is three times larger.) No 37.8 V-standoff avalanche TVS can protect a
48 V connector, because its own breakdown has to sit above the bus and below
nothing in particular. The event is 10/1000 µs on a connector with no
semiconductor junction, so this is a durability question rather than a
destruction one — but it is the residual, and it is the connector rather than
the silicon.

**The body diode does not protect against input-side surges**, and with the
N-channel switch it points the other way than this section used to assume. Its
anode is on the **channel** bus and its cathode on the VBUS side, so it conducts
channel-to-VBUS: no help at all for a surge arriving on USB-C, and for an
output-side transient it defeats per-channel isolation by dumping onto the live
bus and into the TPS54360B input. The body diode is never part of the protection
argument here — it is a liability, which is why it also drives the back-feed
finding in Known electrical limits.

## Mechanical envelope

**Target: no larger than an iPhone 16 (71.6 x 147.6 mm), ideally 2/3 the
height — so 71.6 x 98 mm, 7017 mm2.**

Component area estimates at **~2445 mm2**, counting the passives individually
rather than as an allowance — the itemised list is in the BOM. The three bus TVS
moved from SMA to SMC with the 40 V part: 50.6 mm² against 26 mm² of maximum
package envelope, so the **change** is about 25 mm² each and 74 mm² of the
total — the change, not the new package's full area. The Ethernet
module made the board slightly *bigger*, 575 mm2 against the ~496 it replaced.
At 45% utilisation (2-layer, relaxed) that is roughly
**72 x 74 mm**; at 60% (4-layer, dense) about 72 x 55 mm. The target is still met
with room to spare.

The largest items are where any further shrink comes from:

| Item | mm2 | Lever |
|---|---|---|
| ESP32-C6-WROOM-1-N8 | 459 | ESP32-C6-MINI-1 is 219 mm2 — saves 240 |
| 2x TPS16630 (HTSSOP-20) + copper | 260 | replaces 2x FET + 2x shunt + 2x MSOP at ~370 |
| 8x status LED | 32 | 4.0 mm² each; the SK6812MINI-E was 98 mm² |
| W5500 module (25 × 23 mm) | 575 | unavoidable if Ethernet stays; replaces chip + crystal + jack at ~496 |
| 83 passives | ~196 | with pads: 67 × 0402 at 1.5 = 100, 8 × 0805 at 4 = 32, 3 × 1206 at 6 = 18, 2 × 1210 at 8 = 16, 2 × 0603 at 2.5 = 5, and the 2512 shunt at 25 |
| SW1, SW2 | ~24 | recovery buttons; deletable if a pogo-pin jig is acceptable instead |


### Cooling

**A fan is not in the thermal budget and the board is not designed to need one.**
4.02 W against 4.80 W of free-air capacity is 1.19x at 25 °C ambient — and that
margin is gone by 45 °C, which an enclosure on a wall can reach without trying.
The enclosure is not designed. **J4** is the cheap insurance against finding that
out after the boards arrive: a 1x2 2.54 mm header across the 5 V rail, not fitted
by default, into which a mini fan plugs directly.

**It needs no switch, and that is a constraint rather than a convenience.** The
fan hangs on the 5 V rail unswitched, so it runs whenever the board is powered —
including at vSafe5V, where that rail is in pass-through and every milliamp costs
voltage directly. The binding limit is the 74AHCT541's **4.5 V** supply minimum:

| fan | R_DS typ (0.12 Ω) | 6 V-drive max (0.190 Ω) | **applicable max (≈0.24 Ω)** |
|---|---|---|---|
| none | 4.564 V | 4.515 V | **4.480 V** |
| 25 mm, ~50 mA | 4.554 V | 4.502 V | 4.465 V |
| 58 mA | 4.552 V | 4.500 V | 4.462 V |
| 30 mm, ~70 mA | 4.550 V | 4.497 V | 4.459 V |
| 40 mm, ~100 mA | 4.544 V | 4.489 V | 4.449 V |

Worked from the same inverted Eq. 1 as the dropout table above, **V_F term
included** — dropping it moves every row about 5 mV optimistic.

**The right-hand column is the one that decides, and it is under the floor in
every row including the empty one.** So the fan is not what puts the 74AHCT541
below its minimum; the converter's own worst-case on-resistance already does, and
a fan adds 15-30 mV on top. The honest bound is therefore about the *typical*
part: at 0.12 Ω every row clears by 44 mV or more, and a 25 mm fan costs 10 mV of
that. **Fit 50 mA or less** and treat the floor as a measurement to make on the
first board rather than a margin to spend.

**This bound is deliberately conservative, and it is worth knowing why.** Rail
architecture grades the same 4.5 V floor as non-fatal — the 74AHCT541 drives only
the status chain, so a marginal rail in the pre-negotiation window means the
indicators misbehave, not that the board fails to start. The floor is treated as
hard here because a fan is a part someone fits once and forgets, while the
vSafe5V case recurs on every plug-in; a 30 mm fan is not *forbidden*, it just
moves a documented-cosmetic failure from "never" to "every cold start on a laptop
port". At 36 V none of this
applies: the rail is a real buck off a 3.5 A converter and the fan is free. The
constraint exists only at the bottom of vSafe5V, where there is no heat to move
anyway — the board makes 2.3 W there.

**A larger fan would need a low-side switch, and there is no GPIO for it.** All
23 pads are assigned (see GPIO assignment), and EN_N cannot be strapped because
firmware has to pull it low for the PD chip to negotiate at all. An AO3400A
(C20917) was worked up for the job and is recorded in the knowledge base, but
fitting it means freeing a pin — realistically the UART0 console — and that is a
worse trade than choosing a smaller fan.

**The thermal protection does not depend on the fan.** The eFuses regulate their
own junction temperature and shut down on it, and firmware caps brightness from
the INA226. A fan that fails, or is never fitted, degrades performance rather
than safety.

**The PPTCs are already gone** — there is no PPTC line in the BOM, and this
section is the record of why. They were specified when the connector was 3 A; at
the picoMAX's 10 A the worst case is 28% of rating and their job evaporated. They
were also through-hole radial — bulky, and mass on leads in a vibration
environment. Dropping them saved 192 mm2 and two hand-soldered parts, and the
per-channel overcurrent role they half-filled is now the eFuse's.

## What the ERC will and will not catch

Worth knowing before the first run, because a clean result is easy to
over-read. The rule set covers structure, connectivity, voltage against pin
ratings, decoupling, I2C pull-ups and part-specific rules. Against *this*
design it has specific gaps:

- **`S6-shorted-two-terminal` was added for this board** — a two-terminal part
  with the same net on every pin. It catches the *degenerate* form of the shunt
  mistake: both of R1's pins literally written `GND`. **It does not catch the
  likelier form**, where J2/J3 pin 2 is called `GND` and R1 therefore sits
  between two legitimately different nets — that is a connectivity fact about a
  different component, and no rule inspects it. S6 does **not** cover that case,
  so the split return below remains a layout-discipline item with no automated
  backstop.
- **No I2C address-collision rule.** This design has already got that question
  wrong once (0x42 against the INA226), and the fix depends on pinning A0/A1 by
  hand — which the checker cannot see.
- **No do-not-fit / assembly-class flag**, and the netlist format has no field
  that could carry one — `value` holds `"0R DNF"` for R48, but CLAUDE.md is
  explicit that `value` is cosmetic and the importer ignores it. Three parts
  depend on a human reading it: **R48**, **J4** and **U3**. R48 is the one that
  matters most — a 0 Ω link from the 37.8 V bus onto a pin rated 33 V absolute,
  so fitting it destroys U1 and bypasses the whole follower. Marking all three
  is a step in *Next: from here to a board*, not a note someone has to find.
- **No logic-level-domain rule**, which is the exact failure the 74AHCT541
  exists to prevent.
- **Seven rules have no regression test**: S2, S4-malformed-lcsc, S5, K2, B1,
  Q1 and Q2. The four that would fire on a voltage mistake on *this*
  board now have cases. `P1-undervoltage` needed a part with a `vMin` to fire at
  all, and no record carried one: the 74AHCT541's VCC pin now does, at 4.5-5.5 V,
  which is the floor the whole dropout analysis is about. The rails file takes a
  `voltageMin` alongside `voltage` so the check sees the sag rather than the
  nominal, and `BUS_5V` declares **4.48 V** — the rail at the converter's
  applicable worst-case on-resistance, see Rail architecture.

  **And it does fire** — `P1-undervoltage [U9.20]`, the one warning in an
  otherwise clean report. 4.48 V is about 20 mV under the 74AHCT541's floor,
  with no fan fitted. That is the rule earning its place: the design had been reading its
  margin off the 190 mΩ figure, which is specified at a gate drive this converter
  does not use, and a check against the declared worst case is what surfaced it.
  The consequence is the one *Rail architecture* already reaches — indicators
  misbehave in the pre-negotiation window, the board still starts. E1, E2 and E3
  needed only the pin *types* those records already had.

One documentation gap feeding this: only the **module** datasheet for the
ESP32-C6 is cached. Every GPIO, strapping, ADC and peripheral claim beyond the
pin table rests on the chip datasheet and TRM, which `ds.py` cannot re-read.

## What the board does on each contract

The UVLO change made four bus voltages normal where one used to be. Three things
in firmware are still written against 36 V and have to follow:

| | at 36 V | at a lower contract |
|---|---|---|
| **Limiter divisor** | 5.4 | `V_negotiated × η / 5` — 3.0 at 20 V, 1.8 at 12 V |
| **INA226 alert threshold** | sized against 5.0 A | the contract current, which is 3 A on 12/15 V PDOs |
| **Status power bar** | 30 W per LED across 180 W | must scale to the contract, or it shows one LED on a 36 W contract whatever the wall is doing |

**The ramp no longer scales against the contract.** Inrush is `C_OUT × dV/dt` =
0.72 A per channel whatever the bus voltage, so 1.44 A for both — 29% of a
36 V/5 A contract and 48% of a 12 V/3 A one. No staggering, and the limiter only
has to hold the frame down until the ramp finishes — 165 ms as programmed,
longer once the thermal regulation loop takes over.

**Downward renegotiation is new and untested.** With UVLO at 28 V the channels
opened at 26.2 V on any 36 → 28 → 20 V transition, isolating the module banks.
At the eFuse's 4.28 V they stay closed throughout, so the modules' 6.6 mF now rides the
transition. The attach-time isolation argument still holds — at attach the bus is
5 V, and the inrush is dV/dt-limited in any case — but the renegotiation case is not
covered by it. What that changes for the 15 ms `tSnkNewPower` window below: firmware
has to cut brightness **into a live load**, because the eFuses no longer open to buy
it time. The conclusion there is unaffected — the window is shorter than it looks and
the FAULT interlock is the backstop — but it is now the only mechanism, not the
second one.

**The operator needs to know which contract was negotiated**, and no indicator
currently shows it. The document already notes that "voltage can be encoded in
colour if wanted"; with degradation as a headline feature that is a requirement
rather than an option.

## Layout constraints

For a board carrying 5 A next to a differential pair and an ADC, copper weight,
trace width and return path decide more than the area utilisation factor does.

**Copper weight: 2 oz (70 µm), and it is not a preference.** Trace widths per IPC-2221,
external layer:

| | 5 A bus | 2.80 A channel (worst case) |
|---|---|---|
| 1 oz, 20 °C rise | 1.82 mm | 0.82 mm |
| 1 oz, 10 °C rise | 2.77 mm | 1.24 mm |
| **2 oz, 20 °C rise** | **0.91 mm** | **0.41 mm** |
| 2 oz, 10 °C rise | 1.38 mm | 0.62 mm |

Both the feed and the return need it. On 1 oz a 10 °C-rise bus trace is 2.8 mm
wide, which on a 72 × 74 mm board with four committed edges is awkward; on 2 oz
it is 1.4 mm and routine. 2 oz (70 µm) also halves the copper's contribution to the FET
thermal path.

**The eFuse's PowerPAD is now the thermal path, and it is not optional.** The
TPS16630 dissipates 0.27 W steady per channel and far more during the ramp,
with the device regulating its own junction temperature — which it can only
do if the pad has somewhere to put the heat. TI's HTSSOP-20 PowerPAD wants a
soldered pour with thermal vias.

**D15/D16 and C34/C35 must sit within a few mm of the OUT pins.** §11.1 is about
lead inductance: a Schottky placed away from pin 18-20 adds its own loop to the
path it is meant to shorten and clamps nothing useful. Same for the 100 nF. This
is the one placement constraint on this board where the part does *nothing* if it
is in the wrong place, rather than working less well.

**The low-side shunt forces a split return, and nothing else in this document
says so.** R1 sits in the ground return so that both channels' current passes
through it before joining board ground. If the netlist calls J2/J3 pin 2 `GND`,
the shunt is shorted by the ground pour and **the current sense reads zero**. The
output connectors' return is therefore a net of its own, **`LED_RTN`**, joined to
board ground only at the shunt — and the connector pinout is written
`+36V / LED_RTN / A / B` everywhere in this document for exactly that reason.
`GND` is the name anyone writing the netlist would type, and it is the wrong one. **No ERC
rule catches this** - S6 sees a part shorted by its own two pins, not a connector
wired to the wrong net one component away - so it is a layout-discipline item
with no automated backstop.

On a 2-layer board that also means the bottom layer is **not** a continuous
ground plane under the SPI bus, the differential pair and the ADC divider. Either
accept that and route the return deliberately, or go to 4 layers — which the
area table already shows the board does not need for density.

**The ESP32-C6 module's antenna keep-out is a copper and placement constraint,
not an area one.** Its recommended footprint (datasheet Figure 10 p.30) marks an
**18 × 6 mm antenna area** that must be copper-free on all layers. That area lies
*inside* the 18 × 25.5 mm outline, so the mechanical table's 459 mm² already
contains it; the budget is not short by the keep-out.

What is still true is everything downstream: those 108 mm² carry **no copper on
any layer**, the module wants that edge overhanging the board, and with USB-C,
two angled picoMAX and the Ethernet module's RJ45 **all four edges are
committed** — meaning each edge carries something, not that any is full: those
four items total 75 mm of a 292 mm perimeter, 26%. On a 2-layer board the
keep-out punches a hole in the return path of a board carrying 5 A, which is a
routing problem rather than a space one.

## Thermals and the FET choice

Size and dissipation pull against each other: a TO-252 is cooled by the copper
around it, and a small board has less of it.

The TPS16630's integrated FET is **33 mΩ min / 45 mΩ max at T_J = 85 °C** — p.7's 85 °C row is the one with **no typ column** — against 26 / 30.44 / 34.5 mΩ at 25 °C and 19 / 30.44 / 53 mΩ over −40…+125 °C. The 31 mΩ on the front page is the headline figure and matches no row. At
2.43 A per channel it dissipates **0.27 W each**, 0.53 W for the pair. That is
0.31 W more than the discrete 9.5 mΩ FET plus 10 mΩ shunt it replaced, and it is
the price of the part — thermal headroom goes from 1.29x to **1.19x**.

**The ramp is the device's own problem now, by design.** There is no SOA
calculation to do and no power limit to set: the part regulates its own junction
temperature through the ramp, and TI characterises it powering up into **15 mF**
(Figure 16, at V_IN = 24 V), against the 3.3 mF per channel here. The energy is
½CV², and the two are **not** equal: TI's case is **4.32 J** against **2.14 J**
here, so this design sits 2.0x inside the demonstrated one. What changed is that
the datasheet takes responsibility for it.

The **PowerPAD carries that heat** and must be soldered to a pour — see Layout
constraints.

## Fault handling

**FAULT is wired as a hardware interlock, not just an interrupt — but it has to
be configured first.** The HUSB238A pulls pin 13 high "if the power adapter
cannot supply the required voltage or current, or if an OVP/UVP/OTP event is
detected" (datasheet p.5), and the chip senses that natively, so no software is
needed to *notice* it once the pin is doing that job.

The catch is that pin 13 is **FAULT/OUT2**, dual-function: p.5 says "the output
purpose can be configured as a FAULT pin" and "can also be configured as a
universal output pin (OUT2) via the I²C master". **The datasheet does not state
the power-on default.** If it is OUT2, the interlock does not exist until firmware
writes the register — which is exactly the cold-start window the interlock is
claimed to cover. Until that is confirmed with the part in hand, treat the
interlock as *available after I²C init* and the SHDN pull-downs as the only
fail-safe before it. Carried in Known electrical limits.

**Two** BSS138, Q1 and Q2, gates both on FAULT, sources to ground, and each
drain on **one channel's SHDN pin**. FAULT high turns them on, both SHDN pins go
low, both TPS16630s enter shutdown, and the LED load is shed regardless of what
firmware is doing.

**It has to be two transistors.** A MOSFET drain is a single node, so one device
tied to both SHDN pins would couple the two channels and either enable would then
disable *both* — destroying the independent enable the GPIO budget and
the per-channel shedding both depend on. Two devices, or a diode-OR into each
SHDN pin; two BSS138 at $0.027 is the cheaper of the two.

Supporting pieces:

- **Bus voltage sense**: **470 k / 27 k** divider into an ESP32-C6 ADC, plus
  **100 nF** at the tap. Gives **1.96 V at 36 V** and 2.72 V at 50 V, so events up to ~57 V are on-scale (3.26 V at 60 V is above the SAR's usable ceiling) —
  which matters because this divider is now the only overvoltage measurement the
  system has. The obvious 330 k/33 k pick gives 3.27 V, above the SAR ADC's
  ~3.1 V usable top at 12 dB, which pins the reading at full scale **at the
  normal operating point** — check the divider against the ADC ceiling, not just
  against the rail. The INA226 cannot do this job: its VBUS pin is specified
  0-36 V against a 36 V bus, so no margin. The 100 nF fixes the source
  impedance, which an ESP32 SAR wants nearer 10 kΩ — but 25.5 kΩ with 100 nF is
  **τ = 2.55 ms**, so this cannot see an OV event faster than ~10 ms. It is a
  slow check, not fast protection, which matters because it is carrying the OVP
  role the HUSB238A lost.
- **No hold-up capacitor.** Three versions of one were designed and all three
  failed. On the bus it discharges backwards into a collapsed source — there is
  no switch between VBUS and the bus — and its 29 ms figure assumed only the
  2.2 W rail load, where sharing the bus with 5 A of LEDs drains 36 V to 5 V in
  **0.62 ms**. Behind a series Schottky it isolates correctly but costs 0.35-0.45 V
  on every start, putting the TPS54360B under its 4.5 V minimum at the bottom of
  vSafe5V. **Deleted.** A PD collapse now takes the controller down with the LEDs,
  which is a recoverable event on an LED wall rather than a failure, and it
  removes the largest single contributor to the Type-C bypass-capacitance limit.
- PD renegotiation gives a sink ~15 ms (tSnkNewPower) to reduce draw. One frame
  at 90 fps is 11.1 ms, so firmware can react **only if it is already watching**;
  blocking on a network read would miss the window. The FAULT interlock is the
  backstop for exactly that case.

## Per-IC support networks

Every value here is datasheet-mandated, not a preference. A schematic missing
any of them does not work. Cited so they can be checked rather than trusted.

### TPS54360B (U4) — bus to 5 V

| Part | Value | Source |
|---|---|---|
| BOOT cap, BOOT→SW | 100 nF X7R ≥10 V | p.27 §8.2.2.7 "must be connected for proper operation" |
| RT/CLK to GND | **100 kΩ 1% → 963 kHz** | p.14 §7.3.9 — the pin **cannot float**. The equation gives 963.3 kHz; "1 MHz" elsewhere is that rounded |
| Feedback divider | 53.6 kΩ / 10.2 kΩ 1% (VREF 0.8 V) | p.28 §8.2.2.9 |
| COMP network | ~4.7 kΩ + 33 nF series, 150 pF parallel | p.13 §7.3.5, eq. 44-51 |
| CIN | ≥3 µF **effective after DC-bias derating**, 100 V X7R | p.26 §8.2.2.6 |
| COUT | **≥7.3 µF** at this load | p.25 Eq. 32 at a 0.7 A step and 0.2 V of droop. §8.2.2.4's own "29.2 µF" and its 2 × 47 µF fit are for a 1.75 A step, and that section contains no "22 µF" despite being commonly cited for one. Eq. 33 gives 2.4 µF and Eq. 34 2.3 µF, so Eq. 32 binds. C10/C11 are 2 × 10 µF |

PowerPAD must be **electrically** connected to GND, not just thermally (p.3).
EN is left floating deliberately: abs max 8.4 V (p.4), and a UVLO divider would
sit at ~9.5 V on a 36 V bus — over it. Floating gives the internal 4.3 V UVLO.
No soft-start or PWRGD pin exists on the DDA package.

### SY8089 (U5) — 5 V to 3.3 V

| Part | Value | Source |
|---|---|---|
| Feedback divider | 100 kΩ / 22.1 kΩ 1% (VREF 0.600 V) | p.2, Table 1 p.7 |
| CIN | ≥10 µF ceramic | p.2 and p.7 |
| COUT | **≥22 µF** | p.1 selection table, validated against the 2.2 µH L2 |
| EN pull-up | 100 kΩ to 5 V | p.2 "Do not leave it floating" |

### Ethernet module (U3) — W5500 on a daughterboard

**The board carries a finished W5500 module, not a bare W5500.** The brief said
"W5500" and this design first read that as the chip, which put the PHY front end,
a 25 MHz crystal and an RJ45 with magnetics on the controller board. A module
removes all three.

What that retires, and this is the point rather than the cost saving:

| Gone | Why it mattered |
|---|---|
| EXRES1 12.4 kΩ 1%, TOCAP 4.7 µF, 1V2O 10 nF, RSVD tie, 4 × LED resistors | six values that all had to be exactly right |
| 25 MHz crystal | **CL mismatch was a blocking open question** — the part on hand was 20 pF against a required 18 pF |
| HR911105A and its centre taps | **the TCT/RCT bias network is undocumented** in the W5500 datasheet — the second blocking open question |
| Differential pair routing to the jack | the only controlled-impedance work on an otherwise forgiving board |

Both of those open questions were unresolvable here without WIZnet's reference
schematic. On a module they are the module vendor's problem, already solved in
volume.

**Interface: six signals plus power**, which is exactly what the controller
already budgets — the GPIO assignment does not change.

| Signal | Note |
|---|---|
| SCLK, MOSI, MISO, SCSn | SPI, mode 0, up to 80 MHz |
| RSTn | active low, **≥ 500 µs**; keep the pull-down that holds the PHY in reset while the MCU boots |
| INTn | open-drain interrupt |
| 3V3, GND | the module regulates its own 1.2 V core internally |

Two things a firmware author needs that the table above does not carry: after
RSTn returns high the W5500 runs an internal auto-configuration and **the host
must wait 50 ms** before talking to it (WIZ850io p.2, `TPL ≤ 50 ms`), and the
part accepts **SPI mode 0 or mode 3** — naming only mode 0 would send someone
down a clock-polarity rabbit hole.

**Soldered down, not socketed.** This is a soundwall: the same vibration argument
that chose picoMAX with a latch over screw terminals applies to a stacked
daughterboard. Pin headers soldered through both boards, no receptacle.

**Pin map, confirmed against the module on hand.** Two 1×6 headers, taken from
the WIZ850io datasheet p.2 and checked against the clone's silkscreen:

WIZnet calls the module's two headers J1 and J2, which collide with this board's
own J1 (USB-C) and J2/J3 (picoMAX). They are written **MJ1** and **MJ2** here.

| | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| **MJ1** | GND | GND | MOSI | SCLK | SCSn | INTn |
| **MJ2** | GND | 3V3 | 3V3 | NC | RSTn | MISO |

The clone reads as `INT CS SCK MO G G` and `G V V NC RST WT`. The first of those
is MJ1 right-to-left, which is what reading both headers left-to-right across the
board produces — **MJ1 and MJ2 sit on opposite edges, so they run in opposite
directions.** Both readings are therefore consistent with the table above. MJ2
position 4 being **NC** is not the discriminator it looks like. On the WIZ550io, RDY is **MJ2-3**, MJ2-4 is nRESET, and it is
a **16-pad** part with MISO on MJ1-4 — so the real discriminator is **pad count,
12 against 16**, which a 12-pad clone settles outright. Note that pad count alone
does not exclude the **WIZ820io**, which WIZ850io p.1 says it is hardware
compatible with and which carries a **W5200** — a different register map. Settle
that one by chip marking, or by reading `VERSIONR` at bring-up: the W5500 returns
**0x04**.

**`WT` is MISO by elimination, not by datasheet.** Twelve pads; eleven are
identified, and the only mandatory SPI signal left unexposed is MISO — a W5500
module without it would be useless — and it sits exactly where both official
variants put it. That is sound but weaker than a datasheet line, so it carries
provenance `inferred` in the knowledge base — by a third route neither of the
two obvious ones took. Downgrading `provenance.pins` to
`inferred` would have lost the severity on eleven properly sourced pins; splitting
the record would have duplicated it. Instead a *pin* may carry its own `source`,
and `erc.py`'s `unverified()` prefers it over the record-level field when the rule
is about that pin. **Pin 7** — the symbol's number for the pad silkscreened
J2-6 — carries one; the other eleven stay at full severity. Verified rather
than assumed: `unverified()` returns True for pin 7 and False for pin 3 and for
the record as a whole.

Fixing that exposed a second bug in the same function. It matched the whole
provenance string against the set of weak markers, so `"inferred - by elimination,
the only SPI signal left"` — an honest annotated guess — did not match `inferred`
and was treated as datasheet-backed. It now reads the first word. Both are
covered by cases in `tests/test_erc.py`.

**Neither WIZnet's documentation nor the LCSC datasheet states which physical
end is pin 1** — the wiki page carries a pinmap image and links an external
dimension PDF, and the LCSC file is an export of that page. So the handedness of
the clone against the official footprint cannot be settled on paper.

It can be settled with a multimeter in under a minute, with no power applied.
**All grounds are common**, so ringing MJ2 position 1 against the two G pads on
MJ1 identifies which end of MJ1 is the ground end — and that, with the signal order
already read off the silkscreen, fixes the orientation completely.

**And the layout should make a mismatch visible rather than invisible.** Put the
signal name next to every pad on the controller's silkscreen — `MOSI`, `SCLK`,
`SCSn`, `INTn`, `RSTn`, `MISO`, `3V3`, `GND`. Then at assembly the two
silkscreens are read against each other and a mirrored module is obvious before
it is soldered. This costs nothing and converts the one error class that survives
fabrication into one that cannot survive assembly.

**This does not block the netlist.** The netlist keys `pins` by **pin number**,
not by pin name, and those numbers keep meaning those signals whichever way
round the board is. A mirrored clone needs a **mirrored footprint** so pad 1
lands on its ground end; the netlist is unchanged. Footprint work, not netlist
work.

The numbers themselves are the symbol's 1-12, read with `tools/eda.py` — and
the mapping is not the one the silkscreen suggests. **MJ2 runs 12→7**, so
MJ2-1 (GND) is pin 12 and MJ2-6 (MISO) is pin 7. All six names agree on that
direction; the obvious reading, MJ2-1 = 7, agrees on none of them.

**Symbol, footprint and 3D model come free, via the official part.** The clone
has no EasyEDA library entry, but it does not need one: put **C134462** — the
WIZnet WIZ850io — in the netlist as the `Supplier Part`, and
EasyEDA resolves the symbol, the footprint and the 3D model from its own library.
The clone drops into that footprint because it is the same one.

**Mark it do-not-fit for assembly.** The netlist exists to place the part on the
schematic; it must not end up on a JLCPCB PCBA order, or they will fit a $22.89
WIZ850io where the €12.30 JOY-IT clone in the sourcing table was intended. Treat it like J2/J3, which are in the
BOM but hand-fitted.

**EasyEDA does carry C134462**, verified rather than assumed: `./tools/eda.py
device C134462` returns symbol `WIZ850IO` and footprint `RJ45-TH_WIZ850IO`
from the Pro library — a through-hole footprint that includes the jack. So the fallback of drawing a 2 × 1×6 header footprint by hand is not
needed.

**Area is not the reason to do this.** The module is ~25 × 23 mm = 575 mm²
against roughly 496 mm² for the chip, crystal, jack and support passives, so the
board gets slightly *bigger*. The win is the two retired open questions, the six
retired exact values and the routing.

### ESP32-C6-WROOM-1 (U2)

| Part | Value | Source |
|---|---|---|
| EN RC | 10 kΩ to 3V3 + 1 µF to GND | Figure 7 p.28 "an RC delay circuit must be added" |
| GPIO8 pull-up | 10 kΩ | Figure 7 (R8). GPIO8 has **no internal pull**, and GPIO8=0 with GPIO9=0 is an invalid strap (Table 7 p.13) |
| GPIO15 | external resistor to define the strap | §3.3.4 p.14 — must not be high-Z |
| 3V3 bulk | **22 µF + 0.1 µF** | Figure 7 p.28 |

### HUSB238A (U1) — the PD controller

Absent from this section until a review pointed out that the one IC the whole
design is built around had no entry.

| Part | Value | Source |
|---|---|---|
| **VDD (pin 5) to 3V3** | tie it, do not leave it on VBUS alone | p.4: VDD is an **input supply**, "recommended to tie this pin to … a 3.3 V power rail". This is what makes the follower's drop survivable — see below |
| VDD decoupling | **1 µF ceramic** | p.4, pin 5 — and an error-severity rule in the part's KB record |
| D14, 5V rail → VBUS pin | **RB751V-40** (C7502691) | lifts the VBUS pin at vSafe5V; reverse-biased once the follower takes over |
| ADDR, DEBUG_N | 910 kΩ each | p.4-5 calls for 900 kΩ, which is not an E96 value; 910 kΩ is the nearest stocked one. Keeps standby current low |
| INT_N pull-up | 10 kΩ | p.5, open-drain |
| Q3 follower base pull-up (R44) | **3.24 kΩ** to VBUS | same source, but **not** the vendor's 10 kΩ: see the PD front end. Nothing here is a pull-*up* on the FAULT interlocks — that is Q1/Q2 sharing one 10 kΩ pull-**down** (R15) on `PD_FAULT`, because FAULT/OUT2 is push-pull and a pull-up would hold both SHDN pins low and the channels off |
| Follower bypass link | 0 Ω, **do not fit** at 36 V | same source — it exists for ≤28 V builds |
| EN_HVDCP/OUT1 (pin 7) | **910 kΩ to GND** | p.11 Table 7: GND via 900 kΩ = BC1.2 only, and 910 kΩ is the nearest stocked value — E24, since E96's neighbour is 909 kΩ. Floating would enable HVDCP detection, which is pointless with D+/D− unconnected |
| FLGIN (pin 14) | **tie to GND** | p.7: a digital input (VIH 2 V / VIL 0.8 V) that cannot float. p.5 gives it **two** functions — disabling the GATE driver *and* raising an INT_N interrupt on a valid high voltage — and either can be configured alone. Neither is wanted here; p.5 Table 1 makes HIGH the assert level, so GND is the inactive state. **The vendor leaves this pin open in all three application figures**, so tying it low is a deliberate deviation, taken because an unterminated CMOS input is not a state |

Pin 17 is the exposed pad and the **only** GND connection.

**GATE (pin 15) is left unconnected.** Its job is driving an external VBUS
switch, and there is no longer one — see Channel switching and inrush. FAULT into
the interlock transistor pair covers load shedding instead, by pulling both
TPS16630 SHDN pins low.

**D+/D− (pins 1-2) are left unconnected.** The chip drives BC1.2 pull-ups onto
that pair for legacy charger detection, and the pair belongs to the ESP32-C6's
native USB — sharing them would break enumeration. We only want PD, which runs
on CC1/CC2, so the legacy detection is given up deliberately.

### TPS16630 (U10, U11) — the channel eFuses

Two identical instances. Derivations are in Channel switching and inrush; this
table is the schematic checklist.

| Part | Value | Source |
|---|---|---|
| R_ILIM, ILIM→GND | **3.24 kΩ** 1% | §10.2.2.1 — sets the 5.56 A overload limit |
| C_dVdT, dVdT→GND | **220 nF** | §10.2.2.3 — sets the output slew, and so the inrush |
| UVLO/OVP string | **732 kΩ / 255 kΩ / 30.0 kΩ** 1% | IN → R_UV1 → **UVLO** → R_UV2 → **OVP** → R_UV3 → GND, 1.2 V on both pins. UVLO is the **upper** tap — swapping them gives a part that never turns on |
| C_IN, IN→GND | **220 nF** ≥100 V | p.6 Recommended Operating Conditions: 0.1 µF **minimum** at IN, P_IN and OUT. Oversized because a nominal 100 nF ±10% meets a minimum with zero margin |
| SHDN pull-down | **10 kΩ** to GND | active-low shutdown. 100 kΩ sits at 0.77 V against a 0.8 V threshold against the pin's own source — see Enable |
| SHDN series from GPIO | **1 kΩ** | lets the FAULT transistor win without shorting the GPIO |
| **P_IN (pin 6) to IN** | direct, no element | p.5 Pin Functions: "Always connect P_IN to IN directly" |
| **GND (pin 9)** | wired, **in addition to** the PowerPAD | p.5: "Do not use PowerPad as the only electrical connection to GND" |
| PGOOD pull-up | **10 kΩ** to 3V3, shared | open drain |
| C_OUT, **OUT → GND** | **220 nF** ≥100 V | p.6 Recommended Operating Conditions: 0.1 µF **minimum** at IN, P_IN *and* OUT. C34/C35, oversized so the minimum survives tolerance and DC bias |
| Transient Schottky, **OUT → GND** | **MBRS3100T3G**, cathode to OUT | §11.1 p.28: interrupting current drives OUT negative against a −0.3 V absolute maximum. D15/D16. Place within a few mm of the pin — lead inductance is what the section is about |
| MODE | **leave open** = latch off | p.24 Table 1: open latches off after `tCL_PLIM(dly)`, GND auto-retries after `t(TSD_retry)`. Latch is chosen because firmware already owns SHDN and reads PGOOD, so it can implement a retry policy with back-off and reporting — where auto-retry would re-pulse 13 W per channel blindly into a board that is already hot. Reset by toggling SHDN |

**IMON is unused.** The part outputs a current-proportional voltage per channel,
which is exactly the per-channel sensing the shared low-side shunt cannot give —
but there are no spare ADC pins. Worth revisiting if the GPIO budget ever loosens.

The **PowerPAD must be soldered to a ground pour** — it is the thermal path the
part's own temperature-regulation loop depends on — **but it is not the ground
connection**: pin 9 must be wired as well, which the datasheet states outright.

**FLT (pin 15) is left unconnected, and that is a compromise worth naming.**
PGOOD alone cannot tell a fault from a commanded shutdown, since it goes low for
both, and the two PGOODs are wire-ORed onto one pin — so the single bit firmware
gets is ambiguous twice over. Per-channel FLT would cost two GPIOs the budget
does not have. Recorded in Known electrical limits rather than left silent.

### Tie-offs that are inputs, not options

- **74AHCT541**: OE0 (pin 1) and OE1 (pin 19) are active-LOW enables and must go
  to GND, or every output stays high-Z. The seven unused A inputs must be tied.
- **INA226**: A0 and A1 must each be tied to GND/SCL/SDA/VS (p.3) — they are
  inputs. **Tie both to GND**, giving address **0x40**. This is not free choice:
  the HUSB238A sits at 0x42, which is also a legal INA226 address (A1 GND, A0
  SDA), so the no-clash argument in Known electrical limits depends on pinning
  this one here. VBUS (pin 8) cannot float either; tie it to 3V3 and treat the bus and
  power registers as unused, since bus sensing is done by the ADC divider.
- **MAX3485**: DE to VCC **and RE to VCC** — RE is a CMOS input, "unused" is not
  a state. 0.1 µF at each VCC. **DI needs a pull-down**, so the line idles low
  and GPIO4/GPIO5 have defined strap levels through MCU reset (MTMS=0/MTDI=0 is
  a valid SDIO combination).
- **The driver's edge rate is unlimited, and that is a radiated-emissions choice
nobody made.** p.5 gives t_TD 5 ns typ / 20 ns max and p.1 says it outright: "the
driver slew rates is not limited". A 5 ns edge has a ~100 MHz knee and occupies
about 1 m of cable, so an 11 m run is 11 rise-lengths long and a round trip 22 — on a pair this
document's own ESD section calls "an antenna". The signal's narrowest feature is
400 ns, so none of that speed is needed: a slew-limited 2.5 Mbps-class part would
still place a 400 ns pulse comfortably and cut radiated emissions by roughly an
order of magnitude. **Series resistors at the driver outputs are the cheap
partial mitigation** and should be fitted whether or not the part changes.
Reflections are secondary — one far-end termination is the right topology — but a
pair pulled from generic 4-conductor cable is 80-120 Ω at best, and a partial
reflection returns ~110 ns after the edge, inside a 400 ns pulse.

**Fail-safe is a converter-board problem.** "Failsafe biasing is not needed,
  since DE is permanently asserted and the pair is never idle-floating" is true of
  the **driver** end and only that end. The fitted part's fail-safe is **open-circuit only** (p.1,
  p.5 function table, p.9) with V_TH = ±200 mV and RO undefined inside that band
  — and a *terminated but undriven* pair is not an open circuit: the far end's
  own 120 Ω holds it at ~0 mV differential, squarely indeterminate. Two cases are
  uncovered: controller brownout or power loss, where the modules' 3.3 mF keeps
  the converter boards alive for milliseconds with a chattering receiver driving
  their LED chains, and the cable unplugged at the controller. The fix belongs on
  the **converter board** — a 560/120/560 bias divider, or a true-fail-safe
  receiver such as SN65HVD75 (C57928), which the KB rejected over $0.24.
- **Channel enable**: SHDN takes a **10 kΩ pull-down** and the GPIO drives it
  through **1 kΩ** in series. Through reset and the whole boot window, when the
  GPIOs are high-Z, the pull-down holds both channels off. Firmware drives the
  GPIO **high to enable** — not inverted. See Enable for why 100 kΩ is not
  enough and why there is no transistor in this path.
- **HUSB238A ADDR**: tie to **GND (0x42)**, not VDD. Mode and address latch at
  power-up, when the 3.3 V rail does not yet exist — so a VDD tie reads as GND
  anyway, or worse is ambiguous against the float-means-GPIO-mode state.
- **SK6812 chain**: ~500 Ω series resistors on the data line and 100 nF per LED.
  The datasheet figure puts one at the chain's DIN and one at its DOUT, because
  the chain in that figure continues to another board. **This one ends at LED8**,
  so a resistor on LED8's DOUT would damp nothing. The second one goes on the
  3.3 V side instead, between GPIO15 and the buffer input — a real trace, and
  it keeps the GPIO behind a series element. Writing the netlist is what forced
  this to be decided; "in and out of the chain" had been carried for several
  revisions without anyone asking what the second one terminated into. The *value* is from the **WS2812D-F8** p.5 (`104`); SK6812MINI-E p.9 shows
  one capacitor per LED with no value and says only that "the decoupling
  capacitance between each LED is essential". The 500 Ω series figure on that
  page is verbatim. Note the **SK6812-EC20** fitted here specifies VIH as
  **0.6 × VDD** — the most permissive of the three SK6812 variants
  evaluated, and not the 0.7 × VDD that applies to the WS2812D-F8.
- **INA226 ALERT** is worth more than an interrupt line. Configured as a
  shunt-overvoltage comparator it fires within one conversion — **140 µs to
  1.1 ms** — which is faster than the eFuse's own fault response and far
  faster than firmware polling. It gives no per-channel discrimination, since the
  shunt is in the shared return, but it is the only sub-millisecond hardware *detection*
  path on the board — the shed itself is still firmware driving SHDN, because
  ALERT lands on a GPIO and not on the interlock. Treating it as "a slow
  safety net" undersells it.
- **INA226 ALERT** is open-drain (p.3) and needs a **10 kΩ pull-up** — fitted as
  **R45**. The INA226 record carries a pin map with pin 3 typed
  `open_collector`, so `E3-missing-pullup` **does** check this one.
- **TPS16630 PGOOD** outputs are open-drain; the two are wired together into
  one GPIO with a **10 kΩ pull-up**.

### Still unresolved in this section

- The Ethernet module's handedness, which no datasheet settles — resolve it with
  a ground-continuity check and a labelled silkscreen. See Ethernet module. It
  affects the footprint, not the netlist.
## GPIO assignment

The module exposes **exactly 23** GPIO pads (datasheet Table 3, pp.10-11):
0-13, 15-23. GPIO14 does not exist on this package; GPIO24-30 serve the internal
QSPI flash and reach no pad.

The design needs **20**, counting the status chain and the bus-voltage ADC that
a quick tally leaves out:

| Signal | Count |
|---|---|
| W5500 SPI: SCLK, MOSI, MISO, SCSn | 4 |
| W5500 RSTn, INTn | 2 |
| I2C SDA, SCL | 2 |
| HUSB238A INT_N, EN_N | 2 |
| INA226 ALERT | 1 |
| Channel data to MAX3485 DI x2 | 2 |
| Channel load-switch enables x2 | 2 |
| SK6812 status chain | 1 |
| Bus-voltage ADC divider | 1 |
| USB D-/D+ (GPIO12/13, fixed) | 2 |
| TPS16630 PGOOD, both channels wire-ORed | 1 |
| **total** | **20** |

**Seven pins carry constraints**, though three of them can still carry signals.
GPIO4 and GPIO5 are MTMS/MTDI straps (§3.3 p.11-12) carrying the MAX3485 DI
lines — benign, because they only select the SDIO sampling edge, which is unused.
The cost is that external JTAG is no longer available; see Recovery and debug.
GPIO12/13 are the USB pair and are unavailable. GPIO8 must read high at reset and
GPIO15 must not be high-Z — both usable if the signal's idle state matches the
required strap, which the assignment below exploits. GPIO9 wants the BOOT button.
It fits at all only because of those two strap-sharing tricks — and the one pad
they bought has since gone to the shared PGD line, so the count below is exact
with nothing left over.

A workable assignment, which also shows how tight it is:

| GPIO | Signal | Note |
|---|---|---|
| 6, 7 | I2C SDA, SCL | LP_I2C pins |
| 18, 19, 20 | W5500 SCLK, MOSI, MISO | |
| 8 | W5500 SCSn | the mandatory 10 kΩ strap pull-up doubles as deselect-at-reset |
| 21, 22 | W5500 RSTn, INTn | RSTn needs a pull-down to hold the PHY in reset while the MCU boots |
| 23, 10 | HUSB238A INT_N, EN_N | |
| 11 | INA226 ALERT | |
| 4, 5 | MAX3485 ch1/ch2 DI | |
| 0, 1 | channel enables | drive SHDN through 1 kΩ. The **10 kΩ pull-down** (R13/R14) sits on SHDN, not on the GPIO — the far side of the series resistor — and holds the channel OFF through MCU reset. High enables. Costs the 32.768 kHz crystal option |
| 15 | SK6812 chain | a pull-down gives the right idle state. With factory eFuses GPIO15 selects the JTAG source and is **ignored**, so no strap level is required — only "not high-Z" |
| 3 | bus-voltage ADC | GPIO2/3 are the **only** ADC pins free of strap, JTAG or 32 kHz conflicts |
| 12, 13 | USB D-/D+ | fixed |
| 9 | BOOT button | |
| 16, 17 | UART0 console | |
| **2** | **TPS16630 PGOOD, both channels** | open-drain pair wired together with a 10 kΩ pull-up |

**No spare GPIO left.** The last free pin carries the shared PGD line, so the
budget is 20 signals on exactly 23 pads once BOOT and UART0 are counted.
**The PARLIO clock-pin worry that used to sit here is resolved and was the wrong
worry.** `clk_out_gpio_num = -1` is accepted from IDF v5.3 onward, so no pin is
at risk and the PGOOD line is safe. (It would not have been a usable fallback
anyway: one shunt in the shared return cannot tell which channel failed.)

What is real is the other direction. The ESP32-C6 has exactly **one** PARLIO TX
unit, not two — so the two channels are one unit at `data_width = 2` driving two
GPIOs with time-aligned bit streams, sharing a clock, a DMA stream and a
start/stop. Unequal chains must be zero-padded to the longer one. RMT is not an
alternative for both: the C6 has only **2 RMT TX channels**, which cannot carry
two data channels *and* the SK6812 status chain. `data_width = 4` on the single
PARLIO unit is the clean answer if the status chain ever needs to join them.

## Recovery and debug

### Flashing: plug in the USB-C port, same as a devkit

GPIO12/13 carry the ESP32-C6's **native USB Serial/JTAG** and go straight to the
connector's D+/D−, so a host sees the chip without a bridge chip and without
buttons. A factory-fresh part enumerates from ROM; after that `esptool` puts it
into download mode over the same link. The HUSB238A's own D+/D− are deliberately
unconnected, so nothing contends for the pair.

**Bus-powered current is the one number that differs from a devkit**, because
this board reaches 3.3 V through two conversions rather than one LDO:

| | from USB 5 V | legacy USB-A (500 mA) |
|---|---|---|
| Flashing — WiFi off, Ethernet idle | **~80 mA** | 16% |
| Full operation — WiFi TX peak + Ethernet | **~464 mA** | **93%** |

Flashing is trivially within any port. Running the whole board from a laptop is
not: 464 mA against a legacy 500 mA port is 93%, and that is before the status
LEDs. On a USB-C host the default Rp advertises at least 1.5 A and it is a
non-issue; on an old USB-A port, bring up WiFi *or* Ethernet, not both.

**The LED channels come up on USB power. How much light that buys is a
different question.** The TPS16630 operates from **4.5 V** and vSafe5V is
4.75-5.5 V, so the channels switch on from a plain laptop port. The port's budget
minus the controller leaves

    15 W port − 2.3 W controller = 12.7 W

at the connector — but that 12.7 W is **not convertible into light**, because the
module's XL1509 needs 6.5 V in to regulate and gets at most 5 V. It runs as a
saturated pass-through instead, so the module rail is the bus minus the switch
drop at whatever current flows. At the full 2.5 A the drop is the datasheet's
1.2-1.4 V and the module rail lands near **3.3 V** — at or below the WS2812D-F8's
**3.5 V** supply minimum. The LEDs do not work there.

**At low current they do, and there is more light in it than that sounds.** The
drop falls with current. The datasheet characterises VCE at one point only, so
the rest is a model — `VCE ≈ 0.30 + 0.45·I`, fitted to that point — carried here
with the board's own 55 mΩ, a metre of 0.5 mm² cable and the module inductor's
DCR:

| module current | rail at a 5.0 V bus | at the 4.75 V corner | share of full white | perceived |
|---|---|---|---|---|
| 0.6 A | 4.33 V | 4.08 V | 28% | **56%** |
| 1.0 A | 4.08 V | 3.83 V | 46% | **70%** |
| 1.5 A | 3.76 V | 3.51 V | 69% | 85% |
| 2.16 A | 3.35 V | 3.10 V | 100% | **dark** |

The WS2812D-F8 needs **3.5 V**, so the rail holds up to about **1.0 A** even at
the bottom of the USB-C tolerance — **one module at roughly 70% perceived
brightness**, since halving power costs far less than half the apparent light.
Past that the blue channel loses its driver headroom first, so the failure mode
is a warm colour cast before it is darkness.

**The binding constraint is not the port.** 12.7 W would be 2.5 A at 5 V and the
port would supply it; the module's own buck is what stops it, two rows below
where the LEDs give up. Reasoning from the port budget alone lands on "one module
at 88% of full-white power" — the last row of that table, where the rail has
collapsed and nothing lights. The model above is fitted to a single datasheet point
and wants a measurement before any of it is quoted as fact.

**That is still the difference between developing against a dark board and a lit
one**, which is what matters here. But the honest floor for *full* operation is
the **9 V PDO** — the lowest contract that clears the module converter's 6.5 V
input — not vSafe5V.

**This is why the channel switch changed.** The LM5069 it replaced needed 8 V to
operate, so a laptop port powered nothing downstream whatever firmware did. The
TPS16630 lets the controller run, the channels switch and a module glow from the
same cable that flashes the board.

**A laptop port and a laptop charger are different things.** A MacBook *sinks*
100 W or more; as a *source* its USB-C port offers 5 V only, up to 15 W on the
first port. Its charger is what has the high-voltage PDOs — the 140 W one carries
28 V, which is an EPR fixed PDO.

Measured against 20 modules, where full white is 288 W. Note the 175 W columns
are module power at the *connector*: with the 10 m reference cable the two runs
burn 8.1 W on top, which is what puts the 5 A contract at 5.2 A — so the firmware
limiter's target is contract current measured at the shunt, not 175 W of module
power. See Known electrical limits.


| Source | PDO | for LEDs | perceived brightness |
|---|---|---|---|
| MacBook port, as a source | 5 V / 15 W | ~5 W usable | **~70% of one module** — the module buck cannot regulate below 6.5 V in, so the rail sags with current and caps it near 1.0 A, well before the port's 12.7 W. See Recovery |
| 9 V PDO | 8.55–9.45 V | 22 W | **~31%** — 9 V × 3 A less the 5 W controller, on the same basis as the row below. The lowest PDO that clears the module converter's 6.5 V input, so the first one that produces full-colour light |
| 12 V PDO | 11.4–12.6 V | 31 W | ~36% |
| Apple 96 W charger | 20.5 V / 4.7 A | 91 W | **59%** |
| Apple 140 W charger | 28 V / 5 A | 135 W | **71%** |
| 180 W EPR charger | 36 V / 5 A | 175 W | 80% |

**The gamma curve is what makes this work.** Perceived brightness follows γ = 2.2,
so halving the electrical power costs only about twenty percentage points of
apparent brightness: 91 W against 175 W is 52% of the power and 59% against 80%
of what the eye reports. That is the whole argument for degrading rather than
refusing — and it is what the old 28 V UVLO threw away.

Full brightness on 20 modules still needs a 180 W EPR source and a 5 A e-marked
cable. Everything below that now dims instead of going dark.

**No PD contract forms on a PC port.** The HUSB238A presents Rd and stays idle
until firmware pulls EN_N low; a non-PD host simply never answers, and the bus
stays at vSafe5V. Nothing needs disabling to flash.

### Recovery

**Fitted.** Native USB Serial/JTAG covers normal flashing, but it lives on
GPIO12/13 and disappears the moment firmware reconfigures them or the chip hangs
before USB enumerates — so the board carries the fallback Espressif's reference
shows (Figure 7, p.28): **SW1** on GPIO9 (BOOT), **SW2** on EN (reset), and
**TP1-TP3**, bare pads on UART0 TX, RX and GND. The three pads are drawn by
hand after import; `UART_TX` and `UART_RX` are in the netlist with a scoped
`C1-single-pin-net` suppression so they survive as named nets to attach to.

Also not yet specified as parts rather than prose: four M3 mounting holes and
three fiducials (the vibration section argues for standoffs at several points),
and the module's header footprint and mechanical retention.

## Thermal budget

At the 72 x 74 mm envelope the board is 53.3 cm2.

| Source | W | Note |
|---|---|---|
| ESP32-C6 (TX peak) | 1.26 | 382 mA at 3.3 V, module datasheet |
| W5500 module | 0.50 | module draws the same as the bare PHY |
| TPS54360B catch diode | 0.30 | 0.60 A average at 0.7 A out, conducts 86% of the cycle |
| TPS54360B IC | ~0.5 | conduction + switching at ~1 MHz |
| 2x TPS16630 FET | 0.53 | 45 mΩ max at 85 °C, 2.43 A each |
| 2x TPS16630 IQ + dividers | 0.13 | 1.7 mA max at 37.8 V, plus the 1 MΩ strings |
| shunt 5 mΩ | 0.125 | at full 5 A |
| SY8089 + inductor | 0.19 | |
| 10 µH inductor DCR | 0.04 | 72 mΩ at 0.7 A |
| MMBT5551 follower + 3.24 kΩ + D1 | 0.11 | 8.4 mW collector at the 800 µA the pin draws with VDD tied; the 27 V clamp draws 2.78 mA through R44, so 25 mW in the resistor and 75 mW in the Zener |
| 2x MAX3485 driving 120 Ω | 0.06 | DE tied high, line never idle |
| 74AHCT541 | 0.02 | |
| R2 bus bleeder | 0.13 | 36 V across 10 kΩ, continuous |
| D15/D16 leakage | ~0.004 | 2 × 50 µA at 25 °C and 36 V, which is 36% of the part's 100 V rating. A reverse-biased diode drawing µA self-heats by nothing, so it sits at ambient and the 5 mA 125 °C figure does not apply. Two SS36 here would have been 0.036 W |
| status LEDs, capped | 0.12 | eight at one colour, 25% — see below |
| **total** | **4.02** | TVS leakage not counted; D15/D16 are |

The rows sum to 4.02 W against **4.80 W** of capacity at 0.09 W/cm² over
53.3 cm², so **1.19x headroom** — down from 1.29x. The eFuse is most of that
change but not all of it, and two of the five terms pull the other way: of the
0.31 W added, **+0.31 W** is the eFuse's 45 mΩ
max against the 9.5 mΩ discrete FET plus a 10 mΩ shunt it replaced, **−0.04 W** is the
lower-DCR inductor, **+0.04 W** is the PD follower (the 27 V clamp draws less than
the 20 V one did, but the NPN's 3.24 kΩ base pull-up draws considerably more than
the 10 kΩ it replaced), **−0.125 W** is halving R1 to 5 mΩ, and **+0.12 W** is the status-LED row, which the 3.71 W figure simply
did not count. The other four net to −0.005 W, so the eFuse alone would give
the same 1.19x — a coincidence of this revision, not a reason to stop counting
them. And that is
at **25 °C ambient**. Inside an
enclosure on a soundwall it is worse; at 45 °C ambient the margin is gone. This
still needs resolving before layout, but the power path is no longer the reason:
the switched path is 0.66 W, and the two largest rows are the MCU and that path.

### Status LEDs: why addressable, and why capped

**They are not plain SMD LEDs.** Each SK6812 contains a controller and
three 12 mA constant-current drivers, which is where the power goes — and what
buys the thing the GPIO budget cannot otherwise afford: **eight indicators on one
pin.** Eight discrete LEDs would need eight pins, and the assignment table has
none spare. (An I²C LED driver on the bus that already exists would also cost
zero pins and about a twentieth of the power, at the cost of colour. Not taken,
but it is the alternative if the budget ever tightens.)

**The brightness cap is a hard firmware requirement with a number**, not a
preference:

| Case | Power | |
|---|---|---|
| all eight, three colours, 100% | 1.44 W | above the datasheet's own limit |
| all eight, three colours, **70%** | **1.01 W** | the datasheet's ceiling (p.3: "when illuminating the tricolor light, use 70% greyscale") |
| all eight, one colour, 100% | 0.48 W | |
| **all eight, one colour, 25%** | **0.12 W** | **the budgeted case** |

The budget carries **0.12 W**, giving a board total of **4.02 W against 4.80 W —
1.19× headroom**. Budgeting the 70% tricolour case instead **replaces** that row
rather than adding to it — 4.02 − 0.12 + 1.01 = **4.91 W**, or **1.02× over
capacity**. That case is a floodlight,
not a status display, and nothing about indicating six power levels and two fault
states needs white at 70%.

**What makes this safe is that firmware cannot be allowed to produce the
uncapped case**, since it would be a single API call away. The cap belongs in the
LED driver, not in the display logic that calls it. The board survives 1.01 W
briefly — it is 1.02× of a steady-state figure, not an absolute maximum — but not
as an operating point, and certainly not at the 45 °C ambient this section
already calls over budget.

Hot spots need local copper rather than relying on the board average: the
ESP32-C6 (1.26 W), the Ethernet module and the TPS54360B (0.50 W each), the catch diode
(0.30 W) and the shunt (0.125 W). The channel eFuses are now among the hot spots — 0.265 W each at the balanced 2.43 A, and **0.353 W** on the one device that carries the 2.80 A worst case while the other channel is dark — they
dissipate more than the discrete FET and shunt they replaced. The two bucks should not share a thermal zone with the module or
the Ethernet controller.

The eFuse costs 0.31 W more than the discrete FET and shunt it replaced, which is
the single largest change to this budget and is recorded in Channel switching and
inrush as the price of working from 4.5 V.

**The TPS54360B needs an external catch diode** (datasheet p.26) — this was
missing from the BOM until the thermal pass. SS36 covers it: 60 V blocks the
36 V input, 3 A against **0.60 A** average at the 0.7 A rail load used
everywhere else in this document.

## Assumptions

Everything below is a number this design uses and cannot fully source. Each row
says what it rests on, what breaks if it is wrong, and **how it gets settled** —
because that last column is what decides whether a board has to be built and
measured before the design can be trusted, or whether the question can be closed
at a desk or in firmware.

The short version: **nothing here forces a build-test-respin cycle.** An
assumption only forces a respin if it is baked into copper, wrong, *and*
undetectable. None of these is all three.

| Assumption | Rests on | If wrong | Settled by |
|---|---|---|---|
| **Q3 V_BE = 0.6-0.8 V** at 800 µA | silicon junction over −20…+85 °C. The MMBT5551 datasheet publishes no V_BE(on) at any current; its only V_BE figure is V_BE(sat) at forced β 10, which is deep saturation and does not apply | the 12 V contract false-disconnects — **not** a dead board: the source reverts to vSafe5V and firmware renegotiates higher | **firmware, at runtime.** R31/R32 divide VBUS onto an ADC pin, so the board asks for 12 V, measures the bus, and drops 12 V from the ladder if it did not hold. A bench measurement of V_BE closes it properly |
| **h_FE = 60** at 800 µA | the datasheet's 80 min is at I_C = 1.0 mA **and V_CE = 5.0 V**. This follower runs at V_CE ≈ 0.84 V on the 12 V contract — quasi-saturation, where β droops hardest — so the row brackets the current and not the voltage | the 12 V margin shrinks: 0.22 V at β 40, 0.15 V at β 20, zero near β 9 | measurement. The 60 is conservative but it is not "already settled", because no published row covers this operating point |
| **HUSB238A FAULT/OUT2 default** | not stated. p.5 says the pin "can be configured as" either | the hardware interlock does not exist until I²C init | **design, not measurement.** The SHDN pull-downs already hold both channels off in that window, so the answer changes nothing. One register read confirms it |
| **Rd survives the powered-but-disabled window** (see Cold-start sequence, which depends on it) | not stated. p.11 says that with EN_N high "the whole system is disabled"; p.14 guarantees Rd only "even in the un-powered state". Between those two the board sits powered with EN_N held high by its internal pull-up for the whole MCU boot — see Cold-start step 3 — and no line covers it | the source detaches after tCCDebounce, VBUS drops, the rails collapse and the board power-cycles in a loop. This is the one assumption here that could stop it booting at all | **bench, and cheap**: plug into a PD source and watch CC with firmware never pulling EN_N low. If it fails, the fix is a pull-down on `PD_EN_N` so the chip enables before the MCU does — a part this board has room for |
| **C134462 J2-6 is MISO** | elimination: the only SPI signal left, on the only unaccounted pad. Not stated anywhere | SPI does not work | continuity check on the physical module, before soldering it down |
| **Figure 13 applies to the HTSSOP-20** | TI took it on a VQFN device on an EVM board | the ramp's thermal margin is smaller than plotted | 6.5-7.7 J available against 2.14 J needed at the design point is 3-3.6×, which a package change does not eat. The spread is the reading uncertainty on a light-grey trace over a log grid. Thermal measurement on the first board |
| **MAX3485 line current ~12 mA** each | estimated. The datasheet gives neither a loaded I_CC nor a V_OD at 120 Ω | the 3.3 V rail budget and the 0.06 W thermal row move | measurement, and the rail has 0.4 W of slack |
| **0.09 W/cm² free-air** | rule of thumb, for a 40 °C rise from 25 °C ambient | thermal headroom is not 1.19× | **J4**, the fan header — see Cooling. The *footprint* exists before the question is answered; nothing is fitted by default |
| **XL1509 V_CE ≈ 0.30 + 0.45·I** | fitted to the single datasheet point, 1.2 V at 2 A | the vSafe5V brightness estimate is wrong | measurement on one module. Affects a convenience figure, not the design |
| **Tier flags** (basic/extended) | jlcsearch's cached `is_basic` | an unexpected per-part assembly fee | confirm against JLCPCB when ordering. `./tools/stock.py` already covers the stock half |
| **20 mA per LED channel → 10.8 W per module** | p.3 of the WS2812D-F8 datasheet gives I_F = 20 mA as the *test condition* for the die's V_F and I_v, not as a rating of the internal sink | **everything.** Every brightness row, every fps row, the 2.43 A channel current, the 4.0 A flash peak, R_ILIM and the whole 180 W budget derive from it; `figures.py` hard-codes it as `FULL_W` | measure one module at full white. This is the single most load-bearing unsourced number in the design |
| **75% module buck efficiency** | characterised at 28 V, applied at 36 V | the bus-current model, hence the limiter divisor | measurement, or the XL1509 efficiency curve read at 36 V |
| **ESP32-C6 claims beyond the pin table** | only the *module* datasheet is cached. GPIO14's absence, the strapping tables, the ADC pin set, the PARLIO and RMT channel counts and `clk_out_gpio_num` all rest on the chip datasheet, the TRM and IDF 5.3 | a GPIO assignment that does not exist, or one that conflicts with a peripheral | cache the chip datasheet and TRM, then re-check the GPIO table against them |
| **TPS54360B R_DS(on) at 3 V gate drive** | the p.5 maximum of 190 mΩ is specified at `BOOT-SW = 6 V`; §8.2 says this design runs at low dropout, where Figure 1's 3 V curve applies. That curve's **maximum** is not published — only the typical, 0.12 Ω | the 5 V rail's floor. At the 1.25-1.30× Figure 1 ratio the worst case is ≈0.24 Ω and the rail is 4.48 V, under the 74AHCT541's 4.5 V minimum; at the published 190 mΩ it would be 4.515 V and clear | measure the rail at the bottom of vSafe5V on the first board. This is now the binding number for that floor, and it is an inference from a curve |
| **HUSB238A VBUS-pin current above a 5 V contract** | p.6 gives 330 µA typ / 800 µA max at `VDD=3.3 V & **VBUS=5 V**` — the only condition published for it. The adjacent row is 4/4.5 mA at VBUS = 29.4 V with VDD unpowered, so the pin draw is clearly bias-dependent | the 12 V margin, through R44's IR drop. At 3.24 kΩ it is 0.237 V at 800 µA, 0.183 V at 1.8 mA and 0.037 V at the 4.5 mA max — positive across the range, which is why R44 was sized for insensitivity rather than for the nominal | measure I_VBUS at a 12 V contract. The design is already built not to need the answer |
| **BSS138 R_DS at the FAULT gate drive** | not characterised at that V_GS | the interlock pulls SHDN down too slowly | 330 Ω is needed against a 909 Ω source, and the part is ~2 Ω fully on; the margin is two orders of magnitude |

Three of these — the pad numbers, the FAULT default and the MISO pin — are not
measurements at all. Two more are already mitigated by hardware that is on the
board (the ADC divider, the fan header). The remainder change numbers in this
document rather than decisions in the schematic.

## Known electrical limits

**Live limits only.** Things that were found and then fixed belong in the commit
that fixed them and in Resolved, not here — a limits list that doubles as a
changelog stops being read. Each entry below is either open, accepted, or a
constraint the layout has to honour.

**The 12 V contract has 0.24 V of margin at the PD undervoltage detector.** The
emitter follower feeding the HUSB238A VBUS pin subtracts a fixed V_BE while the
disconnect threshold is 86% of the requested voltage, and 12 V sits just inside
that band — where the required offset is at its smallest. Worked at V_BE 0.80 V
and h_FE 60 the pin clears by 0.24 V on a 10.32 V threshold, i.e. 2.3%. Every other
contract has 0.51 V or better. What would close it is a **measured V_BE**: the
datasheet publishes no V_BE(on) at any current, and its only V_BE-family figure is
V_BE(sat) ≤ 1.0 V at I_C = 10 mA with a forced β of 10 — deep saturation, which
does not bound the active-region value at 800 µA and should not be read as if it
did. h_FE is *not* the gap it was once called here: p.1 carries a row at
**I_C = 1.0 mA → 80 min**, which brackets the operating point, and using 80
instead of the conservative 60 widens the 12 V margin to 0.25 V. Treat 12 V as
the contract to test first on real hardware.

One thing that makes the analysis above valid and is not obvious: the base sits
**below** the collector by exactly I_B·R44 ≈ **43 mV** on any contract the Zener
does not clamp, because R44 is the only path into the base. (At 28 V and 36 V the
clamp holds the base 7-10 V below the collector instead, which is the same
conclusion with more room.) So V_BC is never positive and the transistor is
always inside the active region — never saturated, where V_BE
would climb. That is also why V_BE(sat) is the wrong spec to reach for.

**The HUSB238A's FAULT pin may not be a FAULT pin at reset.** Pin 13 is
FAULT/OUT2 and p.5 says it "can be configured as a FAULT pin" or "as a universal
output pin (OUT2) via the I²C master" — without stating the power-on default. If
it defaults to OUT2 the hardware interlock does not exist until firmware writes
the register, which is exactly the cold-start window the interlock was introduced
to cover. One register read with the part in hand closes this; until then the
SHDN pull-downs are the only fail-safe before I²C init.

**H5VL10B on CC1/CC2 is fine, and this entry had the condition backwards.** It
read the part's 5 V standoff against "a 3.0 A Rp that pulls CC toward 5 V with no
cable attached" — but that is the voltage on a *source's* CC. This board is a
**sink**: the HUSB238A presents Rd, which is how it is detected at all and how its
ORIENT pin knows which CC is live. CC therefore never floats to vRp; it sits at
the Rp/Rd divider output, which the Type-C spec caps at **2.60 V** for a 3.0 A
advertisement (1.86 V with a 10 kΩ Rp against 5.1 kΩ). The standoff clears that by
**2.4 V**.

The case a 5 V part genuinely would not survive is a VBUS-to-CC short in a damaged
cable, which puts 36 V on the line — and no 6 V-class part survives that either.
**Closed.**

**Fault reporting is ambiguous, twice over.** The eFuse's PGOOD goes low both for
a real fault and for a commanded shutdown, and the two channels' PGOODs are
wire-ORed onto one GPIO. So firmware cannot tell a faulted channel from one it
disabled, nor which channel it was. **FLT** (pin 15) would disambiguate the first
and a separate pin the second, at two GPIOs the budget does not have. Accepted;
the INA226 sees total current and the ADC divider sees the bus, so a fault is
*detectable*, just not attributable.

**The module draws 2.16 A from a 2.0 A converter.** Every brightness, current and
fps figure in this document derives from 14.4 W per module, which derives from
that 8 % exceedance. It is recorded in the module KB but was never surfaced here.
The 75 % efficiency behind 14.4 W is also characterised at 28 V, so at 36 V the
real figure is worse and 2.43 A per channel is a floor, not a ceiling.

**Thermal ambient is unstated.** The 0.08-0.1 W/cm² and the **1.19x** headroom
assume a 40 °C rise from **25 °C** ambient. Inside an enclosure on a soundwall
that is optimistic: at 45 °C ambient the allowed rise halves, capacity falls to
roughly 2.4 W, and the board is at **0.60x** — over budget, not merely tight.
This is the largest unquantified risk left in the thermal section.

**The channel body diodes back-feed the USB-C receptacle.** An N-channel
high-side switch has its body diode anode on the channel bus, so it conducts
module→bus→VBUS regardless of gate state — the switch change did not fix this.
On unplug or a PD hard reset the module bank holds the VBUS contacts near 36 V
with ~4 J behind it, and the HUSB238A's own discharge path is lost to the clamp
topology.

**A passive bleeder cannot meet vSafe0V against 6.6 mF.** Discharging it from
36 V to 0.8 V inside the 650 ms deadline needs ~26 Ω, which burns **50 W
continuously** at 36 V. What actually drains the module banks is the modules' own
load, which keeps conducting until their converters drop out — an unquantified
path, because the module's low-voltage behaviour is not characterised.

Specified: **R2, 10 kΩ 0.25 W across the bus**, which handles the controller-side
capacitance (τ = 50 ms at ~5 µF, so under 0.8 V in ~190 ms) and costs 0.13 W. The
module-side energy is **not** covered and is recorded here as the open part.

**The power budget omits cable loss.** 175 W of modules at 20 modules is 4.86 A;
add 2× 10 m of 0.5 mm² at 4.06 W per channel (0.23 A) and 0.14 A of controller
draw and the total is **≈5.2 A against a 5.0 A contract**. The 5 % drop line is
described as an efficiency choice; at the 20-module target it is an overrun.

**The current limit is a deliberate trade, not an impossibility.** It has to sit
above the **4.0 A** white-flash peak so a flash does not trip it, and below the
**5.0 A** contract so the eFuse sheds a faulted channel before the PD source
gives up on the whole bus. The eFuse's ±7% tolerance makes that window **real**:
a nominal 4.30-4.67 A satisfies both, and R_ILIM = 4.02 kΩ gives 4.185 A min /
4.815 A max.

**This design takes 3.24 kΩ (5.56 A) anyway**, giving up per-channel isolation to
keep a 1.29x flash margin instead of 1.05x. The reason is that the 4.0 A flash
figure is itself derived from an *inferred* 20 mA per LED channel, and Known
limits already records that 2.43 A sustained is a floor rather than a ceiling.
Sitting 5% above an uncertain number invites nuisance trips that black out the
wall; losing per-channel isolation only costs something in a fault that is
already taking the bus down.

The consequence is that on a hard short the source's own protection acts first,
in a few milliseconds, against the eFuse's own fault response — so the whole
wall goes dark rather than one chain. **Per-channel autonomous isolation is
therefore not reliably obtainable**, and the protection list has been corrected
to say so. Closing this needs either a tighter-tolerance controller or a lower
flash peak, i.e. fewer modules per channel.

**The eFuse's OVP now trips before the TVS conducts at all, which is the
ordering you want.** The SMCJ40CA's V_BR is **44.4-49.1 V** and the TPS16630's
OVP cut-off lands between **39.1 V and 42.3 V** with 1% resistors. The whole OVP
band sits below the whole breakdown band, so the eFuse always sheds the load
before the clamp starts dissipating — where the previous 36 V part's 40.0-44.2 V
breakdown overlapped the OVP window and the order depended on which corner each
landed at.

**It is still not a clean ordering, and OVP could not fully fix it anyway.** OVP
sheds the **channels**, while the TVS, the rails and the PD front end all sit
**upstream** of the eFuses. There is now a **2.1 V window** between the OVP's
high corner (42.3 V) and the TVS's breakdown minimum (44.4 V) where neither
acts — that is the price of the clean ordering above, and it is the right price,
because the alternative is the two bands overlapping. Above 44.4 V the TVS
conducts continuously, and it is a 1.5 kW 10/1000 µs part: a surge device, not a
sustained one. Nothing on this board disconnects the upstream node. Accepted.

**L1 still saturates before the converter current-limits, but less.** The part
was changed from ANR5040T100M (2.9 A Isat, 163 mΩ) to **SPM6530T-100M** (3.8 A,
72 mΩ): a metal-composite type in a 7.1 × 6.5 mm package. Against the
TPS54360B's 4.5 A minimum open-loop limit that is **0.84x** rather than 0.64x —
the inductor now saturates just as protection acts rather than well before it.

**Nothing in the catalogue closes it fully.** A 10 µH part with Isat ≥ 4.5 A was
searched for and not found at usable stock; SRP6540-100M reaches 4.0 A at five
times the price. Accepted at 0.84x.

The swap pays for itself elsewhere: **halving the DCR** lifts the 5 V rail from
4.50 V to **4.56 V** at the bottom of vSafe5V, which was sitting exactly on the
74AHCT541's floor, and cuts the inductor's own loss from 0.08 W to 0.04 W.

**TPS54360B at 963 kHz sits at its pulse-skip boundary.** The governing formula
is the one **tagged (9)** on p.15, `f_SW(max skip)` — and the citation is worth a
sentence because the datasheet contradicts itself: TI's prose on that page says
"Equation 10 calculates the maximum switching frequency limitation set by the
minimum controllable on time", while the equation actually tagged (10) is
`f_SW(shift)`, the short-circuit foldback limit. The tags are right and the prose
is wrong — following the prose leads you to cite (10). Read by
formula rather than by number, it gives f_SW(max skip) ≈
1.13 MHz typ, falling to 1.07 MHz at a +5 % PDO. Against the 963 kHz the fitted
R_T sets, that is **1.11-1.17×** on nominal parts, and **1.01×** once the ±10%
f_SW spread from p.5 is carried. TI's own example sits at 0.85×. RT = 200 kΩ (500 kHz) would
give 2.2× and halve switching loss.

**Reverting is not the cheap fix it looked like.** At 500 kHz a 10 µH inductor
gives **123% ripple** (0.86 A on a 0.7 A load), so the revert needs ~22 µH at the
same ≥3.8 A saturation — and the SPM6530 family has no 22 µH part in stock. It
would mean a different family and a larger package. The original reason for 1 MHz
(buying saturation margin) is weaker now that L1 is a 3.8 A part, but the revert
costs more than it saves. **Accepted at 1.01× worst case, 1.11-1.17× nominal.**

## Open questions

### Blocking the netlist

**Nothing. `netlist.json` exists, and `erc.py` reports no errors against it.**
See Netlist for what is in it and what the clean report does not mean.

### Verification, not design

1. **The Ethernet module's handedness.** Ring MJ2 position 1 against the two G
   pads on MJ1 to find which end of MJ1 is ground, and read the controller's
   silkscreen against the module's at assembly. Needs the physical module, not a
   datasheet — WIZnet does not publish the pin-1 end. Affects the **footprint**,
   not the netlist.

2. **The eFuse's ramp is characterised by TI, not derived here.** The TPS16630
   regulates its own junction temperature through start-up and is published
   powering into 15 mF, against 3.3 mF per channel here. There is no SOA
   calculation left to verify — which is the point of moving to an integrated
   part after a discrete FET's SOA cost four review rounds. What is **not**
   verified is the thermal path that lets it do that: the PowerPAD pour and its
   vias are a layout item with no datasheet to check them against.

3. **The thermal model is unmeasured.** ~70% sustainable brightness, calculated
   not observed. Testable for free using the XL1509's own thermal shutdown: run
   one module at full white for 15 minutes and watch for the LEDs cutting out and
   recovering. The TSD threshold is unspecified, so a pass means "below some
   unknown value", not "at 70%".

### Depends on the converter board

4. **The differential receiver footprint and 120 Ω termination** must exist on
   the first converter board of each chain. Populating only the controller end is
   useless.

5. **The module's inductor, catch diode and input bulk capacitor** are unknown —
   the .epro2 converter sheet has no LCSC parts assigned. None of them block the
   controller: the inrush ramp was sized for a worst case beyond what it can
   power.

### Accepted

6. **The board is not fully JLCPCB-assemblable.** Two kinds of board item are
   hand fitted — the W5500 module and the two picoMAX headers, three parts in
   all — so a PCBA order covers everything else and those three are soldered
   afterwards. Order numbers
   are in the sourcing table. This is a consequence of choosing a finished
   Ethernet module and a latching connector, both of which were the right call
   for their own reasons.

7. **No overvoltage protection above 28 V**, inherent to the HUSB238A topology.
    The SMCJ40CA and the ADC divider carry it.

8. **Connector orientation is handled mechanically** — picoMAX is polarised and
    each module sits in a hard shell. The residual risk is a mis-wired cable, and
    the failure modes are asymmetric: A/B swapped is non-destructive, power onto
    a data pole destroys the transceiver.

9. **The overvoltage trip protects the modules as well as the board.** The
    TPS16630's OVP reference is **±2%**, so a 40.68 V nominal trip lands
    between **39.1 V and 42.3 V** with 1% resistors: 1.3 V over the maximum bus
    and 2.7 V under the modules' absolute rating. The divider that achieves it
    is 732k/255k/30.0k.

## Netlist

`netlist.json` is generated by `netlist.py`, not written by hand, and both are
committed. **127 components, 437 pins, 76 real nets** — `erc.py` prints 106,
because it counts the thirty single-pin `NC_*` nets alongside them. The ERC reports
**0 errors** and one warning, the documented `P1-undervoltage` on U9.20.

The generator exists for one reason: the importer requires the component keys
to be exactly `gge1 … ggeN` with no gaps, so deleting a part means renumbering
every key after it. Here the keys follow position in a list, so deleting a part
is deleting a line. The JSON is still the artefact that gets imported.

    nix-shell --run './designs/led-matrix-controller/netlist.py'
    nix-shell --run './tools/erc.py designs/led-matrix-controller/netlist.json'
    nix-shell --run './tools/consistency.py designs/led-matrix-controller'

The third one checks **this document** against the netlist: every count stated
above, every BOM C-number and every price cell. It exists because the counts
here are derivable and were repeatedly left behind when the netlist changed.

**The pin keys come from the EasyEDA library, not from the datasheets.** This
distinction had been written down as a chore and turned out to be the most
productive hour of the netlist work, because the library disagrees with the
datasheet on four parts and in every case the datasheet is the one that reads
as obviously right:

| Part | The datasheet says | The symbol says |
|---|---|---|
| WIZ850io (C134462) | two headers, `J1-1 … J2-6` | 1-12, with **J2 running 12→7** — all six names agree on that direction and none on the other |
| CX90B-16P (C3198004) | 16 contacts `A1 … B12` | the same 16 names, plus shield tabs **0 and 1** and mid-plate tabs **2 and 3** |
| TS-1088-AR02016 (C720477) | four legs in two common pairs | **two** pads, each spanning both legs of one side |
| ESP32-C6-WROOM-1 (C5366877) | 29 pins, the underside one `EPAD` | 37 — the pad is broken out as **nine** GND pins, 29-37 |

A netlist keyed from the datasheet would have placed a pin numbered `J2-1` that
resolves to nothing, wired pads 3 and 4 of a two-pad switch, and left eight
ground pads of the MCU module with no net. None of that is visible on the
canvas afterwards. `./tools/eda.py verify <file>` now checks a whole BOM against
the library in one pass; it is how the last two were found, after the first two
had been fixed by hand.

### Deliberately open pins

Each on **its own** net named `NC_<what it is>`, which `erc.py` treats as "open
on purpose" rather than "nobody got to it". The distinction matters: the second
is a bug and the checker has to tell them apart.

**One net per pin, never a shared `NC`.** A shared net puts all thirty on one
node, which is inert only if the *importer* treats the name specially too — and
nothing in this toolchain can verify that it does. Shared, this board would have
tied seven enabled 74AHCT541 outputs to the TPS54360B's EN pin, both eFuse MODE
pins and the status chain's last DOUT. `netlist.py` refuses to write a netlist
in which two pins share an `NC_*` name.

**Thirty pins, and this list is all of them** — `./tools/consistency.py` checks
the count against the netlist, because a table that silently omits one is worse
than no table: the point of it is to let a reviewer tell a decision from an
oversight.

| Pin | Why it is open |
|---|---|
| U1.1, U1.2 (D+/D−) | the pair belongs to the MCU's native USB; sharing it would break enumeration |
| U1.15 (GATE) | there is no external VBUS switch left for it to drive |
| U4.3 (EN) | abs max 8.4 V — any UVLO divider off a 36 V bus sits over it; open gives the internal 4.3 V UVLO |
| U7.1, U8.1 (RO) | RE_N is tied high, so the receiver is off and its output is high-Z |
| U9.11-17 (Y1-Y7) | one buffer channel is used; the other seven drive nothing |
| U10/U11 .12 (MODE) | **open is the setting** — it selects latch-off, which is the chosen fault behaviour |
| U10/U11 .14, .15 (IMON, FLT) | real losses, argued in Known electrical limits — there are no GPIOs left |
| J1.A8, J1.B8 (SBU1/2) | no audio accessory or debug accessory mode |
| LED8.2 (DOUT) | end of the status chain |
| U10/U11 .4, .5, .17; U2.22; U3.9 | the parts' own **NC** pins — eight of the thirty. Typed `nc` in the knowledge base, so `K5` would object if any were wired to a real net |

### Hand-drawn after import

**Five symbols** — J2, J3 and TP1-TP3 — are not in the netlist and must be
drawn on the canvas, because the importer resolves components only through
`Supplier Part` and these have none. (A separate count, not to be confused with
it: **three parts are hand-SOLDERED** onto a board that is otherwise
JLCPCB-assembled — U3, J2 and J3 — and **three are do-not-fit**, R48, J4 and
U3. U3 is in all three lists for three different reasons.)

- **J2 and J3**, the picoMAX 3.5 headers. LCSC carries some WAGO parts but not
  the 2091 series — checked, not assumed. Giving them a placeholder C-number
  was the alternative and is worse: the importer would place a *different*
  connector, silently, with no footprint field in the property table to correct
  it afterwards. A part that is visibly absent beats a wrong part that looks
  present. Their nets all exist already — `CH1_36V`, `CH2_36V`, `LED_RTN`,
  `CH1_A/B`, `CH2_A/B` — so this is attaching a symbol to live nets, not
  rebuilding connectivity.
- **TP1-TP3**, the UART0 console pads. `UART_TX` and `UART_RX` are named in the
  netlist and carry one pin each; `design.yaml` suppresses `C1-single-pin-net`
  at those two nets specifically, so the rule stays live everywhere else.

The third and last suppression is unrelated to anything hand-drawn:
**`P2-no-decoupling@PD_VBUS_SENSE`**, because that pin is a sense input behind
the follower and not a supply — the chip's supply is VDD, decoupled by C18 and
C19. Fitting a capacitor there to satisfy the rule would delay the undervoltage
detector by about 1.2 ms, and UVP is the one protection the clamp topology
preserves above 28 V. The reasoning is in `design.yaml` beside the entry.

**`LED_RTN` is the one to check twice.** It is the module return and it is *not*
GND: R1, the 5 mΩ shunt, sits between them and they meet nowhere else. Wiring
J2/J3 pin 2 to GND shorts the shunt through the ground pour and the current
sense reads zero — with no ERC rule able to catch it, because a connector wired
to the wrong net is indistinguishable from one wired to the right one.

### What the clean report does not mean

The ERC passing is a floor, not a result. It has nothing to say about whether
R25/R26 divide to 5 V, whether the COMP network is stable, or whether the
UVLO string trips where the arithmetic in *Values, derived* says it does. Those
are the next two steps: the import diff, then ngspice on the two regulators.

## Next: from here to a board

The netlist is written and checked. What remains needs the EasyEDA editor and,
eventually, a board.

**1. Import.** Netlist Rebuild in EasyEDA Pro, select `netlist.json`.

**2. Draw the five hand-drawn symbols** — J2, J3 and TP1-TP3 — onto nets that
already exist. See *Hand-drawn after import*, and check `LED_RTN` against GND
while doing it.

**3. Mark the three do-not-fit parts**, before anything else touches the
board. Nothing in the netlist format can carry this — `value` says `0R DNF` on
R48 and the importer ignores it:

| Ref | Why |
|---|---|
| **R48** | 0 Ω from the 37.8 V bus onto a pin rated 33 V absolute. Fitting it **destroys U1** and bypasses the follower. It exists only for a ≤28 V build |
| **J4** | fan header, fitted only if the thermal model turns out optimistic — and only at ≤50 mA, see Cooling |
| **U3** | the $22.89 WIZnet original must not land on a PCBA order; the €12.30 JOY-IT clone is hand-fitted into the same footprint |

**4. Verify by diffing.** Export the netlist back out of EasyEDA and diff it
against the JSON that went in. This is not optional and the canvas is not a
substitute: the failure modes that matter are pins that look wired but carry no
net port, and separate nets silently merged into one. EasyEDA's DRC checks
geometry, not intent.

**5. Simulate the two regulators.** EasyEDA Pro's built-in ngspice, on the
TPS54360B stage and the SY8089 stage. The ERC has nothing to say about loop
stability or whether a divider produces the voltage it is supposed to. Not
worth running on the board as a whole — SPICE has no idea what firmware does.

**6. Layout.** Everything in *Layout constraints* applies: 2 oz (70 µm) copper,
the trace widths at the 2.80 A worst case, the split `LED_RTN` return joined to
ground only at the shunt, D15/D16 and C34/C35 within a few mm of the eFuse OUT
pins, the eFuse PowerPADs on a soldered pour with vias, and the module's
18 × 6 mm antenna keep-out copper-free on every layer.

**7. Re-check stock before ordering.** `./tools/stock.py
designs/led-matrix-controller/README.md`. The figures in this document carry a
retrieval date and five parts in an earlier BOM were at zero while a catalogue
mirror reported six figures.

Two things to carry into the first board rather than resolve on paper: the
5 V rail's real dropout at the bottom of vSafe5V, which decides whether
`P1-undervoltage` on U9.20 is cosmetic as argued, and the Ethernet module's
handedness, which a continuity check settles in a minute.

## Resolved

Kept so they are not re-opened:

- **Module connector** — Wago picoMAX 3.5 (2091 series), 4-pole, 10 A per
  contact, pinout **+36V / LED_RTN / A / B**. Push-in spring and an integrated
  locking latch, chosen for the vibration environment. At 10 A the worst case is
  28% of rating, so the connector stopped being a binding constraint.
  (Supersedes the Wurth WR-PHD 2.54 mm at 3 A, and the Micro-Fit alternative.)
- **Catch diode** — SS36 (C2903825), 3.0 A / 60 V. On the **controller** it is
  the TPS54360B's catch diode at **0.60 A** average (20% of rating), for the
  0.7 A rail load used everywhere else in this document. The 1.86 A / 62%
  figure belongs to the **module's** diode, not the controller's — two live
  parts, one part number, which is why that one is named here.
- **Rails** — TPS54360B to 5 V, SY8089 to 3.3 V. Chosen for ~100% duty
  pass-through so a 5 V bus still works. See Rail architecture.
- **Bus voltage and budget** — 36 V / 180 W, the highest EPR PDO the modules
  survive.
- **Channel count** — two, verified against three and four. See Why two channels.
- **Module BOM** — extracted from the .epro2 with `tools/epro.py`. The project is
  titled V2 but is V3.
- **No hold-up capacitor.** Three designs for one all failed, and it was causing
  three separate defects at once: the 5 V cold start through its series Schottky,
  an unfillable BOM line, and almost the whole Type-C bypass-capacitance overrun.
  What it bought was ride-through of a PD renegotiation — and the LEDs go dark on
  a bus collapse regardless, so all it protected was the controller from a
  brownout reset. **Do not re-add it as a Schottky**; that is the forward drop
  that broke the cold start. If ride-through is ever wanted it needs an
  ideal-diode controller.
- **HUSB238A supply at vSafe5V** — **VDD (pin 5) tied to 3V3**, plus **D14**
  (RB751V-40) from the 5 V rail to the VBUS pin. VDD is an input supply, not an
  output, so tying it drops the VBUS-pin requirement from 4.5 V to 3.15 V and its
  draw from 4.5 mA to 800 µA; the Schottky then holds the pin at 4.19 V on a
  4.75 V bus where the follower alone gives 3.91-4.11 V. Residual: `VBUS_OK`
  rising clears by **+0.11 V** at the converter's applicable R_DS corner (+0.19 V
  on a typical one), and the under-voltage detector publishes no band covering a
  5 V RDO — its adjacent band (F1, 80%) puts the bound at 4.00 V, which the pin
  clears by the same margin. Works on a typical part, not guaranteed at the
  corner.
- **USB-C receptacle** — **CX90B-16P** (C3198004), Hirose CX series, **5 A /
  48 V AC/DC**, 16-position USB 2.0. Closes the longest-standing blocking
  question. Note LCSC's parameter table says 20 V for it and is **wrong**; the
  48 V comes from Hirose's own series catalogue. Reaching 5 A needs all four
  VBUS and all four GND contacts paralleled — 1.25 A per contact.
- **A/B pair TVS** — **SMAJ7.0CA** (C19077529), Vrwm 7 V, Vc **12 V**. The
  SMAJ15CA it replaces clamped at 24.4 V against the MAX3485's ±15 V absolute
  maximum, so it could not conduct until the pin was already past its limit. The
  −7/+12 V window it was sized against is the transceiver's *capability*; this
  cable imposes only its own return IR drop, 0.84 V sustained and 1.38 V on a
  white flash, because both ends share GND in one connector shell. **Not
  covered:** a mis-wired cable putting +36 V on A/B, two poles away in the same
  shell — mechanical, not electrical.
- **HUSB238A ADDR to GND (0x42)** — the INA226 occupies one address, 0x40 with
  A0/A1 both to GND, which is pinned in the tie-off list. GND is the only tie
  valid at the moment ADDR latches, since that happens when 3V3 arrives.
- **HUSB238A EN_N** — the internal pull-up is the correct default and no external
  part is needed. A pull-**down** here would enable the chip before the MCU
  exists.
- **The FAULT interlock no longer fights the GPIOs** — its transistors sit on the
  TPS16630 SHDN pins, not on driven gates, so two open drains on a
  logic input cannot contend. (Its *drive level* is still marginal and stays in
  Known electrical limits.)
- **Type-C bypass capacitance** — **7.56 µF** against the 10 µF limit, now
  computed rather than asserted. At attach the bus is at vSafe5V and both eFuses
  are off, so what counts is C33 (2.2) + C8 (4.7) + C9 (0.22) + C3 (0.22) +
  C4 (0.22). These are 100 V parts at 5 V, so DC-bias derating is negligible and
  effective is near nameplate — there is nothing to hide behind. C33 is 2.2 µF
  rather than a second 4.7 µF for exactly this reason: two of them would be
  **10.06 µF** and just outside.
- **FAULT interlock drive** — **BSS138** rather than 2N7002 for Q1/Q2.
  V_GS(th) **1.6 V max** (C7420339 datasheet, not the 1.5 V of other vendors' BSS138) against the 2N7002's 2.5 V, so the HUSB238A's 2.64 V V_OH leaves 1.04 V
  of worst-case overdrive instead of 0.14 V. (The 2.64 V is the HUSB238A's own
  `VOH_PP`, p.8: 0.8 × VDD minimum at 1 mA source — not a figure borrowed from the
  MCU, though it happens to be the same expression.) Q3, the follower, is no longer a BSS138, so this is two parts
  rather than a reuse. The *current*
  half of the problem was removed by the eFuse, whose SHDN is a logic input
  rather than a divider node.
- **Channel switching** — one **TPS16630** eFuse per channel, after four discrete
  gate networks and one LM5069 arrangement. Integrated 60 V FET, dV/dt-controlled
  inrush, adjustable current limit and overvoltage cut-off, working from 4.5 V so
  the LED output follows the rails down to a laptop port. Values
  are derived in Channel switching and inrush. **Do not re-open this as a
  discrete network** — the four attempts and why each failed are recorded there.

## Reference design

`reference/*.epro2` holds the existing LED matrix module project (EasyEDA Pro
3.2.149). **The project is titled "V2" but is actually V3** — V2 used a
different converter and had no decoupling capacitors.

Read it with `tools/epro.py`:

    ./tools/epro.py bom "reference/ProPrj_LED Matrix V2_2026-10-01 (2).epro2"

That yields the real BOM with LCSC numbers. **Only the 36 WS2812D-F8 and the 36
per-LED 100nF capacitors were actually confirmed this way** — see below for why
the converter parts were not. See the CLAUDE.md section on reading an EasyEDA project for the file
format and its one parsing trap.

**The archive holds several boards, and the converter is not resolved:**

| board | contents | converter |
|---|---|---|
| PCB_LED Matrix | 36x WS2812 + 36x 100nF + 3 connectors | none |
| PCB_LED Matrix v1 | as above + LT1085CT-5 | LT1085 linear |
| PCB6 | has `L`, `D`, `COUT`, `U x4` | present, **no LCSC parts assigned** |

So the XL1509, the SS36 and the electrolytics are in the project *library* but
are **not placed on any complete board** in the archive. The XL1509 is taken as
the module's converter because you said so, not because the file says so, and
every thermal and inrush figure here is conditional on that.

That also means the input bulk capacitor cannot be read out. It no longer
blocks anything — the inrush ramp was sized for a worst case beyond what this
controller can power — but it is why the module's own ripple and damping
behaviour is still estimated rather than known.

Multiple archives are present and they differ in size, so they are not duplicate
downloads. The most recent is `(2)`; check before trusting an older one.

For a netlist to run ERC against, **export one from EasyEDA Pro directly**
(File -> Export). `tools/epro.py` is a read-only reverse-engineering aid, not a
substitute for the supported path.
