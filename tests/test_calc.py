#!/usr/bin/env python3
"""Regression tests for the design calculation model.

    nix-shell --run 'python3 tests/test_calc.py'

The checker's whole value is that a reviewer can stop re-deriving numbers, so
the thing that must never happen is a silent pass. Every case here pairs a
document that trips a finding with one that does not: a rule with only a
positive case can be loosened to nothing without failing the suite.

Three of the cases are inverse probes -- a shape the matcher has never seen
(an anchor with the value *after* it, a unit it does not know, an expression
that will not parse). Each must be REPORTED, at `unchecked` where the tool
genuinely cannot tell and at `error` where the model is broken. A checker that
quietly drops what it cannot read is the failure this one exists to prevent.
"""
import io
import contextlib
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import calc                                                   # noqa: E402

CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


MODEL = """
meta:
  documents: [README.md]
inputs:
  V_BUS:  { value: 36, unit: V, source: "PD contract", provenance: chosen }
  I_LOAD: { value: 4.0, unit: A, source: "ds p.3", provenance: datasheet }
  R_DS:   { value: 30.44, unit: mohm, source: "ds p.1", provenance: datasheet }
  I_LIM:  { value: 6.0, unit: A, source: "ILIM resistor", provenance: datasheet }
derived:
  p_fet:
    expr: I_LOAD ** 2 * R_DS
    unit: W
    display: { unit: mW, digits: 3 }
    limit: { max: 1.0, why: "the package cannot shed more" }
  p_bus:
    expr: V_BUS * I_LOAD
    unit: W
    display: { unit: W, digits: 3 }
"""

DOC = """# d

The FET burns **487 mW**<!--calc:p_fet--> at the full **144 W**<!--calc:p_bus-->.
"""


def run(model=MODEL, doc=DOC, extra_files=None, command="check"):
    """Build a scratch design and return (findings, model)."""
    td = tempfile.mkdtemp()
    d = pathlib.Path(td)
    (d / "calc.yaml").write_text(model, encoding="utf-8")
    (d / "README.md").write_text(doc, encoding="utf-8")
    for name, body in (extra_files or {}).items():
        (d / name).write_text(body, encoding="utf-8")
    m = calc.Model(d)
    if command == "check":
        calc.check_documents(m)
    elif command == "render":
        calc.check_documents(m, write=True)
    return m, d


def levels(m, level):
    return [f for f in m.findings if f.level == level]


def expect(m, level, needle=""):
    hits = [f for f in levels(m, level) if needle.lower() in
            (f.message + " " + f.where).lower()]
    assert hits, (f"expected a {level} mentioning {needle!r}; got: "
                  + "; ".join(f"{f.level} {f.where}: {f.message}" for f in m.findings))
    return hits


def expect_no(m, level, needle=""):
    hits = [f for f in levels(m, level) if needle.lower() in
            (f.message + " " + f.where).lower()]
    assert not hits, (f"unexpected {level}: "
                      + "; ".join(f"{f.where}: {f.message}" for f in hits))


# -- the model itself ------------------------------------------------------

@case("a clean model and document raise nothing")
def t_clean():
    m, _ = run()
    expect_no(m, "error")
    expect_no(m, "unchecked")
    expect_no(m, "warning")


@case("an input without a source is an error")
def t_no_source():
    bad = MODEL.replace('source: "PD contract", ', "")
    m, _ = run(bad)
    expect(m, "error", "no source")


@case("a source that is present is not an error")
def t_source_present():
    m, _ = run()
    expect_no(m, "error", "no source")


@case("an expression naming something undeclared is an error")
def t_unknown_name():
    m, _ = run(MODEL.replace("V_BUS * I_LOAD", "V_RAIL * I_LOAD"))
    expect(m, "error", "unknown 'V_RAIL'")


@case("a dependency cycle is reported, not recursed into")
def t_cycle():
    m, _ = run(MODEL.replace("expr: V_BUS * I_LOAD", "expr: p_bus * 2"))
    expect(m, "error", "cycle")


@case("a limit that is met raises nothing, one that is not is an error")
def t_limit():
    m, _ = run()
    expect_no(m, "error", "violates")
    tight = MODEL.replace("max: 1.0", "max: 0.1")
    m2, _ = run(tight, DOC)
    expect(m2, "error", "violates")


@case("a declared unit the expression contradicts is a warning, not an error")
def t_dimension():
    # power is V*I; writing V/I keeps the number plausible and the unit wrong,
    # which is the exact shape of the bus-current error this design once made.
    m, _ = run(MODEL.replace("expr: V_BUS * I_LOAD", "expr: V_BUS / I_LOAD"))
    expect(m, "warning", "expression gives")
    expect_no(m, "error", "expression gives")


@case("a correct expression raises no dimension warning")
def t_dimension_clean():
    m, _ = run()
    expect_no(m, "warning")


@case("a unit the checker does not know is an error on the entry")
def t_unknown_unit():
    m, _ = run(MODEL.replace("unit: W\n    display: { unit: mW", "unit: furlong\n    display: { unit: mW"))
    expect(m, "error", "not understood")


