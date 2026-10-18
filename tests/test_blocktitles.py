#!/usr/bin/env python3
"""Regression tests for tools/blocktitles.py.

The tool's one safety claim is that it only appends: twelve attributes
in, every existing record out unchanged and in order. That is checked
here and in the tool, because a title is not worth a changed record --
and the check in the tool is the one that deletes the output, so it has
to be exercised by something other than itself.
"""

import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "blocktitles.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


PARTS = [("U1", "Front"), ("C1", "Front"), ("U2", "Back")]


def build(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    nl = {f"gge{i + 1}": {"props": {"Designator": des, "Supplier Part": "C9"},
                          "pins": {"1": "GND"}}
          for i, (des, _) in enumerate(PARTS)}
    (d / "netlist.json").write_text(json.dumps(nl), encoding="utf-8")
    gen = []
    for sec in ("Front", "Back"):
        gen.append(f"    # ---- {sec} " + "-" * 30)
        for des, s in PARTS:
            if s == sec:
                gen.append(f'        ("{des}", "C9", "x", "y", {{}}),')
    (d / "netlist.py").write_text("\n".join(gen) + "\n", encoding="utf-8")

    rows, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        rows.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, (des, _) in enumerate(PARTS):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": "P.1", "x": i * 200, "y": 0, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{i}"},
            {"parentId": f"c{i}", "key": "Designator", "value": des})
        add({"type": "ATTR", "id": f"s{i}"},
            {"parentId": f"c{i}", "key": "Symbol", "value": "sym"})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "sym"})
    add({"type": "RECT", "id": "r"},
        {"dotX1": -20, "dotY1": -20, "dotX2": 20, "dotY2": 20})

    body = "\n".join(
        json.dumps(h, separators=(",", ":")) + "||"
        + json.dumps(b, separators=(",", ":")) + "|" for h, b in rows)
    with zipfile.ZipFile(d / "in.epro2", "w") as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", body)
    return d / "in.epro2", len(rows)


def build_at(d: Path, places):
    """Like build(), but the caller says where each part goes.

    The shared fixture puts every part in one row, so the bottom right
    corner is always occupied and the title block could only ever go
    below. That made the free-corner branch untestable with it.
    """
    d.mkdir(parents=True, exist_ok=True)
    nl = {f"gge{i + 1}": {"props": {"Designator": des, "Supplier Part": "C9"},
                          "pins": {"1": "GND"}}
          for i, (des, _, _) in enumerate(places)}
    (d / "netlist.json").write_text(json.dumps(nl), encoding="utf-8")
    gen = ["    # ---- Front " + "-" * 30]
    gen += [f'        ("{des}", "C9", "x", "y", {{}}),' for des, _, _ in places]
    (d / "netlist.py").write_text("\n".join(gen) + "\n", encoding="utf-8")

    rows, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        rows.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, (des, px, py) in enumerate(places):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": "P.1", "x": px, "y": py, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{i}"},
            {"parentId": f"c{i}", "key": "Designator", "value": des})
        add({"type": "ATTR", "id": f"s{i}"},
            {"parentId": f"c{i}", "key": "Symbol", "value": "sym"})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "sym"})
    add({"type": "RECT", "id": "r"},
        {"dotX1": -20, "dotY1": -20, "dotX2": 20, "dotY2": 20})
    body = "\n".join(
        json.dumps(h, separators=(",", ":")) + "||"
        + json.dumps(b, separators=(",", ":")) + "|" for h, b in rows)
    with zipfile.ZipFile(d / "in.epro2", "w") as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", body)
    return d / "in.epro2"


def read(path):
    z = zipfile.ZipFile(path)
    e = [n for n in z.namelist() if n.endswith(".epru")][0]
    return [l for l in z.read(e).decode("utf-8").split("\n") if l]


def _title_ys(path):
    """The y of every title-block line in a written sheet."""
    out = []
    for line in read(path):
        h, _, b = line.partition("||")
        try:
            hh, bb = json.loads(h), json.loads(b.rstrip("|"))
        except json.JSONDecodeError:
            continue
        if hh.get("type") == "ATTR" and str(bb.get("key", "")).startswith(
                "Sheet ") and isinstance(bb.get("y"), (int, float)):
            out.append(bb["y"])
    return out


