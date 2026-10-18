#!/usr/bin/env python3
"""Regression tests for tools/handplace.py.

The tool translates whole clusters by hand-written coordinates, and that
argument is `arrange.py`'s: rotate about the anchor, translate rigidly, so
every coincidence inside the cluster survives. What is new here is the
`wire` line -- a wire a person drew -- and a wire is nothing *but*
coincidences. Both of its ends have to land on the net it names, and the
route has to stay off every other net's pins, because a wire crossing one
of those is a connection the net diff cannot see.

The four answers are kept apart on purpose: a point on the net, a point on
a different net, a point on nothing at all, and a wire that crosses a
stranger on its way. Collapsing the third into the second is how a typo
reads as a pass.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "handplace.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def build(path, comps):
    """(designator, x, y, [(net, pin_x, pin_y, free_x, free_y)]).

    Each stub is a WIRE with a LINE and a NET attribute naming it, which
    is what the importer emits and what `net_points` reads. The symbol
    carries a PIN so the cluster can be attributed.
    """
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for d, x, y, stubs in comps:
        cid = f"c{d}"
        add({"type": "COMPONENT", "id": cid},
            {"partId": f"SYM_{d}.1", "x": x, "y": y, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"a{d}"},
            {"key": "Designator", "value": d, "parentId": cid})
        add({"type": "ATTR", "id": f"s{d}"},
            {"key": "Symbol", "value": f"sym{d}", "parentId": cid})
        for i, (net, px, py, fx, fy) in enumerate(stubs):
            wid = f"w{d}_{i}"
            add({"type": "WIRE", "id": wid}, {"groupId": "", "zIndex": 1})
            add({"type": "LINE", "id": f"l{d}_{i}"},
                {"lineGroup": wid, "startX": px, "startY": py,
                 "endX": fx, "endY": fy, "strokeWidth": 1})
            add({"type": "ATTR", "id": f"n{d}_{i}"},
                {"key": "NET", "value": net, "parentId": wid,
                 "x": (px + fx) // 2, "y": (py + fy) // 2})
    for d, _, _, _ in comps:
        add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": f"sym{d}"})
        add({"type": "PIN", "id": f"p{d}"},
            {"partId": f"SYM_{d}.1", "x": 0, "y": 0, "length": 10,
             "rotation": 0})
    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


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


# Two parts, a ground net and a signal on each, far enough apart that a
# route between the grounds has to be drawn rather than being a stub. No
# two stub ends share a coordinate: an earlier fixture put a ground pin
# and a signal's free end on the same point, and the crossing probe then
# passed for a reason that had nothing to do with the rule.
COMPS = [
    ("R1", 0, 0, [("GND", 0, 0, 40, 0), ("SIG", 0, 60, 40, 60)]),
    ("R2", 0, 200, [("GND", 0, 200, 40, 200), ("SIG", 0, 260, 40, 260)]),
]
GOOD = "wire GND 0,0 -60,0 -60,200 40,200"
CROSSES = "wire GND 0,0 -60,0 -60,260 40,260 40,200"


def sheet(tmp, name="s.epro2"):
    p = tmp / name
    build(p, COMPS)
    return p


def place(tmp, body, name="p.txt"):
    p = tmp / name
    p.write_text("# hand placement\nR1 0 0 0\nR2 0 200 0\n" + body + "\n",
                 encoding="utf-8")
    return p


def test_wire_drawn(tmp):
    """A wire the file draws lands on the canvas, without a net of its own."""
    src = sheet(tmp)
    out = tmp / "o.epro2"
    # From R1's ground pin round to R2's ground stub end, away from both
    # signals' pins and free ends.
    rc, log = run(src, place(tmp, GOOD), "-o", out)
    check("a wire that lands on its own net is accepted", rc == 0, log)
    check("...and the run says so", "wire   GND from 0,0 40,200" in log, log)
    if rc != 0:
        return
    recs = read(out)
    lines = [b for h, b in recs if h.get("type") == "LINE"
             and b.get("lineGroup", "").startswith("hw")]
    check("...three segments for four points", len(lines) == 3, len(lines))
    ends = {(b["startX"], b["startY"]) for b in lines}
    check("...starting where the file said", (0, 0) in ends, ends)
    # No NET attribute of its own: `epro.py diff` counts one key=NET per
    # pin, so a wire carrying one lifts the export's count above the
    # netlist's and reads as a connection that is not there.
    wid = lines[0]["lineGroup"] if lines else None
    check("...and no NET attribute to lift the diff's count",
          wid is not None and not any(
              h.get("type") == "ATTR" and b.get("key") == "NET"
              and b.get("parentId") == wid for h, b in recs), wid)
    orig = read(src)
    kept = [r for r in recs
            if not str(r[0].get("id", "")).startswith("hw")]
    check("...with every original record left in place, in order",
          [(h.get("id"), h.get("type")) for h, _ in kept]
          == [(h.get("id"), h.get("type")) for h, _ in orig],
          "record sequence differs")
    # The wire's own records land inside the schematic document, not after
    # the last SYMBOL one, where a COMPONENT or WIRE would belong to the
    # wrong document.
    at = [i for i, (h, _) in enumerate(recs)
          if str(h.get("id", "")).startswith("hw")]
    check("...and inside the schematic document, not past its end",
          at and max(at) < max(i for i, (h, _) in enumerate(recs)
                               if h.get("type") == "DOCHEAD"),
          at[:3])

    # The inverse: no wire line, no wire drawn. Without this the probe
    # above would pass on a tool that always emits four records.
    out2 = tmp / "o2.epro2"
    rc, log = run(src, place(tmp, ""), "-o", out2)
    check("...and without the line nothing is drawn",
          rc == 0 and len(read(out2)) == len(orig), log)


def test_wire_refused(tmp):
    """Every way the wire can be wrong, and each one says which it is."""
    src = sheet(tmp)
    out = tmp / "bad.epro2"

    def attempt(body):
        out.unlink(missing_ok=True)
        return run(src, place(tmp, body), "-o", out)

    # An end on a different net is a short.
    rc, log = run(src, place(tmp, "wire GND 0,0 40,60"), "-o", out)
    check("an end on another net is refused",
          rc == 1 and "which is SIG" in log, log)
    check("...and the output is deleted", not out.exists())

    # An end on nothing at all is the shape a typo makes, and it must not
    # read as "that point is fine".
    rc, log = run(src, place(tmp, "wire GND 0,0 0,77"), "-o", out)
    check("an end on no net is refused, not passed",
          rc == 1 and "which is no net at all" in log, log)

    # ...and both ends are checked, not just the first.
    rc, log = run(src, place(tmp, "wire GND 0,20 0,77"), "-o", out)
    check("the far end is checked too",
          rc == 1 and "ends at (0, 77)" in log, log)

    # A route that crosses a stranger's pin connects to it in a geometric
    # schematic, and the net diff reads names, so it cannot see this. Both
    # ends here land on GND and the crossing is in the middle.
    rc, log = run(src, place(tmp, CROSSES), "-o", out)
    check("a route across a foreign pin is refused",
          rc == 1 and "which is on SIG" in log, log)
    check("...and that output is deleted too", not out.exists())
    # ...naming the place, not just refusing: "invalid wire" leaves the
    # reader to find the crossing by eye.
    check("...naming the crossing, not just refusing",
          "crosses (0, 260)" in log, log)

    # ...and a crossing of another net's *wire* counts, not only of its
    # pins. The pad's ground wire on the supply sheet ran straight across
    # the bootstrap's SW_5V line, and the first version of this check --
    # which tested points and nothing else -- said the wire was fine.
    # (20, 60) is the middle of a signal stub, not either of its ends.
    rc, log = run(src, place(tmp, "wire GND 0,0 20,0 20,200 40,200"),
                  "-o", out)
    check("a route across a foreign net's wire is refused",
          rc == 1 and "runs across" in log and "SIG" in log, log)
    check("...and that output is deleted too", not out.exists())

    # The malformed line: one point cannot be a route, and the parser says
    # so by line number rather than drawing a zero-length wire.
    rc, log = run(src, place(tmp, "wire GND 0,0"), "-o", out)
    check("a wire with one point is refused at the parser",
          rc != 0 and "at least two integer points" in log, log)
    rc, log = run(src, place(tmp, "wire GND 0,zero"), "-o", out)
    check("...and so is a point that is not a pair of integers",
          rc != 0 and "at least two integer points" in log, log)


def test_stub_length(tmp):
    """`stub DESIGNATOR LENGTH` cuts that part's leads.

    A part lying *across* a row is as wide as its two leads put together,
    and nothing can be placed within that -- which is what stops a compact
    row being built. It is stated by designator rather than by coordinate
    on purpose: the leads are short because of what the part is, not
    because of where it happens to be, so moving it does not invalidate
    the line.
    """
    src = sheet(tmp)
    out = tmp / "cut.epro2"
    rc, log = run(src, place(tmp, ""), "-o", out)
    check("leads are the importer's thirty by default",
          rc == 0 and (0, 0, 40, 0) in [(b["startX"], b["startY"],
                                         b["endX"], b["endY"])
                                        for h, b in read(out)
                                        if h.get("type") == "LINE"], log)

    rc, log = run(src, place(tmp, "stub R1 10"), "-o", out)
    check("a stub line cuts that designator's leads", rc == 0
          and "cut to the length" in log, log)
    if rc == 0:
        lines = [(b["startX"], b["startY"], b["endX"], b["endY"])
                 for h, b in read(out) if h.get("type") == "LINE"]
        check("...to the length it states: still on the pin, ten units out",
              (0, 0, 10, 0) in lines or (10, 0, 0, 0) in lines, lines)
        check("...and the other part's lead is untouched",
              any(abs(x0 - x1) == 40 for x0, y0, x1, y1 in lines), lines)

    # ...and the name on the lead moves with it. The importer puts the
    # label at the lead's midpoint, and the overlap check measures every
    # coordinate a component owns -- text included -- so a cut lead with
    # an unmoved label keeps the footprint at its old width.
    if rc == 0:
        lab = {b["parentId"]: b.get("x") for h, b in read(out)
               if h.get("type") == "ATTR" and b.get("key") == "NET"
               and b.get("value") == "GND"}
        check("...and the name on it moves to the cut lead's midpoint",
              lab.get("wR1_0") == 5, lab)
        check("...while the lead that was not cut keeps its own",
              lab.get("wR2_0") == 20, lab)

    # A designator the sheet does not carry is refused by name, not
    # silently ignored.
    out.unlink(missing_ok=True)
    rc, log = run(src, place(tmp, "stub NOPE 10"), "-o", out)
    check("a stub line naming no part is reported",
          rc == 1 and "NOPE" in log, (rc, log))
    check("...and nothing is written", not out.exists())


def main():
    tmp = Path(tempfile.mkdtemp())
    test_wire_drawn(tmp)
    test_wire_refused(tmp)
    test_stub_length(tmp)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all handplace tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
