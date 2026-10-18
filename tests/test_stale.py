#!/usr/bin/env python3
"""Regression tests for the superseded-value hunt.

    nix-shell --run 'python3 tests/test_stale.py'

`tools/stale.py` skipped `.txt` by suffix. That is right for the extracted
datasheet text -- hundreds of pages of numbers off somebody else's part,
where a hit means nothing -- and wrong for `placement.txt`, which is
hand-written prose carrying the supply sheet's measured dimensions. A stale
A3 note sat in that blind spot through several review rounds and only a
human caught it, in the one check that exists for exactly that failure.

Both directions, per the house rule. The rename probe is the obvious one:
a needle in a hand-written `.txt` must be found. The inverse is what makes
it a test rather than a claim -- a needle in the cached datasheet text must
NOT be found, because dropping the suffix skip without the directory guard
would silently start reporting every number off every cached datasheet.
"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import stale                                                  # noqa: E402

CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


def hunt(tree, needle):
    """Run stale.py over a scratch tree; return (hit count, output)."""
    old_roots = stale.ROOTS
    stale.ROOTS = [str(tree)]
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["stale", needle]
    try:
        with contextlib.redirect_stdout(buf):
            rc = stale.main()
    finally:
        sys.argv = argv
        stale.ROOTS = old_roots
    return rc, buf.getvalue()


def tree(files):
    td = tempfile.TemporaryDirectory()
    root = pathlib.Path(td.name)
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return td, root


@case("a value in a hand-written .txt is found")
def _():
    # The blind spot. `placement.txt` is prose, not machine output, and the
    # measured dimensions it carries are the numbers a reviewer checks the
    # layout notes against.
    td, root = tree({"designs/x/sheets/supply/placement.txt":
                     "# the drawing is 1079 x 709 units\n"})
    rc, out = hunt(root, "1079 x 709")
    assert rc == 1, f"a prose .txt was not scanned\n{out}"
    assert "placement.txt" in out, out
    td.cleanup()


@case("the cached datasheet text is not scanned")
def _():
    # The inverse. Without it, removing `.txt` from SKIP_SUFFIX would pass
    # the case above while quietly turning every run into a report of every
    # number in 47 cached datasheets.
    td, root = tree({"kb/datasheets/C2890041.txt":
                     "Thermal Resistance 1079 x 709 units\n"})
    rc, out = hunt(root, "1079 x 709")
    assert rc == 0, f"datasheet text was scanned\n{out}"
    td.cleanup()


@case("the guard is on the directory, not the extension")
def _():
    # A `.txt` under a directory that merely *contains* the word does not
    # count; only the datasheet cache itself is machine output. This is the
    # shape that makes the fix a directory rule rather than a name match.
    td, root = tree({"designs/x/datasheets-notes.txt":
                     "the drawing is 1079 x 709 units\n"})
    rc, out = hunt(root, "1079 x 709")
    assert rc == 1, f"a prose .txt beside a datasheet dir was skipped\n{out}"
    td.cleanup()


@case("markdown and JSON are still scanned")
def _():
    for rel in ("designs/x/README.md", "kb/parts/C5446.json",
                "rules/README.md", "tools/some.py"):
        td, root = tree({rel: "the drawing is 1079 x 709 units\n"})
        rc, out = hunt(root, "1079 x 709")
        assert rc == 1, f"{rel} was skipped\n{out}"
        td.cleanup()


@case("binary and generated artefacts are still skipped")
def _():
    for rel in ("designs/x/figures/f.png", "designs/x/figures/f.svg",
                "designs/x/a.epro2", "kb/datasheets/C1.pdf"):
        td, root = tree({rel: "the drawing is 1079 x 709 units\n"})
        rc, out = hunt(root, "1079 x 709")
        assert rc == 0, f"{rel} was scanned\n{out}"
        td.cleanup()


@case("a value that is gone reports nothing")
def _():
    # The retire-in-the-commit rule, as a check: the old number must not
    # still be standing anywhere the tool looks.
    td, root = tree({"designs/x/README.md": "the drawing is 1079 x 709 units\n"})
    rc, out = hunt(root, "990 x 779")
    assert rc == 0, out
    td.cleanup()


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
