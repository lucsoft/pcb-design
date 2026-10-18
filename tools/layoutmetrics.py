#!/usr/bin/env python3
"""Measure what a reader sees, not only what collides.

Every check this project had about a drawn sheet was a **zero bound**:
label on a body, label on a foreign wire, text on text, stub end too
close to a neighbour. They count how often a distance is too *small*.
Readability dies at both ends, and nothing here measured the other one.

That gap is not academic. A hand-laid supply sheet and an automatic one
were graded 3,0 and 4,5 by an independent reviewer, and every one of the
five numbers the project already had said the automatic sheet was the
better of the two. Three of those five do not change at all if you
delete every wire from both files.

    ./tools/layoutmetrics.py <sheet.epro2> <netlist.json>
    ./tools/layoutmetrics.py <sheet.epro2> <netlist.json> --json

So, four upper bounds:

**detour** -- per drawn wire, path length over the Manhattan distance
between the two pins it joins. A straight run is 1,0. Reported as the
median, the count above 1,3 and the count with three or more corners.

**span** -- per net, the half-perimeter of the bounding box over *all*
its pins including flagged ones, against the sheet's own half-perimeter.
This is the one that sees two capacitors on the same node standing 400
units apart, which no collision check can.

**reversal** -- pairs of parts whose order along x contradicts the order
of their rails in the power tree. A buck's output capacitor sitting to
the left of its input capacitor is legible to a netlist and to nobody
else.

**enclosed** -- area ringed by wires that contains no symbol, over the
sheet area. A routed loop around an IC reads as a part outline and is
not one.

Nothing here is a pass/fail gate: there is no threshold these should
meet, only two files to compare. Exit status is 0 unless the file cannot
be read.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schwire import grid, load, pin_positions, stub_nets   # noqa: E402
from arrange import read_rail_kinds                        # noqa: E402

import hashlib


def drawn_nets(rows, nets):
    """net -> [segments], for the wires schwire added.

    Those carry no NET attribute -- connectivity stays on the stubs --
    so they are found the way schwire mints them: the wire id is
    sha256("wire/<net>")[:16].
    """
    byid = {hashlib.sha256(f"wire/{n}".encode()).hexdigest()[:16]: n
            for n in nets}
    segs = collections.defaultdict(list)
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup") in byid:
            segs[byid[b["lineGroup"]]].append(
                (b["startX"], b["startY"], b["endX"], b["endY"]))
    return segs


def flag_points(rows):
    """Where the rail flags sit. A flag carries no Designator."""
    hasdesig = {b["parentId"] for h, b, _, _ in rows
                if h and h["type"] == "ATTR" and b.get("key") == "Designator"
                and b.get("value")}
    return {grid(b["x"], b["y"]) for h, b, doc, _ in rows
            if h and h["type"] == "COMPONENT" and doc
            and doc[0] in ("SCH", "SCH_PAGE") and h["id"] not in hasdesig
            and isinstance(b.get("x"), (int, float))}


def corners(segs):
    """How many times a polyline changes direction."""
    dirs = []
    for x1, y1, x2, y2 in segs:
        if x1 == x2 and y1 == y2:
            continue
        dirs.append("h" if y1 == y2 else "v")
    return sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("netlist", type=Path)
    ap.add_argument("--design", type=Path,
                    help="design.yaml, for the rail order (default: beside "
                         "the netlist)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    for q in (a.export, a.netlist):
        if not q.exists():
            print(f"error: {q} not found", file=sys.stderr)
            return 2

    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    pinsof = {c["props"]["Designator"]: c["pins"] for c in design.values()}
    allnets = {n for p in pinsof.values() for n in p.values()}

    name, rows = load(a.export)
    pins = pin_positions(rows)
    at = stub_nets(rows)
    flagpt = flag_points(rows)

    xs = [x for _, x, _ in pins] + [p[0] for p in flagpt]
    ys = [y for _, _, y in pins] + [p[1] for p in flagpt]
    if not xs:
        print("error: no pins found", file=sys.stderr)
        return 2
    sheet_w, sheet_h = max(xs) - min(xs), max(ys) - min(ys)
    half = (sheet_w + sheet_h) or 1

    out: dict = {"sheet": [sheet_w, sheet_h]}

    # --- detour ------------------------------------------------------------
    # Per drawn net: the path it takes against the straight line it could
    # have taken. 1.0 is a run with no corner in it.
    segs = drawn_nets(rows, allnets)
    det, bends = [], 0
    for n, ss in segs.items():
        drawn = sum(abs(x2 - x1) + abs(y2 - y1) for x1, y1, x2, y2 in ss)
        pts = sorted({(x, y) for _, x, y in pins if n in at.get((x, y), ())})
        if len(pts) < 2 or not drawn:
            continue
        # The shortest tree joining the pins is at least the half-perimeter
        # of their bounding box; using the two-pin Manhattan distance would
        # make every three-pin net look like a detour.
        floor = (max(p[0] for p in pts) - min(p[0] for p in pts)
                 + max(p[1] for p in pts) - min(p[1] for p in pts)) or 1
        det.append((drawn / floor, n))
        if corners(ss) >= 3:
            bends += 1
    det.sort()
    med = det[len(det) // 2][0] if det else 0.0
    bad = [n for u, n in det if u > 1.3]
    out["detour"] = {"median": round(med, 2), "over_1_3": len(bad),
                     "three_or_more_corners": bends, "drawn": len(det),
                     "worst": [[round(u, 2), n] for u, n in det[-3:][::-1]]}

    # --- span --------------------------------------------------------------
    # Over *all* pins of a net, flagged ones included. Two capacitors on
    # one node standing 400 units apart is invisible to every collision
    # check there is, and is the single commonest reason a sheet reads as
    # scattered.
    netpts = collections.defaultdict(set)
    for d, x, y in pins:
        for n in at.get((x, y), ()):
            netpts[n].add((x, y))
    spans = []
    for n, pts in netpts.items():
        if len(pts) < 2 or n.upper().startswith("NC_"):
            continue
        s = (max(p[0] for p in pts) - min(p[0] for p in pts)
             + max(p[1] for p in pts) - min(p[1] for p in pts)) / half
        spans.append((s, n))
    spans.sort()
    out["span"] = {"over_0_4": sum(1 for s, _ in spans if s > 0.4),
                   "nets": len(spans),
                   "worst": [[round(s, 2), n] for s, n in spans[-3:][::-1]]}

    # --- reversal ----------------------------------------------------------
    # The power tree gives the rails an order; x on the sheet should not
    # contradict it. A part fed from 3V3 sitting left of one fed from VBUS
    # is correct to a netlist and backwards to a reader.
    yml = a.design or a.netlist.with_name("design.yaml")
    kinds = read_rail_kinds(yml) if yml.exists() else {}
    supply = [r for r, spec in kinds.items() if spec != "ground"]
    # Declaration order in design.yaml is the tree order; a rail the file
    # does not name cannot be placed in it, and guessing one would be the
    # fail-open shape this project warns about.
    rank = {r: i for i, r in enumerate(supply)}
    cx, cy = {}, {}
    for d, x, y in pins:
        cx.setdefault(d, []).append(x)
        cy.setdefault(d, []).append(y)
    centre = {d: sum(v) / len(v) for d, v in cx.items()}
    yext = {d: (min(v) - 60, max(v) + 60) for d, v in cy.items()}
    fed = {}
    for d, ps in pinsof.items():
        rs = [rank[n] for n in ps.values() if n in rank]
        if rs and d in centre:
            fed[d] = min(rs)
    # Only within a band. A sheet that stacks two converters one above
    # the other reads correctly left-to-right inside each of them, and
    # comparing a part in the upper band against one in the lower asks a
    # question the reader never asks -- it scored the hand-laid sheet
    # 16 against the scattered one's 15, which is the metric measuring
    # the stacking rather than the flow.
    rev, cmp_ = 0, 0
    parts = sorted(fed)
    for i, p in enumerate(parts):
        for q in parts[i + 1:]:
            if fed[p] == fed[q]:
                continue
            if yext[p][1] < yext[q][0] or yext[q][1] < yext[p][0]:
                continue
            cmp_ += 1
            lo, hi = (p, q) if fed[p] < fed[q] else (q, p)
            if centre[lo] > centre[hi]:
                rev += 1
    out["reversal"] = {"pairs": rev, "compared": cmp_,
                       "ranked_parts": len(fed), "rails_in_order": supply}

    # --- contact -----------------------------------------------------------
    # Two nets whose segments cross or share a point. In a geometric
    # schematic that is a connection, and `epro.py diff` reads the
    # declared names -- so a drawn short is invisible to it. The only
    # thing that answers the question is arithmetic on the coordinates.
    #
    # A reviewer asked for exactly this before accepting any further
    # layout work: "solange unklar ist, ob die Senkrechte das LX_3V3-Netz
    # berührt, ist jede weitere Umsortierung Arbeit auf einem Blatt, das
    # möglicherweise einen Kurzschluss zeichnet."
    byid = {hashlib.sha256(f"wire/{n}".encode()).hexdigest()[:16]: n
            for n in allnets}
    netof_ = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "NET"
              and b.get("value")}
    frame_ = {hashlib.sha256(s).hexdigest()[:16]
              for s in (b"frame/iso", b"frame/sheet")}
    wseg = []
    for h, b, doc, _ in rows:
        if (not h or h["type"] != "LINE" or not b.get("lineGroup")
                or b["lineGroup"] in frame_ or doc is None
                or doc[0] not in ("SCH", "SCH_PAGE")):
            continue
        nm = netof_.get(b["lineGroup"]) or byid.get(b["lineGroup"])
        if nm is None:
            continue
        a_ = grid(b["startX"], b["startY"])
        c_ = grid(b["endX"], b["endY"])
        if a_ != c_:
            wseg.append((a_, c_, nm))

    def _on(s_, q_):
        (x1, y1), (x2, y2) = s_[0], s_[1]
        if x1 == x2:
            return q_[0] == x1 and min(y1, y2) <= q_[1] <= max(y1, y2)
        if y1 == y2:
            return q_[1] == y1 and min(x1, x2) <= q_[0] <= max(x1, x2)
        return False

    touch = []
    for i_ in range(len(wseg)):
        for j_ in range(i_ + 1, len(wseg)):
            a_, b_ = wseg[i_], wseg[j_]
            if a_[2] == b_[2]:
                continue
            ah_ = a_[0][1] == a_[1][1]
            bh_ = b_[0][1] == b_[1][1]
            hit = None
            if ah_ != bh_:
                h_, v_ = (a_, b_) if ah_ else (b_, a_)
                px_, py_ = v_[0][0], h_[0][1]
                if (min(h_[0][0], h_[1][0]) <= px_ <= max(h_[0][0], h_[1][0])
                        and min(v_[0][1], v_[1][1]) <= py_
                        <= max(v_[0][1], v_[1][1])):
                    hit = (px_, py_)
            if hit is None:
                for q_ in (b_[0], b_[1]):
                    if _on(a_, q_):
                        hit = q_
                        break
            if hit:
                touch.append([a_[2], b_[2], list(hit)])
    out["contact"] = {"pairs": len(touch), "worst": touch[:3]}

    # --- enclosed ----------------------------------------------------------
    # A wire loop with nothing inside it reads as a part outline. This is
    # what five nested rectangles around a buck converter look like to a
    # counter.
    allsegs = [s for ss in segs.values() for s in ss]
    bodies_ = [(x - 50, y - 50, x + 50, y + 50) for _, x, y in pins]
    empty = 0
    for n, ss in segs.items():
        if corners(ss) < 3:
            continue
        x0 = min(min(s[0], s[2]) for s in ss)
        x1 = max(max(s[0], s[2]) for s in ss)
        y0 = min(min(s[1], s[3]) for s in ss)
        y1 = max(max(s[1], s[3]) for s in ss)
        area = (x1 - x0) * (y1 - y0)
        if area <= 0:
            continue
        inside = sum(1 for bx in bodies_
                     if bx[0] > x0 and bx[2] < x1 and bx[1] > y0 and bx[3] < y1)
        if not inside:
            empty += area
    out["enclosed"] = {"fraction": round(empty / (sheet_w * sheet_h or 1), 3)}

    if a.json:
        print(json.dumps(out, indent=2))
        return 0

    d_, s_, r_, e_ = (out["detour"], out["span"], out["reversal"],
                      out["enclosed"])
    print(f"\n{a.export.name}  {sheet_w} x {sheet_h} units")
    print(f"  detour    median {d_['median']}, {d_['over_1_3']} of "
          f"{d_['drawn']} over 1.3, {d_['three_or_more_corners']} with 3+ "
          f"corners")
    for u, n in d_["worst"]:
        print(f"              {u:>5}  {n}")
    print(f"  span      {s_['over_0_4']} of {s_['nets']} nets over 0.4 of "
          f"the sheet")
    for v, n in s_["worst"]:
        print(f"              {v:>5}  {n}")
    if r_["rails_in_order"]:
        print(f"  reversal  {r_['pairs']} of {r_['compared']} same-band "
              f"pair(s) against the power tree "
              f"({' > '.join(r_['rails_in_order'])})")
    else:
        print(f"  reversal  unchecked: {yml} names no supply rails")
    print(f"  enclosed  {e_['fraction']:.1%} of the sheet ringed by wire "
          f"with no part in it")
    c_ = out["contact"]
    print(f"  contact   {c_['pairs']} point(s) where two nets touch — "
          f"a short the net diff cannot see")
    for n1_, n2_, pt_ in c_["worst"]:
        print(f"              {n1_} x {n2_} at {tuple(pt_)}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
