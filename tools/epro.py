#!/usr/bin/env python3
"""Read an EasyEDA Pro .epro2 archive.

The archive is a zip holding `project2.json` and one or more `.epru` documents.
An `.epru` is line-oriented, and each line is two JSON objects:

    {"type":"FILL","ticket":22,"id":"e5"}||{"groupId":0,"netName":"",...}|

a header giving the record type, then the payload, then a trailing `|`. The
whole file is NOT one `||`-delimited stream, which is the easy mistake: split
on newlines first, then on `||` within each line.

    epro.py types  <file.epro2>          record types and counts
    epro.py bom    <file.epro2>          components with LCSC numbers
    epro.py nets   <file.epro2>          net names
    epro.py diff   <file.epro2> <netlist.json>   exported nets vs the netlist
    epro.py dump   <file.epro2> TYPE     raw payloads of one record type
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import zipfile
from pathlib import Path


def records(path: Path):
    """Yield (header, payload) for every record in every .epru in the archive."""
    z = zipfile.ZipFile(path)
    for name in z.namelist():
        if not name.endswith(".epru"):
            continue
        raw = z.read(name).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip().rstrip("|")
            if not line or "||" not in line:
                continue
            head, _, body = line.partition("||")
            try:
                h = json.loads(head)
                b = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError:
                continue
            yield h, b


def cmd_types(path: Path) -> int:
    counts = collections.Counter(h.get("type") or h.get("docType")
                                 for h, _ in records(path))
    print(f"\n{sum(counts.values())} records\n")
    for k, v in counts.most_common():
        print(f"  {str(k):<28} {v:>6}")
    print()
    return 0


def _walk(obj, want):
    """Find values for any of `want` keys, anywhere in a nested structure."""
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in want and isinstance(v, (str, int, float)):
                found[k] = v
            else:
                found.update(_walk(v, want))
    elif isinstance(obj, list):
        for v in obj:
            found.update(_walk(v, want))
    return found


def cmd_bom(path: Path) -> int:
    """List the distinct devices used.

    Two record types matter. META carries the library device definition and its
    `attributes` dict: supplier part, manufacturer part, JLCPCB part class. ATTR
    carries per-instance attribute placements on the canvas, including the real
    designator, linked back by `parentId`. The device list comes from META; the
    instance count comes from ATTR.
    """
    devices = {}
    designators = collections.Counter()

    for h, b in records(path):
        t = h.get("type") or h.get("docType") or ""
        if t == "META":
            attrs = b.get("attributes") or {}
            lcsc = attrs.get("Supplier Part", "")
            if not (lcsc or attrs.get("Manufacturer Part")):
                continue
            devices[b.get("title") or attrs.get("Name", "?")] = {
                "lcsc": lcsc,
                "mpn": attrs.get("Manufacturer Part", ""),
                "mfr": attrs.get("Manufacturer", ""),
                "cls": attrs.get("JLCPCB Part Class", ""),
                "desig": attrs.get("Designator", ""),
                "fp": attrs.get("Footprint", ""),
            }
        elif t == "ATTR" and b.get("key") == "Designator":
            v = (b.get("value") or "").strip()
            if v and v != "?":
                designators[v] += 1

    if not devices:
        print("no devices found", file=sys.stderr)
        return 1

    print(f"\n{len(devices)} distinct devices, "
          f"{len(designators)} placed designators\n")
    print(f"  {'LCSC':<11} {'device':<20} {'MPN':<26} {'class':<9} manufacturer")
    print(f"  {'-'*11} {'-'*20} {'-'*26} {'-'*9} {'-'*20}")
    for name in sorted(devices, key=lambda n: (devices[n]["desig"], n)):
        d = devices[name]
        cls = "basic" if "Basic" in d["cls"] else ("ext" if "Extended" in d["cls"] else "")
        print(f"  {str(d['lcsc'])[:11]:<11} {str(name)[:20]:<20} "
              f"{str(d['mpn'])[:26]:<26} {cls:<9} {str(d['mfr'])[:20]}")

    if designators:
        pre = collections.Counter("".join(c for c in d if c.isalpha())
                                  for d in designators)
        print(f"\n  placed by prefix: "
              + ", ".join(f"{k}x{v}" for k, v in sorted(pre.items())))
    print()
    return 0


def read_nets(path: Path) -> tuple[collections.Counter, collections.Counter]:
    """Net name -> reference count, plus which representation carried it.

    Two representations, and reading only the first is what made this report
    "0 nets" on a schematic holding 108 of them:

    - **PCB copper** carries the net on the object: `netName` on the trace.
    - **A schematic does not.** The wire record holds only an id; the net is an
      `ATTR` with `key: "NET"` whose `parentId` points back at the wire.
    """
    nets, how = collections.Counter(), collections.Counter()
    for h, b in records(path):
        n = b.get("netName") or b.get("net")
        if isinstance(n, str) and n.strip():
            nets[n] += 1
            how["object netName (PCB copper)"] += 1
            continue
        if (h.get("type") or "").upper() == "ATTR" and b.get("key") == "NET":
            v = b.get("value")
            if isinstance(v, str) and v.strip():
                nets[v] += 1
                how["ATTR key=NET (schematic wire)"] += 1
    return nets, how


def cmd_nets(path: Path) -> int:
    nets, how = read_nets(path)
    print(f"\n{len(nets)} nets\n")
    for k, v in nets.most_common():
        print(f"  {k:<28} {v:>5} refs")
    print()
    if not nets:
        # Say which shapes were looked for. A bare "0 nets" reads as "this
        # document has none" when it may mean "this document stores them in a
        # third way nobody here has seen".
        print("  no record carried a net name, in either representation:")
        print("    - PCB:       `netName` / `net` on the object")
        print("    - schematic: ATTR with key=NET, parentId -> the wire")
        print("  if the document plainly has nets, the format has moved.")
        print()
        return 1
    for k, v in how.most_common():
        print(f"  carried by {k}: {v} record(s)")
    print()
    return 0


def cmd_diff(path: Path, netlist: Path) -> int:
    """Compare an exported document's nets against the netlist that went in.

    This is step 8 of the workflow, and it is the only check that catches the
    two failure modes the canvas hides: a pin that looks wired but carries no
    net, and two nets silently merged into one. A merge removes a name; a lost
    net port lowers one name's count. Both show up here and nowhere else.

    Counting is by *pin references*, not by net, because a net that kept its
    name while losing half its pins is the case a name-only diff passes.
    """
    want: collections.Counter = collections.Counter()
    try:
        design = json.loads(netlist.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {netlist}: {e}", file=sys.stderr)
        return 2
    for comp in design.values():
        for net in (comp.get("pins") or {}).values():
            want[net] += 1

    have, how = read_nets(path)
    if not have:
        print(f"\nerror: {path.name} carried no net names at all — refusing to "
              f"report a clean diff against nothing", file=sys.stderr)
        return 2

    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    differ = sorted(n for n in set(want) & set(have) if want[n] != have[n])

    print(f"\n{netlist}")
    print(f"  {len(want):>4} nets, {sum(want.values()):>4} pin connections")
    print(f"{path}")
    print(f"  {len(have):>4} nets, {sum(have.values()):>4} pin connections")
    for k, v in how.most_common():
        print(f"       carried by {k}")
    print()

    for n in missing:
        print(f"  MISSING   {n}: {want[n]} pin(s) in the netlist, absent from the export")
    for n in extra:
        print(f"  EXTRA     {n}: {have[n]} pin(s) in the export, not in the netlist")
    for n in differ:
        print(f"  COUNT     {n}: netlist {want[n]} pin(s), export {have[n]}")

    bad = len(missing) + len(extra) + len(differ)
    if bad:
        print(f"\n  {bad} discrepancy(ies) — do not proceed to layout\n")
        return 1
    print(f"  identical: every net and every pin count agrees\n")
    return 0


def cmd_dump(path: Path, want: str, limit: int) -> int:
    n = 0
    for h, b in records(path):
        t = h.get("type") or h.get("docType") or ""
        if t.upper() != want.upper():
            continue
        print(json.dumps(b, ensure_ascii=False)[:1500])
        n += 1
        if n >= limit:
            break
    if not n:
        print(f"no records of type {want}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("types", "bom", "nets"):
        s = sub.add_parser(name)
        s.add_argument("file", type=Path)
    f = sub.add_parser("diff")
    f.add_argument("file", type=Path)
    f.add_argument("netlist", type=Path)
    d = sub.add_parser("dump")
    d.add_argument("file", type=Path)
    d.add_argument("recordtype")
    d.add_argument("-n", "--limit", type=int, default=3)
    a = ap.parse_args()
    if not a.file.exists():
        print(f"error: {a.file} not found", file=sys.stderr)
        return 2
    if a.cmd == "types": return cmd_types(a.file)
    if a.cmd == "bom":   return cmd_bom(a.file)
    if a.cmd == "nets":  return cmd_nets(a.file)
    if a.cmd == "diff":  return cmd_diff(a.file, a.netlist)
    if a.cmd == "dump":  return cmd_dump(a.file, a.recordtype, a.limit)
    return 2


if __name__ == "__main__":
    sys.exit(main())
