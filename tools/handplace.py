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

A line beginning `wire` draws one wire by hand:

    wire  GND   920,125  900,170  745,170  745,150

The net comes first, then the polyline. Both ends have to land on the
named net -- a pin of it or the free end of one of its stubs -- and the
route has to stay off every other net's pins and wires, because a wire
crossing one of those is a connection nothing downstream can see. It is
here rather than in `schwire.py` because it is a drawing decision, not a
rule: the net is one the never-draw list keeps as labels, and the one
place it has to become a line is a case no rule finds.

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
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arrange import (ROT, anchor_geometry, read_records,    # noqa: E402
                     reread, rotate_cluster, translate)


def read_placement(path: Path):
    """(designator -> (x, y, rotation), [(net, pts)], {desig: len})."""
    out, wires, stubs, bad = {}, [], {}, []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "wire":
            pts = []
            try:
                for q in parts[2:]:
                    px, _, py = q.partition(",")
                    pts.append((int(px), int(py)))
            except ValueError:
                pts = []
            if len(parts) < 4 or len(pts) != len(parts) - 2:
                bad.append((lineno, raw, "want `wire NET x,y x,y ...`, at "
                                         "least two integer points"))
                continue
            wires.append((parts[1], pts))
            continue
        if parts[0] == "stub":
            if len(parts) != 3:
                bad.append((lineno, raw, "want `stub DESIGNATOR LENGTH`"))
                continue
            try:
                stubs[parts[1]] = int(parts[2])
            except ValueError:
                bad.append((lineno, raw, "length must be an integer"))
            continue
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
    return out, wires, stubs


def hand_wire(net: str, pts, first_ticket: int):
    """WIRE + one LINE per segment, for a hand-drawn wire.

    No `NET` attribute. `epro.py diff` counts one `key=NET` per pin, so a
    wire carrying one of its own lifts the export's count above the
    netlist's and reads as a connection that is not there -- the same
    reason netflags' ground bus has none. The net is carried by geometry:
    the ends land on pins and stub ends that already name it.
    """
    wid = "hw" + hashlib.sha256(
        f"{net}/{pts}".encode()).hexdigest()[:14]
    recs = [({"type": "WIRE", "ticket": first_ticket, "id": wid},
             {"groupId": "", "zIndex": 1})]
    for i, (a_, b_) in enumerate(zip(pts, pts[1:])):
        recs.append(({"type": "LINE", "ticket": first_ticket + i + 1,
                      "id": f"{wid}l{i}"},
                     {"lineGroup": wid, "startX": a_[0], "startY": a_[1],
                      "endX": b_[0], "endY": b_[1], "strokeWidth": 1}))
    return recs


def pin_coords(rows):
    """(x, y) -> designator for every placed pin, as the file has them.

    The same transform `arrange.py` uses to attribute a wire to a
    component: anchor plus the pin's symbol-local offset, rotated.
    """
    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    sympins = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and h["type"] == "PIN" and doc and doc[0] == "SYMBOL":
            sympins[doc[1]].append(b)
    out = {}
    for h, b, _, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig):
            continue
        r = (b.get("rotation") or 0) % 360
        if r not in ROT:
            continue
        for q in sympins.get(symref.get(h["id"]), []):
            lx = -q.get("x", 0) if b.get("isMirror") else q.get("x", 0)
            dx, dy = ROT[r](lx, q.get("y", 0))
            out[(round(b["x"] + dx), round(b["y"] + dy))] = desig[h["id"]]
    return out


def read_rows(path: Path):
    """(header, payload, dochead, raw) for every line of the one .epru."""
    z = zipfile.ZipFile(path)
    name = [n for n in z.namelist() if n.endswith(".epru")][0]
    rows, cur = [], None
    for raw in z.read(name).decode("utf-8").split("\n"):
        if "||" not in raw:
            rows.append((None, None, None, raw))
            continue
        head, _, body = raw.partition("||")
        try:
            h, b = json.loads(head), json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            rows.append((None, None, None, raw))
            continue
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        rows.append((h, b, cur, raw))
    return rows


def net_points(rows):
    """net -> every (x, y) its stubs end at, pin end included.

    Both ends of a stub line, because a stub starts on its component's pin
    and a wire may land on either.
    """
    netof = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    out = collections.defaultdict(set)
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                out[netof[b["lineGroup"]]].add((round(x), round(y)))
    return out


def _crosses(p, q, r, s):
    """Do segments pq and rs meet, endpoints included?"""
    def side(o, u, v):
        return ((u[1] - o[1]) * (v[0] - o[0])
                - (u[0] - o[0]) * (v[1] - o[1]))
    d1, d2 = side(p, q, r), side(p, q, s)
    d3, d4 = side(r, s, p), side(r, s, q)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    def on(o_, u_, v_):
        return (min(u_[0], v_[0]) <= o_[0] <= max(u_[0], v_[0])
                and min(u_[1], v_[1]) <= o_[1] <= max(u_[1], v_[1]))
    return (d1 == 0 and on(r, p, q) or d2 == 0 and on(s, p, q)
            or d3 == 0 and on(p, r, s) or d4 == 0 and on(q, r, s))


