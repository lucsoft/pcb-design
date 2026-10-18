#!/usr/bin/env python3
"""Render an EasyEDA Pro schematic export to PNG, to look at it.

Without this, judging a layout means asking a human to open EasyEDA and
export an image — so every iteration costs a round trip and the loop that
layout work actually needs (change it, look at it, change it) does not
exist. Four layout variants were shipped in this session on numbers alone,
and two of them turned out to look worse.

It draws what the file says: each component's symbol primitives transformed
by its anchor and rotation, its pins, the wires on the canvas, and the
designator. It is a **previewer, not a renderer** — EasyEDA's own output is
authoritative and this exists to answer "is the arrangement sane" in one
look.

    schpreview.py <export.epro2> -o preview.png
    schpreview.py <export.epro2> -o preview.png --band "MCU"
    schpreview.py <export.epro2> -o corner.png --region 180,60,420,260

`--band` crops to a component's own extents; `--region` crops to a
rectangle of the canvas. They answer different questions -- the first
"what does this part look like", the second "what sits between these two"
-- and only the second is any use at a size the net names can be read at.


**More negative y is higher on the sheet** — the title block, always at the
bottom, sits at y = -230 while components span -1905 to -35. The y axis is
flipped on output so the picture matches the editor.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle, Ellipse, Polygon, Arc

ROT = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}

INK = "#1b1b1b"
WIRE = "#2f6f4f"
PIN = "#8a3b3b"
LABEL = "#3a5f8a"
NETTEXT = "#8a6a2f"
VALUE = "#5c5c5c"


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru, found {len(eprus)}")
    rows, cur = [], None
    for line in z.read(eprus[0]).decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        head, _, body = line.partition("||")
        try:
            h = json.loads(head)
            b = json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            continue
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        rows.append((h, b, cur))
    return rows


def place(ax, ay, rot, mirror, px, py):
    """A symbol-local point in canvas coordinates."""
    if mirror:
        px = -px
    dx, dy = ROT[rot % 360](px, py)
    return ax + dx, ay + dy


def circumcenter(x1, y1, x2, y2, x3, y3):
    """Centre and radius of the circle through three points, or None.

    None when the points are collinear, which is the arc's degenerate case:
    a straight "arc" has no centre and no sweep to draw.
    """
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return None
    a1, b1 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2
    a2 = x3 * x3 + y3 * y3
    ox = (a1 * (y2 - y3) + b1 * (y3 - y1) + a2 * (y1 - y2)) / d
    oy = (a1 * (x3 - x2) + b1 * (x1 - x3) + a2 * (x2 - x1)) / d
    return ox, oy, math.hypot(x1 - ox, y1 - oy)


def draw(path: Path, out: Path, band: str | None, dpi: int, region=None):
    rows = load(path)
    desig = {b["parentId"]: b["value"] for h, b, _ in rows
             if h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _ in rows
              if h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    dpos = {b["parentId"]: (b["x"], b["y"]) for h, b, _ in rows
            if h["type"] == "ATTR" and b.get("key") == "Designator"
            and b.get("valueVisible") is True
            and isinstance(b.get("x"), (int, float))}
    anchor = {h["id"]: (b["x"], b["y"]) for h, b, _ in rows
              if h["type"] == "COMPONENT" and b.get("partId")
              and isinstance(b.get("x"), (int, float))}
    sym = collections.defaultdict(list)
    for h, b, doc in rows:
        if doc and doc[0] == "SYMBOL":
            sym[doc[1]].append((h["type"], b))

    want = None
    if band:
        want = {d.strip() for d in band.split(",")}

    fig, ax = plt.subplots(figsize=(24, 24))
    xs, ys = [], []
    # component-only extents, so --band crops to what it drew rather than
    # to the whole sheet the wires still span
    cxs, cys = [], []

    def line(x1, y1, x2, y2, colour, lw, own=False):
        ax.add_line(Line2D([x1, x2], [-y1, -y2], color=colour, lw=lw,
                           solid_capstyle="round"))
        xs.extend((x1, x2)); ys.extend((-y1, -y2))
        if own:
            cxs.extend((x1, x2)); cys.extend((-y1, -y2))

    # --- canvas wires -----------------------------------------------------
    for h, b, doc in rows:
        if h["type"] == "LINE" and b.get("lineGroup") and doc and \
                doc[0] in ("SCH_PAGE", "SCH"):
            line(b["startX"], b["startY"], b["endX"], b["endY"], WIRE, 1.0)

    # --- components -------------------------------------------------------
    n = 0
    flagname = {b["parentId"]: b["value"] for h, b, _ in rows
                if h["type"] == "ATTR" and b.get("key") == "Name"
                and b.get("value")}
    for h, b, doc in rows:
        if h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        # A ground or supply flag is a component with no designator. Drawing
        # only designated ones hid all 117 of them, which is the opposite of
        # what a previewer is for.
        d = desig.get(h["id"]) or flagname.get(h["id"])
        if not d or d.endswith("?"):
            continue
        if want and d not in want and h["id"] in desig:
            continue
        if want and h["id"] not in desig:
            # a flag shows only if it sits inside the cropped view
            if not (cxs and min(cxs) - 120 <= b["x"] <= max(cxs) + 120
                    and min(cys) - 120 <= -b["y"] <= max(cys) + 120):
                continue
        n += 1
        cx, cy = b["x"], b["y"]
        r = (b.get("rotation") or 0) % 360
        m = bool(b.get("isMirror"))
        for kind, g in sym.get(symref.get(h["id"]), []):
            if kind == "RECT":
                p = [place(cx, cy, r, m, x, y) for x, y in
                     ((g["dotX1"], g["dotY1"]), (g["dotX2"], g["dotY1"]),
                      (g["dotX2"], g["dotY2"]), (g["dotX1"], g["dotY2"]))]
                ax.add_patch(Polygon([(x, -y) for x, y in p], closed=True,
                                     fill=False, ec=INK, lw=0.9))
                xs.extend(x for x, _ in p); ys.extend(-y for _, y in p)
                cxs.extend(x for x, _ in p); cys.extend(-y for _, y in p)
            elif kind == "POLY" and g.get("points"):
                p = [place(cx, cy, r, m, q["x"], q["y"]) for q in g["points"]]
                # A closed shape arrives as a point list that repeats its
                # first point; the record's own `closed` field is false on
                # every POLY in this format and says nothing. matplotlib's
                # Polygon drops the last vertex of an unclosed path when it
                # repeats the first, and that dropped vertex is the edge
                # that shuts the shape -- so the diode triangle, six points
                # in, came out as four segments and its base was missing,
                # leaving an open "Λ" with the anode lead hanging off
                # nothing. Six in, five out is the triangle.
                shut = len(p) > 2 and p[0] == p[-1]
                ax.add_patch(Polygon([(x, -y) for x, y in p], closed=shut,
                                     fill=False, ec=INK, lw=0.9))
                xs.extend(x for x, _ in p); ys.extend(-y for _, y in p)
                cxs.extend(x for x, _ in p); cys.extend(-y for _, y in p)
            elif kind == "ARC":
                # An ARC is three points -- start, a reference on the arc,
                # and end -- and the shape is the circle through them.
                # Nothing said that until an inductor chose to draw itself
                # in arcs: L2's four humps are ARC records while L1's are
                # POLY, so the second inductor came out as a bare wire with
                # a gap where its body should be, and the gaps in the
                # rail's line looked like a broken connection.
                #
                # Drawn as the circumcircle's span from start to end
                # *through* the reference, which is the only reading of the
                # three points that cannot pick the far side of the circle
                # by accident.
                p0 = place(cx, cy, r, m, g.get("startX", 0), g.get("startY", 0))
                p1 = place(cx, cy, r, m, g.get("referX", 0), g.get("referY", 0))
                p2 = place(cx, cy, r, m, g.get("endX", 0), g.get("endY", 0))
                cc = circumcenter(p0[0], -p0[1], p1[0], -p1[1], p2[0], -p2[1])
                if cc is None:
                    continue
                ox, oy, rad = cc
                t0 = math.degrees(math.atan2(-p0[1] - oy, p0[0] - ox))
                tm = math.degrees(math.atan2(-p1[1] - oy, p1[0] - ox))
                t2 = math.degrees(math.atan2(-p2[1] - oy, p2[0] - ox))
                # Sweep the way round that passes the reference; matplotlib
                # draws counter-clockwise from theta1 to theta2, so a
                # reference outside that span means the other direction.
                if (tm - t0) % 360 > (t2 - t0) % 360:
                    t0, t2 = t2, t0
                ax.add_patch(Arc((ox, oy), rad * 2, rad * 2, angle=0,
                                 theta1=t0, theta2=t2, ec=INK, lw=0.9))
                for k in range(25):
                    ta = math.radians(t0 + (t2 - t0) * k / 24.0)
                    sx, sy = ox + rad * math.cos(ta), oy + rad * math.sin(ta)
                    xs.append(sx); ys.append(sy)
                    cxs.append(sx); cys.append(sy)
            elif kind in ("ELLIPSE", "CIRCLE"):
                ex, ey = place(cx, cy, r, m, g.get("centerX", 0), g.get("centerY", 0))
                rx = abs(g.get("radiusX") or g.get("radius") or 1)
                ry = abs(g.get("radiusY") or g.get("radius") or 1)
                ax.add_patch(Ellipse((ex, -ey), rx * 2, ry * 2, fill=False,
                                     ec=INK, lw=0.9))
                xs.append(ex); ys.append(-ey)
            elif kind == "PIN":
                # The pin's (x, y) is where a wire attaches and the line
                # runs from there to the body, `length` long. Its own
                # rotation counts the other way round from a component's:
                # three library symbols settle it, and all three are drawn
                # backwards by the sign that reads like the component one.
                # The GND flag has its bars at +y and a 270 pin, the 5 V
                # flag its bar at -y and a 90 pin, and U4's top pin sits
                # at y=-35 outside a body that starts at -25 -- each pin
                # has to run toward its own body, so 90 is -y and 270 is
                # +y. Drawn the other way the ground flag's stem goes up
                # into the switch-node rail and reads as a short, and U4's
                # top pin sticks out into the air.
                px, py = g.get("x", 0), g.get("y", 0)
                L = g.get("length", 10) or 10
                a = math.radians((g.get("rotation") or 0))
                ex, ey = px + L * math.cos(a), py - L * math.sin(a)
                x1, y1 = place(cx, cy, r, m, px, py)
                x2, y2 = place(cx, cy, r, m, ex, ey)
                line(x1, y1, x2, y2, PIN, 0.8, own=True)
            elif kind == "ATTR" and g.get("key") in ("Pin Name",
                                                     "Pin Number") \
                    and g.get("valueVisible") is True and g.get("value") \
                    and isinstance(g.get("x"), (int, float)):
                # Pin names and numbers live in the SYMBOL document at
                # symbol-local coordinates, carry valueVisible true, and
                # are what EasyEDA draws inside every IC body. Leaving
                # them out made U4 a nameless rectangle: a reviewer could
                # not tell which pin was BOOT and capped the sheet's
                # readability at 1,8 for it. The file had all 253 of
                # them. Fourth time this previewer has decided a grade.
                tx, ty = place(cx, cy, r, m, g["x"], g["y"])
                ax.text(tx, -ty, str(g["value"]), fontsize=2.2,
                        color=PIN if g["key"] == "Pin Number" else INK,
                        ha="center", va="center", family="monospace")
        if h["id"] in desig:
            # Where the file puts it, not where this previewer guessed.
            # Drawing every designator at the anchor plus 22 put twenty
            # of them across their own part's lead on a sheet where the
            # records say otherwise -- and a reviewer marked the sheet
            # down for it. 23 of 23 carry coordinates here; the anchor
            # offset is the fallback for the ones that do not, which is
            # what EasyEDA itself does with them.
            dx_, dy_ = dpos.get(h["id"], (None, None))
            if dx_ is None:
                ax.text(cx, -cy + 22, d, fontsize=3.4, color=LABEL,
                        ha="center", va="bottom", family="monospace")
            else:
                ax.text(dx_, -dy_, d, fontsize=3.4, color=LABEL,
                        ha="center", va="center", family="monospace")

    # The component values, which are what a reader checks the circuit
    # against. The file does not store them as text: the `Name` attribute
    # holds a *formula* -- `={Value}` or `={Manufacturer Part}` -- and
    # EasyEDA evaluates it against the instance's own attributes. Drawing
    # only the designator left all 23 parts here as bare references, so a
    # reviewer saw `C8` where the sheet reads `4.7uF` and graded a
    # schematic they could not check the dividers on. That is the mirror
    # of the four earlier failures -- those flattered the input by hiding
    # net names and pin names; this one hid the values.
    inst_attr = collections.defaultdict(dict)
    for h, b, _ in rows:
        if h["type"] == "ATTR" and b.get("parentId") in desig:
            inst_attr[b["parentId"]][b.get("key")] = b.get("value")
    dev_attr = {}
    for h, b, doc in rows:
        if doc and doc[0] == "META" and isinstance(b.get("attributes"), dict):
            dev_attr[doc[1]] = b["attributes"]

    def shown_name(pid):
        """The `Name` formula, resolved the way EasyEDA resolves it."""
        raw = inst_attr[pid].get("Name") or ""
        if raw.startswith("={") and raw.endswith("}"):
            key = raw[2:-1]
            v = inst_attr[pid].get(key)
            if v is None:
                v = dev_attr.get(inst_attr[pid].get("Device"), {}).get(key)
            return v or ""
        return raw

    for h, b, doc in rows:
        if (h["type"] != "ATTR" or b.get("key") != "Name"
                or b.get("valueVisible") is False
                or b.get("parentId") not in desig
                or not isinstance(b.get("x"), (int, float))):
            continue
        txt = str(shown_name(b["parentId"]))
        if not txt or txt.endswith("?"):
            continue
        if want and not (cxs and min(cxs) - 120 <= b["x"] <= max(cxs) + 120
                         and min(cys) - 120 <= -b["y"] <= max(cys) + 120):
            continue
        ax.text(b["x"], -b["y"], txt, fontsize=2.8, color=VALUE,
                ha="center", va="center", family="monospace")

    # The net names, which are the bulk of what a reader actually sees.
    # Leaving them out made the preview look cleaner than the sheet: all
    # 441 have valueVisible true, so in the editor there are 441 more
    # strings than this picture used to show, 124 of them reading GND. A
    # previewer that flatters its input is worse than none.
    # Block titles, which carry their own x/y and a larger font. Drawn
    # before the net text so a label never sits on top of one.
    for h, b, doc in rows:
        if (h["type"] == "ATTR"
                and (b.get("key") == "Block"
                     or str(b.get("key", "")).startswith("Sheet ")
                     or str(b.get("key", "")).startswith("Xref ")
                     or str(b.get("key", "")).startswith("Zone ")
                     or str(b.get("key", "")).startswith("DNP ")
                     or str(b.get("key", "")).startswith("DNF "))
                and b.get("value")
                and b.get("valueVisible") is not False
                and isinstance(b.get("x"), (int, float))):
            if want and not (cxs and min(cxs) - 120 <= b["x"] <= max(cxs) + 120
                             and min(cys) - 120 <= -b["y"] <= max(cys) + 120):
                continue
            ax.text(b["x"], -b["y"], b["value"],
                    fontsize=(b.get("fontSize") or 16) * 0.42, color=INK,
                    ha={"left": "left", "center": "center",
                        "right": "right"}.get(b.get("align"), "center"),
                    va="bottom", family="monospace", fontweight="bold")
            xs.append(b["x"]); ys.append(-b["y"])

    # A flag says its net through `Global Net Name`, not through NET, and
    # leaving it out drew 226 identical bare bars -- a reviewer reading
    # this picture could not tell a 3V3 decoupler from a 37.8 V one and
    # said so. The file was right and the preview was not, which is the
    # one failure mode a previewer must not have.
    for h, b, doc in rows:
        # A flag's name carries no coordinates of its own -- `x` and `y`
        # are null and the position comes from the component it hangs on.
        # Requiring them drew none of the 90 supply names, and a reviewer
        # read the result as "every rail is the same unlabelled bar" and
        # marked the board down for it. The file was right and the
        # picture was not, which is the second time that has happened
        # here and the one failure a previewer must not have.
        # Only a flag on the canvas names a net. `Global Net Name` also
        # lives inside every symbol definition that draws one, where it is
        # the template the instance copies -- and it carries real local
        # coordinates, so drawing every one of them put a `GND` in zone A2
        # that the sheet does not have and a reviewer had to trace it back
        # to a `PART` document to find out it was this previewer's. Two
        # here, against 21 that are placed.
        if (h["type"] == "ATTR" and b.get("key") == "Global Net Name"
                and b.get("value") and b.get("valueVisible") is not False
                and b.get("parentId") in anchor):
            px = b["x"] if isinstance(b.get("x"), (int, float)) \
                else anchor.get(b.get("parentId"), (None, None))[0]
            py = b["y"] if isinstance(b.get("y"), (int, float)) \
                else anchor.get(b.get("parentId"), (None, None))[1]
            if px is None or py is None:
                continue
            # Clear of the symbol, not on it. The attribute carries no
            # coordinates -- EasyEDA places it -- so drawing it at the
            # component anchor puts every rail name on its own bar, and
            # a reviewer read that as 90 overprinted labels in the file.
            # The file says nothing of the sort. Offsetting is a guess
            # too, but it is the guess that does not invent a defect.
            b = dict(b, x=px + 26, y=py)
            if want and not (cxs and min(cxs) - 120 <= b["x"] <= max(cxs) + 120
                             and min(cys) - 120 <= -b["y"] <= max(cys) + 120):
                continue
            ax.text(b["x"], -b["y"] + 4, b["value"], fontsize=2.6,
                    color=NETTEXT, ha="center", va="bottom",
                    family="monospace", rotation=b.get("rotation") or 0)

    for h, b, doc in rows:
        if (h["type"] == "ATTR" and b.get("key") == "NET" and b.get("value")
                and b.get("valueVisible") is not False
                and isinstance(b.get("x"), (int, float))):
            if want and not (cxs and min(cxs) - 120 <= b["x"] <= max(cxs) + 120
                             and min(cys) - 120 <= -b["y"] <= max(cys) + 120):
                continue
            ax.text(b["x"], -b["y"] + 4, b["value"], fontsize=2.6,
                    color=NETTEXT, ha="center", va="bottom",
                    family="monospace", rotation=b.get("rotation") or 0)

    if not xs:
        print("error: nothing to draw", file=sys.stderr)
        return 1
    pad = 60
    vx, vy = (cxs, cys) if (want and cxs) else (xs, ys)
    if region:
        # A canvas rectangle, for reading one corner of the sheet at a
        # size the text is legible at. `--band` crops to a component's own
        # extents, which is the wrong box when the question is what sits
        # *between* two of them.
        rx0, ry0, rx1, ry1 = region
        ax.set_xlim(rx0, rx1)
        ax.set_ylim(-ry1, -ry0)
        vx, vy = [rx0, rx1], [-ry0, -ry1]
    else:
        ax.set_xlim(min(vx) - pad, max(vx) + pad)
        ax.set_ylim(min(vy) - pad, max(vy) + pad)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("#fdfdfb")
    out.parent.mkdir(parents=True, exist_ok=True)
    if region:
        # `bbox_inches="tight"` recomputes the frame from every artist, which
        # undoes the crop: the first version of this option set the limits and
        # still wrote the whole sheet, 6976 x 4921, because the off-region
        # text was an artist like any other. Pin the figure to the requested
        # rectangle instead, at a size the text is legible at.
        w_u, h_u = rx1 - rx0, ry1 - ry0
        scale = 11.0 / max(w_u, h_u)          # long side 11 in
        fig.set_size_inches(max(w_u * scale, 0.4), max(h_u * scale, 0.4))
        ax.set_position([0, 0, 1, 1])
        fig.savefig(out, dpi=dpi, facecolor=fig.get_facecolor())
    else:
        fig.savefig(out, dpi=dpi, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
    plt.close(fig)
    w = (max(vx) - min(vx)) * 0.254
    hgt = (max(vy) - min(vy)) * 0.254
    print(f"  {n} component(s), {int(max(vx)-min(vx))} x {int(max(vy)-min(vy))} "
          f"units = {w:.0f} x {hgt:.0f} mm")
    print(f"  wrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--band", help="only these designators, comma separated")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--region", metavar="X0,Y0,X1,Y1",
                    help="canvas rectangle to draw, for reading one corner "
                         "of the sheet at a legible size")
    a = ap.parse_args()
    region = None
    if a.region:
        try:
            region = tuple(float(v) for v in a.region.split(","))
        except ValueError:
            print("error: --region takes four numbers X0,Y0,X1,Y1",
                  file=sys.stderr)
            return 2
        if len(region) != 4:
            print("error: --region takes four numbers X0,Y0,X1,Y1",
                  file=sys.stderr)
            return 2
    if not a.export.exists():
        print(f"error: {a.export} not found", file=sys.stderr)
        return 2
    return draw(a.export, a.out, a.band, a.dpi, region)


if __name__ == "__main__":
    sys.exit(main())
