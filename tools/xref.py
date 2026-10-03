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
# "see X" / "see the X", allowing **emphasis**, line wraps, Ω and an em dash
# terminator -- every one of those truncated or dropped a real reference in an
# earlier version. The honest way to know the capture is wide enough is not to
# count: it is to rename every referenced heading in turn and check the tool
# reports each one. tests/ does not do that; the probe is five lines and worth
# re-running after any change here.
# Two deliberate shapes, so a verb use of "see" is not mistaken for a pointer:
# a Capitalised target, or "the <something> table/section". "parts in the
# middle see amplified reflections" matches neither.
# `\s` not a literal space, so a reference wrapped across a line is still seen;
# Ω and the em dash are inside the class and the terminator set respectively,
# because both appear in real references here and both used to end the match
# early. The length ceiling is 80: "See Netlist for what is in it and what the
# clean report does not mean" is 62 and was over the old 61.
REF = re.compile(
    r"[Ss]ee\s+(?:the\s+)?\*{0,2}("
    r"[A-Z][A-Za-zΩ0-9\s,'’-]{1,80}?"
    r"|[a-z][a-zΩ0-9\s'’-]{1,60}?\s(?:table|section)"
    r")\*{0,2}\s*(?=[.,;:)\]|—–]|$)")
# Genuinely outside this file. Each needs a reason that is true.
# Matched against the WHOLE target, like NOT_A_TARGET and for the same reason:
# a first-word test here dropped "See KB partitioning scheme" silently.
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


def clauses(heading):
    """A heading and its leading clauses.

    "The follower does not reach the thresholds at 5 V, and the fix is two
    parts" is referred to by its first clause, and "Ethernet module (U3) —
    W5500 on a daughterboard" by its first two words. Both are naming the
    heading, not a different thing, so both resolve -- but only at a break the
    heading itself contains, never at an arbitrary word boundary. That is what
    separates this from prefix matching: `Recovery` and `Recovery and debug`
    stay distinguishable, because neither is a leading clause of the other.
    """
    out = {heading}
    for sep in (",", ";", ":", " (", " — ", " – ", " - "):
        if sep in heading:
            out.add(heading.split(sep)[0].strip())
    return {c for c in out if c}


def resolves(target, heads):
    """True if some leading phrase of the target is EXACTLY a heading.

    Prefix matching was the obvious generalisation and it is fail-open. This
    document has `Recovery` and `Recovery and debug`, and `Status indication`
    beside `Status LEDs: ...` -- so renaming one let its reference re-resolve
    against the sibling, and five of the twenty-one referenced headings could
    be renamed with the checker saying nothing. Two of those five were the
    ones the edits in the same commit existed to protect.

    Exact matching costs abbreviated references ("see Known" for "Known
    electrical limits"), which is the right price: a pointer should name the
    heading it means.
    """
    words = target.split()
    for n in range(len(words), 0, -1):
        if words[n - 1] in STOP:
            continue                       # do not end a phrase on a stopword
        if " ".join(words[:n]) in heads:
            return True
    return False


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    heads = set()
    for m in HEADING.finditer(raw):
        heads |= clauses(norm(m.group(1)))

    # Flatten whitespace before looking for references. With re.M, `$` ends a
    # match at every line break, so a reference wrapped mid-phrase was
    # truncated -- "See\nChannel switching and inrush" came out as "channel
    # switching and" and reported as dangling.
    doc = re.sub(r"\s+", " ", raw)
    refs = {}
    for m in REF.finditer(doc):
        t = norm(m.group(1))
        refs[t] = refs.get(t, 0) + 1

    bad = [(t, n) for t, n in sorted(refs.items())
           if t not in EXTERNAL
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
