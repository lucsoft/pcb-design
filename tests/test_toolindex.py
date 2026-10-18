#!/usr/bin/env python3
"""The tool list in CLAUDE.md names every tool, and only real ones.

A second list of the tools would be a second thing to maintain, and this
project already has one -- `CLAUDE.md`'s Layout section. So there is no
`tools/README.md`: there is this check, which is what makes the list
worth trusting.

It was written because the list had already drifted. `autoplace.py` was
missing and `mcp.sh` was named where the file is `jlcpcb-mcp.sh` -- a
reader looking for either would have come away with the wrong answer,
and nothing reported it.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def listed():
    """Every tool named in CLAUDE.md's tree, with its one-line summary."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    block = re.search(r"├── tools/\n(.*?)\n    └── tests/", text, re.S)
    if not block:
        return {}
    out = {}
    for line in block.group(1).splitlines():
        m = re.search(r"([\w.-]+\.(?:py|sh))\s+(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def on_disk():
    return {p.name for p in (ROOT / "tools").iterdir()
            if p.suffix in (".py", ".sh") and p.name != "__init__.py"}


def main():
    named, real = listed(), on_disk()
    check("CLAUDE.md's tree lists tools at all", len(named) > 15, len(named))

    missing = sorted(real - set(named))
    check("every tool on disk is in the list", not missing,
          f"not listed: {', '.join(missing)}")

    ghosts = sorted(set(named) - real)
    check("every tool in the list exists", not ghosts,
          f"no such file: {', '.join(ghosts)}")

    # A name with no summary is a line a reader cannot use.
    blank = sorted(k for k, v in named.items() if len(v) < 10)
    check("...and each one says what it does", not blank, blank)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print(f"all {len(named)} tools listed and accounted for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
