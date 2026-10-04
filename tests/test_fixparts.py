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

    add({"type": "DOCHEAD"}, {"docType": "SCH_PAGE", "uuid": "sch"})
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
        # empty, as the importer leaves them
        add({"type": "ATTR", "id": f"cls{i}"},
            {"key": "JLCPCB Part Class", "value": "", "parentId": f"c{i}"})
        add({"type": "ATTR", "id": f"fp{i}"},
            {"key": "Footprint", "value": None, "parentId": f"c{i}"})
        # already carrying a deliberate value, which must survive
        add({"type": "ATTR", "id": f"val{i}"},
            {"key": "Value", "value": f"KEEP{i}", "parentId": f"c{i}"})
    if with_device:
        for i, (d, lcsc, _) in enumerate(parts):
            add({"type": "DOCHEAD"}, {"docType": "DEVICE", "uuid": f"dev{i}"})
            add({"type": "META", "id": f"meta{i}"},
                {"title": f"P{i}", "attributes": {
                "Supplier Part": lcsc, "JLCPCB Part Class": "Extended Part",
                "Footprint": f"fpuuid{i}", "Value": f"DEVICE{i}",
                # keys the instance has no attribute for at all -- what
                # --complete has to create, and the two that must not be
                # created however complete the copy gets
                "Description": f"DESC{i}", "RDS(on)": "30.44m",
                "Name": "={Manufacturer Part}", "Designator": "U?"}})

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
        others = sorted(k for k in a if a[k] != b[k])
        check("only the intended attributes differ",
              others == ["cls0", "cls1", "fp0", "fp1", "s0"], str(others))
        check("no other key on that record moved",
              {k for k in set(a["s0"]) | set(b["s0"])
               if a["s0"].get(k) != b["s0"].get(k)} == {"value"})

    if out.exists():
        # --- the other device fields -------------------------------------
        # EasyEDA fills these itself, lazily, when a human opens the part.
        # There is no "assigned" flag anywhere; whether a part counts as
        # assigned is just whether these are filled.
        check("an empty JLCPCB Part Class is filled from the device",
              b["cls0"]["value"] == "Extended Part", str(b["cls0"]))
        check("a null Footprint is filled from the device",
              b["fp0"]["value"] == "fpuuid0", str(b["fp0"]))
        check("a non-empty instance value is NOT overwritten",
              b["val0"]["value"] == "KEEP0", str(b["val0"]))
        check("...including on a component that needed no C-number fix",
              b["val1"]["value"] == "KEEP1", str(b["val1"]))
        check("the template's fields are left alone too",
              b["cls2"]["value"] == "" and b["fp2"]["value"] is None,
              f'{b["cls2"]} {b["fp2"]}')

    # --- --complete: create what the instance lacks -----------------------
    comp = tmp / "complete.epro2"
    rc, log = run(src, "--complete", "-o", comp)
    check("--complete succeeds", rc == 0, log)
    if comp.exists():
        a2, b2 = read(src), read(comp)
        check("every original record survives unchanged",
              all(k in b2 for k in a2) and
              all(a2[k] == b2[k] for k in a2
                  if not (b2[k].get("key") in ("Supplier Part", "JLCPCB Part Class",
                                               "Footprint"))),
              "an original record was altered")
        made = {k: v for k, v in b2.items() if k not in a2}
        check("records were added, not replaced", len(made) > 0 and len(b2) > len(a2))
        keys = {v.get("key") for v in made.values()}
        check("Name is never created from the device's formula",
              "Name" not in keys, str(keys))
        check("Designator is never created from the device's template",
              "Designator" not in keys, str(keys))
        check("the template component gets nothing",
              not any(v.get("parentId") == "c2" for v in made.values()))

        # determinism: the ids are hashes, so a second run must be identical
        comp2 = tmp / "complete2.epro2"
        run(src, "--complete", "-o", comp2)
        import hashlib
        h1 = hashlib.sha256(zipfile.ZipFile(comp).read("doc.epru")).hexdigest()
        h2 = hashlib.sha256(zipfile.ZipFile(comp2).read("doc.epru")).hexdigest()
        check("two --complete runs are byte-identical", h1 == h2)

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