def check_wires(rows, wires):
    """Every reason a hand-drawn wire is wrong, as a list of strings.

    Run on what was *written*, not on what was meant: a wire is a set of
    coincidences, and the coordinates that matter are the ones in the
    file. Three answers where the data can be absent -- a point on the
    net, a point on nothing, and a point on a different net -- because
    the second is what a typo looks like and it must not pass as the
    first.
    """
    pts = net_points(rows)
    on_net = {}
    for n, qs in pts.items():
        for q in qs:
            on_net.setdefault(q, set()).add(n)
    # Every line in the file, with the net it carries where one of its
    # ends names it. A crossing of a *segment* is as much a connection as
    # a crossing of a pin, and leaving it out made the docstring above
    # false: the pad's wire to U4's ground ran straight across the
    # bootstrap's SW_5V line and the check said nothing.
    segs = []
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("startX") is not None:
            ends = ((round(b["startX"]), round(b["startY"])),
                    (round(b["endX"]), round(b["endY"])))
            here = on_net.get(ends[0], set()) | on_net.get(ends[1], set())
            segs.append((ends[0], ends[1], here))
    bad = []
    for net, poly in wires:
        for q in (poly[0], poly[-1]):
            here = on_net.get(q, set())
            if net not in here:
                what = "/".join(sorted(here)) if here else "no net at all"
                bad.append(f"the wire on {net} ends at {q}, which is {what}")
        for a_, b_ in zip(poly, poly[1:]):
            for q, ns in sorted(on_net.items()):
                if net in ns:
                    continue
                if _crosses(a_, b_, q, q):
                    bad.append(f"the wire on {net} crosses {q}, which is on "
                               f"{'/'.join(sorted(ns))}")
            for p_, q_, ns in segs:
                if net in ns or ns == set():
                    continue
                if _crosses(a_, b_, p_, q_):
                    bad.append(f"the wire on {net} runs across {p_}-{q_}, "
                               f"which carries {'/'.join(sorted(ns))}")
    return bad


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

    want, wires, stubs = read_placement(a.placement)
    name, lines = read_records(a.export)
    of_record, owner = cluster_owner(lines)
    present = set(owner)

    print(f"\n{a.export.name} <- {a.placement.name}")
    print(f"  {len(present)} placed components, "
          f"{len(of_record)} canvas records attributed")

    stray = sorted(set(stubs) - present - {"*"})
    if stray:
        print(f"  error: the placement states a lead length for "
              f"{', '.join(stray[:8])}, which "
              f"{'is' if len(stray) == 1 else 'are'} not on this sheet",
              file=sys.stderr)
        return 1
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

    # --- leads cut to a stated length -------------------------------------
    # The importer gives every pin a thirty-unit lead. That is a fine
    # length for a part standing upright -- its footprint stays a line --
    # and a bad one for a part lying *across* a row, where the two leads
    # make a hundred-unit footprint and nothing can be placed within it.
    # `stub R26 15` states what that part's leads are, by designator and
    # not by coordinate, so moving the part does not invalidate it.
    #
    # Cut here, before anything reads the geometry: the overlap check, the
    # write and the drift fingerprint all then see the leads the sheet
    # really has. Cutting it a stage later, in netflags, left this tool
    # refusing positions that were fine.
    cut, cut_lines = 0, set()
    if stubs.get("*") is not None:
        # `stub * 5` -- every part on the sheet. A lead is the importer's
        # runway out to where a net's name sits, and on a sheet whose
        # layout is hand-set nothing needs a long one: the names move with
        # the leads, and every unit of runway is a unit no neighbour can
        # stand in.
        for d in present:
            stubs.setdefault(d, stubs["*"])
    if stubs:
        pinset = pin_coords(lines2)
        for i, (h, b, _, _) in enumerate(lines2):
            if not h or h["type"] != "LINE" or not b.get("lineGroup"):
                continue
            d = owner.get(of_record.get(i))
            if d not in stubs:
                continue
            a_ = (round(b["startX"]), round(b["startY"]))
            c_ = (round(b["endX"]), round(b["endY"]))
            if a_ in pinset and c_ not in pinset:
                pin, free = a_, c_
            elif c_ in pinset and a_ not in pinset:
                pin, free = c_, a_
            else:
                continue
            ln = stubs[d]
            u = (0 if free[0] == pin[0] else (1 if free[0] > pin[0] else -1),
                 0 if free[1] == pin[1] else (1 if free[1] > pin[1] else -1))
            new_ = (pin[0] + u[0] * ln, pin[1] + u[1] * ln)
            nb = dict(b)
            if a_ == free:
                nb["startX"], nb["startY"] = new_
            else:
                nb["endX"], nb["endY"] = new_
            lines2[i] = (h, nb, lines2[i][2],
                         f"{json.dumps(h, separators=(',', ':'))}||"
                         f"{json.dumps(nb, separators=(',', ':'))}|")
            cut_lines.add(i)
            cut += 1
        # ...and the name that sits on it moves with it. The importer puts
        # the label at the lead's midpoint, so a cut lead leaves the text
        # hanging out beyond its end -- and the box the overlap check
        # measures is every coordinate the component owns, text included,
        # so an unmoved label keeps the footprint at its old width and the
        # whole point of cutting is lost.
        lgl = {r[1]["lineGroup"]: i for i, r in enumerate(lines2)
               if r[1] and r[1].get("lineGroup")}
        for i, (h, b, _, _) in enumerate(lines2):
            if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                    or b.get("parentId") not in lgl
                    or lgl[b["parentId"]] not in cut_lines
                    or not isinstance(b.get("x"), (int, float))):
                continue
            j = lgl[b["parentId"]]
            h2, b2, _, _ = lines2[j]
            if h2 is None:
                continue
            nb = dict(b)
            nb["x"] = (round(b2["startX"]) + round(b2["endX"])) // 2
            nb["y"] = (round(b2["startY"]) + round(b2["endY"])) // 2
            lines2[i] = (h, nb, lines2[i][2],
                         f"{json.dumps(h, separators=(',', ':'))}||"
                         f"{json.dumps(nb, separators=(',', ':'))}|")
        if cut:
            print(f"  stub   {cut} lead(s) cut to the length the file states")

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

    # The hand-drawn wires go at the end of the schematic document, with
    # the rest of the canvas records and not inside a SYMBOL or DEVICE
    # one. They are added after the translation because their coordinates
    # are the ones the placement file states -- the finished canvas, which
    # is what a person drawing on the sheet reads off.
    nwire = 0
    if wires:
        last_sch = max(i for i, (h, _, doc, _) in enumerate(lines2)
                       if h and doc and doc[0] in ("SCH", "SCH_PAGE"))
        ticket = 1 + max((h["ticket"] for h, _, _, _ in lines2
                          if h and isinstance(h.get("ticket"), int)),
                         default=0)
        new = []
        for net, poly in wires:
            recs = hand_wire(net, poly, ticket)
            ticket += len(recs)
            new += recs
        out_lines = (out_lines[:last_sch + 1]
                     + [f"{json.dumps(h, separators=(',', ':'))}||"
                        f"{json.dumps(b, separators=(',', ':'))}|"
                        for h, b in new]
                     + out_lines[last_sch + 1:])
        nwire = len(new)

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))
    print(f"  {touched} records translated"
          + (f", {nwire} drawn by hand" if nwire else ""))

    # --- the proof ---------------------------------------------------------
    # `reread` requires the output to be the input's records one for one,
    # which is true of a pure translation and false once a wire has been
    # drawn. So the hand wires are taken back out first and the same
    # comparison run on what is left: the point of it is that no original
    # record moved or changed, not that the file is the same length.
    if nwire:
        back = read_rows(a.out)
        kept = [r for r in back
                if not (r[0] and str(r[0].get("id", "")).startswith("hw"))]
        if len(kept) != len(lines2) or any(
                (h1 is None) != (h2 is None) or (
                    h1 is not None and (h1.get("id"), h1.get("type"))
                    != (h2.get("id"), h2.get("type")))
                for (h1, _, _, _), (h2, _, _, _) in zip(lines2, kept)):
            a.out.unlink(missing_ok=True)
            print(f"\n  error: drawing the hand wire disturbed the record "
                  f"sequence; {a.out} deleted\n", file=sys.stderr)
            return 1
        after = anchor_geometry(kept, of_record, owner)
    else:
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

    # A hand-drawn wire is checked on the file that was written, not on
    # the placement line that asked for it: the coordinates that matter
    # are the ones now in the canvas, and a route that ends nowhere is
    # exactly the fault this is for.
    if wires:
        wrong = check_wires(read_rows(a.out), wires)
        if wrong:
            for w in wrong[:10]:
                print(f"  BADWIRE {w}", file=sys.stderr)
            a.out.unlink(missing_ok=True)
            print(f"\n  {len(wrong)} problem(s) with the hand-drawn wire(s); "
                  f"{a.out} deleted\n", file=sys.stderr)
            return 1
        for net, poly in wires:
            ends = " ".join(f"{q[0]},{q[1]}" for q in (poly[0], poly[-1]))
            print(f"  wire   {net} from {ends}, {len(poly) - 1} segment(s)")
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
