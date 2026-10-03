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
# The character class is now "anything but a terminator", not a list of
# allowed characters. A list is a fail-open design: every character missing
# from it -- `(`, `/`, `&`, a digit at the start -- silently truncated or
# dropped a reference, and the rename probe cannot see that, because it only
# exercises references the regex already captures. The inverse probe is in
# tests/test_xref.py.
# Two deliberate shapes, so "the modules see the bus up to that point" is not
# read as a pointer: a Capitalised target, or "the <something> table/section".
# Within a shape the class is "anything but a terminator" rather than a list
# of allowed characters -- a list is fail-open, and every character missing
# from the previous one (`(`, `/`, `&`, a leading digit) silently truncated or
# dropped a reference. The rename probe cannot see that, because it exercises
# only references the regex already captures; tests/test_xref.py has the
# inverse probe that can.
REF = re.compile(
    r"[Ss]ee\s+(?:the\s+)?\*{0,2}("
    r"[A-Z0-9][^.,;:)\]|—–*\n]{1,120}?"
    r"|[a-z][^.,;:)\]|—–*\n]{1,100}?\s(?:table|section)"
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
# "see below for why ..." is a direction, not a heading, whatever follows --
# so this one IS a first-word rule, deliberately, and it is a shape rather
# than an identity. EXTERNAL and NOT_A_TARGET match whole targets.
DIRECTIONS = {"below", "above"}


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


def ambiguous(names):
    """FULL heading names where one opens another.

    Compared against full names, never against the clause set: "HUSB238A" and
    "HUSB238A (U1)" are two clauses of one heading, and reporting that pair
    would be noise about a section that cannot be confused with itself.

    No matcher can report a rename of `Recovery and debug` while `Recovery`
    survives, because the reference to the longer one contains the shorter as
    a leading phrase of itself. That is not fixable in the matcher -- it is
    fixable in the document, by not giving two sections names where one opens
    the other. So the checker reports the structure instead of pretending.
    """
    out = []
    for h in sorted(names):
        for other in names:
            if other != h and other.startswith(h + " "):
                out.append((h, other))
                break
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    heads, names = set(), set()
    for m in HEADING.finditer(raw):
        n = norm(m.group(1))
        names.add(n)
        heads |= clauses(n)

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
           and t.split()[0] not in DIRECTIONS
           and not resolves(t, heads)]
    pairs = ambiguous(names)
    for t, n in bad:
        print(f"  UNRESOLVED  'see {t}' x{n} — no heading matches it, or any "
              f"leading phrase of it")
    for short, long in pairs:
        print(f"  AMBIGUOUS   '{short}' opens '{long}' — a reference to the "
              f"longer one also matches the shorter, so renaming it could "
              f"not be reported")
    print(f"\n{len(heads)} heading(s), {len(refs)} distinct reference(s), "
          f"{len(bad)} unresolved, {len(pairs)} ambiguous heading pair(s)")
    return 1 if bad or pairs else 0


if __name__ == "__main__":
    sys.exit(main())
