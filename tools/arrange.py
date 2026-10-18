#!/usr/bin/env python3
"""Group an imported EasyEDA Pro schematic into functional blocks.

The netlist importer places every component on a regular grid and gives each
pin its own short wire stub carrying the net name. That is electrically
complete and visually useless: 129 symbols in reading order with no relation
to what they do.

This moves them. It does NOT draw wires, and it does not try to -- see the
note on why below.

**What makes this safe is that it only translates.** Connectivity in an
EasyEDA schematic is geometric: a wire is connected to a pin because its
endpoint coincides with the pin's coordinate. Nothing names the pair. So an
edit that recomputes geometry can silently disconnect a pin while the canvas
still looks wired, which is the one failure this whole workflow exists to
prevent.

A rigid translation cannot do that. Every coordinate inside a cell moves by
the same delta, so every coincidence that held before holds after. The
property is structural, not something to test for -- but it is tested for
anyway, because the premise it rests on (that cells partition the document
cleanly) is an observation about one importer's output and not a guarantee.

Three checks, and the tool refuses to write if any fails:

1. every component sits in its own cell, and no two share one
2. no wire crosses a cell boundary -- if one did, translating its cell would
   stretch it off a pin
3. each component's wire count matches its pin count in the netlist, so the
   cell really does hold that component's wiring and not a neighbour's

After writing, `epro.py diff` must still report the export identical to the
netlist. That is the real proof and it is not optional.

    arrange.py <export.epro2> <netlist.json> -o <out.epro2>
    arrange.py <export.epro2> <netlist.json> --dry-run

Blocks come from the `# ---- Name ----` section comments in the netlist
generator beside the netlist, and from a `blocks:` mapping in design.yaml for
anything the generator builds in a loop. A designator in neither is an error:
guessing where it belongs would put a part in the wrong block silently, and
one wrong part in a block is worse than an honest refusal.

**On drawing wires.** Replacing the 441 named stubs with drawn polylines is a
different problem and a much harder one: it means generating geometry, where
an endpoint off by one unit is an open circuit that renders as a connection.
Grouping is the prerequisite for it either way -- you cannot route between
symbols that are scattered -- so this runs first and on its own.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
import zipfile
from pathlib import Path

SECTION_RE = re.compile(r"^\s*#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
TUPLE_RE = re.compile(r'^\s*\(\s*"([^"]+)"\s*,\s*"(C\d+)"')


# --------------------------------------------------------------------------
# reading


def read_records(path: Path):
    """Return (name, [(header, payload, raw_line)]) for the one .epru inside.

    Several .epru documents in one archive would each need their own grid, and
    nothing here has seen that case, so it is refused rather than guessed at.
    """
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected exactly one .epru, found {len(eprus)}: "
                         f"{eprus}")
    name = eprus[0]
    out, cur = [], None
    for line in z.read(name).decode("utf-8").split("\n"):
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
        # The document each record belongs to. Without it, three separate
        # `doc[0] == "SYMBOL"` tests were comparing against the first
        # character of the raw line and silently matching nothing -- so the
        # star layout's side assignment, which needs symbol pin
        # coordinates, has been returning the (0,0) fallback for every
        # satellite since it was written.
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        out.append((h, b, cur, line))
    return name, out


def read_rail_kinds(yml: Path) -> dict:
    """net -> "ground" or "supply", from design.yaml's `rails:` block."""
    out, inside, cur = {}, False, None
    if not yml.exists():
        return out
    for line in yml.read_text(encoding="utf-8").splitlines():
        if re.match(r"^rails:\s*$", line):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            break
        if not inside:
            continue
        m = re.match(r"^\s{2}(\S[^:]*):\s*$", line)
        if m:
            cur = m.group(1).strip().strip("\"'")
            out[cur] = "supply"
            continue
        if cur and re.match(r"^\s{4}type:\s*ground\s*$", line):
            out[cur] = "ground"
    return out


def read_rails(yml: Path) -> list[str]:
    """The net names declared under `rails:` in design.yaml.

    The same deliberately small reader the block mapping uses: one level of
    keys under one heading, which is all the shape this needs and one less
    dependency than a YAML library.
    """
    if not yml.exists():
        return []
    out, inside = [], False
    for line in yml.read_text(encoding="utf-8").splitlines():
        if re.match(r"^rails:\s*$", line):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            break
        if not inside:
            continue
        m = re.match(r"^\s{2}(\S[^:]*):\s*$", line)
        if m:
            out.append(m.group(1).strip().strip("\"'"))
    return out


def read_blocks(netlist: Path, designators: set[str]) -> dict[str, str]:
    """designator -> block name, from the generator's sections and design.yaml."""
    blocks: dict[str, str] = {}

    gen = netlist.with_name("netlist.py")
    if gen.exists():
        section = None
        for line in gen.read_text(encoding="utf-8").splitlines():
            m = SECTION_RE.match(line)
            if m:
                section = m.group(1)
                continue
            m = TUPLE_RE.match(line)
            if m and section:
                blocks[m.group(1)] = section

    yml = netlist.with_name("design.yaml")
    if yml.exists():
        # A deliberately small reader: `blocks:` holding `Name: [A, B, C]`.
        # Pulling in a YAML dependency to read one mapping is not worth it,
        # and the shape is fixed by this tool's own documentation.
        inside, cur, pending = False, None, None
        for line in yml.read_text(encoding="utf-8").splitlines():
            if re.match(r"^blocks:\s*$", line):
                inside = True
                continue
            if inside and line and not line[0].isspace():
                inside = False
            if not inside:
                continue
            if pending is not None:
                # A flow sequence wrapped across lines. Keep gathering until
                # the brackets balance; a list of sixteen designators does not
                # fit on one line and silently reading half of it would place
                # eight parts and refuse the rest.
                pending += " " + line.strip()
                if pending.count("[") == pending.count("]"):
                    for d in pending[pending.index("[") + 1:
                                     pending.rindex("]")].split(","):
                        d = d.strip().strip("\"'")
                        if d:
                            blocks[d] = cur
                    pending = None
                continue
            m = re.match(r"^\s{2}(\S.*?):\s*(.*)$", line)
            if m:
                cur = m.group(1).strip().strip("\"'")
                rest = m.group(2).strip()
                if rest.startswith("["):
                    pending = rest
                    if pending.count("[") == pending.count("]"):
                        for d in pending[1:pending.rindex("]")].split(","):
                            d = d.strip().strip("\"'")
                            if d:
                                blocks[d] = cur
                        pending = None
                continue
            m = re.match(r"^\s{4,}-\s*(\S+)\s*$", line)
            if m and cur:
                blocks[m.group(1).strip().strip("\"'")] = cur
        if pending is not None:
            raise SystemExit(f"error: unterminated list in {yml.name} "
                             f"under blocks: {cur!r}")

    missing = sorted(designators - set(blocks))
    if missing:
        raise SystemExit(
            f"error: {len(missing)} designator(s) belong to no block: "
            f"{', '.join(missing[:20])}"
            f"{' ...' if len(missing) > 20 else ''}\n"
            f"  add them to a `blocks:` mapping in {yml.name}, or put them "
            f"under a `# ---- Name ----` section in {gen.name}.\n"
            f"  they are not placed anywhere by default: a part silently "
            f"dropped into the wrong block is worse than this message.")
    return blocks


# --------------------------------------------------------------------------
# the grid


class Grid:
    """The importer's placement grid, inferred from the component anchors."""

    def __init__(self, anchors: list[tuple[int, int]]):
        xs = sorted({x for x, _ in anchors})
        ys = sorted({y for _, y in anchors})
        self.w = self._spacing(xs, "x")
        self.h = self._spacing(ys, "y")
        self.x0, self.y0 = xs[0], ys[0]

    @staticmethod
    def _spacing(vals: list[int], axis: str) -> int:
        if len(vals) < 2:
            raise SystemExit(f"error: only one distinct {axis} among the "
                             f"components; cannot infer a grid")
        gaps = {vals[i + 1] - vals[i] for i in range(len(vals) - 1)}
        step = min(gaps)
        off = [g for g in gaps if g % step]
        if off:
            raise SystemExit(
                f"error: {axis} spacings {sorted(gaps)} are not multiples of "
                f"{step}; the placement is not on one grid and translating "
                f"cells would move wires off their pins")
        return step

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor((x - self.x0) / self.w + 0.5),
                math.floor((y - self.y0) / self.h + 0.5))

    def origin(self, col: int, row: int) -> tuple[int, int]:
        return (self.x0 + col * self.w, self.y0 + row * self.h)


# --------------------------------------------------------------------------
# layout


def extents(lines, grid, of_record, owner):
    """Per component: its bounding box relative to its cell origin.

    Packing by anchor would be packing by a point, and the symbols differ by
    an order of magnitude -- U1 is 260 x 210 where a 0402 is 100 x 30. The
    box is what actually has to fit, and it includes the net labels, which on
    a passive are most of its footprint.
    """
    box = {}
    for i, (h, b, _, _) in enumerate(lines):
        c = of_record.get(i)
        d = owner.get(c)
        if d is None:
            continue
        ox, oy = grid.origin(*c)
        for ax, ay in (("x", "y"), ("startX", "startY"), ("endX", "endY")):
            if isinstance(b.get(ax), (int, float)) and isinstance(b.get(ay), (int, float)):
                x, y = b[ax] - ox, b[ay] - oy
                e = box.setdefault(d, [x, y, x, y])
                e[0] = min(e[0], x); e[1] = min(e[1], y)
                e[2] = max(e[2], x); e[3] = max(e[3], y)
    return {d: tuple(v) for d, v in box.items()}


