#!/usr/bin/env python3
"""Regression tests for tools/layoutmetrics.py.

These four numbers exist because the five the project already had were
all zero bounds -- they count how often a distance is too small, and a
sheet can be unreadable entirely by distances that are too large. So
the probes here are the mirror image of the usual ones: each metric is
shown to **rise** on a deliberately spread-out fixture and stay low on a
tight one. A metric that only ever reads zero would pass a one-sided
test and report nothing useful about a real sheet.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "layoutmetrics.py"
sys.path.insert(0, str(ROOT / "tests"))
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path, parts, wires=(), flags=()):
    """parts: [(designator, x, y, [(dx, dy, net)])]; wires: [(net, [segs])]"""
    import hashlib
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, (d, cx, cy, pins) in enumerate(parts):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": f"P{i}.1", "x": cx, "y": cy, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"dg{i}"},
            {"key": "Designator", "value": d, "parentId": f"c{i}"})
        add({"type": "ATTR", "id": f"sy{i}"},
            {"key": "Symbol", "value": f"sym{i}", "parentId": f"c{i}"})
        for j, (dx, dy, net) in enumerate(pins):
            wid = f"w{i}_{j}"
            px, py = cx + dx, cy + dy
            add({"type": "WIRE", "id": wid}, {"groupId": "", "zIndex": 1})
            add({"type": "LINE", "id": f"l{i}_{j}"},
                {"lineGroup": wid, "startX": px, "startY": py,
                 "endX": px - 20, "endY": py, "strokeWidth": 1})
            add({"type": "ATTR", "id": f"nt{i}_{j}"},
                {"key": "NET", "value": net, "parentId": wid,
                 "valueVisible": True, "x": px - 10, "y": py})
    for k, (fx, fy) in enumerate(flags):
        add({"type": "COMPONENT", "id": f"flag{k}"},
            {"partId": f"F{k}.1", "x": fx, "y": fy, "rotation": 0})
    # drawn wires, minted the way schwire does
    for net, segs in wires:
        wid = hashlib.sha256(f"wire/{net}".encode()).hexdigest()[:16]
        add({"type": "WIRE", "id": wid}, {"groupId": "", "zIndex": 1})
        for i, (x1, y1, x2, y2) in enumerate(segs):
            add({"type": "LINE", "id": f"d{net}{i}"},
                {"lineGroup": wid, "startX": x1, "startY": y1,
                 "endX": x2, "endY": y2, "strokeWidth": 1})
    for i, (d, cx, cy, pins) in enumerate(parts):
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{i}"})
        for j, (dx, dy, net) in enumerate(pins):
            add({"type": "PIN", "id": f"p{i}_{j}"},
                {"partId": f"P{i}.1", "x": dx, "y": dy, "length": 10,
                 "rotation": 0})

    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def netlist(path, parts):
    out = {}
    for n, (d, _, _, pins) in enumerate(parts, 1):
        out[f"gge{n}"] = {"props": {"Designator": d, "Supplier Part": "C1"},
                          "pins": {str(k + 1): net
                                   for k, (_, _, net) in enumerate(pins)}}
    path.write_text(json.dumps(out), encoding="utf-8")


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args), "--json"],
                       capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"stderr": p.stdout + p.stderr}


def main():
    tmp = Path(tempfile.mkdtemp())
    yml = tmp / "design.yaml"
    yml.write_text("rails:\n  GND:\n    type: ground\n"
                   "  VIN:\n    voltage: 12.0\n"
                   "  VOUT:\n    voltage: 5.0\n", encoding="utf-8")

    # --- detour: a straight run against a dog-leg -------------------------
    # Two pins 400 apart. Straight is 400; the detour goes down 300, across
    # and back up, which is 1000.
    parts = [("U1", 0, 0, [(0, 0, "SIG")]), ("U2", 400, 0, [(0, 0, "SIG")])]
    netlist(tmp / "n.json", parts)

    build(tmp / "straight.epro2", parts, wires=[("SIG", [(0, 0, 400, 0)])])
    rc, m = run(tmp / "straight.epro2", tmp / "n.json", "--design", yml)
    check("a straight wire has detour 1.0",
          rc == 0 and m.get("detour", {}).get("median") == 1.0, m)
    check("...and no corner is counted",
          m.get("detour", {}).get("three_or_more_corners") == 0, m)

    build(tmp / "dogleg.epro2", parts,
          wires=[("SIG", [(0, 0, 0, 300), (0, 300, 200, 300),
                          (200, 300, 200, 150), (200, 150, 400, 150),
                          (400, 150, 400, 0)])])
    rc, m = run(tmp / "dogleg.epro2", tmp / "n.json", "--design", yml)
    check("a dog-leg is reported as a detour",
          rc == 0 and m["detour"]["median"] > 1.3, m)
    check("...and its corners are counted",
          m["detour"]["three_or_more_corners"] == 1, m)

    # --- enclosed: the dog-leg rings nothing, so it is empty --------------
    check("a wire loop with no part inside is reported",
          m["enclosed"]["fraction"] > 0, m)
    rc, m2 = run(tmp / "straight.epro2", tmp / "n.json", "--design", yml)
    check("...while a straight wire rings nothing",
          m2["enclosed"]["fraction"] == 0, m2)

    # --- span: the same net, tight and spread ----------------------------
    tight = [("C1", 0, 0, [(0, 0, "VOUT")]), ("C2", 60, 0, [(0, 0, "VOUT")]),
             ("U1", 600, 0, [(0, 0, "SIG")]), ("U2", 620, 0, [(0, 0, "SIG")])]
    netlist(tmp / "t.json", tight)
    build(tmp / "tight.epro2", tight)
    rc, mt = run(tmp / "tight.epro2", tmp / "t.json", "--design", yml)

    spread = [("C1", 0, 0, [(0, 0, "VOUT")]), ("C2", 600, 0, [(0, 0, "VOUT")]),
              ("U1", 300, 0, [(0, 0, "SIG")]), ("U2", 320, 0, [(0, 0, "SIG")])]
    netlist(tmp / "s.json", spread)
    build(tmp / "spread.epro2", spread)
    rc, ms = run(tmp / "spread.epro2", tmp / "s.json", "--design", yml)
    check("spreading one net over the sheet raises its span",
          ms["span"]["over_0_4"] > mt["span"]["over_0_4"],
          f"tight {mt['span']} spread {ms['span']}")

    # --- reversal: against the power tree, and only within a band --------
    # VIN ranks before VOUT, so the VIN part belongs to the left.
    fwd = [("C1", 0, 0, [(0, 0, "VIN")]), ("C2", 400, 0, [(0, 0, "VOUT")])]
    netlist(tmp / "f.json", fwd)
    build(tmp / "fwd.epro2", fwd)
    rc, mf = run(tmp / "fwd.epro2", tmp / "f.json", "--design", yml)
    check("a part order matching the power tree reports no reversal",
          mf["reversal"]["pairs"] == 0 and mf["reversal"]["compared"] == 1, mf)

    back = [("C1", 400, 0, [(0, 0, "VIN")]), ("C2", 0, 0, [(0, 0, "VOUT")])]
    netlist(tmp / "b.json", back)
    build(tmp / "back.epro2", back)
    rc, mb = run(tmp / "back.epro2", tmp / "b.json", "--design", yml)
    check("...and the reversed order is reported",
          mb["reversal"]["pairs"] == 1, mb)

    # The band rule: the same reversal, but one part a long way below the
    # other, is not a reversal a reader ever sees.
    band = [("C1", 400, 0, [(0, 0, "VIN")]), ("C2", 0, 900, [(0, 0, "VOUT")])]
    netlist(tmp / "bd.json", band)
    build(tmp / "band.epro2", band)
    rc, mbd = run(tmp / "band.epro2", tmp / "bd.json", "--design", yml)
    check("...but not across two bands",
          mbd["reversal"]["pairs"] == 0 and mbd["reversal"]["compared"] == 0
          and mbd["reversal"]["ranked_parts"] == 2, mbd)

    # --- the rails must come from the file, not be guessed ---------------
    bare = tmp / "bare.yaml"
    bare.write_text("rails:\n  GND:\n    type: ground\n", encoding="utf-8")
    rc, mn = run(tmp / "back.epro2", tmp / "b.json", "--design", bare)
    check("with no supply rails declared, reversal reports nothing to rank",
          mn["reversal"]["rails_in_order"] == [], mn)


    # --- contact: the one fault a net diff cannot see --------------------
    # Two nets whose wires cross are connected in a geometric schematic,
    # and `epro.py diff` compares declared names, so it reports the file
    # identical. Only arithmetic on the coordinates answers it.
    cross_ = [("U1", 0, 0, [(0, 0, "AAA")]), ("U2", 400, 0, [(0, 0, "BBB")])]
    netlist(tmp / "x.json", cross_)
    # The AAA wire stops at 300: running it to 400 ends it on U2's pin,
    # which carries BBB -- the fixture drew the very short it was meant
    # to be the clean case for, and the counter was right.
    build(tmp / "clean2.epro2", cross_,
          wires=[("AAA", [(0, 0, 300, 0)]),
                 ("BBB", [(200, -100, 200, -50)])])
    rc, mc = run(tmp / "clean2.epro2", tmp / "x.json", "--design", yml)
    check("two nets that keep apart report no contact",
          rc == 0 and mc["contact"]["pairs"] == 0, mc)

    build(tmp / "shorted.epro2", cross_,
          wires=[("AAA", [(0, 0, 300, 0)]),
                 ("BBB", [(200, -100, 200, 100)])])
    rc, ms = run(tmp / "shorted.epro2", tmp / "x.json", "--design", yml)
    check("a wire crossing another net is reported",
          rc == 0 and ms["contact"]["pairs"] >= 1, ms)
    # Collinear, not crossing: BBB starts on top of AAA and runs along
    # it. Perpendicular shapes are caught by the crossing test, so a T
    # does not exercise the shared-point half at all -- the first
    # version of this probe used one and the mutation survived.
    build(tmp / "tee.epro2", cross_,
          wires=[("AAA", [(0, 0, 300, 0)]),
                 ("BBB", [(250, 0, 500, 0)])])
    rc, mt_ = run(tmp / "tee.epro2", tmp / "x.json", "--design", yml)
    check("a wire running along another net is reported too",
          rc == 0 and mt_["contact"]["pairs"] >= 1, mt_)

    check("...and both nets are named",
          ms["contact"]["worst"]
          and set(ms["contact"]["worst"][0][:2]) == {"AAA", "BBB"},
          ms["contact"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all layoutmetrics tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
