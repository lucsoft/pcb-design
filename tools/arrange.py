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
    out = []
    for line in z.read(name).decode("utf-8").split("\n"):
        if "||" not in line:
            out.append((None, None, line))
            continue
        head, _, body = line.partition("||")
        try:
            h = json.loads(head)
            b = json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            out.append((None, None, line))
            continue
        out.append((h, b, line))
    return name, out


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


def plan(blocks: dict[str, str], order: list[str], per_row: int
         ) -> dict[str, tuple[int, int]]:
    """designator -> (col, row) in the new grid, blocks stacked top to bottom.

    Row 0 is the bottom in this coordinate space (y grows upward from a
    negative origin), so the list is laid out bottom-up to keep the first
    block of the netlist at the top of the sheet, where a reader starts.
    """
    members = collections.defaultdict(list)
    for d, b in blocks.items():
        members[b].append(d)

    rows_for = {}
    for b in order:
        rows_for[b] = max(1, math.ceil(len(members[b]) / per_row))
    total = sum(rows_for[b] for b in order) + len(order) - 1

    out, row_top = {}, total - 1
    for b in order:
        ds = sorted(members[b], key=_desig_key)
        for i, d in enumerate(ds):
            out[d] = (i % per_row, row_top - i // per_row)
        row_top -= rows_for[b] + 1          # one empty row between blocks
    return out


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
    for h, b, _ in lines:
        if h is not None and h.get("id") is not None:
            by_id[h["id"]] = (h, b)

    desig = {b["parentId"]: b["value"] for h, b, _ in lines
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value") and b["value"] != "?"}

    canvas = set()
    for h, b, _ in lines:
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
    for h, b, _ in lines:
        if h is None or h["type"] != "COMPONENT" or h["id"] not in canvas:
            continue
        c = grid.cell(b["x"], b["y"])
        d = desig[h["id"]]
        if c in owner:
            clash.append((c, owner[c], d))
        comp_cell[h["id"]], owner[c] = c, d

    wire_cells = collections.defaultdict(set)
    for h, b, _ in lines:
        if h is not None and h["type"] == "LINE" and b.get("lineGroup"):
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                wire_cells[b["lineGroup"]].add(grid.cell(x, y))

    of_record = {}
    for i, (h, b, _) in enumerate(lines):
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


def local_geometry(lines, grid, of_record, owner):
    """Per component: every coordinate it owns, relative to its cell origin.

    This is the fingerprint a rigid translation must leave untouched, and the
    net diff cannot see a break in it: moving a symbol and leaving its wire
    behind keeps every NET attribute in the file and still opens the circuit,
    because connection is coincidence of coordinates and nothing records the
    pair.
    """
    out = collections.defaultdict(list)
    for i, (h, b, _) in enumerate(lines):
        c = of_record.get(i)
        d = owner.get(c)
        if d is None:
            continue
        ox, oy = grid.origin(*c)
        for ax, ay in (("x", "y"), ("startX", "startY"), ("endX", "endY")):
            if isinstance(b.get(ax), (int, float)) and isinstance(b.get(ay), (int, float)):
                out[d].append((h["type"], ax, b[ax] - ox, b[ay] - oy))
    return {d: sorted(v) for d, v in out.items()}


def fingerprint(path):
    """Read a written archive back and fingerprint it the same way."""
    _, lines = read_records(path)
    _, desig, canvas, _ = canvas_index(lines)
    anchors = [(b["x"], b["y"]) for h, b, _ in lines
               if h and h["type"] == "COMPONENT" and h["id"] in canvas]
    grid = Grid(anchors)
    of_record, owner, *_ = assign_cells(lines, grid)
    return local_geometry(lines, grid, of_record, owner)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="the .epro2 exported from EasyEDA")
    ap.add_argument("netlist", type=Path, help="the netlist.json that went in")
    ap.add_argument("-o", "--out", type=Path, help="where to write the result")
    ap.add_argument("--dry-run", action="store_true",
                    help="check and report the plan, write nothing")
    ap.add_argument("--per-row", type=int, default=8,
                    help="components per row within a block (default 8)")
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
    anchors = [(b["x"], b["y"]) for h, b, _ in lines
               if h and h["type"] == "COMPONENT" and h["id"] in canvas]
    if not anchors:
        print("error: no placed components found in the export", file=sys.stderr)
        return 2

    grid = Grid(anchors)
    of_record, owner, comp_cell, wire_cells, desig, clash = assign_cells(lines, grid)
    print(f"\n{a.export.name}")
    print(f"  {len(comp_cell)} placed components on a {grid.w} x {grid.h} grid")
    print(f"  {len(of_record)} canvas records, "
          f"{sum(1 for h, _, _ in lines if h) - len(of_record)} library records "
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

    target = plan(blocks, order, a.per_row)
    counts = collections.Counter(blocks.values())
    print(f"\n  {len(order)} blocks:")
    for b in order:
        print(f"    {b:38s} {counts[b]:3d}")

    cell_for = {d: c for c, d in owner.items()}
    deltas = {}
    for d, (col, row) in target.items():
        nx, ny = grid.origin(col, row)
        ox, oy = grid.origin(*cell_for[d])
        deltas[cell_for[d]] = (nx - ox, ny - oy)

    moved = sum(1 for dx, dy in deltas.values() if dx or dy)
    print(f"\n  {moved} of {len(deltas)} components move")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    before = local_geometry(lines, grid, of_record, owner)

    # --- rewrite -----------------------------------------------------------
    out_lines, touched = [], 0
    for i, (h, b, raw) in enumerate(lines):
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
    after = fingerprint(a.out)
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
    print(f"  wrote {a.out}")
    print(f"\n  now prove the nets too:  ./tools/epro.py diff {a.out} {a.netlist}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