def seriate(members, nets):
    """Order a block's components so connected ones end up adjacent.

    Sorting by designator puts C1, C2, C3 in a row and the parts they
    actually connect to somewhere else, which is what makes a correct
    schematic read as a parts list. This is a greedy chain: start at the
    least-connected component, then repeatedly take whichever unplaced
    neighbour shares the most nets.

    Only nets with at most four pins count as a connection. The four power
    rails carry 209 of this board's 441 pins, and letting GND vote would make
    every component adjacent to every other, which is the same as no
    information at all.

    Ties break on the designator, so the result is deterministic.
    """
    adj = collections.defaultdict(collections.Counter)
    for members_of_net in nets.values():
        inside = [d for d in members_of_net if d in members]
        if not 2 <= len(members_of_net) <= 4 or len(inside) < 2:
            continue
        for a in inside:
            for b in inside:
                if a != b:
                    adj[a][b] += 1

    todo = sorted(members, key=_desig_key)
    if not adj:
        return todo
    start = min(todo, key=lambda d: (len(adj[d]), _desig_key(d)))
    out, left = [start], set(todo) - {start}
    while left:
        cur = out[-1]
        cand = [d for d in left if d in adj[cur]]
        if cand:
            nxt = max(cand, key=lambda d: (adj[cur][d], -_desig_key(d)[1]))
        else:
            # chain exhausted: start a new one at the least-connected leftover
            nxt = min(left, key=lambda d: (len(adj[d] or {}), _desig_key(d)))
        out.append(nxt)
        left.discard(nxt)
    return out


def local_pins(lines, grid_, of_record, owner):
    """designator -> [(symbol-local dx, dy, net)] for every placed pin.

    Derived from the export, not fetched: each pin already carries a stub
    whose endpoint is the pin and whose NET attribute names the net, and the
    component's anchor and rotation turn that back into a symbol-local
    offset. So the star layout needs no library call and no `Pin Number`
    attribute -- which is just as well, since PIN record ids are not unique
    and reading a number from them returns nonsense.
    """
    netof = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    at = {}
    for h, b, _, _ in lines:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                at.setdefault((round(x), round(y)), set()).add(netof[b["lineGroup"]])

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

    rot = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
           180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}
    out = collections.defaultdict(list)
    for h, b, _, _ in lines:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig or desig[h["id"]].endswith("?")):
            continue
        r = (b.get("rotation") or 0) % 360
        if r not in rot:
            continue
        for q in sympins.get(symref.get(h["id"]), []):
            dx, dy = rot[r](q.get("x", 0), q.get("y", 0))
            pt = (round(b["x"] + dx), round(b["y"] + dy))
            for n in at.get(pt, ()):
                out[desig[h["id"]]].append((dx, dy, n))
    return out


def owners(members, nets, pinsof, rails, decl):
    """satellite -> the component it belongs beside, within one band.

    Two populations, two rules, measured rather than assumed:

    - **rail-only parts**, every pin on a declared rail: the owner is the
      non-rail-only component whose pins contain every rail this one
      touches, nearest in declaration order. 31 of this board's 39. Where
      that finds nothing, fall back to sharing *any* rail, which places 7
      more -- C10 and C11 at L1, C13 at L2, R1 at U6, D8 at U10. Strict
      first and loose only as fallback, because the loose rule on its own
      moves 19 components off an answer the strict rule had right.
    - **everything else**: the hub in the band reached over a non-rail net,
      most shared nets first.

    The rail-only rule is what finds C24 beside LED1 through C31 beside
    LED8. A hub model keyed on pin count sends all eight to U9, because U9
    is the only thing in that band with five pins.
    """
    hubs = {d for d in members if len(pinsof.get(d, ())) >= 5}
    ronly = {d for d in members
             if pinsof.get(d) and all(n in rails for n in pinsof[d])}
    out = {}
    for d in sorted(ronly, key=_desig_key):
        want = set(pinsof[d])
        for test in (lambda o: want <= o, lambda o: bool(want & o)):
            cand = [o for o in members
                    if o != d and o not in ronly and test(set(pinsof.get(o, ())))]
            if cand:
                out[d] = min(cand, key=lambda o: (abs(decl[o] - decl[d]), decl[o]))
                break
    for d in sorted(members - ronly - hubs, key=_desig_key):
        shared = collections.Counter()
        for h in hubs:
            for n in set(pinsof.get(d, ())) & set(pinsof.get(h, ())):
                if n not in rails:
                    shared[h] += 1
        if shared:
            out[d] = max(sorted(shared), key=lambda h: (shared[h], -decl[h]))
    return out, hubs


def pack(blocks, order, box, width, gap_x, gap_y, gap_block, nets=None):
    """designator -> (dx, dy) offset for its anchor, packed by real size.

    The *input* grid has to be uniform, because that is what lets a record be
    attributed to exactly one component. The output does not: a rigid
    translation is rigid wherever it lands. So rows here are as tall as their
    tallest member rather than as tall as the biggest component on the board,
    which is the difference between a 200-unit row for a 30-unit resistor and
    a 70-unit one.
    """
    members = collections.defaultdict(list)
    for d, b in blocks.items():
        members[b].append(d)

    # More negative y is HIGHER on the sheet: the title block, which is
    # always at the bottom of a sheet, sits at y = -230 while the components
    # span -1905 to -35. Stepping y downward therefore walks UP the page, so
    # stacking the blocks in netlist order put the USB-C inlet at the bottom
    # and the fan header at the top and the signal flow read backwards.
    # Stacking in reverse is what puts the first block at the top.
    placed, y = {}, 0.0
    for name in reversed(order):
        ds = (seriate(set(members[name]), nets) if nets
              else sorted(members[name], key=_desig_key))
        row, x, row_h = [], 0.0, 0.0
        rows = []
        for d in ds:
            x0, y0, x1, y1 = box[d]
            w = x1 - x0
            if row and x + w > width:
                rows.append((row, row_h))
                row, x, row_h = [], 0.0, 0.0
            row.append((d, x))
            x += w + gap_x
            row_h = max(row_h, y1 - y0)
        if row:
            rows.append((row, row_h))
        for r, h in rows:
            y -= h + gap_y
            for d, rx in r:
                x0, y0, x1, y1 = box[d]
                # Place the box's top-left at (rx, y + h); the anchor offset
                # follows from where the box sits relative to it.
                placed[d] = (rx - x0, (y + h) - y1)
        y -= gap_block
    return placed


def collisions(placed, box, pad=10):
    """Pairs whose boxes overlap once placed.

    This is the check that makes tightening the layout safe, and nothing else
    covers it. `epro.py diff` reads the net *names* declared on each wire, so
    it cannot see two components pushed close enough that a stub endpoint
    lands on a neighbour's pin -- which in a geometric schematic is a new
    connection, created silently, with every declared name unchanged.
    """
    items = []
    for d, (dx, dy) in placed.items():
        x0, y0, x1, y1 = box[d]
        items.append((d, x0 + dx - pad, y0 + dy - pad, x1 + dx + pad, y1 + dy + pad))
    items.sort(key=lambda i: i[1])
    bad = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if b[1] >= a[3]:
                break
            if a[2] < b[4] and b[2] < a[4]:
                bad.append((a[0], b[0]))
    return bad


def _desig_key(d: str):
    m = re.match(r"([A-Za-z]+)(\d+)$", d)
    return (m.group(1), int(m.group(2))) if m else (d, 0)


# --------------------------------------------------------------------------
# the rewrite


def translate(payload: dict, dx: int, dy: int) -> dict:
    """Shift every coordinate in one record. Keys are explicit on purpose.

    A generic "shift anything called x" would also move a font size called
    `x` in some future record type, so the pairs this understands are named.
    """
    for a, b in (("x", "y"), ("startX", "startY"), ("endX", "endY")):
        if isinstance(payload.get(a), (int, float)):
            payload[a] += dx
        if isinstance(payload.get(b), (int, float)):
            payload[b] += dy
    return payload


def canvas_index(lines):
    """Classify every record as a canvas placement or as library data.

    This distinction is the whole correctness of the tool and the first
    version did not make it. An `.epru` mixes two coordinate spaces in one
    stream: instance placements on the sheet, and the symbol and footprint
    *definitions* those instances refer to. A PIN record's x/y is a position
    inside its symbol, shared by every instance of the part.

    Treating "any payload with an x and a y" as canvas geometry translated
    1040 library records along with J1 -- which would have moved pins inside
    their symbols, for every placement of those parts, with the net names all
    still in place and the diff still clean.

    So the rule is a positive allowlist, not an exclusion:

    - COMPONENT, but only one carrying a partId and a designator (the
      importer also leaves one empty template at the origin)
    - LINE, reached through its wire's `lineGroup`
    - ATTR, only when its parentId chain ends at one of those two

    Returns (kind_of_id, cell_source) where cell_source maps a record index
    to the object whose position decides its cell.
    """
    by_id = {}
    for h, b, _, _ in lines:
        if h is not None and h.get("id") is not None:
            by_id[h["id"]] = (h, b)

    desig = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value") and b["value"] != "?"}

    canvas = set()
    for h, b, _, _ in lines:
        if h is None:
            continue
        if h["type"] == "COMPONENT" and b.get("partId") and h["id"] in desig:
            canvas.add(h["id"])
        elif h["type"] == "WIRE":
            canvas.add(h["id"])

    def root(i, depth=0):
        """Follow parentId up to a canvas object, or give up."""
        if depth > 8 or i is None:
            return None
        if i in canvas:
            return i
        rec = by_id.get(i)
        if rec is None:
            return None
        return root(rec[1].get("parentId"), depth + 1)

    return by_id, desig, canvas, root


