#!/usr/bin/env python3
"""Check every LCSC part a document references against live stock.

The catalogue mirrors lie. jlcsearch's index reported six- and seven-figure
stock for five parts in this project's BOM that the live endpoint put at
**zero** — including the 100 nF 0402 used in eighteen places. CLAUDE.md
already says "stock in jlcsearch output is a hint, not a fact"; this is the
check that enforces it.

    ./tools/stock.py designs/led-matrix-controller/README.md
    ./tools/stock.py --kb                  # every part in kb/parts/
    ./tools/stock.py C1525 C25744          # named parts
    ./tools/stock.py --refresh <file>      # also write stock back into kb/

Exit status is 1 if any part is out of stock **or unreachable**, so it gates a
BOM the same way erc.py gates a netlist. Unreachable matters: the LCSC endpoint
is unofficial and has moved once already, and an exit of 0 because nothing could
be read would be the worst possible answer.
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

API = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode="
UA = {"User-Agent": "Mozilla/5.0"}
ROOT = pathlib.Path(__file__).resolve().parent.parent
CNUM = re.compile(r"\bC\d{3,9}\b")

# Below this, a part is worth re-sourcing before committing to a BOM: one
# reel is 5000 parts, and a run of this board wants a few hundred of the
# common values.
LOW = 5000


def live(code):
    """Return (model, stock) from LCSC, or (None, None) if unreachable."""
    try:
        req = urllib.request.Request(API + code, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.load(r).get("result") or {}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None, None
    if not result:
        return None, None
    return result.get("productModel"), result.get("stockNumber")


def codes_from(paths):
    """Every distinct C-number mentioned, in first-seen order."""
    seen = {}
    for p in paths:
        text = pathlib.Path(p).read_text(encoding="utf-8")
        for c in CNUM.findall(text):
            seen.setdefault(c, p)
    return seen


def kb_record(code):
    f = ROOT / "kb" / "parts" / f"{code}.json"
    return f if f.exists() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*",
                    help="files to scan for C-numbers, or C-numbers themselves")
    ap.add_argument("--kb", action="store_true", help="check every part in kb/parts/")
    ap.add_argument("--refresh", action="store_true",
                    help="write the live stock figure back into the kb record")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.kb:
        where = {f.stem: "kb/parts" for f in sorted((ROOT / "kb" / "parts").glob("C*.json"))}
    else:
        explicit = [t for t in args.targets if CNUM.fullmatch(t)]
        files = [t for t in args.targets if not CNUM.fullmatch(t)]
        where = {c: "argv" for c in explicit}
        if files:
            where.update(codes_from(files))
    if not where:
        ap.error("nothing to check — give a file, a C-number, or --kb")

    rows = []
    for code in where:
        model, stock = live(code)
        rows.append({"lcsc": code, "model": model, "stock": stock,
                     "where": where[code]})
        if args.refresh and stock is not None:
            f = kb_record(code)
            if f:
                d = json.loads(f.read_text(encoding="utf-8"))
                d["stock"] = stock
                # Stamp the date too. A fresh number under a stale date is
                # worse than a stale number, because the date is what the
                # workflow uses to decide whether to believe the figure.
                d["retrieved"] = datetime.date.today().isoformat()
                f.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")

    rows.sort(key=lambda r: (r["stock"] is None, r["stock"] or 0))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            if r["stock"] is None:
                mark, s = "?", "unreachable"
            elif r["stock"] == 0:
                mark, s = "!", "OUT OF STOCK"
            elif r["stock"] < LOW:
                mark, s = "-", f"{r['stock']:,} — thin"
            else:
                mark, s = " ", f"{r['stock']:,}"
            print(f" {mark} {r['lcsc']:<11} {(r['model'] or '?'):<24} {s}")

    dead = [r for r in rows if r["stock"] == 0]
    unknown = [r for r in rows if r["stock"] is None]
    thin = [r for r in rows if r["stock"] is not None and 0 < r["stock"] < LOW]
    if not args.json:
        print(f"\n{len(rows)} part(s): {len(dead)} out of stock, "
              f"{len(thin)} thin, {len(unknown)} unreachable")
        if dead:
            print("\nRe-source the out-of-stock parts before ordering. The "
                  "catalogue mirrors are cached and will not tell you.")
        if unknown:
            print("\nSome parts could not be reached. The LCSC endpoint is "
                  "unofficial and has moved before — see CLAUDE.md under "
                  "Maintenance. A clean report here proves nothing.")
    # Unreachable is not the same as in stock. If the endpoint moves, every
    # part comes back None and a status of 0 would read as "BOM is fine".
    return 1 if dead or unknown else 0


if __name__ == "__main__":
    sys.exit(main())
