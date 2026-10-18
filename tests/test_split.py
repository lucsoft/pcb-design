#!/usr/bin/env python3
"""Regression tests for tools/split.py.

The failure this tool has to be proof against is silent and symmetrical:
a sheet map that leaves a block out drops those components off the board,
and one that names a block twice places them twice. Neither shows up
anywhere downstream, because each sheet is checked on its own and each
one looks complete. So the partition is checked here, in both directions,
and the component and pin counts are compared against the input rather
than assumed.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "split.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


PARTS = [
    ("U1", "C1", {"1": "VCC", "2": "GND", "3": "SIG"}),
    ("C1", "C2", {"1": "VCC", "2": "GND"}),
    ("R1", "C3", {"1": "SIG", "2": "GND"}),
    ("U2", "C4", {"1": "VCC", "2": "GND", "3": "OUT"}),
    ("R2", "C5", {"1": "OUT", "2": "GND"}),
]
SECTIONS = {"U1": "Front", "C1": "Front", "R1": "Front",
            "U2": "Back", "R2": "Back"}


def build(d: Path, sheets: str | None):
    d.mkdir(parents=True, exist_ok=True)
    nl = {f"gge{i + 1}": {"props": {"Designator": des, "Supplier Part": lcsc},
                          "pins": pins}
          for i, (des, lcsc, pins) in enumerate(PARTS)}
    (d / "netlist.json").write_text(json.dumps(nl, indent=2), encoding="utf-8")

    gen = []
    for sec in ("Front", "Back"):
        gen.append(f"    # ---- {sec} " + "-" * 40)
        for des, lcsc, pins in PARTS:
            if SECTIONS[des] == sec:
                gen.append(f'        ("{des}", "{lcsc}", "x", "y", '
                           + json.dumps(pins) + "),")
    (d / "netlist.py").write_text("\n".join(gen) + "\n", encoding="utf-8")
    (d / "design.yaml").write_text(
        "rails:\n  GND: ground\n  VCC: 3.3\n" + (sheets or ""),
        encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp())

    # --- the good case ----------------------------------------------------
    d = tmp / "ok"
    build(d, "sheets:\n  A: [Front]\n  B: [Back]\n")
    out = d / "sheets"
    rc, log = run(d, "-o", out)
    check("a partition is accepted", rc == 0, log)
    check("...and writes a directory per sheet",
          (out / "a" / "netlist.json").exists()
          and (out / "b" / "netlist.json").exists())
    check("...each with its own design.yaml",
          (out / "a" / "design.yaml").exists()
          and (out / "b" / "design.yaml").exists())
    if (out / "a" / "design.yaml").exists():
        ya = (out / "a" / "design.yaml").read_text()
        yb = (out / "b" / "design.yaml").read_text()
        # Carrying every block in a shared file makes each sheet demand
        # geometry for the whole board, and arrange.py then refuses --
        # so a sheet's design.yaml must name only its own blocks.
        check("...naming only that sheet's blocks",
              "Front" in ya and "Back" not in ya
              and "Back" in yb and "Front" not in yb, ya + "---" + yb)
        check("...and carrying the rails across",
              "rails:" in ya and "GND" in ya, ya)
    check("...and reports every component placed once",
          "5 of 5 components and 12 of 12 pin connections" in log, log)

    if (out / "a" / "netlist.json").exists():
        a = json.loads((out / "a" / "netlist.json").read_text())
        b = json.loads((out / "b" / "netlist.json").read_text())
        check("...with gge keys renumbered from 1 on each sheet",
              list(a) == ["gge1", "gge2", "gge3"]
              and list(b) == ["gge1", "gge2"], f"{list(a)} {list(b)}")
        both = {c["props"]["Designator"]
                for s in (a, b) for c in s.values()}
        check("...and the union is the whole netlist",
              both == {p[0] for p in PARTS}, str(sorted(both)))

    # GND and VCC touch both sheets; SIG and OUT do not
    check("...and the crossing nets are named",
          "GND" in log and "VCC" in log, log)
    check("...while a net inside one sheet is not called crossing",
          "2 net(s) cross" in log, log)

    # --- break probe: a block on no sheet ---------------------------------
    # This is the one that matters. Dropping a block loses components off
    # the board, and every per-sheet check still passes, because the sheet
    # that remains is complete in itself.
    d = tmp / "missing"
    build(d, "sheets:\n  A: [Front]\n")
    rc, log = run(d, "-o", d / "sheets")
    check("a block on no sheet is refused", rc == 1, log)
    check("...and names it", "'Back' is on no sheet" in log, log)
    check("...and writes nothing", not (d / "sheets").exists())

    # --- break probe: a block on two sheets -------------------------------
    d = tmp / "twice"
    build(d, "sheets:\n  A: [Front, Back]\n  B: [Back]\n")
    rc, log = run(d, "-o", d / "sheets")
    check("a block on two sheets is refused", rc == 1, log)
    check("...and names both", "'A'" in log and "'B'" in log, log)
    check("...and writes nothing", not (d / "sheets").exists())

    # --- break probe: a sheet naming a block that does not exist ----------
    # A typo here is the quiet one: the block it meant to name is then
    # also on no sheet, but saying only that sends the reader hunting for
    # a block they did write.
    d = tmp / "typo"
    build(d, "sheets:\n  A: [Front]\n  B: [Bakc]\n")
    rc, log = run(d, "-o", d / "sheets")
    check("a misspelt block name is refused", rc == 1, log)
    check("...and reported as unknown, not merely as missing",
          "does not exist" in log, log)

    # --- no map at all ----------------------------------------------------
    d = tmp / "nomap"
    build(d, None)
    rc, log = run(d, "--dry-run")
    check("no sheets mapping is refused", rc == 1, log)
    check("...and the available block names are listed",
          "Front" in log and "Back" in log, log)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all split tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
