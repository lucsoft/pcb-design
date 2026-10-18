#!/usr/bin/env python3
"""Regression tests for tools/schpreview.py.

The previewer exists so a layout can be judged without a round trip through
EasyEDA, which means every grade a reviewer gives a sheet is partly a grade
of this picture. Its failure mode is therefore not "it crashed" but "it
showed something the file does not say", and that has happened seven times:
net names left out, pin names left out, flag names left out, component
values left out, a library template's net name drawn as if it were on the
canvas, a closed symbol outline drawn open, and a pin drawn back through
its own connection point.

Every one of them is silent -- the preview renders fine either way -- so
nothing but a test on what the tool emits catches them, and a reviewer
looking at the PNG has no way to tell a value the file carries from one the
previewer forgot to draw.

The two geometric ones are the worst to catch by eye, because both invent a
connection the file does not have. An open diode triangle reads as a symbol
drawn that way, with its lead attached to an edge that is not there. A pin
drawn backwards walks a flag's stem out of its own symbol and onto whatever
sits one grid step away -- on the supply sheet, the ground flag beside the
catch diode, whose stem landed on the switch-node rail and read as a short.
Both are pictures of a broken circuit, and both had a file behind them that
was correct.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.axes                                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "schpreview.py"
sys.path.insert(0, str(ROOT / "tools"))
import schpreview                                          # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path):
    """A minimal export carrying each shape the previewer has to tell apart.

    `R1` holds its value in a formula; `U1` names itself through its
    manufacturer part; `f0` is a real flag on the canvas; and `part0` is a
    symbol *definition* with a `Global Net Name` of its own -- the template
    every instance of it copies, which carries real local coordinates and
    is not a placement.
    """
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})

    add({"type": "COMPONENT", "id": "c0"},
        {"partId": "P0.1", "x": 0, "y": 0, "rotation": 0, "isMirror": False})
    add({"type": "ATTR", "id": "d0"},
        {"key": "Designator", "value": "R1", "parentId": "c0",
         "valueVisible": True, "x": 0, "y": -10})
    add({"type": "ATTR", "id": "n0"},
        {"key": "Name", "value": "={Value}", "parentId": "c0",
         "valueVisible": True, "x": 0, "y": 10})
    add({"type": "ATTR", "id": "v0"},
        {"key": "Value", "value": "10kΩ", "parentId": "c0"})
    add({"type": "ATTR", "id": "dv0"},
        {"key": "Device", "value": "dev0", "parentId": "c0"})
    add({"type": "ATTR", "id": "sy0"},
        {"key": "Symbol", "value": "sym0", "parentId": "c0"})

    add({"type": "COMPONENT", "id": "c1"},
        {"partId": "P1.1", "x": 200, "y": 0, "rotation": 0,
         "isMirror": False})
    add({"type": "ATTR", "id": "d1"},
        {"key": "Designator", "value": "U1", "parentId": "c1",
         "valueVisible": True, "x": 200, "y": -10})
    add({"type": "ATTR", "id": "n1"},
        {"key": "Name", "value": "={Manufacturer Part}", "parentId": "c1",
         "valueVisible": True, "x": 200, "y": 10})
    # `Device` is the uuid of the device document, and it is the only
    # route to an attribute the instance has not copied. On a sheet that
    # has been through `fixparts --complete` the copy is always there, so
    # drop it here: the fallback is a real branch and this is the only
    # fixture that reaches it.
    add({"type": "ATTR", "id": "dv1"},
        {"key": "Device", "value": "dev0", "parentId": "c1"})
    add({"type": "ATTR", "id": "sy1"},
        {"key": "Symbol", "value": "sym1", "parentId": "c1"})

    # a placed rail flag: a component with no Designator
    add({"type": "COMPONENT", "id": "f0"},
        {"partId": "F0.1", "x": 400, "y": 0, "rotation": 0,
         "isMirror": False})
    add({"type": "ATTR", "id": "g0"},
        {"key": "Global Net Name", "value": "VBUS", "parentId": "f0",
         "valueVisible": True, "x": None, "y": None})

    # the symbol documents, one of which carries the phantom name
    for i in (0, 1):
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{i}"})
        add({"type": "RECT", "id": f"rc{i}"},
            {"dotX1": -10, "dotY1": -15, "dotX2": 10, "dotY2": 15})
        # Two pins pointing straight at the body, which is what the library
        # means by these rotations: 90 is -y and 270 is +y. They are the
        # ones a sign copied from the component rotation draws backwards,
        # and the body of every flag symbol on a real sheet is only one
        # grid step away -- so backwards is not a cosmetic difference, it
        # is the ground flag's stem landing on the rail beside it.
        add({"type": "PIN", "id": f"p90_{i}"},
            {"x": 0, "y": -20, "length": 10, "rotation": 90})
        add({"type": "PIN", "id": f"p270_{i}"},
            {"x": 0, "y": 20, "length": 10, "rotation": 270})
    # A closed outline, written the way EasyEDA writes one: three corners
    # and a repeat of the first. Nobody sets the record's `closed` field.
    add({"type": "POLY", "id": "tri0"},
        {"points": [{"x": 5, "y": -7}, {"x": -5, "y": 0}, {"x": -5, "y": 0},
                    {"x": 5, "y": 7}, {"x": 5, "y": 7}, {"x": 5, "y": -7}],
         "closed": False})
    # ...and an open one with three points, which must not gain an edge.
    add({"type": "POLY", "id": "zig0"},
        {"points": [{"x": -7, "y": -7}, {"x": -5, "y": -7},
                    {"x": -5, "y": 7}],
         "closed": False})
    add({"type": "PART", "id": "part0"}, {"partId": "P0.1"})
    add({"type": "ATTR", "id": "tp0"},
        {"key": "Global Net Name", "value": "GND", "parentId": "part0",
         "valueVisible": True, "x": 0, "y": 25})

    add({"type": "DOCHEAD"}, {"docType": "META", "uuid": "dev0"})
    add({"type": "META", "id": "m0"},
        {"attributes": {"Manufacturer Part": "RC0805FR-0710KL"}})

    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def drawn_text(path, tmp, region=None):
    """Everything `draw` puts on the canvas, as strings.

    Spy on `Axes.text` rather than re-implementing the walk: the point is
    to assert on what the real code path emits, and a copy of the walk
    would pass while the tool stayed wrong.
    """
    seen = []
    orig = matplotlib.axes.Axes.text

    def spy(self, x, y, s, *a, **k):
        seen.append(str(s))
        return orig(self, x, y, s, *a, **k)

    matplotlib.axes.Axes.text = spy
    try:
        rc = schpreview.draw(path, tmp / "preview.png", None, 60, region)
    finally:
        matplotlib.axes.Axes.text = orig
    return rc, seen


def drawn_polygons(path, tmp):
    """Every Polygon `draw` builds, as (vertices handed in, closed, patch).

    Spying on the constructor rather than on the finished canvas is what
    makes the shape identifiable: the points are the same, placed and
    rotated, and the closed flag is the one the tool chose -- so a test can
    say "the six-point outline" without depending on where it landed.
    """
    made, orig = [], schpreview.Polygon

    def spy(verts, *a, **k):
        p = orig(verts, *a, **k)
        made.append((list(verts), bool(k.get("closed")), p))
        return p

    schpreview.Polygon = spy
    try:
        schpreview.draw(path, tmp / "polygons.png", None, 60, None)
    finally:
        schpreview.Polygon = orig
    return made


def drawn_segments(path, tmp):
    """Every straight line `draw` puts on the canvas, as ((x, y), (x, y)).

    Screen coordinates, y already flipped the way the picture has it.
    """
    seen, orig = [], matplotlib.axes.Axes.add_line

    def spy(self, ln, *a, **k):
        xs, ys = ln.get_xdata(), ln.get_ydata()
        seen.append(((float(xs[0]), float(ys[0])),
                     (float(xs[-1]), float(ys[-1]))))
        return orig(self, ln, *a, **k)

    matplotlib.axes.Axes.add_line = spy
    try:
        schpreview.draw(path, tmp / "segments.png", None, 60, None)
    finally:
        matplotlib.axes.Axes.add_line = orig
    return seen


def edges(patch):
    """The segments `draw` actually puts on the canvas, as (start, end) pairs.

    The path is the honest source and the vertex list is not: matplotlib
    drops the last vertex of an *open* polygon when that vertex repeats the
    first, which is exactly the edge that goes missing.
    """
    path = matplotlib.path.Path
    pth = patch.get_path()
    codes = pth.codes
    if codes is None:                           # an unclosed polygon
        codes = [path.MOVETO] + [path.LINETO] * (len(pth.vertices) - 1)
    out, cur = [], None
    for code, xy in zip(codes, pth.vertices):
        code, xy = int(code), tuple(float(q) for q in xy)
        if code == path.MOVETO:
            cur = xy
        elif code == path.LINETO:
            out.append((cur, xy))
            cur = xy
        elif code == path.CLOSEPOLY and cur is not None:
            out.append((cur, xy))
    return out


def shut(segs):
    """True when the drawn segments return to where they started."""
    return len(segs) > 2 and segs[-1][1] == segs[0][0]


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "in.epro2"
    build(src)

    rc, seen = drawn_text(src, tmp)
    check("the previewer draws the sheet", rc == 0, rc)

    # The value lives in a formula on the `Name` attribute, and the file
    # stores the text on a different attribute entirely. A previewer that
    # drew only the designator left `R1` where the sheet reads `10kΩ`, and
    # a reviewer graded a divider they could not check.
    check("a component's value is drawn", "10kΩ" in seen, seen)
    check("...resolved through the device when the instance has no copy",
          "RC0805FR-0710KL" in seen, seen)

    # The inverse probe. `Global Net Name` also lives inside every symbol
    # definition that draws a flag, where it is the template, and it has
    # real local coordinates -- so an unguarded walk puts a name on the
    # canvas that the sheet does not have. Dropping the guard makes the
    # first of these fail and the second pass, which is the pair that says
    # the filter is doing something rather than matching nothing.
    check("a symbol definition's own net name is not drawn",
          "GND" not in seen, seen)
    check("...but a flag placed on the canvas still is", "VBUS" in seen, seen)

    # A closed outline arrives as a point list that repeats its first
    # point, and EasyEDA writes `closed: false` on every POLY it emits, so
    # the flag carries nothing. matplotlib's Polygon drops that repeated
    # last vertex from an unclosed path, which is the edge that shuts the
    # shape -- the diode triangle lost its base and the anode lead hung
    # off nothing. Six points in, five segments out is the fixed shape;
    # four is the base missing.
    polys = drawn_polygons(src, tmp)
    tri = [p for p in polys if len(p[0]) == 6]
    zig = [p for p in polys if len(p[0]) == 3]
    check("the fixture's closed and open outlines are both drawn",
          len(tri) == 1 and len(zig) == 1, (len(tri), len(zig)))
    if len(tri) == 1 and len(zig) == 1:
        check("the closed outline is drawn shut",
              tri[0][1] and shut(edges(tri[0][2])), edges(tri[0][2]))
        # The inverse probe: an open path that happens to be a closed
        # loop's prefix must not gain the closing edge, or the rule would
        # be "draw everything shut" -- which breaks every genuine polyline
        # in a symbol. Dropping the `shut` test above fails the check
        # before this one and leaves this passing, which is the pair.
        check("...and the open one is not, and gains no edge",
              not shut(edges(zig[0][2])) and len(edges(zig[0][2])) == 2,
              edges(zig[0][2]))

    # A pin's (x, y) is where a wire attaches and its line runs to the
    # body; its own rotation counts the opposite way round from a
    # component's, so 90 is -y and 270 is +y. Both pins here are handed a
    # body one grid step away, and drawn the wrong way round each one
    # reaches past its own symbol into whatever sits beside it.
    segs = drawn_segments(src, tmp)

    def has(a, b):
        # The angle goes through cos/sin, so 0.0 arrives as 6e-16.
        def near(p, q):
            return abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) < 1e-6
        return any((near(s[0], a) and near(s[1], b))
                   or (near(s[0], b) and near(s[1], a)) for s in segs)

    check("a 90-degree pin is drawn from its connection point to the body",
          has((0, 20), (0, 30)), segs)
    check("a 270-degree pin likewise", has((0, -20), (0, -30)), segs)
    # The inverse: the segment the reversed sign produces must be absent,
    # or the two checks above could pass on some other symbol's lead.
    check("...and neither is drawn back through its connection point",
          not has((0, 20), (0, 10)) and not has((0, -20), (0, -10)), segs)

    # A region is a canvas rectangle; malformed input must fail rather
    # than silently falling back to the whole sheet, which is what the
    # first version of the option did.
    rc, log = run(src, "-o", tmp / "r.png", "--region", "1,2,3")
    check("a malformed --region is refused", rc == 2, log)
    rc, log = run(src, "-o", tmp / "r.png", "--region", "0,0,100,80")
    check("...and a well-formed one renders", rc == 0, log)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all schpreview tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
