#!/usr/bin/env python3
"""Read the pin table of an LCSC part out of the EasyEDA Pro library.

The netlist format keys `"pins"` by the **pin number the EasyEDA symbol uses**,
and nothing else resolves it: the importer places the library's own symbol, so
a key that matches the datasheet but not the symbol wires nothing. For most
parts the two agree and this tool is redundant. For connectors they routinely
do not -- the CX90B-16P symbol numbers its signal contacts `A1 ... B12` after
Hirose's drawing, its shield tabs `0` and `1` and its mid-plate tabs `2` and
`3` -- none of which any datasheet pin table states, because it is a property of the symbol rather than of the part.

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
import pathlib
import re
import sys
import urllib.error
import urllib.request

SEARCH = "https://pro.easyeda.com/api/eda/product/search?keyword={}&currPage=1&pageSize=1"
DEVICE = "https://pro.easyeda.com/api/devices/{}"
COMPONENT = "https://pro.easyeda.com/api/components/{}"

PARTS = pathlib.Path(__file__).resolve().parent.parent / "kb" / "parts"
CNUM = re.compile(r"\bC\d{3,9}\b")

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


def verify(codes):
    """Compare each kb record's pin numbers against the symbol's.

    Returns the number of parts that disagree. A disagreement is not always a
    bug -- a part with no pin map is simply unchecked -- but a kb key the
    symbol does not have is one, every time: the netlist would carry a pin the
    importer cannot place, and it places silently.
    """
    bad = unreachable = 0
    for code in codes:
        f = PARTS / f"{code}.json"
        if not f.exists():
            print(f"  ?  {code:<11} not in the knowledge base")
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        kb = {str(p["number"]) for p in rec.get("pins") or []}
        if not kb:
            print(f"  -  {code:<11} no pin map -- unchecked")
            continue
        try:
            _, rows = pins(code)
        except SystemExit as e:
            # Absent from the EasyEDA library is not a wrong key -- it means
            # the importer cannot place the part at all, which S4/K1 are the
            # right rules for. Counting it here made `verify --kb` unusable as
            # a gate over a knowledge base that holds evaluated-and-rejected
            # parts as well as fitted ones.
            print(f"  ?  {code:<11} {e}")
            unreachable += 1
            continue
        sym = {n for n, _, _ in rows}
        missing = sorted(kb - sym)   # in kb, not in the symbol: the netlist breaks
        extra = sorted(sym - kb)     # in the symbol, not in kb: merely unrecorded
        if missing:
            bad += 1
            print(f"  !  {code:<11} {len(missing)} key(s) the symbol does not "
                  f"have: {', '.join(missing)}")
            if extra:
                print(f"     {'':<11} the symbol instead has: {', '.join(extra)}")
        elif extra:
            print(f"  -  {code:<11} symbol has {len(extra)} pin(s) the record "
                  f"omits: {', '.join(extra)}")
        else:
            print(f"     {code:<11} {len(kb)} pin(s) agree")
    return bad, unreachable


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
    v = sub.add_parser("verify",
                       help="check kb pin numbers against the symbol's")
    v.add_argument("targets", nargs="*",
                   help="C-numbers, or files to scan for them")
    v.add_argument("--kb", action="store_true", help="every part in kb/parts/")
    args = ap.parse_args()

    if args.cmd == "verify":
        if args.kb:
            codes = sorted(f.stem for f in PARTS.glob("C*.json"))
        else:
            codes, seen = [], set()
            for t in args.targets:
                if CNUM.fullmatch(t):
                    found = [t]
                else:
                    found = CNUM.findall(
                        pathlib.Path(t).read_text(encoding="utf-8"))
                for c in found:
                    if c not in seen:
                        seen.add(c)
                        codes.append(c)
        if not codes:
            ap.error("nothing to check -- give a file, a C-number, or --kb")
        bad, unreachable = verify(codes)
        print(f"\n{len(codes)} part(s), {bad} with a key the symbol does not "
              f"have, {unreachable} unreachable")
        if bad:
            print("\nFix these before writing the netlist. The importer keys "
                  "pins by the symbol's numbering and ignores anything else, "
                  "so a key it does not have is a pin that silently goes "
                  "nowhere.")
        if unreachable:
            # Unreachable is not agreement. The endpoint is unofficial and has
            # moved before: if it moves again every part comes back '?' and an
            # exit of 0 would read as "every pin map checks out". stock.py
            # already fails this way; this now matches it.
            print(f"\n{unreachable} part(s) could not be read. A few are parts "
                  f"the library does not carry, which S4 and K1 cover -- but if "
                  f"it is most of them the endpoint has moved, and a clean "
                  f"report here proves nothing. See CLAUDE.md under Maintenance.")
        # Fail when nothing could be checked, or when most of it could not.
        return 1 if bad or unreachable > max(1, len(codes) // 10) else 0

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