def test_title_block_corner(tmp):
    """Inside the drawing area when the corner is free, below when not.

    Appending it below always costs height, and on a sheet whose bottom
    right quadrant is empty that was a whole paper format -- A2 for a
    drawing that fits A3 with the field beside it.
    """
    args = ["--sheet-title", "T", "--doc-number", "D", "--sheet", "1 of 1",
            "--revision", "A", "--date", "2026-01-01"]

    # an L: a row along the top and a column down the left, so the
    # bottom right is empty
    free = build_at(tmp / "free",
                    [("R1", 0, 0), ("R2", 400, 0), ("R3", 800, 0),
                     ("R4", 0, 400), ("R5", 0, 800)])
    rc, log = run(free, (tmp / "free") / "netlist.json",
                  *args, "-o", tmp / "free" / "out.epro2")
    # The coordinate, not the message. Checking only what the tool says
    # it did let both mutations through -- forcing the field above the
    # drawing and forcing it below were each invisible, because the
    # sentence is computed separately from the position.
    ys = _title_ys(tmp / "free" / "out.epro2")
    check("a free bottom right corner takes the title block",
          rc == 0 and ys and max(ys) <= 820, f"{log}\n{ys}")

    # a full rectangle: every corner occupied
    taken = build_at(tmp / "taken",
                     [("R1", 0, 0), ("R2", 400, 0), ("R3", 800, 0),
                      ("R4", 0, 400), ("R5", 400, 400), ("R6", 800, 400)])
    rc, log = run(taken, (tmp / "taken") / "netlist.json",
                  *args, "-o", tmp / "taken" / "out.epro2")
    ys2 = _title_ys(tmp / "taken" / "out.epro2")
    check("an occupied corner pushes it below the drawing",
          rc == 0 and ys2 and min(ys2) > 420, f"{log}\n{ys2}")


def test_iso_sheet(tmp):
    """The frame is a real ISO drawing area, not a box round the content.

    The probe measures the aspect ratio, because that is the property
    that failed: a frame hugging its content came out at 0.853 while the
    title block beside it said A3, and every ISO A format is 0.707.
    """
    args = ["--sheet-title", "T", "--doc-number", "D", "--sheet", "1 of 1",
            "--revision", "A", "--date", "2026-01-01", "--iso-sheet"]

    def frame_of(path):
        xs, ys = [], []
        for line in read(path):
            h, _, b = line.partition("||")
            try:
                hh, bb = json.loads(h), json.loads(b.rstrip("|"))
            except json.JSONDecodeError:
                continue
            if hh.get("type") == "LINE" and str(
                    bb.get("lineGroup", "")) and "startX" in bb:
                xs += [bb["startX"], bb["endX"]]
                ys += [bb["startY"], bb["endY"]]
        return (max(xs) - min(xs), max(ys) - min(ys)) if xs else (0, 0)

    small = build_at(tmp / "iso1", [("R1", 0, 0), ("R2", 300, 0),
                                    ("R3", 0, 200)])
    rc, log = run(small, (tmp / "iso1") / "netlist.json",
                  *args, "-o", tmp / "iso1" / "out.epro2")
    check("a small drawing gets an ISO sheet", rc == 0 and " sheet:" in log,
          log)
    if rc == 0:
        w, h = frame_of(tmp / "iso1" / "out.epro2")
        check("...whose frame has the ISO aspect ratio",
              w and abs(h / w - 0.707) < 0.02, f"{w} x {h} = {h / w:.3f}")
        check("...and the smallest format that holds it",
              "A4 sheet" in log, log)

    # Wider than A4's drawing area (1051 units), so the next format up.
    big = build_at(tmp / "iso2", [("R1", 0, 0), ("R2", 1400, 0),
                                  ("R3", 0, 300)])
    rc, log = run(big, (tmp / "iso2") / "netlist.json",
                  *args, "-o", tmp / "iso2" / "out.epro2")
    check("a drawing too wide for A4 moves up a format",
          rc == 0 and "A3 sheet" in log, log)
    if rc == 0:
        w, h = frame_of(tmp / "iso2" / "out.epro2")
        check("...and that frame is ISO-shaped too",
              w and abs(h / w - 0.707) < 0.02, f"{w} x {h} = {h / w:.3f}")


