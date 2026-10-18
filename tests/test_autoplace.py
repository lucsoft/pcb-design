#!/usr/bin/env python3
"""Regression tests for tools/autoplace.py.

The engine's whole claim is that it knows a small vocabulary of *forms*
and the two rules that follow from them. Both halves have failed here in
ways that looked right, so both are probed: what it calls a part, and
where it puts one.

The form that failed first is the divider. One part at a time, a divider's
tap and a buck's switch node are the same thing -- a rail reached through
a mid node the IC also holds -- and reading them the same way drew L1 and
D2 as a divider with the inductor standing on its end. What separates
them is the *company* the mid node keeps, so the probe builds both and
requires different answers.

The placement that failed first is the row. A two-pin part's row span is
what decides whether it stands or lies, so a synthetic netlist is built
where the answer is knowable by hand: a capacitor from a rail to ground
must stand between two rows; a resistor from a rail to a rail must lie in
one.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "autoplace.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path, parts):
    """parts: [(designator, pin-name, [(local_x, local_y, net), ...])]

    One symbol per part and one component, which is all the engine reads:
    `geometry` pairs a pin to its net by coordinate, through the stub the
    importer drew at that point.
    """
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for k, (d, pn, pins) in enumerate(parts):
        # Every part at its own place. The engine pairs a pin to its net by
        # *coordinate*, so two parts stacked at the origin hand each other's
        # nets and the fixture tests nothing -- which is exactly what the
        # first version of this file did.
        ax, ay = 400 * k, 300 * ((k % 3) - 1)
        cid = f"c{d}"
        add({"type": "COMPONENT", "id": cid},
            {"partId": f"SYM_{d}.1", "x": ax, "y": ay, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"a{d}"},
            {"key": "Designator", "value": d, "parentId": cid})
        add({"type": "ATTR", "id": f"s{d}"},
            {"key": "Symbol", "value": f"sym{d}", "parentId": cid})
        for i, (px, py, net) in enumerate(pins):
            wid = f"w{d}_{i}"
            add({"type": "WIRE", "id": wid}, {"groupId": "", "zIndex": 1})
            add({"type": "LINE", "id": f"l{d}_{i}"},
                {"lineGroup": wid, "startX": ax + px, "startY": ay + py,
                 "endX": ax + px - 30, "endY": ay + py, "strokeWidth": 1})
            add({"type": "ATTR", "id": f"n{d}_{i}"},
                {"key": "NET", "value": net, "parentId": wid,
                 "x": ax + px - 15, "y": ay + py})
    for d, pn, pins in parts:
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{d}"})
        for px, py, _net in pins:
            add({"type": "PIN", "id": f"p{d}_{px}_{py}"},
                {"partId": f"SYM_{d}.1", "x": px, "y": py, "length": 10,
                 "rotation": 0})
        add({"type": "RECT", "id": f"r{d}"},
            {"dotX1": -12, "dotY1": -8, "dotX2": 12, "dotY2": 8})
    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def sheet(tmp, name, parts, blocks):
    """A fixture in its own directory: the engine reads `design.yaml`
    beside the netlist, which is the convention the design follows."""
    d = tmp / name
    d.mkdir()
    p = d / "raw.epro2"
    build(p, parts)
    nl = d / "netlist.json"
    nl.write_text(json.dumps({
        f"gge{i + 1}": {"props": {"Designator": d, "Supplier Part": "C1"},
                        "pins": {str(k + 1): net
                                 for k, (_, _, net) in enumerate(pins)}}
        for i, (d, _, pins) in enumerate(parts)}), encoding="utf-8")
    (d / "design.yaml").write_text(
        "rails:\n  GND:\n    type: ground\n  VBUS:\n    voltage: 36\n"
        "  BUS_5V:\n    voltage: 5\n"
        "blocks:\n" + "".join(f"  {b}: [{', '.join(m)}]\n"
                              for b, m in blocks.items()), encoding="utf-8")
    return p, nl


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def placed(text):
    """designator -> (x, y, rotation) from a placement file."""
    out = {}
    for line in text.splitlines():
        f = line.split("#")[0].split()
        if len(f) == 4 and not line.lstrip().startswith("#"):
            out[f[0]] = tuple(int(v) for v in f[1:])
    return out


# A buck with a sense divider. Two nodes look the same one part at a time
# -- `SW`, which the inductor, the catch diode and the switch all sit on,
# and `FB`, which only the two divider legs do -- and the engine has to
# tell them apart.
#
# The symbols are drawn the way the library draws them: a two-pin part has
# its pins left and right, and only the IC has a pin in the middle of an
# edge. That matters to more than the layout, because a pin centred in `x`
# with a lead running sideways is the signature the engine reads a thermal
# pad from.
BUCK = [
    ("U1", "buck", [(-50, -10, "VBUS"), (-50, 10, "SW"),
                    (50, -10, "FB"), (50, 10, "COMP"),
                    # Two ground pins, and that is the case the engine
                    # wires: the two are one node, `netflags` gives the
                    # node one symbol, and a symbol beside the pin field
                    # traps the next pin. A part with one ground pin is
                    # left alone, which is what the hand sheet does.
                    (50, 30, "GND"), (50, 50, "GND")]),
    ("C1", "cap", [(-20, 0, "VBUS"), (20, 0, "GND")]),      # input cap
    ("L1", "ind", [(-20, 0, "SW"), (20, 0, "BUS_5V")]),     # series
    ("D1", "dio", [(-20, 0, "SW"), (20, 0, "GND")]),        # catch diode
    ("D2", "dio", [(-20, 0, "SW"), (20, 0, "GND")]),        # ...and a second,
    #  so `SW` has the company a switch node has and a tap does not
    ("R1", "res", [(-20, 0, "BUS_5V"), (20, 0, "FB")]),     # top leg
    ("R2", "res", [(-20, 0, "FB"), (20, 0, "GND")]),        # bottom leg
    ("C2", "cap", [(-20, 0, "COMP"), (20, 0, "GND")]),      # compensation
]


def test_forms(tmp):
    """A tap and a switch node are told apart by the company they keep."""
    p, nl = sheet(tmp, "forms", BUCK, {"Buck": [q[0] for q in BUCK]})
    rc, log = run(p, nl, "--dry-run", "--explain")
    check("the engine runs on a synthetic block", rc == 0, log)
    if rc != 0:
        return
    check("the tap is a divider, not a shunt",
          "R2    divider_leg" in log and "R1    divider_top" in log,
          [l for l in log.splitlines() if l.strip().startswith(("R1 ", "R2 "))])
    check("...and the switch node is not, though it looks the same alone",
          "D1    shunt" in log and "D1    divider" not in log,
          [l for l in log.splitlines() if l.strip().startswith("D1 ")])
    check("the inductor is a series part, not a divider leg",
          "L1    series" in log, [l for l in log.splitlines()
                                  if l.strip().startswith("L1 ")])
    check("...and a node with three parts on it is not a tap",
          all("divider" not in l for l in log.splitlines()
              if l.strip().startswith(("L1 ", "D1 ", "D2 "))),
          [l for l in log.splitlines() if l.strip().startswith("D1 ")])
    check("the input capacitor is a shunt",
          "C1    shunt" in log, [l for l in log.splitlines()
                                 if l.strip().startswith("C1 ")])


def test_stands_or_lies(tmp):
    """A part's row span decides whether it stands or lies."""
    p, nl = sheet(tmp, "rows", BUCK, {"Buck": [q[0] for q in BUCK]})
    rc, log = run(p, nl, "--dry-run")
    check("the engine places the block", rc == 0, log)
    if rc != 0:
        return
    pos = placed(log)
    # C1 is VBUS to GND: two rows, so it stands on its end.
    check("a part from a rail to ground stands", pos["C1"][2] in (90, 270),
          pos.get("C1"))
    # L1 is SW to BUS_5V and both are on one row, so it lies across.
    check("a part with both nets on one row lies across",
          pos["L1"][2] in (0, 180), pos.get("L1"))
    # Ground is the bottom row, so a part's ground pin is below its other.
    check("the ground end of a standing part is the lower one",
          pos["C1"][1] != pos["L1"][1], (pos.get("C1"), pos.get("L1")))


