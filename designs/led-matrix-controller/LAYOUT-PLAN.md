---
type: Plan
title: "Schematic layout: rails and rotation, in stages"
description: A staged plan for making an imported schematic read as a circuit — rail-role rows first, rotation only where it is unambiguous, rail lines only once the record model is settled. Revised after review found the first version not ready.
status: draft
generated: { by: claude-code/claude-opus-5, at: 2026-10-04T18:00:00Z }
verified:
  - { by: process:review, at: 2026-10-04T18:40:00Z }
sources:
  - id: netlist
    resource: /designs/led-matrix-controller/netlist.json
    title: 129 components, 441 pins, 78 nets — what has to be laid out
  - id: design-yaml
    resource: /designs/led-matrix-controller/design.yaml
    title: Declares the eight rails this plan classifies from
  - id: tools
    resource: /tools/arrange.py and /tools/schwire.py
    title: The placement and wiring this plan extends
---

# Schematic layout, in stages

## Why the obvious approach lost

Three layouts, measured over the nets of two to four pins:

| | total bounding span |
|---|---|
| connectivity chain (shipped) | 49,285 units |
| force-directed, re-sorted into rows | 50,845 — 3% worse |
| force-directed, snapped to a 2D grid | 74,005 — 50% worse |

**Wire length is the wrong objective.** A schematic is readable because of
convention — power up, ground down, signal left to right, a divider drawn so
it reads as a divider. A solver minimising length breaks all of them.

The first version of this plan drew that conclusion and then over-reached.
Review found eleven factual errors and seven blocking defects; the numbers
below are the corrected ones, and the staging exists because the review
showed which parts are safe and which are not.

## The board, counted

| | |
|---|---|
| components | 129, of which **106 have two pins** |
| pins | 441, plus 30 deliberately open `NC_*` |
| nets | 78 |
| **rails declared in `design.yaml`** | **eight**, carrying **233 pins — 53%** |
| | GND 124, BUS_3V3 31, BUS_5V 28, VBUS 26, LED_RTN 8, CH1_36V 6, CH2_36V 6, PD_VBUS_SENSE 4 |
| nets whose pins sit in one functional block | **52** |
| cross-block nets | 26, of which 5 are rails; **21 signal nets, 64 pins** actually stay labels |

The grouping into twelve bands comes from **two** sources, not one: the
`# ---- Name ----` sections in `netlist.py` cover 113 components, and
`design.yaml`'s `blocks:` mapping covers the 16 the generator builds in a
loop.

## Stage 0 — Done. Rotation works, with two corrections

R9 was rotated by hand in EasyEDA and the file exported. The transform
holds: **441 of 441 pin positions land on their stub**, R9's included, and
`epro.py diff` is still identical. `ROT` is validated against something
other than the identity for the first time.

Two things the test found that no amount of reading would have:

**The stored angle is counter-clockwise; the toolbar button is clockwise.**
One press of the clockwise button wrote `"rotation": 270`, which is the same
thing — CCW 270 = CW 90 — so the file follows the ordinary mathematical
convention and nothing is inverted. Verified against R9: stored 270 puts
pin 1 twenty units **below** the anchor, and that is where the export has
it.

The table stage 2 needs, for a two-pin part whose pins are local (+20, 0)
and (−20, 0):

| stored | pin 1 ends | use when |
|---|---|---|
| 0 | right | signal + signal |
| 90 | **up** | **pin 1 is the supply pin** |
| 180 | left | — |
| 270 | **down** | **pin 1 is the ground pin** |

So "supply up" is stored **90** when the supply is on pin 1 and **270** when
it is on pin 2 — which is why it has to be computed from the symbol's pin
order rather than written as a constant.

**EasyEDA writes fractional anchors.** R9 came back at
`y: -1374.9999999999998`, so its computed pin missed the stub by 2 × 10⁻¹³
and `schwire.py` refused the whole file. Fail-closed and useless. Every
coordinate now goes through a `grid()` snap before it is compared or
emitted, with a regression probe for a fractional anchor. An exact tuple
match against a float is a check that works until a human touches the file.

What is still unverified: `ROT[90]` and `ROT[180]` specifically — R9
exercised 270 — and `isMirror`, which `arrange.py` still does not guard at
all and which four committed `.epro2` contain.

## Stage 1 — Rail-role rows, no new geometry

Give `pack()` a per-band row assignment instead of one flat chain:

- a component with a pin on the band's **ground** rail goes in the bottom row
- one with only **supply** pins goes in the top row
- everything else goes between

```
   top     [C33]   [C8]              supply-only parts
   middle  [R21]  ┌────┐  [R44]      signal path
                  │ U1 │
   bottom  [C19]  └────┘  [C18]      parts touching ground
```

**This is pure translation.** Every structural check in `arrange.py`
survives unchanged, the local-geometry fingerprint still proves rigidity,
`collisions()` still refuses overlap, the three-seed determinism test still
applies, and `epro.py diff` cannot be affected because no record is created
or deleted. It buys "power at the top, ground at the bottom" at band
granularity **without drawing a single line**.

