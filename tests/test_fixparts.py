#!/usr/bin/env python3
"""Regression tests for tools/fixparts.py.

The importer fills a per-instance `Supplier Part` attribute with the partId
(`ESP32-C6-WROOM-1-N8.1`) where the real value is on the DEVICE document
(`C5366877`). This tool copies the right one onto the instance.

The thing worth testing hardest is not that it fixes the value -- it is that
it fixes *only* that value. A tool that rewrites an export has to leave every
coordinate, every record and every other key exactly as it found them, or the
schematic it hands back is not the one that was checked.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "fixparts.py"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def make(path, parts, with_device=True):
    """parts: [(designator, lcsc, instance_value)]"""
    recs, t = [], 0

    def add(h, b):
        nonlocal t
        t += 1
        h["ticket"] = t
        recs.append((h, b))

    add({"type": "DOCHEAD"}, {"docType": "SCHEMATIC", "uuid": "sch"})
    for i, (d, lcsc, inst) in enumerate(parts):
        add({"type": "COMPONENT", "id": f"c{i}"},
            {"partId": f"P{i}.1", "x": i * 300, "y": -100})
        add({"type": "ATTR", "id": f"d{i}"},
            {"key": "Designator", "value": d, "parentId": f"c{i}"})
        add({"type": "ATTR", "id": f"v{i}"},
            {"key": "Device", "value": f"dev{i}", "parentId": f"c{i}"})
        add({"type": "ATTR", "id": f"s{i}"},
            {"key": "Supplier Part", "value": inst, "parentId": f"c{i}",
             "x": i * 300, "y": -60})
        add({"type": "ATTR", "id": f"m{i}"},
            {"key": "Manufacturer Part", "value": f"MPN{i}", "parentId": f"c{i}"})
    if with_device:
        for i, (d, lcsc, _) in enumerate(parts):
            add({"type": "DOCHEAD"}, {"docType": "DEVICE", "uuid": f"dev{i}"})
            add({"type": "META", "id": f"meta{i}"},
                {"title": f"P{i}", "attributes": {"Supplier Part": lcsc}})

    lines = [f"{json.dumps(h, separators=(',', ':'))}||"
             f"{json.dumps(b, separators=(',', ':'))}|" for h, b in recs]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project2.json", json.dumps({"title": "t"}))
        z.writestr("doc.epru", "\n".join(lines))


def read(path):
    z = zipfile.ZipFile(path)
    out = {}
    for line in z.read("doc.epru").decode("utf-8").split("\n"):
        if "||" not in line:
            continue
        h, _, b = line.partition("||")
        h, b = json.loads(h), json.loads(b.rstrip("|"))
        if h.get("id") is not None:
            out[h["id"]] = b
    return out


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main():
    tmp = Path(tempfile.mkdtemp())

    parts = [("U1", "C5366877", "ESP32-C6-WROOM-1-N8.1"),   # the decoy
             ("R1", "C25744", "C25744"),                    # already right
             ("U?", "C999", "TEMPLATE.1")]                  # a library template
    src = tmp / "in.epro2"
    out = tmp / "out.epro2"
    make(src, parts)

    rc, log = run(src, "-o", out)
    check("it succeeds", rc == 0, log)

    if out.exists():
        a, b = read(src), read(out)
        check("the decoy value is replaced by the device's C-number",
              b["s0"]["value"] == "C5366877", str(b["s0"]))
        check("an already-correct value is left alone",
              b["s1"]["value"] == "C25744")
        check("a library template (designator ends in ?) is not touched",
              b["s2"]["value"] == "TEMPLATE.1", str(b["s2"]))
        check("...and is reported as a template, not as a skip",
              "library template" in log, log)

        # the point of the whole file
        check("no coordinate anywhere changed",
              all(a[k].get("x") == b[k].get("x") and a[k].get("y") == b[k].get("y")
                  for k in a))
        check("no record appeared or vanished", set(a) == set(b))
        others = [k for k in a if a[k] != b[k]]
        check("exactly one attribute differs, and it is the intended one",
              others == ["s0"], str(others))
        check("no other key on that record moved",
              {k for k in set(a["s0"]) | set(b["s0"])
               if a["s0"].get(k) != b["s0"].get(k)} == {"value"})

    # --- break probe: nothing to copy from --------------------------------
    bare = tmp / "bare.epro2"
    gone = tmp / "gone.epro2"
    make(bare, parts, with_device=False)
    rc, log = run(bare, "-o", gone)
    check("an export with no DEVICE document is refused",
          rc == 2 and "no DEVICE document" in log, log)
    check("...and nothing is written", not gone.exists())

    # --- dry run writes nothing -------------------------------------------
    dry = tmp / "dry.epro2"
    rc, log = run(src, "--dry-run")
    check("--dry-run reports and writes nothing",
          rc == 0 and "nothing written" in log and not dry.exists(), log)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all fixparts tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
