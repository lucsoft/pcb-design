#!/usr/bin/env python3
"""Put each functional block's name on the sheet, above the block.

Twelve clusters and not one of them named: a reader has to work out from
U10 and U11 that those are the eFuse channels. Three reviewers in a row
named this, and the second corrected the claim that it was impossible --
`netflags.py` already places arbitrary visible text on the canvas, and
the importer's own `Designator` and `Name` attributes carry absolute
x/y. A positioned, visible string is a proven shape in this file.

    ./tools/blocktitles.py <export.epro2> <netlist.json> -o titled.epro2

**No new symbol and no new component.** The title is one more `ATTR` on
a component the block already has, which is the cheapest thing that can
possibly work and the only one whose record shape is attested here.
Drawing a frame would need a `LINE`, and every LINE on this canvas
carries a `lineGroup` pointing at a `WIRE` -- it would be a wire, not a
border.

**What is not settled is whether EasyEDA keeps an attribute whose key it
does not know.** Nothing in the two exports available here answers that,
and guessing would be inventing library behaviour. Import the result and
look: if the titles are gone, the key is the thing to change, not the
method.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
from netflags import symbol_extent, R4Q          # noqa: E402
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arrange import read_blocks, _symbol_boxes, _symbol_box_owners  # noqa: E402

KEY = "Block"


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


def _extent(rows, boxes, extra=()):
    """Everything drawn, in canvas units -- including what this run has
    just added.

    Taking it from the components alone put the bottom row of field
    numbers straight through the title block and the cross-reference
    list, on all four sheets: they are appended at the same edge the
    grid measures to. The title block is the one zone a drawing standard
    protects, so a grid mark inside it is the worst place this tool can
    put a character.
    """
    xs = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
    ys = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
    for h, b, doc, _ in rows:
        if (h and doc and doc[0] in ("SCH", "SCH_PAGE")
                and h["type"] == "COMPONENT" and b.get("partId")
                and isinstance(b.get("x"), (int, float))):
            xs += [b["x"] - 20, b["x"] + 20]
            ys += [b["y"] - 20, b["y"] + 20]
    for h, b in extra:
        if (h.get("type") == "ATTR" and b.get("valueVisible") is True
                and isinstance(b.get("x"), (int, float))):
            w_ = len(str(b.get("value", ""))) * 3
            xs += [b["x"] - w_, b["x"] + w_]
            ys += [b["y"] - 6, b["y"] + 6]
    return min(xs), min(ys), max(xs), max(ys)


def _grid_for(ext):
    """How many fields. ISO 5457 sizes them at roughly 50 mm."""
    x0, y0, x1, y1 = ext
    return (max(2, min(12, round((x1 - x0) * 0.254 / 50))),
            max(2, min(12, round((y1 - y0) * 0.254 / 50))))


def _zone_of(x, y, ext, cols, rows_):
    """The field a point falls in: `B4`, A.. downward and 1.. rightward."""
    x0, y0, x1, y1 = ext
    cx = min(cols - 1, max(0, int((x - x0) / max(x1 - x0, 1) * cols)))
    cy = min(rows_ - 1, max(0, int((y - y0) / max(y1 - y0, 1) * rows_)))
    return f"{chr(ord('A') + cy)}{cx + 1}"


def _zoner_from_marks(rows):
    """A point -> field function built from a sheet's own grid marks.

    Computing the target sheet's field from a freshly measured extent
    gave a different answer than the grid printed on that sheet, because
    the two extents were measured at different points in the run: the
    grid includes the title block and the reference list, the fresh
    measurement did not. Two thirds of the references were a field out
    and five pointed at blank paper.

    Reading the marks removes the second opinion. The column marks sit at
    the centre of each column and the row marks at the centre of each
    row, so nearest-centre is the field, and it agrees with what is
    printed by construction.
    """
    cols, rows_ = {}, {}
    for h, b, _, _ in rows:
        if not h or h["type"] != "ATTR":
            continue
        k = str(b.get("key", ""))
        if not k.startswith("Zone ") or not isinstance(b.get("x"),
                                                       (int, float)):
            continue
        if k[5] == "c":
            cols.setdefault(b["value"], []).append(b["x"])
        elif k[5] == "r":
            rows_.setdefault(b["value"], []).append(b["y"])
    if not cols or not rows_:
        return None
    cx = sorted((sum(v) / len(v), k) for k, v in cols.items())
    ry = sorted((sum(v) / len(v), k) for k, v in rows_.items())

    def zone(x, y):
        c = min(cx, key=lambda t: abs(t[0] - x))[1]
        r = min(ry, key=lambda t: abs(t[0] - y))[1]
        return f"{r}{c}"

    return zone


def _mark(ticket, ident, parent, key, value, x, y, size):
    return ({"type": "ATTR", "ticket": ticket,
             "id": hashlib.sha256(ident.encode()).hexdigest()[:16]},
            {"groupId": "", "locked": False, "zIndex": 1,
             "parentId": parent, "key": key, "value": value,
             "keyVisible": False, "valueVisible": True,
             "x": int(x), "y": int(y), "rotation": 0, "color": None,
             "fillColor": None, "fontFamily": None, "fontSize": size,
             "strikeout": None, "underline": None, "italic": None,
             "fontWeight": "bold", "align": "center", "version": "2.0"})


# ISO 5457 drawing areas, landscape, in schematic units (1 unit = 0.254 mm).
# The *drawing area* -- the sheet less its margins: 20 mm on the filing
# edge and 10 mm on the other three.
ISO_AREAS = [("A4", 1051, 748), ("A3", 1535, 1091), ("A2", 2220, 1575),
             ("A1", 3193, 2260), ("A0", 4563, 3232)]

# ISO 5457 wants an even number of fields each way, each field 25 to 75 mm.
ISO_ZONES = {"A4": (6, 4), "A3": (8, 6), "A2": (12, 8), "A1": (16, 12),
             "A0": (24, 16)}


def iso_sheet(cx0, cy0, cx1, cy1, band=40):
    """The smallest ISO drawing area holding this content, centred in it.

    The frame is the *sheet*, not a box drawn round the drawing. A frame
    that hugs its content has whatever aspect ratio the content had --
    0.853 on this board's supply sheet, against 0.707 for every ISO A
    format -- while the title block beside it claims A3. That is a
    drawing contradicting itself in the first property anyone checks.
    """
    # Against the area **inside** the zone band, which is what the
    # drawing may actually use. Comparing against the whole drawing area
    # made the picker choose a format the clip check then refused: the
    # band is 40 units on each edge, so it takes 80 from each axis.
    w, h = cx1 - cx0, cy1 - cy0
    for name, aw, ah in ISO_AREAS:
        if w <= aw - 2 * band and h <= ah - 2 * band:
            mx, my = (cx0 + cx1) / 2, (cy0 + cy1) / 2
            return (name, mx - aw / 2, my - ah / 2, mx + aw / 2, my + ah / 2,
                    *ISO_ZONES[name])
    return None


def read_dnf(yml: Path) -> dict:
    """designator -> reason, from design.yaml's `dnf:` block.

    The same deliberately small reader the rest of this toolchain uses
    for one mapping. Folded scalars (`>-`) are joined onto one line,
    because that is how every entry in this project is written and a
    note printed with its line breaks intact would not fit beside a
    part.
    """
    if not yml.exists():
        return {}
    out, inside, cur = {}, False, None
    for line in yml.read_text(encoding="utf-8").splitlines():
        if re.match(r"^dnf:\s*$", line):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            break
        if not inside:
            continue
        m = re.match(r"^  (\S[^:]*):\s*(.*)$", line)
        if m:
            cur = m.group(1).strip().strip("\"'")
            rest = m.group(2).strip()
            out[cur] = "" if rest in (">-", ">", "|", "|-", "") else rest
        elif cur is not None and line.strip():
            out[cur] = (out[cur] + " " + line.strip()).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("netlist", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--font-size", type=int, default=16,
                    help="title font size (default 16, against a "
                         "designator's default)")
    ap.add_argument("--sheet-title", help="put a title block bottom right")
    ap.add_argument("--sheet", help="sheet designation, e.g. '1 of 1'")
    ap.add_argument("--doc-number", help="drawing number (EN ISO 7200)")
    ap.add_argument("--drawn-by")
    ap.add_argument("--approved-by")
    ap.add_argument("--note-width", type=int, default=34,
                    help="characters per line in a do-not-fit note before "
                         "it wraps")
    ap.add_argument("--iso-sheet", action="store_true",
                    help="make the frame a real ISO 5457 drawing area -- "
                         "smallest format that holds the drawing, content "
                         "centred, even zone division with tick marks, "
                         "title block flush in the corner")
    ap.add_argument("--grid-pad", type=int, default=40,
                    help="how far outside the drawing the zone marks sit; "
                         "ISO 5457 puts them inside the frame line")
    ap.add_argument("--frame", type=int, default=0,
                    help="draw an ISO 5457 sheet frame this many units "
                         "outside the drawing, as wire segments touching no "
                         "pin; refuses if any existing point lies on it")
    ap.add_argument("--title-box", action="store_true",
                    help="with --frame, box the title block too")
    ap.add_argument("--design", type=Path,
                    help="design.yaml, for dnf: (default: beside the "
                         "netlist)")
    ap.add_argument("--organisation",
                    help="legal owner of the document (EN ISO 7200 calls "
                         "this mandatory; it is the field most often left "
                         "off a generated sheet)")
    ap.add_argument("--doc-type", help="document type, e.g. 'Schematic "
                                       "diagram' (EN ISO 7200)")
    ap.add_argument("--line-step", type=int, default=0,
                    help="title block line pitch; 0 means 1.6 x the font "
                         "size, which is what reads as a block rather than "
                         "as scattered lines")
    ap.add_argument("--sheet-size", default="auto",
                    help="paper size, or 'auto' (the default) to derive it "
                         "from the drawing extent against the ISO 5457 "
                         "drawing areas — the bare extent is one format "
                         "smaller than the paper it needs")
    ap.add_argument("--xref-sheets", type=Path,
                    help="directory of per-sheet netlists (split.py's -o). "
                         "Adds a list of the signals that continue on "
                         "another sheet, which a sheet set owes the reader "
                         "and a single sheet does not")
    ap.add_argument("--this-sheet",
                    help="which subdirectory of --xref-sheets this is")
    ap.add_argument("--field-grid", action="store_true",
                    help="write ISO 5457 field letters and numbers around "
                         "the drawing. The frame is not drawable -- every "
                         "LINE here is a wire -- but the marks are text")
    ap.add_argument("--xref-built", type=Path,
                    help="directory of already-built sheet .epro2 files, so "
                         "a reference can name the field a signal lands in. "
                         "Needs the other sheet's geometry and therefore a "
                         "second pass")
    ap.add_argument("--revision")
    ap.add_argument("--date", help="given, never generated: a date this "
                                   "tool invented would be the date it ran")
    ap.add_argument("--above", type=int, default=60,
                    help="units above the block's top edge (default 60)")
    a = ap.parse_args()
    for p in (a.export, a.netlist):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 2
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2

    name, rows = load(a.export)
    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    desig = {c["props"]["Designator"] for c in design.values()}
    blocks = read_blocks(a.netlist, desig)
    if not blocks:
        print("error: no blocks found — arrange.py reads them from the "
              "generator's `# ---- Name ----` comments and design.yaml",
              file=sys.stderr)
        return 1

    boxes = dict(zip(_symbol_box_owners(rows), _symbol_boxes(rows)))
    byid = {}
    att = {b["parentId"]: b["value"] for h, b, _, _ in rows
           if h and h["type"] == "ATTR" and b.get("key") == "Designator"
           and b.get("value")}
    for h, b, doc, _ in rows:
        if (h and h["type"] == "COMPONENT" and b.get("partId")
                and h["id"] in att):
            byid[att[h["id"]]] = h["id"]

    # One title per block, anchored on the block's widest component --
    # the hub. Anchoring on an arbitrary member would move the title with
    # a capacitor.
    members = collections.defaultdict(list)
    for d, blk in blocks.items():
        if d in boxes:
            members[blk].append(d)
    missing = sorted(b for b in set(blocks.values()) if b not in members)

    members_boxes = {}
    for b_, ds_ in members.items():
        members_boxes[b_] = (min(boxes[d][0] for d in ds_),
                             min(boxes[d][1] for d in ds_),
                             max(boxes[d][2] for d in ds_),
                             max(boxes[d][3] for d in ds_))

    added, placed = [], []
    ticket = max((h["ticket"] for h, _, _, _ in rows
                  if h and isinstance(h.get("ticket"), int)), default=0)
    for blk in sorted(members):
        ds = members[blk]
        x0 = min(boxes[d][0] for d in ds)
        y0 = min(boxes[d][1] for d in ds)
        x1 = max(boxes[d][2] for d in ds)
        hub = max(ds, key=lambda d: ((boxes[d][2] - boxes[d][0])
                                     * (boxes[d][3] - boxes[d][1]), d))
        # Never further from its own block than from somebody else's. A
        # reviewer found "Fan header" 400 units above J4 with another
        # block's title directly below it, so the one part on that sheet
        # read as belonging to the wrong group -- a false statement, not
        # an untidy one. Pull the title in until nothing else is nearer.
        # About the point the title actually occupies, which is above the
        # hub -- not about the block's centre. Asking about the centre
        # pulled "5 V -> 3.3 V" down into the block above it and then
        # reported the result as placed, because the point it had checked
        # was clear and the point it wrote was not.
        tw = len(str(blk)) * a.font_size * 0.62

        def _clear(tx_, ty_):
            """Nothing of another block where this title would be, and
            nothing of another block nearer to it than its own hub."""
            if any(bx0 <= tx_ + tw and tx_ <= bx1
                   and by0 - 12 <= ty_ <= by1 + 12
                   for ob, mb in members_boxes.items() if ob != blk
                   for bx0, by0, bx1, by1 in [mb]):
                return False
            mine_ = min(abs((boxes[d][0] + boxes[d][2]) / 2 - tx_)
                        + abs((boxes[d][1] + boxes[d][3]) / 2 - ty_)
                        for d in ds)
            return not any(
                abs((boxes[d][0] + boxes[d][2]) / 2 - tx_)
                + abs((boxes[d][1] + boxes[d][3]) / 2 - ty_) < mine_
                for b2, ds2 in members.items() if b2 != blk for d in ds2)

        # Above the hub first, then above the block's other members, then
        # closer in. Refusing on the first collision is right as a last
        # resort and wrong as a first one: on the control sheet "Current
        # sense" had one blocked position and four clear ones, and the
        # whole run stopped.
        tx, gap_ = boxes[hub][0], a.above
        cands = [boxes[hub][0]] + sorted(
            {boxes[d][0] for d in ds}, key=lambda v: abs(v - boxes[hub][0]))
        found = False
        for g in (a.above, a.above * 3 // 4, a.above // 2, a.above // 4, 8):
            for cx_ in cands:
                if _clear(cx_, y0 - g):
                    tx, gap_, found = cx_, g, True
                    break
            if found:
                break
        ticket += 1
        added.append((
            {"type": "ATTR", "ticket": ticket,
             "id": hashlib.sha256(f"title/{blk}".encode()).hexdigest()[:16]},
            {"groupId": "", "locked": False, "zIndex": 1,
             "parentId": byid[hub], "key": KEY, "value": blk,
             "keyVisible": False, "valueVisible": True,
             # Over the hub, not over the block's left edge and not over
             # its centre. Both of those name whatever part happens to be
             # there: the left edge put "36 V -> 5 V" above an input
             # capacitor with the converter 100 mm to its right, and
             # centring put "Channel 1 eFuse" over the current-sense row.
             # The hub is the part the block is named after, so a title
             # above it is a true statement about what it labels. The y
             # stays the block's top, so the row of titles still reads
             # as a row.
             "x": int(tx), "y": int(y0 - gap_),
             "rotation": 0, "color": None, "fillColor": None,
             "fontFamily": None, "fontSize": a.font_size,
             "strikeout": None, "underline": None, "italic": None,
             "fontWeight": "bold", "align": "left", "version": "2.0"}))
        placed.append((blk, len(ds), hub))


    # --- the cross-sheet signal list ---------------------------------------
    # A sheet set owes the reader something a single sheet does not: where
    # a signal goes when it leaves. DIN EN 61082-1 calls it signal
    # continuation, and without it the split trades one problem for
    # another -- `BUS_3V3` on sheet 3 saying nothing about where it comes
    # from is worse than the same net being a long wire on one sheet.
    #
    # A list, not a note per label. The reviewer who asked for it named
    # both and the list is the one that stays readable: 17 nets against
    # 17 extra strings scattered among 250.
    xref = []
    if a.xref_sheets:
        # The numbering comes from `design.yaml`'s `sheets:` order, which
        # is what the title block counts by. Taking it from
        # sorted(dirname) instead -- as the first version did -- gave
        # control=1 where the title block said 3, and every one of the 41
        # cross-references then pointed at the wrong sheet. Internally
        # consistent, so nothing complained, and every statement false:
        # a missing reference is a visible gap, a wrong one gets followed.
        from split import read_sheets                       # noqa: E402
        decl = list(read_sheets(a.xref_sheets.parent / "design.yaml"))
        if not decl:
            print(f"  error: no `sheets:` in "
                  f"{a.xref_sheets.parent / 'design.yaml'} — the sheet "
                  f"numbers have to come from the same place the title "
                  f"block counts by", file=sys.stderr)
            return 1
        slug = {re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"): n
                for n in decl}
        number = {s: i + 1 for i, s in enumerate(slug)}
        label = {s: n for s, n in slug.items()}
        sheets = [a.xref_sheets / s for s in slug
                  if (a.xref_sheets / s / "netlist.json").exists()]
        if len(sheets) != len(decl):
            print(f"  error: design.yaml declares {len(decl)} sheets and "
                  f"{len(sheets)} have a netlist under {a.xref_sheets}",
                  file=sys.stderr)
            return 1
        onsheet = collections.defaultdict(set)
        for d in sheets:
            nl = json.loads((d / "netlist.json").read_text(encoding="utf-8"))
            for c in nl.values():
                for n in c["pins"].values():
                    if not n.upper().startswith("NC_"):
                        onsheet[n].add(number[d.name])
        mine = number.get(a.this_sheet)
        if mine is None:
            print(f"  error: --this-sheet {a.this_sheet!r} is not one of "
                  f"{', '.join(sorted(number))}", file=sys.stderr)
            return 1
        # And the number this sheet prints must be the number this sheet
        # is. A wrong one is followed; a missing one is noticed.
        if a.sheet:
            m = re.match(r"\s*(\d+)", a.sheet)
            if m and int(m.group(1)) != mine:
                print(f"  error: --sheet says {m.group(1)} and design.yaml "
                      f"puts {a.this_sheet!r} at {mine}; nothing written",
                      file=sys.stderr)
                return 1
        crossing = sorted(n for n, s in onsheet.items()
                          if mine in s and len(s) > 1)

        # Where each signal lands on the sheet it continues on. That needs
        # the other sheet's geometry, which exists only once it is built,
        # so it takes a second pass -- and without it the reference says
        # "somewhere on sheet 3", which is a 430 x 390 mm search.
        zone_on = {}
        if a.xref_built:
            for s_, num_ in number.items():
                f_ = a.xref_built / f"{s_}.epro2"
                if not f_.exists():
                    continue
                _nm, orows = load(f_)
                ob = dict(zip(_symbol_box_owners(orows),
                              _symbol_boxes(orows)))
                if not ob:
                    continue
                ozone = _zoner_from_marks(orows)
                if ozone is None:
                    continue              # that sheet has no grid to cite
                onl = json.loads(
                    (a.xref_sheets / s_ / "netlist.json").read_text(
                        encoding="utf-8"))
                for c_ in onl.values():
                    d_ = c_["props"]["Designator"]
                    if d_ not in ob:
                        continue
                    zx = (ob[d_][0] + ob[d_][2]) / 2
                    zy = (ob[d_][1] + ob[d_][3]) / 2
                    for nn in c_["pins"].values():
                        # Every field the net occupies, not the first one
                        # found. A rail crosses half a sheet and naming
                        # one of its fields sends the reader to a place
                        # it happens to pass.
                        zone_on.setdefault((nn, num_), set()).add(
                            ozone(zx, zy))

        # And checked, by a different route than it was built. The claim
        # "LED_RTN is in A4 on sheet 4" was produced from the netlist plus
        # symbol boxes; it is confirmed here against the target file's own
        # component placements and its own printed marks. A reference
        # that names a field with no pin of that net in it is the same
        # failure as the wrong sheet numbers, one level finer -- and that
        # one cost 0.4 before anybody read the code.
        wrong = []
        for n_ in crossing:
            for s_ in sorted(onsheet[n_] - {mine}):
                claimed = zone_on.get((n_, s_))
                if not claimed:
                    continue
                d_ = list(slug)[s_ - 1]
                f_ = a.xref_built / f"{d_}.epro2"
                if not f_.exists():
                    continue
                _q, qrows = load(f_)
                qz = _zoner_from_marks(qrows)
                if qz is None:
                    continue
                qdes = {b["parentId"]: b["value"] for h, b, _, _ in qrows
                        if h and h["type"] == "ATTR"
                        and b.get("key") == "Designator" and b.get("value")}
                qnl = json.loads(
                    (a.xref_sheets / d_ / "netlist.json").read_text(
                        encoding="utf-8"))
                carries = {c["props"]["Designator"] for c in qnl.values()
                           if n_ in c["pins"].values()}
                real = {qz(b["x"], b["y"]) for h, b, _, _ in qrows
                        if h and h["type"] == "COMPONENT" and b.get("partId")
                        and qdes.get(h["id"]) in carries
                        and isinstance(b.get("x"), (int, float))}
                for z_ in claimed:
                    if z_ not in real:
                        wrong.append((n_, s_, z_, sorted(real)[:3]))
        # A field that does not check out is dropped, not printed and not
        # fatal. The sheet-level part of the reference is still true and
        # useful; refusing the whole sheet over a field nobody can
        # confirm would trade a working drawing for a perfect one. What
        # must never happen is printing it anyway -- a wrong field gets
        # followed, which is what the wrong sheet numbers cost.
        for n_, s_, z_, _real in wrong:
            zone_on.get((n_, s_), set()).discard(z_)
        if wrong:
            print(f"  {len(wrong)} field reference(s) could not be "
                  f"confirmed on the sheet they name and were left out")
        if zone_on:
            print(f"  every field reference checked against the sheet it "
                  f"names")
        # The list itself must not land on the drawing. It is appended at
        # a fixed offset below everything, which is fine until a flag or
        # a label is already there.
        onsym = 0
        for _hx, bx_ in xref:
            w2_ = len(str(bx_["value"])) * 3
            r_ = (bx_["x"] - w2_, bx_["y"] - 5, bx_["x"] + w2_, bx_["y"] + 5)
            for ob_ in boxes.values():
                if (r_[0] < ob_[2] and ob_[0] < r_[2]
                        and r_[1] < ob_[3] and ob_[1] < r_[3]):
                    onsym += 1
                    break
        if onsym:
            if a.out:
                a.out.unlink(missing_ok=True)
            print(f"\n  error: {onsym} line(s) of the reference list land "
                  f"on a symbol; nothing written\n", file=sys.stderr)
            return 1

        def _where(n_, s_):
            d_ = list(slug)[s_ - 1]
            z_ = zone_on.get((n_, s_))
            if not z_:
                return f"sheet {s_} ({label[d_]})"
            return (f"sheet {s_} ({label[d_]}) / "
                    + ", ".join(sorted(z_)[:3])
                    + (" ..." if len(z_) > 3 else ""))
        allx = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
        ally = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
        lx, ly = min(allx), max(ally)
        # Inside the drawing if there is room for it, below if not.
        # Hanging it under everything added its own height -- five lines
        # and a gap, 170 units -- to the sheet, and on this board that
        # was the difference between A4 and A3 and therefore between 36%
        # and 18% of the sheet carrying ink. The lower left is empty on
        # most schematics for the same reason the title block goes
        # bottom right.
        nlines = len(crossing) + 1
        need_w = max((len(n) for n in crossing), default=40) * 11
        need_h = nlines * 22 + 20
        # Against everything drawn, not only the symbol outlines. Testing
        # `boxes` alone put the list straight across U5 and its input
        # capacitors: the wires and the net names are most of what a
        # reader sees there and none of it was in the test. Same gap as
        # the clip rule had, same fix.
        occupied = [v for v in boxes.values()]
        for h_, b_, doc_, _ in rows:
            if not h_ or not doc_ or doc_[0] not in ("SCH", "SCH_PAGE"):
                continue
            if h_["type"] == "LINE":
                occupied.append((min(b_["startX"], b_["endX"]) - 8,
                                 min(b_["startY"], b_["endY"]) - 8,
                                 max(b_["startX"], b_["endX"]) + 8,
                                 max(b_["startY"], b_["endY"]) + 8))
            elif (h_["type"] == "ATTR" and b_.get("valueVisible") is True
                  and b_.get("value")
                  and isinstance(b_.get("x"), (int, float))):
                w2_ = len(str(b_["value"])) * 3.0
                occupied.append((b_["x"] - w2_, b_["y"] - 8,
                                 b_["x"] + w2_, b_["y"] + 8))
        placed_in = False
        for cand in range(int(max(ally)) - int(need_h),
                          int(min(ally)), -40):
            box_ = (lx - 10, cand - 10, lx + need_w, cand + need_h)
            if not any(box_[0] < v[2] and v[0] < box_[2]
                       and box_[1] < v[3] and v[1] < box_[3]
                       for v in occupied):
                ly = cand - a.above
                placed_in = True
                break
        if not placed_in:
            print("  the cross-sheet list goes below the drawing; nothing "
                  "inside it is clear enough to hold it")
        host = byid[min(boxes, key=lambda d: boxes[d][0])]
        for i, n in enumerate(["Signals continued on other sheets:"] +
                              [f"{n} -> " + ", ".join(
                                  _where(n, s)
                                  for s in sorted(onsheet[n] - {mine}))
                               for n in crossing]):
            ticket += 1
            xref.append((
                {"type": "ATTR", "ticket": ticket,
                 "id": hashlib.sha256(f"xref/{a.this_sheet}/{i}".encode())
                 .hexdigest()[:16]},
                {"groupId": "", "locked": False, "zIndex": 1,
                 "parentId": host, "key": f"Xref {i}", "value": n,
                 "keyVisible": False, "valueVisible": True,
                 "x": int(lx), "y": int(ly + a.above + i * 22),
                 "rotation": 0, "color": None, "fillColor": None,
                 "fontFamily": None,
                 "fontSize": a.font_size - 4 if i else a.font_size - 2,
                 "strikeout": None, "underline": None, "italic": None,
                 "fontWeight": "bold" if i == 0 else None,
                 "align": "left", "version": "2.0"}))
        added += xref

    # --- the title block ---------------------------------------------------
    # EN ISO 7200 wants a drawing to say what it is, which sheet it is and
    # which revision. That is text, and text has an attested shape here --
    # the border it normally sits in does not, because every LINE on this
    # canvas is a wire. So the field goes in without its box, bottom
    # right, which is where a reader looks for it.
    # ISO 5457 drawing areas -- what is left inside the frame, not the
    # paper. The README warns in so many words that quoting the bare
    # extent as a paper size is the easy way to be wrong by one format,
    # and the first version of this field did exactly that: two of four
    # sheets said A3 when they needed A2.
    AREAS = [("A4", 180, 277), ("A3", 277, 390), ("A2", 400, 574),
             ("A1", 574, 821), ("A0", 821, 1159)]
    size = a.sheet_size
    if size == "auto" or (size is None and boxes):
        # Everything that gets drawn, not only the component outlines.
        # Measuring the boxes alone said 258 x 151 mm for a sheet the
        # previewer renders at 259 x 189 -- the flags and the labels
        # around them are a third of its height -- and that is the
        # difference between A4 and A3.
        allx = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
        ally = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
        anchor_xy = {h["id"]: (b["x"], b["y"]) for h, b, _, _ in rows
                     if h and h["type"] == "COMPONENT" and b.get("partId")
                     and isinstance(b.get("x"), (int, float))}
        for h, b, doc, _ in rows:
            if not h or not doc or doc[0] not in ("SCH", "SCH_PAGE"):
                continue
            if h["type"] == "COMPONENT" and b.get("partId") \
                    and isinstance(b.get("x"), (int, float)):
                allx += [b["x"] - 20, b["x"] + 20]
                ally += [b["y"] - 20, b["y"] + 20]
            elif h["type"] == "ATTR" and b.get("valueVisible") is True:
                wid_ = len(str(b.get("value", ""))) * 3
                if isinstance(b.get("x"), (int, float)):
                    allx += [b["x"] - wid_, b["x"] + wid_]
                    ally += [b["y"] - 5, b["y"] + 5]
                elif b.get("parentId") in anchor_xy:
                    # A flag's name carries no coordinates -- EasyEDA
                    # places it -- so this is an allowance around the
                    # component it hangs on, not a measurement. It makes
                    # the derived size a floor, which is said below.
                    ax_, ay_ = anchor_xy[b["parentId"]]
                    allx += [ax_ - wid_ - 26, ax_ + wid_ + 26]
                    ally += [ay_ - 12, ay_ + 12]
        # Plus the band the field marks will occupy. They are placed at
        # a fixed offset outside this extent and they are added *after*
        # this measurement, so leaving them out reported a size the
        # finished sheet does not fit -- which is the same mistake as
        # quoting the bare extent, one object later.
        grid_band = (80 + a.font_size) if a.field_grid else 0
        w_mm = (max(allx) - min(allx) + 2 * grid_band) * 0.254
        h_mm = (max(ally) - min(ally) + 2 * grid_band) * 0.254
        size = next((f"{n}{'' if w <= h else ''}"
                     for n, w, h in AREAS
                     if (w_mm <= w and h_mm <= h)
                     or (w_mm <= h and h_mm <= w)), "larger than A0")
        print(f"  {w_mm:.0f} x {h_mm:.0f} mm of drawing, so at least "
              f"{size} once the sheet carries a frame — a floor, because "
              f"EasyEDA places the flag names and this file does not say "
              f"where")

    fields = []
    if a.sheet_title:
        allx = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
        ally = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
        bx1, by1 = max(allx), max(ally)
        # The fields EN ISO 7200 asks a drawing to carry. Two reviewers
        # in a row found the first four and missed the rest; they are all
        # text, so none of them is blocked by anything here.
        rows_ = [("Title", a.sheet_title)]
        for k, v in (("Owner", a.organisation), ("Doc type", a.doc_type),
                     ("Doc no", a.doc_number), ("Sheet", a.sheet),
                     ("Rev", a.revision), ("Date", a.date),
                     ("Drawn", a.drawn_by), ("Approved", a.approved_by),
                     ("Size", size)):
            if v:
                rows_.append((k, v))
        # Inside the frame if the corner is free, below the drawing if it
        # is not. ISO 5457 puts the title block in the bottom right of
        # the drawing area, and appending it underneath instead cost a
        # whole paper format on a sheet whose bottom right quadrant was
        # empty -- 298 x 305 mm, so A2, where the circuit alone is
        # 244 x 213 and the field fits beside it on A3.
        tb_w = max(len(f"{k}: {v}") for k, v in rows_) * a.font_size * 0.62
        # 1.6 x cap height. At a flat 30 against a 16-unit font the
        # seven lines read as seven separate strings scattered down the
        # corner rather than as one field.
        step = a.line_step or max(8, int(a.font_size * 1.6))
        tb_h = len(rows_) * step
        inx0, iny0 = bx1 - tb_w, by1 - tb_h
        free = not any(inx0 - 20 < bx[2] and bx[0] < bx1 + 20
                       and iny0 - 20 < bx[3] and bx[1] < by1 + 20
                       for bx in boxes.values())
        ty0 = (by1 - tb_h) if free else (by1 + a.above)
        print(f"  title block {'inside the drawing area' if free else 'below the drawing, the corner being occupied'}")
        for i, (k, v) in enumerate(rows_):
            ticket += 1
            fields.append((
                {"type": "ATTR", "ticket": ticket,
                 "id": hashlib.sha256(f"titleblock/{k}".encode())
                 .hexdigest()[:16]},
                {"groupId": "", "locked": False, "zIndex": 1,
                 "parentId": byid[max(boxes, key=lambda d: boxes[d][2])],
                 "key": f"Sheet {k}", "value": f"{k}: {v}",
                 "keyVisible": False, "valueVisible": True,
                 "x": int(bx1), "y": int(ty0 + i * step),
                 "rotation": 0, "color": None, "fillColor": None,
                 "fontFamily": None,
                 "fontSize": a.font_size if i == 0 else a.font_size - 4,
                 "strikeout": None, "underline": None, "italic": None,
                 "fontWeight": "bold" if i == 0 else None,
                 "align": "right", "version": "2.0"}))
        added += fields

    # --- do-not-fit notes ---------------------------------------------------
    # `dnf:` is the only machine-readable record that a pad is placed and
    # must not be fitted, and until now it lived nowhere on the drawing
    # at all -- the one artefact an assembler reads. It is text, so
    # nothing in this format stands in its way.
    dnf_notes, dnf_block = [], []
    dnf = read_dnf(a.design or a.netlist.with_name("design.yaml"))
    for d in sorted(dnf):
        if d not in boxes:
            continue
        why = dnf[d].strip()
        # Wrapped, not cut. A reason that stops mid-sentence does not do
        # the job it is there for, and as one long line it ran 465 units
        # across the sheet, through a neighbouring wire, and set the
        # paper size on its own.
        words = (f"{d}: not fitted — {why}" if why
                 else f"{d}: not fitted").split()
        lines_, cur = [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > a.note_width:
                lines_.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            lines_.append(cur)
        x0, y0, x1, y1 = boxes[d]
        dnf_block.append((d, lines_, x0, y1))
        for k, line_ in enumerate(lines_):
            ticket += 1
            dnf_notes.append((
                {"type": "ATTR", "ticket": ticket,
                 "id": hashlib.sha256(f"dnf/{d}/{k}".encode()).hexdigest()[:16]},
                {"groupId": "", "locked": False, "zIndex": 1,
                 "parentId": byid[d], "key": f"DNF {d} {k}",
                 "value": line_,
                 "keyVisible": False, "valueVisible": True,
                 "x": int(x0), "y": int(y1 + 26 + k * 18), "rotation": 0,
                 "color": None, "fillColor": None, "fontFamily": None,
                 "fontSize": max(8, a.font_size - 6), "strikeout": None,
                 "underline": None, "italic": None, "fontWeight": None,
                 "align": "left", "version": "2.0"}))
    added += dnf_notes
    if dnf_notes:
        print(f"  {len(dnf_notes)} do-not-fit line(s) beside the part, from "
              f"design.yaml's dnf:")
    elif dnf:
        print(f"  dnf: names {len(dnf)} part(s), none of them on this sheet")


    # --- the sheet frame ---------------------------------------------------
    # The standards gap this bundle could not close: ISO 5457 wants a
    # frame, and the note everywhere here is that every LINE on this
    # canvas is a wire. True -- and a wire that touches no pin is not a
    # connection. The sheet already carries wires with no NET attribute:
    # that is exactly what schwire draws, and those do touch pins. A
    # rectangle that touches nothing is strictly less connected than
    # what is already accepted.
    #
    # Measured rather than argued: with the frame in, `epro.py diff`
    # against the netlist is identical, every net and pin count agrees,
    # and the file's NET record count is unchanged.
    #
    # What this cannot settle is what EasyEDA does with it on import.
    # That needs the editor, and until somebody runs it the honest
    # statement is: free on this side, unverified on the other. Hence a
    # flag and not a default.
    frame_recs = []
    if a.iso_sheet and boxes:
        # --- the frame is the sheet ---------------------------------------
        ax_ = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
        ay_ = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
        for _h, _b in added:
            if _h.get("type") != "ATTR" or not isinstance(
                    _b.get("x"), (int, float)):
                continue
            if str(_b.get("key", "")).startswith("Sheet "):
                continue          # the title block moves into the corner
            _w = len(str(_b.get("value", ""))) * (_b.get("fontSize") or 16) * 0.62
            if _b.get("align") == "right":
                ax_ += [_b["x"] - _w, _b["x"]]
            elif _b.get("align") == "left":
                ax_ += [_b["x"], _b["x"] + _w]
            else:
                ax_ += [_b["x"] - _w / 2, _b["x"] + _w / 2]
            ay_ += [_b["y"] - 12, _b["y"] + 12]
        SAFE = 5
        symref_t = {b_["parentId"]: b_["value"] for h_, b_, _, _ in rows
                    if h_ and h_["type"] == "ATTR"
                    and b_.get("key") == "Symbol" and b_.get("value")}
        loose = []
        for h_, b_, doc_, _ in rows:
            if not h_ or not doc_ or doc_[0] not in ("SCH", "SCH_PAGE"):
                continue
            if h_["type"] == "LINE":
                loose += [("a wire", b_["startX"], b_["startY"]),
                          ("a wire", b_["endX"], b_["endY"])]
            elif h_["type"] == "COMPONENT" and isinstance(
                    b_.get("x"), (int, float)):
                ext = symbol_extent(rows, symref_t.get(h_["id"]))
                who = "a flag" if h_["id"] not in byid.values() else "a part"
                if ext is None:
                    loose.append((who, b_["x"], b_["y"]))
                else:
                    r_ = (b_.get("rotation") or 0) % 360
                    cs = [R4Q[r_](x_, y_) for x_, y_ in
                          ((ext[0], ext[1]), (ext[2], ext[1]),
                           (ext[0], ext[3]), (ext[2], ext[3]))] \
                        if r_ in R4Q else [(0, 0)]
                    loose += [(who, b_["x"] + cx_, b_["y"] + cy_)
                              for cx_, cy_ in cs]
            elif (h_["type"] == "ATTR" and b_.get("valueVisible") is True
                  and b_.get("value")
                  and isinstance(b_.get("x"), (int, float))):
                w_ = len(str(b_["value"])) * 3.0
                if (b_.get("rotation") or 0) % 180:
                    loose += [("a label", b_["x"], b_["y"] - w_),
                              ("a label", b_["x"], b_["y"] + w_)]
                else:
                    loose += [("a label", b_["x"] - w_, b_["y"]),
                              ("a label", b_["x"] + w_, b_["y"])]
        # The picker measures what the checker checks. Sizing the sheet
        # from the component boxes alone and then testing every wire,
        # flag and label against the result is the same mismatch as
        # before, one level down: it put R30's ground symbol and its
        # lead below the frame and then refused the file it had just
        # sized.
        ax_ += [q[1] for q in loose]
        ay_ += [q[2] for q in loose]
        got = iso_sheet(min(ax_) - SAFE, min(ay_) - SAFE,
                        max(ax_) + SAFE, max(ay_) + SAFE)
        # A drawing office that fixes its format gets to say so, and
        # then the clip rule is what tells it the drawing does not fit.
        # It is also the only way to reach that rule from a test: with
        # the picker choosing, it sizes the sheet so nothing can fall
        # outside, which is the point of it.
        if a.sheet_size not in (None, "auto"):
            want_ = [z for z in ISO_AREAS if z[0] == a.sheet_size]
            if not want_:
                print(f"  error: --sheet-size {a.sheet_size} is not an ISO "
                      f"format\n", file=sys.stderr)
                return 1
            nm_, aw_, ah_ = want_[0]
            mx_ = (min(ax_) + max(ax_)) / 2
            my_ = (min(ay_) + max(ay_)) / 2
            got = (nm_, mx_ - aw_ / 2, my_ - ah_ / 2,
                   mx_ + aw_ / 2, my_ + ah_ / 2, *ISO_ZONES[nm_])
        if got is None:
            print(f"  error: the drawing is "
                  f"{(max(ax_) - min(ax_)) * 0.254:.0f} x "
                  f"{(max(ay_) - min(ay_)) * 0.254:.0f} mm and does not fit "
                  f"A0; nothing written\n", file=sys.stderr)
            return 1
        iso_name, fx0, fy0, fx1, fy1, ncol_, nrow_ = got
        size = iso_name
        # The title block goes flush into the bottom right corner of the
        # drawing area, which is where ISO 5457 puts it and where a
        # reader looks. Rewritten rather than re-placed: the records
        # already exist and only their coordinates change.
        band = 40          # the zone strip, inside the frame line
        if fields:
            # Inside the *inner* line. Anchoring on the outer frame put
            # the title block in the zone strip, over the row letters --
            # the strip is not drawing area, it is the margin the zone
            # marks live in.
            step_ = a.line_step or max(8, int(a.font_size * 1.6))
            for i, (_h, _b) in enumerate(fields):
                _b["x"] = int(fx1 - band - 12)
                _b["y"] = int(fy1 - band - 12
                              - (len(fields) - 1 - i) * step_)
                if _b.get("key") == "Sheet Size":
                    _b["value"] = f"Size: {iso_name}"
            tbw = max(len(str(_b.get("value", ""))) for _, _b in fields) \
                * a.font_size * 0.62
        segs = [(fx0, fy0, fx1, fy0), (fx1, fy0, fx1, fy1),
                (fx1, fy1, fx0, fy1), (fx0, fy1, fx0, fy0),
                # the second line that makes the zone strip a strip
                (fx0 + band, fy0 + band, fx1 - band, fy0 + band),
                (fx1 - band, fy0 + band, fx1 - band, fy1 - band),
                (fx1 - band, fy1 - band, fx0 + band, fy1 - band),
                (fx0 + band, fy1 - band, fx0 + band, fy0 + band)]
        # ... and the ticks that say where one field ends and the next
        # begins. Without them the letters are decoration: a reference
        # to "C4" cannot be resolved because the field has no boundary.
        for c in range(1, ncol_):
            gx = fx0 + band + c * (fx1 - fx0 - 2 * band) / ncol_
            segs += [(gx, fy0, gx, fy0 + band), (gx, fy1 - band, gx, fy1)]
        for r in range(1, nrow_):
            gy = fy0 + band + r * (fy1 - fy0 - 2 * band) / nrow_
            segs += [(fx0, gy, fx0 + band, gy), (fx1 - band, gy, fx1, gy)]
        # Each do-not-fit note gets its own box. EN ISO 7200 keeps the
        # title block for title-block data; a remark that shares its
        # rectangle interleaves with the fields -- "J4: not fitted"
        # reading across from "Owner: lucsoft" -- and belongs in a
        # bounded remarks field of its own.
        for d_, lines_, nx_, ny_ in dnf_block:
            nw_ = max(len(s) for s in lines_) * max(
                8, a.font_size - 6) * 0.62
            nh_ = (len(lines_) - 1) * 18
            bx0_, by0_ = nx_ - 10, ny_ + 26 - 12
            bx1_, by1_ = nx_ + nw_ + 10, ny_ + 26 + nh_ + 10
            segs += [(bx0_, by0_, bx1_, by0_), (bx1_, by0_, bx1_, by1_),
                     (bx1_, by1_, bx0_, by1_), (bx0_, by1_, bx0_, by0_)]

        # The title block's own box, flush into the inner corner. ISO
        # 7200 thinks in bounded data fields; nine right-aligned lines in
        # open space are not that.
        if fields:
            bx0_ = fx1 - band - 12 - tbw - 12
            by0_ = fy1 - band - 12 - (len(fields) - 1) * step_ - 14
            segs += [(bx0_, by0_, fx1 - band, by0_),
                     (fx1 - band, by0_, fx1 - band, fy1 - band),
                     (fx1 - band, fy1 - band, bx0_, fy1 - band),
                     (bx0_, fy1 - band, bx0_, by0_)]
        wid = hashlib.sha256(b"frame/iso").hexdigest()[:16]
        ticket += 1
        frame_recs.append((
            {"type": "WIRE", "ticket": ticket, "id": wid},
            {"groupId": "", "locked": False, "zIndex": 1}))
        for i, (x1_, y1_, x2_, y2_) in enumerate(segs):
            ticket += 1
            frame_recs.append((
                {"type": "LINE", "ticket": ticket,
                 "id": hashlib.sha256(f"iso/{i}".encode()).hexdigest()[:16]},
                {"lineGroup": wid, "startX": int(x1_), "startY": int(y1_),
                 "endX": int(x2_), "endY": int(y2_), "strokeColor": None,
                 "strokeStyle": None, "fillColor": None, "strokeWidth": 1,
                 "fillStyle": None}))
        # the zone letters and digits, centred in their field, in the strip
        host0 = byid[min(boxes, key=lambda d: boxes[d][0])]
        for c in range(ncol_):
            gx = fx0 + band + (c + 0.5) * (fx1 - fx0 - 2 * band) / ncol_
            for sfx, gy in (("t", fy0 + band / 2), ("b", fy1 - band / 2)):
                ticket += 1
                frame_recs.append(_mark(ticket, f"iso/c{c}{sfx}", host0,
                                        f"Zone c{c}{sfx}", str(c + 1),
                                        gx, gy, a.font_size - 2))
        for r in range(nrow_):
            gy = fy0 + band + (r + 0.5) * (fy1 - fy0 - 2 * band) / nrow_
            for sfx, gx in (("l", fx0 + band / 2), ("r", fx1 - band / 2)):
                ticket += 1
                frame_recs.append(_mark(ticket, f"iso/r{r}{sfx}", host0,
                                        f"Zone r{r}{sfx}",
                                        chr(ord("A") + r), gx, gy,
                                        a.font_size - 2))
        # --- nothing may leave the drawing area ---------------------------
        # ISO 5457 makes the zone band a coordinate reference, not
        # drawing space, and the title block a reserved field. A part
        # standing in either is cut off by the plotter. R29's ground
        # symbol came out 13 mm outside the sheet entirely and the tool
        # reported the run as finished.
        inner = (fx0 + band, fy0 + band, fx1 - band, fy1 - band)
        tb_rect = None
        if fields:
            tb_rect = (fx1 - band - 12 - tbw - 12,
                       fy1 - band - 12 - (len(fields) - 1) * step_ - 14,
                       fx1 - band, fy1 - band)
        out_of = []
        # Every canvas record, not only the component outlines. The
        # first version checked `boxes` -- symbol boxes, keyed by
        # designator -- and so never saw a wire, a ground symbol, a net
        # label or a note. R29's ground sat 60 units below the frame and
        # two leads crossed the zone band while the run reported itself
        # finished. A check that looks at one class of object and
        # reports on the sheet is the fail-open shape this project warns
        # about everywhere.
        # The boxes this tool draws itself count too. The remarks field
        # ran into the title block and the clip rule said nothing,
        # because it checked text and symbols and not the rectangles
        # drawn around them.
        for d_, lines_, nx_, ny_ in dnf_block:
            nw_ = max(len(s) for s in lines_) * max(8, a.font_size - 6) * 0.62
            nh_ = (len(lines_) - 1) * 18
            loose += [("a remarks box", nx_ - 10, ny_ + 26 - 12),
                      ("a remarks box", nx_ + nw_ + 10, ny_ + 26 + nh_ + 10)]

        for who, px_, py_ in loose:
            if not (inner[0] + SAFE <= px_ <= inner[2] - SAFE
                    and inner[1] + SAFE <= py_ <= inner[3] - SAFE):
                out_of.append((f"{who} at ({int(px_)}, {int(py_)})",
                               "outside the drawing area"))
            elif tb_rect and tb_rect[0] <= px_ <= tb_rect[2] \
                    and tb_rect[1] <= py_ <= tb_rect[3]:
                out_of.append((f"{who} at ({int(px_)}, {int(py_)})",
                               "in the title block"))
        for d, bx in sorted(boxes.items()):
            if not (inner[0] <= bx[0] and bx[2] <= inner[2]
                    and inner[1] <= bx[1] and bx[3] <= inner[3]):
                out_of.append((d, "outside the drawing area"))
            elif tb_rect and bx[0] < tb_rect[2] and tb_rect[0] < bx[2] \
                    and bx[1] < tb_rect[3] and tb_rect[1] < bx[3]:
                out_of.append((d, "in the title block"))
        for _h, _b in added:
            if _h.get("type") != "ATTR" or not isinstance(
                    _b.get("x"), (int, float)) or not _b.get("value"):
                continue
            if str(_b.get("key", "")).startswith(("Sheet ", "Zone ")):
                continue
            w_ = len(str(_b["value"])) * (_b.get("fontSize") or 16) * 0.62
            lx_ = (_b["x"] - w_ if _b.get("align") == "right" else
                   _b["x"] if _b.get("align") == "left" else _b["x"] - w_ / 2)
            if not (inner[0] <= lx_ and lx_ + w_ <= inner[2]
                    and inner[1] <= _b["y"] - 10
                    and _b["y"] + 10 <= inner[3]):
                out_of.append((str(_b["value"])[:28],
                               "outside the drawing area"))
        if out_of:
            for who, why in out_of[:8]:
                print(f"  OUTSIDE {who}: {why}", file=sys.stderr)
            print(f"\n  error: {len(out_of)} item(s) do not fit the "
                  f"{iso_name} drawing area; the zone band and the title "
                  f"block are reserved and a plotter cuts at the frame. "
                  f"Nothing written.\n", file=sys.stderr)
            return 1

        added += frame_recs
        print(f"  {iso_name} sheet: {(fx1 - fx0) * 0.254:.0f} x "
              f"{(fy1 - fy0) * 0.254:.0f} mm drawing area, "
              f"{ncol_} x {nrow_} zones, title block flush bottom right")
    elif a.frame and boxes:
        # Everything that has been added by now, not only the parts.
        # Measured from the components alone, the frame cut straight
        # through the cross-reference list and left the do-not-fit note
        # outside it -- on a drawing whose whole point is that the frame
        # says where the sheet ends.
        ax_ = [v[0] for v in boxes.values()] + [v[2] for v in boxes.values()]
        ay_ = [v[1] for v in boxes.values()] + [v[3] for v in boxes.values()]
        for _h, _b in added:
            if _h.get("type") != "ATTR" or not isinstance(
                    _b.get("x"), (int, float)):
                continue
            _w = len(str(_b.get("value", ""))) * _b.get("fontSize", 16) * 0.62
            if _b.get("align") == "right":
                ax_ += [_b["x"] - _w, _b["x"]]
            elif _b.get("align") == "left":
                ax_ += [_b["x"], _b["x"] + _w]
            else:
                ax_ += [_b["x"] - _w / 2, _b["x"] + _w / 2]
            ay_ += [_b["y"] - 12, _b["y"] + 12]
        fx0, fy0 = min(ax_) - a.frame, min(ay_) - a.frame
        fx1, fy1 = max(ax_) + a.frame, max(ay_) + a.frame
        # Every pin and stub end, so the frame can be proved clear of
        # them. A frame segment crossing a pin would be a connection,
        # and the net diff cannot see it -- the same hazard schwire
        # guards against, and it has to be guarded here too.
        pts = []
        for h, b, doc, _ in rows:
            if not h or not doc or doc[0] not in ("SCH", "SCH_PAGE"):
                continue
            for ax, ay in (("x", "y"), ("startX", "startY"),
                           ("endX", "endY")):
                if isinstance(b.get(ax), (int, float)) and \
                        isinstance(b.get(ay), (int, float)):
                    pts.append((b[ax], b[ay]))
        segs = [(fx0, fy0, fx1, fy0), (fx1, fy0, fx1, fy1),
                (fx1, fy1, fx0, fy1), (fx0, fy1, fx0, fy0)]
        if a.title_box and fields:
            tb = [v for v in (
                (min(f[1]["x"] - max(len(str(r[1])) for r in rows_) *
                     a.font_size * 0.62 for f in fields) - 20,
                 min(f[1]["y"] for f in fields) - 20,
                 max(f[1]["x"] for f in fields) + 20,
                 max(f[1]["y"] for f in fields) + 20),)][0]
            segs += [(tb[0], tb[1], tb[2], tb[1]),
                     (tb[2], tb[1], tb[2], tb[3]),
                     (tb[2], tb[3], tb[0], tb[3]),
                     (tb[0], tb[3], tb[0], tb[1])]
        near = []
        for x1_, y1_, x2_, y2_ in segs:
            lo_x, hi_x = min(x1_, x2_), max(x1_, x2_)
            lo_y, hi_y = min(y1_, y2_), max(y1_, y2_)
            for px, py in pts:
                if (lo_x - 4 <= px <= hi_x + 4
                        and lo_y - 4 <= py <= hi_y + 4):
                    near.append((px, py))
        if near:
            # Named, not counted. "One point lies on the frame" sends the
            # reader looking at a whole sheet; the coordinate and the
            # nearest designator send them to the part.
            for px, py in sorted(set(near))[:4]:
                who = min(boxes, key=lambda d: abs(
                    (boxes[d][0] + boxes[d][2]) / 2 - px) + abs(
                    (boxes[d][1] + boxes[d][3]) / 2 - py))
                print(f"  ON FRAME ({int(px)}, {int(py)}), nearest {who}",
                      file=sys.stderr)
            print(f"\n  error: {len(set(near))} existing point(s) lie on the "
                  f"frame; a frame segment through a pin is a connection the "
                  f"net diff cannot see — move the part or raise --frame\n",
                  file=sys.stderr)
            return 1
        wid = hashlib.sha256(b"frame/sheet").hexdigest()[:16]
        ticket += 1
        frame_recs.append((
            {"type": "WIRE", "ticket": ticket, "id": wid},
            {"groupId": "", "locked": False, "zIndex": 1}))
        for i, (x1_, y1_, x2_, y2_) in enumerate(segs):
            ticket += 1
            frame_recs.append((
                {"type": "LINE", "ticket": ticket,
                 "id": hashlib.sha256(f"frame/{i}".encode()).hexdigest()[:16]},
                {"lineGroup": wid, "startX": int(x1_), "startY": int(y1_),
                 "endX": int(x2_), "endY": int(y2_), "strokeColor": None,
                 "strokeStyle": None, "fillColor": None, "strokeWidth": 1,
                 "fillStyle": None}))
        added += frame_recs
        print(f"  {len(segs)}-segment frame, clear of all "
              f"{len(pts)} existing points"
              + (" (sheet and title block)" if a.title_box and fields
                 else ""))

    # --- the field grid ----------------------------------------------------
    # ISO 5457 wants a frame with centring marks and a field grid. The
    # frame is not drawable -- every LINE on this canvas is a wire -- but
    # the grid *marks* are text, and they are the half a cross-reference
    # needs: "sheet 3" on a 430 x 390 mm sheet is a search, "sheet 3 / B4"
    # is a place.
    grid_marks = []
    # Deliberately not the reference list. Its lines grow in the second
    # pass -- "sheet 3 (Control)" becomes "sheet 3 (Control) / B9" --
    # and letting that move the extent moves the grid, so the field a
    # reference cites comes from a different grid than the one printed
    # on the sheet it cites. The grid has to be the same in both passes
    # or a reference can never be right.
    # The reference list counts, but at a fixed width rather than its
    # real one. Its lines grow in the second pass -- "sheet 3 (Control)"
    # becomes "sheet 3 (Control) / B9" -- and letting that move the
    # extent moves the grid, so a reference would cite a field from a
    # different grid than the one printed. Excluding the list instead put
    # the marks on top of it. A fixed allowance does both: same grid in
    # both passes, and room reserved for the longest line.
    _fixed = [(h_, dict(b_, value="x" * 70)) for h_, b_ in xref]
    ext = _extent(rows, boxes,
                  [r for r in added if r not in xref] + _fixed)
    ncol, nrow = _grid_for(ext)
    host0 = byid[min(boxes, key=lambda d: boxes[d][0])]
    if a.field_grid:
        x0, y0, x1, y1 = ext
        # ISO 5457 puts the zone marks between the frame line and the
        # drawing area, so inside the frame rather than outside it. At 40
        # they set the sheet size on their own: this sheet measured A2
        # purely because of the margin its own marks claimed.
        pad = a.grid_pad
        for c in range(ncol):
            gx = x0 + (c + 0.5) * (x1 - x0) / ncol
            for side, gy in (("t", y0 - pad), ("b", y1 + pad)):
                ticket += 1
                grid_marks.append(_mark(ticket, f"zone/c{c}{side}", host0,
                                        f"Zone c{c}{side}", str(c + 1),
                                        gx, gy, a.font_size - 2))
        for r in range(nrow):
            gy = y0 + (r + 0.5) * (y1 - y0) / nrow
            for side, gx in (("l", x0 - pad), ("r", x1 + pad)):
                ticket += 1
                grid_marks.append(_mark(ticket, f"zone/r{r}{side}", host0,
                                        f"Zone r{r}{side}",
                                        chr(ord("A") + r),
                                        gx, gy, a.font_size - 2))
        # And measured, not assumed. The first version put a field
        # number inside the title block on all four sheets -- the one
        # zone a drawing standard protects -- because it took its extent
        # from the components and the title block was appended after.
        anc_ = {h["id"]: (b["x"], b["y"]) for h, b, _, _ in rows
                if h and h["type"] == "COMPONENT" and b.get("partId")
                and isinstance(b.get("x"), (int, float))}
        other = []
        for h, b in [(h, b) for h, b, _, _ in rows
                     if h and h["type"] == "ATTR"] + added:
            if not (b.get("valueVisible") is True and b.get("value")):
                continue
            bx_, by_ = b.get("x"), b.get("y")
            if not isinstance(bx_, (int, float)):
                bx_, by_ = anc_.get(b.get("parentId"), (None, None))
            if bx_ is None:
                continue
            w_ = len(str(b["value"])) * 3
            other.append((bx_ - w_, by_ - 5, bx_ + w_, by_ + 5))
        # Symbols too, not only text. The list ran through the ground
        # symbols of C36 and R52 on one sheet and C13 on another, and the
        # check reported clean because it asked text-against-text -- the
        # question whose answer was already yes.
        other += list(boxes.values())
        # A mark that lands on something gets pushed further out, not
        # refused. Refusing was right while the marks were silently
        # wrong; now that they are checked, moving them is the better
        # answer -- a sheet without a field grid helps nobody, and the
        # grid's job is only to be outside the drawing.
        cx_ = (ext[0] + ext[2]) / 2
        cy_ = (ext[1] + ext[3]) / 2

        def hits(bm):
            mw = len(bm["value"]) * 3
            m = (bm["x"] - mw, bm["y"] - 5, bm["x"] + mw, bm["y"] + 5)
            return any(m[0] < o[2] and o[0] < m[2]
                       and m[1] < o[3] and o[1] < m[3] for o in other)

        moved_, stuck_ = 0, 0
        for _hm, bm in grid_marks:
            if not hits(bm):
                continue
            horiz = str(bm["key"])[5] == "r"
            step = 20 * (1 if (bm["x"] if horiz else bm["y"])
                         > (cx_ if horiz else cy_) else -1)
            for _ in range(20):
                if horiz:
                    bm["x"] += step
                else:
                    bm["y"] += step
                if not hits(bm):
                    moved_ += 1
                    break
            else:
                stuck_ += 1
        # The size, measured against the outermost thing actually on the
        # sheet -- the marks themselves. Measuring the components and
        # adding an allowance was 63 mm short on one sheet, which put
        # "A3" in the title block of a 320 mm drawing. The marks are
        # placed last and furthest out, so their own boxes are the sheet.
        mx = [bm["x"] - len(bm["value"]) * 3 for _h, bm in grid_marks] +              [bm["x"] + len(bm["value"]) * 3 for _h, bm in grid_marks]
        my = [bm["y"] - 6 for _h, bm in grid_marks] +              [bm["y"] + 6 for _h, bm in grid_marks]
        if mx:
            fw = (max(mx + [ext[2]]) - min(mx + [ext[0]])) * 0.254
            fh = (max(my + [ext[3]]) - min(my + [ext[1]])) * 0.254
            real = next((n for n, w, h in AREAS
                         if (fw <= w and fh <= h) or (fw <= h and fh <= w)),
                        "larger than A0")
            for _h, bb in fields:
                if bb.get("key") == "Sheet Size":
                    bb["value"] = f"Size: {real}"
            if real != size:
                print(f"  {fw:.0f} x {fh:.0f} mm with the grid, so {real} "
                      f"and not {size} — the marks are the sheet's edge")
        if moved_ or stuck_:
            print(f"  {moved_} field mark(s) pushed clear of the drawing"
                  + (f", {stuck_} could not be" if stuck_ else ""))
        if stuck_:
            a.out.unlink(missing_ok=True) if a.out else None
            print(f"  error: {stuck_} field mark(s) cannot be placed clear; "
                  f"nothing written", file=sys.stderr)
            return 1
        added += grid_marks

    print(f"\n{a.export.name}")
    # Fail closed. A title nearer another block's part than its own says
    # something false about that part, and saying it quietly is how the
    # first version of this went out: "5 V -> 3.3 V" sat under the 5 V
    # converter's feedback divider and the tool reported three titles
    # placed.
    wrong_title = []
    for blk, _n, hub in placed:
        tx = boxes[hub][0]
        ty = members_boxes[blk][1] - a.above
        for rec_h, rec_b in added:
            if rec_b.get("key") == KEY and rec_b.get("value") == blk:
                tx, ty = rec_b["x"], rec_b["y"]
                break
        mine = min(abs((boxes[d][0] + boxes[d][2]) / 2 - tx)
                   + abs((boxes[d][1] + boxes[d][3]) / 2 - ty)
                   for d in members[blk])
        foreign = [(abs((boxes[d][0] + boxes[d][2]) / 2 - tx)
                    + abs((boxes[d][1] + boxes[d][3]) / 2 - ty), d)
                   for b2, ds2 in members.items() if b2 != blk for d in ds2]
        if foreign and min(foreign)[0] < mine:
            wrong_title.append((blk, min(foreign)[1]))
    if wrong_title:
        for blk, d in wrong_title[:6]:
            print(f"  MISLABELS {blk!r} sits nearer {d}, which is not in it",
                  file=sys.stderr)
        print(f"\n  error: {len(wrong_title)} block title(s) would name "
              f"another block's part; nothing written\n", file=sys.stderr)
        return 1

    print(f"  {len(placed)} block title(s), each above its block's hub")
    for blk, n, hub in placed:
        print(f"    {blk:38s} {n:3d} parts, anchored on {hub}")
    if grid_marks:
        print(f"  {ncol} x {nrow} field grid, {len(grid_marks)} marks")
    if xref:
        print(f"  {len(xref) - 1} signal(s) continued on another sheet, "
              f"listed top left")
    if fields:
        print(f"  {len(fields)}-line title block, bottom right")
    else:
        print(f"  no title block — give --sheet-title for one")
    if missing:
        print(f"  {len(missing)} block(s) have no component with geometry "
              f"and get no title: {', '.join(missing[:4])}")

    if a.dry_run:
        print("\n  --dry-run: nothing written\n")
        return 0

    last_sch = max(i for i, (h, _, doc, _) in enumerate(rows)
                   if h and doc and doc[0] in ("SCH_PAGE", "SCH"))
    out_lines = []
    for i, (h, b, doc, raw) in enumerate(rows):
        out_lines.append(raw)
        if i == last_sch:
            out_lines += [f"{json.dumps(hh, separators=(',', ':'))}||"
                          f"{json.dumps(bb, separators=(',', ':'))}|"
                          for hh, bb in added]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    src = zipfile.ZipFile(a.export)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    # --- the proof ---------------------------------------------------------
    # Append-only, checked: every original line survives, in order, with
    # exactly the expected number of additions. Anything else and the
    # output goes, because a title is not worth a changed record.
    _, back = load(a.out)
    if len(back) != len(rows) + len(added):
        a.out.unlink(missing_ok=True)
        print(f"\n  error: {len(back)} records out of {len(rows)} + "
              f"{len(added)}; {a.out} deleted\n", file=sys.stderr)
        return 1
    orig = [r for _, _, _, r in rows]
    kept = [r for _, _, _, r in back]
    newids = {h["id"] for h, _ in added}
    kept = [r for r in kept if not any(f'"{i}"' in r for i in newids)]
    if kept != orig:
        a.out.unlink(missing_ok=True)
        print(f"\n  error: an existing record changed; {a.out} deleted\n",
              file=sys.stderr)
        return 1
    print(f"  {len(added)} record(s) added, every existing one untouched")
    print(f"  wrote {a.out}")
    print(f"\n  whether EasyEDA keeps an attribute keyed {KEY!r} is not "
          f"answerable from here — import it and look\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
