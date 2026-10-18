#!/usr/bin/env python3
"""Draw wires between pins that already sit next to each other.

The netlist importer gives every pin a short stub carrying its net name. That
is electrically complete and reads as a parts list. This adds real drawn
wires for the nets where both ends are close enough that a line helps.

**It adds, and it deletes one thing.** Connectivity is still carried by name,
so a drawn wire is decoration on top of a connection that already exists: get
the geometry slightly wrong and the circuit is unchanged, which is the
opposite of the usual failure here, where a drawn wire *is* the connection and
being one unit short is an open circuit.

The deletion is the stub a drawn wire has made redundant. A pin's stub is the
importer's lead out to where its name sits; once a rail is drawn *through*
the pin, the half of that stub beyond the junction is a wire to nowhere, and
on the 5 V node it read as two capacitors with a free-standing cable beside
them, already connected and still drawn as if not. `dead_tails` names the
groups that qualify and the write loop drops their line records. The name
does not leave with them — the label is re-parented onto the drawn wire,
which is where `epro.py diff` reads a net from anyway.

The one way an added wire can do harm is by touching a pin belonging to a
different net, which in a geometric schematic creates a connection nobody
asked for. That is checked for every segment before anything is written.

**A wire somebody else drew is checked here too, at last.** `handplace.py`
verifies its own `wire` lines when it writes them, and at that moment this
tool has not drawn anything -- so a hand lead that crosses a rail laid down
*here* passed every check the chain had, twice, and was found by eye both
times. `hand_crossings` runs the same test over the hand wires against the
finished geometry, which is the only place both exist at once.

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


def hand_crossings(rows, netof, at, added=(), wid_of=None):
    """Hand-drawn wires that run across another net's wire, as a list.

    `handplace.py` checks its own wires when it writes them, and at that
    point this tool has not drawn anything yet -- so a hand lead that
    crosses a rail laid down *here* is invisible to every check the chain
    has. It was found by eye, twice, and each time on a route whose ends
    were right. This is that check, run where the finished geometry is.

    The hand wire's own net is read off the stubs its ends land on, the
    same way `handplace` reads it, because a hand wire carries no NET
    attribute of its own.

    `added` is what this run is about to write, and leaving it out was the
    first version's whole defect: reading only `rows` -- the *input* --
    meant the check could not see the wires it exists to check the hand
    wires against, and it reported clean on a sheet where COMP_5V crossed
    a hand-drawn ground lead twice. `wid_of` names each added wire's net,
    which is not in `netof` because the tool never writes a NET attribute
    of its own.
    """
    hw = {h["id"] for h, b, _, _ in rows
          if h and h["type"] == "WIRE" and str(h["id"]).startswith("hw")}
    if not hw:
        return []
    def net_at(v):
        return set(at.get((round(v[0]), round(v[1])), ()))
    mine, others = [], []
    for h, b, _, _ in rows:
        if not h or h["type"] != "LINE" or b.get("startX") is None:
            continue
        seg = ((round(b["startX"]), round(b["startY"])),
               (round(b["endX"]), round(b["endY"])))
        if b.get("lineGroup") in hw:
            mine.append((seg, net_at(seg[0]) | net_at(seg[1])))
        else:
            others.append((seg, {netof[b["lineGroup"]]}
                           if b.get("lineGroup") in netof else set()))
    for hh, bb in added:
        if hh.get("type") != "LINE" or bb.get("startX") is None:
            continue
        n_ = (wid_of or {}).get(bb.get("lineGroup"))
        others.append((((round(bb["startX"]), round(bb["startY"])),
                        (round(bb["endX"]), round(bb["endY"]))),
                       {n_} if n_ else set()))
    out = []
    for (a, b), ns in mine:
        for c, d in [o[0] for o in others]:
            on = next((o[1] for o in others if o[0] == (c, d)), set())
            if on and on & ns:
                continue
            if on == set():
                continue
            if _meet(a, b, c, d):
                out.append((a, b, c, d, sorted(on)))
    return out


def _meet(p, q, r, s):
    """Do segments pq and rs cross, endpoints *not* counted?

    A hand wire ending on another wire of its own net is a junction, and
    that is what half of them are for. Only a crossing is reported.
    """
    def side(o, u, v):
        return ((u[1] - o[1]) * (v[0] - o[0])
                - (u[0] - o[0]) * (v[1] - o[1]))
    d1, d2 = side(p, q, r), side(p, q, s)
    d3, d4 = side(r, s, p), side(r, s, q)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def touches(seg, pt):
    """Does an axis-aligned segment pass through or end on a point?"""
    x1, y1, x2, y2 = seg
    x, y = pt
    if y1 == y2:
        return y == y1 and min(x1, x2) <= x <= max(x1, x2)
    if x1 == x2:
        return x == x1 and min(y1, y2) <= y <= max(y1, y2)
    return False


def dead_tails(rows, pins, chosen, flagpt, hidden):
    """Stub groups a drawn wire has made redundant, as {lineGroup: (net,
    wire to keep)}. The second element is None where the wire that reaches
    the pin is one this tool is about to draw — the caller knows its id —
    and the id of an existing wire where it is one already in the file.

    A pin's stub is the importer's lead from the pin out to where its name
    sits. Once schwire draws a rail *through* that pin the stub has nothing
    left to do, and the segment on the far side of the junction is a wire to
    nowhere: on the 5 V node, C10 and C11 each kept a cable standing in the
    air beside a pin the rail already reached. The reader sees a part that
    is connected and drawn as if it is not.

    The name must not leave with the stub, so the label is re-parented onto
    the drawn wire rather than removed — `epro.py diff` counts one
    `ATTR key=NET` per pin, and the net a wire carries is that attribute's
    `parentId`, not where its text sits. Four things have to hold, and each
    false one is a reason to leave the stub alone:

    - the stub's net was drawn;
    - one end is a pin and the drawn wire passes through *that* end. A stub
      with two pins, or a pin in its middle, is not something this has seen
      and is skipped rather than guessed at;
    - the far end carries nothing — no pin, no flag, no second stub end. A
      flagged stub is the flag's *lead*: netflags runs it to the stub's far
      end, so deleting one would leave the symbol hanging in the air;
    - every label on the stub is already hidden, *where the drawn net is
      what reaches the pin*. A visible one is the node's name on screen,
      and then the tail is the cheapest thing to keep.

    The fifth way in is a wire that is already in the file rather than one
    this tool drew: the ground bus `netflags --ground-bus` runs through the
    pins of a row. That bus is drawing over a connection each member's own
    stub already carries, so the members' stubs are the comb the bus was
    drawn to replace, and they go where the drawn net's would not — because
    their name is on screen as the ground *symbol* at the end of the run,
    not as the text on their own tail. The bus states no net of its own (a
    wire carrying one would lift the export's ATTR count above the
    netlist's), so it is accepted only where it states none; a wire naming
    a different net is a short, and not this rule's to fix. The entry then
    points the label at that wire instead of at a drawn one, and the caller
    hides it, since the name has already left the tail.
    """
    netof = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    shown = {b["parentId"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("parentId") in netof and b.get("valueVisible") is not False
             and id(b) not in hidden}
    geom = collections.defaultdict(list)
    at_end = collections.Counter()
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup"):
            s = (grid(b["startX"], b["startY"]), grid(b["endX"], b["endY"]))
            geom[b["lineGroup"]].append(s)
            at_end[s[0]] += 1
            at_end[s[1]] += 1
    pinpts = {(x, y) for _, x, y in pins}

    # Which *nodes* carry a symbol that names them. The fifth way in below
    # leans on the name having already left the tail for something on the
    # sheet, and the case it was written for is a ground bus: the members'
    # stubs go because the ground *symbol* at the end of the run names the
    # node. A hand-drawn wire over a node that carries no symbol is not
    # that case, and taking it as one hid every visible `COMP_5V` on the
    # supply sheet -- the wire stayed, and nothing on the sheet said which
    # net it was.
    #
    # Asked of the *node*, not of the net. A net is not all one node --
    # a divider's sense net is two, far apart, one flagged and one not --
    # and a net-level answer would let a symbol on one excuse eating the
    # name on the other. Union-find over the points wires meet at is the
    # same "what is one node" this project asks everywhere else.
    _up = {}

    def _find(p):
        _up.setdefault(p, p)
        while _up[p] != p:
            _up[p] = _up[_up[p]]
            p = _up[p]
        return p

    for _segs in geom.values():
        for _s, _e in _segs:
            _a, _b = _find(_s), _find(_e)
            if _a != _b:
                _up[_a] = _b
    # A pin in the *middle* of a wire is a T, and that is what half of
    # these joins are: the ground bus runs through its members' pins
    # rather than ending on them. Unioning endpoints alone calls the bus
    # one node and every part hanging off its length another, which is
    # exactly the answer that made five stubs keep their tails.
    _pts = set(pinpts) | set(flagpt) | {p for _ss in geom.values()
                                        for _s in _ss for p in _s}
    for _p in _pts:
        for _ss in geom.values():
            for _s, _e in _ss:
                if (_p != _s and _p != _e
                        and touches((_s[0], _s[1], _e[0], _e[1]), _p)):
                    _a, _b = _find(_p), _find(_s)
                    if _a != _b:
                        _up[_a] = _b
    named = set()
    for lg, segs in geom.items():
        if not netof.get(lg):
            continue
        for s, e in segs:
            if any(touches((s[0], s[1], e[0], e[1]), fp) for fp in flagpt):
                named.add(_find(s))

    out = {}
    for lg, segs in geom.items():
        n = netof.get(lg)
        if not n:
            continue
        ends = {p for s in segs for p in s}
        pins_here = ends & pinpts
        if len(pins_here) != 1:
            continue
        pin_end = next(iter(pins_here))
        far = next(iter(ends - {pin_end}))
        if far in flagpt or far in pinpts or at_end[far] != 1:
            continue
        # ...and no flag anywhere along it either. A flag is placed by
        # netflags to name a node, and a stub it sits on is that node's
        # lead whatever point of it the symbol landed on -- netflags runs
        # its own lead out to the stub's end, but nothing in the file
        # promises it always will.
        if any(touches((s[0], s[1], e[0], e[1]), fp)
               for s, e in segs for fp in flagpt):
            continue
        drew = chosen.get(n)
        if drew is not None:
            # A net this tool draws: the tail goes only where its label is
            # off screen already, since a visible label *is* the node's name.
            if lg in shown:
                continue
            if any(touches(s, pin_end) for s in drew):
                out[lg] = (n, None)
            continue
        # ...or a wire already in the file reaching the same pin, which is
        # the ground bus. See the docstring: no net of its own, and taken
        # only where it names none, since a wire naming another net is a
        # short and a short is not a stub to trim.
        hosts = {lg2 for lg2, segs2 in geom.items()
                 if lg2 != lg and netof.get(lg2, n) in (n, None)
                 and any(touches((s2[0][0], s2[0][1], s2[1][0], s2[1][1]),
                                 pin_end) for s2 in segs2)}
        if len(hosts) == 1 and _find(pin_end) in named:
            out[lg] = (n, next(iter(hosts)))
    return out


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
    ap.add_argument("--local-rail", type=int, default=0,
                    help="a flagged net whose pins all lie within this "
                         "many units is drawn as a wire anyway; it is one "
                         "node of the circuit, not a rail crossing the "
                         "sheet")
    ap.add_argument("--skip-flagged", action="store_true",
                    help="do not route to a pin that already carries a rail "
                         "flag; the flag names and connects it, so a wire "
                         "there states the same thing twice")
    ap.add_argument("--never-draw", default="GND",
                    help="nets that stay labels whatever their grouping: "
                         "every pin on one carries a symbol already, so a "
                         "line between them is noise. Default GND")
    ap.add_argument("--max-pins", type=int, default=4,
                    help="leave a net as a label above this pin count "
                         "(default 4; the power rails are far above it)")
    ap.add_argument("--keep-text", action="store_true",
                    help="leave every stub's net name visible, including "
                         "the ones on a pin the drawn wire already reaches "
                         "(default: one name per drawn node)")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    for q in (a.export, a.netlist):
        if not q.exists():
            print(f"error: {q} not found", file=sys.stderr)
            return 2

    priority = {s.strip() for s in a.priority_nets.split(",") if s.strip()}
    never = {s.strip() for s in a.never_draw.split(",") if s.strip()}

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

    # A pin carrying a rail flag is already connected, by symbol, and the
    # flag names the net -- so a wire does not have to reach it, and one
    # that does states the same connection twice. The second statement is
    # the expensive one: it is what makes a three-pin rail cross the sheet
    # to collect a pin a +5V flag had already finished with. Drawing among
    # the *unflagged* pins only beats both the old behaviour and skipping
    # the net, which would drop the pins that have no flag back to labels.
    #
    # A flag is a placed component carrying no Designator -- netflags.py
    # mints them that way, and that is what keeps them out of the BOM. So
    # this reads the file rather than taking a second --nets list that
    # would drift out of step with the one netflags was given.
    hasdesig = {b["parentId"] for h, b, _, _ in rows
                if h and h["type"] == "ATTR" and b.get("key") == "Designator"
                and b.get("value")}
    flagpt = {grid(b["x"], b["y"]) for h, b, doc, _ in rows
              if h and h["type"] == "COMPONENT" and doc
              and doc[0] in ("SCH", "SCH_PAGE") and h["id"] not in hasdesig
              and isinstance(b.get("x"), (int, float))}
    flagged_pin = set()
    if flagpt:
        stubgeom = collections.defaultdict(list)
        for h, b, _, _ in rows:
            if h and h["type"] == "LINE" and b.get("lineGroup"):
                stubgeom[b["lineGroup"]].append(
                    (grid(b["startX"], b["startY"]),
                     grid(b["endX"], b["endY"])))
        pinpts = {(x, y) for _, x, y in pins}
        for ends in stubgeom.values():
            touched = {p for seg in ends for p in seg}
            if touched & flagpt:
                flagged_pin |= touched & pinpts

    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    want = collections.Counter()
    for c in design.values():
        for n in c["pins"].values():
            if not n.upper().startswith("NC_"):
                want[n] += 1

    draw, skipped = {}, collections.Counter()
    dropped_flagged = local_rails = 0
    for n, members in bynet.items():
        pts = sorted({(x, y) for _, x, y in members})
        if n in never:
            # Ground is the one net a reader does not follow: every pin
            # of it carries a symbol, and a line joining those symbols
            # says nothing the symbols do not. The local-rail rule below
            # draws a grouped net *before* the pin-count test, so this
            # has to come first -- left to the router it depended on the
            # route failing, and when it stopped failing the 5 V sheet
            # grew a wire round six ground pins that nobody had drawn.
            skipped["carried by a symbol on every pin"] += 1
            continue
        if a.skip_flagged and flagged_pin:
            # ... unless the whole net is a local node. L1's output and
            # the two capacitors across it are one point of the circuit
            # three hundred units wide, and with every pin flagged
            # nothing was drawn between them: two capacitors hanging
            # between a +5V flag and a ground triangle with no visible
            # tie to anything, and the inductor's stub ending in air.
            # A rail that leaves the sheet is a flag by right; a rail
            # that is one node on it is a wire.
            # A rail crosses the sheet, but it does not do so evenly:
            # L1's output, the two capacitors across it and the feedback
            # divider are one point of the circuit, and the jump to the
            # second converter is a different matter. Grouping the pins
            # by proximity draws the local node and leaves the jump to
            # the flags -- which is what a schematic does. Before this,
            # BUS_5V was flagged end to end and the output capacitors
            # hung between a +5V flag and a ground triangle with nothing
            # tying them to the inductor they smooth.
            groups = []
            for q in sorted(pts):
                for g in groups:
                    if any(abs(q[0] - r[0]) + abs(q[1] - r[1])
                           <= a.local_rail for r in g):
                        g.append(q)
                        break
                else:
                    groups.append([q])
            merged = True
            while merged:
                merged = False
                for i in range(len(groups)):
                    for j in range(i + 1, len(groups)):
                        if any(abs(u[0] - v[0]) + abs(u[1] - v[1])
                               <= a.local_rail
                               for u in groups[i] for v in groups[j]):
                            groups[i] += groups.pop(j)
                            merged = True
                            break
                    if merged:
                        break
            keep = [g for g in groups if len(g) > 1]
            stranded = [p for g in groups if len(g) == 1 for p in g]
            # A grouped net is drawn locally and its far pins are left to
            # the flag that already names and connects them -- but only a
            # pin that *has* one. FB_5V is three pins with the feedback
            # divider 405 units from U4: proximity kept the divider pair
            # and stranded U4's pin, which carries no flag, so that pin
            # kept a stub ending in air while its net was drawn elsewhere
            # on the sheet. The pinch is not describing a rail when a
            # pinched-off pin has nothing to fall back on, so the net is
            # routed whole and the router gets to say whether it reaches.
            if keep and a.local_rail and all(p in flagged_pin
                                             for p in stranded):
                local_rails += len(keep)
                dropped_flagged += len(pts) - sum(len(g) for g in keep)
                draw[n] = keep
                continue
            # A pin inside a group this net is drawing stays in it, flag
            # or no flag. Dropping it because it also carries a symbol
            # left C8's supply ending in a VBUS flag beside a wire that
            # ran from C9 to U4's VIN pin -- one rail drawn as two, which
            # reads as two nets. Only the *stranded* pins are left to
            # their flags; that is what the flag is for.
            grouped = {q for g in keep for q in g}
            free = [p for p in pts if p not in flagged_pin or p in grouped]
            if len(free) < len(pts):
                dropped_flagged += len(pts) - len(free)
                pts = free
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
        draw[n] = [pts]

    print(f"  {len(draw)} net(s) to draw, {sum(skipped.values())} left as labels")
    if local_rails:
        print(f"         {local_rails:3d}  flagged net(s) drawn anyway, "
              f"being one node rather than a rail across the sheet")
    if dropped_flagged:
        print(f"         {dropped_flagged:3d}  pin(s) left to their rail flag, "
              f"which already names and connects them")
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

    # The wires a person drew, which are in `rows` but not in `stubs`: a
    # hand lead carries no NET attribute of its own, and `stubs` is keyed
    # on one. They were invisible to the router for that reason, and it
    # answered by routing COMP_5V straight across the hand-drawn ground
    # lead between U4's ground pin and the catch diode -- twice, at
    # (400,130) and (410,150). A crossing is a junction in a geometric
    # schematic, so the two nets read as one, and `epro.py diff` counts
    # names and cannot see it. Tested with `_meet` rather than `cross`:
    # a drawn wire ending *on* a hand wire is a junction the sheet wants
    # -- J4's ground arrives on R26's pin that way -- and only passing
    # through is the fault.
    hands = []
    for h, b, _, _ in rows:
        if (h and h["type"] == "LINE" and b.get("startX") is not None
                and str(b.get("lineGroup", "")).startswith("hw")):
            sx, sy = grid(b["startX"], b["startY"])
            ex, ey = grid(b["endX"], b["endY"])
            hands.append((sx, sy, ex, ey))

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
        # A net name a wire runs through is a false statement about that
        # wire, and the three fixed shapes never consulted the obstacle
        # map -- only the maze did. So fourteen labels sat on a foreign
        # line on a sheet this check called clean. Its own name is
        # exempt: a label on the wire it names is the point of it.
        for lx0, ly0, lx1, ly1, lnet in label_boxes:
            if lnet == net:
                continue
            if any(crosses_box(s, (lx0, ly0, lx1, ly1)) for s in segs):
                return (lx0, ly0, lx1, ly1), [f"across the {lnet} label"]
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
            for q in hands:
                if _meet((s[0], s[1]), (s[2], s[3]),
                         (q[0], q[1]), (q[2], q[3])):
                    return q, ["crosses a hand-drawn wire"]
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
                label_boxes.append((lx0 - 4, ly0 - w0, lx0 + 4, ly0 + w0,
                                    b["value"]))
            else:
                label_boxes.append((lx0 - w0, ly0 - 4, lx0 + w0, ly0 + 4,
                                    b["value"]))

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

    # Label boxes go in as *owned*, beside the pin boxes, not as anonymous
    # bodies. They were anonymous, and an anonymous box blocks every net
    # including the one it names -- so a net whose own label sat on the
    # only way in could not be routed to itself. COMP_5V is exactly that:
    # its label on C7's and R27's stubs closed the two ends of the net, and
    # the maze reported "no clean path" while the straight shapes were
    # rejected by `collides` for the same reason, the labels being its own.
    # As a net box the owner is exempt and every other net still stops.
    obst = Obstacles(list(body.values()), at,
                     stubs, a.clearance, a.end_clearance,
                     list(pin_boxes) + list(label_boxes))
    # ...and the hand-drawn wires, as obstacles to every net. `None` is
    # what says "no net of ours": nothing this run draws is a ground
    # lead, so a hand lead is a stranger to all of it. `collides` alone
    # keeps a route off them but cannot find one -- the maze would spend
    # its search box on paths it is about to throw away, and report "no
    # clean path" for a net that has one. On the supply sheet, while the
    # ground line still reached the far side of stage 2, that is what it
    # did: two nets that had been drawn came back as labels, which is a
    # worse sheet than the crossing they replaced.
    obst.add_wire(hands, None)

    # The node each pin was assigned to, kept for the label rule below:
    # routing replaces `draw` with segments and the grouping is gone.
    nodegroups = {n: [list(g) for g in gs] for n, gs in draw.items()}

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
        flat = [q for g in pts for q in g]
        return (item[0] not in priority,
                max(x for x, _ in flat) - min(x for x, _ in flat)
                + max(y for _, y in flat) - min(y for _, y in flat), item[0])

    for n, groups_ in sorted(draw.items(), key=_span):
        got = []
        for pts in groups_:
            for segs in routes(pts, obst, a.detour, n):
                if collides(n, pts, segs, laid) is None:
                    got += segs
                    laid += [(x1, y1, x2, y2, n) for x1, y1, x2, y2 in segs]
                    obst.add_wire(segs, n)
                    break
        if got:
            chosen[n] = got
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
    wid_of = {n: hashlib.sha256(f"wire/{n}".encode()).hexdigest()[:16]
              for n in draw}
    for n in sorted(draw):
        segs = draw[n]
        wid = wid_of[n]
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
    moved, hidden = 0, 0
    hide = set()

    # --- one name per drawn node -------------------------------------------
    # `--one-per-node` in netflags gave a drawn rail one flag rather than
    # one per pin, and the stub labels were left standing beside it, so
    # the 5 V rail still read BUS_5V four times: three texts and a flag,
    # and the reader has four chances to ask whether they are the same
    # node. They are -- schwire has just drawn them together. A name at
    # a pin the wire already reaches adds nothing, and where the node
    # carries a flag the symbol *is* the name. Visibility only: the
    # attribute stays, so the value that carries the connectivity and
    # `epro.py diff` are both untouched.
    if not a.keep_text:
        pinpts_ = {(x, y) for _, x, y in pins}
        lglines = collections.defaultdict(list)
        for h_, b_, _, _ in rows:
            if h_ and h_["type"] == "LINE" and b_.get("lineGroup"):
                lglines[b_["lineGroup"]].append(
                    (grid(b_["startX"], b_["startY"]),
                     grid(b_["endX"], b_["endY"])))
        flagged_pt = set()
        for _lg, _ends in lglines.items():
            _touched = {p for _s, _e in _ends for p in (_s, _e)}
            if _touched & flagpt:
                flagged_pt |= _touched & pinpts_
        bypin = collections.defaultdict(list)      # (net, pin) -> [ATTR]
        for h_, b_, _, _ in rows:
            if (not h_ or h_["type"] != "ATTR" or b_.get("key") != "NET"
                    or b_.get("valueVisible") is not True
                    or b_.get("parentId") not in lglines):
                continue
            for _s, _e in lglines[b_["parentId"]]:
                _p = _s if _s in pinpts_ else (_e if _e in pinpts_ else None)
                if _p is not None:
                    bypin[b_["value"], _p].append(b_)
        # What the drawn wires cover. A lead *inside* a drawn span is one
        # the route has taken over -- its name belongs on the wire. A lead
        # sticking out of the node is where its name lives, and the wire
        # only reaches it at the pin.
        drawn_segs = []
        for h_, b_ in added:
            if h_.get("type") == "LINE" and b_.get("startX") is not None:
                drawn_segs.append((grid(b_["startX"], b_["startY"]),
                                   grid(b_["endX"], b_["endY"])))

        def _covered(p_, q_):
            for r_, s_ in drawn_segs:
                if r_[0] == s_[0] == p_[0] == q_[0]:
                    lo, hi = sorted((r_[1], s_[1]))
                    if lo <= min(p_[1], q_[1]) and max(p_[1], q_[1]) <= hi:
                        return True
                elif r_[1] == s_[1] == p_[1] == q_[1]:
                    lo, hi = sorted((r_[0], s_[0]))
                    if lo <= min(p_[0], q_[0]) and max(p_[0], q_[0]) <= hi:
                        return True
            return False

        def _onwire(b_):
            """Is this name on the wire, or on a lead it has not taken
            over? The latter is what keeps a dead spur on the sheet."""
            for _s, _e in lglines.get(b_.get("parentId"), []):
                if _s in pinpts_ and _e in pinpts_:
                    continue                     # the drawn wire itself
                if not _covered(_s, _e):
                    return False
            return True

        for n_ in sorted(chosen):
            for g_ in nodegroups[n_]:
                gset = set(g_)
                here = [b_ for p_ in gset for b_ in bypin.get((n_, p_), [])]
                if not here:
                    continue
                # A node with a symbol keeps none of its texts; one
                # without keeps the first in reading order among those on
                # the wire, so the net is still named exactly once and the
                # name is never what holds a dead spur up. R27 and C6 are
                # the case: both name the node between them, C6's lead
                # points away from its own body into empty space, and
                # reading order alone picked that one for sitting higher.
                keep_ = (None if gset & flagged_pt
                         else min(here, key=lambda b_: (not _onwire(b_),
                                                        b_.get("y") or 0,
                                                        b_.get("x") or 0)))
                for b_ in here:
                    if b_ is not keep_:
                        hide.add(id(b_))
        if hide:
            print(f"         {len(hide):3d}  stub label(s) hidden, the node "
                  f"they name already carrying a flag or another label")

    # --- drop the stub a drawn wire has made dead --------------------------
    # Runs after the one-name-per-node rule because that rule is what says
    # whether a stub's label is still the node's name on screen, and a stub
    # whose label is doing that job keeps its line.
    trimmed = dead_tails(rows, pins, chosen, flagpt, hide)
    drop, reparent = set(), {}
    for h, b, _, _ in rows:
        if not h:
            continue
        if h["type"] == "WIRE" and h["id"] in trimmed:
            drop.add(h["id"])
        elif h["type"] == "LINE" and b.get("lineGroup") in trimmed:
            drop.add(h["id"])
        elif h["type"] == "ATTR" and b.get("parentId") in trimmed:
            n_, tgt = trimmed[b["parentId"]]
            reparent[id(b)] = wid_of[n_] if tgt is None else tgt
            if tgt is not None:
                # The name left the tail for a wire already on the sheet,
                # so the text has to go with it: left visible it would sit
                # at the old free end, naming a wire that is not there.
                hide.add(id(b))
    if trimmed:
        print(f"         {len(trimmed):3d}  stub(s) dropped, a drawn wire "
              f"reaching the pin the stub led to")

    # Bound outside the flag on purpose. Both are read by the write loop
    # below, and `hide` is no longer filled only here: the one-name-per-node
    # rule hides a stub label on any flag-carrying node whether or not the
    # labels are being moved, so the write loop reaches for `label_fix`
    # without --labels-outward and used to raise NameError instead.
    label_fix, moved = {}, 0

    if a.labels_outward:
        pinpts = {(x, y) for _, x, y in pins}
        geom = {}
        for h, b, _, _ in rows:
            if h and h["type"] == "LINE" and b.get("lineGroup"):
                geom[b["lineGroup"]] = (grid(b["startX"], b["startY"]),
                                        grid(b["endX"], b["endY"]))

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
            # Sideways as well as outward. A rotated label is a narrow
            # vertical strip centred on its own x, and its only offset
            # was along the stub -- so the line it names ran straight
            # through the glyphs, 21 times on one sheet. Offsetting
            # perpendicular as well puts the name beside the line, which
            # is where every drawing standard puts it.
            # Perpendicular, and only on the axis where there is room.
            # A rotated name moves 7 units sideways and clears its line.
            # A horizontal one cannot: the pin pitch is 10 and the text
            # is 8 tall, so every offset that takes it off its own wire
            # puts it between two pin rows. Measured, all counters at
            # once -- 0: thirteen names on their own line and nothing
            # else; -5: none, but nine labels nearer a foreign stub than
            # their own; -7: nine of those and six on a foreign net's
            # wire as well. A name lying on the line it names is
            # unambiguous; a name between two rows is not.
            sx = 7 if vertical else 0
            sy = 0
            label_fix[id(b)] = (free[0] + dx * a.label_offset + sx,
                                free[1] + dy * a.label_offset + sy,
                                "left" if dx > 0 else
                                "right" if dx < 0 else "center",
                                90 if vertical else 0)
        # Labels that would land on top of each other get pushed further
        # out, one after another. Nine `GND` strings on U2's pin row all
        # arrive at the same place otherwise. Deleting them instead was
        # tried and left 53 pins with a stub ending in nothing, which
        # reads as not connected -- the overlap was the lesser fault and
        # this is where it belongs.
        #
        # Stagger only the labels that actually touch. The first version
        # bucketed them on a coarse 40-unit grid and pushed the second and
        # third member of every bucket outward, which is a false positive
        # by construction: a 10-unit pin row holds text 8 tall, so three
        # names down one row do not touch and it staggered them anyway.
        # That is what put NC_BUCK_EN 32 units off its own stub and left
        # it nearer C9's pin -- on a different net -- than its own line.
        # Overlap, not proximity, is the thing to resolve.
        lval = {id(b): b["value"] for h, b, _, _ in rows
                if h and h["type"] == "ATTR" and b.get("key") == "NET"
                and b.get("value")}

        def _lbox(k):
            lx, ly, _al, rot_ = label_fix[k]
            w_ = len(str(lval[k])) * 3.0
            return ((lx - 4, ly - w_, lx + 4, ly + w_) if rot_ % 180
                    else (lx - w_, ly - 4, lx + w_, ly + 4))

        # A rotated label needs no stagger: it is a pin pitch wide, so its
        # neighbours cannot reach it. Left out of the overlap graph as
        # well as the loop.
        lkeys = [k for k in label_fix if not label_fix[k][3]]
        lparent = {}

        def _lfind(x):
            lparent.setdefault(x, x)
            while lparent[x] != x:
                lparent[x] = lparent[lparent[x]]
                x = lparent[x]
            return x

        for i, k1 in enumerate(lkeys):
            b1 = _lbox(k1)
            for k2 in lkeys[i + 1:]:
                b2 = _lbox(k2)
                if (b1[0] < b2[2] and b2[0] < b1[2]
                        and b1[1] < b2[3] and b2[1] < b1[3]):
                    ra, rb = _lfind(k1), _lfind(k2)
                    if ra != rb:
                        lparent[ra] = rb
        lgroups = collections.defaultdict(list)
        for k in lkeys:
            lgroups[_lfind(k)].append(k)
        for keys in lgroups.values():
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
                if al == "left":
                    label_fix[key] = (lx + step, ly, al, rot_)
                elif al == "right":
                    label_fix[key] = (lx - step, ly, al, rot_)
                else:
                    label_fix[key] = (lx, ly + step, al, rot_)
        moved = len(label_fix)

        # Two copies of ONE name landing on top of each other -- the two
        # resistors of a divider, each labelling the node between them --
        # is the commonest text collision left, and it is redundancy
        # rather than ambiguity. Hiding one is safe in a way that hiding
        # a lone label is not: the net is still named, a few units away,
        # on the other half of itself. The value is untouched, so the
        # connectivity the attribute carries is untouched.
        val = {}
        boxes_ = {}
        for h, b, _, _ in rows:
            if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                    or b.get("valueVisible") is not True
                    or id(b) not in label_fix):
                continue
            lx, ly, _al, rot_ = label_fix[id(b)]
            w_ = len(str(b["value"])) * 3.0
            boxes_[id(b)] = ((lx - 4, ly - w_, lx + 4, ly + w_) if rot_ % 180
                             else (lx - w_, ly - 4, lx + w_, ly + 4))
            val[id(b)] = b["value"]
        order_ = sorted(boxes_, key=lambda k: (val[k], label_fix[k]))
        for ii, k1 in enumerate(order_):
            if k1 in hide:
                continue
            for k2 in order_[ii + 1:]:
                if val[k1] != val[k2] or k2 in hide:
                    continue
                u, v = boxes_[k1], boxes_[k2]
                if u[0] < v[2] and v[0] < u[2] and u[1] < v[3] and v[1] < u[3]:
                    hide.add(k2)
        hidden = len(hide)

    # Designators clear of their own package. Every one of this sheet's
    # 23 sat on the outline of the part it names -- the importer puts
    # them at the symbol origin and nothing had moved them since. An
    # upright part gets its name to the right of the body, a flat one
    # above it; both outside the outline by a glyph height.
    desig_fix, dmoved = {}, 0
    if a.labels_outward:
        # Where every net label has ended up, so a designator can be put
        # somewhere none of them is.
        label_rects = []
        for h, b, _, _ in rows:
            if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                    or b.get("valueVisible") is not True
                    or id(b) in hide):
                continue
            if id(b) in label_fix:
                lx, ly, _al, rot_ = label_fix[id(b)]
            elif isinstance(b.get("x"), (int, float)):
                lx, ly, rot_ = b["x"], b["y"], (b.get("rotation") or 0)
            else:
                continue
            w2 = len(str(b["value"])) * 3.0
            label_rects.append((lx - 4, ly - w2, lx + 4, ly + w2)
                               if rot_ % 180 else
                               (lx - w2, ly - 4, lx + w2, ly + 4))
        for h, b, _, _ in rows:
            if (not h or h["type"] != "ATTR" or b.get("key") != "Designator"
                    or b.get("valueVisible") is not True
                    or not isinstance(b.get("x"), (int, float))):
                continue
            bx = body.get(b.get("value"))
            if bx is None:
                continue
            w_ = len(str(b["value"])) * 3.0
            upright = (bx[3] - bx[1]) > (bx[2] - bx[0])
            # Four sides, first clear one wins. One side is not enough:
            # J4's pins face left, so its name went right onto the two
            # net labels already there and turned one fault into two.
            tries = ([(bx[2] + 8 + w_, (bx[1] + bx[3]) / 2),
                      (bx[0] - 8 - w_, (bx[1] + bx[3]) / 2),
                      ((bx[0] + bx[2]) / 2, bx[1] - 12),
                      ((bx[0] + bx[2]) / 2, bx[3] + 12)] if upright
                     else [((bx[0] + bx[2]) / 2, bx[1] - 12),
                           ((bx[0] + bx[2]) / 2, bx[3] + 12),
                           (bx[2] + 8 + w_, (bx[1] + bx[3]) / 2),
                           (bx[0] - 8 - w_, (bx[1] + bx[3]) / 2)])
            for tx, ty in tries:
                r_ = (tx - w_, ty - 4, tx + w_, ty + 4)
                if any(r_[0] < q[2] and q[0] < r_[2]
                       and r_[1] < q[3] and q[1] < r_[3]
                       for q in label_rects) or \
                   any(r_[0] < q[2] and q[0] < r_[2]
                       and r_[1] < q[3] and q[1] < r_[3]
                       for q in body.values()):
                    continue
                desig_fix[id(b)] = (tx, ty)
                dmoved += 1
                break

    out_lines = []
    for i, (h, b, doc, raw) in enumerate(rows):
        nb = None
        if h and id(b) in desig_fix:
            nb = dict(b)
            nb["x"], nb["y"] = (int(v) for v in desig_fix[id(b)])
        elif h and id(b) in hide:
            nb = dict(b)
            nb["valueVisible"] = False
            if id(b) in label_fix:
                nb["x"], nb["y"], nb["align"], nb["rotation"] = \
                    label_fix[id(b)]
        elif moved and h and id(b) in label_fix:
            nb = dict(b)
            nb["x"], nb["y"], nb["align"], nb["rotation"] = \
                label_fix[id(b)]
        if id(b) in reparent and not (h and h.get("id") in drop):
            nb = dict(nb) if nb is not None else dict(b)
            nb["parentId"] = reparent[id(b)]
        if nb is not None:
            raw = (f"{json.dumps(h, separators=(',', ':'))}||"
                   f"{json.dumps(nb, separators=(',', ':'))}|")
        if not (h and h.get("id") in drop):
            out_lines.append(raw)
        if i == last_sch:
            out_lines += [f"{json.dumps(hh, separators=(',', ':'))}||"
                          f"{json.dumps(bb, separators=(',', ':'))}|"
                          for hh, bb in added]

    cross = hand_crossings(rows, netof, at, added, wid_of)
    if cross:
        for p_, q_, r_, s_, ns in cross[:6]:
            print(f"  CROSS  a hand wire {p_}-{q_} runs across {r_}-{s_} "
                  f"on {'/'.join(ns)}")
        a.out.unlink(missing_ok=True)
        print(f"\n  {len(cross)} hand-drawn wire(s) cross another net's "
              f"wire; {a.out} deleted -- a crossing reads as a junction, "
              f"and which it is cannot be told from the file\n",
              file=sys.stderr)
        return 1

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    _, back = load(a.out)
    n_drop = sum(1 for h, _, _, _ in rows if h and h.get("id") in drop)
    if len(back) != len(rows) - n_drop + len(added):
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(back)} lines out, expected {len(rows)} - "
              f"{n_drop} + {len(added)} — {a.out} deleted\n", file=sys.stderr)
        return 1
    new_ids = {h["id"] for h, _ in added}
    kept = [r for r in back if not (r[0] and r[0].get("id") in new_ids)]
    expect = [(h, b) for h, b, _, _ in rows
              if not (h and h.get("id") in drop)]
    if len(kept) != len(expect):
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(kept)} original lines out, expected "
              f"{len(expect)} — {a.out} deleted\n", file=sys.stderr)
        return 1
    # Append-only, with three named exceptions now. `--labels-outward`
    # repositions net labels; the one-name-per-node rule hides a stub label
    # on a node that already carries a name, which is a `valueVisible` flip
    # and nothing else; and `dead_tails` re-parents a hidden label onto the
    # drawn wire that made its stub redundant. Everything else stays as
    # locked as before -- loosening this to "some records may differ" would
    # retire the check it exists to be.
    LABEL_KEYS = {"x", "y", "align", "rotation", "valueVisible"}
    for (h1, b1), (h2, b2, _, _) in zip(expect, kept):
        b1e = ({**b1, "parentId": reparent[id(b1)]}
               if id(b1) in reparent else b1)
        if (h1, b1e) == (h2, b2):
            continue
        differs = {k for k in set(b1e) | set(b2) if b1e.get(k) != b2.get(k)}
        moved_label = (a.labels_outward and h1 == h2
                       and b1.get("key") in ("NET", "Designator")
                       and differs <= LABEL_KEYS)
        just_hidden = (h1 == h2 and b1.get("key") == "NET"
                       and differs <= {"valueVisible"}
                       and b2.get("valueVisible") is False)
        reparented = (h1 == h2 and id(b1) in reparent
                      and differs <= {"valueVisible"}
                      and b2.get("valueVisible") is False
                      and b2.get("parentId") == reparent[id(b1)])
        if not (moved_label or just_hidden or reparented):
            a.out.unlink(missing_ok=True)
            print(f"  error: an existing record changed in "
                  f"{sorted(differs) or 'its header'} — {a.out} deleted\n",
                  file=sys.stderr)
            return 1

    if a.labels_outward:
        print(f"  {moved} net label(s) moved past their stub end")
        if dmoved:
            print(f"  {dmoved} designator(s) moved clear of their own body")
    if hidden:
        print(f"  {hidden} duplicate net label(s) hidden where two copies of "
              f"one name overlapped")

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
    # Every pin still reaches copper. `dead_tails` only drops a stub whose
    # pin the drawn wire already covers, so this should find nothing --
    # which is the reason to check it here rather than trust that: the rule
    # asks whether the *planned* segment covers the pin, and this asks
    # whether the *written* file, with the stub actually gone, still does.
    # A pin that fails this is an open circuit on the sheet that every
    # other check passes, because `epro.py diff` reads the label that was
    # re-parented and is happy either way.
    segs = [(grid(b["startX"], b["startY"]), grid(b["endX"], b["endY"]))
            for h, b, _, _ in fin
            if h and h["type"] == "LINE" and b.get("lineGroup")
            and isinstance(b.get("startX"), (int, float))]
    orphan = [p for p in pins
              if not any(touches((s[0], s[1], e[0], e[1]), (p[1], p[2]))
                         for s, e in segs)]
    if orphan:
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(orphan)} pin(s) left with no wire under them "
              f"after the stub was dropped, starting at {orphan[0][0]} "
              f"({orphan[0][1]}, {orphan[0][2]}) — {a.out} deleted\n",
              file=sys.stderr)
        return 1
    # A text the *symbol* places -- a flag's `Global Net Name` -- carries
    # no coordinates, and nothing in the file says where EasyEDA will draw
    # it. Two guesses were tried and both invented defects: boxing it at
    # the component anchor put 'FB_3V3' on top of C12, and reading the
    # offset off the symbol (Power-5V holds it at (0, -15)) moved the
    # collision to 'COMP_5V' and 'FB_5V' -- against a renderer that draws
    # both apart, eighty units of sheet between them. So it is not
    # guessed at, it is *counted and left out*, and the count is reported:
    # "no overlap" over a set that quietly shed nine labels is the false
    # clean this whole check exists to avoid.
    texts = []
    unplaced = 0
    for h, b, _, _ in fin:
        if (not h or h["type"] != "ATTR" or not b.get("value")
                or b.get("valueVisible") is not True
                or b.get("key") not in ("NET", "Global Net Name",
                                        "Designator")):
            continue
        x, y = b.get("x"), b.get("y")
        if not isinstance(x, (int, float)):
            unplaced += 1
            continue
        w = len(b["value"]) * 3.0
        if (b.get("rotation") or 0) % 180:
            # Set along its stub: one pin pitch wide, as tall as it is
            # long. Measuring it as if it were horizontal reported 174
            # collisions on a sheet that has none.
            texts.append(((x - 4, y - w, x + 4, y + w), b["value"]))
        else:
            texts.append(((x - w, y - 4, x + w, y + 4), b["value"]))
    clashes = 0
    clash_at = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            u, v = texts[i][0], texts[j][0]
            if u[0] < v[2] and v[0] < u[2] and u[1] < v[3] and v[1] < u[3]:
                clashes += 1
                clash_at.append((texts[i][1], texts[j][1],
                                 (int(min(u[0], v[0])), int(min(u[1], v[1])))))
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
    lnet = {b["parentId"]: b["value"] for h, b, _, _ in fin
            if h and h["type"] == "ATTR" and b.get("key") == "NET"
            and b.get("value")}
    adrift = 0
    for h, b, _, _ in fin:
        if (not h or h["type"] != "ATTR" or b.get("key") != "NET"
                or b.get("valueVisible") is not True
                or not isinstance(b.get("x"), (int, float))
                or b.get("parentId") not in lgeom):
            continue
        # Nearest stub of a *different net*, against the nearest of its
        # own. By line group this reported a divider's two resistors as
        # ambiguous for a label sitting between two stubs that carry the
        # same name -- which is not ambiguous at all, because either
        # reading gives the reader the same net.
        own = [dist for dist, w in
               ((((b["x"] - pt[0]) ** 2 + (b["y"] - pt[1]) ** 2) ** 0.5, w)
                for pt, w in lends)
               if lnet.get(w) == b["value"]]
        other = [dist for dist, w in
                 ((((b["x"] - pt[0]) ** 2 + (b["y"] - pt[1]) ** 2) ** 0.5, w)
                  for pt, w in lends)
                 if lnet.get(w) not in (None, b["value"])]
        if own and other and min(other) <= min(own):
            adrift += 1
    print(f"  {adrift} net label(s) sit nearer a foreign stub than their own"
          + ("" if adrift == 0 else " — which pin they name is a guess"))

    # Text lying on a wire that is not its own. The counter exists
    # because the last two rounds each traded one fault for another and
    # nothing saw the new one: capping the stagger cured the drift and
    # stacked the labels, turning them cured the stack and put them on
    # the wire. A measure of what was corrected is not a measure of what
    # was caused.
    # By **net**, not by line group. The first version exempted only the
    # label's own stub, so a divider's two resistors facing each other
    # scored two violations for a label lying on the other half of its
    # own net, and every label sitting on the wire this tool had just
    # drawn for it scored one more. On the hand-laid supply sheet all
    # fourteen reported faults were of those two kinds -- a label on the
    # line it names is the point of it. Fifth time a counter here has
    # asked a question next to the one that matters.
    wirenet = {hashlib.sha256(f"wire/{n}".encode()).hexdigest()[:16]: n
               for n in want}
    netof_ = {b["parentId"]: b["value"] for h, b, _, _ in fin
              if h and h["type"] == "ATTR" and b.get("key") == "NET"
              and b.get("value")}
    segs_ = [(round(b["startX"]), round(b["startY"]),
              round(b["endX"]), round(b["endY"]),
              netof_.get(b["lineGroup"]) or wirenet.get(b["lineGroup"]))
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
        for x1, y1, x2, y2, gnet in segs_:
            # Its own net, wherever on it: its own stub, the other half
            # of the net, or the wire drawn for it.
            if gnet is None or gnet == b["value"]:
                continue
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
    for n_a, n_b, pt_ in sorted(clash_at)[:4]:
        print(f"           {n_a!r} and {n_b!r} meet at {pt_}")
    # Named, not folded into the zero above: these are labels the symbol
    # places, so where they land is not in the file and not guessed at.
    print(f"  {unplaced} visible label(s) placed by their symbol, "
          f"not checked for overlap")
    print(f"  {len(added)} record(s) added")
    print(f"  wrote {a.out}")
    print(f"\n  prove it:  ./tools/epro.py diff {a.out} {a.netlist}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
