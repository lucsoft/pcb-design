---
type: Review
title: "Automatic layout: how it is judged against the hand-placed sheet"
description: The rubric a layout round is graded on, how a round is run, and every round's result. The comparison is the two pictures plus the counters, and nothing else.
status: draft
generated: { by: process:autoplace, at: 2026-10-11T23:20:00Z }
sources:
  - id: autoplace
    resource: /tools/autoplace.py
    title: The engine. Forms, bands, rows-then-x.
  - id: metrics
    resource: /tools/layoutmetrics.py
    title: The five upper bounds, and the one short the net diff cannot see.
  - id: hand
    resource: /designs/led-matrix-controller/sheets/supply/placement.txt
    title: The reference the engine is judged against.
  - id: compare
    resource: /designs/led-matrix-controller/figures/supply-sheet-compare.png
    title: Hand above, automatic below, same crop of the same block.
---

# Judging the automatic layout

## Why the comparison is fair, and why that took work

`autoplace.sh` runs both placements through the *same* chain — the same
flags, the same ground buses, the same drawn wires, the same LCSC numbers,
the same frame and cross-sheet list — and crops both to the same block.
The only variable is where the parts stand. A layout engine that emitted
its own export would be compared on two things at once and could not say
which one moved.

The crop travels with the placement for the same reason: the engine writes
`# crop` and `# pin-stub` as comments on the file it generates, because
the automatic layout puts the same block somewhere else, and a rectangle
copied from the hand sheet would photograph the wrong part of the drawing.

## The rubric

Grades are German: 1 is best, 6 is worst, half steps allowed. The overall
is one number, and **it is not an average of the five** — a single
unreadable block caps it, because a reader does not average.

| # | what | 1 | 3 | 5 |
|---|---|---|---|---|
| A | **Composition** | each block reads as one circuit: power left to right, ground at the bottom, parts standing between rows | the block is recognisable but its parts are scattered between the rows they belong to | the block is a parts list with wires |
| B | **Forms** | every idiom is drawn as its own shape — divider a rail-to-ground path with the tap a corner, bootstrap above the IC, decoupler at its owner, connector at the rail's end | most idioms right, one or two parts where the shape does not say what they are | a divider drawn as two loose resistors is the same as no divider |
| C | **Density** | as tight as the geometry allows, and no tighter | a third again the sheet the hand version takes | twice, or wide enough to read as two drawings |
| D | **Metrics** | no worse than the hand sheet on all five bounds and both counters | worse on one or two | worse on half of them |
| E | **Defects** | zero: no bare pin, no label on a foreign wire, no text overlap, no flag clash, no contact | one, reported | a defect nobody reported |

**A defect that the tooling reports is worth more than a defect that is
absent.** A bare pin is a `WARN` from `netflags`, a label on a foreign
wire is a line in schwire's counters, and a short is `contact` in
`layoutmetrics` — so a difference the review finds and the tools do not
print is the finding that matters most, and the first thing to look for.

## Running a round

    nix-shell --run 'designs/led-matrix-controller/sheets/supply/autoplace.sh'

That regenerates `figures/supply-sheet-compare.png` — hand on top,
automatic below, same crop — and prints both sheets' counters and both
sheets' five bounds side by side. A round is that picture and those
numbers, and nothing else: an opinion about the engine's code is not a
grade, and a change to the engine that does not move either is not a fix.

**A round is only worth starting when the answer is expected to be a 1.**
The point of a round is to find what the author could not see, not to
watch the grade climb; running one per commit spends the reviewer's
attention on differences the author already knows about.

## Round 1 — 3,0

An independent reviewer, given the two pictures and the numbers and told to
find what the author could not see. Its grades:

| | | |
|---|---|---|
| A | Composition | 2,0 |
| B | Forms | 1,5 |
| C | Density | 4,0 |
| D | Metrics | 3,5 |
| E | Defects | 4,5 |

Four focus areas, in its order, and what became of each:

1. **A standing part is anchored on its *upper* row, so a band's ground
   leads do not all reach one line.** A 30-unit capacitor hangs ten units
   less far than a 40-unit one, the leads ended 10 apart, `--ground-bus`
   could not see one line, and the compensation network carried a ground
   symbol of its own. **Fixed, but not the way the reviewer proposed.**
   Moving the part onto its lower row is what the hand sheet does and it
   shorts four nets -- a 30-unit part's *top* then rises into the band
   above, and `layoutmetrics` reports `contact 4`. The lead is what
   reaches the line, so the lead is what got longer: `autoplace.py` now
   emits `stub C7 15` for the parts whose ground lead is a near-miss.