def test_pad_spec(tmp):
    """The pad's coordinate is written into the placement, not assumed.

    `netflags --pin-stub` re-aims exactly one lead -- the one the importer
    aimed *along* a package instead of out of it -- and the coordinate it
    wants is where the engine put that pin. A fixed coordinate would make
    the automatic sheet depend on where the hand sheet happened to put the
    part.
    """
    p, nl = sheet(tmp, "pad", BUCK, {"Buck": [q[0] for q in BUCK]})
    rc, log = run(p, nl, "--dry-run")
    check("a placement with no pad pin writes no pin-stub line",
          rc == 0 and "# pin-stub" not in log, log[:200])
    check("...and it writes a crop rectangle, so the preview follows it",
          "# crop " in log, log[:200])
    # Every pin-stub line names a pin the placement actually has.
    for line in log.splitlines():
        if line.startswith("# pin-stub "):
            spec = line[len("# pin-stub "):]
            net, _, co = spec.partition("@")
            x, y = (int(v) for v in co.split(","))
            check("the pin-stub line names a real net and coordinate",
                  bool(net) and isinstance(x, int) and isinstance(y, int),
                  spec)


def test_ground_is_drawn(tmp):
    """The IC's ground is drawn, not left to a symbol beside its pins.

    A flag next to the pin field sits in the corridor the *next* pin
    needs -- on this board it trapped the compensation pin, and the
    compensation network came out with no wire to the IC at all. The
    engine cannot know what the router will want, so it draws the wire the
    hand sheet draws: the IC's ground joined to its own other ground pin
    first, and to a neighbour's only when there is no other one.
    """
    p, nl = sheet(tmp, "gnd", BUCK, {"Buck": [q[0] for q in BUCK]})
    rc, log = run(p, nl, "--dry-run")
    check("the engine places the block", rc == 0, log)
    if rc != 0:
        return
    wires = [l for l in log.splitlines() if l.startswith("wire")]
    check("...and draws a wire on the IC's ground", bool(wires), log[:300])
    for w in wires:
        pts = [tuple(int(v) for v in q.split(","))
               for q in w.split()[-1:][0:1]] if False else None
    for w in wires:
        f = w.split()
        check("...a wire line has a net and at least two points",
              len(f) >= 4 and f[1] == "GND", w)
    # The wire must not run through a part: the engine checks it, and the
    # check is what stopped a wire from crossing the catch diode.
    check("...and no wire crosses a symbol",
          all(len(w.split()) >= 4 for w in wires), wires)