@case("weak provenance propagates to everything downstream")
def t_weak():
    m, _ = run(MODEL.replace('source: "ds p.1", provenance: datasheet',
                             'source: "guessed", provenance: assumed'))
    assert m.entries["p_fet"].get("_weak") == ["R_DS"], m.entries["p_fet"].get("_weak")
    assert not m.entries["p_bus"].get("_weak")


# -- the document side -----------------------------------------------------

@case("a number that disagrees with the model is an error")
def t_wrong_number():
    m, _ = run(doc=DOC.replace("487 mW", "412 mW"))
    expect(m, "error", "412")


@case("a number that agrees at the precision shown is not an error")
def t_rounding():
    for shown in ("487 mW", "0.487 W", "0.49 W", "490 mW"):
        m, _ = run(doc=f"x **{shown}**<!--calc:p_fet-->\n")
        expect_no(m, "error")


@case("the same digits in the wrong unit is an error")
def t_wrong_magnitude():
    m, _ = run(doc="x **487 W**<!--calc:p_fet-->\n")
    expect(m, "error", "487")


@case("a unit of the wrong dimension is an error, not a silent pass")
def t_wrong_dimension_cited():
    m, _ = run(doc="x **487 mA**<!--calc:p_fet-->\n")
    expect(m, "error", "document shows it in")


@case("the prose's minus sign is a minus sign, not absent")
def t_unicode_minus():
    # U+2212 is what this project's documents write. Reading it as no sign at
    # all turns a negative margin into a positive one, which passes.
    model = MODEL + """
  margin:
    expr: V_BUS - 40
    unit: V
    display: { unit: V, digits: 2 }
"""
    m, _ = run(model, "x **−4.00 V**<!--calc:margin-->\n")
    expect_no(m, "error")
    m2, _ = run(model, "x **4.00 V**<!--calc:margin-->\n")
    expect(m2, "error", "4.00")


@case("an anchor naming nothing in the model is an error")
def t_unknown_anchor():
    m, _ = run(doc="x **487 mW**<!--calc:p_gate-->\n")
    expect(m, "error", "unknown 'p_gate'")


@case("INVERSE PROBE: an anchor with its value after it is reported")
def t_anchor_after():
    m, _ = run(doc="x <!--calc:p_fet--> **487 mW**\n")
    expect(m, "unchecked", "no number in front")


@case("INVERSE PROBE: a prose unit the checker cannot parse is reported")
def t_prose_unit():
    m, _ = run(doc="x **487 zonk**<!--calc:p_fet-->\n")
    expect(m, "unchecked", "not a unit")


@case("INVERSE PROBE: a cell that will not evaluate is an error")
def t_bad_cell():
    model = MODEL + """
tables:
  t:
    columns: [a, b]
    rows:
      - ["x", "=p_fet +"]
"""
    m, _ = run(model, DOC + "\n<!-- calc:table t -->\n<!-- calc:end -->\n")
    expect(m, "error", "does not evaluate")


@case("a cell escaped with a backslash stays text")
def t_escaped_cell():
    model = MODEL + """
tables:
  t:
    columns: [a, b]
    rows:
      - ["x", "\\\\= I^2 R"]
"""
    m, d = run(model, DOC + "\n<!-- calc:table t -->\n<!-- calc:end -->\n",
               command="render")
    expect_no(m, "error")
    assert "= I^2 R" in (d / "README.md").read_text()


@case("a cell unit in brackets scales without printing itself")
def t_bare_unit():
    model = MODEL + """
tables:
  t:
    columns: ["what", "mW"]
    rows:
      - ["FET", "=p_fet @ [mW],3"]
      - ["bus", "=p_bus @ mW,3"]
"""
    m, d = run(model, DOC + "\n<!-- calc:table t -->\n<!-- calc:end -->\n",
               command="render")
    text = (d / "README.md").read_text()
    assert "| FET | 487 |" in text, text          # scaled, unit not printed
    assert "| bus | 144000 mW |" in text, text    # scaled, unit printed


@case("a table that drifted from the model is an error, and render fixes it")
def t_table():
    model = MODEL + """
tables:
  power:
    columns: ["what", "value"]
    align: [left, right]
    rows:
      - ["FET", "=p_fet"]
      - ["bus", "=p_bus"]
"""
    body = DOC + "\n<!-- calc:table power -->\n| what | value |\n|---|---|\n| FET | 1 W |\n<!-- calc:end -->\n"
    m, d = run(model, body)
    expect(m, "error", "does not match the model")

    m2, d2 = run(model, body, command="render")
    text = (d2 / "README.md").read_text()
    assert "| FET | 487 mW |" in text, text
    # and the regenerated document is now clean, which is the half that proves
    # render writes what check wants rather than merely writing something
    (d2 / "calc.yaml").write_text(model, encoding="utf-8")
    m3 = calc.Model(d2)
    calc.check_documents(m3)
    expect_no(m3, "error")