def test_clip_covers_everything(tmp):
    """Two separate things, which the first version conflated.

    The *picker* must size the sheet from everything the checker will
    test -- wires and flags included -- or it chooses a format and then
    refuses the file it just sized. The *checker* must still refuse a
    part that lands in the title block, which no format change fixes.

    The first probe put a stray wire 4000 units out, which exhausted
    the format table; the run stopped at the picker and the clip rule
    was never reached, so deleting it changed nothing and the probe
    passed anyway.
    """
    args = ["--sheet-title", "T", "--doc-number", "D", "--sheet", "1 of 1",
            "--revision", "A", "--date", "2026-01-01", "--iso-sheet"]

    def with_stray(path, dy, dx=0):
        import zipfile as _z
        src = _z.ZipFile(build_at(path.parent, [("R1", 0, 0), ("R2", 300, 0),
                                                ("R3", 0, 200)]))
        body = src.read("doc.epru").decode("utf-8").split("\n")
        if dy or dx:
            body.insert(1, json.dumps({"type": "WIRE", "ticket": 900,
                                       "id": "stray"}, separators=(",", ":"))
                        + "||" + json.dumps({"groupId": ""},
                                            separators=(",", ":")) + "|")
            body.insert(2, json.dumps({"type": "LINE", "ticket": 901,
                                       "id": "strayl"},
                                      separators=(",", ":")) + "||"
                        + json.dumps({"lineGroup": "stray",
                                      "startX": dx, "startY": dy,
                                      "endX": dx + 50, "endY": dy},
                                     separators=(",", ":"))
                        + "|")
        with _z.ZipFile(path, "w") as z:
            z.writestr("project2.json", src.read("project2.json"))
            z.writestr("doc.epru", "\n".join(body))
        return path

    def fmt(log):
        m_ = re.search(r"(A[0-4]) sheet", log)
        return m_.group(1) if m_ else None

    d1 = tmp / "nostray"
    d1.mkdir(parents=True, exist_ok=True)
    rc, log1 = run(with_stray(d1 / "probe.epro2", 0), d1 / "netlist.json",
                   *args, "-o", d1 / "out.epro2")
    check("a small drawing is written", rc == 0, log1)

    # A wire 900 units below the parts: still a real format, but a
    # bigger one. The picker must see it.
    d2 = tmp / "stray"
    d2.mkdir(parents=True, exist_ok=True)
    rc, log2 = run(with_stray(d2 / "probe.epro2", 900), d2 / "netlist.json",
                   *args, "-o", d2 / "out.epro2")
    check("a stray wire makes the picker choose a bigger sheet",
          rc == 0 and fmt(log1) != fmt(log2),
          f"{fmt(log1)} -> {fmt(log2)}\n{log2}")

    # The same in x. One axis is not two: with only a vertical stray
    # the x half of the picker could be deleted and nothing noticed.
    d2b = tmp / "strayx"
    d2b.mkdir(parents=True, exist_ok=True)
    rc, log2b = run(with_stray(d2b / "probe.epro2", 0, 1400),
                    d2b / "netlist.json", *args, "-o", d2b / "out.epro2")
    check("a stray wire far to the right does the same",
          rc == 0 and fmt(log1) != fmt(log2b),
          f"{fmt(log1)} -> {fmt(log2b)}\n{log2b}")

    # And the checker itself: a part in the bottom right corner lands in
    # the title block, which growing the sheet does not fix.
    d3 = tmp / "intb"
    # Nearly the whole of A4's usable area, so the title block really is
    # where the corner part is. With a small drawing the sheet simply
    # grows and the part ends up beside the field, not in it -- which is
    # why the first version of this probe never fired.
    corner = build_at(d3, [("R1", 0, 0), ("R2", 930, 0), ("R3", 0, 620),
                           ("R4", 910, 600)])
    # A4 forced: the picker would pick A3 and then nothing could fall
    # outside, which is exactly what makes the clip rule unreachable
    # when the picker is right.
    rc, log3 = run(corner, d3 / "netlist.json", *args,
                   "--sheet-size", "A4", "-o", d3 / "out.epro2")
    check("a part in the title block stops the run",
          rc != 0 and "title block" in log3, log3)


