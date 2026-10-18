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
import math
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
        # Ground and supply flags count. They carry no designator, so the
        # first version skipped all 180 of them and the router drew
        # straight across their bars -- six times through the ground
        # spikes of the LED chain alone. Every such touch reads as a
        # junction, and this project's own argument makes it one: if
        # nothing crosses, a touch is a node. A flag that is not an
        # obstacle turns that argument into a liability.
        if not h or h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        if h["id"] not in desig or desig[h["id"]].endswith("?"):
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


def bodies(rows):
    """designator -> the box its symbol outline occupies on the canvas.

    A wire through the middle of a 37-pin MCU is not a short and no check
    here fired on it: `collides()` tests whether a segment touches a
    foreign *pin*, and the body between the pins is not a pin. It is still
    the first thing a reader notices and nobody draws it that way.
    """
    flagof = {b["parentId"]: f'flag:{b["value"]}@{b["parentId"][:6]}'
              for h, b, _, _ in rows
              if h and h["type"] == "ATTR"
              and b.get("key") == "Global Net Name" and b.get("value")}
    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    shapes = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and doc and doc[0] == "SYMBOL" and h["type"] in (
                "RECT", "POLY", "ELLIPSE", "CIRCLE", "ARC"):
            shapes[doc[1]].append((h["type"], b))

    out = {}
    for h, b, _, _ in rows:
        # Ground and supply flags count as bodies. They carry no
        # designator, so the first version skipped all 180 and the
        # router drew straight across their bars -- six times through
        # the ground spikes of the LED chain alone. Every such touch
        # reads as a junction, and this project's own argument makes it
        # one: if nothing crosses, a touch is a node. A flag that is not
        # an obstacle turns that argument into a liability.
        if not h or h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        who = desig.get(h["id"]) or flagof.get(h["id"])
        if who is None or who.endswith("?"):
            continue
        r = (b.get("rotation") or 0) % 360
        m = bool(b.get("isMirror"))
        pts = []
        for kind, g in shapes.get(symref.get(h["id"]), []):
            if kind == "RECT":
                local = [(g["dotX1"], g["dotY1"]), (g["dotX2"], g["dotY2"])]
            elif kind == "POLY" and g.get("points"):
                local = [(q["x"], q["y"]) for q in g["points"]]
            elif kind == "ARC":
                # An inductor is four arcs and nothing else, so L2 had no
                # body: the router could not see it and would happily draw
                # straight through the coil. Start, refer and end bound
                # the arc closely enough for a clearance test.
                local = [(g["startX"], g["startY"]),
                         (g["referX"], g["referY"]),
                         (g["endX"], g["endY"])]
            else:
                cxl, cyl = g.get("centerX", 0), g.get("centerY", 0)
                rx = abs(g.get("radiusX") or g.get("radius") or 0)
                ry = abs(g.get("radiusY") or g.get("radius") or 0)
                local = [(cxl - rx, cyl - ry), (cxl + rx, cyl + ry)]
            for lx, ly in local:
                if m:
                    lx = -lx
                dx, dy = ROT[r](lx, ly)
                pts.append(grid(b["x"] + dx, b["y"] + dy))
        if pts:
            out[who] = (min(x for x, _ in pts), min(y for _, y in pts),
                                   max(x for x, _ in pts), max(y for _, y in pts))
    return out


def crosses_box(seg, bx):
    """Does an axis-aligned segment pass through a box?"""
    x1, y1, x2, y2 = seg
    bx0, by0, bx1, by1 = bx
    if y1 == y2:
        return by0 < y1 < by1 and min(x1, x2) < bx1 and bx0 < max(x1, x2)
    if x1 == x2:
        return bx0 < x1 < bx1 and min(y1, y2) < by1 and by0 < max(y1, y2)
    return False


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


GRID = 10          # the pin pitch; every pin and stub end sits on it


