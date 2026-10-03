#!/usr/bin/env python3
"""Check a document's internal cross-references against its own headings.

    ./tools/xref.py designs/led-matrix-controller/README.md

A 2900-line design document accumulates "see X" pointers, and a section
rename leaves them aimed at nothing. Two in this project pointed at a
"PD front end" heading that had never existed -- the material was real and
three sections away, which is the worst version: the reader does not know
whether they failed to find it or it is gone.

Exit status is 1 on an unresolved reference, so it gates a document.
"""
import pathlib
import re
import sys

HEADING = re.compile(r"^#{2,6}\s+(.+)$", re.M)
# "see X" / "see the X", stopping at the first punctuation. Deliberately
# conservative: a missed reference is better than a false alarm, because
# false alarms are how a checker gets ignored.
REF = re.compile(r"[Ss]ee (?:the )?\*{0,2}([A-Z][A-Za-z0-9 ,'’-]{3,45}?)\*{0,2}"
                 r"(?=[.,;:)\]|]|$)", re.M)
# Targets that are not headings in this file and are not meant to be.
EXTERNAL = {"claude", "recovery and debug for what that leaves"}


def norm(s):
    return s.strip().strip("*").rstrip(".").lower()


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    doc = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    heads = {norm(m.group(1)) for m in HEADING.finditer(doc)}

    refs, bad = {}, []
    for m in REF.finditer(doc):
        t = norm(m.group(1))
        refs[t] = refs.get(t, 0) + 1
    for t, n in sorted(refs.items()):
        if t in EXTERNAL or t.startswith("claude"):
            continue
        if t in heads or any(h.startswith(t) or t in h for h in heads):
            continue
        bad.append((t, n))

    for t, n in bad:
        print(f"  UNRESOLVED  'see {t}' x{n} — no heading matches")
    print(f"\n{len(heads)} heading(s), {len(refs)} distinct reference(s), "
          f"{len(bad)} unresolved")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
