#!/usr/bin/env python3
"""Regression tests for the cross-reference checker.

    nix-shell --run 'python3 tests/test_xref.py'

`tools/xref.py` had three fail-open regressions in three consecutive commits,
each found by an ad-hoc probe that was never committed. Two kinds of probe are
needed and only one is obvious:

- **the rename probe** -- rename a heading and require a report. It exercises
  resolution, and it is what caught prefix matching absorbing a renamed
  heading into its sibling.
- **the inverse probe** -- inject a reference the tool has never seen the shape
  of and require a report. It exercises CAPTURE, which the rename probe cannot
  reach by construction: a reference the regex never matches is invisible to
  it, and that is how an allow-list character class hid four dangling shapes.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import xref                                                   # noqa: E402

CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


def check(body):
    """Run the tool over a scratch document; return (exit code, output)."""
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "doc.md"
        f.write_text(body, encoding="utf-8")
        buf = io.StringIO()
        argv = sys.argv
        sys.argv = ["xref", str(f)]
        try:
            with contextlib.redirect_stdout(buf):
                rc = xref.main()
        finally:
            sys.argv = argv
        return rc, buf.getvalue()


DOC = "## Real heading\n\n## Other heading, with a clause\n\ntext\n\n"


@case("a reference to a heading resolves")
def _():
    for ref in ("See Real heading.",
                "See Real heading for the rest.",
                "see *Real heading*;",
                "See\nReal heading.",                  # wrapped mid-reference
                "See Other heading, with a clause.",
                "See Other heading."):                 # a leading clause
        rc, out = check(DOC + ref + "\n")
        assert rc == 0, (ref, out)


@case("a dangling reference is reported, in every shape")
def _():
    # The inverse probe. Each of these was silently accepted at some point:
    # a bracket, a slash, an ampersand, a leading digit, a long tail, an Ω,
    # an em-dash terminator, and a lower-case "the <x> table" pointer.
    for ref in ("See Gone heading.",
                "See Gone heading (U3).",
                "See Gone/Missing heading.",
                "See Gone & Missing heading.",
                "See 5 V behaviour heading.",
                "See Gone heading " + "x" * 60 + ".",
                "See Gone heading at 10 Ω.",
                "See Gone heading — and more.",
                "See the dropout table.",
                "see the follower section."):
        rc, out = check(DOC + ref + "\n")
        assert rc == 1, f"not reported: {ref!r}\n{out}"


@case("a verb use of 'see' is not a reference")
def _():
    for line in ("Parts in the middle see amplified reflections.",
                 "The modules see the bus up to that point.",
                 "The detectors see both edges.",
                 "See below for why.",
                 "See above."):
        rc, out = check(DOC + line + "\n")
        assert rc == 0, (line, out)


@case("renaming any referenced heading is reported")
def _():
    doc = ("## Recovery and debug\n\n## Flashing\n\ntext\n\n"
           "See Recovery and debug for what that leaves.\nSee Flashing.\n")
    assert check(doc)[0] == 0
    for head in ("## Recovery and debug", "## Flashing"):
        rc, out = check(doc.replace(head, "## ZZQQ renamed"))
        assert rc == 1, f"renaming {head!r} was not reported\n{out}"


@case("two headings where one opens the other are reported as ambiguous")
def _():
    # No matcher can report a rename of "Recovery and debug" while "Recovery"
    # survives: the reference to the longer one contains the shorter as a
    # leading phrase of itself. Unfixable in the matcher, fixable in the
    # document -- so the checker reports the structure rather than pretending.
    doc = ("## Recovery\n\n## Recovery and debug\n\ntext\n\n"
           "See Recovery and debug for what that leaves.\n")
    rc, out = check(doc)
    assert rc == 1 and "AMBIGUOUS" in out, out
    # A heading and its own clauses are not two headings.
    ok = ("## HUSB238A (U1) — the PD controller\n\ntext\n\nSee HUSB238A.\n")
    rc, out = check(ok)
    assert rc == 0, out


@case("EXTERNAL and NOT_A_TARGET match whole targets, not first words")
def _():
    # A first-word test here dropped "See KB partitioning scheme" silently,
    # and "the" in the set dropped every "see The ..." reference.
    rc, out = check(DOC + "See KB partitioning scheme.\n")
    assert rc == 1, out
    rc, out = check(DOC + "See KB.\n")
    assert rc == 0, out


def main():
    passed = failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            print(f"  FAIL  {name}\n          {exc}")
            failed += 1
        else:
            print(f"  ok    {name}")
            passed += 1
    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