class Obstacles:
    """Can a wire take this step? Geometry, not a rasterised point set.

    The point set was rasterised on multiples of GRID while the maze walks
    the lattice anchored at its *start pin*, and only 55 of 441 pins here
    sit on a multiple of 10 in x. So the two never met: the obstacle map
    was invisible to the search, which walked straight through U7 and U2,
    and `collides` then threw the path away. That is 20 of the 37 routable
    nets, reported as "no clean path".

    Bodies are bucketed so a step tests a handful of boxes rather than all
    129; pins are indexed by row and column, because a step must not run
    over a foreign pin, not merely avoid landing on one.
    """

    CELL = 200

    def __init__(self, boxes, pins, stubs=(), clear=0, endclear=0,
                 netboxes=()):
        # Not touching is not the same as reading as separate. The maze
        # took the only free corridor and that runs flush along the
        # symbol outline: three of LED1's four edges came out green, so
        # the border and the wire were the same line. Inflating every
        # body and every stub by `clear` turns "does not cross" into
        # "keeps its distance", which is the question a reader asks.
        self.clear = clear
        self.boxes = [(x0 - clear, y0 - clear, x1 + clear, y1 + clear)
                      for x0, y0, x1, y1 in boxes]
        self.bucket = collections.defaultdict(list)
        for bx in self.boxes:
            x0, y0, x1, y1 = bx
            for cx in range(int(x0 // self.CELL), int(x1 // self.CELL) + 1):
                for cy in range(int(y0 // self.CELL), int(y1 // self.CELL) + 1):
                    self.bucket[(cx, cy)].append(bx)
        # Boxes that belong to a net: a pin body blocks every net but
        # the one whose pin it is, or the wire could never leave the pin
        # it starts on.
        self.netboxes = [(x0 - clear, y0 - clear, x1 + clear, y1 + clear, n)
                         for x0, y0, x1, y1, n in netboxes]
        self.by_row = collections.defaultdict(list)
        self.by_col = collections.defaultdict(list)
        for px, py in pins:
            self.by_row[py].append(px)
            self.by_col[px].append(py)
        # The stubs themselves, not just the pin at the end of each. A
        # segment crossing a foreign stub halfway along is exactly as
        # ambiguous as one crossing its pin, and there is no junction
        # record in this format to disambiguate it -- the hand-drawn
        # reference has none, so EasyEDA draws the dot itself and nothing
        # here can ask for one. Avoiding the crossing is the only honest
        # answer available.
        # A full grid of room around each stub's free *end*, and nothing
        # along its flank. The end is what reads as a node: a trunk run
        # down the side of U10, a millimetre clear of ten stub ends on
        # ten different nets, is ten T-junctions to the eye -- and that
        # is the same argument as "no crossings means every meeting is a
        # node", turned around. The flank is not: a wire beside a stub
        # for part of its length reads as two wires. Giving the flank a
        # band as well was tried and costs 18 nets for nothing anyone
        # asked for.
        self.endclear = endclear
        self.send_row = collections.defaultdict(list)
        self.srow = collections.defaultdict(list)
        self.scol = collections.defaultdict(list)
        # Bucketed, not a list. A linear scan over 882 endpoints per
        # expansion took one run from 24 s to 560 s.
        self.sends = collections.defaultdict(list)
        for x1, y1, x2, y2, net in stubs:
            for ex, ey in ((x1, y1), (x2, y2)):
                self.sends[(ex // self.CELL, ey // self.CELL)].append(
                    (ex, ey, net))
        for x1, y1, x2, y2, net in stubs:
            if y1 == y2:
                self.srow[y1].append((min(x1, x2), max(x1, x2), net))
            elif x1 == x2:
                self.scol[x1].append((min(y1, y2), max(y1, y2), net))

    def add_wire(self, segs, net):
        """Record a drawn net, so later ones route around it rather than
        crossing it and being rejected afterwards."""
        for x1, y1, x2, y2 in segs:
            if y1 == y2:
                self.srow[y1].append((min(x1, x2), max(x1, x2), net))
            elif x1 == x2:
                self.scol[x1].append((min(y1, y2), max(y1, y2), net))

    def step_blocked(self, x1, y1, x2, y2, exempt, net=None):
        near = set()
        for cx in range(int(min(x1, x2) // self.CELL),
                        int(max(x1, x2) // self.CELL) + 1):
            for cy in range(int(min(y1, y2) // self.CELL),
                            int(max(y1, y2) // self.CELL) + 1):
                near.update(id(b) for b in self.bucket.get((cx, cy), ()))
        if near:
            for bx in self.boxes:
                if id(bx) in near and crosses_box((x1, y1, x2, y2), bx):
                    return True
        for x0, y0, bx1, by1, n in self.netboxes:
            if n == net:
                continue
            if crosses_box((x1, y1, x2, y2), (x0, y0, bx1, by1)):
                return True
        e = self.endclear
        if e:
            lox, hix = min(x1, x2) - e, max(x1, x2) + e
            loy, hiy = min(y1, y2) - e, max(y1, y2) + e
            for cx in range(int(lox // self.CELL), int(hix // self.CELL) + 1):
                for cy in range(int(loy // self.CELL),
                                int(hiy // self.CELL) + 1):
                    for ex, ey, en in self.sends.get((cx, cy), ()):
                        if en == net or (ex, ey) in exempt:
                            continue
                        if lox <= ex <= hix and loy <= ey <= hiy:
                            return True
        if y1 == y2:
            lo, hi = min(x1, x2), max(x1, x2)
            for px in self.by_row.get(y1, ()):
                if lo <= px <= hi and (px, y1) not in exempt:
                    return True
            # Crossing a foreign stub, exactly. Giving the stubs a
            # clearance band as well was tried and costs 18 of the 48
            # nets for nothing the eye asked for: the complaint was
            # wires lying on symbol outlines, which is the body.
            for sx, spans in self.scol.items():
                if not (lo <= sx <= hi):
                    continue
                for a0, a1, sn in spans:
                    if sn != net and a0 <= y1 <= a1:
                        return True
        else:
            lo, hi = min(y1, y2), max(y1, y2)
            for py in self.by_col.get(x1, ()):
                if lo <= py <= hi and (x1, py) not in exempt:
                    return True
            for sy, spans in self.srow.items():
                if not (lo <= sy <= hi):
                    continue
                for a0, a1, sn in spans:
                    if sn != net and a0 <= x1 <= a1:
                        return True
        return False


def _route_pair(a, b, obst, exempt, detour=6, net=None):
    """An orthogonal path from a to b that goes around obstacles.

    Three fixed shapes -- across-then-down, down-then-across, and a Z --
    were what the first router tried, and 22 of 58 hub-to-satellite
    connections had none of them clean. A wire that has to detour round a
    capacitor is not an exotic case on a schematic; it is most of them.

    So: breadth-first on the pin pitch, with a bend costing more than a
    straight so the result prefers long runs to staircases. `blocked` is
    the set of grid points a wire may not pass through -- component bodies
    and foreign pins -- and the two endpoints are exempt because a wire has
    to reach its own pins.
    """
    import heapq
    if a == b:
        return []
    sx, sy = a
    tx, ty = b
    # A parent map, not a path carried on the queue. The first version
    # pushed `path + ((x, y),)` at every expansion, copying the whole path
    # per node -- which made this 500 of the pipeline's 512 seconds, 98%
    # of the total. Reconstructing from parents at the end is the textbook
    # form and the only reason it was not written that way first is that
    # the tuple version was shorter.
    best = {(sx, sy, None): 0}
    came = {}
    pq = [(0, 0, sx, sy, None)]          # (f, g, x, y, direction)
    # Search inside a box around the two endpoints, not the whole sheet.
    # A route that needs to leave this box is one nobody would draw
    # anyway, and without the bound a *failed* search walks the entire
    # canvas before giving up -- which is where the time went: 21 failures
    # cost more than the 16 successes.
    pad = detour * GRID
    lox, hix = min(sx, tx) - pad, max(sx, tx) + pad
    loy, hiy = min(sy, ty) - pad, max(sy, ty) + pad
    while pq:
        _, cost, x, y, d = heapq.heappop(pq)
        # Compare the true cost against the stored one. The first version
        # popped f = g + h and compared it to a stored g, so almost every
        # node looked stale and was skipped -- the search was fast and
        # found half as many routes.
        if cost > best.get((x, y, d), 1 << 60):
            continue
        if (x, y) == (tx, ty):
            path, key = [], (x, y, d)
            while key is not None:
                path.append((key[0], key[1]))
                key = came.get(key)
            return path[::-1]
        # The lattice is anchored at the start, so a target that is not a
        # whole GRID away in both axes is unreachable by GRID steps alone
        # -- and only 55 of this board's 441 pins sit on a multiple of 10
        # in x, where all ten residues occur. That silently declined 29 of
        # 37 routable nets, reported as "no clean path", which reads as a
        # layout that is too tight rather than a step size that cannot
        # land. So each axis also offers the partial step that aligns it
        # with the target. It is shorter than GRID, so it cannot skip a
        # lattice point, and `collides` remains the backstop.
        steps = [(GRID, 0), (-GRID, 0), (0, GRID), (0, -GRID)]
        if 0 < abs(tx - x) < GRID:
            steps.append((tx - x, 0))
        if 0 < abs(ty - y) < GRID:
            steps.append((0, ty - y))
        for ndx, ndy in steps:
            nx, ny = x + ndx, y + ndy
            if not (lox <= nx <= hix and loy <= ny <= hiy):
                continue
            if obst.step_blocked(x, y, nx, ny, exempt, net):
                continue
            nd = (ndx, ndy)
            step = GRID + (0 if d == nd or d is None else GRID * 6)
            nc = cost + step
            if nc >= best.get((nx, ny, nd), 1 << 60):
                continue
            best[(nx, ny, nd)] = nc
            came[(nx, ny, nd)] = (x, y, d)
            # A* rather than plain Dijkstra: the Manhattan distance is
            # admissible here because every step costs at least GRID.
            heapq.heappush(pq, (nc + abs(nx - tx) + abs(ny - ty), nc,
                                nx, ny, nd))
    return None


def _segments(points):
    """Collapse a point path into the fewest axis-aligned segments."""
    if not points or len(points) < 2:
        return []
    segs, run = [], [points[0]]
    for prev, cur in zip(points, points[1:]):
        if len(run) >= 2:
            d0 = (run[-1][0] - run[-2][0], run[-1][1] - run[-2][1])
            d1 = (cur[0] - prev[0], cur[1] - prev[1])
            if d0 != d1:
                segs.append((run[0][0], run[0][1], run[-1][0], run[-1][1]))
                run = [run[-1]]
        run.append(cur)
    segs.append((run[0][0], run[0][1], run[-1][0], run[-1][1]))
    return [s for s in segs if (s[0], s[1]) != (s[2], s[3])]


def routes(points, obst=None, detour=6, net=None):
    """Candidate orthogonal paths through every point, best first.

    The straight shapes come first because they are what a human would
    draw; the maze is the fallback for the ones that have to detour.
    """
    xs = [q[0] for q in points]
    ys = [q[1] for q in points]
    pts = sorted(points, key=(lambda q: (q[0], q[1]))
                 if max(xs) - min(xs) >= max(ys) - min(ys)
                 else (lambda q: (q[1], q[0])))

    def simple(shape):
        segs = []
        for a, b in zip(pts, pts[1:]):
            (x1, y1), (x2, y2) = a, b
            if x1 == x2 or y1 == y2:
                segs.append((x1, y1, x2, y2))
            elif shape == "h":
                segs += [(x1, y1, x2, y1), (x2, y1, x2, y2)]
            elif shape == "v":
                segs += [(x1, y1, x1, y2), (x1, y2, x2, y2)]
            else:
                mx = int(round((x1 + x2) / 2))
                segs += [(x1, y1, mx, y1), (mx, y1, mx, y2), (mx, y2, x2, y2)]
        return [s for s in segs if (s[0], s[1]) != (s[2], s[3])]

    for shape in ("h", "v", "z"):
        yield simple(shape)

    if obst is None:
        return
    exempt = set(pts)
    segs, ok = [], True
    for a, b in zip(pts, pts[1:]):
        path = _route_pair(a, b, obst, exempt, detour, net)
        if path is None:
            ok = False
            break
        segs += _segments(path)
    if ok and segs:
        yield segs


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
    ap.add_argument("--priority-nets", default="",
                    help="comma-separated nets to draw first and to "
                         "exempt from --max-pins. Shortest-first is best "
                         "for crossing-freedom and wrong for reading: it "
                         "spends the free space on short signals and "
                         "leaves the power path as text")
    ap.add_argument("--labels-outward", action="store_true",
                    help="move each net label past the free end of its "
                         "stub and align it away from the part. A stub is "
                         "three pin pitches long and a fourteen-character "
                         "net name is not")
    ap.add_argument("--label-offset", type=int, default=8,
                    help="units beyond the stub end (default 8)")
    ap.add_argument("--clearance", type=int, default=0,
                    help="units a drawn segment must keep from any body "
                         "and any foreign stub (default 0). Not touching "
                         "is not the same as reading as separate: at 0 "
                         "the maze runs flush along symbol outlines")
    ap.add_argument("--end-clearance", type=int, default=0,
                    help="units a drawn segment must keep from the free "
                         "end of any foreign stub (default 0). The end is "
                         "what reads as a node; the flank does not")
    ap.add_argument("--detour", type=int, default=6,
                    help="how far outside the two pins' bounding box the "
                         "maze router may go, in pin pitches (default 6). "
                         "A tighter sheet needs a wider box: the detour "
                         "round an obstacle is what it is, and bounding it "
                         "too hard turns a routable net into a label")
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

    priority = {s.strip() for s in a.priority_nets.split(",") if s.strip()}

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
        if want[n] > a.max_pins and n not in priority:
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
    body = bodies(rows)
    members = {n: {c["props"]["Designator"] for c in design.values()
                   if n in c["pins"].values()} for n in want}

    netof = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    stubs = []
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            sx, sy = grid(b["startX"], b["startY"])
            ex, ey = grid(b["endX"], b["endY"])
            stubs.append((sx, sy, ex, ey, netof[b["lineGroup"]]))

    def cross(a, b):
        """Do a horizontal and a vertical segment cross?"""
        ah, bh = a[1] == a[3], b[1] == b[3]
        if ah == bh:
            return False
        h_, v_ = (a, b) if ah else (b, a)
        return (min(h_[0], h_[2]) <= v_[0] <= max(h_[0], h_[2])
                and min(v_[1], v_[3]) <= h_[1] <= max(v_[1], v_[3]))

    def collides(net, pts, segs, drawn=()):
        """The first foreign pin, body or wire this path would hit."""
        for pt, nets in at.items():
            if pt in pts or net in nets:
                continue
            if any(touches(s, pt) for s in segs):
                return pt, sorted(nets)
        # No exemption for the net's own members. The first version
        # skipped them -- a wire may legitimately start at U5's pin -- and
        # that let eleven segments run straight through U5, U4 and R49,
        # which is the thing anyone looking at the sheet notices first.
        # `crosses_box` uses strict inequalities, so a segment that merely
        # ends on an edge is fine; only passing through is rejected.
        for d, bx in body.items():
            if any(crosses_box(s, bx) for s in segs):
                return bx, [f"through {d}"]
        # Crossing a foreign stub, or a wire already drawn for another
        # net. There is no junction record in this format -- the
        # hand-drawn reference has none, so EasyEDA renders the dot
        # itself and nothing here can ask for one. A crossing and a T are
        # therefore indistinguishable on the sheet, which makes avoiding
        # the crossing the only answer available. The three fixed shapes
        # are tried before the maze and were checked against pins and
        # bodies but not against this, so a crossing shape won before the
        # maze that avoids them was ever asked.
        for s in segs:
            for q in stubs:
                if q[4] != net and cross(s, q):
                    return q, [f"crosses {q[4]}"]
            for q in drawn:
                if q[4] != net and cross(s, q):
                    return q, [f"crosses drawn {q[4]}"]
        return None

    # Where every visible net label is going to end up, worked out
    # before the routing rather than after it. 60 labels across four
    # sheets sat on a wire that was not theirs, and the reason was the
    # order: the router could not avoid a box nobody had computed yet.
    # Offsetting the label afterwards was tried and traded one fault for
    # another; reserving its space beforehand does not.
    label_boxes = []
    if a.labels_outward:
        pinpts0 = {(x, y) for _, x, y in pins}
        for h, b, _, _ in rows:
            if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                    or b.get("valueVisible") is not True
                    or not b.get("value")):
                continue
            g = None
            for hh, bb, _, _ in rows:
                if (hh and hh["type"] == "LINE"
                        and bb.get("lineGroup") == b.get("parentId")):
                    g = (grid(bb["startX"], bb["startY"]),
                         grid(bb["endX"], bb["endY"]))
                    break
            if g is None:
                continue
            (sx0, sy0), (ex0, ey0) = g
            if (sx0, sy0) in pinpts0 and (ex0, ey0) not in pinpts0:
                pin0, free0 = (sx0, sy0), (ex0, ey0)
            elif (ex0, ey0) in pinpts0 and (sx0, sy0) not in pinpts0:
                pin0, free0 = (ex0, ey0), (sx0, sy0)
            else:
                continue
            ddx = (free0[0] > pin0[0]) - (free0[0] < pin0[0])
            ddy = (free0[1] > pin0[1]) - (free0[1] < pin0[1])
            lx0 = free0[0] + ddx * a.label_offset
            ly0 = free0[1] + ddy * a.label_offset
            w0 = len(str(b["value"])) * 3.0
            if ddy:
                label_boxes.append((lx0 - 4, ly0 - w0, lx0 + 4, ly0 + w0))
            else:
                label_boxes.append((lx0 - w0, ly0 - 4, lx0 + w0, ly0 + 4))

    # The pin bodies, as thin boxes. The check asked "over a foreign pin
    # *point*" and answered zero while 203 segments crossed a pin
    # *body* -- a pin is a stroke, not a dot, and a wire down the gutter
    # between a package and its stub ends crosses every pin on that
    # side. At U10 that was 29 segments, at U11 nineteen; in a format
    # with no junction dot each of those reads as a connection.
    pin_boxes = []
    pb_desig = {b["parentId"] for h, b, _, _ in rows
                if h and h["type"] == "ATTR"
                and b.get("key") == "Designator" and b.get("value")}
    pb_sym = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    pb_pins = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and doc and doc[0] == "SYMBOL" and h["type"] == "PIN":
            pb_pins[doc[1]].append(b)
    for h, b, _, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in pb_desig):
            continue
        r0 = (b.get("rotation") or 0) % 360
        if r0 not in ROT:
            continue
        for q in pb_pins.get(pb_sym.get(h["id"]), []):
            qx, qy = q.get("x", 0), q.get("y", 0)
            L0 = q.get("length", 10) or 10
            ang = math.radians(q.get("rotation") or 0)
            ex0, ey0 = qx + L0 * math.cos(ang), qy + L0 * math.sin(ang)
            if b.get("isMirror"):
                qx, ex0 = -qx, -ex0
            p1 = ROT[r0](qx, qy)
            p2 = ROT[r0](ex0, ey0)
            ax_, ay_ = b["x"] + p1[0], b["y"] + p1[1]
            bx_, by_ = b["x"] + p2[0], b["y"] + p2[1]
            pnet = next(iter(at.get((round(ax_), round(ay_)), ()) or
                             at.get((round(bx_), round(by_)), ()) or
                             (None,)), None)
            pin_boxes.append((min(ax_, bx_) - 1, min(ay_, by_) - 1,
                              max(ax_, bx_) + 1, max(ay_, by_) + 1,
                              pnet[0] if isinstance(pnet, tuple) else pnet))

    obst = Obstacles(list(body.values()) + label_boxes, at,
                     stubs, a.clearance, a.end_clearance, pin_boxes)

    chosen, shorted, laid = {}, [], []
    # Shortest first. Every drawn net becomes an obstacle for the next,
    # so the order decides who gets the clean path -- and a short net
    # boxed out by a long one is a worse trade than the reverse. Ties
    # break on the name so the result stays deterministic.
    def _span(item):
        pts = item[1]
        # Named nets first, whatever their span. A reviewer put it best:
        # on a 36 V board the power path is the one thing that has to be
        # drawn, and shortest-first spends the clear space on short
        # signals and leaves VBUS -> U4 -> BUS_5V as three labels. Within
        # each group it is still shortest-first, because a short net
        # boxed out by a long one is the worse trade.
        return (item[0] not in priority,
                max(x for x, _ in pts) - min(x for x, _ in pts)
                + max(y for _, y in pts) - min(y for _, y in pts), item[0])

    for n, pts in sorted(draw.items(), key=_span):
        for segs in routes(pts, obst, a.detour, n):
            if collides(n, pts, segs, laid) is None:
                chosen[n] = segs
                laid += [(x1, y1, x2, y2, n) for x1, y1, x2, y2 in segs]
                obst.add_wire(segs, n)
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

    # --- push the net labels off their components --------------------------
    # 46 of the 261 visible labels overlapped a symbol body, almost all of
    # them on the multi-pin ICs: a stub is about three pin pitches long
    # and `NC_EFUSE1_MODE` does not fit in that, so it runs into the
    # package. Moving the text is safe in a way that moving a wire is not
    # -- the net is carried by the attribute's *value* on the wire, not by
    # where its text sits -- so this only ever repositions, never renames.
    moved = 0
    if a.labels_outward:
        pinpts = {(x, y) for _, x, y in pins}
        geom = {}
        for h, b, _, _ in rows:
            if h and h["type"] == "LINE" and b.get("lineGroup"):
                geom[b["lineGroup"]] = (grid(b["startX"], b["startY"]),
                                        grid(b["endX"], b["endY"]))
        label_fix = {}
        for h, b, _, _ in rows:
            if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                    or b.get("valueVisible") is not True
                    or b.get("parentId") not in geom):
                continue
            (sx, sy), (ex, ey) = geom[b["parentId"]]
            if (sx, sy) in pinpts and (ex, ey) not in pinpts:
                pin, free = (sx, sy), (ex, ey)
            elif (ex, ey) in pinpts and (sx, sy) not in pinpts:
                pin, free = (ex, ey), (sx, sy)
            else:
                continue
            dx = (free[0] > pin[0]) - (free[0] < pin[0])
            dy = (free[1] > pin[1]) - (free[1] < pin[1])
            # A label on a *vertical* stub is set along the stub, not
            # across it. At a 10-unit pin pitch a horizontal name is
            # wider than the gap between two pins, so a row of them can
            # only be staggered -- and staggering moved the ambiguity
            # rather than removing it: three or four height levels over
            # one stub row, with no way to tell which name belongs to
            # which pin. Turned ninety degrees the text is one pitch
            # wide and the question cannot arise.
            # Offsetting the rotated label half a pitch sideways was
            # tried here and measured: labels on a foreign wire 60 -> 57,
            # labels nearer a foreign stub than their own 10 -> 46. A
            # trade, not a fix -- the text does not lie on the wire
            # because it is centred on the stub, it lies there because
            # the routing was done before anyone knew where the labels
            # would go. The obstacle set is the place to put that.
            # Anchoring the rotated label by its near edge instead of
            # its centre was tried and measured worse: labels further
            # than half a pin pitch from their own line went 17 to 178.
            # Centring puts the text *across* the stub end, which is
            # where a reader looks for it; pushing the near edge out
            # moves the whole name away by its own length.
            vertical = dy != 0
            label_fix[id(b)] = (free[0] + dx * a.label_offset,
                                free[1] + dy * a.label_offset,
                                "left" if dx > 0 else
                                "right" if dx < 0 else "center",
                                90 if vertical else 0)
        # Labels that would land on top of each other get pushed further
        # out, one after another. Nine `GND` strings on U2's pin row all
        # arrive at the same place otherwise. Deleting them instead was
        # tried and left 53 pins with a stub ending in nothing, which
        # reads as not connected -- the overlap was the lesser fault and
        # this is where it belongs.
        # Grouped on a coarse grid in both axes. Bucketing on the single
        # axis they "should" stack on was tried and measured worse -- 22
        # overlapping pairs against 19 -- because the collisions are not
        # all column-shaped. Measured beats reasoned here.
        bucket = collections.defaultdict(list)
        for key, (lx, ly, al, _r) in label_fix.items():
            bucket[(al, round(lx / 40) if al == "center" else round(ly / 40),
                    round(ly / 40) if al == "center" else round(lx / 40)
                    )].append(key)
        for keys in bucket.values():
            if len(keys) < 2:
                continue
            for rank, key in enumerate(sorted(keys, key=lambda k: label_fix[k])):
                lx, ly, al, rot_ = label_fix[key]
                # Capped, and the cap is the point. Unbounded ranks put a
                # GND 72 units -- 18 mm -- from the pin it belongs to,
                # with eleven stubs at U2 left bare beside it. A name
                # that far from its pin is worse than two names touching:
                # the reader can still read an overlap, but a detached
                # label belongs to nothing. Three levels, then wrap.
                step = (rank % 3) * a.label_offset * 2
                # A rotated label needs no stagger: it is a pin pitch
                # wide, so its neighbours cannot reach it.
                if rot_:
                    continue
                if al == "left":
                    label_fix[key] = (lx + step, ly, al, rot_)
                elif al == "right":
                    label_fix[key] = (lx - step, ly, al, rot_)
                else:
                    label_fix[key] = (lx, ly + step, al, rot_)
        moved = len(label_fix)

    out_lines = []
    for i, (h, b, doc, raw) in enumerate(rows):
        if moved and h and id(b) in label_fix:
            nb = dict(b)
            nb["x"], nb["y"], nb["align"], nb["rotation"] = \
                label_fix[id(b)]
            raw = (f"{json.dumps(h, separators=(',', ':'))}||"
                   f"{json.dumps(nb, separators=(',', ':'))}|")
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
    # Append-only, with exactly one named exception. `--labels-outward`
    # repositions net labels, so those records *do* change -- and that is
    # the only change allowed, in only three keys. Loosening the check to
    # "some records may differ" would retire the thing it is for; naming
    # the exception keeps every other record as locked as before.
    LABEL_KEYS = {"x", "y", "align", "rotation"}
    for (h1, b1, _, _), (h2, b2, _, _) in zip(rows, kept):
        if (h1, b1) == (h2, b2):
            continue
        differs = {k for k in set(b1) | set(b2) if b1.get(k) != b2.get(k)}
        allowed = (a.labels_outward and h1 == h2
                   and b1.get("key") == "NET" and differs <= LABEL_KEYS)
        if not allowed:
            a.out.unlink(missing_ok=True)
            print(f"  error: an existing record changed in "
                  f"{sorted(differs) or 'its header'} — {a.out} deleted\n",
                  file=sys.stderr)
            return 1

    if a.labels_outward:
        print(f"  {moved} net label(s) moved past their stub end")

    # Text against text, which nothing measured until a reviewer counted
    # it by eye and found U2's pin row reading `GNDGNDGNDGND`. The checks
    # here covered label-against-body and flag-against-flag -- both
    # predicates that already held -- which is the fail-open shape this
    # project warns about everywhere else: a check that asks the question
    # whose answer is already yes.
    _, fin = load(a.out)
    anc = {h["id"]: (b["x"], b["y"]) for h, b, _, _ in fin
           if h and h["type"] == "COMPONENT" and b.get("partId")
           and isinstance(b.get("x"), (int, float))}
    texts = []
    for h, b, _, _ in fin:
        if (not h or h["type"] != "ATTR" or not b.get("value")
                or b.get("valueVisible") is not True
                or b.get("key") not in ("NET", "Global Net Name",
                                        "Designator")):
            continue
        x, y = b.get("x"), b.get("y")
        if not isinstance(x, (int, float)):
            x, y = anc.get(b.get("parentId"), (None, None))
        if x is None:
            continue
        w = len(b["value"]) * 3.0
        if (b.get("rotation") or 0) % 180:
            # Set along its stub: one pin pitch wide, as tall as it is
            # long. Measuring it as if it were horizontal reported 174
            # collisions on a sheet that has none.
            texts.append((x - 4, y - w, x + 4, y + w))
        else:
            texts.append((x - w, y - 4, x + w, y + 4))
    clashes = 0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            u, v = texts[i], texts[j]
            if u[0] < v[2] and v[0] < u[2] and u[1] < v[3] and v[1] < u[3]:
                clashes += 1
    # The question a reader actually asks of a label: which pin is this
    # one for? A name that is nearer somebody else's stub end than its
    # own cannot be answered, and no overlap count sees it -- staggering
    # labels to avoid collisions is exactly what produces it.
    lgeom = {}
    for h, b, _, _ in fin:
        if h and h["type"] == "LINE" and b.get("lineGroup"):
            lgeom[b["lineGroup"]] = (
                (round(b["startX"]), round(b["startY"])),
                (round(b["endX"]), round(b["endY"])))
    lends = [(pt, w) for w, g in lgeom.items() for pt in g]
    adrift = 0
    for h, b, _, _ in fin:
        if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                or b.get("valueVisible") is not True
                or not isinstance(b.get("x"), (int, float))
                or b.get("parentId") not in lgeom):
            continue
        near = sorted((((b["x"] - pt[0]) ** 2
                        + (b["y"] - pt[1]) ** 2) ** 0.5, w)
                      for pt, w in lends)
        mine_ = next(i for i, (_, w) in enumerate(near)
                     if w == b["parentId"])
        if mine_ > 0 and near[0][0] <= near[mine_][0]:
            adrift += 1
    print(f"  {adrift} net label(s) sit nearer a foreign stub than their own"
          + ("" if adrift == 0 else " — which pin they name is a guess"))

    # Text lying on a wire that is not its own. The counter exists
    # because the last two rounds each traded one fault for another and
    # nothing saw the new one: capping the stagger cured the drift and
    # stacked the labels, turning them cured the stack and put them on
    # the wire. A measure of what was corrected is not a measure of what
    # was caused.
    segs_ = [(round(b["startX"]), round(b["startY"]),
              round(b["endX"]), round(b["endY"]), b["lineGroup"])
             for h, b, _, _ in fin
             if h and h["type"] == "LINE" and b.get("lineGroup")]
    onwire_ = 0
    for h, b, _, _ in fin:
        if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                or b.get("valueVisible") is not True
                or not isinstance(b.get("x"), (int, float))):
            continue
        w = len(str(b["value"])) * 3.0
        if (b.get("rotation") or 0) % 180:
            r = (b["x"] - 4, b["y"] - w, b["x"] + 4, b["y"] + w)
        else:
            r = (b["x"] - w, b["y"] - 4, b["x"] + w, b["y"] + 4)
        for x1, y1, x2, y2, g in segs_:
            if g == b.get("parentId"):
                continue              # its own stub is where it belongs
            if y1 == y2:
                hit = (r[1] < y1 < r[3] and min(x1, x2) < r[2]
                       and r[0] < max(x1, x2))
            elif x1 == x2:
                hit = (r[0] < x1 < r[2] and min(y1, y2) < r[3]
                       and r[1] < max(y1, y2))
            else:
                hit = False
            if hit:
                onwire_ += 1
                break
    print(f"  {onwire_} net label(s) lie on a wire that is not their own")

    print(f"  {clashes} pair(s) of visible text overlap"
          + ("" if clashes == 0 else " — unreadable where they do"))
    print(f"  {len(added)} record(s) added")
    print(f"  wrote {a.out}")
    print(f"\n  prove it:  ./tools/epro.py diff {a.out} {a.netlist}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
