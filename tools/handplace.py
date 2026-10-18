#!/usr/bin/env python3
"""Place components at hand-written coordinates.

`arrange.py` packs blocks by rule. This one does what the rules are
trying to approximate: it takes a file saying where each part goes and
puts it there. The point is not to lay out a board by hand -- 129 parts
is too many -- but to **produce the reference an automatic layout can be
judged against**, on one sheet, so "the packer is not as good as a human"
stops being an opinion and becomes a diff.

    ./tools/handplace.py <sheet.epro2> <placement.txt> -o out.epro2

The placement file is one line per component:

    U4   0     0    0      # designator, anchor x, anchor y, rotation
    C8  -300  -100  270

Coordinates are canvas units (1 unit = 10 mil = 0.254 mm), the same
space the export uses; more negative y is **up**. Rotation is absolute
and one of 0/90/180/270. Blank lines and `#` comments are ignored, and
a component the file does not name stays where it is.

**Attribution is by pin coincidence, not by the cell grid.** `arrange.py`
infers a uniform grid because that is what the importer produces; a file
this tool has already touched has no such grid, and neither does one a
human has been editing. A wire group belongs to the component whose pin
one of its ends sits on, which is the same rule `rotate_cluster` uses and
is true of any file.

The safety argument is `arrange.py`'s, unchanged: rotate the cluster
about its own anchor, then translate it rigidly, so every coincidence
inside the cluster survives and every wire still meets the pin it met.
That is checked after writing -- anchor-relative geometry per component,
identical or the output is deleted -- and `epro.py diff` still has to be
run on top, because the fingerprint proves the geometry and the diff
proves the nets.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arrange import (ROT, anchor_geometry, read_records,    # noqa: E402
                     reread, rotate_cluster, translate)


def read_placement(path: Path) -> dict[str, tuple[int, int, int]]:
    """designator -> (x, y, rotation), from the hand-written file."""
    out, bad = {}, []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) not in (3, 4):
            bad.append((lineno, raw, "want `designator x y [rotation]`"))
            continue
        d = parts[0]
        try:
            x, y = int(parts[1]), int(parts[2])
            r = int(parts[3]) % 360 if len(parts) == 4 else 0
        except ValueError:
            bad.append((lineno, raw, "x, y and rotation must be integers"))
            continue
        if r not in ROT:
            bad.append((lineno, raw, f"rotation {r} is not 0/90/180/270"))
            continue
        if d in out:
            bad.append((lineno, raw, f"{d} is placed twice"))
            continue
        out[d] = (x, y, r)
    if bad:
        for lineno, raw, why in bad[:10]:
            print(f"  error: line {lineno}: {why}\n           {raw}",
                  file=sys.stderr)
        raise SystemExit(1)
    return out


def cluster_owner(lines):
    """Attribute every canvas record to the component it belongs to.

    By pin coincidence: a wire group is owned by the component whose pin
    one of its ends sits on. An attribute is owned by its parent, whether
    that parent is a component or one of those wire groups.
    """
    desig = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in lines
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    sympins = collections.defaultdict(list)
    for h, b, doc, _ in lines:
        if h and h["type"] == "PIN" and doc and doc[0] == "SYMBOL":
            sympins[doc[1]].append(b)

    owner_at, comp_id = {}, {}
    for h, b, doc, _ in lines:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or doc is None or doc[0] not in ("SCH", "SCH_PAGE")
                or h["id"] not in desig):
            continue
        d = desig[h["id"]]
        comp_id[h["id"]] = d
        r = (b.get("rotation") or 0) % 360
        if r not in ROT:
            continue
        for q in sympins.get(symref.get(h["id"]), []):
            lx = -q.get("x", 0) if b.get("isMirror") else q.get("x", 0)
            dx, dy = ROT[r](lx, q.get("y", 0))
            owner_at[(round(b["x"] + dx), round(b["y"] + dy))] = d

    group = {}
    for h, b, doc, _ in lines:
        if (h and h["type"] == "LINE" and b.get("lineGroup")
                and doc and doc[0] in ("SCH", "SCH_PAGE")):
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                d = owner_at.get((round(x), round(y)))
                if d:
                    group[b["lineGroup"]] = d

    of_record = {}
    for i, (h, b, doc, _) in enumerate(lines):
        if h is None or doc is None or doc[0] not in ("SCH", "SCH_PAGE"):
            continue
        d = None
        if h["type"] == "COMPONENT":
            d = comp_id.get(h["id"])
        elif h["type"] == "WIRE":
            d = group.get(h["id"])
        elif h["type"] == "LINE":
            d = group.get(b.get("lineGroup"))
        elif h["type"] == "ATTR":
            pid = b.get("parentId")
            d = comp_id.get(pid) or group.get(pid)
        if d is not None:
            of_record[i] = d
    return of_record, {d: d for d in set(of_record.values())}


def boxes(lines, of_record, owner):
    """designator -> (x0, y0, x1, y1), every coordinate it owns."""
    out = {}
    for i, (h, b, _, _) in enumerate(lines):
        d = owner.get(of_record.get(i))
        if d is None:
            continue
        for ax, ay in (("x", "y"), ("startX", "startY"), ("endX", "endY")):
            if isinstance(b.get(ax), (int, float)) and \
                    isinstance(b.get(ay), (int, float)):
                x0, y0, x1, y1 = out.get(d, (b[ax], b[ay], b[ax], b[ay]))
                out[d] = (min(x0, b[ax]), min(y0, b[ay]),
                          max(x1, b[ax]), max(y1, b[ay]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("placement", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pad", type=int, default=10,
                    help="clearance required between two components (units)")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    for p in (a.export, a.placement):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 2

    want = read_placement(a.placement)
    name, lines = read_records(a.export)
    of_record, owner = cluster_owner(lines)
    present = set(owner)

    print(f"\n{a.export.name} <- {a.placement.name}")
    print(f"  {len(present)} placed components, "
          f"{len(of_record)} canvas records attributed")

    unknown = sorted(set(want) - present)
    if unknown:
        print(f"  error: the placement names {', '.join(unknown[:8])}, which "
              f"{'is' if len(unknown) == 1 else 'are'} not on this sheet",
              file=sys.stderr)
        return 1
    # A part the file forgets keeps its imported position, which is in the
    # middle of the grid the rest has just been moved out of. Silence there
    # would read as "placed deliberately".
    unplaced = sorted(present - set(want))
    if unplaced:
        print(f"  {len(unplaced)} component(s) not in the placement, left "
              f"where they are: {', '.join(unplaced[:8])}"
              + (" ..." if len(unplaced) > 8 else ""))

    # --- rotate first ------------------------------------------------------
    # The extent of a part depends on its rotation, so the overlap check
    # has to see the turned boxes, not the imported ones.
    turns = {d: r for d, (_, _, r) in want.items()}
    lines2, turned, nrot = rotate_cluster(lines, turns)
    if turned:
        lines2 = read_records_from(lines2)
        of_record, owner = cluster_owner(lines2)
    else:
        lines2 = lines
    print(f"  {len(turned)} component(s) rotated ({nrot} records)")

    anchors = {}
    for i, (h, b, _, _) in enumerate(lines2):
        d = owner.get(of_record.get(i))
        if h is not None and h["type"] == "COMPONENT" and d is not None \
                and isinstance(b.get("x"), (int, float)):
            anchors[d] = (b["x"], b["y"])

    deltas = {d: (want[d][0] - anchors[d][0], want[d][1] - anchors[d][1])
              for d in want if d in anchors}
    missing = sorted(set(want) - set(anchors))
    if missing:
        print(f"  error: no anchor found for {', '.join(missing[:8])}",
              file=sys.stderr)
        return 1

    # --- overlap check -----------------------------------------------------
    # Two symbols pushed together can put a stub end on a neighbour's pin,
    # which is a new connection the net diff cannot see.
    box = boxes(lines2, of_record, owner)
    final = {}
    for d, (x0, y0, x1, y1) in box.items():
        dx, dy = deltas.get(d, (0, 0))
        final[d] = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
    hit = []
    ds = sorted(final)
    for i, p in enumerate(ds):
        for q in ds[i + 1:]:
            ax0, ay0, ax1, ay1 = final[p]
            bx0, by0, bx1, by1 = final[q]
            if (ax0 - a.pad < bx1 and bx0 - a.pad < ax1
                    and ay0 - a.pad < by1 and by0 - a.pad < ay1):
                hit.append((p, q))
    if hit:
        for p, q in hit[:10]:
            print(f"  OVERLAP {p} and {q}")
        print(f"\n  {len(hit)} overlapping pair(s) at --pad {a.pad} — a stub "
              f"end landing on a neighbour's pin is a connection, and the "
              f"net diff cannot see it\n", file=sys.stderr)
        return 1

    xs = [v for d in final for v in (final[d][0], final[d][2])]
    ys = [v for d in final for v in (final[d][1], final[d][3])]
    w, h = int(max(xs) - min(xs)), int(max(ys) - min(ys))
    print(f"  sheet {w} x {h} units = {w * 0.254:.0f} x {h * 0.254:.0f} mm")
    moved = sum(1 for dx, dy in deltas.values() if dx or dy)
    print(f"  {moved} of {len(present)} components move")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    before = anchor_geometry(lines2, of_record, owner)

    out_lines, touched = [], 0
    for i, (h, b, _, raw) in enumerate(lines2):
        d = owner.get(of_record.get(i))
        dx, dy = deltas.get(d, (0, 0)) if d is not None else (0, 0)
        if h is None or (not dx and not dy):
            out_lines.append(raw)
            continue
        nb = translate(json.loads(json.dumps(b)), dx, dy)
        out_lines.append(f"{json.dumps(h, separators=(',', ':'))}||"
                         f"{json.dumps(nb, separators=(',', ':'))}|")
        touched += 1

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))
    print(f"  {touched} records translated")

    # --- the proof ---------------------------------------------------------
    after = reread(a.out, lines2, of_record, owner)
    drift = [d for d in before if before[d] != after.get(d)]
    gone = sorted(set(before) - set(after))
    if drift or gone:
        for d in drift[:8]:
            print(f"  DRIFT  {d}: local geometry changed — not a translation")
        for d in gone[:8]:
            print(f"  LOST   {d}: no longer in the output")
        a.out.unlink(missing_ok=True)
        print(f"\n  {len(drift) + len(gone)} component(s) did not move "
              f"rigidly; {a.out} deleted\n", file=sys.stderr)
        return 1
    print(f"  {len(before)} components: local geometry unchanged, so every "
          f"wire still meets the pin it met")
    print(f"\n  now prove the nets:  ./tools/epro.py diff {a.out} "
          f"<netlist.json>\n")
    return 0


def read_records_from(raw_lines):
    """Re-parse rotate_cluster's output, which is raw text lines."""
    out, cur = [], None
    for line in raw_lines:
        if "||" not in line:
            out.append((None, None, None, line))
            continue
        head, _, body = line.partition("||")
        try:
            h = json.loads(head)
            b = json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            out.append((None, None, None, line))
            continue
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        out.append((h, b, cur, line))
    return out


if __name__ == "__main__":
    sys.exit(main())
