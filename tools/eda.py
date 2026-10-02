#!/usr/bin/env python3
"""Read the pin table of an LCSC part out of the EasyEDA Pro library.

The netlist format keys `"pins"` by the **pin number the EasyEDA symbol uses**,
and nothing else resolves it: the importer places the library's own symbol, so
a key that matches the datasheet but not the symbol wires nothing. For most
parts the two agree and this tool is redundant. For connectors they routinely
do not -- the CX90B-16P symbol numbers its signal contacts `A1 ... B12` after
Hirose's drawing and its shield tabs `1 ... 4`, which no datasheet pin table
states, because it is a property of the symbol rather than of the part.

    ./tools/eda.py pins C3198004          # pin number -> name, as the symbol has it
    ./tools/eda.py pins C3198004 --kb     # emit a ready kb.py set-pins command
    ./tools/eda.py device C3198004        # symbol/footprint titles and uuids

This reads the same library the importer reads, so a pin map built from it is
checkable before the import rather than after. It is an unofficial endpoint --
see CLAUDE.md under Maintenance; `tools/kb.py` reads a different one, so the
two fail independently.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

SEARCH = "https://pro.easyeda.com/api/eda/product/search?keyword={}&currPage=1&pageSize=1"
DEVICE = "https://pro.easyeda.com/api/devices/{}"
COMPONENT = "https://pro.easyeda.com/api/components/{}"

# The short "Mozilla/5.0" that works against wmsc.lcsc.com gets a 403 here.
# The endpoint is behind a CDN that wants a browser-shaped agent string.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        sys.exit(f"EasyEDA request failed: {e}\n"
                 f"  {url}\n"
                 "If every part fails rather than one, the endpoint has moved "
                 "-- see CLAUDE.md under Maintenance.")


def device(code):
    """Resolve an LCSC code to its EasyEDA Pro device record."""
    res = (get(SEARCH.format(code)).get("result") or {})
    items = res.get("productList") or []
    if not items:
        sys.exit(f"{code}: not in the EasyEDA Pro library")
    uuid = items[0].get("hasDevice")
    if not uuid:
        sys.exit(f"{code}: found in the catalogue but carries no device -- "
                 "there is no symbol to import, so the netlist cannot place it")
    d = get(DEVICE.format(uuid)).get("result") or {}
    d["product_code"] = d.get("product_code") or code
    return d


def pins(code):
    """[(number, name, type)] in symbol order."""
    dev = device(code)
    sym = (dev.get("symbol") or {}).get("uuid")
    if not sym:
        sys.exit(f"{code}: the device has no symbol")
    data = (get(COMPONENT.format(sym)).get("result") or {}).get("dataStr") or ""

    # dataStr is one JSON array per line. A PIN record declares the pin and
    # carries an id; its NAME, NUMBER and "Pin Type" arrive as separate ATTR
    # records pointing back at that id. Order of the PIN records is the order
    # the symbol draws them, which is the order worth printing.
    order, attrs = [], {}
    for line in data.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec[0] == "PIN":
            order.append(rec[1])
        elif rec[0] == "ATTR" and len(rec) > 4 and rec[2]:
            attrs.setdefault(rec[2], {})[rec[3]] = rec[4]
    out = []
    for pid in order:
        a = attrs.get(pid, {})
        out.append((str(a.get("NUMBER", "?")), a.get("NAME", ""),
                    a.get("Pin Type", "")))
    return dev, out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pins", help="pin number -> name, as the symbol has it")
    p.add_argument("code")
    p.add_argument("--kb", action="store_true",
                   help="emit a kb.py set-pins command instead of a table")
    p.add_argument("--json", action="store_true")
    d = sub.add_parser("device", help="symbol and footprint titles and uuids")
    d.add_argument("code")
    args = ap.parse_args()

    if args.cmd == "device":
        dev = device(args.code)
        print(f"{dev['product_code']}  {dev.get('display_title','?')}")
        for k in ("symbol", "footprint"):
            v = dev.get(k) or {}
            print(f"  {k:<10} {v.get('display_title','?'):<32} {v.get('uuid','?')}")
        return 0

    dev, rows = pins(args.code)
    if args.json:
        print(json.dumps([{"number": n, "name": nm, "type": t}
                          for n, nm, t in rows], indent=2))
    elif args.kb:
        # Pin type is left off: the symbol's "Undefined" is not evidence, and
        # kb.py would record it as fact. Fill the types in by hand from the
        # datasheet, which is the source the ERC is entitled to trust.
        print(f"./tools/kb.py set-pins {args.code} \\")
        print(f"  --source \"EasyEDA Pro symbol "
              f"{(dev.get('symbol') or {}).get('display_title','?')} "
              f"(tools/eda.py pins)\" \\")
        for n, nm, _ in rows:
            print(f"  --pin \"{n}:{nm}:TYPE\" \\")
        print("  # replace each TYPE from the datasheet -- see kb/VOCABULARY.md")
    else:
        print(f"{dev['product_code']}  {dev.get('display_title','?')}  "
              f"symbol {(dev.get('symbol') or {}).get('display_title','?')}")
        for n, nm, t in rows:
            print(f"  {n:<6} {nm:<16} {t}")
        print(f"\n{len(rows)} pin(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
