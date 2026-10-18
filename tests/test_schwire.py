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


def build(path, parts, flags=(), desig_at=False, vertical=False, bodies=None):
    """parts: [(designator, x, y, [(pin_dx, pin_dy, net), ...])]

    `bodies` maps a part index to a half-box, drawn as the symbol's RECT
    -- so a fixture can put an obstacle where no pin sits, which is what
    the router has to go round rather than through.

    Each pin gets a symbol PIN record and a labelled stub ending on it,
    exactly as the importer emits, so the tool's transform check passes.

    `flags` is a list of (x, y) where a rail flag sits -- a placed
    component carrying **no Designator**, which is how netflags.py mints
    them and what keeps them out of the BOM. Put one on a stub endpoint
    and that stub's pin counts as connected by symbol.

    `vertical` says which parts' stubs leave their pin perpendicular to
    the row: True and False mean all and none, and a set of indices means
    those. Two parts whose stubs differ in direction is not a shape the
    importer makes on its own, and it is the shape the label rule turns
    on: a lead lying along the drawn wire has been taken over by it, and
    a lead sticking out of the node has not.
    """
    recs, t = [], 0
    vert = ((lambda i: bool(vertical)) if isinstance(vertical, bool)
            else (lambda i: i in vertical))

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
                        if vert(i) else (px - 20, py))
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
        if bodies and i in bodies:
            hx, hy = bodies[i]
            add({"type": "RECT", "id": f"rb{i}"},
                {"dotX1": -hx, "dotY1": -hy, "dotX2": hx, "dotY2": hy})

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


def drawn(path):
    """Every LINE this tool drew, as (x0, y0, x1, y1).

    The hand wires are in the file too, and they are what a drawn wire
    must be clear of, so they are the one thing this leaves out.
    """
    z = zipfile.ZipFile(path)
    out = []
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        h, b = json.loads(h), json.loads(b.rstrip("|"))
        if (h.get("type") == "LINE"
                and not str(b.get("lineGroup", "")).startswith("hw")):
            out.append((b["startX"], b["startY"], b["endX"], b["endY"]))
    return out


def _crosses(p, q, r, s):
    (x1, y1), (x2, y2) = p, q
    (x3, y3), (x4, y4) = r, s
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if d == 0:
        return False
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    return 0 < t < 1 and 0 < u < 1


def hand_wire(path, a, b):
    """Add a `hw`-prefixed WIRE and LINE, as handplace.py mints them."""
    z = zipfile.ZipFile(path)
    entries = {n: z.read(n) for n in z.namelist()}
    z.close()
    wid = "hwtest"
    entries["doc.epru"] += (
        f'\n{{"type":"WIRE","id":"{wid}"}}||'
        f'{{"groupId":"","zIndex":1}}|'
        f'\n{{"type":"LINE","id":"{wid}l"}}||'
        f'{{"lineGroup":"{wid}","startX":{a[0]},"startY":{a[1]},'
        f'"endX":{b[0]},"endY":{b[1]},"strokeWidth":1}}|').encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as w:
        for n, data in entries.items():
            w.writestr(n, data)


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


def test_hand_wire_crossing(tmp):
    """A hand-drawn lead across another net's wire is refused here.

    handplace.py checks its own wires when it writes them, and at that
    point schwire has not drawn anything yet -- so a hand lead that
    crosses a rail laid down here is invisible to every check the chain
    has. It was found by eye, twice, and both times the route's *ends*
    were right; only the way it got there was wrong.

    Two answers, and the second is what keeps the first from being a rule
    that fires on correct drawings: a hand wire ending on another wire of
    its own net is a junction, which is what half of them are for.
    """
    d = tmp / "handcross"
    d.mkdir()
    parts = [("R1", 0, 0, [(0, 0, "GND")]),
             ("R2", 0, 100, [(0, 0, "GND")]),
             ("R3", 10, 50, [(0, 0, "SIG")])]
    netlist(d / "n.json", parts)
    build(d / "in.epro2", parts)
    # R3's stub runs from (10, 50) to (-10, 50) -- across the line
    # the hand wire takes from one ground pin to the other, in
    # the middle of both.
    hand_wire(d / "in.epro2", (0, 0), (0, 100))
    rc, log = run(d / "in.epro2", d / "n.json", "-o", d / "out.epro2")
    check("a hand wire across another net's wire is refused",
          rc == 1 and "CROSS" in log, log)
    check("...and the output is deleted", not (d / "out.epro2").exists())

    # The inverse: no crossing part, same hand wire, and it is drawn.
    parts2 = [("R1", 0, 0, [(0, 0, "GND")]),
              ("R2", 0, 100, [(0, 0, "GND")])]
    netlist(d / "n2.json", parts2)
    build(d / "in2.epro2", parts2)
    hand_wire(d / "in2.epro2", (0, 0), (0, 100))
    rc, log = run(d / "in2.epro2", d / "n2.json", "-o", d / "out2.epro2")
    check("...whereas the same wire with nothing in its way is not",
          rc == 0 and "CROSS" not in log, log)