def assign_cells(lines, grid):
    """Record index -> the cell it belongs to, for canvas records only.

    Library records get None and are never touched. A wire takes the cell its
    segments lie in; an attribute takes the cell of whatever its parentId
    chain roots at.
    """
    by_id, desig, canvas, root = canvas_index(lines)

    comp_cell, owner = {}, {}
    clash = []
    for h, b, _, _ in lines:
        if h is None or h["type"] != "COMPONENT" or h["id"] not in canvas:
            continue
        c = grid.cell(b["x"], b["y"])
        d = desig[h["id"]]
        if c in owner:
            clash.append((c, owner[c], d))
        comp_cell[h["id"]], owner[c] = c, d

    wire_cells = collections.defaultdict(set)
    for h, b, _, _ in lines:
        if h is not None and h["type"] == "LINE" and b.get("lineGroup"):
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                wire_cells[b["lineGroup"]].add(grid.cell(x, y))

    of_record = {}
    for i, (h, b, _, _) in enumerate(lines):
        if h is None:
            continue
        if h["type"] == "LINE" and b.get("lineGroup") in wire_cells:
            cs = wire_cells[b["lineGroup"]]
            of_record[i] = next(iter(cs)) if len(cs) == 1 else None
            continue
        r = root(h["id"]) if h["type"] in ("COMPONENT", "WIRE") else root(b.get("parentId"))
        if r is None:
            continue
        if r in comp_cell:
            of_record[i] = comp_cell[r]
        elif r in wire_cells and len(wire_cells[r]) == 1:
            of_record[i] = next(iter(wire_cells[r]))
    return of_record, owner, comp_cell, wire_cells, desig, clash


def anchors_of(lines, of_record, owner):
    """designator -> the x/y of its COMPONENT record in these lines."""
    out = {}
    for i, (h, b, _, _) in enumerate(lines):
        if h is not None and h["type"] == "COMPONENT":
            d = owner.get(of_record.get(i))
            if d is not None and isinstance(b.get("x"), (int, float)):
                out[d] = (b["x"], b["y"])
    return out


def anchor_geometry(lines, of_record, owner):
    """Per component: every coordinate it owns, relative to its own anchor.

    Relative to the *anchor*, not to a grid cell, because the output is
    deliberately not on a grid: rows are packed to the height of their
    tallest member. An earlier version re-derived a grid from the written
    file to fingerprint it, which reported all 129 components as drifted the
    moment the layout stopped being uniform -- a false alarm, but one that
    would have been indistinguishable from the real thing.

    This is the invariant the net diff cannot check. Move a symbol and leave
    its wire behind and every NET attribute is still in the file, still
    naming the right net, while the circuit is open.
    """
    anchors = anchors_of(lines, of_record, owner)
    out = collections.defaultdict(list)
    for i, (h, b, _, _) in enumerate(lines):
        d = owner.get(of_record.get(i))
        if d is None or d not in anchors:
            continue
        ax0, ay0 = anchors[d]
        for ax, ay in (("x", "y"), ("startX", "startY"), ("endX", "endY")):
            if isinstance(b.get(ax), (int, float)) and isinstance(b.get(ay), (int, float)):
                out[d].append((h["type"], ax, b[ax] - ax0, b[ay] - ay0))
    return {d: sorted(v) for d, v in out.items()}


def reread(path, lines, of_record, owner):
    """Fingerprint a written archive, reusing the input's attribution.

    The rewrite preserves line order and count exactly, so record i in the
    output is record i in the input. Attributing the output independently
    would mean inferring a grid from a file that no longer has one.
    """
    _, back = read_records(path)
    if len(back) != len(lines):
        raise SystemExit(f"error: {path.name} has {len(back)} lines, input had "
                         f"{len(lines)} — the rewrite did not preserve order")
    for (h1, _, _, _), (h2, _, _, _) in zip(lines, back):
        if (h1 is None) != (h2 is None) or (
                h1 is not None and (h1.get("id"), h1.get("type")) !=
                (h2.get("id"), h2.get("type"))):
            raise SystemExit(f"error: {path.name} record sequence differs from "
                             f"the input — refusing to compare")
    return anchor_geometry(back, of_record, owner)


# About 10 mm. What reads as one wire is a property of the reader, not of
# the --gap the tool happened to be run with, so the threshold is absolute.
# Scaling it with the gap made the count zero at every setting, which is
# the shape of a check that cannot fail.
COLINEAR_LIMIT = 40

ROT = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}


