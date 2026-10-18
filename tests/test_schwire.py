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
import collections
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


def build(path, parts, flags=(), desig_at=False, vertical=False):
    """parts: [(designator, x, y, [(pin_dx, pin_dy, net), ...])]

    Each pin gets a symbol PIN record and a labelled stub ending on it,
    exactly as the importer emits, so the tool's transform check passes.

    `flags` is a list of (x, y) where a rail flag sits -- a placed
    component carrying **no Designator**, which is how netflags.py mints
    them and what keeps them out of the BOM. Put one on a stub endpoint
    and that stub's pin counts as connected by symbol.
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
            ({"key": "Designator", "value": d, "parentId": f"c{i}",
              "valueVisible": True, "x": cx, "y": cy, "rotation": 0}
             if desig_at else
             {"key": "Designator", "value": d, "parentId": f"c{i}"}))
        add({"type": "ATTR", "id": f"sy{i}"},
            {"key": "Symbol", "value": f"sym{i}", "parentId": f"c{i}"})
        for j, (dx, dy, net) in enumerate(pins):
            wid = f"w{i}_{j}"
            px, py = cx + dx, cy + dy
            add({"type": "WIRE", "id": wid}, {"groupId": "", "zIndex": 1})
            ex_, ey_ = ((px, py + 20 * (1 if dy >= 0 else -1))
                        if vertical else (px - 20, py))
            add({"type": "LINE", "id": f"l{i}_{j}"},
                {"lineGroup": wid, "startX": px, "startY": py,
                 "endX": ex_, "endY": ey_, "strokeWidth": 1})
            add({"type": "ATTR", "id": f"nt{i}_{j}"},
                {"key": "NET", "value": net, "parentId": wid,
                 "valueVisible": True,
                 "x": (px + ex_) // 2, "y": (py + ey_) // 2})
    for k, (fx, fy) in enumerate(flags):
        add({"type": "COMPONENT", "id": f"flag{k}"},
            {"partId": f"F{k}.1", "x": fx, "y": fy, "rotation": 0,
             "isMirror": False})

    # the symbol documents the pins live in
    for i, (d, cx, cy, pins) in enumerate(parts):
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{i}"})
        for j, (dx, dy, net) in enumerate(pins):
            add({"type": "PIN", "id": f"p{i}_{j}"},
                {"partId": f"P{i}.1", "x": dx, "y": dy, "length": 10,
                 "rotation": 0})
        if desig_at:
            # A body to be clear of. Only with desig_at, because the
            # other probes measure routes against a pin field and a
            # symbol outline would change what they are testing.
            add({"type": "RECT", "id": f"rc{i}"},
                {"dotX1": -10, "dotY1": -15, "dotX2": 10, "dotY2": 15})

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


def net_attrs(path):
    """Every NET attribute in the export, as dicts."""
    out = []
    for line in zipfile.ZipFile(path).read("doc.epru").decode(
            "utf-8").split("\n"):
        if '"key":"NET"' not in line:
            continue
        out.append(json.loads(line.partition("||")[2].rstrip("|")))
    return out


def visible(path):
    """net name -> how many of its labels are still visible."""
    c = collections.Counter()
    for b in net_attrs(path):
        if b.get("valueVisible") is True:
            c[b["value"]] += 1
    return c


def main():
    tmp = Path(tempfile.mkdtemp())

    # The rail gets five pins so it is above --max-pins and counts as one;
    # with three it would sit under the default and be drawn, which is the
    # correct behaviour and the wrong fixture for this assertion. It is
    # deliberately NOT ground: ground is exempt from drawing whatever its
    # pin count, so a `GND` fixture here would be skipped by that rule
    # before the pin-count test ever ran and this probe would be measuring
    # nothing.
    parts = [("R1", 0, 0, [(0, 0, "SIG"), (0, -40, "RAIL")]),
             ("R2", 100, 0, [(0, 0, "SIG"), (0, -40, "RAIL")]),
             ("R3", 200, 0, [(0, 0, "OTHER"), (0, -40, "RAIL")]),
             ("R4", 300, 0, [(0, 0, "OTHER"), (0, -40, "RAIL")]),
             ("R5", 400, 0, [(0, 0, "SPARE"), (0, -40, "RAIL")])]
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

    # --- a pin that already carries a rail flag needs no wire ------------
    # The failure this guards against is a quiet one: the option could
    # stop routing altogether and every counter would improve, because
    # the cheapest way to have no bad wires is to have no wires. So the
    # inverse probe is the one that matters -- an unflagged net must
    # still be drawn with the option on.
    d = tmp / "flagged"
    d.mkdir()
    # Three pins on SIG. The middle one is 600 units away; without a flag
    # the wire has to span the sheet to collect it.
    parts = [("U1", 0, 0, [(0, 0, "SIG")]),
             ("U2", 200, 0, [(0, 0, "SIG")]),
             ("U3", 800, 0, [(0, 0, "SIG")])]
    netlist(d / "n.json", parts)

    build(d / "plain.epro2", parts)
    rc, log = run(d / "plain.epro2", d / "n.json", "--max-span", "2000",
                  "-o", d / "plain-out.epro2")
    plain = len(wires(d / "plain-out.epro2")) if rc == 0 else -1

    # the same file with a flag sitting on U3's stub end
    build(d / "flag.epro2", parts, flags=[(780, 0)])
    rc, log = run(d / "flag.epro2", d / "n.json", "--max-span", "2000",
                  "--skip-flagged", "-o", d / "flag-out.epro2")
    check("a flagged pin is left to its flag", rc == 0, log)
    check("...and is reported rather than silently dropped",
          "left to their rail flag" in log, log)
    if rc == 0:
        # Against the input, not against a coordinate threshold: every
        # pin keeps its own stub, and U3's stub is at x=800 whether a
        # wire reaches it or not. The first version of this check counted
        # that stub as a wire and failed on a correct file.
        stubs = set(wires(d / "flag.epro2"))
        added = [s for s in wires(d / "flag-out.epro2") if s not in stubs]
        far = [s for s in added if max(s[0], s[2]) > 700]
        check("...so no drawn wire reaches it", not far, f"{far}")
        # U1 and U2 are both unflagged, so they must still be joined.
        check("...while the unflagged pins are still wired together",
              added, f"{len(added)} added segment(s)")

    # inverse probe: no flag anywhere, option on -- routing must survive
    rc, log = run(d / "plain.epro2", d / "n.json", "--max-span", "2000",
                  "--skip-flagged", "-o", d / "noflag-out.epro2")
    check("--skip-flagged does not disable routing on its own",
          rc == 0 and len(wires(d / "noflag-out.epro2")) == plain,
          f"{log}")
    check("...and reports nothing when there is nothing to report",
          "left to their rail flag" not in log, log)

    # --- a label on a foreign net's wire, which must still be reported --
    # The net-aware exemption took this count from 14 to 0 on a real
    # sheet, and a counter that reads zero right after it was loosened
    # is the fail-open shape this project warns about everywhere. So:
    # a label parked on top of a different net's stub, which the tool
    # must still call out.
    d = tmp / "onwire"
    d.mkdir()
    parts = [("U1", 0, 0, [(0, 0, "AAA")]), ("U2", 0, 200, [(0, 0, "BBB")])]
    netlist(d / "n.json", parts)
    build(d / "clean.epro2", parts)
    rc, log = run(d / "clean.epro2", d / "n.json", "-o", d / "c-out.epro2")
    check("two labels on their own stubs read as clean",
          "0 net label(s) lie on a wire" in log, log)

    # move AAA's label onto BBB's stub, which runs from (0,200) to (-20,200)
    import zipfile as _zf
    src = _zf.ZipFile(d / "clean.epro2")
    out = []
    for line in src.read("doc.epru").decode("utf-8").split("\n"):
        if '"key":"NET","value":"AAA"' in line:
            h, _, b = line.partition("||")
            bb = json.loads(b.rstrip("|"))
            bb["x"], bb["y"] = -10, 200
            line = (h + "||" + json.dumps(bb, separators=(",", ":")) + "|")
        out.append(line)
    with _zf.ZipFile(d / "dirty.epro2", "w", _zf.ZIP_DEFLATED) as z:
        z.writestr("project2.json", src.read("project2.json"))
        z.writestr("doc.epru", "\n".join(out))
    rc, log = run(d / "dirty.epro2", d / "n.json", "-o", d / "d-out.epro2")
    check("a label parked on a foreign net's stub is reported",
          "1 net label(s) lie on a wire" in log, log)

    # --- two copies of one name, and the one that must stay ------------
    # Hiding a label is the one edit here that can make a connected pin
    # read as unconnected, so the probe that matters is the negative
    # one: a lone label must survive. The positive case is a divider's
    # two halves, whose names land on each other.
    d = tmp / "dupe"
    d.mkdir()
    # The two MID pins have to be close enough that their labels really
    # land on each other -- 40 units apart they do not, and the first
    # version of this probe failed against correct code.
    parts = [("R1", 0, 0, [(0, -20, "TOP"), (0, 20, "MID")]),
             ("R2", 0, 44, [(0, -20, "MID"), (0, 20, "BOT")])]
    netlist(d / "n.json", parts)
    build(d / "dupe.epro2", parts)
    rc, log = run(d / "dupe.epro2", d / "n.json", "--labels-outward",
                  "-o", d / "out.epro2")
    check("two copies of one name on one spot: one is hidden",
          rc == 0 and "duplicate net label(s) hidden" in log, log)
    if rc == 0:
        import zipfile as _z
        vis = collections.Counter()
        for line in _z.ZipFile(d / "out.epro2").read(
                "doc.epru").decode("utf-8").split("\n"):
            if '"key":"NET"' not in line:
                continue
            bb = json.loads(line.partition("||")[2].rstrip("|"))
            if bb.get("valueVisible") is True:
                vis[bb["value"]] += 1
        check("...and exactly one copy of that name stays visible",
              vis["MID"] == 1, dict(vis))
        check("...while every other name is untouched",
              vis["TOP"] == 1 and vis["BOT"] == 1, dict(vis))

    # The negative probe, and the geometry has to be the same as the
    # positive one. With the two parts far apart nothing overlapped at
    # all, so deleting the name test changed nothing and the probe
    # passed against code that would hide any neighbour. Same spacing,
    # different names: two labels land on each other and both must stay.
    parts2 = [("R1", 0, 0, [(0, -20, "AAA"), (0, 20, "BBB")]),
              ("R2", 0, 44, [(0, -20, "CCC"), (0, 20, "DDD")])]
    netlist(d / "n2.json", parts2)
    build(d / "solo.epro2", parts2)
    rc, log = run(d / "solo.epro2", d / "n2.json", "--labels-outward",
                  "-o", d / "out2.epro2")
    check("four distinct names: none hidden",
          rc == 0 and "duplicate net label(s) hidden" not in log, log)
    if rc == 0:
        import zipfile as _z2
        vis2 = collections.Counter()
        for line in _z2.ZipFile(d / "out2.epro2").read(
                "doc.epru").decode("utf-8").split("\n"):
            if '"key":"NET"' not in line:
                continue
            bb = json.loads(line.partition("||")[2].rstrip("|"))
            if bb.get("valueVisible") is True:
                vis2[bb["value"]] += 1
        check("...and BBB and CCC, which do land on each other, both stay",
              vis2["BBB"] == 1 and vis2["CCC"] == 1, dict(vis2))

    # --- designators clear of their own body, labels beside their line --
    d = tmp / "place"
    d.mkdir()
    # An upright two-pin part: pins above and below, body in between.
    parts = [("R1", 0, 0, [(0, -20, "TOP"), (0, 20, "BOT")])]
    netlist(d / "n.json", parts)
    build(d / "in.epro2", parts, desig_at=True, vertical=True)
    rc, log = run(d / "in.epro2", d / "n.json", "--labels-outward",
                  "-o", d / "out.epro2")
    check("the designator is moved clear of its own body",
          rc == 0 and "designator(s) moved clear" in log, log)
    if rc == 0:
        import zipfile as _z3
        recs = {}
        for line in _z3.ZipFile(d / "out.epro2").read(
                "doc.epru").decode("utf-8").split("\n"):
            if "||" not in line:
                continue
            hh, _, bb = line.partition("||")
            hh, bb = json.loads(hh), json.loads(bb.rstrip("|"))
            if hh.get("type") == "ATTR":
                recs.setdefault(bb.get("key"), []).append(bb)
        dg = [b for b in recs.get("Designator", [])
              if isinstance(b.get("x"), (int, float))]
        check("...and carries a position to be moved to", dg, recs.keys())
        if dg:
            # the body spans the pins; the name must be outside it in x
            check("...outside its own outline",
                  abs(dg[0]["x"]) > 10 or abs(dg[0]["y"]) > 15, dg[0])
        rot = [b for b in recs.get("NET", [])
               if (b.get("rotation") or 0) % 180]
        check("a rotated label is offset sideways from its own line",
              rot and all(b["x"] != 0 for b in rot),
              [(b.get("value"), b.get("x")) for b in rot])

    # --- a rail is drawn where it is one node, flagged where it jumps --
    # A flagged rail used to be dropped end to end, which left the
    # output capacitors hanging between a +5V flag and a ground
    # triangle with nothing tying them to the inductor they smooth. A
    # rail crossing the sheet is still a flag -- that is what flags are
    # for -- so the rule has to tell the two apart.
    d = tmp / "rail"
    d.mkdir()
    parts = [("U1", 0, 0, [(0, 0, "VCC")]),
             ("C1", 120, 0, [(0, 0, "VCC")]),
             ("C2", 240, 0, [(0, 0, "VCC")]),
             ("U2", 2000, 0, [(0, 0, "VCC")])]
    netlist(d / "n.json", parts)
    build(d / "in.epro2", parts, flags=[(-20, 0), (100, 0), (220, 0),
                                        (1980, 0)])
    stubs0 = set(wires(d / "in.epro2"))
    rc, log = run(d / "in.epro2", d / "n.json", "--max-span", "4000",
                  "--skip-flagged", "--local-rail", "400",
                  "-o", d / "out.epro2")
    check("a flagged rail is drawn where its pins sit together",
          rc == 0 and "one node rather than a rail" in log, log)
    # Exactly one group: the three close pins. The lone far pin is not a
    # node and counting it inflates the report without drawing anything,
    # which is invisible on the sheet and wrong in the log.
    check("...counted as one node, not as two",
          "  1  flagged net(s) drawn anyway" in log, log)
    if rc == 0:
        added_ = [s for s in wires(d / "out.epro2") if s not in stubs0]
        near = [s for s in added_ if max(s[0], s[2]) <= 300]
        far = [s for s in added_ if max(s[0], s[2]) > 300]
        check("...joining the three that are close", near, f"{added_}")
        check("...and not reaching across to the far one", not far, f"{far}")

    # Without --local-rail the old behaviour stands: nothing drawn.
    rc, log = run(d / "in.epro2", d / "n.json", "--max-span", "4000",
                  "--skip-flagged", "-o", d / "plain.epro2")
    check("without --local-rail a flagged rail stays flags",
          rc == 0 and "one node rather than a rail" not in log, log)

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

    # --- ground is never drawn, however close its pins are ---------------
    # Every pin of GND already carries a symbol, so a line joining those
    # symbols says nothing they do not -- and ground is the one net a
    # reader does not follow. Left to the router it depended on the route
    # failing, and when it stopped failing the 5 V sheet grew a wire round
    # six ground pins nobody had drawn. The inverse probe is the one that
    # matters: with the rule off, the same two pins must be wired, or a
    # rule that quietly stopped all routing would pass this.
    d = tmp / "ground"; d.mkdir()
    gparts = [("R1", 0, 0, [(0, 0, "GND")]), ("R2", 100, 0, [(0, 0, "GND")])]
    netlist(d / "n.json", gparts)
    build(d / "in.epro2", gparts)
    g0 = set(wires(d / "in.epro2"))
    rc, log = run(d / "in.epro2", d / "n.json", "-o", d / "out.epro2")
    check("ground is left to its symbols",
          rc == 0 and "carried by a symbol on every pin" in log, log)
    check("...and nothing is drawn for it",
          rc == 0 and not [s for s in wires(d / "out.epro2") if s not in g0],
          log)
    rc, log = run(d / "in.epro2", d / "n.json", "--never-draw", "",
                  "-o", d / "off.epro2")
    check("...but --never-draw '' wires it, so the rule is not a no-op",
          rc == 0 and [s for s in wires(d / "off.epro2") if s not in g0], log)

    # --- one name per drawn node, and the flag that replaces them --------
    # The importer gives every pin its own labelled stub, so once a wire
    # joins two pins the net is named once per pin. Hiding all but one is
    # the point of the rule. The negative probe carries it: a node with no
    # flag must still keep exactly one name, or a rule that hid every
    # label would pass and the sheet would read as unconnected.
    d = tmp / "onename"; d.mkdir()
    oparts = [("R1", 0, 0, [(0, 0, "SIG")]), ("R2", 100, 0, [(0, 0, "SIG")])]
    netlist(d / "n.json", oparts)
    build(d / "noflag.epro2", oparts)
    rc, log = run(d / "noflag.epro2", d / "n.json", "-o", d / "nf.epro2")
    check("a drawn node keeps exactly one of its names",
          rc == 0 and visible(d / "nf.epro2").get("SIG") == 1,
          f"{visible(d / 'nf.epro2') if rc == 0 else log}")
    rc, log = run(d / "noflag.epro2", d / "n.json", "--keep-text",
                  "-o", d / "kt.epro2")
    check("...and --keep-text keeps both, so the rule is not a no-op",
          rc == 0 and visible(d / "kt.epro2").get("SIG") == 2,
          f"{visible(d / 'kt.epro2') if rc == 0 else log}")
    build(d / "flag.epro2", oparts, flags=[(-20, 0)])
    rc, log = run(d / "flag.epro2", d / "n.json", "-o", d / "fl.epro2")
    check("...and a flag on the node keeps none of them",
          rc == 0 and visible(d / "fl.epro2").get("SIG") is None,
          f"{visible(d / 'fl.epro2') if rc == 0 else log}")

    # --- names down a pin row stay on their own stub ---------------------
    # A 10-unit pin row holds text 8 tall, so three names down one row do
    # not touch. The first version of this rule bucketed them on a coarse
    # 40-unit grid and staggered the second and third outward anyway --
    # which on the real sheet put NC_BUCK_EN 32 units off its own stub,
    # nearer C9's pin on VBUS than its own line. Overlap, not proximity,
    # is what has to be resolved, and the count the tool already keeps
    # measures it: a name nearer a foreign stub end than its own.
    d = tmp / "row"; d.mkdir()
    rparts = [("U1", 0, 0, [(0, 0, "AAA"), (0, 10, "BBB"), (0, 20, "CCC")])]
    netlist(d / "n.json", rparts)
    build(d / "in.epro2", rparts)
    rc, log = run(d / "in.epro2", d / "n.json", "--labels-outward",
                  "-o", d / "out.epro2")
    # The offset probe is the one that catches the old behaviour: against
    # the 40-unit bucket it reports three distinct offsets, -60 / -44 /
    # -28, for three names that never touched. The adrift counter below
    # does *not* fire on this fixture -- its own stub end is still the
    # nearest for every one of the three -- so it is a guard on the
    # counter being wired up, not the probe that finds the regression.
    # Saying which is which matters: a line that reads as evidence and
    # cannot fail is the thing this file exists to avoid.
    if rc == 0:
        xs = sorted({b["x"] for b in net_attrs(d / "out.epro2")})
        check("names down a pin row are not staggered", len(xs) == 1, xs)
    check("...and the tool reports no label adrift",
          rc == 0 and "0 net label(s) sit nearer a foreign stub" in log, log)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all schwire tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