def _xref_fixture(d: Path, with_flag: bool):
    """A two-sheet design and a drawing with room for the cross-sheet list.

    Three parts stacked vertically, so the list has several candidate
    rows to choose from, and one net that also lands on the other sheet,
    which is what makes it cross.
    """
    d.mkdir(parents=True, exist_ok=True)
    sheets = d / "sheets"
    (sheets / "this").mkdir(parents=True, exist_ok=True)
    (sheets / "other").mkdir(parents=True, exist_ok=True)

    (d / "design.yaml").write_text(
        "sheets:\n  This: [Front]\n  Other: [Back]\n", encoding="utf-8")

    nl = {f"gge{i + 1}": {"props": {"Designator": des, "Supplier Part": "C9"},
                          "pins": {"1": "GND"}}
          for i, des in enumerate(("R1", "R2", "R3"))}
    (sheets / "this" / "netlist.json").write_text(json.dumps(nl),
                                                  encoding="utf-8")
    (sheets / "this" / "netlist.py").write_text(
        "    # ---- Front " + "-" * 30 + "\n"
        + '        ("R1", "C9", "x", "y", {}),\n'
        + '        ("R2", "C9", "x", "y", {}),\n'
        + '        ("R3", "C9", "x", "y", {}),\n', encoding="utf-8")
    (sheets / "other" / "netlist.json").write_text(json.dumps(
        {"gge1": {"props": {"Designator": "U9", "Supplier Part": "C9"},
                  "pins": {"1": "GND"}}}), encoding="utf-8")

    rows, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        rows.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, py in enumerate((0, 200, 400)):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": "P.1", "x": 0, "y": py, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{i}"},
            {"parentId": f"c{i}", "key": "Designator", "value": f"R{i + 1}"})
        add({"type": "ATTR", "id": f"s{i}"},
            {"parentId": f"c{i}", "key": "Symbol", "value": "sym"})
    if with_flag:
        # A net flag: a COMPONENT with a partId and *no Designator*, which
        # is exactly what `_symbol_boxes` drops. It sits where the list
        # lands when the flag is invisible -- 25 units to the right of the
        # list's left edge, on the third candidate row.
        add({"type": "COMPONENT", "id": "f0"},
            {"partId": "FLAG.1", "x": 25, "y": 316, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": "fs0"},
            {"parentId": "f0", "key": "Symbol", "value": "flagsym"})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "sym"})
    add({"type": "RECT", "id": "r"},
        {"dotX1": -20, "dotY1": -20, "dotX2": 20, "dotY2": 20})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "flagsym"})
    add({"type": "RECT", "id": "fr"},
        {"dotX1": -5, "dotY1": -6, "dotX2": 5, "dotY2": 6})

    body = "\n".join(
        json.dumps(h, separators=(",", ":")) + "||"
        + json.dumps(b, separators=(",", ":")) + "|" for h, b in rows)
    with zipfile.ZipFile(d / "in.epro2", "w") as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", body)
    return d / "in.epro2"


def _xref_lines(path):
    """(x, y, value) for every cross-sheet list line in a written sheet."""
    out = []
    for line in read(path):
        h, _, b = line.partition("||")
        try:
            hh, bb = json.loads(h), json.loads(b.rstrip("|"))
        except json.JSONDecodeError:
            continue
        if (hh.get("type") == "ATTR"
                and str(bb.get("key", "")).startswith("Xref")
                and isinstance(bb.get("x"), (int, float))):
            out.append((bb["x"], bb["y"], str(bb.get("value", ""))))
    return out


def test_xref_avoids_flags(tmp):
    """The cross-sheet list must not land on a net flag.

    Two fail-opens hid the same collision and the sheet carried it: the
    flags are absent from `boxes`, because `_symbol_boxes` drops every
    component without a Designator, and the reserved width came from the
    net *names* rather than the lines, so a 77-unit box stood for 213
    units of text. Either one alone leaves the list where it was.
    """
    args = ["--sheet-title", "T", "--doc-number", "D", "--sheet", "1 of 2",
            "--revision", "A", "--date", "2026-01-01"]

    d1 = tmp / "xref-noflag"
    src1 = _xref_fixture(d1, with_flag=False)
    rc1, log1 = run(src1, d1 / "sheets" / "this" / "netlist.json", *args,
                    "--xref-sheets", d1 / "sheets", "--this-sheet", "this",
                    "-o", d1 / "out.epro2")
    check("a cross-sheet list is written", rc1 == 0, log1)
    plain = _xref_lines(d1 / "out.epro2")
    check("...with a line per crossing signal", len(plain) == 2, str(plain))

    d2 = tmp / "xref-flag"
    src2 = _xref_fixture(d2, with_flag=True)
    rc2, log2 = run(src2, d2 / "sheets" / "this" / "netlist.json", *args,
                    "--xref-sheets", d2 / "sheets", "--this-sheet", "this",
                    "-o", d2 / "out.epro2")
    check("...and it is written with a flag in the way", rc2 == 0, log2)
    moved = _xref_lines(d2 / "out.epro2")

    # The flag's own box, as `net_flag_boxes` computes it: a 10 x 12 RECT
    # at (25, 316), rotation 0.
    fx0, fy0, fx1, fy1 = 20, 310, 30, 322

    def hits(x, y, value):
        # The same conservative 3 units per character the tool reserves
        # with, so the check is not looser than the placement.
        return (x - 10) < fx1 and fx0 < (x + len(value) * 3) \
            and (y - 8) < fy1 and fy0 < (y + 8)

    bad = [l for l in moved if hits(*l)]
    check("...and it does not sit on the flag", not bad, str(bad))

    # And the flag is what moved it: with the flag invisible the list
    # keeps the row it would have had anyway, and the probe above passes
    # for the wrong reason. This is the half that fails on the old code.
    check("...which the flag changes, rather than being luck",
          [l[1] for l in moved] != [l[1] for l in plain],
          f"{[l[1] for l in plain]} vs {[l[1] for l in moved]}")


