#!/usr/bin/env python3
"""Draw wires between pins that already sit next to each other.

The netlist importer gives every pin a short stub carrying its net name. That
is electrically complete and reads as a parts list. This adds real drawn
wires for the nets where both ends are close enough that a line helps.

**It only adds.** The stubs and their net labels stay exactly where they are,
so connectivity is still carried by name and a drawn wire is decoration on
top of a connection that already exists. Get the geometry slightly wrong and
the circuit is unchanged — which is the opposite of the usual failure here,
where a drawn wire *is* the connection and being one unit short is an open
circuit.

The one way an added wire can do harm is by touching a pin belonging to a
different net, which in a geometric schematic creates a connection nobody
asked for. That is checked for every segment before anything is written.

    schwire.py <export.epro2> <netlist.json> -o out.epro2
    schwire.py <export.epro2> <netlist.json> --dry-run --max-span 400

**Pin positions are not computed from the symbol library on faith.** The
transform — anchor plus the pin's symbol-local offset rotated by the
component's rotation — is applied and then checked against the stubs the
importer drew: every pin must land on an existing wire endpoint. On this
board that is 441 of 441, and anything less is refused rather than routed
around.

`--max-span` is the bounding span, in units, above which a net is left as a
label. The default of 400 (about 100 mm) comes from the distribution after
connectivity ordering: 19 nets fall under 200 and 13 more under 400, while
23 exceed 800 and would be a line across the sheet rather than a circuit.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROT = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}


def grid(x, y):
    """Snap a coordinate pair to whole units.

    EasyEDA's own rotation writes fractional anchors: rotating R9 by hand
    put it at y = -1374.9999999999998, and the pin derived from it missed
    its stub by 2e-13. The tool refused, correctly and uselessly. Every
    comparison and every emitted coordinate goes through here, because an
    exact tuple match against a float is a check that works until someone
    touches the file in the editor.
    """
    return (int(round(x)), int(round(y)))


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru, found {len(eprus)}")
    rows, cur = [], None
    for line in z.read(eprus[0]).decode("utf-8").split("\n"):
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
    return eprus[0], rows


def pin_positions(rows):
    """(designator, x, y) for every placed pin, validated against the stubs.

    The transform is anchor + rotate(symbol-local offset). Mirrored
    placements are refused rather than guessed at: this board has none, so
    the sign convention for `isMirror` has never been observed and inventing
    one would put pins in the wrong place silently.
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

    out = []
    for h, b, _, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig or desig[h["id"]].endswith("?")):
            continue
        r = (b.get("rotation") or 0) % 360
        if r not in ROT:
            raise SystemExit(f"error: {desig[h['id']]} is rotated {r}°, "
                             f"which is not one of 0/90/180/270")
        mirror = bool(b.get("isMirror"))
        sp = sympins.get(symref.get(h["id"]), [])
        local = sorted((q.get("x", 0), q.get("y", 0)) for q in sp)
        symmetric = local == sorted((-x, y) for x, y in local)
        if mirror and r in (90, 270) and not symmetric:
            # Mirroring is negating x -- measured, by flipping Q1 in the
            # editor and requiring all 441 pins to still hit their stubs.
            # What that did NOT settle is whether the negation happens
            # before or after the rotation, because the two mirrored parts
            # available were a symmetric 2-pin resistor and an asymmetric
            # part at rotation 0, and both commute. The orders differ only
            # for a part that is asymmetric under negate-x AND turned to 90
            # or 270, which is exactly this case and which nothing has
            # observed. A symbol that IS symmetric under negate-x is let
            # through: there the two orders provably agree, so refusing it
            # would be caution with no question behind it.
            raise SystemExit(
                f"error: {desig[h['id']]} is mirrored and rotated {r}°. "
                f"Mirroring is negating x, but whether that happens before "
                f"or after the rotation is unobserved, and this is the one "
                f"case where the two differ. Rotate it to 0 or 180 by hand "
                f"and export, or un-mirror it.")
        for p in sp:
            px, py = p.get("x", 0), p.get("y", 0)
            if mirror:
                px = -px
            dx, dy = ROT[r](px, py)
            gx, gy = grid(b["x"] + dx, b["y"] + dy)
            out.append((desig[h["id"]], gx, gy))
    return out


