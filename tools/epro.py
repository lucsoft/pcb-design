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


def cmd_nets(path: Path) -> int:
    nets = collections.Counter()
    for _, b in records(path):
        n = b.get("netName") or b.get("net")
        if isinstance(n, str) and n.strip():
            nets[n] += 1
    print(f"\n{len(nets)} nets\n")
    for k, v in nets.most_common():
        print(f"  {k:<28} {v:>5} refs")
    print()
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
    if a.cmd == "dump":  return cmd_dump(a.file, a.recordtype, a.limit)
    return 2


if __name__ == "__main__":
    sys.exit(main())