def test_hand_wire_steered_round(tmp):
    """A hand wire is routed around, not reported after the fact.

    Checking the finished geometry is necessary and not sufficient: the
    first version of it read `rows`, the *input*, so it could not see the
    wires it exists to check the hand wires against. Then, once it did,
    the maze still proposed paths straight through a hand lead and
    `collides` threw every one away -- and two nets that had been drawn
    came back as labels, which is worse than the crossing.
    """
    d = tmp / "handsteer"
    d.mkdir()
    parts = [("R1", 0, 0, [(0, 0, "SIG")]),
             ("R2", 0, 100, [(0, 0, "SIG")])]
    netlist(d / "n.json", parts)
    build(d / "in.epro2", parts)
    # Straight up the middle, which is exactly where the hand wire is.
    hand_wire(d / "in.epro2", (-40, 50), (40, 50))
    rc, log = run(d / "in.epro2", d / "n.json", "-o", d / "out.epro2")
    check("a hand wire in the way is routed round, not refused",
          rc == 0, log)
    if rc != 0:
        return
    segs = drawn(d / "out.epro2")
    check("...and the net is still drawn", bool(segs), segs)
    check("...with no segment crossing the hand wire",
          not any(_crosses((s[0], s[1]), (s[2], s[3]),
                           (-40, 50), (40, 50)) for s in segs), segs)

    # The inverse: move the hand wire out of the corridor and the route
    # is the straight line again. Without this, a tool that always
    # detoured would pass the probe above.
    build(d / "in2.epro2", parts)
    hand_wire(d / "in2.epro2", (200, 50), (260, 50))
    rc, log = run(d / "in2.epro2", d / "n.json", "-o", d / "out2.epro2")
    check("...and with the hand wire elsewhere it is a straight line",
          rc == 0 and (0, 0, 0, 100) in drawn(d / "out2.epro2"),
          drawn(d / "out2.epro2"))


