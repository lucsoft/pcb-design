#!/usr/bin/env python3
"""Regression tests for tools/schwire.py.

The tool adds drawn wires on top of a schematic whose connectivity is
already carried by per-pin net labels. That makes its failure mode unusual
and worth stating: a wire that is geometrically wrong changes nothing,
because the label still connects the pin. The one thing that *does* break
the circuit is a wire passing over a pin belonging to a different net, which
in a geometric schematic is a connection nobody asked for.

So the collision probe is the important one here, not the "did it draw a
line" one. A tool that drew nothing would be useless; a tool that drew a
short would be worse than useless, and `epro.py diff` could not see it —
the declared net names are unchanged by a geometric short.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "schwire.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path, parts):
    """parts: [(designator, x, y, [(pin_dx, pin_dy, net), ...])]

    Each pin gets a symbol PIN record and a labelled stub ending on it,
    exactly as the importer emits, so the tool's transform check passes.
    """
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
                 "x": px - 10, "y": py})
    # the symbol documents the pins live in
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
    out, n = {}, 0
    for d, _, _, pins in parts:
        n += 1
        out[f"gge{n}"] = {"props": {"Designator": d, "Supplier Part": "C1"},
                          "pins": {str(k + 1): net
                                   for k, (_, _, net) in enumerate(pins)}}
    path.write_text(json.dumps(out), encoding="utf-8")


def wires(path):
    z = zipfile.ZipFile(path)
    segs = []
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        h, b = json.loads(h), json.loads(b.rstrip("|"))
        if h.get("type") == "LINE":
            segs.append((b["startX"], b["startY"], b["endX"], b["endY"]))
    return segs


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp())

    # GND gets five pins so it is above --max-pins and counts as a rail;
    # with three it would sit under the default and be drawn, which is the
    # correct behaviour and the wrong fixture for this assertion.
    parts = [("R1", 0, 0, [(0, 0, "SIG"), (0, -40, "GND")]),
             ("R2", 100, 0, [(0, 0, "SIG"), (0, -40, "GND")]),
             ("R3", 200, 0, [(0, 0, "OTHER"), (0, -40, "GND")]),
             ("R4", 300, 0, [(0, 0, "OTHER"), (0, -40, "GND")]),
             ("R5", 400, 0, [(0, 0, "SPARE"), (0, -40, "GND")])]
    src, out = tmp / "in.epro2", tmp / "out.epro2"
    build(src, parts)
    nl = tmp / "netlist.json"
    netlist(nl, parts)

    before = len(wires(src))
    rc, log = run(src, nl, "-o", out)
    check("it succeeds", rc == 0, log)
    check("the transform is verified against every stub",
          "landing on a labelled stub" in log and "NOSTUB" not in log, log)

    if out.exists():
        after = len(wires(out))
        check("a wire was drawn for the adjacent 2-pin net", after > before,
              f"{before} -> {after}")
        check("the 5-pin rail is left as a label",
              "more than 4 pins (rail or bus)" in log, log)

        # determinism
        out2 = tmp / "out2.epro2"
        run(src, nl, "-o", out2)
        h1 = hashlib.sha256(zipfile.ZipFile(out).read("doc.epru")).hexdigest()
        h2 = hashlib.sha256(zipfile.ZipFile(out2).read("doc.epru")).hexdigest()
        check("two runs are byte-identical", h1 == h2)

    # --- the probe that matters ------------------------------------------
    # R1 and R3 share FAR, and R2's OTHER pin sits exactly on the straight
    # line between them. Every candidate shape must be rejected.
    shorty = [("R1", 0, 0, [(0, 0, "FAR")]),
              ("R2", 100, 0, [(0, 0, "OTHER")]),
              ("R3", 200, 0, [(0, 0, "FAR")])]
    s2, o2 = tmp / "short.epro2", tmp / "shortout.epro2"
    build(s2, shorty)
    n2 = tmp / "short.json"
    netlist(n2, shorty)
    rc, log = run(s2, n2, "-o", o2)
    check("a path over a foreign pin is refused", rc == 0 and "FAR" in log
          and "no clean path" in log, log)
    if o2.exists():
        check("...and no wire was drawn for it",
              len(wires(o2)) == len(wires(s2)),
              f"{len(wires(s2))} -> {len(wires(o2))}")

    # --- span and pin-count gates -----------------------------------------
    rc, log = run(src, nl, "--dry-run", "--max-span", "10")
    check("a net wider than --max-span is left alone",
          "span over 10 units" in log, log)
    rc, log = run(src, nl, "--dry-run", "--max-pins", "1")
    check("a net with more pins than --max-pins is left alone",
          "more than 1 pins" in log, log)

    # --- a fractional anchor, as EasyEDA's own rotation writes ------------
    # Rotating R9 by hand in the editor put its anchor at
    # y = -1374.9999999999998, and the pin derived from it missed its stub
    # by 2e-13. The tool refused -- fail-closed, and useless on any file a
    # human has touched.
    frac = tmp / "frac.epro2"
    build(frac, parts)
    data = zipfile.ZipFile(frac).read("doc.epru").decode("utf-8")
    data = data.replace('"x":100,"y":0,"rotation":0',
                        '"x":100,"y":-0.0000000000002,"rotation":0', 1)
    with zipfile.ZipFile(frac, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", data)
    fo = tmp / "fracout.epro2"
    rc, log = run(frac, nl, "-o", fo)
    check("a fractional anchor still matches its stub",
          rc == 0 and "NOSTUB" not in log, log)

    # --- mirroring --------------------------------------------------------
    # Measured by flipping Q1 in the editor: mirror is negating x, and all
    # 441 pins still hit their stubs. What that did NOT settle is whether
    # the negation happens before or after the rotation, because the only
    # mirrored parts available were a symmetric resistor and an asymmetric
    # part at rotation 0 -- both commute. The orders differ only for a part
    # that is asymmetric under negate-x AND turned to 90 or 270.
    def mirrored(path, parts, rot, flip=True):
        build(path, parts)
        d = zipfile.ZipFile(path).read("doc.epru").decode("utf-8")
        d = d.replace('"rotation":0,"isMirror":false',
                      f'"rotation":{rot},"isMirror":{str(flip).lower()}', 1)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project2.json", json.dumps({"title": "t"}))
            z.writestr("doc.epru", d)

    asym = [("U1", 0, 0, [(0, 20, "SIG"), (-20, 0, "GND"), (0, -20, "OTHER")]),
            ("R1", 200, 0, [(0, 0, "SIG"), (0, -40, "GND")])]
    an = tmp / "asym.json"
    netlist(an, asym)

    m0 = tmp / "m0.epro2"
    mirrored(m0, asym, 0)
    rc, log = run(m0, an, "--dry-run")
    check("mirrored at rotation 0 is accepted", "is mirrored and rotated" not in log, log)

    m90 = tmp / "m90.epro2"
    mirrored(m90, asym, 90)
    rc, log = run(m90, an, "--dry-run")
    check("mirrored AND rotated 90, asymmetric, is refused",
          rc != 0 and "is mirrored and rotated 90" in log, log)

    # the same combination on a symmetric symbol is provably safe
    sym = [("R1", 0, 0, [(20, 0, "SIG"), (-20, 0, "GND")]),
           ("R2", 200, 0, [(20, 0, "SIG"), (-20, 0, "GND")])]
    sn = tmp / "sym.json"
    netlist(sn, sym)
    ms = tmp / "msym.epro2"
    mirrored(ms, sym, 90)
    rc, log = run(ms, sn, "--dry-run")
    check("...but a symmetric symbol in that combination is let through",
          "is mirrored and rotated" not in log, log)

    # --- a pin that does not land on a stub must stop the run -------------
    bad = tmp / "bad.epro2"
    build(bad, parts)
    data = zipfile.ZipFile(bad).read("doc.epru").decode("utf-8")
    data = data.replace('"startX":0,"startY":0', '"startX":7,"startY":7', 1)
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", data)
    gone = tmp / "never.epro2"
    rc, log = run(bad, nl, "-o", gone)
    check("an unverified transform stops the run",
          rc == 1 and "NOSTUB" in log, log)
    check("...and nothing is written", not gone.exists())

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all schwire tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
