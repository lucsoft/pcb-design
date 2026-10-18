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
                 "valueVisible": True, "x": px - 10, "y": py})
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
    check("the straight path over a foreign pin is not taken", rc == 0, log)
    if o2.exists():
        # It used to be refused outright, because the three fixed shapes
        # all crossed R2's pin and there was nothing else to try. The maze
        # fallback detours instead, which is what a human would draw -- so
        # the assertion is no longer "nothing was drawn" but the stronger
        # "something was drawn and it misses the pin".
        import json as _j
        z = zipfile.ZipFile(o2)
        recs = [( _j.loads(l.partition("||")[0]), _j.loads(l.partition("||")[2].rstrip("|")))
                for l in z.read("doc.epru").decode("utf-8").split("\n") if "||" in l]
        netof = {b["parentId"] for h, b in recs
                 if h.get("type") == "ATTR" and b.get("key") == "NET"}
        drawn = [(b["startX"], b["startY"], b["endX"], b["endY"])
                 for h, b in recs if h.get("type") == "LINE"
                 and b.get("lineGroup") not in netof]
        check("...a detour was drawn instead", len(drawn) > 0,
              f"{len(drawn)} segment(s)")
        import importlib.util as _i
        _sp = _i.spec_from_file_location("sw", TOOL)
        _m = _i.module_from_spec(_sp); _sp.loader.exec_module(_m)
        check("...and it does not touch the foreign pin",
              not any(_m.touches(s, (100, 0)) for s in drawn),
              str(drawn))

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

    # --- the maze: a target off the step lattice -------------------------
    # The lattice is anchored at the start pin, so a target that is not a
    # whole GRID away in both axes used to be unreachable -- and only 55 of
    # the real board's 441 pins sit on a multiple of 10 in x. The search
    # reported "no clean path", which reads as a layout too tight rather
    # than a step size that cannot land, and that cost 29 of 37 nets.
    sys.path.insert(0, str(ROOT / "tools"))
    import schwire

    clear = schwire.Obstacles([], [])
    G = schwire.GRID
    straight = schwire._route_pair((0, 0), (3 * G, 0), clear, {(0, 0), (3 * G, 0)})
    check("a target on the lattice is reached", straight is not None)

    off = (3 * G + 3, 0)
    skew = schwire._route_pair((0, 0), off, clear, {(0, 0), off})
    check("...and so is one three units off it",
          skew is not None and skew[-1] == off, str(skew))

    both = (3 * G + 3, 2 * G + 7)
    skew2 = schwire._route_pair((0, 0), both, clear, {(0, 0), both})
    check("...and one off the lattice in both axes",
          skew2 is not None and skew2[-1] == both, str(skew2))

    # --- obstacles are geometry, not a rasterised point set --------------
    # The point set was rasterised on multiples of GRID while the search
    # walks the start pin's lattice, so the two never met and the router
    # drove straight through U7 and U2. An inverse probe: put the body
    # somewhere no multiple of GRID lands inside it.
    # The wall spans no multiple of GRID in x, and is short enough in y
    # that a detour exists -- so "no path" cannot stand in for "went
    # around", which is what makes this probe worth running.
    wall = schwire.Obstacles([(13, -15, 17, 15)], [])
    around = schwire._route_pair((0, 0), (30, 0), wall, {(0, 0), (30, 0)})
    check("a route past a body off the lattice is found",
          around is not None, str(around))
    check("...and it does not pass through it",
          around is not None
          and not any(13 < x < 17 and -15 < y < 15 for x, y in around),
          str(around))
    check("...because the direct step is refused",
          wall.step_blocked(0, 0, 30, 0, set()) is True)
    check("...while a step clear of it is not",
          wall.step_blocked(0, 50, 10, 50, set()) is False)

    # --- a foreign pin may not be run over, the net's own may ------------
    pinned = schwire.Obstacles([], [(20, 0)])
    check("a step running over a foreign pin is refused",
          pinned.step_blocked(0, 0, 30, 0, set()))
    check("...and allowed when that pin belongs to this net",
          not pinned.step_blocked(0, 0, 30, 0, {(20, 0)}))

    # --- a foreign stub is an obstacle, not just its endpoint ------------
    # There is no junction record in this format -- the hand-drawn
    # reference has none -- so a crossing and a T are indistinguishable
    # on the sheet and the only honest answer is not to cross. Blocking
    # the pin alone left 283 crossings; blocking the stub leaves none.
    vstub = [(50, -40, 50, 40, "GND")]
    withstub = schwire.Obstacles([], [], vstub)
    check("a step crossing a foreign stub is refused",
          withstub.step_blocked(0, 0, 100, 0, set(), "SIG") is True)
    check("...and allowed when the stub is this net's own",
          withstub.step_blocked(0, 0, 100, 0, set(), "GND") is False)
    check("...and a step that misses the stub is allowed",
          withstub.step_blocked(0, 200, 100, 200, set(), "SIG") is False)

    # a wire drawn for an earlier net becomes an obstacle for later ones
    withstub.add_wire([(70, -40, 70, 40)], "VCC")
    check("a wire already drawn is an obstacle too",
          withstub.step_blocked(60, 0, 100, 0, set(), "SIG") is True)
    check("...and not for the net that owns it",
          withstub.step_blocked(60, 0, 100, 0, set(), "VCC") is False)

    # --- clearance: not touching is not the same as reading apart -------
    # At clearance 0 the maze takes the only free corridor and that runs
    # flush along the symbol outline -- 42 segments on this board lay
    # exactly on one, and three of LED1's four edges came out green.
    box = [(0, 0, 100, 100)]
    tight = schwire.Obstacles(box, [])
    loose = schwire.Obstacles(box, [], (), 5)
    check("a segment along the outline is allowed at clearance 0",
          tight.step_blocked(-10, 0, 110, 0, set()) is False)
    check("...and refused once a clearance is asked for",
          loose.step_blocked(-10, 0, 110, 0, set()) is True)
    check("...while one beyond the clearance is still allowed",
          loose.step_blocked(-10, -20, 110, -20, set()) is False)
    check("...and the clearance does not block the far side of the sheet",
          loose.step_blocked(-10, -500, 110, -500, set()) is False)

    # --- the free end of a stub needs more room than its flank ----------
    # "No crossings means every meeting is a node" reads the other way
    # too: a trunk running a millimetre clear of ten foreign stub ends is
    # ten T-junctions to the eye. Measured on the real board, 70 drawn
    # segments passed within half a grid of a foreign end; end-clearance
    # 6 leaves one. The flank is different -- a wire beside a stub for
    # part of its length reads as two wires -- so the rule is asymmetric.
    stub1 = [(50, 0, 50, 40, "GND")]
    flank = schwire.Obstacles([], [], stub1, 0, 0)
    ended = schwire.Obstacles([], [], stub1, 0, 6)
    check("a step passing 4 units from a foreign stub end is allowed at 0",
          flank.step_blocked(0, 44, 100, 44, set(), "SIG") is False)
    check("...and refused once the end has a clearance",
          ended.step_blocked(0, 44, 100, 44, set(), "SIG") is True)
    check("...while 8 units away is allowed again",
          ended.step_blocked(0, 48, 100, 48, set(), "SIG") is False)
    check("...and the net's own stub end never blocks it",
          ended.step_blocked(0, 44, 100, 44, set(), "GND") is False)

    # --- labels off the body, and the exception that allows it ----------
    # 46 of 261 visible labels overlapped a symbol: a stub is three pin
    # pitches long and `NC_EFUSE1_MODE` does not fit in that. Moving the
    # text is safe where moving a wire is not -- the net is carried by
    # the attribute's value on the wire, not by where its text sits.
    #
    # But schwire is append-only, and this is the one thing that changes
    # an existing record. So the proof names the exception instead of
    # being loosened: only a NET attribute, only x/y/align. The probe
    # that matters is the second one, which checks the *refusal* still
    # works -- a check relaxed to "some records may differ" would have
    # retired itself.
    d = tmp / "labels"; d.mkdir()
    lparts = [("R1", 0, 0, [(0, 0, "SIG"), (0, -40, "GND")]),
              ("R2", 100, 0, [(0, 0, "SIG"), (0, -40, "GND")])]
    lsrc, nl2 = d / "in.epro2", d / "netlist.json"
    build(lsrc, lparts)
    netlist(nl2, lparts)
    plain, moved = d / "plain.epro2", d / "moved.epro2"
    rc1, _ = run(lsrc, nl2, "-o", plain)
    rc2, log2 = run(lsrc, nl2, "--labels-outward", "-o", moved)
    check("--labels-outward is accepted", rc2 == 0, log2)
    # A count, and a non-zero one. The first version of this fixture had
    # no  on its NET attributes, so nothing was eligible,
    # nothing moved, and every probe below passed against an untouched
    # file -- including a deliberate tamper. A probe that cannot fail is
    # the failure mode this file exists to prevent.
    import re as _re
    _m = _re.search(r"(\d+) net label\(s\) moved", log2)
    check("...and says how many it moved, which is not zero",
          _m is not None and int(_m.group(1)) > 0, log2)

    if plain.exists() and moved.exists():
        def nets(path):
            out = {}
            for line in zipfile.ZipFile(path).read("doc.epru").decode(
                    "utf-8").split("\n"):
                if "||" not in line:
                    continue
                h, _, b = line.partition("||")
                try:
                    hh, bb = json.loads(h), json.loads(b.rstrip("|"))
                except json.JSONDecodeError:
                    continue
                if hh.get("type") == "ATTR" and bb.get("key") == "NET":
                    out[hh["id"]] = bb
            return out
        a_, b_ = nets(plain), nets(moved)
        check("...and every label keeps its net name",
              {k: v.get("value") for k, v in a_.items()}
              == {k: v.get("value") for k, v in b_.items()})
        # rotation joined the list: a label on a vertical stub is set
        # along it, because a horizontal name is wider than a pin pitch
        # and a row of them can then only be staggered. Still only these
        # four keys -- the exception stays named rather than widened.
        check("...while only x, y, align and rotation differ",
              all({q for q in set(a_[k]) | set(b_[k])
                   if a_[k].get(q) != b_[k].get(q)}
                  <= {"x", "y", "align", "rotation"}
                  for k in a_ if k in b_))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all schwire tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
