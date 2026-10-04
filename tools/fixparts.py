#!/usr/bin/env python3
"""Put the real LCSC number on each component instance in an EasyEDA export.

The netlist importer writes a per-instance attribute called `Supplier Part`
and fills it with the **partId** -- `ESP32-C6-WROOM-1-N8.1` where the part is
`C5366877`. The correct number is on the DEVICE document the instance points
at, so the file resolves correctly today; but the property panel shows the
partId, and an instance attribute is the normal way to *override* a device
one. A BOM or PCBA export that honours the override carries a supplier part
that resolves to nothing.

A hand-drawn schematic has no per-instance copy of this field at all. The
value is extension noise, not something EasyEDA put there, which is why
correcting it rather than deleting it is the conservative choice: a wrong
value overridden by the right one is safe under either precedence rule, where
deleting a field assumes a precedence rule nobody here has confirmed.

    fixparts.py <export.epro2> -o <out.epro2>
    fixparts.py <export.epro2> --dry-run

**It touches nothing but the value of that one attribute.** No coordinate, no
record is added or removed, no other key. That is checked after writing: the
output must differ from the input in exactly the `Supplier Part` instance
attributes and nowhere else.

What this cannot settle is whether EasyEDA reads the instance or the device
when both exist. Nothing readable from outside answers that. **Export the BOM
from EasyEDA and look** -- that is the only thing that does, and it takes a
minute.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

KEY = "Supplier Part"


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru, found {len(eprus)}")
    rows, cur = [], None
    for line in z.read(eprus[0]).decode("utf-8").split("\n"):
        if "||" not in line:
            rows.append((None, None, None, line))
            continue
        head, _, body = line.partition("||")
        try:
            h = json.loads(head)
            b = json.loads(body.rstrip("|"))
        except json.JSONDecodeError:
            rows.append((None, None, None, line))
            continue
        if h.get("type") == "DOCHEAD":
            cur = (b.get("docType"), b.get("uuid"))
        rows.append((h, b, cur, line))
    return eprus[0], rows


def device_parts(rows) -> dict[str, str]:
    """DEVICE document uuid -> its Supplier Part."""
    out = {}
    for h, b, doc, _ in rows:
        if h and h["type"] == "META" and doc and doc[0] == "DEVICE":
            sp = (b.get("attributes") or {}).get(KEY)
            if sp:
                out[doc[1]] = sp
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    if not a.export.exists():
        print(f"error: {a.export} not found", file=sys.stderr)
        return 2

    name, rows = load(a.export)
    dev = device_parts(rows)
    if not dev:
        print(f"error: {a.export.name} holds no DEVICE document, so there is "
              f"no correct value to copy from", file=sys.stderr)
        return 2

    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    ref = {b["parentId"]: b["value"] for h, b, _, _ in rows
           if h and h["type"] == "ATTR" and b.get("key") == "Device"
           and b.get("value")}

    print(f"\n{a.export.name}")
    print(f"  {len(dev)} device definition(s), {len(desig)} placed component(s)")

    out_lines, fixes, skipped, templates = [], [], [], 0
    for h, b, doc, raw in rows:
        if (h is None or h["type"] != "ATTR" or b.get("key") != KEY
                or b.get("parentId") not in desig):
            out_lines.append(raw)
            continue
        cid = b["parentId"]
        # A designator ending in "?" is a library template inside a DEVICE
        # document, not a placement on the sheet. Correcting it would be
        # editing the part definition rather than this board's use of it.
        if desig[cid].endswith("?"):
            templates += 1
            out_lines.append(raw)
            continue
        want = dev.get(ref.get(cid))
        if not want:
            skipped.append((desig[cid], "points at no DEVICE document"))
            out_lines.append(raw)
            continue
        if b.get("value") == want:
            out_lines.append(raw)
            continue
        fixes.append((desig[cid], b.get("value"), want))
        nb = dict(b)
        nb["value"] = want
        out_lines.append(f"{json.dumps(h, separators=(',', ':'))}||"
                         f"{json.dumps(nb, separators=(',', ':'))}|")

    for d, old, new in fixes[:10]:
        print(f"  {d:6s} {old!r} -> {new}")
    if len(fixes) > 10:
        print(f"  ... and {len(fixes) - 10} more")
    for d, why in skipped:
        print(f"  SKIP   {d}: {why}")
    print(f"\n  {len(fixes)} attribute(s) to correct, "
          f"{templates} library template(s) left alone, {len(skipped)} skipped")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    # --- prove nothing else moved ----------------------------------------
    _, back = load(a.out)
    if len(back) != len(rows):
        a.out.unlink(missing_ok=True)
        print(f"  error: line count changed — {a.out} deleted\n", file=sys.stderr)
        return 1
    drift = []
    for (h1, b1, _, _), (h2, b2, _, _) in zip(rows, back):
        if h1 is None or h2 is None:
            if h1 is not h2 and (h1 is None) != (h2 is None):
                drift.append("a record appeared or vanished")
            continue
        if h1 != h2:
            drift.append(f"header changed: {h1.get('id')}")
            continue
        if b1 == b2:
            continue
        # The only permitted difference, on the only permitted key.
        changed = {k for k in set(b1) | set(b2) if b1.get(k) != b2.get(k)}
        if (h1["type"] != "ATTR" or b1.get("key") != KEY
                or changed != {"value"}):
            drift.append(f"{h1.get('id')}: changed {sorted(changed)}")
    if drift:
        a.out.unlink(missing_ok=True)
        for d in drift[:8]:
            print(f"  DRIFT  {d}")
        print(f"\n  {len(drift)} unintended change(s); {a.out} deleted\n")
        return 1

    print(f"  nothing else differs: same records, same order, same coordinates")
    print(f"  wrote {a.out}")
    print(f"\n  EasyEDA's own BOM export is the only thing that settles whether")
    print(f"  it reads the instance or the device. Export it and look.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