def _symbol_boxes(lines):
    """Every placed component's outline, in canvas coordinates.

    `collinear_stubs` calls its pairs "foreign" but had no way to tell:
    it compared net names and nothing else. So an upright decoupling
    capacitor -- supply stub above, ground stub below, its own body in
    between -- counted as two wires misreading as one, which is both
    wrong and the single most common shape on this sheet.
    """
    desig = {b["parentId"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in lines
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    prims = collections.defaultdict(list)
    for h, b, doc, _ in lines:
        if h and doc and doc[0] == "SYMBOL" and h["type"] in ("RECT", "POLY",
                                                              "ARC"):
            prims[doc[1]].append((h["type"], b))
    out = []
    for h, b, doc, _ in lines:
        if not h or h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        if h["id"] not in desig:          # a net flag, which is not a body
            continue
        pts = []
        for kind, g in prims.get(symref.get(h["id"]), []):
            if kind == "RECT":
                pts += [(g["dotX1"], g["dotY1"]), (g["dotX2"], g["dotY2"])]
            elif kind == "ARC":
                # An inductor is four arcs and nothing else -- no RECT and
                # no POLY -- so L2 had no body at all: invisible to the
                # router, and not a separating body for the colinear
                # check. Start, refer and end bound the arc closely enough.
                pts += [(g["startX"], g["startY"]), (g["referX"], g["referY"]),
                        (g["endX"], g["endY"])]
            elif g.get("points"):
                pts += [(q["x"], q["y"]) for q in g["points"]]
        if not pts:
            continue
        r = (b.get("rotation") or 0) % 360
        m = bool(b.get("isMirror"))
        tp = [ROT[r](-x if m else x, y) for x, y in pts]
        out.append((b["x"] + min(x for x, _ in tp),
                    b["y"] + min(y for _, y in tp),
                    b["x"] + max(x for x, _ in tp),
                    b["y"] + max(y for _, y in tp)))
    return out


def stubs_through_bodies(lines):
    """Stubs that end up inside another component's symbol.

    Placement cannot prevent this on its own: `extents` includes the stubs
    in each box, so non-overlapping boxes keep them clear -- and then
    `--upright` turns a part's two stubs from horizontal to vertical, after
    the boxes were measured. The same shape of mistake as measuring the
    colinear count before the rotation that changes it.
    """
    boxes = {}
    desig = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    for (x0, y0, x1, y1), d in zip(_symbol_boxes(lines),
                                   _symbol_box_owners(lines)):
        boxes[d] = (x0, y0, x1, y1)
    # Which pins belong to whom, so a part's own stub crossing its own
    # body is not reported. The first version did report it -- two on J4,
    # whose symbol draws its pins inward -- which is the same mistake the
    # colinear check made: claiming "another" and never testing it.
    own = collections.defaultdict(set)
    symref = {b["parentId"]: b["value"] for h, b, _, _ in lines
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    sympins = collections.defaultdict(list)
    for h, b, doc, _ in lines:
        if h and doc and doc[0] == "SYMBOL" and h["type"] == "PIN":
            sympins[doc[1]].append((b.get("x", 0), b.get("y", 0)))
    for h, b, _, _ in lines:
        if not h or h["type"] != "COMPONENT" or h["id"] not in desig:
            continue
        r = (b.get("rotation") or 0) % 360
        m = bool(b.get("isMirror"))
        for px, py in sympins.get(symref.get(h["id"]), ()):
            dx, dy = ROT[r](-px if m else px, py)
            own[desig[h["id"]]].add((round(b["x"] + dx), round(b["y"] + dy)))

    out = []
    for h, b, _, _ in lines:
        if not h or h["type"] != "LINE" or not b.get("lineGroup"):
            continue
        sx, sy = round(b["startX"]), round(b["startY"])
        ex, ey = round(b["endX"]), round(b["endY"])
        for d, (x0, y0, x1, y1) in boxes.items():
            if (sx, sy) in own[d] or (ex, ey) in own[d]:
                continue                 # its own pin, not a foreign body
            if sy == ey:
                hit = y0 < sy < y1 and min(sx, ex) < x1 and x0 < max(sx, ex)
            elif sx == ex:
                hit = x0 < sx < x1 and min(sy, ey) < y1 and y0 < max(sy, ey)
            else:
                hit = False
            if hit:
                out.append((d, (sx, sy, ex, ey)))
    return out


def _symbol_box_owners(lines):
    """The designators `_symbol_boxes` returns boxes for, in the same order."""
    desig = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in lines
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    prims = collections.defaultdict(list)
    for h, b, doc, _ in lines:
        if h and doc and doc[0] == "SYMBOL" and h["type"] in ("RECT", "POLY",
                                                              "ARC"):
            prims[doc[1]].append((h["type"], b))
    out = []
    for h, b, _, _ in lines:
        if not h or h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        if h["id"] not in desig:
            continue
        pts = []
        for kind, g in prims.get(symref.get(h["id"]), []):
            if kind == "RECT":
                pts += [(g["dotX1"], g["dotY1"])]
            elif kind == "ARC":
                pts += [(g["startX"], g["startY"])]
            elif g.get("points"):
                pts += [(q["x"], q["y"]) for q in g["points"]]
        if not pts:
            continue
        out.append(desig[h["id"]])
    return out


def collinear_stubs(lines, limit=COLINEAR_LIMIT):
    """Pairs of stubs on different nets that lie on one line, close enough
    to read as a single wire.

    This is the most *misleading* thing a layout can produce, as opposed
    to merely the ugliest: two 5 mm-apart colinear green segments are one
    wire to the eye, and the only thing saying otherwise is 2 pt text.
    Nothing else here looks for it -- `collisions()` compares component
    boxes and a stub lives outside every box.
    """
    netof = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    segs = [(round(b["startX"]), round(b["startY"]), round(b["endX"]),
             round(b["endY"]), netof[b["lineGroup"]])
            for h, b, _, _ in lines
            if h and h["type"] == "LINE" and b.get("lineGroup") in netof]
    rows_, colsd = collections.defaultdict(list), collections.defaultdict(list)
    for s in segs:
        (rows_ if s[1] == s[3] else colsd)[s[1] if s[1] == s[3] else s[0]].append(s)
    boxes = _symbol_boxes(lines)

    def blocked(horiz, fixed, lo, hi):
        """Is there a component body in the gap between the two stubs?"""
        for x0, y0, x1, y1 in boxes:
            if horiz:
                if y0 <= fixed <= y1 and x0 < hi and x1 > lo:
                    return True
            elif x0 <= fixed <= x1 and y0 < hi and y1 > lo:
                return True
        return False

    bad = 0
    for bucket in (rows_, colsd):
        for group in bucket.values():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if a[4] == b[4]:
                        continue
                    horiz = a[1] == a[3]
                    if horiz:
                        g = max(min(b[0], b[2]) - max(a[0], a[2]),
                                min(a[0], a[2]) - max(b[0], b[2]))
                        lo = min(max(a[0], a[2]), max(b[0], b[2]))
                        hi = max(min(a[0], a[2]), min(b[0], b[2]))
                        fixed = a[1]
                    else:
                        g = max(min(b[1], b[3]) - max(a[1], a[3]),
                                min(a[1], a[3]) - max(b[1], b[3]))
                        lo = min(max(a[1], a[3]), max(b[1], b[3]))
                        hi = max(min(a[1], a[3]), min(b[1], b[3]))
                        fixed = a[0]
                    if g > limit:
                        continue
                    # A body in the gap is what makes the pair readable:
                    # an upright decoupling capacitor is a supply stub
                    # above and a ground stub below with its own symbol
                    # between them, and nobody reads that as one wire.
                    if blocked(horiz, fixed, min(lo, hi), max(lo, hi)):
                        continue
                    bad += 1
    return bad


def relax(placed, box, gap, rounds=200):
    """Push overlapping components apart, in the space `collisions()` reads.

    Deliberately not a second rectangle calculation. Three attempts at an
    in-band resolver each used a different reading of what the placement
    dict holds -- corner, anchor, top edge -- and each left pairs behind
    for `collisions()` to catch. The fix is to have one definition: resolve
    here, on the final offsets, with the same `x0 + dx` arithmetic the
    check uses, so the two cannot disagree.

    Always pushes the lower of a pair further down, so it terminates.
    """
    for _ in range(rounds):
        bad = collisions(placed, box, pad=gap // 2)
        if not bad:
            return placed
        for a, b in bad:
            ax0, ay0, ax1, ay1 = box[a]
            bx0, by0, bx1, by1 = box[b]
            adx, ady = placed[a]
            bdx, bdy = placed[b]
            # whichever sits lower gets pushed below the other
            lo, hi = (b, a) if (bdy + by1) < (ady + ay1) else (a, b)
            hx0, hy0, hx1, hy1 = box[hi]
            lx0, ly0, lx1, ly1 = box[lo]
            top = placed[hi][1] + hy0
            placed[lo] = (placed[lo][0], top - gap - (ly1 - ly0) - ly0)
    return placed


def fanout_metrics(blocks, nets, pinsof, rails, decl, pos):
    """How far each part sits from its nets, and how often the links cross.

    The README used to quote these as a before/after pair measured by hand
    in a throwaway script, so when the layout changed they were simply
    wrong and nothing could recompute them. They belong in the tool.

    The star model, which is topology-free: every net gets the centroid of
    the parts on it, and every part a spoke to that centroid. Picking a
    chain or a tree instead would make the number depend on an ordering
    nobody chose. Rails are left out -- GND touches 117 parts and its
    centroid means nothing, which is also why they get flag symbols rather
    than wires.
    """
    skip = set(rails)
    onnet = collections.defaultdict(list)
    for d, ns in pinsof.items():
        if d not in pos:
            continue
        for n in set(ns):
            if n not in skip and not n.upper().startswith("NC_"):
                onnet[n].append(d)
    links = []
    for n, ds in onnet.items():
        if len(ds) < 2:
            continue
        cx = sum(pos[d][0] for d in ds) / len(ds)
        cy = sum(pos[d][1] for d in ds) / len(ds)
        links += [(pos[d], (cx, cy)) for d in ds]
    dist = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in links)

    def side(o, a, b):
        return ((b[0] - a[0]) * (o[1] - a[1]) - (b[1] - a[1]) * (o[0] - a[0]))

    cross = 0
    for i, (p1, p2) in enumerate(links):
        for q1, q2 in links[i + 1:]:
            d1, d2 = side(q1, p1, p2), side(q2, p1, p2)
            d3, d4 = side(p1, q1, q2), side(p2, q1, q2)
            if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
                cross += 1
    return int(dist), cross


def flow_order(order, blocks, nets, kinds):
    """Re-order the bands by how far they are from the board's input.

    The star layout minimises distance to a net's centroid and knows
    nothing about direction, which a reviewer put as: the Ethernet
    controller sits *above* the MCU rather than beside it, and the eFuse
    ICs sit below their own output network, so the power runs up the
    sheet. Neither is wrong by the objective; the objective has no
    notion of a signal going somewhere.

    Direction that can be derived rather than declared: breadth-first
    from the block holding the lowest-numbered connector, across nets two
    blocks share. Rails are excluded -- GND touches everything and would
    make every block one hop from every other. Depth is then "how far
    downstream", and bands sort by it.

    Ties keep declaration order, which is what keeps the two eFuse
    channels adjacent: they are the same distance from the input and
    nothing else distinguishes them.
    """
    members = collections.defaultdict(set)
    for d, b in blocks.items():
        members[b].add(d)
    # Only the grounds. GND touches 117 parts and would put every block
    # one hop from every other -- but excluding the *supply* rails too
    # drops both switching regulators out of the graph entirely, because
    # VBUS, BUS_5V and BUS_3V3 are the only nets they share with
    # anything. They then sort as unreachable and land at the far end of
    # the sheet, which is the opposite of where the conversion belongs.
    skip = {n for n, kind in kinds.items() if kind == "ground"}
    netblocks = collections.defaultdict(set)
    for d, ns in nets.items():
        b = blocks.get(d)
        if b is None:
            continue
        for n in ns:
            if n not in skip and not n.upper().startswith("NC_"):
                netblocks[n].add(b)
    adj = collections.defaultdict(set)
    for bs in netblocks.values():
        for x in bs:
            adj[x] |= bs - {x}

    def conn_key(d):
        m = re.match(r"^([A-Za-z]+)(\d+)$", d)
        return (m.group(1), int(m.group(2))) if m else (d, 0)

    conns = sorted((d for d in blocks if d[:1].upper() == "J"), key=conn_key)
    src = blocks[conns[0]] if conns else (order[0] if order else None)
    if src is None:
        return order

    depth = {src: 0}
    queue = collections.deque([src])
    while queue:
        cur = queue.popleft()
        for nxt in sorted(adj[cur]):
            if nxt not in depth:
                depth[nxt] = depth[cur] + 1
                queue.append(nxt)
    # A block the input cannot reach goes last, in declaration order --
    # saying "depth 0" for it would put it beside the connector.
    far = max(depth.values(), default=0) + 1
    out = sorted(order, key=lambda b: (depth.get(b, far), order.index(b)))
    return out, {b: depth.get(b, far) for b in out}


def star_pack(blocks, order, box, nets, pinsof, localpins, rails, decl,
              gap_x, gap_y, gap_block, columns=1, max_row=1200, stagger=0,
              depths=None):
    """Each band as its hubs with satellites in a row above and below.

    The first attempt hung satellites down one side at their pin's y and
    produced a single column 1,367 mm tall -- a star in the data and a list
    on the page. Rendering it was what showed that; the numbers had said it
    was fine.

    So: the hub sits on a middle line, and each satellite goes into the row
    above or below depending on which half of the hub its connecting pin is
    on, at that pin's x. Three rows per band instead of one column, the
    satellite still over the pin that reaches it, and the height bounded by
    the row count rather than by the tallest hub's pin span.
    """
    members = collections.defaultdict(set)
    for d, b in blocks.items():
        members[b].add(d)

    # Lay the bands out once to learn their sizes, then deal them into
    # columns. One column leaves the right half of the sheet empty and makes
    # a 610 x 932 mm ribbon; the bands are narrow and there is nothing else
    # to put beside them.
    laid, sizes = {}, {}
    for name in order:
        laid[name] = _band(name, members[name], box, nets, pinsof, localpins,
                           rails, decl, gap_x, gap_y, max_row, stagger)
        xs = [bx for bx, _ in laid[name].values()]
        ys = [by for _, by in laid[name].values()]
        sizes[name] = (max(xs[i] + (box[d][2] - box[d][0])
                           for i, d in enumerate(laid[name])) - min(xs),
                       max(by + (box[d][3] - box[d][1])
                           for d, (_, by) in laid[name].items()) - min(ys))

    # Split the band order into contiguous runs, one per column, at the
    # cut points that balance height best. Dealing greedily to the
    # shortest column balances better but shuffles the order, and it put
    # the two eFuse channels -- the same circuit twice -- in different
    # columns at different heights, 430 mm apart. Comparing them side by
    # side is the first check anyone makes on a two-channel board.
    if columns == 0:
        # Shelf packing. Columns give every band the width of the widest
        # one in its column, so a band of two parts takes as much room as
        # a band of twenty -- on one sheet that was R48 and D14 alone
        # setting a third of the width. Here each band takes its own
        # width, and a shelf wraps when the budget is used up.
        #
        # Order is preserved, which is the property the column dealer was
        # built for: consecutive bands land side by side or on the next
        # shelf, so the two eFuse channels stay comparable. Dealing
        # greedily by size would not.
        #
        # The budget is searched rather than asked for, and the score is
        # area weighted by how far the result is from a sheet's shape.
        # Minimising area alone makes ribbons -- one sheet came out
        # 699 x 133 mm, which packs tightly and fits no paper. Paper is
        # root-two, so that is what the search aims at.
        placed = {}
        best_area, best = None, None
        wtotal = sum(sizes[n][0] + gap_block for n in order)
        for frac in (0.25, 0.33, 0.4, 0.5, 0.6, 0.75, 1.0):
            budget = wtotal * frac
            shelves, cur_, cw = [], [], 0.0
            for i_, name in enumerate(order):
                w_ = sizes[name][0] + gap_block
                # Not between two bands the flow put at the same depth.
                # Channel 1 and Channel 2 eFuse are equally far from the
                # input and nothing else separates them; a wrap between
                # them put one at the end of a shelf and the other at the
                # start of the next, 472 mm apart diagonally. Comparing
                # the two channels side by side is the first check anyone
                # makes on a two-channel board, and keeping them that way
                # is what the column model this replaced existed for.
                # A preference, not a veto. Forbidding the break outright
                # chained every band sharing a depth onto one shelf and
                # made the sheet 1795 x 199 mm -- most blocks here are the
                # same distance from the input, so "same depth" is a weak
                # signal on its own. Half a shelf of overflow is what it
                # is worth.
                # Capping the run at two bands instead -- to force
                # Channel 1 and Channel 2 onto one shelf, which three
                # reviewers have asked for -- was tried and measured
                # worse on every number: 388 mm apart becomes 535,
                # crossings 114 to 138, spoke length 60,540 to 80,151.
                # The width search dominates shelf composition, so a
                # local gluing rule moves everything else. Left as the
                # overflow allowance it was.
                glued = (depths is not None and i_ > 0
                         and depths.get(name) == depths.get(order[i_ - 1])
                         and cw + w_ <= budget * 1.5)
                if cur_ and cw + w_ > budget and not glued:
                    shelves.append(cur_)
                    cur_, cw = [], 0.0
                cur_.append(name)
                cw += w_
            if cur_:
                shelves.append(cur_)

            # First-fitting a later band into an earlier shelf's leftover
            # width was tried here and removed. A reviewer priced it as
            # the largest remaining lever -- the hole beside the USB-C
            # band is about 290 x 230 mm -- and on this board it fires
            # zero times: it needs a shelf holding a single band, and the
            # budget search never produces one. The arrangement is not
            # what sets the sheet extent either; the flags and the labels
            # around it are, so the 200 units the placement did save did
            # not reach the page. Left out rather than left in doing
            # nothing.

            pos, y_cur, wmax = {}, 0.0, 0.0
            # Reversed, like `pack()` and the column path. Both already
            # carry the comment: stacking in netlist order put the USB-C
            # inlet at the bottom and the flow read backwards. The shelf
            # path was written without it and reproduced exactly that --
            # shelf 0 at the foot of the sheet, read upwards.
            for sh in reversed(shelves):
                x_cur = 0.0
                for name in sh:
                    pos[name] = (x_cur, y_cur)
                    x_cur += sizes[name][0] + gap_block
                wmax = max(wmax, x_cur - gap_block)
                y_cur += max(sizes[n][1] for n in sh) + gap_block
            hgt = y_cur - gap_block
            if wmax <= 0 or hgt <= 0:
                continue
            ratio = max(wmax, hgt) / min(wmax, hgt)
            score = wmax * hgt * (1 + abs(math.log(ratio / 2 ** 0.5)))
            if best_area is None or score < best_area:
                best_area, best = score, pos
        for name, (ox, oy) in best.items():
            band = laid[name]
            lo = min(by for _, by in band.values())
            hi = max(by + (box[d][3] - box[d][1])
                     for d, (_, by) in band.items())
            bx0 = min(bx for bx, _ in band.values())
            for d, (bx, by) in band.items():
                x0, y0, x1, y1 = box[d]
                placed[d] = (ox + (bx - bx0) - x0,
                             -(oy + (hi - by)) - y1)
        return placed

    ncol = max(1, columns)
    tall = [sizes[n][1] + gap_block for n in order]
    total = sum(tall)
    cols, acc, cur = [], 0.0, []
    for i, name in enumerate(order):
        cur.append(name)
        acc += tall[i]
        remaining = ncol - len(cols) - 1
        if remaining > 0 and acc >= (total - sum(tall[:i + 1]) + acc) / (remaining + 1):
            cols.append(cur)
            cur, acc = [], 0.0
    cols.append(cur)
    while len(cols) < ncol:
        cols.append([])
    colx, x_cursor = {}, 0.0
    for i, col in enumerate(cols):
        for name in col:
            colx[name] = x_cursor
        x_cursor += max((sizes[n][0] for n in col), default=0) + 3 * gap_block

    placed = {}
    for col in cols:
        y_cursor = 0.0
        for name in reversed(col):
            band = laid[name]
            lo = min(by for _, by in band.values())
            hi = max(by + (box[d][3] - box[d][1]) for d, (_, by) in band.items())
            bx0 = min(bx for bx, _ in band.values())
            for d, (bx, by) in band.items():
                x0, y0, x1, y1 = box[d]
                placed[d] = (colx[name] + (bx - bx0) - x0,
                             -(y_cursor + (hi - by)) - y1)
            y_cursor += (hi - lo) + gap_block
    return placed


def _series_order(row, pinsof, rails):
    """Put parts that share a signal net next to each other in a row.

    The star objective places every satellite under the hub pin that
    reaches it, independently of its neighbours, so a divider comes out
    as separate resistors joined only by a name. R7 and R11 on this board
    ended up in the gap between the two eFuse channels, under the wrong
    heading, because nothing said they belong beside R5, R9 and R13.

    An edge is any shared net that is not a rail. The earlier attempt
    required the net to touch exactly two components and found no edges
    at all: CH1_UVLO touches U10, R5 *and* R7. The looser rule is the
    right one -- what matters is that two parts in the same row share a
    signal, not how many others do.
    """
    if len(row) < 2:
        return row
    names = [s for _, s in row]
    idx = {s: i for i, s in enumerate(names)}
    sig = {s: {n for n in pinsof.get(s, ())
               if n not in rails and not n.upper().startswith("NC_")}
           for s in names}
    adj = collections.defaultdict(set)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if sig[a] & sig[b]:
                adj[a].add(b)
                adj[b].add(a)
    if not adj:
        return row
    out, seen = [], set()
    for s in names:
        if s in seen:
            continue
        run, stack = [], [s]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            run.append(cur)
            stack += sorted(adj.get(cur, ()), key=_desig_key, reverse=True)
        out += run
    return [row[idx[s]] for s in out]


def _band(name, mem, box, nets, pinsof, localpins, rails, decl, gap_x, gap_y,
          maxw=1200, stagger=0):
    """One band's internal arrangement, in its own coordinates."""
    if True:
        own, hubs = owners(mem, nets, pinsof, rails, decl)
        band, x = {}, 0.0

        def w(d):
            return box[d][2] - box[d][0]

        def h(d):
            return box[d][3] - box[d][1]

        rowh = max((h(d) for d in mem), default=30)
        # Two parts side by side closer than the colinear limit put their
        # facing stubs on one line with a sub-limit gap, by construction:
        # that is 20 of the 30 remaining pairs, 14 of them the LED chain.
        # So the horizontal pitch has a floor the --gap knob cannot go
        # under, while the vertical gap stays whatever was asked for.
        pitch = max(gap_x, COLINEAR_LIMIT + 1)
        for hub in sorted(hubs, key=_desig_key):
            up, down = [], []
            for sat, o in own.items():
                if o != hub:
                    continue
                shared = set(pinsof.get(sat, ())) & set(pinsof.get(hub, ()))
                cands = [(dx, dy) for dx, dy, n in localpins.get(hub, ())
                         if n in shared]
                dx, dy = max(cands, key=lambda c: (abs(c[1]), c[0])) if cands else (0, 0)
                (up if dy > 0 else down).append((dx, sat))
            # A hub's pins often all sit on one half -- the SY8089's five
            # are, so the dy split sent every satellite up and the row ran
            # off to the right of a symbol a fifth its width. Balance the
            # two rows by count once the pin has had its say, and centre
            # each row on the hub so the cluster stays a cluster.
            if not down and len(up) > 2:
                up.sort(key=lambda t: (t[0], _desig_key(t[1])))
                down = up[len(up) // 2:]
                up = up[:len(up) // 2]
            elif not up and len(down) > 2:
                down.sort(key=lambda t: (t[0], _desig_key(t[1])))
                up = down[len(down) // 2:]
                down = down[:len(down) // 2]

            hw = w(hub)
            rows = {}
            for row, sign in ((up, 1), (down, -1)):
                # Ordering a row by series-chain adjacency was tried
                # here and removed: it finds zero chained parts in every
                # row on this board, and the reason is structural rather
                # than a tuning miss. Satellites connect to their *hub*,
                # not to each other -- where two parts really are in
                # series, one of them owns the other and they never land
                # in the same row. A divider reads as three unrelated
                # resistors because the star model has no relation that
                # says otherwise, and the fix is a different model, not a
                # different sort key.
                order_ = _series_order(
                    sorted(row, key=lambda t: (t[0], _desig_key(t[1]))),
                    pinsof, rails)
                width = sum(w(s) for _, s in order_) + pitch * max(len(order_) - 1, 0)
                rows[sign] = (order_, width)
            span = max([hw] + [min(v[1], maxw) for v in rows.values()])
            base = x
            band[hub] = (base + (span - hw) / 2, 0.0)
            for sign, (order_, width) in rows.items():
                # Wrap a long row rather than let it set the sheet width.
                # The status chain is one hub and sixteen satellites, and
                # in a single row it made the sheet 1,026 mm wide on its
                # own -- every other band is a third of that.
                per = max(1, int(maxw // (max((w(s) for _, s in order_),
                                              default=1) + pitch)))
                for k in range(0, len(order_), per):
                    chunk = order_[k:k + per]
                    cw = sum(w(s) for _, s in chunk) + pitch * (len(chunk) - 1)
                    cx = base + (span - cw) / 2
                    lvl = k // per
                    for k2, (_, sat) in enumerate(chunk):
                        # Stagger alternate neighbours vertically. Two
                        # horizontal stubs at the same y, a gap apart, read
                        # as one wire -- that is what forced --gap 60 and
                        # cost the A1 sheet. Half a row of offset breaks
                        # the alignment for a fraction of the space.
                        off = -(stagger if k2 % 2 else 0) * (1 if sign > 0 else -1)
                        band[sat] = (cx, sign * (rowh + gap_y) * (lvl + 1) + off)
                        cx += w(sat) + pitch
            x = base + span + 2 * gap_x

        # anything with no owner lines up on the middle row after the hubs
        # Only the genuinely ownerless. A part whose owner is itself in
        # this row has to wait for the stacking pass below, or it is placed
        # here first and the pass then skips it -- which is what left the
        # eight LED capacitors in a row beside their LEDs instead of under
        # them.
        # Wrapped, like the satellite rows. Unwrapped it was the thing
        # setting the sheet width: the eight-LED chain lives here, and at
        # the pitch floor that is one 700-unit row no --max-row reached.
        rest = sorted(mem - set(band) - set(own), key=_desig_key)
        rx, lvl, widest = x, 0, x
        for d in rest:
            if rx > x and rx - x + w(d) > maxw:
                rx, lvl = x, lvl + 1
            band[d] = (rx, -lvl * (rowh + gap_y))
            rx += w(d) + pitch
            widest = max(widest, rx)
        x = widest

        # Runs AFTER the leftover row, not before: LED1-LED8 reach U9 only
        # through R33, so they share no net with it and get no owner, which
        # put them in the leftover row -- and their capacitors looked for
        # them before they were placed, found nothing, and ended up in the
        # same row instead of under them.
        #
        # A satellite of a non-hub owner goes BELOW it, not beside it.
        # Beside doubled the status chain's width: eight LEDs in a row with
        # eight capacitors next to them made that one band 2,280 units,
        # twice the next widest and the thing setting the sheet size. Under
        # its LED is also where a per-device decoupling capacitor belongs.
        stacked = collections.Counter()
        for sat, o in sorted(own.items()):
            if o in hubs or o not in band or sat in band:
                continue
            ox, oy = band[o]
            stacked[o] += 1
            # Alternating, not centred. Centred on the owner puts a
            # whole stack on one x: C11, C10 and L1 under the buck, a
            # ground symbol between each pair, which reads as a series
            # chain from the rail down to SW_5V. Half a stagger each
            # way makes them three columns instead of one line.
            side = stagger / 2 if stacked[o] % 2 else -stagger / 2
            band[sat] = (ox + (w(o) - w(sat)) / 2 + side,
                         oy - stacked[o] * (rowh + gap_y))

        # spread within a row, in x only, so the band stays three rows tall
        byrow = collections.defaultdict(list)
        for d, (bx, by) in band.items():
            byrow[round(by)].append(d)
        # Alternate rows start half a stagger in. The in-row stagger
        # breaks up a row; it does nothing about the columns that
        # stacked rows form, and three satellites of one hub on one x --
        # C11, C10 and L1 in the buck, a ground symbol between each
        # pair -- read as a series chain from the rail to SW_5V.
        for ri, row in enumerate(sorted(byrow, key=lambda k: -k)):
            row = byrow[row]
            cx = (ri % 2) * (stagger / 2) or None
            for d in sorted(row, key=lambda d: (band[d][0], _desig_key(d))):
                bx, by = band[d]
                if cx is not None and bx < cx:
                    bx = cx
                band[d] = (bx, by)
                cx = bx + w(d) + pitch

    return band


def upright(lines, design, rails, wide=False):
    """Which parts to stand up, and which way round, measured per part.

    The first version keyed on the netlist's pin *number* -- 270 when the
    ground is on pin 2 -- having checked that on R9, where symbol-local
    (+20, 0) is pin 1. That mapping is not constant: on R2 the (+20, 0)
    offset carries GND. So 29 of 30 parts came out with the ground pin at
    the top, which is worse than leaving them horizontal, and the README
    claimed the opposite.

    The rule that does not depend on it: find which symbol-local offset
    actually carries the ground net, by reading the stub at that pin, and
    pick the rotation that sends that offset downward. More positive y is
    down -- the title block, always at the bottom of a sheet, sits at
    y = -230 while the components span -1905 to -35.
    """
    ground = {n for n, spec in rails.items() if spec == "ground"}
    supply = set(rails) - ground
    pins = {c["props"]["Designator"]: c["pins"] for c in design.values()}
    kinds = {c["props"]["Designator"]: (c["props"].get("device_name") or "")
             for c in design.values()}

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
    netof = {b["parentId"]: b["value"] for h, b, _, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    at = {}
    for h, b, _, _ in lines:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                at.setdefault((round(x), round(y)), set()).add(netof[b["lineGroup"]])
    R = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
         180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}

    want = {}
    for h, b, _, _ in lines:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig):
            continue
        d = desig[h["id"]]
        live = {k: v for k, v in pins.get(d, {}).items()
                if not v.upper().startswith("NC_")}
        # Two pins, or more if --upright-wide is asked for. A four-pin
        # LED in a chain is as much a rail-to-ground part as a capacitor
        # is -- VDD above, GND below, data in and out across -- and
        # leaving it flat is what makes an eight-LED chain run the width
        # of the sheet.
        if len(live) != 2 and not (wide and len(live) > 2):
            continue
        if any(kinds[d].upper().startswith(x) for x in ("D", "SS", "SM", "MBR",
                                                        "BZT", "RB7", "H5V")):
            continue                      # polarity is not a net role
        # Three patterns, one rule. A two-pin part with exactly one rail
        # pin is oriented so that pin points at its rail: ground down,
        # supply up. That covers the decoupler (supply + ground), the
        # pull-up and the pull-down (rail + signal) -- 86 of this board's
        # 106 two-pin parts -- and leaves the series element (signal +
        # signal) horizontal, which is where it belongs.
        vals = set(live.values())
        nrail = len(vals & ground) + len(vals & supply)
        if nrail != 1 and not (len(vals & ground) == 1
                               and len(vals & supply) == 1):
            continue
        cur = (b.get("rotation") or 0) % 360
        # Which symbol-local offset carries which kind of net. Read, not
        # inferred from the pin number -- that mapping is not constant,
        # and assuming it stood 29 of 30 parts on their head.
        gloc = sloc = None
        for q in sympins.get(symref.get(h["id"]), []):
            dx, dy = R[cur](q.get("x", 0), q.get("y", 0))
            nets = at.get((round(b["x"] + dx), round(b["y"] + dy)), set())
            if nets & ground:
                gloc = (q.get("x", 0), q.get("y", 0))
            elif nets & supply:
                sloc = (q.get("x", 0), q.get("y", 0))
        # Ground wins when a part has both: down is the stronger
        # convention, and a decoupler with its ground at the bottom has
        # its supply at the top by construction.
        if gloc not in (None, (0, 0)):
            loc, below = gloc, True
        elif sloc not in (None, (0, 0)):
            loc, below = sloc, False
        else:
            continue
        for r in (90, 270):
            if (R[r](*loc)[1] > 0) == below:
                want[d] = r
                break
    return want


def rotate_cluster(lines, want):
    """Turn the named components, and the stubs and labels that touch them.

    Attribution is by **pin coincidence**, not by the cell model: this runs
    on an already-packed file, where the uniform grid the cells depend on
    no longer exists. Using `assign_cells` here set 31 components' rotation
    and left every one of their stubs lying horizontal, which `schwire`'s
    transform check then caught as 62 pins missing their stub.

    A rotation of the whole cluster is as safe as a translation of one: the
    stored rotation turns the pins about the anchor, and turning the stubs
    about the same anchor by the same angle moves them identically, so
    every coincidence survives.
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
    R = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
         180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}

    owner_at, anchors, turn = {}, {}, {}
    for h, b, _, _ in lines:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig):
            continue
        d = desig[h["id"]]
        if d not in want:
            continue
        cur = (b.get("rotation") or 0) % 360
        theta = (want[d] - cur) % 360
        if not theta:
            continue
        turn[d] = theta
        anchors[d] = (b["x"], b["y"])
        for q in sympins.get(symref.get(h["id"]), []):
            dx, dy = R[cur](q.get("x", 0), q.get("y", 0))
            owner_at[(round(b["x"] + dx), round(b["y"] + dy))] = d

    # which wire groups touch a pin of a component being turned
    group = {}
    for h, b, _, _ in lines:
        if h and h["type"] == "LINE" and b.get("lineGroup"):
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                d = owner_at.get((round(x), round(y)))
                if d:
                    group[b["lineGroup"]] = d

    def spin(x, y, d):
        ax, ay = anchors[d]
        dx, dy = x - ax, y - ay
        rx, ry = R[turn[d]](dx, dy)
        return int(round(ax + rx)), int(round(ay + ry))

    out, touched = [], 0
    for h, b, _, raw in lines:
        if h is not None:
            d = None
            if h["type"] == "COMPONENT" and desig.get(h["id"]) in turn:
                d = desig[h["id"]]
            elif h["type"] == "LINE" and b.get("lineGroup") in group:
                d = group[b["lineGroup"]]
            elif h["type"] == "ATTR" and b.get("parentId") in group:
                d = group[b["parentId"]]
            elif (h["type"] == "ATTR" and b.get("parentId") in desig
                  and desig[b["parentId"]] in turn):
                d = desig[b["parentId"]]
            if d:
                nb = json.loads(json.dumps(b))
                if h["type"] == "COMPONENT":
                    nb["rotation"] = want[d]
                for px, py in (("x", "y"), ("startX", "startY"),
                               ("endX", "endY")):
                    if isinstance(nb.get(px), (int, float)) and \
                            isinstance(nb.get(py), (int, float)):
                        nb[px], nb[py] = spin(nb[px], nb[py], d)
                raw = (f"{json.dumps(h, separators=(',', ':'))}||"
                       f"{json.dumps(nb, separators=(',', ':'))}|")
                touched += 1
        out.append(raw)
    return out, turn, touched


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="the .epro2 exported from EasyEDA")
    ap.add_argument("netlist", type=Path, help="the netlist.json that went in")
    ap.add_argument("-o", "--out", type=Path, help="where to write the result")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and report the plan, write nothing")
    ap.add_argument("--width", type=int, default=2400,
                    help="canvas units to wrap a block's row at (default 2400)")
    ap.add_argument("--gap", type=int, default=40,
                    help="units between neighbours (default 40)")
    ap.add_argument("--stagger", type=int, default=0,
                    help="offset alternate neighbours in a row by this many "
                         "units, so their stubs stop being colinear")
    ap.add_argument("--max-row", type=int, default=1200,
                    help="wrap a hub's satellite row past this width "
                         "(--layout star only)")
    ap.add_argument("--upright", action="store_true",
                    help="stand the supply-to-ground two-pin parts up, "
                         "supply pin at the top")
    ap.add_argument("--columns", type=int, default=1,
                    help="deal the bands into this many columns "
                         "(--layout star only). 0 packs them onto shelves "
                         "by their real width instead, which is what a "
                         "band of two parts needs when the column model "
                         "would give it the width of a band of twenty")
    ap.add_argument("--layout", choices=("row", "star"), default="row",
                    help="row: one connectivity-ordered chain per band. "
                         "star: hubs with their satellites hung off the pins "
                         "that reach them (default row)")
    ap.add_argument("--upright-wide", action="store_true",
                    help="stand up parts with more than two pins too, when "
                         "exactly one ground and one supply pin say which "
                         "way. An eight-LED chain lying flat runs the width "
                         "of the sheet")
    ap.add_argument("--flow", action="store_true",
                    help="order the bands by distance from the board's "
                         "input connector, so the sheet reads from what "
                         "comes in to what goes out")
    ap.add_argument("--grid-from", type=Path,
                    help="infer the placement grid from this export rather "
                         "than from the one being arranged. A carved sheet "
                         "holds a subset, whose spacings are multiples of "
                         "the real pitch and not the pitch")
    ap.add_argument("--block-gap", type=int, default=120,
                    help="extra units between blocks (default 120)")
    a = ap.parse_args()

    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    for p in (a.export, a.netlist):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 2

    name, lines = read_records(a.export)
    _, desig, canvas, _ = canvas_index(lines)
    anchors = [(b["x"], b["y"]) for h, b, _, _ in lines
               if h and h["type"] == "COMPONENT" and h["id"] in canvas]
    if not anchors:
        print("error: no placed components found in the export", file=sys.stderr)
        return 2

    # A carved sheet holds a subset of the board's components, so the
    # spacings between what is left are multiples of the real pitch and
    # not the pitch itself -- the inferred grid comes out wrong and
    # records fall outside their cell, which surfaces as "no geometry
    # found". Read the grid from the uncarved export instead.
    if a.grid_from:
        if not a.grid_from.exists():
            print(f"error: {a.grid_from} not found", file=sys.stderr)
            return 2
        _, glines = read_records(a.grid_from)
        _, _, gcanvas, _ = canvas_index(glines)
        ganchors = [(b["x"], b["y"]) for h, b, _, _ in glines
                    if h and h["type"] == "COMPONENT" and h["id"] in gcanvas]
        if not ganchors:
            print(f"error: no placed components in {a.grid_from}",
                  file=sys.stderr)
            return 2
        grid = Grid(ganchors)
    else:
        grid = Grid(anchors)
    of_record, owner, comp_cell, wire_cells, desig, clash = assign_cells(lines, grid)
    print(f"\n{a.export.name}")
    print(f"  {len(comp_cell)} placed components on a {grid.w} x {grid.h} grid")
    print(f"  {len(of_record)} canvas records, "
          f"{sum(1 for h, _, _, _ in lines if h) - len(of_record)} library records "
          f"left alone")

    # --- check 1: one component per cell ---------------------------------
    if clash:
        for c, x, y in clash[:8]:
            print(f"  CLASH  cell {c}: {x} and {y}")
        print(f"\n  {len(clash)} cell(s) hold more than one component — "
              f"translating one would drag the other's wiring\n")
        return 1

    # --- check 2: no wire crosses a cell boundary ------------------------
    spanning = {w: c for w, c in wire_cells.items() if len(c) > 1}
    if spanning:
        for w, c in list(spanning.items())[:8]:
            print(f"  SPANS  wire {w}: cells {sorted(c)}")
        print(f"\n  {len(spanning)} wire(s) cross a cell boundary — a rigid "
              f"translation would stretch them off their pins\n")
        return 1

    # --- check 3: each cell holds its own component's wiring -------------
    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    pins = {c["props"]["Designator"]: len(c["pins"]) for c in design.values()}
    got = collections.Counter()
    orphan = 0
    for w, cs in wire_cells.items():
        d = owner.get(next(iter(cs)))
        if d is None:
            orphan += 1
        else:
            got[d] += 1
    # A multi-pin wire legitimately serves several pins of one component, so
    # fewer wires than pins is fine; more is not, and a wire in an empty cell
    # belongs to nobody.
    over = [(d, pins.get(d, 0), got[d]) for d in got if got[d] > pins.get(d, 0)]
    if over or orphan:
        for d, pn, g in over[:8]:
            print(f"  EXTRA  {d}: {pn} pin(s) in the netlist, {g} wire(s) in its cell")
        if orphan:
            print(f"  ORPHAN {orphan} wire(s) sit in a cell with no component")
        print()
        return 1
    print(f"  {len(wire_cells)} wires, none crossing a cell, none orphaned")

    # --- plan -------------------------------------------------------------
    placed = {owner[c] for c in owner}
    blocks = read_blocks(a.netlist, placed)
    order, seen = [], set()
    gen = a.netlist.with_name("netlist.py")
    if gen.exists():
        for line in gen.read_text(encoding="utf-8").splitlines():
            m = SECTION_RE.match(line)
            if m and m.group(1) in blocks.values() and m.group(1) not in seen:
                order.append(m.group(1)); seen.add(m.group(1))
    for b in blocks.values():
        if b not in seen:
            order.append(b); seen.add(b)

    box = extents(lines, grid, of_record, owner)
    # Reserve the area each part will occupy *after* --upright, not
    # before. `extents` folds the stubs into the box, so non-overlapping
    # boxes keep them clear of their neighbours -- and then the rotation
    # pass turns a two-pin part's stubs from horizontal to vertical, after
    # the boxes were measured, and nine of them ended up inside a
    # neighbouring symbol. `upright` reads symbol-local offsets and stub
    # nets, both of which survive a translation, so it can be asked now
    # and the box swapped to match.
    if a.upright:
        pre_kinds = read_rail_kinds(a.netlist.with_name("design.yaml"))
        if pre_kinds:
            for d in upright(lines, design, pre_kinds):
                if d not in box:
                    continue
                x0, y0, x1, y1 = box[d]
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                w, h = (x1 - x0), (y1 - y0)
                box[d] = (cx - h / 2, cy - w / 2, cx + h / 2, cy + w / 2)

    missing_box = sorted(set(blocks) - set(box))
    if missing_box:
        print(f"  error: no geometry found for {', '.join(missing_box[:8])}",
              file=sys.stderr)
        return 1

    nets = collections.defaultdict(list)
    for c in design.values():
        for netname in c["pins"].values():
            if not netname.upper().startswith("NC_"):
                nets[netname].append(c["props"]["Designator"])
    if a.layout == "star":
        rails = set(read_rails(a.netlist.with_name("design.yaml")))
        if not rails:
            print("  error: --layout star needs design.yaml's rails:",
                  file=sys.stderr)
            return 2
        pinsof = {c["props"]["Designator"]:
                  [v for v in c["pins"].values()
                   if not v.upper().startswith("NC_")]
                  for c in design.values()}
        decl = {c["props"]["Designator"]: i
                for i, c in enumerate(design.values())}
        localpins = local_pins(lines, grid, of_record, owner)
        flow_depth = None
        if a.flow:
            was = list(order)
            fk = read_rail_kinds(a.netlist.with_name("design.yaml"))
            if not fk:
                print("  error: --flow needs design.yaml's rails:",
                      file=sys.stderr)
                return 1
            order, flow_depth = flow_order(order, blocks, pinsof, fk)
            if order != was:
                print("  flow order: " + " -> ".join(order[:4])
                      + (" ..." if len(order) > 4 else ""))
        placed = relax(star_pack(blocks, order, box, nets, pinsof,
                                 localpins, rails, decl, a.gap, a.gap,
                                 a.block_gap, a.columns, a.max_row,
                                 a.stagger, flow_depth), box, a.gap)
        missing = sorted(set(blocks) - set(placed))
        if missing:
            print(f"  error: star layout placed nothing for "
                  f"{', '.join(missing[:8])}", file=sys.stderr)
            return 1
    else:
        placed = pack(blocks, order, box, a.width, a.gap, a.gap,
                      a.block_gap, nets)
    counts = collections.Counter(blocks.values())
    print(f"\n  {len(order)} blocks:")
    for b in order:
        print(f"    {b:38s} {counts[b]:3d}")

    clashes = collisions(placed, box)
    if clashes:
        for x, y in clashes[:8]:
            print(f"  OVERLAP {x} and {y} would overlap once placed")
        print(f"\n  {len(clashes)} overlapping pair(s) — raise --gap or "
              f"--width. Two symbols pushed together can put a wire end on a "
              f"neighbour's pin, which is a new connection that the net diff "
              f"cannot see.\n")
        return 1

    xs = [box[d][0] + placed[d][0] for d in placed] + \
         [box[d][2] + placed[d][0] for d in placed]
    ys = [box[d][1] + placed[d][1] for d in placed] + \
         [box[d][3] + placed[d][1] for d in placed]
    print(f"\n  sheet {int(max(xs) - min(xs))} x {int(max(ys) - min(ys))} units"
          f"  (was {grid.w * 15} x {grid.h * 9})")

    cell_for = {d: c for c, d in owner.items()}

    if a.layout == "star":
        was = {d: grid.origin(*cell_for[d]) for d in placed}
        now = {d: (placed[d][0] + (box[d][0] + box[d][2]) / 2,
                   placed[d][1] + (box[d][1] + box[d][3]) / 2)
               for d in placed}
        d0, c0 = fanout_metrics(blocks, nets, pinsof, rails, decl, was)
        d1, c1 = fanout_metrics(blocks, nets, pinsof, rails, decl, now)
        print(f"  net-spoke crossings {c0:,} -> {c1:,}, "
              f"total spoke length {d0:,} -> {d1:,} units")

    deltas = {}
    for d, (dx, dy) in placed.items():
        ox, oy = grid.origin(*cell_for[d])
        deltas[cell_for[d]] = (int(round(dx - ox)), int(round(dy - oy)))

    moved = sum(1 for dx, dy in deltas.values() if dx or dy)
    print(f"\n  {moved} of {len(deltas)} components move")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    before = anchor_geometry(lines, of_record, owner)

    # --- rewrite -----------------------------------------------------------
    out_lines, touched = [], 0
    for i, (h, b, _, raw) in enumerate(lines):
        c = of_record.get(i)
        dx, dy = deltas.get(c, (0, 0)) if c is not None else (0, 0)
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
            if entry == name:
                z.writestr(entry, "\n".join(out_lines).encode("utf-8"))
            else:
                z.writestr(entry, src.read(entry))
    print(f"  {touched} records translated")


    # --- the proof ---------------------------------------------------------
    after = reread(a.out, lines, of_record, owner)
    drift = [d for d in before if before[d] != after.get(d)]
    gone = sorted(set(before) - set(after))
    if drift or gone:
        for d in drift[:8]:
            print(f"  DRIFT  {d}: local geometry changed — this is NOT a translation")
        for d in gone[:8]:
            print(f"  LOST   {d}: no longer found in the output")
        a.out.unlink(missing_ok=True)
        print(f"\n  {len(drift) + len(gone)} component(s) did not translate "
              f"rigidly; {a.out} deleted rather than left to be imported\n")
        return 1

    print(f"  {len(before)} components: local geometry unchanged, so every "
          f"wire still meets the pin it met")
    if a.upright:
        kinds = read_rail_kinds(a.netlist.with_name("design.yaml"))
        if not kinds:
            print("  error: --upright needs design.yaml's rails:",
                  file=sys.stderr)
            return 1
        _, back_lines = read_records(a.out)
        wants = upright(back_lines, design, kinds, a.upright_wide)
        out2, turned, n2 = rotate_cluster(back_lines, wants)
        if turned:
            src2 = zipfile.ZipFile(a.out)
            data = {e: src2.read(e) for e in src2.namelist()}
            src2.close()
            with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
                for e, blob in data.items():
                    z.writestr(e, "\n".join(out2).encode("utf-8")
                               if e == name else blob)
            byr = collections.Counter(turned.values())
            print(f"  upright {len(turned)} two-pin part(s) stood up, "
                  f"supply at the top: "
                  + ", ".join(f"{k}deg x{v}" for k, v in sorted(byr.items())))
            print(f"          {n2} record(s) rotated with them")

            # Its own proof, and a stronger one than the translation
            # check it replaces. Translation preserved anchor-relative
            # geometry; rotation deliberately does not. What must hold is
            # the property that actually matters: every pin, recomputed
            # from its component's new stored rotation, still lands on a
            # stub endpoint. That is the coincidence connectivity rests on.
            #
            # An earlier version attributed stubs with the cell model and
            # passed its own fingerprint while leaving all 62 stubs of the
            # 31 rotated parts lying horizontal. This check would have
            # failed on that, and `schwire` did.
            _, chk = read_records(a.out)
            cdes = {b["parentId"]: b["value"] for h, b, _, _ in chk
                    if h and h["type"] == "ATTR"
                    and b.get("key") == "Designator" and b.get("value")}
            csym = {b["parentId"]: b["value"] for h, b, _, _ in chk
                    if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
                    and b.get("value")}
            cpin = collections.defaultdict(list)
            for h, b, doc, _ in chk:
                if h and h["type"] == "PIN" and doc and doc[0] == "SYMBOL":
                    cpin[doc[1]].append(b)
            ends = {(round(b["startX"]), round(b["startY"])) for h, b, _, _ in chk
                    if h and h["type"] == "LINE" and b.get("lineGroup")}
            ends |= {(round(b["endX"]), round(b["endY"])) for h, b, _, _ in chk
                     if h and h["type"] == "LINE" and b.get("lineGroup")}
            RR = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
                  180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}
            adrift = []
            for h, b, _, _ in chk:
                if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                        or cdes.get(h["id"]) not in turned):
                    continue
                r = (b.get("rotation") or 0) % 360
                for q in cpin.get(csym.get(h["id"]), []):
                    dx, dy = RR[r](q.get("x", 0), q.get("y", 0))
                    if (round(b["x"] + dx), round(b["y"] + dy)) not in ends:
                        adrift.append(cdes[h["id"]])
            if adrift:
                a.out.unlink(missing_ok=True)
                for d in sorted(set(adrift))[:8]:
                    print(f"  DRIFT  {d}: a pin no longer meets its stub")
                print(f"\n  {len(adrift)} pin(s) came off their stub; "
                      f"{a.out} deleted\n")
                return 1
            print(f"          every rotated pin still meets its stub")
    # Last, on the file that is actually written. It used to run before
    # the --upright pass, so it described a state no artefact ever had:
    # standing a part up turns its two stubs from horizontal to vertical,
    # which is exactly what this measures.
    _, chk0 = read_records(a.out)
    near = collinear_stubs(chk0)
    print(f"  {near} pair(s) of foreign stubs lie colinear within "
          f"{COLINEAR_LIMIT} units ({COLINEAR_LIMIT * 0.254:.0f} mm)"
          + ("" if near == 0 else " — raise --gap or --stagger; "
             "each reads as one wire"))

    through = stubs_through_bodies(chk0)
    if through:
        names = sorted({d for d, _ in through})
        print(f"  {len(through)} stub(s) end up inside a symbol "
              f"({', '.join(names[:6])}{' ...' if len(names) > 6 else ''}) "
              f"— raise --gap; --upright turns stubs after the boxes were "
              f"measured")
    else:
        print(f"  no stub ends up inside another component's symbol")

    print(f"  wrote {a.out}")
    print(f"\n  now prove the nets too:  ./tools/epro.py diff {a.out} {a.netlist}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
