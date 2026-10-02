#!/usr/bin/env python3
"""Regression tests for the ERC engine.

Each case is a tiny netlist built to trip exactly one rule. The assertion is
that the rule fires — and, for the negative cases, that it stays quiet when
the design is correct, because a checker that always complains gets ignored.

    nix-shell --run 'python3 tests/test_erc.py'
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _erc():
    """Import tools/erc.py directly. The rest of this file shells out to it,
    which is the right way to test findings; a few checks are about helper
    behaviour and need the module itself."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import erc
    return erc

ROOT = Path(__file__).resolve().parent.parent
ERC = ROOT / "tools" / "erc.py"

DESIGN = """
rails:
  GND: {type: ground}
  VBUS: {voltage: 5.0}
  VCC_3V3: {voltage: 3.3}
  V12: {voltage: 12.0}
buses: []
"""

I2C_DESIGN = """
rails:
  GND: {type: ground}
  VCC_3V3: {voltage: 3.3}
buses:
  - {type: i2c, sda: SDA, scl: SCL}
"""


def comp(key, desig, lcsc, pins, **props):
    return {key: {"props": {"Designator": desig, "Supplier Part": lcsc, **props},
                  "pins": pins}}


def run(netlist: dict, design: str = DESIGN) -> list:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "netlist.json").write_text(json.dumps(netlist))
        (d / "design.yaml").write_text(design)
        out = subprocess.run(
            [sys.executable, str(ERC), str(d / "netlist.json"), "--json"],
            capture_output=True, text=True)
        if out.returncode == 2:
            raise RuntimeError(out.stderr)
        return json.loads(out.stdout)


def rules(findings) -> set:
    return {f["rule"] for f in findings}


# A correct LDO stage: both caps present, nothing floating.
GOOD_LDO = {
    **comp("gge1", "U1", "C5446", {"1": "GND", "2": "VCC_3V3", "3": "VBUS"}),
    **comp("gge2", "C1", "C131394", {"1": "VBUS", "2": "GND"}),
    **comp("gge3", "C2", "C131394", {"1": "VCC_3V3", "2": "GND"}),
}

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("clean design produces no errors")
def _():
    found = run(GOOD_LDO)
    errs = [f for f in found if f["severity"] == "error"]
    assert not errs, f"unexpected errors: {[e['rule'] for e in errs]}"


@case("C1 catches a net with a single pin")
def _():
    n = dict(GOOD_LDO)
    n["gge3"]["pins"]["2"] = "ORPHAN"
    assert "C1-single-pin-net" in rules(run(n))


@case("C2 catches nets differing only in punctuation")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge3"]["pins"]["1"] = "VCC3V3"   # vs VCC_3V3
    assert "C2-net-name-collision" in rules(run(n))


@case("C3 suggests the intended net for a one-character typo")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge3"]["pins"]["2"] = "GN"
    found = run(n)
    assert "C3-probable-typo" in rules(found)
    hint = next(f["hint"] for f in found if f["rule"] == "C3-probable-typo")
    assert "GND" in hint, hint


@case("K1 flags a part that is not in the knowledge base")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n.update(comp("gge4", "U2", "C999999999", {"1": "VBUS", "2": "GND"}))
    assert "K1-unknown-part" in rules(run(n))


@case("K3 flags a pin number the part does not have")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge1"]["pins"]["7"] = "GND"
    assert "K3-unknown-pin" in rules(run(n))


@case("K4 flags an unconnected supply pin as an error")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    del n["gge1"]["pins"]["3"]           # drop VIN
    found = run(n)
    assert "K4-unconnected-pin" in rules(found)
    sev = next(f["severity"] for f in found if f["rule"] == "K4-unconnected-pin")
    assert sev == "error", f"a floating supply pin should be an error, got {sev}"


