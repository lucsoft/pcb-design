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


def read(path):
    z = zipfile.ZipFile(path)
    out = []
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        out.append((json.loads(h), json.loads(b.rstrip("|"))))
    return out


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

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all netflags tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
