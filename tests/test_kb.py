#!/usr/bin/env python3
"""Regression tests for the knowledge-base context-coverage check.

    nix-shell --run 'python3 tests/test_kb.py'

A term the JSON-LD context does not define is dropped by a JSON-LD reader
and by nothing else, so this check is the only thing standing between the
knowledge base and the claim in kb/VOCABULARY.md quietly going false. That
makes it exactly the kind of check that can be loosened to nothing without
anyone noticing, which is what these probes are for.

Both probes, as the rule convention requires. The rename probe deletes a
term the data uses and requires the report. The inverse probe injects a key
the tool has never seen -- the case a rename cannot reach, because renaming
something the walker already visits says nothing about whether it would
visit a key that was never there before.
"""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import kb  # noqa: E402

CONTEXT = {
    "@context": {
        "@version": 1.1,
        "pcb": "https://lucsoft.local/ns/pcb#",
        "lcscId": "pcb:lcscId",
        "pins": {"@id": "pcb:hasPin", "@container": "@set"},
        "number": "pcb:pinNumber",
        "type": "pcb:pinType",
        "contact": "pcb:contactDesignation",
        "parameters": {"@id": "pcb:parameter", "@container": "@index"},
        "envelope": {"@id": "pcb:envelope", "@type": "@json"},
    }
}

RECORD = {
    "@context": "../context.jsonld",
    "lcscId": "C1",
    "pins": [{"number": "1", "type": "passive", "contact": "J1-1"}],
    "parameters": {"Capacitance": "33nF"},
}

MODULE = {"@context": "../context.jsonld", "lcscId": "M1",
          "envelope": {"busVoltageRange": {"min": 24, "max": 36}}}


def coverage(context=CONTEXT, record=RECORD, module=MODULE):
    """Run check_context_coverage against a throwaway kb/ and return
    (problem count, printed output)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "kb" / "parts").mkdir(parents=True)
        (root / "kb" / "modules").mkdir(parents=True)
        if context is not None:
            (root / "kb" / "context.jsonld").write_text(json.dumps(context))
        if record is not None:
            (root / "kb" / "parts" / "C1.json").write_text(json.dumps(record))
        if module is not None:
            (root / "kb" / "modules" / "m.json").write_text(json.dumps(module))

        saved = kb.ROOT, kb.PARTS, kb.MODULES
        kb.ROOT = root
        kb.PARTS = root / "kb" / "parts"
        kb.MODULES = root / "kb" / "modules"
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                n = kb.check_context_coverage()
        finally:
            kb.ROOT, kb.PARTS, kb.MODULES = saved
        return n, buf.getvalue()


def test_fully_covered_records_are_silent():
    n, out = coverage()
    assert n == 0, out


def test_rename_probe_a_dropped_term_is_reported():
    # The exact drift this check was written for: a term the documentation
    # promises falls out of the context and the data goes on reading fine.
    ctx = json.loads(json.dumps(CONTEXT))
    del ctx["@context"]["contact"]
    n, out = coverage(context=ctx)
    assert n == 1, out
    assert "contact" in out, out


def test_rename_probe_reports_a_dropped_container_term():
    ctx = json.loads(json.dumps(CONTEXT))
    del ctx["@context"]["pins"]
    n, out = coverage(context=ctx)
    assert n >= 1, out
    assert "pins" in out, out


def test_inverse_probe_an_unseen_key_is_reported():
    # The capture. A key no fixture has ever contained, nested inside a
    # structure the walker does descend into.
    record = json.loads(json.dumps(RECORD))
    record["pins"][0]["whollyNewKey"] = "x"
    n, out = coverage(record=record)
    assert n == 1, out
    assert "whollyNewKey" in out, out


def test_inverse_probe_reaches_assembly_records_too():
    module = json.loads(json.dumps(MODULE))
    module["neverSeenBefore"] = {"a": 1}
    n, out = coverage(module=module)
    assert n == 1, out
    assert "neverSeenBefore" in out, out


def test_json_typed_terms_are_opaque():
    # `envelope` is "@type": "@json", so its value survives verbatim and the
    # keys inside it are data. Reporting them would make the check fire on
    # ~150 legitimate keys in one assembly record, which is how a checker
    # gets ignored.
    module = json.loads(json.dumps(MODULE))
    module["envelope"]["anythingAtAll"] = {"deeply": {"nested": 1}}
    n, out = coverage(module=module)
    assert n == 0, out


def test_index_containers_are_opaque():
    record = json.loads(json.dumps(RECORD))
    record["parameters"]["Some Vendor Parameter"] = "x"
    n, out = coverage(record=record)
    assert n == 0, out


def test_each_undefined_key_is_reported_once():
    # Two records sharing one undefined key is one problem, not two: a
    # per-record report on `designVerdict` would have printed 97 lines.
    record = json.loads(json.dumps(RECORD))
    record["designVerdict"] = "SELECTED"
    n, out = coverage(record=record)
    assert n == 1, out


def test_unreadable_context_is_unchecked_not_clean():
    # Three answers, not two. A missing context must not read as a knowledge
    # base with no problems.
    n, out = coverage(context=None)
    assert n == 0, out
    assert "UNCHECKED" in out, out


def test_malformed_context_is_unchecked_not_clean():
    n, out = coverage(context={"not_a_context": True})
    assert n == 0, out
    assert "UNCHECKED" in out, out


def test_real_kb_has_full_coverage():
    buf = io.StringIO()
    with redirect_stdout(buf):
        n = kb.check_context_coverage()
    assert n == 0, buf.getvalue()
    assert "UNCHECKED" not in buf.getvalue(), buf.getvalue()


CASES = [(name, fn) for name, fn in sorted(globals().items())
         if name.startswith("test_") and callable(fn)]


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