def stub_nets(rows):
    """(x, y) -> net name, read off the labelled stubs the importer drew."""
    netof = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    at = {}
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            n = netof[b["lineGroup"]]
            for pt in (grid(b["startX"], b["startY"]),
                       grid(b["endX"], b["endY"])):
                at.setdefault(pt, set()).add(n)
    return at


def _join(a, b, shape):
    """Two points, one of three orthogonal shapes between them."""
    (x1, y1), (x2, y2) = a, b
    if x1 == x2 or y1 == y2:
        return [(x1, y1, x2, y2)]
    if shape == "h":                       # across, then down
        return [(x1, y1, x2, y1), (x2, y1, x2, y2)]
    if shape == "v":                       # down, then across
        return [(x1, y1, x1, y2), (x1, y2, x2, y2)]
    mx = int(round((x1 + x2) / 2))         # across, down, across
    return [(x1, y1, mx, y1), (mx, y1, mx, y2), (mx, y2, x2, y2)]


def routes(points):
    """Candidate orthogonal paths through every point, best shape first.

    Three shapes, not a shortest-path search. These are nets whose ends are
    already neighbours; what decides whether a wire is usable is not its
    length but whether it crosses a pin belonging to something else, and
    that is a filter rather than a cost. A net with no clean candidate is
    left as a label, which it already was.
    """
    xs = [q[0] for q in points]
    ys = [q[1] for q in points]
    pts = sorted(points, key=(lambda q: (q[0], q[1]))
                 if max(xs) - min(xs) >= max(ys) - min(ys)
                 else (lambda q: (q[1], q[0])))
    for shape in ("h", "v", "z"):
        segs = []
        for a, b in zip(pts, pts[1:]):
            segs += _join(a, b, shape)
        yield [s for s in segs if (s[0], s[1]) != (s[2], s[3])]


