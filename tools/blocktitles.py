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
        gap_ = a.above
        while gap_ > 8:
            ty = y0 - gap_
            inside = any(bx0 <= (x0 + x1) / 2 <= bx1 and by0 <= ty <= by1
                         for ob, members in members_boxes.items()
                         if ob != blk
                         for bx0, by0, bx1, by1 in [members])
            if not inside:
                break
            gap_ //= 2
        ticket += 1
        added.append((
            {"type": "ATTR", "ticket": ticket,
             "id": hashlib.sha256(f"title/{blk}".encode()).hexdigest()[:16]},
            {"groupId": "", "locked": False, "zIndex": 1,
             "parentId": byid[hub], "key": KEY, "value": blk,
             "keyVisible": False, "valueVisible": True,
             # Left edge, not centre. A centred title over a wide block
             # lands above whatever happens to be in the middle, which on
             # this board put "Channel 1 eFuse" over the current-sense
             # row. The left edge is where a reader starts, and every
             # title sitting the same distance above its block makes the
             # row of them readable as a row.
             "x": int(x0), "y": int(y0 - gap_),
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
        for k, v in (("Doc no", a.doc_number), ("Sheet", a.sheet),
                     ("Rev", a.revision), ("Date", a.date),
                     ("Drawn", a.drawn_by), ("Approved", a.approved_by),
                     ("Size", size)):
            if v:
                rows_.append((k, v))
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
                 "x": int(bx1), "y": int(by1 + a.above + i * 30),
                 "rotation": 0, "color": None, "fillColor": None,
                 "fontFamily": None,
                 "fontSize": a.font_size if i == 0 else a.font_size - 4,
                 "strikeout": None, "underline": None, "italic": None,
                 "fontWeight": "bold" if i == 0 else None,
                 "align": "right", "version": "2.0"}))
        added += fields

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
        pad = 40
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
    print(f"  {len(placed)} block title(s), each on its block's widest part")
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
