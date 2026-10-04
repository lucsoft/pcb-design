#!/usr/bin/env python3
"""Regression tests for the OKF v0.2 conformance check.

    nix-shell --run 'python3 tests/test_okf.py'

Every rule gets two probes, because one is not enough and this project has
the scars to prove it:

- a **rename probe** breaks a conforming bundle one field at a time and
  requires the matching rule to fire. It exercises the matching.
- an **inverse probe** feeds a shape the tool has never seen and requires it
  to be reported rather than waved through. It exercises the capture, which
  the rename probe cannot reach by construction -- a document the scanner
  never looks at is invisible to every rename you could make inside it.

A rule with only a positive case can be loosened to nothing without failing
the suite. So each case also asserts the rule stays QUIET on the good
fixture: a check that fires on a correct bundle is a false positive, and
false positives are how a checker gets ignored.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OKF = ROOT / "tools" / "okf.py"

GOOD = """---
type: Design Record
title: A board
description: One sentence.
status: stable
generated: { by: human:lucsoft, at: 2026-10-03T08:32:59+02:00 }
verified:
  - { by: process:erc, at: 2026-10-04T10:33:16Z }
stale_after: 2099-01-01T00:00:00Z
sources:
  - id: netlist
    resource: /designs/x/netlist.json
    title: The netlist
---

# Body

A claim that cites its source.[^netlist]

[^netlist]: The netlist
"""

ROOT_INDEX = """---
okf_version: "0.2"
---

# Section

* [A board](board.md) - One sentence.
"""


def run(files):
    """Write a bundle to a temp dir and return okf.py's JSON findings."""
    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td)
        for name, text in files.items():
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(OKF), str(bundle), "--json"],
            capture_output=True, text=True)
        assert proc.returncode in (0, 1), proc.stderr
        return json.loads(proc.stdout)


def rules(report):
    return {f["rule"] for f in report["findings"]}


def unchecked(report):
    return {u["where"] for u in report["unchecked"]}


# --------------------------------------------------------------- the baseline

def test_good_bundle_is_silent():
    report = run({"index.md": ROOT_INDEX, "board.md": GOOD})
    assert not report["findings"], report["findings"]
    assert not report["unchecked"], report["unchecked"]
    assert report["concepts"] == 1, report
    assert report["reserved"] == 1, report


# ------------------------------------------------------- §11.1 / §11.2 errors

def test_missing_frontmatter_is_an_error():
    report = run({"board.md": "# Just a heading\n\nNo frontmatter here.\n"})
    assert "O1-no-frontmatter" in rules(report)


def test_unclosed_frontmatter_is_an_error():
    report = run({"board.md": "---\ntype: Design Record\n\n# Body\n"})
    assert "O1-no-frontmatter" in rules(report)


def test_unparseable_yaml_is_an_error():
    report = run({"board.md": "---\ntype: [unclosed\n---\n\n# Body\n"})
    assert "O1-no-frontmatter" in rules(report)


def test_missing_type_is_an_error():
    report = run({"board.md": GOOD.replace("type: Design Record\n", "", 1)})
    assert "O2-no-type" in rules(report)


def test_empty_type_is_an_error():
    report = run({"board.md": GOOD.replace("type: Design Record", 'type: ""')})
    assert "O2-no-type" in rules(report)


def test_unknown_type_is_accepted():
    # §4.1/§11: type values are not registered centrally and consumers MUST
    # tolerate unknown ones. A tool that enumerated valid types would reject
    # conforming bundles.
    report = run({"board.md": GOOD.replace("type: Design Record",
                                           "type: Wholly Invented Kind")})
    assert not rules(report), report["findings"]


# ------------------------------------------------- §5 families, opt-in only

def test_absent_families_are_not_findings():
    # §11: a concept carrying just `type` is fully conformant. This is the
    # case that keeps the optional families from becoming de-facto required.
    report = run({"board.md": "---\ntype: Reference\n---\n\n# Body\n"})
    assert not rules(report), report["findings"]


def test_bare_actor_without_prefix_warns():
    report = run({"board.md": GOOD.replace("by: human:lucsoft", "by: lucsoft")})
    assert "O5-actor-format" in rules(report)


def test_agent_actor_form_is_accepted():
    report = run({"board.md": GOOD.replace("by: human:lucsoft",
                                           "by: claude-code/claude-opus-5")})
    assert "O5-actor-format" not in rules(report), report["findings"]


def test_bare_verified_mapping_is_one_element_list():
    # §11 makes this a MUST for consumers: a single verifier may be written
    # without the list dash.
    text = GOOD.replace(
        "verified:\n  - { by: process:erc, at: 2026-10-04T10:33:16Z }",
        "verified: { by: process:erc, at: 2026-10-04T10:33:16Z }")
    report = run({"board.md": text})
    assert not rules(report), report["findings"]


def test_timestamp_without_offset_warns():
    report = run({"board.md": GOOD.replace("at: 2026-10-04T10:33:16Z",
                                           "at: 2026-10-04T10:33:16")})
    assert "O6-timestamp-format" in rules(report)


def test_date_only_timestamp_warns():
    # PyYAML turns this into a datetime.date, not a string. Checking only the
    # string form would miss it, which is the whole reason as_aware_datetime
    # looks at the parsed type.
    report = run({"board.md": GOOD.replace("at: 2026-10-04T10:33:16Z",
                                           "at: 2026-10-04")})
    assert "O6-timestamp-format" in rules(report)