def touches(seg, pt):
    """Does an axis-aligned segment pass through or end on a point?"""
    x1, y1, x2, y2 = seg
    x, y = pt
    if y1 == y2:
        return y == y1 and min(x1, x2) <= x <= max(x1, x2)
    if x1 == x2:
        return x == x1 and min(y1, y2) <= y <= max(y1, y2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("netlist", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-span", type=int, default=400,
                    help="leave a net as a label above this bounding span "
                         "in units (default 400, about 100 mm)")
    ap.add_argument("--max-pins", type=int, default=4,
                    help="leave a net as a label above this pin count "
                         "(default 4; the power rails are far above it)")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    for q in (a.export, a.netlist):
        if not q.exists():
            print(f"error: {q} not found", file=sys.stderr)
            return 2

    name, rows = load(a.export)
    pins = pin_positions(rows)
    at = stub_nets(rows)

    unplaced = [p for p in pins if (p[1], p[2]) not in at]
    print(f"\n{a.export.name}")
    print(f"  {len(pins)} pin(s), {len(pins) - len(unplaced)} landing on a "
          f"labelled stub")
    if unplaced:
        for d, x, y in unplaced[:6]:
            print(f"  NOSTUB {d} at ({x}, {y})")
        print(f"\n  {len(unplaced)} pin position(s) do not match a stub, so "
              f"the transform is not verified on this file — refusing\n")
        return 1

    bynet = collections.defaultdict(list)
    for d, x, y in pins:
        for n in at[(x, y)]:
            bynet[n].append((d, x, y))

    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    want = collections.Counter()
    for c in design.values():
        for n in c["pins"].values():
            if not n.upper().startswith("NC_"):
                want[n] += 1

    draw, skipped = {}, collections.Counter()
    for n, members in bynet.items():
        pts = sorted({(x, y) for _, x, y in members})
        if n.upper().startswith("NC_") or len(pts) < 2:
            skipped["single pin or deliberately open"] += 1
            continue
        if want[n] > a.max_pins:
            skipped[f"more than {a.max_pins} pins (rail or bus)"] += 1
            continue
        span = (max(p[0] for p in pts) - min(p[0] for p in pts)
                + max(p[1] for p in pts) - min(p[1] for p in pts))
        if span > a.max_span:
            skipped[f"span over {a.max_span} units"] += 1
            continue
        draw[n] = pts

    print(f"  {len(draw)} net(s) to draw, {sum(skipped.values())} left as labels")
    for why, c in skipped.most_common():
        print(f"         {c:3d}  {why}")

    # --- the one way this can do harm ------------------------------------
    # A drawn wire that passes over a foreign pin connects to it. Try each
    # shape and keep the first clean one; a net with none is left as a label,
    # which costs nothing because the stub already carries the connection.
    def collides(net, pts, segs):
        """The first foreign pin this path would touch, or None."""
        for pt, nets in at.items():
            if pt in pts or net in nets:
                continue
            if any(touches(s, pt) for s in segs):
                return pt, sorted(nets)
        return None

    chosen, shorted = {}, []
    for n, pts in sorted(draw.items()):
        for segs in routes(pts):
            if collides(n, pts, segs) is None:
                chosen[n] = segs
                break
        else:
            shorted.append(n)
    if shorted:
        print(f"  {len(shorted)} net(s) have no clean path and stay labels: "
              + ", ".join(shorted[:8]) + (" ..." if len(shorted) > 8 else ""))
    print(f"  {len(chosen)} net(s) routed, no segment touching a foreign pin")
    draw = chosen

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    # --- emit --------------------------------------------------------------
    ticket = max((h["ticket"] for h, _, _, _ in rows
                  if h and isinstance(h.get("ticket"), int)), default=0)
    last_sch = max(i for i, (h, _, doc, _) in enumerate(rows)
                   if h and doc and doc[0] in ("SCH_PAGE", "SCH"))

    added = []
    for n in sorted(draw):
        segs = draw[n]
        wid = hashlib.sha256(f"wire/{n}".encode()).hexdigest()[:16]
        ticket += 1
        added.append((
            {"type": "WIRE", "ticket": ticket, "id": wid},
            {"groupId": "", "locked": False, "zIndex": 1}))
        for i, (x1, y1, x2, y2) in enumerate(segs):
            ticket += 1
            added.append((
                {"type": "LINE", "ticket": ticket,
                 "id": hashlib.sha256(f"seg/{n}/{i}".encode()).hexdigest()[:16]},
                {"lineGroup": wid, "startX": x1, "startY": y1,
                 "endX": x2, "endY": y2, "strokeColor": None,
                 "strokeStyle": None, "fillColor": None, "strokeWidth": 1,
                 "fillStyle": None}))

    out_lines = []
    for i, (h, b, doc, raw) in enumerate(rows):
        out_lines.append(raw)
        if i == last_sch:
            out_lines += [f"{json.dumps(hh, separators=(',', ':'))}||"
                          f"{json.dumps(bb, separators=(',', ':'))}|"
                          for hh, bb in added]

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    _, back = load(a.out)
    if len(back) != len(rows) + len(added):
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(back)} lines out, expected "
              f"{len(rows)} + {len(added)} — {a.out} deleted\n", file=sys.stderr)
        return 1
    new_ids = {h["id"] for h, _ in added}
    kept = [r for r in back if not (r[0] and r[0].get("id") in new_ids)]
    for (h1, b1, _, _), (h2, b2, _, _) in zip(rows, kept):
        if (h1, b1) != (h2, b2):
            a.out.unlink(missing_ok=True)
            print(f"  error: an existing record changed — {a.out} deleted\n",
                  file=sys.stderr)
            return 1

    print(f"  {len(added)} record(s) added, every existing one untouched")
    print(f"  wrote {a.out}")
    print(f"\n  prove it:  ./tools/epro.py diff {a.out} {a.netlist}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