@case("P1 catches a pin driven above its absolute maximum")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge1"]["pins"]["3"] = "V12"       # VIN rated 7.0 V, rail is 12 V
    n["gge2"]["pins"]["1"] = "V12"
    found = run(n)
    assert "P1-overvoltage" in rules(found), rules(found)


@case("P2 catches a supply rail with no capacitor")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    del n["gge2"]                        # drop the input cap
    n = {f"gge{i+1}": v for i, v in enumerate(n.values())}
    assert "P2-no-decoupling" in rules(run(n))


@case("R-vout-cap fires when the LDO loses its stability capacitor")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    del n["gge3"]
    found = run(n)
    assert "R-vout-cap" in rules(found), rules(found)


@case("S1 catches non-sequential component keys")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge9"] = n.pop("gge3")
    assert "S1-key-sequence" in rules(run(n))


@case("S3 catches a reused designator")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge3"]["props"]["Designator"] = "C1"
    assert "S3-duplicate-designator" in rules(run(n))


@case("S4 catches a missing LCSC part number")
def _():
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge3"]["props"]["Supplier Part"] = ""
    assert "S4-missing-lcsc" in rules(run(n))


@case("S6 catches a two-terminal part shorted by its own nets")
def _():
    # The failure this exists for: a sense resistor in a ground return whose
    # load side is simply called GND, so the pour shorts it and the current
    # measurement silently reads zero.
    n = json.loads(json.dumps(GOOD_LDO))
    n["gge2"]["pins"]["1"] = "GND"      # C1 now has GND on both pins
    assert "S6-shorted-two-terminal" in rules(run(n))


@case("S6 does not fire on a capacitor across two different nets")
def _():
    assert "S6-shorted-two-terminal" not in rules(run(GOOD_LDO))


@case("B2 catches an I2C bus with no pull-ups")
def _():
    n = {
        **comp("gge1", "U1", "C5446", {"1": "GND", "2": "VCC_3V3", "3": "SDA"}),
        **comp("gge2", "C1", "C131394", {"1": "VCC_3V3", "2": "GND"}),
        **comp("gge3", "C2", "C131394", {"1": "SCL", "2": "GND"}),
    }
    assert "B2-i2c-no-pullup" in rules(run(n, I2C_DESIGN))


@case("first_word reads the provenance marker off an annotated source")
def _():
    erc = _erc()
    # The failure this exists for: provenance is written for people, so an
    # honest "inferred - by elimination" was matched against the whole string
    # and treated as datasheet-backed.
    assert erc.first_word("inferred - by elimination, the only SPI signal left") == "inferred"
    assert erc.first_word("assumed: default for this package") == "assumed"
    assert erc.first_word("datasheet p.2 PIN ASSIGNMENT") == "datasheet"
    assert erc.first_word("") == ""
    assert erc.first_word(None) == ""
    # Punctuation people actually write. Splitting on a space missed every one
    # of these, and each failed in the direction that raises an error on a guess.
    for s in ("inferred. SOT-23 standard", "assumed; standard pinout",
              "(inferred)", "inferred	by tab", "inferred-by-elimination"):
        assert erc.first_word(s) in erc.UNVERIFIED, s


@case("a pin's own source overrides the record's provenance.pins")
def _():
    erc = _erc()
    # One inferred pin in an otherwise datasheet-backed map must not downgrade
    # the other eleven, and must not be trusted at full severity either.
    comp = erc.Component(
        key="gge1", designator="U1", lcsc="C134462", props={}, pins={},
        kb={"provenance": {"pins": "datasheet p.2 Pin Description"},
            "pins": [{"number": "1", "name": "VDD"},
                     {"number": "6", "name": "MISO",
                      "source": "inferred - by elimination"}]})
    check = erc.Check.__new__(erc.Check)
    assert check.unverified(comp, "pins", "6") is True
    assert check.unverified(comp, "pins", "1") is False
    assert check.unverified(comp, "pins") is False


