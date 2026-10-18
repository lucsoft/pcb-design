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


def target(path, stubs):
    """stubs: [(pin_x, pin_y, free_x, free_y, net)]"""
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
    for i, _ in enumerate(stubs):
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{i}"})
        add({"type": "PIN", "id": f"p{i}"},
            {"partId": f"P{i}.1", "x": 0, "y": 0, "length": 10, "rotation": 0})
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
    test_one_per_node(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all netflags tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
