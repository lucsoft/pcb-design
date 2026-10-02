#!/usr/bin/env python3
"""Knowledge base for LCSC parts.

Each part is one JSON-LD document under kb/parts/<C-number>.json. `add` pulls
everything LCSC already knows — description, package, parametric specs, stock,
price, datasheet URL — so the only thing left to fill in by hand is the part
of the record that LCSC does not publish and the ERC actually needs: the pin
map, with a type and a voltage limit per pin.

    kb.py add C8734        create or refresh a record from LCSC
    kb.py show C8734       print the record
    kb.py list             table of every known part
    kb.py check            validate all records, report gaps
    kb.py pins C8734       show the pin map

`add` never overwrites hand-written pins, rules, or provenance; it refreshes
only the fields it owns. Stock and price go stale, so they are stamped with
the date they were retrieved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "kb" / "parts"
API = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

VALID_PIN_TYPES = {
    "power_in", "power_out", "input", "output", "bidirectional", "tri_state",
    "open_collector", "open_emitter", "passive", "nc", "unspecified",
}

# Maps LCSC catalogue text onto the coarse category the ERC reasons about.
#
# Split into two passes because LCSC's own taxonomy nests specific parts under
# generic parents: an LDO sits under "Integrated Circuits (ICs)", so a single
# ordered list would classify every regulator as a plain IC. Specific terms are
# tried across the whole catalogue string first; generic ones only if nothing
# more precise matched.
SPECIFIC_HINTS = [
    ("voltage regulator", "regulator"), ("ldo", "regulator"),
    ("dc-dc", "regulator"), ("switching regulator", "regulator"),
    ("regulator", "regulator"),
    ("resistor", "resistor"), ("capacitor", "capacitor"),
    ("inductor", "inductor"), ("ferrite", "inductor"),
    ("crystal", "crystal"), ("oscillator", "crystal"), ("resonator", "crystal"),
    ("led", "led"), ("diode", "diode"), ("rectifier", "diode"),
    ("transistor", "transistor"), ("mosfet", "transistor"),
    ("sensor", "sensor"), ("fuse", "fuse"), ("varistor", "fuse"),
    ("switch", "switch"), ("button", "switch"), ("tactile", "switch"),
    ("connector", "connector"), ("header", "connector"), ("socket", "connector"),
    ("terminal", "connector"), ("usb", "connector"),
    ("microcontroller", "ic"), ("mcu", "ic"), ("embedded", "ic"),
]
GENERIC_HINTS = [
    ("integrated circuit", "ic"), ("logic", "ic"), ("interface", "ic"),
    ("amplifier", "ic"), ("memory", "ic"), ("rf", "ic"), ("ic", "ic"),
]


def fetch_lcsc(part: str) -> dict | None:
    try:
        req = urllib.request.Request(API.format(part), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except Exception as exc:
        print(f"error: LCSC request failed: {exc}", file=sys.stderr)
        return None
    if not data.get("ok") and data.get("code") != 200:
        print(f"error: LCSC says: {data.get('msg')}", file=sys.stderr)
        return None
    return data.get("result")


def _match(hints: list, text: str) -> str | None:
    """First hint whose words appear in text, matched on word boundaries.

    Boundaries matter: a plain substring test classifies "Chip Resistor -
    Surface Mount" as RF, because "Surface" contains "rf".
    """
    for needle, cat in hints:
        if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"s?(?![a-z0-9])", text):
            return cat
    return None


def guess_category(res: dict) -> str:
    """Classify a part from its LCSC catalogue, falling back to the blurb."""
    catalogue = " ".join(filter(None, [
        res.get("wmCatalogNameEn", ""),
        res.get("parentCatalogName", ""),
        " ".join(c.get("catalogNameEn", "")
                 for c in res.get("parentCatalogList") or []),
    ])).lower()
    blurb = " ".join(filter(None, [
        res.get("productNameEn", ""), res.get("productModel", ""),
    ])).lower()

    for text in (catalogue, blurb):
        if hit := _match(SPECIFIC_HINTS, text):
            return hit
    for text in (catalogue, blurb):
        if hit := _match(GENERIC_HINTS, text):
            return hit
    return "unknown"


def cmd_add(part: str, force_pins: bool) -> int:
    PARTS.mkdir(parents=True, exist_ok=True)
    path = PARTS / f"{part}.json"
    existing = json.loads(path.read_text()) if path.exists() else {}

    res = fetch_lcsc(part)
    if res is None:
        return 2

    params = {}
    for p in res.get("paramVOList") or []:
        name = p.get("paramNameEn")
        value = p.get("paramValueEn")
        if name and value:
            params[name] = value

    prices = res.get("productPriceList") or []
    unit_price = None
    if prices:
        try:
            unit_price = float(prices[0].get("usdPrice"))
        except (TypeError, ValueError):
            pass

    pdf = (res.get("pdfUrl") or "").split("?", 1)[0] or None
    today = dt.date.today().isoformat()

    record = {
        "@context": "../context.jsonld",
        "@id": f"lcsc:{part}",
        "@type": "Component",
        "lcscId": part,
        "mpn": res.get("productModel"),
        "manufacturer": res.get("brandNameEn"),
        "description": res.get("productIntroEn") or res.get("productNameEn"),
        "category": guess_category(res),
        "package": res.get("encapStandard"),
        "stock": res.get("stockNumber"),
        "priceUsd": unit_price,
        "parameters": params,
        "datasheet": pdf,
        "retrieved": today,
    }
    # Hand-written knowledge is never clobbered by a refresh.
    for key in ("pins", "rules", "provenance", "notes", "tier", "seeAlso"):
        if key in existing and not (key == "pins" and force_pins):
            record[key] = existing[key]

    # Category is LCSC-owned and re-guessed on every refresh, unless its
    # provenance says a human corrected it — LCSC's own classification is
    # coarse and sometimes wrong, so the override has to stick.
    if existing.get("provenance", {}).get("category") == "manual":
        record["category"] = existing.get("category", record["category"])

    record.setdefault("tier", None)
    record.setdefault("pins", [])
    prov = record.setdefault("provenance", {})
    for key in ("description", "package", "parameters", "stock", "priceUsd"):
        prov[key] = "lcsc-api"
    if prov.get("category") != "manual":
        prov["category"] = "lcsc-api"
    prov.setdefault("pins", "")

    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    verb = "updated" if existing else "created"
    print(f"{verb}  {path.relative_to(ROOT)}")
    print(f"  {record['mpn']} — {record['manufacturer']} — {record['package']}")
    print(f"  category {record['category']}, stock {record['stock']}, "
          f"${record['priceUsd']}")
    print(f"  {len(params)} parameter(s) from LCSC")
    if not record["pins"]:
        print(f"\n  pin map is EMPTY — ERC cannot check this part yet.")
        if pdf:
            print(f"  next: tools/ds.py fetch {part} && tools/ds.py index {part}")
        else:
            print(f"  LCSC has no datasheet URL for this part; find one manually.")
    if record.get("tier") is None:
        print(f"  tier unknown — set \"tier\" to \"basic\" or \"extended\" "
              f"to get assembly-fee warnings")
    return 0


def cmd_show(part: str) -> int:
    path = PARTS / f"{part}.json"
    if not path.exists():
        print(f"error: {part} not in the knowledge base", file=sys.stderr)
        return 2
    print(path.read_text())
    return 0


def cmd_pins(part: str) -> int:
    path = PARTS / f"{part}.json"
    if not path.exists():
        print(f"error: {part} not in the knowledge base", file=sys.stderr)
        return 2
    rec = json.loads(path.read_text())
    pins = rec.get("pins") or []
    if not pins:
        print(f"{part} has no pin map")
        return 1
    prov = rec.get("provenance", {}).get("pins", "?")
    print(f"\n{part}  {rec.get('mpn')}  —  {len(pins)} pins  [source: {prov}]\n")
    print(f"  {'pin':<6} {'name':<16} {'type':<16} {'vMax':>7} {'vMin':>7}")
    print(f"  {'-'*6} {'-'*16} {'-'*16} {'-'*7} {'-'*7}")
    for p in pins:
        print(f"  {str(p.get('number','')):<6} {str(p.get('name','')):<16} "
              f"{str(p.get('type','')):<16} "
              f"{str(p.get('vMax','')):>7} {str(p.get('vMin','')):>7}")
    print()
    return 0


def cmd_list() -> int:
    files = sorted(PARTS.glob("*.json"))
    if not files:
        print("knowledge base is empty — add a part with: kb.py add C8734")
        return 0
    print(f"\n{len(files)} part(s)\n")
    print(f"  {'lcsc':<10} {'mpn':<22} {'category':<11} {'pkg':<16} "
          f"{'pins':>5} {'stock':>8}")
    print(f"  {'-'*10} {'-'*22} {'-'*11} {'-'*16} {'-'*5} {'-'*8}")
    for f in files:
        r = json.loads(f.read_text())
        npins = len(r.get("pins") or [])
        print(f"  {r.get('lcscId',f.stem):<10} {str(r.get('mpn'))[:22]:<22} "
              f"{str(r.get('category'))[:11]:<11} {str(r.get('package'))[:16]:<16} "
              f"{npins if npins else '—':>5} {str(r.get('stock','?')):>8}")
    print()
    return 0


def cmd_check() -> int:
    files = sorted(PARTS.glob("*.json"))
    problems = 0
    for f in files:
        try:
            r = json.loads(f.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR {f.name}: invalid JSON — {exc}")
            problems += 1
            continue

        if r.get("lcscId") != f.stem:
            print(f"ERROR {f.name}: lcscId '{r.get('lcscId')}' != filename")
            problems += 1

        pins = r.get("pins") or []
        if not pins:
            print(f"WARN  {f.name}: no pin map — ERC will skip this part")
            problems += 1
        seen = set()
        for p in pins:
            num = str(p.get("number", ""))
            if not num:
                print(f"ERROR {f.name}: a pin has no number")
                problems += 1
            elif num in seen:
                print(f"ERROR {f.name}: pin {num} listed twice")
                problems += 1
            seen.add(num)
            t = p.get("type")
            if t not in VALID_PIN_TYPES:
                print(f"ERROR {f.name}: pin {num} has invalid type '{t}' "
                      f"(see kb/VOCABULARY.md)")
                problems += 1

        if pins and not r.get("provenance", {}).get("pins"):
            print(f"WARN  {f.name}: pin map has no provenance — record where it "
                  f"came from, e.g. 'datasheet p.21 Table 5'")
            problems += 1

        if r.get("tier") not in ("basic", "extended", None):
            print(f"ERROR {f.name}: tier must be 'basic', 'extended' or null")
            problems += 1

    print(f"\n{len(files)} record(s), {problems} problem(s)\n")
    return 1 if problems else 0


def parse_pin_spec(spec: str) -> dict:
    """Parse `number:name:type[:vmax=V][:vmin=V][:imax=mA][:contact=D][:source=S]`.

    Example: 2:VOUT:power_out:vmax=7.0

    `number` is the pin number the **EasyEDA symbol** uses, because that is
    what a netlist keys `pins` by. `contact` is the designation printed on the
    part or given in its datasheet, where the two differ -- the WIZ850io's
    header pins are silkscreened `J1-1 ... J2-6` and numbered 1-12 by the
    symbol, in opposite directions on the J2 side. Recording both is what lets
    a netlist be written from the symbol and a board still be assembled from
    the silkscreen.
    """
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(f"'{spec}' needs at least number:name:type")
    pin = {"number": parts[0].strip(), "name": parts[1].strip(),
           "type": parts[2].strip()}
    if pin["type"] not in VALID_PIN_TYPES:
        raise ValueError(f"'{pin['type']}' is not a valid pin type "
                         f"(see kb/VOCABULARY.md)")
    for extra in parts[3:]:
        if "=" not in extra:
            raise ValueError(f"'{extra}' should look like vmax=7.0")
        key, _, val = extra.partition("=")
        key = key.strip().lower()
        if key in ("contact", "source"):
            # Free text, not a measurement, so neither goes through float().
            # `contact` is a designation like "J1-1" or "A5". `source` overrides
            # the record-level provenance for THIS pin, which is what lets one
            # inferred pin sit in an otherwise datasheet-backed map without
            # dragging the other eleven down to warning severity -- erc.py's
            # unverified() prefers it. Avoid ':' in the value; it is the
            # field separator.
            pin[key] = val.strip()
            continue
        field = {"vmax": "vMax", "vmin": "vMin", "imax": "iMaxMa"}.get(key)
        if not field:
            raise ValueError(f"unknown pin attribute '{key}'")
        pin[field] = float(val)
    return pin


def cmd_set_pins(part: str, specs: list, source: str) -> int:
    path = PARTS / f"{part}.json"
    if not path.exists():
        print(f"error: {part} not in the knowledge base — run: kb.py add {part}",
              file=sys.stderr)
        return 2
    rec = json.loads(path.read_text())
    try:
        pins = [parse_pin_spec(s) for s in specs]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    seen = set()
    for pin in pins:
        if pin["number"] in seen:
            print(f"error: pin {pin['number']} given twice", file=sys.stderr)
            return 2
        seen.add(pin["number"])

    rec["pins"] = pins
    rec.setdefault("provenance", {})["pins"] = source
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    print(f"{part}: {len(pins)} pin(s) recorded, source: {source}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create or refresh a record from LCSC")
    a.add_argument("part")
    a.add_argument("--force-pins", action="store_true",
                   help="also discard the existing pin map")
    for name, helptext in [("show", "print a record"), ("pins", "show the pin map")]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("part")
    sub.add_parser("list", help="table of every known part")
    sub.add_parser("check", help="validate all records")

    sp = sub.add_parser("set-pins", help="record a pin map")
    sp.add_argument("part")
    sp.add_argument("--pin", action="append", required=True, dest="pins",
                    metavar="N:NAME:TYPE[:vmax=V]",
                    help="repeatable, e.g. --pin 2:VOUT:power_out:vmax=7.0")
    sp.add_argument("--source", required=True,
                    help="where this came from, e.g. 'datasheet p.2 PIN ASSIGNMENT'")

    args = ap.parse_args()
    if args.cmd == "add":
        return cmd_add(args.part, args.force_pins)
    if args.cmd == "show":
        return cmd_show(args.part)
    if args.cmd == "pins":
        return cmd_pins(args.part)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "set-pins":
        return cmd_set_pins(args.part, args.pins, args.source)
    return 2


if __name__ == "__main__":
    sys.exit(main())
