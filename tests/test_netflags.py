#!/usr/bin/env python3
"""Regression tests for tools/netflags.py.

A flag's own pin lands on the pin it flags, so placing one is making a
connection. Put it on the wrong coordinate and it is a short -- which is
why the net is read off the stub already there and never worked out, and
why the probe that matters is the one checking a flag never lands on a pin
carrying a different net.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "netflags.py"
FLAG_PART = "pid8a0e77bacb214e"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def write(path, recs):
    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def donor(path):
    """A file that places a GND flag, as the hand-drawn reference does."""
    r, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        r.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    add({"type": "COMPONENT", "id": "f1"},
        {"partId": FLAG_PART, "x": 0, "y": 0, "rotation": 0, "isMirror": False})
    for k, v in (("Symbol", "symG"), ("Device", "devG"), ("Name", "GND")):
        add({"type": "ATTR", "id": f"fa{k}"},
            {"key": k, "value": v, "parentId": "f1"})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "symG"})
    add({"type": "PART", "id": "pG"}, {"BBOX": [-10, -10, 10, -19], "title": ""})
    add({"type": "PIN", "id": "pinG"},
        {"partId": FLAG_PART, "x": 0, "y": 0, "length": 10, "rotation": 270})
    add({"type": "DOCHEAD"}, {"docType": "DEVICE", "uuid": "devG"})
    # The device hands every instance this net name unless the instance
    # overrides it. That is how the donor's +24V flag differs from its +5V
    # one while sharing a device, and it is the mechanism that silently
    # merges rails if a tool sets only `Name`.
    add({"type": "META", "id": "mG"},
        {"title": "Ground-GND", "attributes": {"Global Net Name": "GND"}})
    write(path, r)


def target(path, stubs, stray_pins=()):
    """stubs: [(pin_x, pin_y, free_x, free_y, net)]

    stray_pins: [(x, y)] -- pins with no stub. The importer draws a line
    from every pin, so on a real sheet a pin on the ground bus is always
    *also* a line touching it and the crossing guard alone would refuse.
    A pin the guards can only see through the pin list is the one shape
    that tells the two guards apart, so it has to be built by hand.
    """
    r, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        r.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, (px, py, fx, fy, net) in enumerate(stubs):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": f"P{i}.1", "x": px, "y": py, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{i}"},
            {"key": "Designator", "value": f"R{i + 1}", "parentId": f"c{i}"})
        add({"type": "ATTR", "id": f"s{i}"},
            {"key": "Symbol", "value": f"sym{i}", "parentId": f"c{i}"})
        add({"type": "WIRE", "id": f"w{i}"}, {"groupId": "", "zIndex": 1})
        add({"type": "LINE", "id": f"l{i}"},
            {"lineGroup": f"w{i}", "startX": px, "startY": py,
             "endX": fx, "endY": fy})
        add({"type": "ATTR", "id": f"n{i}"},
            {"key": "NET", "value": net, "parentId": f"w{i}"})
    syms = [(f"sym{i}", f"P{i}.1") for i in range(len(stubs))]
    for j, (sx, sy) in enumerate(stray_pins):
        k = f"x{j}"
        add({"type": "COMPONENT", "id": f"c{k}"},
            {"partId": f"P{k}.1", "x": sx, "y": sy, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{k}"},
            {"key": "Designator", "value": f"R{k}", "parentId": f"c{k}"})
        add({"type": "ATTR", "id": f"s{k}"},
            {"key": "Symbol", "value": f"sym{k}", "parentId": f"c{k}"})
        syms.append((f"sym{k}", f"P{k}.1"))
    for su, pid in syms:
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": su})
        add({"type": "PIN", "id": f"p{su}"},
            {"partId": pid, "x": 0, "y": 0, "length": 10, "rotation": 0})
    write(path, r)


def donor_drawn(path):
    """A donor whose flag symbol actually draws something.

    The shared donor's symbol carries a PART and a PIN and no graphic
    primitive at all, so a flag made from it has no extent and can never
    be found overlapping anything. Nothing about the clash rule was
    reachable with it.
    """
    r, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        r.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    add({"type": "COMPONENT", "id": "f1"},
        {"partId": FLAG_PART, "x": 0, "y": 0, "rotation": 0,
         "isMirror": False})
    for k, v in (("Symbol", "symG"), ("Device", "devG"), ("Name", "GND")):
        add({"type": "ATTR", "id": f"fa{k}"},
            {"key": k, "value": v, "parentId": "f1"})
    # A supply flag as well, placed on the canvas beside the ground one.
    # With only a ground symbol on offer every rail in a fixture has to
    # borrow it -- and then it counts as ground, which is exempt from
    # the per-node rule, so the probe tested the exemption rather than
    # the rule. It goes here, with the other canvas records: put after
    # the ground DEVICE it lands *inside* that document and gets copied
    # along with it, which broke two older probes by one flag each.
    add({"type": "COMPONENT", "id": "f2"},
        {"partId": FLAG_PART, "x": 400, "y": 0, "rotation": 0,
         "isMirror": False})
    for k, v in (("Symbol", "symP"), ("Device", "devP"), ("Name", "+5V")):
        add({"type": "ATTR", "id": f"fb{k}"},
            {"key": k, "value": v, "parentId": "f2"})

    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "symG"})
    add({"type": "PART", "id": "pG"}, {"BBOX": [-10, -10, 10, -19],
                                       "title": ""})
    add({"type": "PIN", "id": "pinG"},
        {"partId": FLAG_PART, "x": 0, "y": 0, "length": 10, "rotation": 270})
    # the bar, reaching 55 units each way -- wider than a stub is long,
    # which is what makes it land back on its own part
    add({"type": "POLY", "id": "gpoly"},
        {"points": [{"x": -55, "y": 0}, {"x": 55, "y": 0},
                    {"x": 55, "y": 30}, {"x": -55, "y": 30}]})
    add({"type": "DOCHEAD"}, {"docType": "DEVICE", "uuid": "devG"})
    add({"type": "META", "id": "mG"},
        {"title": "Ground-GND", "attributes": {"Global Net Name": "GND"}})

    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "symP"})
    add({"type": "PART", "id": "pP"}, {"BBOX": [-10, -10, 10, -19],
                                       "title": ""})
    add({"type": "PIN", "id": "pinP"},
        {"partId": FLAG_PART, "x": 0, "y": 0, "length": 10, "rotation": 270})
    add({"type": "POLY", "id": "ppoly"},
        {"points": [{"x": -5, "y": -10}, {"x": 5, "y": -10},
                    {"x": 5, "y": -5}, {"x": -5, "y": -5}]})
    add({"type": "DOCHEAD"}, {"docType": "DEVICE", "uuid": "devP"})
    add({"type": "META", "id": "mP"},
        {"title": "Power-5V", "attributes": {"Global Net Name": "+5V"}})
    write(path, r)


def target_boxed(path, stubs, half=20, group=None):
    """Like target(), but every component symbol draws a body.

    `group` maps a stub index to a component index, so several stubs can
    belong to one part. Without it every stub gets its own component,
    which made the two-flags-on-one-part case impossible to express.
    """
    r, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        r.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    g = group or {i: i for i in range(len(stubs))}
    comps = {}
    for i, (px, py, fx, fy, net) in enumerate(stubs):
        c = g[i]
        if c not in comps:
            comps[c] = (px, py)
            add({"type": "COMPONENT", "id": f"c{c}"},
                {"partId": f"P{c}.1", "x": px, "y": py, "rotation": 0,
                 "isMirror": False})
            add({"type": "ATTR", "id": f"d{c}"},
                {"key": "Designator", "value": f"R{c + 1}",
                 "parentId": f"c{c}"})
            add({"type": "ATTR", "id": f"s{c}"},
                {"key": "Symbol", "value": f"sym{c}", "parentId": f"c{c}"})
        add({"type": "WIRE", "id": f"w{i}"}, {"groupId": "", "zIndex": 1})
        add({"type": "LINE", "id": f"l{i}"},
            {"lineGroup": f"w{i}", "startX": px, "startY": py,
             "endX": fx, "endY": fy})
        add({"type": "ATTR", "id": f"n{i}"},
            {"key": "NET", "value": net, "parentId": f"w{i}"})
    for c, (cx, cy) in comps.items():
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{c}"})
        for i, (px, py, _fx, _fy, _n) in enumerate(stubs):
            if g[i] == c:
                add({"type": "PIN", "id": f"p{i}"},
                    {"partId": f"P{c}.1", "x": px - cx, "y": py - cy,
                     "length": 10, "rotation": 0})
        add({"type": "RECT", "id": f"rc{c}"},
            {"dotX1": -half, "dotY1": -half, "dotX2": half, "dotY2": half})
    write(path, r)


def append_line(path, a, b):
    """Add a bare wire to a built fixture -- the hand-drawn kind.

    It carries no NET attribute, exactly as `handplace.py`'s `wire` line
    emits it, so the only thing that can attribute it is where it lands.
    """
    z = zipfile.ZipFile(path)
    entries = {n: z.read(n) for n in z.namelist()}
    z.close()
    wid = "handwire"
    entries["doc.epru"] += (
        f'\n{{"type":"WIRE","id":"{wid}"}}||'
        f'{{"groupId":"","zIndex":1}}|'
        f'\n{{"type":"LINE","id":"{wid}l"}}||'
        f'{{"lineGroup":"{wid}","startX":{a[0]},"startY":{a[1]},'
        f'"endX":{b[0]},"endY":{b[1]},"strokeWidth":1}}|').encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as w:
        for n, data in entries.items():
            w.writestr(n, data)


def read(path):
    z = zipfile.ZipFile(path)
    out = []
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        out.append((json.loads(h), json.loads(b.rstrip("|"))))
    return out


def test_flag_clash(tmp):
    """A flag that can only be drawn across a part is not placed at all.

    The pin keeps its net name, which is what names the net; a symbol
    drawn over a package is a fault the sheet gets marked down for.
    """
    dn = tmp / "d2.epro2"
    donor_drawn(dn)

    # The stub points *left*, back over its own 20-unit-half body, and
    # the flag bar reaches 55 units -- so wherever it is rooted it lands
    # on the part. This is J4 on the supply sheet.
    # Two parts: one whose flag cannot be placed and one whose can. With
    # only the clashing part the tool skipped it, found nothing left to
    # place and exited 1, so the probe measured the exit status rather
    # than the rule.
    # A part carrying TWO rail nets, both of whose flags would be drawn
    # across its body. One is skipped; the other is kept, because a part
    # with no flag and no wire reads as unconnected and that is worse
    # than a symbol drawn a little close. With only one net the probe
    # tested nothing: the single flag is that part's only mark and the
    # rule keeps it, correctly.
    tg = tmp / "clash.epro2"
    target_boxed(tg, [(0, -10, -30, -10, "GND"), (0, 10, -30, 10, "VCC"),
                      (600, 0, 600, 200, "GND")],
                 group={0: 0, 1: 0, 2: 1})
    rc, log = run(tg, "--donor", dn, "--nets", "GND,VCC=GND",
                  "--one-per-part", "-o", tmp / "clash-out.epro2")
    check("one of two crowded flags on a part is skipped",
          rc == 0 and "would be drawn across a part" in log, log)
    # The outcome, not the message: whichever path kept it, the crowded
    # part must still carry a flag. Asserting the sentence tied the
    # probe to one of the two ways the rule can reach the right answer.
    if rc == 0:
        near = [b for h, b in read(tmp / "clash-out.epro2")
                if h.get("type") == "COMPONENT"
                and b.get("partId") == FLAG_PART
                and abs(b.get("x", 999)) < 100]
        check("...and the crowded part still carries one", near,
              [(b.get("x"), b.get("y")) for h, b in
               read(tmp / "clash-out.epro2")
               if b.get("partId") == FLAG_PART])
    if rc == 0:
        placed = sum(1 for h, b in read(tmp / "clash-out.epro2")
                     if h.get("type") == "COMPONENT"
                     and b.get("partId") == FLAG_PART)
        check("...so two flags land, not three and not one",
              placed == 2, f"{placed} placed")

    # The inverse: a stub pointing away from a small body has room, and
    # the flag must still be placed. Without this the rule could skip
    # everything and the clash count would read zero for the wrong reason.
    tg2 = tmp / "clear.epro2"
    target_boxed(tg2, [(0, 0, 0, 200, "GND")], half=5)
    rc, log = run(tg2, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", tmp / "clear-out.epro2")
    check("a flag with room is still placed",
          rc == 0 and "would be drawn across a part" not in log, log)
    if rc == 0:
        placed = sum(1 for h, b in read(tmp / "clear-out.epro2")
                     if h.get("type") == "COMPONENT"
                     and b.get("partId") == FLAG_PART)
        check("...and exactly one flag lands", placed == 1, f"{placed} placed")


def test_flag_lead(tmp):
    """A crowded flag is carried out of the pin field, not dropped.

    An IC's pins are ten units apart and a ground symbol is twenty
    across, so a flag at the stub end of a dense row must touch its
    neighbours. Dropping it loses the symbol; keeping it loses the
    reading -- a ground triangle beside an enable stub says the
    regulator is held off. The lead is the third answer, and it is wire
    on the net the flag already belongs to.
    """
    dn = tmp / "d3.epro2"
    donor_drawn(dn)

    # Two pins one pitch apart: GND and a signal. The ground flag at the
    # stub end would sit on the signal stub's end.
    tg = tmp / "crowd.epro2"
    target_boxed(tg, [(0, 0, -30, 0, "GND"), (0, 10, -30, 10, "SIG")],
                 half=5, group={0: 0, 1: 0})
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--strict-flags", "--flag-clearance", "15",
                  "--flag-lead", "40", "-o", tmp / "crowd-out.epro2")
    check("a crowded flag is led out of the pin field",
          rc == 0 and "carried 40 units out" in log, log)
    if rc == 0:
        recs = read(tmp / "crowd-out.epro2")
        flag = [b for h, b in recs if h.get("type") == "COMPONENT"
                and b.get("partId") == FLAG_PART]
        check("...and the flag sits at the lead's tip, not the stub end",
              flag and flag[0]["x"] <= -70, flag)
        leads = [b for h, b in recs if h.get("type") == "LINE"
                 and abs(b.get("startX", 0) - b.get("endX", 0)) == 40]
        check("...with the lead drawn on its own net's line group",
              leads and leads[0].get("lineGroup"), leads)

    # The clearance: a foreign stub end sitting where the lead would go
    # must stop it. Without this the rule could carry a flag straight
    # through somebody else's pin and the probes above would still pass.
    tg2 = tmp / "blocked.epro2"
    target_boxed(tg2, [(0, 0, -30, 0, "GND"), (0, 10, -30, 10, "SIG"),
                       (-100, 0, -70, 0, "OTHER")],
                 half=5, group={0: 0, 1: 0, 2: 1})
    rc, log = run(tg2, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--strict-flags", "--flag-clearance", "15",
                  "--flag-lead", "40", "-o", tmp / "blocked-out.epro2")
    # Not "no lead": a lead may turn now, so a blocker straight ahead is
    # a reason to go round rather than to give up. What must hold is
    # that nothing the lead draws comes near the foreign stub end --
    # which is the property the rule exists for. Asserting the absence
    # of a lead tested the old implementation, not the rule.
    if rc == 0:
        near = []
        for h, b in read(tmp / "blocked-out.epro2"):
            if h.get("type") != "LINE" or "startX" not in b:
                continue
            ends = ((b["startX"], b["startY"]), (b["endX"], b["endY"]))
            # The blocker's own stub ends there by construction; it is
            # the lead that must stay away. The first version of this
            # check counted the blocker and failed against correct code.
            if (-100, 0) in ends:
                continue
            for px, py in ends:
                if abs(px - (-70)) <= 12 and abs(py - 0) <= 12:
                    near.append((px, py))
        check("no lead segment reaches the foreign stub end", not near,
              f"{near}\n{log}")
        # And it must actually go round, not quietly give up: a blocker
        # straight ahead is a reason to turn. Without this the probe
        # passed whether the turning lead existed or not.
        check("...and the lead goes round it", "carried" in log, log)
        turns = [b for h, b in read(tmp / "blocked-out.epro2")
                 if h.get("type") == "LINE" and "startX" in b
                 and b["startX"] == b["endX"]
                 and abs(b["endY"] - b["startY"]) == 40]
        check("...with a segment across its own stub", turns,
              f"{len(turns)} turned segment(s)\n{log}")
    else:
        check("a blocked lead is refused rather than forced", True, log)

    # Without the option the flag stays put: the lead must be the
    # difference, not some other change made at the same time.
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--strict-flags", "--flag-clearance", "15",
                  "-o", tmp / "noled-out.epro2")
    check("without --flag-lead nothing is carried out",
          "carried" not in log, log)


def test_one_per_node(tmp):
    """One flag per node, not per pin -- and ground keeps all of them.

    A rail that is drawn as a wire needs one name. Four +5V flags along
    one drawn rail say the same thing four times, and one of them sat
    on a stub that ended free below the line and read as an open
    connection.

    Ground is the exception and the probe has to show it: ground is
    never drawn as a wire here, so collapsing it to one symbol per node
    leaves most ground pins with nothing at all -- which is what one
    flag for the whole sheet looked like.
    """
    dn = tmp / "d4.epro2"
    donor_drawn(dn)

    # Three VCC pins close together, one far away; four ground pins.
    stubs = [(0, 0, -30, 0, "VCC"), (120, 0, 90, 0, "VCC"),
             (240, 0, 210, 0, "VCC"), (2000, 0, 1970, 0, "VCC"),
             (0, 200, -30, 200, "GND"), (120, 200, 90, 200, "GND"),
             (240, 200, 210, 200, "GND"), (2000, 200, 1970, 200, "GND")]
    tg = tmp / "node.epro2"
    target_boxed(tg, stubs, half=5)
    rc, log = run(tg, "--donor", dn, "--nets", "GND,VCC=+5V",
                  "--one-per-part", "--one-per-node", "400",
                  "-o", tmp / "node-out.epro2")
    check("one flag per node is accepted", rc == 0, log)
    if rc == 0:
        import re as _re
        got = dict(_re.findall(r"place\s+(\d+) x (\S+)", log))
        got = {v: int(k) for k, v in got.items()}
        check("...so the three close VCC pins share one flag",
              got.get("VCC") == 2, got)
        check("...and ground keeps one on every pin",
              got.get("GND") == 4, got)


def test_ground_bus(tmp):
    """Ground's own leads join where they land in one line.

    A stage's parts drop their ground leads to a common height, and a
    symbol per part states the same connection once per part. One line
    joins the run and one symbol marks it -- but only where the run is
    genuinely one line. A bus drawn across a pin, or across a lead on
    another net, is a short the net diff cannot see, so both are refused
    rather than drawn, and each probe has its inverse.
    """
    dn = tmp / "d5.epro2"
    donor_drawn(dn)

    # Three ground leads ending on one line, 100 units apart.
    tg = tmp / "bus.epro2"
    target(tg, [(0, 0, 0, -40, "GND"), (100, 0, 100, -40, "GND"),
                (200, 0, 200, -40, "GND")])
    out = tmp / "bus-out.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "GND",
                  "--ground-bus", "150", "-o", out)
    check("a collinear ground run is accepted", rc == 0, log)
    check("...and it becomes one symbol, not three",
          "place    1 x GND" in log, log)
    check("...joined by one bus wire",
          "1 ground run(s)" in log and "2 symbol(s) fewer" in log, log)
    if rc == 0:
        recs = read(out)
        lines = [b for h, b in recs if h.get("type") == "LINE"
                 and "startX" in b]
        ends = {(b["startX"], b["startY"], b["endX"], b["endY"])
                for b in lines}
        # Through the *pins*, at y=0, not through the stub ends at y=-40.
        # A bar at the ends is a comb of drops; the line the reader wants
        # meets each part where its ground pin actually is.
        bus = [b for b in lines
               if (b["startX"], b["startY"], b["endX"], b["endY"])
               in {(0, 0, 200, 0), (200, 0, 0, 0)}]
        check("...spanning the run from end to end", len(bus) == 1, ends)
        # The bus wire carries no NET attribute, and that is the point:
        # `epro.py diff` counts one ATTR key=NET per pin, so a wire of
        # its own would lift the export's count above the netlist's.
        if len(bus) == 1:
            grp = bus[0].get("lineGroup")
            wires = {h["id"] for h, b in recs if h.get("type") == "WIRE"}
            check("...on a WIRE record of its own", grp in wires, grp)
            check("...which carries no NET attribute",
                  not any(h.get("type") == "ATTR" and b.get("key") == "NET"
                          and b.get("parentId") == grp for h, b in recs),
                  log)

    # The inverse: a gap wider than the run is two leads, not a bus.
    # Without this the rule could join anything at one height and the
    # probe above would still pass.
    tg2 = tmp / "far.epro2"
    target(tg2, [(0, 0, 0, -40, "GND"), (500, 0, 500, -40, "GND")])
    rc, log = run(tg2, "--donor", dn, "--nets", "GND",
                  "--ground-bus", "150", "-o", tmp / "far-out.epro2")
    check("a gap wider than the run is not a bus",
          rc == 0 and "place    2 x GND" in log
          and "ground run(s)" not in log, log)

    # The inverse: a foreign lead crossing the span connects at the
    # crossing in a geometric schematic, which is a short -- so the bus
    # is refused and both leads keep their own symbol.
    #
    # The two ground leads are 120 apart, inside the 150 the bus allows,
    # so a run *would* form: this probe reaches the crossing guard. Span
    # them 200 and it would pass on the "gap wider than the run" rule
    # instead, which the probe above already covers -- the guard itself
    # would then be untested against a fixture that fails without it.
    tg3 = tmp / "cross.epro2"
    target(tg3, [(0, 0, 0, -40, "GND"), (120, 0, 120, -40, "GND"),
                 (60, -100, 60, 0, "VCC")])
    rc, log = run(tg3, "--donor", dn, "--nets", "GND,VCC=GND",
                  "--ground-bus", "150", "-o", tmp / "cross-out.epro2")
    check("a run crossed by another lead is refused",
          rc == 0 and "place    2 x GND" in log
          and "ground run(s)" not in log, log)

    # ...and one with a pin sitting on the span, which is the same short
    # reached the other way. 120 apart again. The pin carries no stub, so
    # no line crosses the span and only the pin guard can refuse it: give
    # it a lead and the crossing guard fires first, which is what happens
    # on a real sheet and would leave this guard untested.
    tg4 = tmp / "inside.epro2"
    target(tg4, [(0, 0, 0, -40, "GND"), (120, 0, 120, -40, "GND")],
           stray_pins=[(60, 0)])
    rc, log = run(tg4, "--donor", dn, "--nets", "GND,VCC=GND",
                  "--ground-bus", "150", "-o", tmp / "inside-out.epro2")
    check("a run with a pin on its span is refused",
          rc == 0 and "place    2 x GND" in log
          and "ground run(s)" not in log, log)

    # Without the option the leads stay separate: the bus has to be the
    # difference, not something the tool did anyway.
    rc, log = run(tg, "--donor", dn, "--nets", "GND",
                  "-o", tmp / "nobus-out.epro2")
    check("without --ground-bus nothing is joined",
          rc == 0 and "place    3 x GND" in log
          and "ground run(s)" not in log, log)


def placed_flags(path):
    """Every flag component in an export, as {net: [(x, y, rotation)]}.

    The net is unnamed on a flag -- it carries no NET attribute, which is
    what keeps the net diff unmoved -- so this reads the position and
    rotation only, and the caller knows which fixture pin it asked for.
    """
    rows = read(path)
    desig = {b["parentId"] for h, b in rows
             if h.get("type") == "ATTR" and b.get("key") == "Designator"}
    return [(b.get("x"), b.get("y"), b.get("rotation"))
            for h, b in rows
            if h.get("type") == "COMPONENT" and b.get("partId") == FLAG_PART]


def test_flag_turn(tmp):
    """A flag is turned when its own drawing would lie on its lead.

    The supply symbol draws its bar on the far side of its pin from the
    wire that feeds it. Reached from below that is right; reached from
    above it puts the bar back along the incoming wire, ten units of it
    sticking out past the bar, so the symbol reads as a crossbar
    threaded on a line rather than a flag. Only that case is turned --
    a bar perpendicular to its wire is what every other flag draws.

    Read off the drawing, not off the net name: the ground triangle
    hangs the other way, so the same two fixtures must give it the
    opposite pair of answers. A rule keyed on "GND" and "+5V" would pass
    the first half of this and be one more table to keep in step with
    the library.
    """
    dn = tmp / "d5.epro2"
    donor_drawn(dn)

    # The stub runs *down* from the pin, so the flag at its free end is
    # reached from above -- the case that needs the turn. The turn is
    # 180, not 90: the bar has to end up on the far side of the flag
    # from the wire, and a quarter turn would lay it along the lead.
    down = tmp / "down.epro2"
    target_boxed(down, [(0, 0, 0, 40, "VCC")], half=5)
    rc, log = run(down, "--donor", dn, "--nets", "VCC=+5V", "--one-per-part",
                  "-o", tmp / "down-out.epro2")
    if rc == 0:
        f = placed_flags(tmp / "down-out.epro2")
        check("a supply flag reached from above is turned about",
              f and f[0][2] == 180, f"{f}\n{log}")
    else:
        check("a supply flag reached from above is turned about", False, log)

    # The control: the same flag reached from below keeps rotation 0, so
    # the probe is measuring the rule and not "flags are rotated".
    up = tmp / "up.epro2"
    target_boxed(up, [(0, 0, 0, -40, "VCC")], half=5)
    rc, log = run(up, "--donor", dn, "--nets", "VCC=+5V", "--one-per-part",
                  "-o", tmp / "up-out.epro2")
    if rc == 0:
        f = placed_flags(tmp / "up-out.epro2")
        check("...and one reached from below is not",
              f and f[0][2] == 0, f"{f}\n{log}")
    else:
        check("...and one reached from below is not", False, log)

    # The inverse probe: the ground symbol draws downwards, so the two
    # fixtures swap over. Without this the rule could be a turn applied
    # to every flag in a row and both probes above would still pass.
    gd = tmp / "gdown.epro2"
    target_boxed(gd, [(0, 0, 0, 40, "GND")], half=5)
    rc, log = run(gd, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", tmp / "gdown-out.epro2")
    if rc == 0:
        f = placed_flags(tmp / "gdown-out.epro2")
        check("...while a ground flag under the same stub is left alone",
              f and f[0][2] == 0, f"{f}\n{log}")
    else:
        check("...while a ground flag under the same stub is left alone",
              False, log)

    gu = tmp / "gup.epro2"
    target_boxed(gu, [(0, 0, 0, -40, "GND")], half=5)
    rc, log = run(gu, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", tmp / "gup-out.epro2")
    if rc == 0:
        f = placed_flags(tmp / "gup-out.epro2")
        check("...and turned when the stub puts it above its pin",
              f and f[0][2] == 180, f"{f}\n{log}")
    else:
        check("...and turned when the stub puts it above its pin", False, log)


def test_lead_turn(tmp):
    """--lead-turn sends the lead square off the row, not along it.

    A straight lead is the shorter one and lands the flag in the pin
    row. Where a neighbouring net has to cross that row to reach its own
    parts, the flag walls it off -- FB_5V on U4 is the case: a straight
    lead seals y=140 from the FB pin to the flag, and that is the only
    line COMP_5V can use to get from its pin down to C7 and R27. The
    square form leaves the row from the stub end itself, which is where
    a supply symbol belongs anyway.
    """
    dn = tmp / "d6.epro2"
    donor_drawn(dn)

    # GND and a signal one pitch apart, so the flag has to be carried out
    # of the row. The neighbour sits on the *down* side, so between the
    # two square directions only the upward one is clear -- which is what
    # makes the expectation a position and not a range.
    tg = tmp / "turn.epro2"
    target_boxed(tg, [(0, 0, -30, 0, "GND"), (0, 10, -30, 10, "SIG")],
                 half=5, group={0: 0, 1: 0})
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--strict-flags", "--flag-clearance", "15",
                  "--flag-lead", "40", "--lead-turn", "GND",
                  "-o", tmp / "turn-out.epro2")
    check("a net in --lead-turn is carried out square",
          rc == 0 and "carried 40 units out" in log, log)
    if rc == 0:
        f = placed_flags(tmp / "turn-out.epro2")
        check("...so the flag sits off the pin row, not along it",
              f and f[0][0] == -30 and f[0][1] == -40, f"{f}\n{log}")
        perp = [b for h, b in read(tmp / "turn-out.epro2")
                if h.get("type") == "LINE" and "startX" in b
                and b["startX"] == b["endX"] == -30
                and abs(b["endY"] - b["startY"]) == 40]
        check("...and the lead's first segment leaves the row square",
              perp, f"{len(perp)} segment(s)")

    # The control: the same fixture without the option lands the flag in
    # the row at the straight lead's tip, so the square form is the
    # option doing it and not some other change made at the same time.
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--strict-flags", "--flag-clearance", "15",
                  "--flag-lead", "40", "-o", tmp / "straight-out.epro2")
    if rc == 0:
        f = placed_flags(tmp / "straight-out.epro2")
        check("...whereas without it the same lead runs along the row",
              f and f[0][0] < 0 and f[0][1] == 0, f"{f}\n{log}")
    else:
        check("...whereas without it the same lead runs along the row",
              False, log)


def test_bus_junction(tmp):
    """A wire *meeting* the bus is the connection it exists for.

    The guard that refuses a bus crossed by another net's wire exempted a
    crossing at one of the run's own pins and not at its leads' free ends.
    On the supply sheet R26's ground stub is horizontal, so its free end
    lies *inside* the run's span; the hand-drawn ground wire lands there,
    the crossing guard called it a stranger, and the run was refused --
    three symbols kept where one was wanted. A wire arriving at a lead of
    the same net is what a bus is, not a fault.

    Both directions: the drawn wire must not refuse the run, and a wire
    crossing the same span anywhere else must still refuse it.
    """
    dn = tmp / "d9.epro2"
    donor_drawn(dn)

    # Three ground leads pointing left, so their free ends at -40, 60 and
    # 160 lie on the line their pins stand on. The middle one is inside
    # the span.
    tg = tmp / "junction.epro2"
    target(tg, [(0, 0, -40, 0, "GND"), (100, 0, 60, 0, "GND"),
                (200, 0, 160, 0, "GND")])
    append_line(tg, (60, -100), (60, 0))
    out = tmp / "junction-out.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "GND",
                  "--ground-bus", "220", "-o", out)
    check("a wire landing on a run member's free end does not refuse it",
          rc == 0 and "place    1 x GND" in log
          and "1 ground run(s)" in log, log)

    # The inverse, unchanged: the same span crossed away from any member
    # is still the short it was refused for before. Without this the guard
    # could be loosened to nothing and the probe above would still pass.
    tg2 = tmp / "stranger.epro2"
    target(tg2, [(0, 0, -40, 0, "GND"), (100, 0, 60, 0, "GND"),
                 (200, 0, 160, 0, "GND")])
    append_line(tg2, (130, -100), (130, 0))
    rc, log = run(tg2, "--donor", dn, "--nets", "GND",
                  "--ground-bus", "220", "-o", tmp / "stranger-out.epro2")
    check("...whereas one crossing the span elsewhere still refuses it",
          rc == 0 and "place    3 x GND" in log
          and "ground run(s)" not in log, log)


def test_wire_covers_node(tmp):
    """One symbol marks the node, even when a wire is what made it one.

    J4's ground was drawn down to the stage's own ground line and U4's
    across to the catch diode's, and in both cases the symbol already
    sitting on the far end of that wire was saying it. Placing a second
    one is two marks on one node.

    Which of the two goes is not "whichever sorts first": a point that
    needed a lead is the one to give up, because a lead is only drawn
    when the pin field was too crowded for the symbol, and the wire has
    taken that job over.
    """
    dn = tmp / "d10.epro2"
    donor_drawn(dn)

    # Two parts with one ground each; a second part's pin sits 15 units
    # from the first, so that one's flag is crowded and gets carried out
    # on a lead. The far ground is not crowded and keeps its stub end.
    stubs = [(0, 0, 0, -40, "GND"), (15, 0, 15, -40, "VCC"),
             (400, 0, 400, -40, "GND")]
    plain = tmp / "plain.epro2"
    target_boxed(plain, stubs, half=5)
    rc, log = run(plain, "--donor", dn, "--nets", "GND,VCC=+5V",
                  "--one-per-part", "--flag-clearance", "20",
                  "--flag-lead", "40", "-o", tmp / "plain-out.epro2")
    check("two grounds with no wire between them keep two symbols",
          rc == 0 and "place    2 x GND" in log, log)
    check("...and the crowded ones are carried out on leads",
          "crowded flag(s) carried" in log, log)

    # The same sheet with a wire joining the two free ends. Nothing else
    # changes, so the difference is the wire.
    joined = tmp / "joined.epro2"
    target_boxed(joined, stubs, half=5)
    append_line(joined, (0, -40), (400, -40))
    out = tmp / "joined-out.epro2"
    rc, log = run(joined, "--donor", dn, "--nets", "GND,VCC=+5V",
                  "--one-per-part", "--flag-clearance", "20",
                  "--flag-lead", "40", "-o", out)
    check("a wire joining them leaves one", rc == 0
          and "place    1 x GND" in log, log)
    # ...and it is the far one, the point that never needed a lead. With
    # the sort left as plain coordinate order the near one survives, so
    # this is what holds the preference to account.
    f = [q[:2] for q in placed_flags(out)]
    check("...and the one that survives is the one that needed no lead",
          (400, -40) in f and (0, -40) not in f, f)
    # The near pin is not reported bare: the wire reaches it, which is the
    # whole reason its symbol could go.
    check("...and the pin it left is not called bare",
          "left bare" not in log, log)


def test_symbol_turned_clear(tmp):
    """A flag whose symbol lands in a part is turned over, not dropped.

    U4's thermal pad: its stub runs out of the pin row and its free end
    is clear of the package, so every check that asks about the *root*
    passes -- and the ground triangle rooted there hangs its bars down
    into the IC. Turned over they sit above it, which is where a person
    draws them.

    The turn is asked even where there is no lead to reason from. The
    `inbody` set is built by asking whether a flag at a point would land
    in a part, and that question arrives with no previous point; answer
    it 0 and the pad reads as in-body, becomes a candidate for being
    carried out of the pin field, and the lead then runs into the SW_5V
    corridor and blocks that net's route.
    """
    dn = tmp / "d11.epro2"
    donor_drawn(dn)
    tg = tmp / "turnclear.epro2"
    # One part, a 20-unit body and a stub leaving its pin upwards, so the
    # free end is 20 units clear of the body -- and the flag's own box is
    # 30 deep, so at rotation 0 it reaches back into it.
    target_boxed(tg, [(0, 0, 0, -40, "GND")], half=20)
    out = tmp / "turnclear-out.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", out)
    check("a flag that would land in a part is still placed", rc == 0, log)
    if rc == 0:
        f = placed_flags(out)
        check("...and turned over so it lands clear of it",
              f and f[0][2] == 180, f)
        check("...with nothing drawn into the symbol for it",
              "CLASH" not in log, log)


def test_pin_stub(tmp):
    """--pin-stub re-aims a lead the importer aimed along the package.

    U4's thermal pad is a vertical pin on the top edge whose lead came out
    sideways, thirty units along the body. A symbol on that lead has
    nowhere to sit that is not to the side of the pin it belongs to, and
    the ground triangle rooted at its end draws its bars into the IC.

    The lead is *moved*, not dropped. The node's `NET` attribute lives on
    that wire and `epro.py diff` counts one of those per pin, so deleting
    the record would break the count where moving it breaks nothing --
    which is why the proof below still has to pass.
    """
    dn = tmp / "d12.epro2"
    # The plain donor: its flag draws nothing, so there is no box for the
    # fit test to trip over and the probe measures the geometry the option
    # is about rather than which way a 110-unit symbol happened to fall.
    donor(dn)
    tg = tmp / "pinstub.epro2"
    # A pin at the left with a body beside it and its lead running *along*
    # the body to the right, which is U4's pad exactly: the free end is
    # clear of the package and the lead still leaves the symbol nowhere
    # sensible to stand.
    target_boxed(tg, [(0, 0, 30, 0, "GND")], half=20)
    out = tmp / "pinstub-out.epro2"

    def segs(path):
        return [(b["startX"], b["startY"], b["endX"], b["endY"])
                for h, b in read(path) if h.get("type") == "LINE"]

    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", out)
    check("the lead runs along the package by default",
          rc == 0 and (0, 0, 30, 0) in segs(out), segs(out))

    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--pin-stub", "GND@0,0", "-o", out)
    check("--pin-stub is accepted", rc == 0 and "re-aimed" in log, log)
    if rc != 0:
        return
    after = segs(out)
    check("...so the lead no longer runs along the package",
          (0, 0, 30, 0) not in after, after)
    # ...and it is the same lead, moved: one end is still on the pin, the
    # other has gone somewhere else. Dropping the record instead would
    # take the node's `NET` attribute with it and break the one-per-pin
    # count `epro.py diff` reads, which is why moving it is the point.
    check("...and it is moved, not dropped",
          any((x0, y0) == (0, 0) or (x1, y1) == (0, 0)
              for x0, y0, x1, y1 in after), after)
    check("...and the lead is short: barely a stub",
          any(abs(x0 - x1) + abs(y0 - y1) <= 10
              for x0, y0, x1, y1 in after), [s for s in after])

    # A spec naming no pin is refused by name rather than silently doing
    # nothing, which is how a typo would read as success.
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--pin-stub", "GND@99,99", "-o", out)
    check("a --pin-stub that names no pin is refused",
          rc == 2 and "--pin-stub wants" in log, (rc, log))


def test_bare_pin(tmp):
    """A pin on a flagged net that no symbol reaches is a warning.

    `--one-per-part` puts one symbol on a part that has several pins on
    the net. That is right when the pins stand in a row -- ten ground
    symbols down one pin pitch is the black ladder the rule exists for --
    and wrong when nothing joins them, because the second pin then reads
    as a bare stub ending in the word GND while the same net is a symbol
    everywhere else on the sheet. U4's thermal pad is the case.

    A warning and not a failure, deliberately: the net is still connected,
    by name, the way everything on this sheet is. What it costs is a
    reader deciding whether that one pin means something else. So it must
    not set the exit status -- `build.sh` runs under `set -e` and would
    stop the chain over it.
    """
    dn = tmp / "d7.epro2"
    donor_drawn(dn)

    # Two ground pins on one part, 300 apart; a third on another part so
    # the run has something to place besides the bare one. The stubs
    # point *down*: the ground symbol's box runs from its root to 30
    # units above it, so a stub in the pin row puts the flag back on the
    # body and the clash rule -- not this one -- decides the outcome.
    tg = tmp / "bare.epro2"
    target_boxed(tg, [(0, 0, 0, -40, "GND"), (0, 300, 0, 260, "GND"),
                      (600, 0, 600, -40, "GND")],
                 half=5, group={0: 0, 1: 0, 2: 1})
    rc, log = run(tg, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "--one-per-node", "400", "-o", tmp / "bare-out.epro2")
    check("a bare pin on a flagged net is reported",
          rc == 0 and "left bare" in log, log)
    check("...naming the part and the coordinate",
          "R1 on GND is left bare at (0, 260)" in log, log)
    check("...and it warns rather than failing the build", rc == 0, rc)
    # ...and it says which of the two shapes it is. "Nothing on this part
    # marks the net" and "the part's other pin carries the symbol" want
    # opposite fixes, so a single sentence covering both would send half
    # its readers the wrong way.
    check("...naming the shape it is, not just the pin",
          "the part's other pin on this net carries a symbol" in log, log)

    # The inverse: a pin its node's symbol already covers. Without this the
    # rule could report every pin that has no symbol of its own, which is
    # most of a rail, and the probe above would still pass.
    tg2 = tmp / "nodep.epro2"
    target_boxed(tg2, [(0, 0, 0, -40, "VCC"), (0, 100, 0, 60, "VCC"),
                       (900, 0, 900, -40, "VCC")],
                 half=5, group={0: 0, 1: 0, 2: 1})
    rc, log = run(tg2, "--donor", dn, "--nets", "VCC=+5V", "--one-per-part",
                  "--one-per-node", "200", "-o", tmp / "nodep-out.epro2")
    check("a pin its node's own symbol reaches is not reported",
          rc == 0 and "no pin on a flagged net is left without" in log, log)

    # The revert probe, and the reason the node branch is load-bearing:
    # take the grouping away and the same fixture is one symbol for the
    # part again, so the far pin goes bare. It is not a second opinion on
    # the rule -- it is what proves the branch is what answered above.
    rc, log = run(tg2, "--donor", dn, "--nets", "VCC=+5V", "--one-per-part",
                  "-o", tmp / "nodep2-out.epro2")
    check("...whereas without the grouping the same pin goes bare",
          rc == 0 and "left bare" in log, log)

    # The inverse: one pin on the net is one symbol, so nothing is bare
    # however the flags are chosen. A part with several pins is what makes
    # the warning reachable at all, and this is the shape that shows it.
    tg3 = tmp / "one.epro2"
    target_boxed(tg3, [(0, 0, 0, -40, "GND"), (600, 0, 600, -40, "GND")],
                 half=5)
    rc, log = run(tg3, "--donor", dn, "--nets", "GND", "--one-per-part",
                  "-o", tmp / "one-out.epro2")
    check("a part with one pin on the net is never bare",
          rc == 0 and "left bare" not in log, log)


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp())
    dn, tg, out = tmp / "donor.epro2", tmp / "t.epro2", tmp / "o.epro2"
    donor(dn)
    target(tg, [(0, 0, -40, 0, "GND"), (200, 0, 160, 0, "VCC"),
                (400, 0, 360, 0, "GND")])

    rc, log = run(tg, "--donor", dn, "-o", out)
    check("it succeeds", rc == 0, log)
    check("the donor's flags are found", "offers: GND" in log, log)

    if out.exists():
        recs = read(out)
        flags = [(h, b) for h, b in recs
                 if h.get("type") == "COMPONENT" and b.get("partId") == FLAG_PART]
        at = {(b["x"], b["y"]) for _, b in flags}
        check("one flag per GND stub, at the free end", at == {(-40, 0), (360, 0)},
              str(sorted(at)))
        check("no flag on the component pin itself",
              (0, 0) not in at and (400, 0) not in at, str(sorted(at)))
        check("no flag on the VCC stub", (160, 0) not in at, str(sorted(at)))
        check("the SYMBOL document came across",
              any(h.get("type") == "DOCHEAD" and b.get("uuid") == "symG"
                  for h, b in recs))
        check("flags carry no Designator, so they stay out of the BOM",
              not any(b.get("key") == "Designator"
                      and b.get("parentId") in {h["id"] for h, _ in flags}
                      for h, b in recs))
        check("flags carry no NET attribute, so the net diff is unmoved",
              not any(b.get("key") == "NET"
                      and b.get("parentId") in {h["id"] for h, _ in flags}
                      for h, b in recs))

        o2 = tmp / "o2.epro2"
        run(tg, "--donor", dn, "-o", o2)
        import hashlib
        h1 = hashlib.sha256(zipfile.ZipFile(out).read("doc.epru")).hexdigest()
        h2 = hashlib.sha256(zipfile.ZipFile(o2).read("doc.epru")).hexdigest()
        check("two runs are byte-identical", h1 == h2)

    # borrowing another flag's symbol under a different name
    o3 = tmp / "o3.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "VCC=GND", "-o", o3)
    check("a net can borrow another flag's symbol", rc == 0 and "1 x VCC" in log, log)

    # --- the name has to be readable, except where the symbol says it ----
    # Hiding `Global Net Name` along with the other attributes put 101
    # identical bare supply bars on the sheet -- BUS_3V3, VBUS and
    # CH1_36V indistinguishable -- while 233 net-name texts were hidden
    # in the same pass. 116 rail pins went from labelled to
    # unidentifiable, which is worse than the text it replaced.
    #
    # GND is the one exception and it has to stay one: an unlabelled
    # ground triangle means GND by convention, so its 90 texts carry
    # nothing. LED_RTN uses the *same* symbol and is a different net, so
    # the rule keys on the net name and not on the symbol -- which is the
    # probe below. Getting that wrong merges LED_RTN onto GND and makes
    # the current sense read zero.
    def flagvis(path, netname):
        rows = read(path)
        ids = {h["id"] for h, b in rows
               if h.get("type") == "ATTR"
               and b.get("key") == "Global Net Name"
               and b.get("value") == netname}
        parents = {b["parentId"] for h, b in rows
                   if h.get("type") == "ATTR"
                   and b.get("key") == "Global Net Name"
                   and b.get("value") == netname}
        return {b.get("key"): b.get("valueVisible") for h, b in rows
                if h.get("type") == "ATTR" and b.get("parentId") in parents}

    if out.exists():
        vis = flagvis(out, "GND")
        check("a GND flag carries no text, because the symbol says it",
              vis.get("Global Net Name") is False, str(vis))
        check("...and the bookkeeping attributes are not visible either",
              vis.get("Symbol") is False and vis.get("Device") is False,
              str(vis))

    # every other net keeps its name, including one on the ground symbol
    o5 = tmp / "o5.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "GND,VCC=GND", "-o", o5)
    if o5.exists():
        v2 = flagvis(o5, "VCC")
        check("...while a net borrowing that symbol keeps its name",
              v2.get("Global Net Name") is True, str(v2))
        check("...and GND on the same sheet still has none",
              flagvis(o5, "GND").get("Global Net Name") is False)

    # --- the probe that matters -------------------------------------------
    # A borrowed flag must rename itself. Without an instance-level
    # `Global Net Name` the device's wins, so every borrowed flag would
    # name the donor's net -- six supply rails merged into one, and
    # LED_RTN onto GND, which design.yaml spends its longest comment
    # forbidding. The net diff cannot see it: a flag carries no NET
    # attribute, so 441 pin connections stay 441 and the report is clean.
    o4 = tmp / "o4.epro2"
    rc, log = run(tg, "--donor", dn, "--nets", "VCC=GND", "-o", o4)
    if o4.exists():
        gnn = [b.get("value") for h, b in read(o4)
               if h.get("type") == "ATTR" and b.get("key") == "Global Net Name"]
        check("a borrowed flag renames itself to its own net",
              gnn and all(v == "VCC" for v in gnn), str(gnn))
    check("...and the run reports it checked", "names the net it sits on" in log,
          log)

    # a net the donor cannot supply must be refused
    rc, log = run(tg, "--donor", dn, "--nets", "VCC", "--dry-run")
    check("an unknown flag name is refused",
          rc != 0 and "no flag for VCC" in log, log)

    # a donor with no flags at all
    bare = tmp / "bare.epro2"
    target(bare, [(0, 0, -40, 0, "GND")])
    rc, log = run(tg, "--donor", bare, "--dry-run")
    check("a donor with no flags is refused",
          rc != 0 and "places no flag" in log, log)

    # --- where the flag's bar ends ---------------------------------------
    # The flag connects at its pin's root; the bar is the drawn far end.
    # Whether EasyEDA joins along a pin graphic is not decidable from the
    # file, so a bar ending on a foreign stub's endpoint is reported
    # rather than passed over. The donor pin is length 10 at rotation
    # 270, and the flag goes on the stub's *outer* end -- so a GND stub
    # running (0, 0) -> (-40, 0) puts the flag at (-40, 0) and its bar
    # end at (-40, -10).
    tg2, out2 = tmp / "tip.epro2", tmp / "tip-out.epro2"
    target(tg2, [(0, 0, -40, 0, "GND"), (-40, -10, 0, -10, "VCC")])
    rc, log = run(tg2, "--donor", dn, "--nets", "GND", "-o", out2)
    check("a flag bar ending on a foreign stub is reported",
          rc == 0 and "flag bar(s) end on a foreign stub" in log, log)
    check("...and it names the net it lands on",
          "VCC" in log and "TIP" in log, log)

    # the control: move that stub out of the bar's way
    tg3, out3 = tmp / "notip.epro2", tmp / "notip-out.epro2"
    target(tg3, [(0, 0, -40, 0, "GND"), (0, -200, 40, -200, "VCC")])
    rc, log = run(tg3, "--donor", dn, "--nets", "GND", "-o", out3)
    check("...and a bar that ends clear of one is not",
          rc == 0 and "no flag bar ends on a foreign stub" in log, log)

    # --- one flag per part, not per pin ----------------------------------
    # U9 has ten GND pins and got ten ground symbols stacked on its pin
    # pitch, bars overlapping into one black ladder. The pins that lose
    # their flag keep their net label, so nothing becomes nameless.
    def one_part_three_pins(path):
        """One component, three pins, all on GND -- which `target` cannot
        express because it gives every stub its own component."""
        r, tk = [], 0

        def add(h, b):
            nonlocal tk
            tk += 1
            h["ticket"] = tk
            r.append((h, b))

        add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
        add({"type": "COMPONENT", "id": "u1"},
            {"partId": "U.1", "x": 0, "y": 0, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": "du1"},
            {"key": "Designator", "value": "U1", "parentId": "u1"})
        add({"type": "ATTR", "id": "su1"},
            {"key": "Symbol", "value": "symU", "parentId": "u1"})
        for i, dy in enumerate((0, 40, 80)):
            add({"type": "WIRE", "id": f"w{i}"}, {"groupId": "", "zIndex": 1})
            add({"type": "LINE", "id": f"l{i}"},
                {"lineGroup": f"w{i}", "startX": 0, "startY": dy,
                 "endX": -40, "endY": dy})
            add({"type": "ATTR", "id": f"n{i}"},
                {"key": "NET", "value": "GND", "parentId": f"w{i}"})
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "symU"})
        for i, dy in enumerate((0, 40, 80)):
            add({"type": "PIN", "id": f"p{i}"},
                {"partId": "U.1", "x": 0, "y": dy, "length": 10,
                 "rotation": 0})
        write(path, r)

    tg4, o4a, o4b = tmp / "many.epro2", tmp / "a.epro2", tmp / "b.epro2"
    one_part_three_pins(tg4)
    rc, log_all = run(tg4, "--donor", dn, "--nets", "GND", "-o", o4a)
    check("without the flag, every pin gets one",
          rc == 0 and "place    3 x GND" in log_all, log_all)
    rc, log_one = run(tg4, "--donor", dn, "--nets", "GND",
                      "--one-per-part", "-o", o4b)
    check("...and with it, the part gets one",
          rc == 0 and "place    1 x GND" in log_one, log_one)
    check("...and both files are written",
          o4a.exists() and o4b.exists())

    test_flag_clash(tmp)
    test_flag_lead(tmp)
    test_flag_turn(tmp)
    test_lead_turn(tmp)
    test_one_per_node(tmp)
    test_ground_bus(tmp)
    test_bus_junction(tmp)
    test_wire_covers_node(tmp)
    test_symbol_turned_clear(tmp)
    test_pin_stub(tmp)
    test_bare_pin(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all netflags tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