@case("E1 catches two push-pull outputs on one net")
def _():
    # Two 74AHCT541 outputs tied together. Not a theoretical worry on this
    # board: the status chain and the differential drivers are both push-pull,
    # and a mis-typed net joins them silently.
    n = {
        **comp("gge1", "U1", "C84548", {"20": "VBUS", "10": "GND", "11": "SIG"}),
        **comp("gge2", "U2", "C84548", {"20": "VBUS", "10": "GND", "12": "SIG"}),
    }
    assert "E1-driver-contention" in rules(run(n))


@case("E2 catches a net of inputs with nothing driving it")
def _():
    n = {
        **comp("gge1", "U1", "C84548", {"20": "VBUS", "10": "GND", "2": "FLOATING"}),
        **comp("gge2", "U2", "C84548", {"20": "VBUS", "10": "GND", "3": "FLOATING"}),
    }
    assert "E2-no-driver" in rules(run(n))


@case("E3 catches an open-drain pin with no pull-up")
def _():
    # The INA226's ALERT. This fired for real on the LED matrix controller,
    # where the pull-up was specified in prose and absent from the BOM.
    n = comp("gge1", "U1", "C49851", {"3": "ALERT_N"})
    assert "E3-missing-pullup" in rules(run(n))


@case("E3 is satisfied by a resistor on the net")
def _():
    n = {
        **comp("gge1", "U1", "C49851", {"3": "ALERT_N"}),
        **comp("gge2", "R1", "C25744", {"1": "ALERT_N", "2": "VCC_3V3"}),
    }
    assert "E3-missing-pullup" not in rules(run(n))


@case("P1-undervoltage catches a rail below a pin's minimum")
def _():
    # The 74AHCT541 wants 4.5-5.5 V. Putting it on 3V3 is the mistake the
    # whole dropout analysis exists to keep the design away from.
    n = comp("gge1", "U1", "C84548", {"20": "VCC_3V3", "10": "GND"})
    assert "P1-undervoltage" in rules(run(n))


@case("P1-undervoltage stays quiet on a rail inside the window")
def _():
    n = comp("gge1", "U1", "C84548", {"20": "VBUS", "10": "GND"})
    assert "P1-undervoltage" not in rules(run(n))


@case("P1-undervoltage reads a rail's declared worst case, not its nominal")
def _():
    # The failure this exists for: a rail declared at its nominal hides the sag
    # that actually crosses a pin's floor. 5.0 V nominal passes, 4.4 V worst
    # case does not, and the 74AHCT541 is the part the distinction is about.
    design = DESIGN.replace("VBUS: {voltage: 5.0}",
                            "VBUS: {voltage: 5.0, voltageMin: 4.4}")
    n = comp("gge1", "U1", "C84548", {"20": "VBUS", "10": "GND"})
    assert "P1-undervoltage" not in rules(run(n))
    assert "P1-undervoltage" in rules(run(n, design))


@case("P1-overvoltage reads a rail's declared worst case, not its nominal")
def _():
    # The mirror of the undervoltage case. A PD bus declared at its nominal
    # hides the +5% a compliant fixed PDO may actually sit at, which is the
    # voltage every margin in this design is worked against. The BZT52C20's
    # pins are rated 20 V, so a 19 V nominal passes and a 21 V worst case
    # does not.
    base = DESIGN.replace("V12: {voltage: 12.0}", "V19: {voltage: 19.0}")
    worst = DESIGN.replace("V12: {voltage: 12.0}",
                           "V19: {voltage: 19.0, voltageMax: 21.0}")
    n = comp("gge1", "D1", "C19077415", {"1": "V19", "2": "GND"})
    assert "P1-overvoltage" not in rules(run(n, base))
    assert "P1-overvoltage" in rules(run(n, worst))


def main() -> int:
    passed = failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            print(f"  FAIL  {name}\n          {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"  ok    {name}")
            passed += 1
    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
