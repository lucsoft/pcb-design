#!/usr/bin/env python3
"""Find superseded values that survived an edit.

Six consecutive reviews of designs/led-matrix-controller caught the same
failure: a number corrected in one section and left standing in another.
Prose has no type checker, so this is the substitute -- run it with the OLD
values after any numeric change and require zero unexplained hits.

    ./tools/stale.py '32.4 k' '5.11 k' '4.58 A'
    ./tools/stale.py --design led-matrix-controller --from-file superseded.txt

Hits are printed with file:line and context. A hit is not automatically a
bug: the same digits legitimately appear elsewhere, and a deliberate
"an earlier version said X" note is the correct way to retire a value.
Judge each one -- the tool's job is to make sure none goes unseen.
"""
import argparse, pathlib, re, sys

# tools/ is in here because a numeric claim can hide in a docstring: the
# rail_voltage_min() comment in erc.py carried a retired 4.50 V through two
# review rounds, structurally invisible to the one check that exists for it.
ROOTS = ["designs", "kb", "rules", "tools"]
SKIP_SUFFIX = {".pdf", ".png", ".svg", ".epro2"}
# The extracted datasheet text is the one place a `.txt` is machine output:
# hundreds of pages of numbers off somebody else's datasheet, where a hit
# means nothing. Skipping `.txt` by suffix covered it and `placement.txt`
# with it -- hand-written prose carrying the sheet's measured dimensions,
# invisible to the one check that exists for numeric claims. It took a
# reviewer to find a stale A3 note sitting in that blind spot. Skip the
# directory, not the extension.
SKIP_PARTS = {"datasheets"}


def files():
    for root in ROOTS:
        base = pathlib.Path(root)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix in SKIP_SUFFIX:
                continue
            if SKIP_PARTS & set(f.parts):
                continue
            yield f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("values", nargs="*", help="superseded values to hunt for")
    ap.add_argument("--from-file", help="read one value per line (# comments ok)")
    ap.add_argument("--quiet", action="store_true", help="only print the count")
    args = ap.parse_args()

    needles = list(args.values)
    if args.from_file:
        for line in pathlib.Path(args.from_file).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                needles.append(line)
    if not needles:
        ap.error("give at least one value, or --from-file")

    total = 0
    for needle in needles:
        hits = []
        pat = re.compile(re.escape(needle), re.IGNORECASE)
        for f in files():
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(lines, 1):
                if pat.search(line):
                    hits.append((f, i, line.strip()))
        total += len(hits)
        if hits and not args.quiet:
            print(f"\n=== {needle!r} — {len(hits)} hit(s) ===")
            for f, i, line in hits:
                print(f"  {f}:{i}")
                print(f"      {line[:150]}")

    print(f"\n{total} hit(s) across {len(needles)} value(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