It also fixes a measured defect in the current ordering that has nothing to
do with rails: `seriate()` only counts nets of two to four pins, so a
decoupling capacitor — whose only nets are rails — has **no edge at all**.
**39 of 129 components are in that position.** The result is visible:

```
seriate("5 V -> 3.3 V")  ->  C12 C13 C14 L2 U5 R28 R29 R30
```

The three capacitors sit in designator order at one end and U5 is fifth.
Row assignment by rail role puts them back where they belong without needing
a new adjacency rule.

## Stage 2 — Rotate only what is unambiguous

Of the 106 two-pin components:

| pins | count | rotation |
|---|---|---|
| **supply + ground** | **36** | **90°, supply pin up** — unambiguous |
| ground + signal | 38 | 90°, deferred — direction depends on the signal's source |
| supply + signal | 17 | 90°, deferred — same |
| signal + signal | 12 | 0° |
| ground + ground | **1** | **R1.** The rule said 0° and that is wrong |
| supply + supply | **2** | **D14, R48.** No rule exists |

**Stage 2 is the 36 only.** No rail lines, no label deletion, no cross-band
geometry. One new check: every pin must still coincide with exactly one
`ATTR NET`, matched by coordinate against the recomputed pin position — and
not re-read by the code that wrote it, or the check is circular.

The three exceptions are exactly the parts the design record argues about
most, which is why they are excluded rather than guessed:

- **R1** is the 5 mΩ shunt. `GND` and `LED_RTN` are both grounds and must
  not merge; the shunt is the only place they meet. It needs 90° to bridge
  two ground lines at different heights, which the role table cannot say.
- **D14** and **R48** are supply-to-supply, a pair the table has no row for.
  R48 is also DNF, and `design.yaml`'s `dnf:` list has no bearing on layout
  yet.

Polarity is a further gap: nothing in a pin's net role says which end of a
diode is the cathode. D2, D10-D13, D15/D16 and the TVS parts all need the
part class from `kb/`, not the pin roles.

## Stage 3 — Rail lines, once the record model exists

Deferred, and these are the questions that must be answered **before** any
code, not during:

1. **How does drawing a rail keep `epro.py diff` at 441?** It counts one
   `ATTR key="NET"` per wire group. Replace 124 GND stubs with one line and
   124 drops and the count moves; keep all 124 labels and the sheet is no
   more readable than now. The arithmetic has to land on exactly 441 and the
   record model has to be written down first.
2. **Two rail lines of different nets, drawn collinear, are one node** — and
   that short survives every check that exists. `epro.py diff` sees both
   names declared; the fingerprint covers per-component geometry and not
   free-standing wires; `collisions()` sizes gaps from component bounding
   boxes, and a rail line is outside every box. Segment-to-segment checking
   is new work and it is the gate on this stage.
3. **Seven of twelve bands carry more than one rail of a kind.** Measured:

   | band | grounds | supplies |
   |---|---|---|
   | USB-C inlet and the PD front end | 1 | **4** |
   | Channel 2 eFuse | **2** | **3** |
   | Channel 1 eFuse | **2** | 2 |
   | Current sense, Differential link | **2** | 1 |
   | 36 V → 5 V, 5 V → 3.3 V | 1 | 2 |

   Five horizontal lines around one row of components is the worst case, and
   it is the majority of the board rather than an eFuse edge case.
4. **A drop from a 37-pin IC** — U2 has 11 GND pins, U9 10, J1 8 — is eleven
   parallel verticals under one symbol. One drop per pin, or one per net per
   component?
5. **A minimum pin count to promote a rail to a line.** VBUS has one pin in
   the Bus voltage ADC band; J4, a DNF connector, is the sole member of its
   band and would get two lines. A line for one pin is worse than the label.
6. **A band that wraps to two rows** — which row do the rails attach to?
7. **The 30 `NC_*` pins**, 7% of the sheet, have no role in the
   classification and presumably keep labels. Say so.

## Phase A's guard, corrected

The first version said: a net absent from `design.yaml` with more than eight
pins is an error. **That guard can never fire on this board.** The largest
non-rail net is `VBUS_ADC` with four pins, while `CH1_36V` and `CH2_36V`
have six and `LED_RTN` has eight — so omitting a real 36 V rail from
`design.yaml` would sail straight past it and phase D would draw a power
rail as a point-to-point signal.

The guard has to be the other way round: **every net with more pins than the
largest declared non-rail net is either declared or an error.** On this
board that threshold is five, not eight.

## The cheapest thing available, which is not this

Splitting to three sheets costs **no code at all** and halves the density
that makes the sheet unreadable. A block-aligned 42/43/44 split exists; of
the nets it cuts, most are rails that stay labels either way. That is
independent of everything above and should probably happen first.

## What this still does not attempt

Crossing minimisation, cross-band signal routing, sheet splitting as code,
or a schematic anyone would ship without editing. The target is "the blocks
are recognisable and the power structure is visible" — the difference
between a document and a dump.

## Reproducibility

The span figures at the top were produced by throwaway scripts and are not
reproducible from anything committed. Before they are cited again they need
a committed measurement tool, in the way `consistency.py` backs the numbers
in the design record. Until then they are observations, not results.