def main():
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "a"
    src, n_in = build(d)
    out = d / "out.epro2"
    rc, log = run(src, d / "netlist.json", "-o", out)
    check("a title is placed for every block", rc == 0 and "2 block" in log, log)
    check("...and it names them", "Front" in log and "Back" in log, log)
    check("...and says which part each is anchored on",
          "anchored on U1" in log or "anchored on C1" in log, log)

    if out.exists():
        before, after = read(src), read(out)
        check("...adding exactly one record per block",
              len(after) == len(before) + 2, f"{len(before)} -> {len(after)}")
        # every original line survives, in order
        titles = [l for l in after if '"key":"Block"' in l]
        rest = [l for l in after if '"key":"Block"' not in l]
        check("...and leaving every existing record untouched",
              rest == before)
        check("...with the block name as the visible value",
              len(titles) == 2
              and all('"valueVisible":true' in l for l in titles),
              str(titles[:1]))
        check("...and coordinates, which an unpositioned attribute lacks",
              all('"x":' in l and '"y":' in l for l in titles))

    # --- the title block --------------------------------------------------
    # EN ISO 7200 wants a drawing to say what it is. The border it
    # normally sits in is not drawable here -- every LINE is a wire --
    # but the text is, and the text is the part the standard asks for.
    d3 = tmp / "c"
    src3, _ = build(d3)
    o3 = d3 / "tb.epro2"
    rc, log = run(src3, d3 / "netlist.json", "--sheet-title", "Demo",
                  "--sheet", "1 of 2", "--revision", "B",
                  "--date", "2026-01-01", "-o", o3)
    check("a title block is written when asked for",
          rc == 0 and "title block" in log, log)
    # The paper size is derived, never taken: quoting the bare extent is
    # how two of four sheets came to say A3 when they needed A2, and the
    # design record warns about exactly that. It is a floor, because the
    # flag names have no coordinates and EasyEDA places them.
    check("...and the paper size is derived from the drawing extent",
          "of drawing, so at least" in log, log)
    check("...and said to be a floor, not a measurement",
          "a floor" in log, log)
    if o3.exists():
        lines = read(o3)
        tb = [l for l in lines if '"key":"Sheet ' in l]
        check("...as right-aligned, positioned strings",
              len(tb) >= 4 and all('"align":"right"' in l for l in tb),
              str(len(tb)))
        check("...carrying what the standard asks for",
              any("Demo" in l for l in tb) and any("1 of 2" in l for l in tb)
              and any("B" in l for l in tb))

    rc, log = run(src3, d3 / "netlist.json", "-o", d3 / "none.epro2")
    check("...and none when not, with the flag named",
          rc == 0 and "--sheet-title" in log, log)

    # The date is given, never generated: a date this tool invented would
    # be the date it ran, which is not when the drawing was made.
    check("...and the date is an argument, not a clock read",
          "--date" in (TOOL.read_text()))

    # --- break probe: no blocks at all ------------------------------------
    d2 = tmp / "b"
    src2, _ = build(d2)
    (d2 / "netlist.py").unlink()
    (d2 / "design.yaml").write_text("rails:\n  GND:\n    type: ground\n",
                                    encoding="utf-8")
    rc, log = run(src2, d2 / "netlist.json", "-o", d2 / "out.epro2")
    check("a netlist with no blocks is refused", rc == 1, log)
    check("...and says where blocks come from", "design.yaml" in log, log)
    check("...and writes nothing", not (d2 / "out.epro2").exists())

    test_title_block_corner(tmp)
    test_iso_sheet(tmp)
    test_clip_covers_everything(tmp)
    test_xref_avoids_flags(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all blocktitles tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
