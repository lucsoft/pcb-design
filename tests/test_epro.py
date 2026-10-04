#!/usr/bin/env python3
"""Regression tests for tools/epro.py — the .epro2 reader and the import diff.

Two probes per behaviour, because one is not enough here:

- a **break probe** removes or merges a net and requires the diff to report it.
  That exercises the comparison.
- an **inverse probe** feeds a net representation the reader has never seen and
  requires it to be found. That exercises the *capture*, which the break probe
  cannot reach: a net the reader never parses is invisible to a comparison, and
  the diff comes back clean because both sides are empty.

The second kind is not hypothetical. `cmd_nets` read only `netName`, which PCB
copper carries and a schematic does not, so it reported "0 nets" on a schematic
holding 108 of them — a checker failing open, and silently.
"""

import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPRO = ROOT / "tools" / "epro.py"

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def make_epro2(path, records):
    """Write a minimal .epro2 holding one .epru with the given (header, payload)."""
    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in records]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def schematic(pin_nets):
    """A schematic-shaped document: one WIRE per pin, named by an ATTR."""
    recs = []
    for i, net in enumerate(pin_nets):
        wid = f"w{i}"
        recs.append(({"type": "WIRE", "ticket": i * 2, "id": wid},
                     {"groupId": "", "locked": False, "zIndex": i}))
        recs.append(({"type": "ATTR", "ticket": i * 2 + 1, "id": f"a{i}"},
                     {"key": "NET", "value": net, "parentId": wid,
                      "x": 0, "y": 0}))
    return recs


def pcb(pin_nets):
    """A PCB-shaped document: the net rides on the copper object itself."""
    return [({"type": "WIRE", "ticket": i, "id": f"w{i}"},
             {"netName": net, "groupId": 0}) for i, net in enumerate(pin_nets)]


def write_netlist(path, pin_nets):
    out, n = {}, 0
    for i, net in enumerate(pin_nets):
        n += 1
        out[f"gge{n}"] = {"props": {"Designator": f"R{n}",
                                    "Supplier Part": "C1"},
                          "pins": {"1": net}}
    path.write_text(json.dumps(out), encoding="utf-8")


def run(*args):
    p = subprocess.run([sys.executable, str(EPRO), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp())
    nets = ["GND", "GND", "VCC", "SDA"]

    # --- capture: both representations must be read -----------------------
    sch = tmp / "sch.epro2"
    make_epro2(sch, schematic(nets))
    rc, out = run("nets", sch)
    check("schematic ATTR key=NET is read", rc == 0 and "GND" in out and "2 refs" in out, out)

    brd = tmp / "pcb.epro2"
    make_epro2(brd, pcb(nets))
    rc, out = run("nets", brd)
    check("PCB netName is still read", rc == 0 and "GND" in out, out)

    # inverse probe: a document with no net in either shape must not read as
    # "no nets here", it must say the reader found nothing and fail.
    empty = tmp / "empty.epro2"
    make_epro2(empty, [({"type": "FILL", "ticket": 1, "id": "e1"}, {"x": 0})])
    rc, out = run("nets", empty)
    check("a net-less document exits non-zero", rc != 0, f"rc={rc}")
    check("...and names the shapes it looked for",
          "ATTR with key=NET" in out and "netName" in out, out)

    # --- the diff ---------------------------------------------------------
    nl = tmp / "netlist.json"
    write_netlist(nl, nets)

    rc, out = run("diff", sch, nl)
    check("matching export diffs clean", rc == 0 and "identical" in out, out)

    # break probe 1: a net port lost on one pin -> the count drops
    lost = tmp / "lost.epro2"
    make_epro2(lost, schematic(["GND", "VCC", "SDA"]))
    rc, out = run("diff", lost, nl)
    check("a dropped pin is reported", rc == 1 and "COUNT" in out and "GND" in out, out)

    # break probe 2: two nets merged into one -> a name disappears
    merged = tmp / "merged.epro2"
    make_epro2(merged, schematic(["GND", "GND", "GND", "SDA"]))
    rc, out = run("diff", merged, nl)
    check("a merged net is reported", rc == 1 and "MISSING" in out and "VCC" in out, out)

    # break probe 3: a net in the export that the netlist never had
    stray = tmp / "stray.epro2"
    make_epro2(stray, schematic(nets + ["MYSTERY"]))
    rc, out = run("diff", stray, nl)
    check("an unexpected net is reported", rc == 1 and "EXTRA" in out and "MYSTERY" in out, out)

    # fail-closed: diffing against a document with no nets must NOT pass
    rc, out = run("diff", empty, nl)
    check("diff against a net-less export refuses to pass", rc == 2, f"rc={rc} {out}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all epro tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