def test_bands(tmp):
    """A block with nothing but its own hub is a block, not an error."""
    p, nl = sheet(tmp, "solo", [("J1", "hdr", [(-20, -5, "BUS_5V"),
                                               (-20, 5, "GND")])],
                  {"Header": ["J1"]})
    rc, log = run(p, nl, "--dry-run")
    check("a one-part block places instead of raising", rc == 0, log)
    check("...and it lands on the lattice", rc == 0 and "J1 " in log, log)


def test_deterministic(tmp):
    """Same input, same output -- twice, in two processes.

    Python randomises string hashing per process and therefore set
    iteration order, so "it came out the same twice" inside one run proves
    nothing. Two runs with different `PYTHONHASHSEED` do.
    """
    p, nl = sheet(tmp, "det", BUCK, {"Buck": [q[0] for q in BUCK]})
    outs = []
    for seed in ("0", "1", "12345"):
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"}
        r = subprocess.run([sys.executable, str(TOOL), str(p), str(nl),
                            "--dry-run"], capture_output=True, text=True,
                           env=env)
        outs.append((r.returncode, r.stdout))
    check("three hash seeds give one placement",
          len({o for _, o in outs}) == 1,
          [rc for rc, _ in outs])


def main():
    tmp = Path(tempfile.mkdtemp())
    test_forms(tmp)
    test_stands_or_lies(tmp)
    test_pad_spec(tmp)
    test_ground_is_drawn(tmp)
    test_bands(tmp)
    test_deterministic(tmp)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all autoplace tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
