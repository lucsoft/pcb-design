#!/usr/bin/env python3
"""Carve one sheet's components out of a whole-board export.

`split.py` writes a netlist per sheet, and importing each one into
EasyEDA is the supported path. This is the shortcut: the whole-board
export already contains every SYMBOL and DEVICE document and every
placed component, so a sheet can be cut from it directly and the arrange
chain run without a round trip through the editor.

    ./tools/carve.py <export.epro2> <sheet.json> -o sheet.epro2

**This deletes, where `arrange.py` only translates, so the safety
argument has to be different.** Translation preserves every coincidence
inside a cell and therefore every connection. Deletion cannot: removing
a component removes its pins from the nets they were on. So the proof is
the other one this project already trusts — `epro.py diff` against the
sheet's own netlist, which counts pin references per net and so catches
both a part left behind and a stub removed that should have stayed.

Library documents are kept whole. An unused SYMBOL or DEVICE costs a few
kilobytes and nothing else, where pruning them risks removing one a
remaining part still points at — and a component whose device is missing
imports as nothing at all.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import zipfile
from pathlib import Path

ROT = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru, found {len(eprus)}")
    name = eprus[0]
    rows, cur = [], None
    for line in z.read(name).decode("utf-8").split("\n"):
        if "||" not in line:
            rows.append((None, None, None, line))
            continue
        head, _, body = line.partition("||")
        try:
            h = json.loads(head)
            b = json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            rows.append((None, None, None, line))
            continue
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        rows.append((h, b, cur, line))
    return name, rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("sheet", type=Path, help="one of split.py's netlists")
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args()
    for p in (a.export, a.sheet):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 2

    design = json.loads(a.sheet.read_text(encoding="utf-8"))
    keep = {c["props"]["Designator"] for c in design.values()}
    name, rows = load(a.export)

    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    sympins = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and doc and doc[0] == "SYMBOL" and h["type"] == "PIN":
            sympins[doc[1]].append(b)

    # Which component each pin coordinate belongs to, so a stub can be
    # attributed to the part it leaves. A stub whose part goes must go
    # with it, or the sheet keeps a wire to nothing -- which still
    # carries a net name and so would read as a connection.
    owner_at: dict[tuple[int, int], str] = {}
    drop_comp = set()
    for h, b, doc, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or doc is None or doc[0] not in ("SCH", "SCH_PAGE")):
            continue
        d = desig.get(h["id"])
        if d is None:
            continue
        if d not in keep:
            drop_comp.add(h["id"])
        r = (b.get("rotation") or 0) % 360
        if r not in ROT:
            continue
        for q in sympins.get(symref.get(h["id"]), []):
            lx = -q.get("x", 0) if b.get("isMirror") else q.get("x", 0)
            dx, dy = ROT[r](lx, q.get("y", 0))
            owner_at[(round(b["x"] + dx), round(b["y"] + dy))] = d

    drop_wire = set()
    for h, b, doc, _ in rows:
        if (not h or h["type"] != "LINE" or not b.get("lineGroup")
                or doc is None or doc[0] not in ("SCH", "SCH_PAGE")):
            continue
        ends = ((round(b["startX"]), round(b["startY"])),
                (round(b["endX"]), round(b["endY"])))
        owners = {owner_at[e] for e in ends if e in owner_at}
        # A stub with no owner at either end belongs to nothing this tool
        # placed; leaving it would keep a named wire with no pin on it.
        if owners and not (owners & keep):
            drop_wire.add(b["lineGroup"])

    out_lines, dropped = [], collections.Counter()
    for h, b, doc, raw in rows:
        if h is None:
            out_lines.append(raw)
            continue
        if doc and doc[0] in ("SCH", "SCH_PAGE"):
            pid = b.get("parentId")
            if h["type"] == "COMPONENT" and h["id"] in drop_comp:
                dropped["component"] += 1
                continue
            if pid in drop_comp:
                dropped["attribute"] += 1
                continue
            if h["type"] == "WIRE" and h["id"] in drop_wire:
                dropped["wire"] += 1
                continue
            if b.get("lineGroup") in drop_wire or pid in drop_wire:
                dropped["segment"] += 1
                continue
        out_lines.append(raw)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    src = zipfile.ZipFile(a.export)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    print(f"\n{a.export.name} -> {a.sheet.name}")
    print(f"  keeping {len(keep)} of {len(desig)} components")
    print("  dropped " + ", ".join(f"{n} {k}(s)"
                                   for k, n in sorted(dropped.items())))

    # --- the proof ---------------------------------------------------------
    _, back = load(a.out)
    left = {desig[h["id"]] for h, b, doc, _ in back
            if h and h["type"] == "COMPONENT" and b.get("partId")
            and doc and doc[0] in ("SCH", "SCH_PAGE")
            and h["id"] in desig}
    extra, short = sorted(left - keep), sorted(keep - left)
    if extra or short:
        a.out.unlink(missing_ok=True)
        for d in (extra + short)[:8]:
            where = "still present" if d in extra else "missing"
            print(f"  WRONG  {d} is {where}", file=sys.stderr)
        print(f"\n  the carved sheet does not hold exactly its own parts; "
              f"{a.out} deleted\n", file=sys.stderr)
        return 1
    print(f"  {len(left)} components, exactly the sheet's own")
    print(f"\n  now prove the nets:  ./tools/epro.py diff {a.out} {a.sheet}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
