#!/usr/bin/env python3
"""Split one netlist into several sheets, one importable JSON per sheet.

Two independent reviewers of `designs/led-matrix-controller` reached the
same conclusion from different directions: on a single A1 the sheet caps
out around 3,0 to 3,5, and the one step that buys a full grade is putting
the twelve functional blocks on three or four sheets. Neither of this
format's two hard limits -- no junction record, no free text -- stands in
its way.

The reason is not room. It is that **a cross-sheet net is a label by
right**, where a cross-block net on one sheet is a wire somebody failed
to draw. 51 of this board's 108 nets stay labels today and most of them
span blocks; divided along the same seams they stop being a shortfall and
start being the normal way a multi-sheet schematic reads.

    ./tools/split.py designs/<name> --dry-run
    ./tools/split.py designs/<name> -o designs/<name>/sheets

The grouping comes from `sheets:` in design.yaml -- sheet name to a list
of block names, using the same block names `arrange.py` reads from the
generator's `# ---- Name ----` comments. A block in no sheet is an error
and so is a block in two: silently dropping a block would lose components
from the board, and silently duplicating one would place them twice.

**What this does not do is make the sheets.** Each output is a netlist in
the importer's format; EasyEDA still has to import each one, and the
arrange chain then runs per sheet against its own export. That round trip
cannot be skipped from here.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arrange import read_blocks                      # noqa: E402


def read_sheets(yml: Path) -> dict[str, list[str]]:
    """`sheets:` as sheet name -> [block name].

    The same deliberately small reader `arrange.py` uses for `blocks:`,
    for the same reason: one mapping is not worth a YAML dependency, and
    the shape is fixed by this tool's own documentation.
    """
    if not yml.exists():
        return {}
    out: dict[str, list[str]] = {}
    inside, cur, pending = False, None, None
    for line in yml.read_text(encoding="utf-8").splitlines():
        if re.match(r"^sheets:\s*$", line):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            inside = False
        if not inside:
            continue
        if pending is not None:
            pending += " " + line.strip()
            if "]" in pending:
                name, _, items = pending.partition(":")
                out[name.strip()] = [
                    s.strip() for s in
                    items.strip().strip("[]").split(",") if s.strip()]
                pending = None
            continue
        m = re.match(r"^\s+(.+?):\s*\[(.*)$", line)
        if m:
            if "]" in m.group(2):
                out[m.group(1).strip()] = [
                    s.strip() for s in
                    m.group(2).split("]")[0].split(",") if s.strip()]
            else:
                pending = line.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design", type=Path, help="designs/<name>")
    ap.add_argument("-o", "--out", type=Path,
                    help="directory for the per-sheet netlists")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    nl = a.design / "netlist.json"
    if not nl.exists():
        print(f"error: {nl} not found", file=sys.stderr)
        return 2
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2

    design = json.loads(nl.read_text(encoding="utf-8"))
    order = list(design.values())
    desig = [c["props"]["Designator"] for c in order]
    blocks = read_blocks(nl, set(desig))

    sheets = read_sheets(a.design / "design.yaml")
    if not sheets:
        print(f"error: no `sheets:` mapping in {a.design / 'design.yaml'}.\n"
              f"  Give it sheet name -> [block name], using the block names\n"
              f"  arrange.py reads. The blocks on this board are:\n"
              + "".join(f"    {b}\n" for b in sorted(set(blocks.values()))),
              file=sys.stderr)
        return 1

    # --- every block on exactly one sheet ---------------------------------
    # Dropping one loses components off the board and duplicating one
    # places them twice; both are silent in every downstream tool, because
    # each sheet is checked on its own and looks complete.
    allblocks = set(blocks.values())
    seen: dict[str, str] = {}
    twice, unknown = [], []
    for sh, bs in sheets.items():
        for b in bs:
            if b not in allblocks:
                unknown.append((sh, b))
            elif b in seen:
                twice.append((b, seen[b], sh))
            else:
                seen[b] = sh
    missing = sorted(allblocks - set(seen))
    if unknown or twice or missing:
        for sh, b in unknown[:6]:
            print(f"  error: sheet {sh!r} names block {b!r}, which does not "
                  f"exist", file=sys.stderr)
        for b, s1, s2 in twice[:6]:
            print(f"  error: block {b!r} is on both {s1!r} and {s2!r}",
                  file=sys.stderr)
        for b in missing[:6]:
            print(f"  error: block {b!r} is on no sheet", file=sys.stderr)
        print(f"\n  the sheet map does not partition the board; "
              f"nothing written\n", file=sys.stderr)
        return 1

    # --- deal the components out ------------------------------------------
    perSheet: dict[str, list] = {sh: [] for sh in sheets}
    for c in order:
        perSheet[seen[blocks[c["props"]["Designator"]]]].append(c)

    pins_in = sum(len(c["pins"]) for c in order)
    netpins: dict[str, set] = collections.defaultdict(set)
    for sh, cs in perSheet.items():
        for c in cs:
            for n in c["pins"].values():
                netpins[n].add(sh)

    print(f"\n{nl}")
    print(f"  {len(order)} components, {pins_in} pin connections, "
          f"{len(netpins)} nets")
    print(f"\n  {len(sheets)} sheets:")
    pins_out = 0
    written = []
    for sh in sheets:
        cs = perSheet[sh]
        pins = sum(len(c["pins"]) for c in cs)
        pins_out += pins
        own = {n for n, s in netpins.items() if s == {sh}}
        shared = {n for n, s in netpins.items() if sh in s and len(s) > 1}
        print(f"    {sh:28s} {len(cs):3d} parts  {pins:4d} pins  "
              f"{len(own):3d} nets of its own, {len(shared):3d} crossing")
        out = {f"gge{i + 1}": c for i, c in enumerate(cs)}
        written.append((sh, out))

    # --- the proof ---------------------------------------------------------
    # Not "it looks right": the components and the pin connections are
    # counted on both sides. A sheet map that drops a block passes every
    # per-sheet check there is, because each sheet is complete in itself.
    got = sum(len(o) for _, o in written)
    if got != len(order) or pins_out != pins_in:
        print(f"\n  error: {got} components and {pins_out} pins out of "
              f"{len(order)} and {pins_in} in — nothing written\n",
              file=sys.stderr)
        return 1
    dupes = collections.Counter(
        c["props"]["Designator"] for _, o in written for c in o.values())
    repeated = [d for d, n in dupes.items() if n > 1]
    if repeated:
        print(f"  error: {', '.join(sorted(repeated)[:8])} appear on more "
              f"than one sheet — nothing written\n", file=sys.stderr)
        return 1
    print(f"\n  {got} of {len(order)} components and {pins_out} of {pins_in} "
          f"pin connections, each on exactly one sheet")

    crossing = sorted(n for n, s in netpins.items() if len(s) > 1)
    print(f"  {len(crossing)} net(s) cross a sheet boundary and are labels "
          f"by right there: " + ", ".join(crossing[:6])
          + (" ..." if len(crossing) > 6 else ""))

    if a.dry_run:
        print("\n  --dry-run: nothing written\n")
        return 0

    a.out.mkdir(parents=True, exist_ok=True)

    # One directory per sheet, each with its own design.yaml. The arrange
    # chain reads `design.yaml` beside the netlist and wants a block for
    # every designator it finds there -- so a shared file carrying all
    # twelve blocks makes every sheet demand geometry for the whole board
    # and refuse. Each sheet gets only its own blocks.
    rails, keep_line = [], False
    # `dnf:` has to travel too. It is the only machine-readable record
    # that a pad is placed and must not be fitted, and a sheet that
    # loses it reports those parts as ordinary -- and, since
    # blocktitles prints the reason beside the part, says nothing on
    # the drawing either. Per sheet, only the designators on it.
    dnf_lines, in_dnf, dnf_cur = [], False, None
    for line in (a.design / "design.yaml").read_text(
            encoding="utf-8").splitlines():
        if line.startswith("rails:"):
            keep_line = True
        elif line and not line[0].isspace():
            keep_line = False
        if keep_line:
            rails.append(line)
        if re.match(r"^dnf:\s*$", line):
            in_dnf = True
            continue
        if in_dnf:
            if line and not line[0].isspace():
                in_dnf = False
            else:
                m = re.match(r"^  (\S[^:]*):", line)
                if m:
                    dnf_cur = m.group(1).strip()
                if dnf_cur:
                    dnf_lines.append((dnf_cur, line))

    for sh, o in written:
        slug = re.sub(r"[^a-z0-9]+", "-", sh.lower()).strip("-")
        d = a.out / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "netlist.json").write_text(json.dumps(o, indent=2) + "\n",
                                        encoding="utf-8")
        mine = collections.defaultdict(list)
        for c in o.values():
            des_ = c["props"]["Designator"]
            mine[blocks[des_]].append(des_)
        yml = [f"# Sheet {sh!r}, written by tools/split.py.",
               "#",
               "# The rails come from the design's own design.yaml. The",
               "# block map is carried explicitly because the generator's",
               "# `# ---- Name ----` comments do not travel with a split",
               "# netlist, and a designator in no block is an error in",
               "# arrange.py -- placing it somewhere by default would put",
               "# a part in the wrong block silently.", ""]
        yml += rails
        mine_dnf = [ln for d_, ln in dnf_lines
                    if d_ in {c["props"]["Designator"] for c in o.values()}]
        if mine_dnf:
            yml += ["", "dnf:"] + mine_dnf
        yml += ["", "blocks:"]
        for b in sorted(mine):
            yml.append(f"  {b}: [{', '.join(sorted(mine[b]))}]")
        (d / "design.yaml").write_text("\n".join(yml) + "\n",
                                       encoding="utf-8")
        print(f"  wrote {d}/netlist.json and its design.yaml")

    print(f"\n  import each with Netlist Rebuild, export it, then run the "
          f"arrange chain per sheet\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
