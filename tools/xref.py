#!/usr/bin/env python3
"""Check a document's internal cross-references against its own headings.

    ./tools/xref.py designs/led-matrix-controller/README.md

A long design document accumulates "see X" pointers, and a section rename
leaves them aimed at nothing. Two in this project pointed at a "PD front end"
heading that had never existed -- the material was real and three sections
away, which is the worst version: the reader cannot tell whether they failed
to find it or it is gone.

**A reference resolves if any leading phrase of it names a heading.** That
matters because "See Recovery and debug for what that leaves" is one sentence
and "Recovery and debug" is the heading: matching the whole captured string
reported it unresolved, and the first version of this tool answered that with
a whitelist entry whose comment said the target was "not a heading in this
file" -- which was false, and is exactly the pattern this project forbids
elsewhere. Matching prefixes fixes the cause instead.

Exit status is 1 on an unresolved reference, so it gates a document.
"""
import pathlib
import re
import sys

HEADING = re.compile(r"^#{2,6}\s+(.+)$", re.M)
# "see X" / "see the X", allowing **emphasis** -- which the first version did
# not, so it reported 22 of the 24 references that exist and a rename probe
# passed when it should have failed. Lower-case targets count too: "see the
# follower section" is a pointer like any other.
# Two deliberate shapes, so a verb use of "see" is not mistaken for a pointer:
# a Capitalised target, or "the <something> table/section". "parts in the
# middle see amplified reflections" matches neither.
REF = re.compile(
    r"[Ss]ee (?:the )?\*{0,2}("
    r"[A-Z][A-Za-z0-9 ,'’-]{2,60}?"
    r"|[a-z][a-z0-9 '’-]{2,40}? (?:table|section)"
    r")\*{0,2}(?=[.,;:)\]|]|$)", re.M)
# Genuinely outside this file. Each needs a reason that is true.
EXTERNAL = {
    "claude",        # CLAUDE.md, the repository's own instructions
    "kb",            # kb/, the knowledge base -- a directory, not a section
}
# Words that end a reference rather than belong to it, so "see X for Y" and
# "see X below" resolve on X.
STOP = {"for", "below", "above", "and", "in", "on", "at", "to", "which",
        "where", "when", "section", "table", "instead", "rather", "it", "them"}
# "see below" and "see above" point at the next paragraph, not at a heading.
# Matched against the WHOLE target, never its first word: "the" in that set
# and a first-word test silently dropped every reference beginning "see The
# ...", which is most of them here -- the same fail-open shape this tool
# exists to catch, introduced while fixing it.
NOT_A_TARGET = {"below", "above", "this", "that", "it", "the"}


def norm(s):
    return s.strip().strip("*").rstrip(".").lower()


def resolves(target, heads):
    """True if the target, or any leading phrase of it, names a heading."""
    words = target.split()
    for n in range(len(words), 0, -1):
        if words[n - 1] in STOP:
            continue                       # do not end a phrase on a stopword
        phrase = " ".join(words[:n])
        if phrase in heads:
            return True
        # A single distinctive word is enough: "see Known" points at
        # "Known electrical limits" and nothing else starts that way.
        if len(phrase) >= 4 and sum(h.startswith(phrase) for h in heads) == 1:
            return True
    return False


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    doc = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    heads = {norm(m.group(1)) for m in HEADING.finditer(doc)}

    refs = {}
    for m in REF.finditer(doc):
        t = norm(m.group(1))
        refs[t] = refs.get(t, 0) + 1

    bad = [(t, n) for t, n in sorted(refs.items())
           if t.split()[0] not in EXTERNAL
           and t not in NOT_A_TARGET
           and not resolves(t, heads)]
    for t, n in bad:
        print(f"  UNRESOLVED  'see {t}' x{n} — no heading matches it, or any "
              f"leading phrase of it")
    print(f"\n{len(heads)} heading(s), {len(refs)} distinct reference(s), "
          f"{len(bad)} unresolved")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
