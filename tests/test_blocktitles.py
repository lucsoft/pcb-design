#!/usr/bin/env python3
"""Regression tests for tools/blocktitles.py.

The tool's one safety claim is that it only appends: twelve attributes
in, every existing record out unchanged and in order. That is checked
here and in the tool, because a title is not worth a changed record --
and the check in the tool is the one that deletes the output, so it has
to be exercised by something other than itself.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "blocktitles.py"
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


PARTS = [("U1", "Front"), ("C1", "Front"), ("U2", "Back")]


def build(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    nl = {f"gge{i + 1}": {"props": {"Designator": des, "Supplier Part": "C9"},
                          "pins": {"1": "GND"}}
          for i, (des, _) in enumerate(PARTS)}
    (d / "netlist.json").write_text(json.dumps(nl), encoding="utf-8")
    gen = []
    for sec in ("Front", "Back"):
        gen.append(f"    # ---- {sec} " + "-" * 30)
        for des, s in PARTS:
            if s == sec:
                gen.append(f'        ("{des}", "C9", "x", "y", {{}}),')
    (d / "netlist.py").write_text("\n".join(gen) + "\n", encoding="utf-8")

    rows, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        rows.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
    for i, (des, _) in enumerate(PARTS):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": "P.1", "x": i * 200, "y": 0, "rotation": 0,
             "isMirror": False})
        add({"type": "ATTR", "id": f"d{i}"},
            {"parentId": f"c{i}", "key": "Designator", "value": des})
        add({"type": "ATTR", "id": f"s{i}"},
            {"parentId": f"c{i}", "key": "Symbol", "value": "sym"})
    add({"type": "DOCHEAD"}, {"docType": "SYMBOL", "uuid": "sym"})
    add({"type": "RECT", "id": "r"},
        {"dotX1": -20, "dotY1": -20, "dotX2": 20, "dotY2": 20})

    body = "\n".join(
        json.dumps(h, separators=(",", ":")) + "||"
        + json.dumps(b, separators=(",", ":")) + "|" for h, b in rows)
    with zipfile.ZipFile(d / "in.epro2", "w") as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", body)
    return d / "in.epro2", len(rows)


def read(path):
    z = zipfile.ZipFile(path)
    e = [n for n in z.namelist() if n.endswith(".epru")][0]
    return [l for l in z.read(e).decode("utf-8").split("\n") if l]


def main():
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "a"
    src, n_in = build(d)
    out = d / "out.epro2"
    rc, log = run(src, d / "netlist.json", "-o", out)
    check("a title is placed for every block", rc == 0 and "2 block" in log, log)
    check("...and it names them", "Front" in log and "Back" in log, log)
    check("...and says which part each is anchored on",
          "anchored on U1" in log or "anchored on C1" in log, log)

    if out.exists():
        before, after = read(src), read(out)
        check("...adding exactly one record per block",
              len(after) == len(before) + 2, f"{len(before)} -> {len(after)}")
        # every original line survives, in order
        titles = [l for l in after if '"key":"Block"' in l]
        rest = [l for l in after if '"key":"Block"' not in l]
        check("...and leaving every existing record untouched",
              rest == before)
        check("...with the block name as the visible value",
              len(titles) == 2
              and all('"valueVisible":true' in l for l in titles),
              str(titles[:1]))
        check("...and coordinates, which an unpositioned attribute lacks",
              all('"x":' in l and '"y":' in l for l in titles))

    # --- the title block --------------------------------------------------
    # EN ISO 7200 wants a drawing to say what it is. The border it
    # normally sits in is not drawable here -- every LINE is a wire --
    # but the text is, and the text is the part the standard asks for.
    d3 = tmp / "c"
    src3, _ = build(d3)
    o3 = d3 / "tb.epro2"
    rc, log = run(src3, d3 / "netlist.json", "--sheet-title", "Demo",
                  "--sheet", "1 of 2", "--revision", "B",
                  "--date", "2026-01-01", "-o", o3)
    check("a title block is written when asked for",
          rc == 0 and "title block" in log, log)
    # The paper size is derived, never taken: quoting the bare extent is
    # how two of four sheets came to say A3 when they needed A2, and the
    # design record warns about exactly that. It is a floor, because the
    # flag names have no coordinates and EasyEDA places them.
    check("...and the paper size is derived from the drawing extent",
          "of drawing, so at least" in log, log)
    check("...and said to be a floor, not a measurement",
          "a floor" in log, log)
    if o3.exists():
        lines = read(o3)
        tb = [l for l in lines if '"key":"Sheet ' in l]
        check("...as right-aligned, positioned strings",
              len(tb) >= 4 and all('"align":"right"' in l for l in tb),
              str(len(tb)))
        check("...carrying what the standard asks for",
              any("Demo" in l for l in tb) and any("1 of 2" in l for l in tb)
              and any("B" in l for l in tb))

    rc, log = run(src3, d3 / "netlist.json", "-o", d3 / "none.epro2")
    check("...and none when not, with the flag named",
          rc == 0 and "--sheet-title" in log, log)

    # The date is given, never generated: a date this tool invented would
    # be the date it ran, which is not when the drawing was made.
    check("...and the date is an argument, not a clock read",
          "--date" in (TOOL.read_text()))

    # --- break probe: no blocks at all ------------------------------------
    d2 = tmp / "b"
    src2, _ = build(d2)
    (d2 / "netlist.py").unlink()
    (d2 / "design.yaml").write_text("rails:\n  GND:\n    type: ground\n",
                                    encoding="utf-8")
    rc, log = run(src2, d2 / "netlist.json", "-o", d2 / "out.epro2")
    check("a netlist with no blocks is refused", rc == 1, log)
    check("...and says where blocks come from", "design.yaml" in log, log)
    check("...and writes nothing", not (d2 / "out.epro2").exists())

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all blocktitles tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
