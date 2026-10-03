#!/usr/bin/env python3
"""Check a design document against the artefacts it describes.

    ./tools/consistency.py designs/led-matrix-controller

`erc.py` checks the netlist and `stale.py` finds a value you changed in one
place. Neither catches the failure that has dominated this project's reviews:
a document stating a *count* of something in the netlist, the count changing,
and the prose not following. Nine consecutive review rounds found an instance.
Those numbers are derivable, so they should not be maintained by hand.

What it checks, all against `netlist.json` and `kb/`:

  component / pin / net / NC-pin counts the prose states
  resistor and capacitor counts, and the passive total
  every BOM row's C-number against the netlist's for that designator
  every designator in one and not the other
  the price column against each kb record's priceUsd

Exit status is 1 on any mismatch, so it gates a document the way erc.py gates
a netlist. A claim it cannot parse is reported as unchecked rather than passed:
silence about a number nobody verified is how this failure survives.
"""
import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS = ROOT / "kb" / "parts"


def load(design: pathlib.Path):
    net = json.loads((design / "netlist.json").read_text(encoding="utf-8"))
    doc = (design / "README.md").read_text(encoding="utf-8")
    return net, doc


def facts(net):
    """Everything about the netlist the prose might state."""
    pins = sum(len(c["pins"]) for c in net.values())
    all_nets = [n for c in net.values() for n in c["pins"].values()]
    nc = [n for n in all_nets if n.upper().startswith("NC")]
    real = {n for n in all_nets if not n.upper().startswith("NC")}
    desig = {c["props"]["Designator"]: c["props"]["Supplier Part"]
             for c in net.values()}
    kind = lambda p: sum(1 for d in desig if re.fullmatch(p, d))
    return {
        "components": len(net),
        "pins": pins,
        "real nets": len(real),
        "NC pins": len(nc),
        "resistors": kind(r"R\d+"),
        "capacitors": kind(r"C\d+"),
        "passives": kind(r"[RC]\d+"),
    }, desig


# Each claim is a regex with one capturing group, and the fact it must equal.
CLAIMS = [
    (r"\*\*(\d+) components, [\d,]+ pins", "components"),
    (r"\*\*[\d,]+ components, (\d+) pins", "pins"),
    (r"pins, (\d+) real nets", "real nets"),
    (r"\*\*(?:Thirty|(\d+)) pins, and this list is all of them\*\*", "NC pins"),
    (r"\*\*Resistors\*\* — (\d+) parts", "resistors"),
    (r"\*\*Capacitors\*\* — (\d+) parts", "capacitors"),
    (r"\| (\d+) passives \|", "passives"),
]

WORDS = {"thirty": 30, "twenty": 20, "ten": 10}


def check_counts(doc, f):
    out = []
    for pattern, key in CLAIMS:
        m = re.search(pattern, doc)
        if not m:
            out.append(("unchecked", f"no claim matching /{pattern}/ for "
                                      f"'{key}' (now {f[key]})"))
            continue
        raw = m.group(1)
        if raw is None:                       # the spelled-out alternative
            word = m.group(0).split()[0].strip("*").lower()
            got = WORDS.get(word)
            if got is None:
                out.append(("unchecked", f"'{key}' written as a word this tool "
                                         f"does not know: {m.group(0)!r}"))
                continue
        else:
            got = int(raw)
        if got != f[key]:
            out.append(("error", f"document says {got} {key}, netlist has {f[key]}"))
    return out


BOM_ROW = re.compile(r"^\|\s*([A-Z]+\d+(?:[,-][A-Z]*\d+)*)\s*\|([^|]*)\|([^|]*)\|")
CNUM = re.compile(r"\bC\d{3,9}\b")


def expand(ref):
    for part in ref.split(","):
        if "-" in part:
            a, b = part.split("-")
            m = re.match(r"([A-Z]+)(\d+)", a)
            n = re.match(r"([A-Z]*)(\d+)", b)
            if not (m and n):
                continue
            for i in range(int(m.group(2)), int(n.group(2)) + 1):
                yield f"{m.group(1)}{i}"
        else:
            yield part.strip()


def check_bom(doc, desig):
    out, claimed = [], {}
    for line in doc.splitlines():
        m = BOM_ROW.match(line)
        if not m:
            continue
        c = CNUM.search(m.group(2) + " " + m.group(3))
        if not c:
            continue
        for d in expand(m.group(1)):
            claimed.setdefault(d, c.group(0))
    for d, lcsc in sorted(claimed.items()):
        if d not in desig:
            out.append(("error", f"{d} has a BOM row ({lcsc}) and is not in the netlist"))
        elif desig[d] != lcsc:
            out.append(("error", f"{d}: BOM says {lcsc}, netlist places {desig[d]}"))
    for d in sorted(set(desig) - set(claimed)):
        out.append(("error", f"{d} ({desig[d]}) is in the netlist with no BOM row "
                             f"naming its C-number"))
    return out


PRICE_ROW = re.compile(r"^\|\s*([A-Z]+\d+(?:[,-][A-Z]*\d+)*)\s*\|.*\|\s*([\d.]+)\s*\|\s*$")


def check_prices(doc):
    out = []
    for line in doc.splitlines():
        if not line.startswith("|"):
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if len(cells) != 6 or not re.fullmatch(r"[\d.]+", cells[5]):
            continue
        c = CNUM.search(cells[2])
        if not c:
            continue
        f = PARTS / f"{c.group(0)}.json"
        if not f.exists():
            continue
        price = json.loads(f.read_text(encoding="utf-8")).get("priceUsd")
        if price is None:
            continue
        want = f"{price:.4f}".rstrip("0").rstrip(".")
        if cells[5] != want:
            out.append(("error", f"{cells[0]}: price column says {cells[5]}, "
                                 f"{c.group(0)}'s record says {want}"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design", help="a designs/<name>/ directory")
    args = ap.parse_args()
    design = pathlib.Path(args.design)
    if not (design / "netlist.json").exists():
        sys.exit(f"{design}/netlist.json not found")

    net, doc = load(design)
    f, desig = facts(net)
    findings = check_counts(doc, f) + check_bom(doc, desig) + check_prices(doc)

    for sev, msg in findings:
        print(f"  {'ERROR' if sev == 'error' else 'unchecked'}  {msg}")
    errors = sum(1 for s, _ in findings if s == "error")
    unchecked = len(findings) - errors
    print(f"\n{design}: {errors} mismatch(es), {unchecked} unchecked claim(s)")
    if not findings:
        print("  every stated count, C-number and price matches the netlist")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
