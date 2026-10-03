# ERC rule catalogue

Every rule `tools/erc.py` can report. Suppress one for a design by listing its
id under `ignore:` in that design's `design.yaml` — and write down why, because
a suppressed rule is a decision, not a cleanup.

An entry is either a bare rule id, which silences the rule across the whole
board, or **`rule@where`**, which silences it at one net or pin only. Prefer the
scoped form. The bare form is how a real check gets lost: `P2-no-decoupling`
has one justified exception on the LED matrix controller — a sense pin that is
worse off with a capacitor — and suppressing it unscoped would have stopped it
guarding both actual supply rails as well. The `where` to match is the one the
finding prints in brackets.

## Structural — the EasyEDA importer's own contract

| id | severity | catches |
|----|----------|---------|
| `S1-key-sequence` | error | component keys are not exactly `gge1..ggeN` in order; the importer indexes on this and fails quietly |
| `S2-missing-designator` | error | empty `props.Designator` |
| `S3-duplicate-designator` | error | two components claiming the same reference |
| `S4-missing-lcsc` / `S4-malformed-lcsc` | error | absent or non-`C#####` `Supplier Part` |
| `S5-prop-case` | warning | a prop key the importer will silently drop |


## Knowledge base — can this part be checked at all

| id | severity | catches |
|----|----------|---------|
| `K1-unknown-part` | error | the C-number is not in `kb/parts/` |
| `K2-no-pin-map` | warning | part is known but has no pins, so electrical checks are skipped |
| `K3-unknown-pin` | error | a pin number the part does not have |
| `K4-unconnected-pin` | error for supply pins, warning otherwise | a declared pin left floating |
| `K5-nc-connected` | warning | a not-connected pin wired to a real net. The literal net `NC` is not a real net — it is how K4 asks you to record "open on purpose" — so it does not fire on that |

## Connectivity

| id | severity | catches |
|----|----------|---------|
| `C1-single-pin-net` | error | a net with exactly one pin — a typo or a forgotten wire |
| `C2-net-name-collision` | error | `VCC_3V3` and `VCC3V3` as separate nets |
| `C3-probable-typo` | error | a one-pin net one character from a busy net, with the likely intended name |
| `S6-shorted-two-terminal` | error (resistor, inductor) / warning (other) | a two-terminal part with the same net on every pin, so it is shorted out. Catches the degenerate case only — a connector wired to the wrong net one component away is invisible to it |

## Electrical

| id | severity | catches |
|----|----------|---------|
| `E1-driver-contention` | error (warning on a declared rail) | two push-pull outputs **from different drivers** on one net — including two drivers inside one package, so a 74AHCT541's Y0 shorted to its Y1 is caught. What is *not* contention is one driver bonded out to several pads, which is how power silicon is packaged: the TPS16630's three OUT pins share the name `OUT`, and the pin name is what separates the two cases |
| `E2-no-driver` | warning | a net of inputs with nothing driving it |
| `E3-missing-pullup` | warning | open-drain pins with no pull-up |

## Power

| id | severity | catches |
|----|----------|---------|
| `P1-overvoltage` | error (warning if pin data is unverified) | rail voltage above a pin's `vMax`. Compared against the rail's `voltageMax` when `design.yaml` declares one, otherwise its nominal `voltage` — a bus stated only at nominal passes a pin that the source's +5% tolerance breaks. For a **two-terminal passive**, which has no per-pin `vMax` because the rating belongs to the part, it falls back to the working voltage in the record's `parameters` |
| `P1-undervoltage` | warning | rail below a pin's `vMin`. Compared against the rail's `voltageMin` when `design.yaml` declares one, otherwise its nominal `voltage` — a rail stated only at nominal hides the sag that crosses the floor |
| `P2-no-decoupling` | error | a supply rail with no capacitor to ground |
| `P2-thin-decoupling` | warning | fewer capacitors than supplied **parts** on a rail. Counted per part, not per pad: a part taking four pins off one bus wants one capacitor, not four |

`P1` needs rail voltages declared in `design.yaml`; without them it cannot fire.

## Buses

| id | severity | catches |
|----|----------|---------|
| `B1-missing-bus-net` | error | `design.yaml` names a bus net nothing connects to |
| `B2-i2c-no-pullup` | error | an I2C line with no pull-up resistor |

## Sourcing

| id | severity | catches |
|----|----------|---------|
| `Q1-extended-parts` | info | extended-tier parts, which carry a per-part JLCPCB feeder fee |
| `Q2-low-stock` | warning | fewer than 100 in stock at LCSC |

## Part-specific rules

A part in `kb/parts/` may carry its own rules, reported as `R-<id>`. This is
how a fact read once from a datasheet becomes a check that runs every time.
Supported `requires` values:

- `decoupling` — a capacitor must sit on this pin's net
- `pullup` — a resistor must sit on this pin's net
- `not-pulled-low` — the pin must not be tied to ground (strapping pins)
- `connected` — the pin must not be left floating. The literal net `NC` does
  **not** satisfy this: `NC` records "open on purpose", and a pin the datasheet
  requires to be tied is not allowed one. This is the opposite of K4's reading
  of the same net, deliberately.

## What this does not check

Worth being explicit, because a clean ERC report is not a correct board:

- **Component values.** Nothing verifies that a current-limiting resistor is
  the right resistance, or that a divider produces the intended voltage.
- **Analogue behaviour.** Loop stability, filter response, and regulator
  transient behaviour need SPICE. EasyEDA Pro has ngspice built in; use it on
  the analogue blocks.
- **Pin function compatibility.** The checker knows a pin's electrical type,
  not that `GPIO4` cannot be an ADC input on that particular part.
- **Layout.** Trace width, creepage, impedance, and thermal relief are all
  decided after this, in EasyEDA.
