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

ROOTS = ["designs", "kb", "rules"]
SKIP_SUFFIX = {".pdf", ".txt", ".png", ".svg", ".epro2"}


def files():
    for root in ROOTS:
        base = pathlib.Path(root)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.suffix not in SKIP_SUFFIX:
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