def test_source_without_resource_is_an_error():
    report = run({"board.md": GOOD.replace(
        "    resource: /designs/x/netlist.json\n", "")})
    assert "O7-source-no-resource" in rules(report)


def test_scope_descriptor_is_a_valid_resource():
    # §5.1: a resource may name a population the consumer cannot follow.
    report = run({"board.md": GOOD.replace(
        "resource: /designs/x/netlist.json",
        "resource: every part record under /kb/parts/ this design cites")})
    assert "O7-source-no-resource" not in rules(report), report["findings"]


def test_unknown_status_warns():
    report = run({"board.md": GOOD.replace("status: stable",
                                           "status: finalised")})
    assert "O8-status-unknown" in rules(report)


def test_past_stale_after_warns():
    report = run({"board.md": GOOD.replace("stale_after: 2099-01-01T00:00:00Z",
                                           "stale_after: 2020-01-01T00:00:00Z")})
    assert "O9-stale" in rules(report)


def test_footnote_not_matching_a_source_is_info_only():
    text = GOOD.replace("[^netlist]", "[^elsewhere]")
    report = run({"board.md": text})
    assert "O10-footnote-unmatched" in rules(report)
    severities = {f["severity"] for f in report["findings"]
                  if f["rule"] == "O10-footnote-unmatched"}
    assert severities == {"info"}, severities


def test_footnotes_are_ignored_without_declared_source_ids():
    # A document that never opted into attribution uses footnotes as ordinary
    # markdown, and the rule must not fire on it.
    text = """---
type: Reference
---

# Body

An aside.[^1]

[^1]: Just a footnote.
"""
    report = run({"board.md": text})
    assert "O10-footnote-unmatched" not in rules(report), report["findings"]


# ------------------------------------------------------ §11.3, §8 and §9

def test_index_outside_root_may_not_carry_frontmatter():
    report = run({"index.md": ROOT_INDEX, "board.md": GOOD,
                  "sub/index.md": '---\nokf_version: "0.2"\n---\n\n# S\n'})
    assert "O3-index-structure" in rules(report)


def test_root_index_may_carry_only_okf_version():
    report = run({"index.md": '---\nokf_version: "0.2"\ntype: Index\n---\n\n# S\n'})
    assert "O3-index-structure" in rules(report)


def test_wrong_okf_version_warns():
    report = run({"index.md": ROOT_INDEX.replace('"0.2"', '"9.9"')})
    assert "O3-index-structure" in rules(report)


def test_log_date_heading_must_be_iso():
    report = run({"log.md": "# Log\n\n## 22 May 2026\n* **Update**: a thing.\n"})
    assert "O4-log-structure" in rules(report)


def test_iso_log_is_silent():
    report = run({"log.md": "# Log\n\n## 2026-05-22\n* **Update**: a thing.\n"})
    assert "O4-log-structure" not in rules(report), report["findings"]


def test_broken_cross_link_is_not_a_finding():
    # §6.1/§11: consumers MUST tolerate broken links -- a link may simply
    # represent not-yet-written knowledge. Reporting one would make the tool
    # non-conformant itself.
    text = GOOD + "\nSee the [missing concept](/nowhere.md).\n"
    report = run({"index.md": ROOT_INDEX, "board.md": text})
    assert not rules(report), report["findings"]


def test_missing_index_is_not_a_finding():
    report = run({"board.md": GOOD})
    assert not rules(report), report["findings"]


# --------------------------------------------------------- the inverse probes

def test_inverse_probe_a_new_document_is_captured():
    # The capture, not the matching. Every rename probe above edits a file the
    # scanner already looks at, so none of them can tell whether the scanner
    # would find a document it has never been pointed at. Drop a
    # non-conforming file into a subdirectory nothing references and require
    # it to be reported.
    report = run({"index.md": ROOT_INDEX, "board.md": GOOD,
                  "deep/nested/unreferenced.md": "# No frontmatter at all\n"})
    assert "O1-no-frontmatter" in rules(report)
    where = {f["where"] for f in report["findings"]}
    assert any("unreferenced.md" in w for w in where), where


def test_inverse_probe_an_unseen_extension_is_out_of_scope():
    # The complement: OKF constrains .md files only. A JSON knowledge-base
    # record is not a concept document, and a tool that grew to report it
    # would fail the whole repository for conforming data.
    report = run({"index.md": ROOT_INDEX, "board.md": GOOD,
                  "kb/parts/C5446.json": '{"@type": "Component"}\n'})
    assert not rules(report), report["findings"]
    assert report["concepts"] == 1, report


def test_exit_status_gates_a_build():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "board.md").write_text("# No frontmatter\n")
        proc = subprocess.run([sys.executable, str(OKF), td, "--no-colour"],
                              capture_output=True, text=True)
        assert proc.returncode == 1, proc.returncode
        Path(td, "board.md").write_text(GOOD)
        proc = subprocess.run([sys.executable, str(OKF), td, "--no-colour"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout


def test_real_bundle_conforms():
    proc = subprocess.run([sys.executable, str(OKF), str(ROOT), "--json"],
                          capture_output=True, text=True)
    report = json.loads(proc.stdout)
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    assert not errors, errors
    assert not report["unchecked"], report["unchecked"]


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