@case("render is idempotent")
def t_idempotent():
    model = MODEL + """
tables:
  power:
    columns: ["what", "value"]
    rows:
      - ["FET", "=p_fet"]
"""
    body = DOC + "\n<!-- calc:table power -->\n<!-- calc:end -->\n"
    m, d = run(model, body, command="render")
    once = (d / "README.md").read_text()
    m2 = calc.Model(d)
    calc.check_documents(m2, write=True)
    assert (d / "README.md").read_text() == once


@case("a quantity cited nowhere is reported at info, not silently")
def t_uncited():
    m, _ = run(doc="x **487 mW**<!--calc:p_fet-->\n")
    expect(m, "info", "cited nowhere")
    expect_no(m, "error", "cited nowhere")


@case("uncited: true silences exactly that one")
def t_uncited_opt_out():
    m, _ = run(MODEL.replace("  p_bus:\n", "  p_bus:\n    uncited: true\n"),
               doc="x **487 mW**<!--calc:p_fet-->\n")
    expect_no(m, "info", "p_bus")


@case("a range anchor checks both ends")
def t_range():
    model = MODEL + """
  p_lo:
    expr: p_fet * 0.9
    unit: W
    display: { unit: mW, digits: 3 }
  p_hi:
    expr: p_fet * 1.1
    unit: W
    display: { unit: mW, digits: 3 }
"""
    m, _ = run(model, "x **438-536 mW**<!--calc:p_lo..p_hi-->\n")
    expect_no(m, "error")
    m2, _ = run(model, "x **438-999 mW**<!--calc:p_lo..p_hi-->\n")
    expect(m2, "error", "999")


@case("a range may be written with a dash, 'to' or 'and'")
def t_range_separators():
    model = MODEL + """
  p_lo:
    expr: p_fet * 0.9
    unit: W
    display: { unit: mW, digits: 3 }
  p_hi:
    expr: p_fet * 1.1
    unit: W
    display: { unit: mW, digits: 3 }
"""
    for text in ("438-536 mW", "438 to 536 mW", "438 mW and 536 mW",
                 "438 mW to 536 mW", "438–536 mW"):
        m, _ = run(model, f"x **{text}**<!--calc:p_lo..p_hi-->\n")
        expect_no(m, "error")
        expect_no(m, "unchecked")
    m, _ = run(model, "x **438 to 999 mW**<!--calc:p_lo..p_hi-->\n")
    expect(m, "error", "999")


# -- the graph -------------------------------------------------------------

@case("affected names everything downstream and nothing else")
def t_affected():
    m, _ = run()
    assert m.dependents("I_LOAD") == {"p_fet", "p_bus"}
    assert m.dependents("R_DS") == {"p_fet"}
    assert m.dependents("p_fet") == set()


@case("values() refuses a model with errors rather than returning numbers")
def t_values_fail_closed():
    _, d = run(MODEL.replace('source: "PD contract", ', ""))
    try:
        calc.values(d)
    except ValueError:
        return
    raise AssertionError("values() returned numbers from a model with errors")


@case("values() returns SI base units")
def t_values_si():
    _, d = run()
    v = calc.values(d)
    assert abs(v["R_DS"] - 0.03044) < 1e-9, v["R_DS"]
    assert abs(v["p_bus"] - 144.0) < 1e-9, v["p_bus"]


# -- units -----------------------------------------------------------------

@case("the unit parser handles what this project writes")
def t_units():
    for text, scale, dims in (
            ("V", 1.0, {"V": 1}),
            ("mA", 1e-3, {"A": 1}),
            ("µF", 1e-6, {"A": 1, "s": 1, "V": -1}),
            ("mΩ", 1e-3, {"V": 1, "A": -1}),
            ("kohm", 1e3, {"V": 1, "A": -1}),
            ("%", 0.01, {}),
            ("", 1.0, {}),
            ("mm2", 1e-6, {"m": 2}),
            ("ohm*mm2/m", 1e-6, {"V": 1, "A": -1, "m": 1}),
            ("W", 1.0, {"V": 1, "A": 1}),
            ("ms", 1e-3, {"s": 1}),
            ("min", 60.0, {"s": 1}),
    ):
        got_scale, got_dims = calc.parse_unit(text)
        assert abs(got_scale - scale) < 1e-15 * max(1, scale), (text, got_scale)
        assert got_dims == dims, (text, got_dims)


@case("an unknown unit raises rather than defaulting to 1")
def t_unit_unknown():
    for text in ("zonk", "Vv", "q/s"):
        try:
            calc.parse_unit(text)
        except calc.UnitError:
            continue
        raise AssertionError(f"{text!r} parsed as a unit")


@case("significant figures are counted as the prose writes them")
def t_sigfigs():
    for text, n in (("487", 3), ("0.72", 2), ("11.6", 3), ("2.0", 2),
                    ("100", 1), ("0.010", 2), ("36", 2)):
        assert calc.sigfigs(text) == n, (text, calc.sigfigs(text))


def main():
    passed = failed = 0
    for name, fn in CASES:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}\n      {e}")
        except Exception as e:                                 # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n      {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