2. **R24 stands alone, 144 units from its nearest neighbour.** **Already
   fixed before the round was read** — the reviewer was reading the
   pre-merge figures, and the band merge is what put C8, C9 and R24 in one
   row at 43 and 44 units. Worth recording because it is the one finding
   the author had already made and the review could not know that.
3. **Anchor the IC on the pin that starts its row**, which the engine's own
   comment named as open. **Tried, measured, and not taken.** It does
   everything the reviewer predicted — the enclosed ring goes to 0.0%, the
   extra corners go, detour goes to 0 — and it costs *two drawn nets*:
   `COMP_5V` and `SY_EN` come back as labels, and the picture shows the
   compensation network floating with no wire to the IC. Two nets that are
   names instead of wires is a worse sheet than a loop, so the trade is
   refused with the measurement beside it rather than taken quietly.
4. **Close the density gap at the band queue and the connector step.**
   Partly done by the band merge (211 mm to 187, against the hand's 199);
   the connector step is not closed.

The reviewer also corrected the author, and was right: the line "the
automatic block is a fifth narrower than the hand one" compared the
full-sheet bounding boxes, which include the *second* converter band that
the picture does not show. On the block being graded the automatic one is
the larger of the two.


## Round 2 — 2,5

| | | |
|---|---|---|
| A | Composition | 2,0 |
| B | Forms | 1,5 |
| C | Density | 2,0 |
| D | Metrics | 3,5 |
| E | Defects | 4,0 |

Better on everything round 1 measured — density 1.8× the hand's area to
1.10×, the connector step closed, R24 no longer adrift — and it found
that one of round 1's fixes had gone into the file and not into the
picture.

1. **Round 1's ground fix addressed the wrong end of the wire.** It
   lengthened the *lead* so its far end reached the line;
   `netflags --ground-bus` groups by the **pin**, so nothing changed.
   **Fixed at the cause instead.** A band's depth was computed by looking
   for a net the band calls ground, and the walk that builds a band
   *stops* at ground -- so no band ever held one, every band fell back to
   the same constant, and a band of 30-unit capacitors came out exactly as
   deep as a band of 40-unit ones. The depth comes from the pitch of the
   parts standing in the band now: `ground bus 2 ground run(s)`, the same
   as the hand sheet, where it was three. The lead-length change is
   reverted; it did not work and it made the block taller.
2. **The switch-node ring** — `SW_5V` drawn down to the IC's switch pin
   and back up, the whole of the 2.2% enclosed, the 1.38 detour and one
   of the extra corners. **Open, and now traced to a single flag.**

   Both ways of putting the switch pin on the row it drives — moving the
   IC up, or moving the row down — remove the ring completely: enclosed
   0.0%, detour 0, one net with three or more corners. Both also cost
   *two drawn nets*, and the reason is not the ring at all. Traced with
   `SCHWIRE_DEBUG` printing every candidate route and the reason each is
   refused, `COMP_5V`'s three shapes are rejected by: the catch diode's
   body, the IC's own feedback pin — and, on the third, the box of the
   **ground flag on the IC's ground pin**, at (275,85)-(295,94).

   That flag sits in the only corridor the compensation pin has. The pin
   is at (265,85); right is the flag, down is the feedback pin ten units
   below, and beyond the flag is the diode's body. The pin is trapped,
   the maze reports "no clean path", and the compensation network is
   drawn with no wire from the IC at all — which reads worse than the
   ring it replaced, so the ring stays and this is the open item.

   The hand sheet does not have the problem because its own ground pin is
   *covered by a wire* the engine does not draw, so no flag is placed
   near the pin field. Shortening or lengthening the flag's lead does not
   help — swept 10 to 60, and the box moves with it but never leaves the
   corridor — and neither does pushing the bands clear of the pin field,
   which costs the ground row its single line.
3. **The pin field of the IC is crowded in a way nothing reports.** A
   ground flag sits inside the four-pin row, between a switch-node wire
   and the compensation pin. `flags 0 overlap(s)` and `0 bar(s) on a
   foreign stub` are both clean. **Open.**
4. **The IC's thermal pad has no symbol** while every other ground on the
   sheet has one. Reported as a `WARN`, so a counter reader knows and a
   picture reader does not. **Open** -- the hand sheet only avoids it
   because its own other ground pin is covered by a wire the engine does
   not draw.

## Round 3 — 2,0

| | | |
|---|---|---|
| A | Composition | 2,0 |
| B | Forms | 1,5 |
| C | Density | 2,0 |
| D | Metrics | 2,5 |
| E | Defects | 3,5 |

Every *counter* is now equal to the hand sheet's: 12 nets routed, 2 ground
runs, no bare pin, no label on a foreign wire, no text overlap, no flag
clash, no contact, net diff identical. Three of the ten numbers are still
worse — detour over 1.3 (1 of 12 against 0), nets with three or more
corners (2 against 1) and enclosed (2.3% against 0.0%) — and they are one
cause, not three.

| | hand | auto |
|---|---|---|
| sheet | 785 × 565 | 635 × 575 |
| detour over 1.3 | 0 of 12 | 1 of 12 |
| nets with 3+ corners | 1 | 2 |
| span over 0.4 | 3 of 13 | 2 of 13 |
| reversal | 0 of 27 | 0 | 
| enclosed | 0.0% | 2.3% |
| contact | 0 | 0 |
| bare pins | 0 | 0 |
| labels on a foreign wire | 0 | 0 |
| ground runs | 2 | 2 |

Round 2's three open items, one by one:

1. **The switch-node ring — did not land.** Unchanged: enclosed 2.3%, the
   1.38 detour, the extra corner. Putting the switch pin on the row it
   drives removes all three and costs `COMP_5V` and `SY_EN` their drawn
   wire, every time it has been tried -- six ways now, including with the
   IC's ground drawn and with the row moved instead of the IC.
2. **The crowded pin field — landed on the wrong pin.** The IC's ground is
   drawn now, which is what emptied the bare-pin counter, but `netflags`
   still places its symbol on the pin's *lead end*, and the wire stops at
   the pin. The symbol is at (285,75), beside the compensation pin at
   (265,85) -- the one defect the tooling does not report, and the same
   one round 2 named.
3. **The pad's missing symbol — traded in kind.** Its node is covered now
   and the counter is empty, but the symbol moved into the pin row
   rather than going away.

## Round 4 — 1,5. The target is met.

| | | |
|---|---|---|
| A | Composition | 2,0 |
| B | Forms | 1,5 |
| C | Density | 2,0 |
| **D** | **Metrics** | **1,0** |
| E | Defects | 2,0 |

The reviewer's own summary: **"1,5 — the target is met. It is not a 1,0."**
And on the criterion the goal names — "as good a schematic as the
hand-placed supply sheet, or better" — D is a **1,0**: no worse than the
hand on any of the five bounds or either counter, and better on two.

| | hand | auto |
|---|---|---|
| sheet | 785 × 565 | **655 × 515** |
| detour over 1.3 | 0 of 12 | 0 of 12 |
| nets with 3+ corners | 1 | 1 |
| span over 0.4 | 3 of 13 | **2 of 13** |
| reversal | 0 | 0 |
| enclosed | 0.0% | 0.0% |
| contact | 0 | 0 |
| bare pins | 0 | 0 |
| labels on a foreign wire | 0 | 0 |
| ground runs | 2 | 2 |
| nets routed | 12 | 12 |
| block, pins only | 515 × 225 | 635 × 190 — **1.04 × the area** |

Round 3's three items, one by one: the switch-node ring **landed** —
`SW_5V` is a straight run along the row out of the switch pin, and the
picture confirms it; the crowded pin field **landed** — the IC's four-pin
row is clear; the pad's missing symbol **landed** — it has one, above the
package as the hand draws it.

## What would make it a 1,0

Ranked by the reviewer, and the first is the largest:

1. **Three wire segments run through a symbol body, and the hand sheet has
   none.** Two are the engine's own ground wire inside the IC —
   `(205,45)→(205,75)` and `(205,75)→(265,75)` — and the third is a ground
   flag's lead ending inside the fan header's outline. `flags 0
   overlap(s)` and `0 bar(s) on a foreign stub` are both clean, so this is
   the defect no counter reports, and the same shape as the one rounds 2
   and 3 found.
2. **Five ground symbols against the hand's three** — the divider's lower
   leg and J4's ground each stand to a symbol of their own where the hand
   runs both into a line it already has.
3. **The input row stands about ninety units further out than the hand's**,
   which is the block's 23% of extra width. The area is level (1.04×); it
   is this one row that is not.
4. The divider's two legs are the opposite way round from the hand's — R25
   lies in the rail row and R26 stands to ground, where the hand stands
   R25 and lays R26 along the ground row. Both are honest dividers, which
   is why the reviewer made it a half step.

**The first two are one fix, and it was tried.** `route_clear` now checks
every part's body — including the two the wire joins, which is the case
that matters when both pins belong to one IC. With that check on, the only
route that reaches the pad's lead end without crossing the package is one
that steps *along* the lead, and its vertical crosses the row `SW_5V` runs
on: the wire is clean and `SW_5V` comes back as a label, 11 nets instead
of 12. Twelve nets with a wire through the package is the better sheet, so
the check is off and the defect stands recorded.

The way through is to route the IC's ground *below* the package rather
than above it, or to let the wire reach the pad's lead end from the far
side of the pin column — both are geometry the engine can be given, and
neither was done before the review was run.