def test_hand_wire_keeps_the_name(tmp):
    """A hand wire over a node must not take that node's only name.

    `dead_tails` drops a stub a wire already reaches, and hides the
    label with it, on the argument that the name has left the tail for
    something else on the sheet. That argument holds for a ground bus --
    the ground *symbol* at the end of the run names the node -- and it
    does not hold for a hand-drawn lead on a net that carries no symbol.
    It hid all three visible `COMP_5V` names on the supply sheet, leaving
    a drawn wire with nothing on the sheet saying which net it was.

    Asked of the node rather than the net, and the second half is why: a
    symbol on one node of a net must not excuse eating the name on
    another. Both halves ride on a pin in the *middle* of the hand wire
    being one node with its ends, which is what half of these joins are.
    """
    d = tmp / "handname"
    d.mkdir()
    # Too far apart for the router's span, so the net stays a label and
    # the host-wire branch is the one under test.
    parts = [("R1", 0, 0, [(0, 0, "SIG")]),
             ("R2", 0, 500, [(0, 0, "SIG")])]
    netlist(d / "n.json", parts)
    build(d / "in.epro2", parts)
    hand_wire(d / "in.epro2", (0, 0), (0, 500))
    rc, log = run(d / "in.epro2", d / "n.json", "-o", d / "out.epro2")
    check("a hand wire over a net with no symbol keeps its names",
          rc == 0 and visible(d / "out.epro2")["SIG"] == 2,
          (rc, visible(d / "out.epro2")))

    # ...and with a symbol naming that node, the stubs go and the names
    # with them: the flag is the name. R3's pin sits in the *middle* of
    # the hand wire, so this only holds if a T is one node.
    parts3 = parts + [("R3", 0, 250, [(0, 0, "SIG")])]
    netlist(d / "n3.json", parts3)
    build(d / "in3.epro2", parts3, flags=[(-20, 250)])
    hand_wire(d / "in3.epro2", (0, 0), (0, 500))
    rc, log = run(d / "in3.epro2", d / "n3.json", "-o", d / "out3.epro2")
    check("...and where a symbol names the node, only its own name stays",
          rc == 0 and visible(d / "out3.epro2")["SIG"] == 1,
          (rc, visible(d / "out3.epro2")))


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
        # Counted as a set difference, not as a line count. A count no
        # longer moves on its own: the rule that drops a stub the wire has
        # made dead deletes as many lines as the router adds, and on this
        # fixture it deleted exactly as many, so `10 -> 10` read as "nothing
        # drawn" on a file that had been drawn on.
        new = set(wires(out)) - set(wires(src))
        check("a wire was drawn for the adjacent 2-pin net", new,
              f"{before} -> {len(wires(out))}, new {sorted(new)}")
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
        # Again by set difference: the drawn wire now carries the net label
        # of the stub it replaced, so "a line with no label on it" is no
        # longer the same question as "a line schwire drew".
        drawn = sorted(set(wires(o2)) - set(wires(s2)))
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

    # --- a reserved label box walls every net but the one it names -------
    # Label boxes were handed to the obstacle map anonymously, so a box
    # blocked its *own* net -- and a net whose own label sat on the only
    # way in was walled out of its own pin. Three answers, because the
    # data can be absent: the owner steps through, everyone else stops,
    # and a name nobody claims is nobody's.
    #
    # These four pin the invariant on the class, not on the call site:
    # they pass whichever way `schwire` hands its boxes over, so none of
    # them is the probe that finds a revert. The end-to-end fixture
    # further down is, and it is the one to keep an eye on.
    owned = schwire.Obstacles([], [], (), 0, 0, [(-4, -4, 4, 4, "SIG")])
    check("a step through the label box of its own net is allowed",
          owned.step_blocked(0, -20, 0, 20, set(), "SIG") is False)
    check("...refused for another net",
          owned.step_blocked(0, -20, 0, 20, set(), "OTHER") is True)
    check("...and refused when the step names no net at all",
          owned.step_blocked(0, -20, 0, 20, set()) is True)
    # ...and the same box as an anonymous body stops its owner, which is
    # what made the fault: the two shapes must not be interchangeable.
    anon = schwire.Obstacles([(-4, -4, 4, 4)], [])
    check("...whereas the same box as an anonymous body stops its owner",
          anon.step_blocked(0, -20, 0, 20, set(), "SIG") is True)

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

    # --- a flagged pin inside the node being drawn stays in it ----------
    # C8's supply ended in a VBUS flag beside a wire that ran from C9 to
    # U4's VIN pin: one rail drawn as two, which reads as two nets. The
    # net is drawn for its local node and its far pin left to the flag --
    # but the fallback dropped *every* flagged pin, including the one in
    # the node being drawn, and then had too few left to route at all.
    d3 = tmp / "flagged-node"
    d3.mkdir()
    parts3 = [("C8", 0, 0, [(0, 0, "VBUS")]),
              ("C9", 120, 0, [(0, 0, "VBUS")]),
              ("U4", 2000, 0, [(0, 0, "VBUS")])]
    netlist(d3 / "n.json", parts3)
    build(d3 / "in.epro2", parts3, flags=[(-20, 0), (100, 0)])
    rc, log = run(d3 / "in.epro2", d3 / "n.json", "--max-span", "4000",
                  "--skip-flagged", "--local-rail", "400",
                  "-o", d3 / "out.epro2")
    check("a net drawn for its node keeps the flagged pin in it",
          rc == 0 and "1 net(s) routed" in log, log)
    if rc == 0:
        # Added wires only: the file's own stubs already end on (0, 0), so
        # reading every segment would pass on a run that drew nothing.
        before = set(wires(d3 / "in.epro2"))
        ends = {e for s in wires(d3 / "out.epro2") if s not in before
                for e in ((s[0], s[1]), (s[2], s[3]))}
        check("...so the flagged pin is reached by a wire, not left alone",
              (0, 0) in ends, sorted(ends)[:8])

    # --- the name goes on the wire, not on a spur -----------------------
    # Two names on one node, one of them on a lead lying along the drawn
    # wire and the other on a lead sticking out of it. Reading order kept
    # whichever sat higher -- on the supply sheet that was C6's, whose
    # stub points away from its own body into empty space, so the name
    # was the only thing stopping the spur being dropped and the reader
    # got a thirty-unit stub with a vertical label on it.
    d4 = tmp / "name-on-wire"
    d4.mkdir()
    # C1 sits at y=10 with its pin offset -10, so its pin is on the row at
    # (0) and its lead leaves *upwards*. That matters: reading order is
    # topmost first, so a lead going down would put its name below R1's
    # and plain reading order would pick R1's anyway -- the probe would
    # pass without the rule. Going up is the shape C6 has on the sheet.
    parts4 = [("R1", 0, 0, [(0, 0, "MID")]),
              ("C1", -100, 10, [(0, -10, "MID")])]
    netlist(d4 / "n.json", parts4)
    build(d4 / "in.epro2", parts4, vertical={1})
    rc, log = run(d4 / "in.epro2", d4 / "n.json", "--labels-outward",
                  "-o", d4 / "out.epro2")
    check("a node drawn as a wire keeps one name", rc == 0, log)
    if rc == 0:
        vis = [b for b in net_attrs(d4 / "out.epro2")
               if b.get("value") == "MID" and b.get("valueVisible") is True]
        check("...keeping exactly one of the two", len(vis) == 1, vis)
        # ...and the one it kept is the one on the wire, which is what
        # lets the spur go: a name on C1's perpendicular lead would have
        # to stay visible to keep that lead alive.
        spur = [s for s in wires(d4 / "out.epro2") if s[0] == s[2] == -100]
        check("...so the lead sticking out of the node is dropped",
              not spur, spur)

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

    # --- a net whose own label box covers its own pin ---------------------
    # Two pins on a long-named net, twenty units of stub each, so the
    # label box laid on past the free end reaches back over the pin.
    # The line between them is refused by a foreign body, which leaves
    # the maze -- and every first step out of R1 crosses that box. With
    # the box anonymous the maze is walled in at its own starting pin
    # and the run reports "no clean path" for a net it can plainly see.
    # Measured both ways: with the boxes handed over anonymously this
    # fixture reports `1 net(s) have no clean path`, and with them
    # owned it reports `1 net(s) routed`.
    d = tmp / "ownbox"; d.mkdir()
    oparts = [("R1", 0, 0, [(0, 0, "ANALOG_RETURN")]),
              ("R2", 0, 200, [(0, 0, "ANALOG_RETURN")]),
              ("W1", 0, 100, [])]
    netlist(d / "n.json", oparts)
    build(d / "in.epro2", oparts, bodies={2: (20, 10)})
    rc, log = run(d / "in.epro2", d / "n.json", "--labels-outward",
                  "-o", d / "out.epro2")
    check("a net is not walled out by its own label box",
          rc == 0 and "no clean path" not in log
          and "1 net(s) routed" in log, log)
    check("...and the wire between the two pins is drawn",
          rc == 0 and set(wires(d / "out.epro2")) - set(wires(d / "in.epro2")),
          sorted(set(wires(d / "out.epro2")) - set(wires(d / "in.epro2")))
          if rc == 0 else log)
    # The control: with no reserved label boxes there is nothing to be
    # owned, so this is not the ownership rule doing the drawing.
    rc, log = run(d / "in.epro2", d / "n.json", "-o", d / "no.epro2")
    check("...and it draws with no label boxes reserved at all",
          rc == 0 and "1 net(s) routed" in log, log)

    # --- the stub a drawn wire has made dead is deleted -------------------
    # A pin's stub leads out to where its name sits. Once the rail is drawn
    # *through* the pin, the rest of the stub is a wire to nowhere -- on the
    # 5 V node, C10 and C11 each kept a cable standing in the air beside a
    # pin the rail already reached. Three pins on one line, their stubs all
    # dropping away, is that shape. The flag sits on the first stub's far
    # end, so it is the flag's lead and must survive; the other two are dead.
    d = tmp / "tails"; d.mkdir()
    tparts = [("R1", 0, 0, [(0, 0, "SIG")]),
              ("R2", 100, 0, [(0, 0, "SIG")]),
              ("R3", 200, 0, [(0, 0, "SIG")])]
    netlist(d / "n.json", tparts)
    build(d / "noflag.epro2", tparts, vertical=True)
    build(d / "flag.epro2", tparts, flags=[(0, 20)], vertical=True)

    def gone(src, out):
        """The segments the run removed -- all of them, by construction,
        stubs: nothing else is ever deleted."""
        return set(wires(src)) - set(wires(out))

    def nets_with_wire(path):
        """Every NET attribute as (net, parentId still a live WIRE)."""
        live, out = set(), []
        for line in zipfile.ZipFile(path).read("doc.epru").decode(
                "utf-8").split("\n"):
            if "||" not in line:
                continue
            h, _, b = line.partition("||")
            try:
                hh, bb = json.loads(h), json.loads(b.rstrip("|"))
            except json.JSONDecodeError:
                continue
            if hh.get("type") == "WIRE":
                live.add(hh["id"])
            elif hh.get("type") == "ATTR" and bb.get("key") == "NET":
                out.append((bb.get("value"), bb.get("parentId")))
        return out, live

    # Two probes, because one alone is passable by a no-op: the tails must
    # go *and* the flag's lead must stay. Against the old code (nothing
    # deleted) the first fails; against a rule that dropped every stub
    # whose pin the wire reaches, the second does.
    rc, log = run(d / "flag.epro2", d / "n.json", "--skip-flagged",
                  "--local-rail", "200", "--labels-outward",
                  "-o", d / "out.epro2")
    before, _ = nets_with_wire(d / "flag.epro2")
    after, live = nets_with_wire(d / "out.epro2")
    check("a stub whose pin the drawn wire reaches is deleted",
          rc == 0 and len(gone(d / "flag.epro2", d / "out.epro2")) == 2,
          f"{sorted(gone(d / 'flag.epro2', d / 'out.epro2')) if rc == 0 else log}")
    check("...but the one carrying the flag stays, it is the flag's lead",
          rc == 0 and (0, 0, 0, 20) not in gone(d / "flag.epro2",
                                                d / "out.epro2"),
          f"{sorted(gone(d / 'flag.epro2', d / 'out.epro2')) if rc == 0 else log}")
    check("...and a wire was drawn to make them dead in the first place",
          rc == 0 and set(wires(d / "out.epro2")) - set(wires(d / "flag.epro2")),
          log)
    check("...and it says how many it dropped", "stub(s) dropped" in log, log)
    # The name leaves with the stub only if nobody carried it: `epro.py
    # diff` counts one ATTR key=NET per pin, so a deletion that took the
    # label too would show up as a lost pin the moment the sheet is
    # checked, and as nothing at all to a reader.
    check("...every net name survives the deletion, one per pin",
          rc == 0 and len(before) == len(after) == 3, f"{before} -> {after}")
    check("...re-parented onto a wire that is still in the file",
          rc == 0 and all(p in live for _, p in after), after)

    # The name a node has left on screen is worth more than the tidier
    # drawing: with no flag, one label per node stays visible, and the stub
    # under that label keeps its line.
    rc2, log2 = run(d / "noflag.epro2", d / "n.json", "--labels-outward",
                    "-o", d / "k.epro2")
    k_gone = gone(d / "noflag.epro2", d / "k.epro2")
    check("a stub whose label is the node's visible name is kept",
          rc2 == 0 and len(k_gone) == 2 and (0, 0, 0, 20) not in k_gone
          and visible(d / "k.epro2").get("SIG") == 1,
          f"{sorted(k_gone) if rc2 == 0 else log2} "
          f"{visible(d / 'k.epro2') if rc2 == 0 else ''}")
    # ...and --keep-text turns the whole rule off, because with every label
    # visible no stub is redundant-looking enough to drop. A probe that
    # cannot fail is what this file exists to avoid, so the mechanism gets
    # a switch and the switch gets a probe.
    rc3, log3 = run(d / "noflag.epro2", d / "n.json", "--keep-text",
                    "-o", d / "kt2.epro2")
    check("...and --keep-text keeps every stub, so the rule is not a no-op",
          rc3 == 0 and not gone(d / "noflag.epro2", d / "kt2.epro2"),
          f"{sorted(gone(d / 'noflag.epro2', d / 'kt2.epro2')) if rc3 == 0 else log3}")

    # --- the stub a *pre-existing* wire has made dead ---------------------
    # `netflags --ground-bus` runs one line through the ground pins of a
    # row, so the members' own stubs hang below it as the comb the bus was
    # drawn to replace. That wire is not one schwire drew and its net --
    # ground -- is never drawn, so nothing else in the tool can reach these
    # stubs: the drawn-wire path above is closed to them by construction.
    # The name is on screen as the ground symbol at the end of the run, so
    # the tail's own label goes rather than being kept for naming the node.
    d = tmp / "bus"; d.mkdir()
    bparts = [("R1", 0, 0, [(0, 0, "GND")]),
              ("R2", 100, 0, [(0, 0, "GND")]),
              ("R3", 200, 0, [(0, 0, "GND")])]
    netlist(d / "n.json", bparts)
    build(d / "plain.epro2", bparts, flags=[(0, 20)], vertical=True)

    def add_bus(src, dst, seg, net=None):
        """Splice a bare WIRE+LINE through `seg` into the sheet.

        No NET attribute on it, which is the shape netflags writes and the
        reason the label has to travel to it by id: there is no net name on
        the wire to re-derive one from. `net` adds one, for the probe that
        asks what happens when the wire says a net of its own.
        """
        z = zipfile.ZipFile(src)
        lines = z.read("doc.epru").decode("utf-8").split("\n")
        k = next(i for i, l in enumerate(lines) if '"SYMBOL"' in l)
        wid = "buswire"
        lines[k:k] = [
            json.dumps({"type": "WIRE", "ticket": 900, "id": wid})
            + "||" + json.dumps({"zIndex": 99}) + "|",
            json.dumps({"type": "LINE", "ticket": 901, "id": "busline"})
            + "||" + json.dumps({"lineGroup": wid, "startX": seg[0],
                                 "startY": seg[1], "endX": seg[2],
                                 "endY": seg[3]}) + "|"]
        if net is not None:
            lines[k + 2:k + 2] = [
                json.dumps({"type": "ATTR", "ticket": 902, "id": "busnet"})
                + "||" + json.dumps({"key": "NET", "value": net,
                                     "parentId": wid, "valueVisible": False})
                + "|"]
        with zipfile.ZipFile(dst, "w") as w:
            w.writestr("doc.epru", "\n".join(lines))

    add_bus(d / "plain.epro2", d / "bus.epro2", (0, 0, 200, 0))
    rc4, log4 = run(d / "bus.epro2", d / "n.json", "-o", d / "bout.epro2")
    b_before, _ = nets_with_wire(d / "bus.epro2")
    b_after, b_live = nets_with_wire(d / "bout.epro2")
    b_gone = gone(d / "bus.epro2", d / "bout.epro2")
    check("a stub the ground bus reaches is deleted", rc4 == 0
          and b_gone == {(100, 0, 100, 20), (200, 0, 200, 20)}, b_gone or log4)
    check("...but the run's own symbol lead stays, it is the flag's lead",
          rc4 == 0 and (0, 0, 0, 20) not in b_gone, b_gone or log4)
    check("...and every name survives, carried by the bus wire",
          rc4 == 0 and len(b_before) == len(b_after) == 3
          and all(p == "buswire" or p in b_live for _, p in b_after),
          b_after)
    check("...and the vacated text is hidden, the symbol naming the node",
          rc4 == 0 and visible(d / "bout.epro2").get("GND") == 1,
          visible(d / "bout.epro2") if rc4 == 0 else log4)
    # The inverse: without the bus nothing reaches those pins and ground is
    # never drawn, so every stub stays. This is the probe that fails if the
    # rule is loosened to "any stub whose pin it cannot see is dead".
    rc5, log5 = run(d / "plain.epro2", d / "n.json", "-o", d / "pout.epro2")
    check("...and with no bus reaching them, nothing is dropped",
          rc5 == 0 and not gone(d / "plain.epro2", d / "pout.epro2"),
          f"{sorted(gone(d / 'plain.epro2', d / 'pout.epro2')) if rc5 == 0 else log5}")
    # ...and a wire that names a net of its own is not a bus. Trimming a
    # stub onto it would be documenting a short as if it were a rail, so it
    # is refused and the geometry is left for `erc.py` to report.
    add_bus(d / "plain.epro2", d / "sig.epro2", (0, 0, 200, 0), net="SIG")
    rc6, log6 = run(d / "sig.epro2", d / "n.json", "-o", d / "sout.epro2")
    check("...and a wire naming another net is not one",
          rc6 == 0 and not gone(d / "sig.epro2", d / "sout.epro2"),
          f"{sorted(gone(d / 'sig.epro2', d / 'sout.epro2')) if rc6 == 0 else log6}")

    test_hand_wire_crossing(tmp)
    test_hand_wire_steered_round(tmp)
    test_hand_wire_keeps_the_name(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all schwire tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
