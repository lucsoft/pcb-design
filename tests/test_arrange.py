#!/usr/bin/env python3
"""Regression tests for tools/arrange.py.

The tool's safety argument is that it only ever translates: every coordinate
inside a cell moves by the same delta, so every wire endpoint that met a pin
still meets it. That argument is only as good as the classification of which
records are canvas placements -- and the first version got that wrong.

It treated any payload carrying an x and a y as canvas geometry. An `.epru`
mixes two coordinate spaces in one stream, and a PIN record's x/y is a
position *inside a symbol definition*, shared by every instance of the part.
So 1040 library records were dragged along with J1, which would have moved
pins inside their symbols board-wide -- with every net name still in place
and `epro.py diff` still reporting the export identical.

That is why `test_library_records_do_not_move` exists and why it is the first
test in the file. The net diff cannot see this failure; only comparing
geometry can.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARRANGE = ROOT / "tools" / "arrange.py"
EPRO = ROOT / "tools" / "epro.py"

CW, CH = 300, 200
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path, comps, extra=(), stub_offset=(-20, 30), stub_len=30):
    """Write an .epro2 shaped like a netlist import.

    `comps` is [(designator, col, row, [net, ...])]. Each net becomes a wire
    stub inside the component's cell, named by an ATTR, exactly as the
    importer emits it.
    """
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    for d, col, row, nets in comps:
        cid = f"c{d}"
        cx, cy = 20 + col * CW, -1620 + row * CH
        add({"type": "COMPONENT", "id": cid},
            {"partId": f"SYM_{d}.1", "x": cx, "y": cy, "rotation": 0,
             "isMirror": False, "attrs": {}, "zIndex": 1})
        add({"type": "ATTR", "id": f"a{d}"},
            {"key": "Designator", "value": d, "parentId": cid,
             "x": cx, "y": cy + 40})
        for i, net in enumerate(nets):
            wid = f"w{d}_{i}"
            sx, sy = cx + stub_offset[0], cy + stub_offset[1] - i * 10
            add({"type": "WIRE", "id": wid},
                {"groupId": "", "locked": False, "zIndex": 2})
            add({"type": "LINE", "id": f"l{d}_{i}"},
                {"lineGroup": wid, "startX": sx, "startY": sy,
                 "endX": sx + stub_len, "endY": sy, "strokeWidth": 1})
            add({"type": "ATTR", "id": f"n{d}_{i}"},
                {"key": "NET", "value": net, "parentId": wid,
                 "x": sx + stub_len // 2, "y": sy})
    for h, b in extra:
        add(h, b)

    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def write_design(d, comps, sections):
    """A netlist.json plus the netlist.py whose sections arrange.py reads."""
    net, n = {}, 0
    for des, _, _, nets in comps:
        n += 1
        net[f"gge{n}"] = {"props": {"Designator": des, "Supplier Part": "C1"},
                          "pins": {str(i + 1): v for i, v in enumerate(nets)}}
    (d / "netlist.json").write_text(json.dumps(net), encoding="utf-8")

    src = ["COMPONENTS = ["]
    for name, members in sections:
        src.append(f"    # ---- {name} " + "-" * 20)
        for m in members:
            src.append(f'    ("{m}", "C1", "X", "v", {{}}),')
    src.append("]")
    (d / "netlist.py").write_text("\n".join(src), encoding="utf-8")


def run(*args):
    p = subprocess.run([sys.executable, str(ARRANGE), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def read_records(path):
    z = zipfile.ZipFile(path)
    out = []
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        out.append((json.loads(h), json.loads(b.rstrip("|"))))
    return out


def main():
    tmp = Path(tempfile.mkdtemp())

    # Four components, two blocks, laid out so arrange has to move them.
    # Two rows, because a grid cannot be inferred from one: with a single
    # distinct y there is no row height to read off, and guessing one would
    # be the sort of silent assumption this tool exists to refuse.
    comps = [("R1", 0, 3, ["GND", "VCC"]),
             ("R2", 1, 3, ["VCC", "SDA"]),
             ("C1", 0, 2, ["GND", "SDA"]),
             ("U1", 1, 2, ["GND", "VCC", "SDA"])]
    sections = [("Power", ["R1", "C1"]), ("Signal", ["R2", "U1"])]

    # --- the inverse probe, first ----------------------------------------
    # A symbol-definition PIN at a small coordinate, in the same numeric
    # range as the canvas. It must come out untouched.
    lib = [({"type": "PIN", "id": "p1"},
            {"partId": "SYM_R1.1", "x": 10, "y": 50, "length": 10,
             "rotation": 90, "groupId": ""}),
           ({"type": "META", "id": "m1"},
            {"title": "SYM_R1", "x": 0, "y": 0, "attributes": {}})]

    d = tmp / "lib"; d.mkdir()
    src = d / "in.epro2"
    build(src, comps, extra=lib)
    write_design(d, comps, sections)
    out = d / "out.epro2"
    rc, log = run(src, d / "netlist.json", "-o", out)
    check("arrange succeeds", rc == 0, log)

    if out.exists():
        got = {h["id"]: b for h, b in read_records(out)}
        check("test_library_records_do_not_move: PIN keeps its symbol-local x/y",
              got.get("p1", {}).get("x") == 10 and got["p1"]["y"] == 50,
              str(got.get("p1")))
        check("...and so does a META record at the origin",
              got.get("m1", {}).get("x") == 0 and got["m1"]["y"] == 0,
              str(got.get("m1")))
        check("the log separates canvas from library records",
              "library records left alone" in log, log)

        # --- the translation really is rigid -----------------------------
        was = {h["id"]: b for h, b in read_records(src)}
        rigid = True
        for des, _, _, nets in comps:
            cb, ca = was[f"c{des}"], got[f"c{des}"]
            dx, dy = ca["x"] - cb["x"], ca["y"] - cb["y"]
            for i in range(len(nets)):
                lb, la = was[f"l{des}_{i}"], got[f"l{des}_{i}"]
                if (la["startX"] - lb["startX"], la["startY"] - lb["startY"]) != (dx, dy):
                    rigid = False
                nb, na = was[f"n{des}_{i}"], got[f"n{des}_{i}"]
                if (na["x"] - nb["x"], na["y"] - nb["y"]) != (dx, dy):
                    rigid = False
        check("every wire and label moves by its component's delta", rigid)

        rc2, out2 = subprocess.run(
            [sys.executable, str(EPRO), "diff", str(out), str(d / "netlist.json")],
            capture_output=True, text=True).returncode, ""
        check("the arranged file still diffs clean against the netlist", rc2 == 0)

    # --- break probe: two components in one cell --------------------------
    d = tmp / "clash"; d.mkdir()
    bad = [("R1", 0, 3, ["GND"]), ("R2", 0, 3, ["VCC"]),
           ("C1", 1, 3, ["GND"]), ("U1", 0, 2, ["VCC"])]
    build(d / "in.epro2", bad)
    write_design(d, bad, [("Power", ["R1", "R2", "C1", "U1"])])
    rc, log = run(d / "in.epro2", d / "netlist.json", "-o", d / "out.epro2")
    check("two components in one cell is refused", rc == 1 and "CLASH" in log, log)
    check("...and nothing is written", not (d / "out.epro2").exists())

    # --- break probe: a wire crossing a cell boundary ---------------------
    d = tmp / "span"; d.mkdir()
    # Long enough to start inside the cell and end in the next one. A stub
    # that merely sits wholly in a neighbour's cell is a different fault and
    # the tool reports it differently (EXTRA/ORPHAN), so this probe has to
    # genuinely straddle the boundary or it exercises the wrong branch.
    build(d / "in.epro2", comps, stub_len=400)
    write_design(d, comps, sections)
    rc, log = run(d / "in.epro2", d / "netlist.json", "-o", d / "out.epro2")
    check("a wire crossing a cell is refused", rc == 1 and "SPANS" in log, log)
    check("...and nothing is written", not (d / "out.epro2").exists())

    # --- break probe: a designator in no block ----------------------------
    d = tmp / "unassigned"; d.mkdir()
    build(d / "in.epro2", comps)
    write_design(d, comps, [("Power", ["R1", "C1"])])      # R2, U1 left out
    rc, log = run(d / "in.epro2", d / "netlist.json", "-o", d / "out.epro2")
    check("an unassigned designator is refused", rc != 0 and "no block" in log, log)
    check("...and names which ones", "R2" in log and "U1" in log, log)
    check("...and nothing is written", not (d / "out.epro2").exists())

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all arrange tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
