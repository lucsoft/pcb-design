#!/usr/bin/env python3
"""Check a design document against the artefacts it describes.

    ./tools/consistency.py designs/led-matrix-controller

`erc.py` checks the netlist and `stale.py` finds a value you changed in one
place. Neither catches the failure that has dominated this project's reviews:
a document stating a *count* of something in the netlist, the count changing,
and the prose not following. Nine consecutive review rounds found an instance.
Those numbers are derivable, so they should not be maintained by hand.

What it checks, all against `netlist.json` and `kb/`:

  component / pin / net / NC-pin counts the prose states, at EVERY occurrence
  resistor and capacitor counts, and the passive total
  every BOM row's C-number, read from the LCSC column, against the netlist's
  every designator in one and not the other
  the Qty column against the reference range's cardinality
  the price column against each kb record's priceUsd
  the "deliberately open pins" table, set-differenced both ways against the
    netlist's NC_* pins -- it is expanded, not counted

BOM tables are read by their HEADERS, so a table without a Qty or $ column is
not expected to have one. A table that has a Ref column and C-numbers but no
readable LCSC heading is reported rather than skipped: that is the shape where
renaming one heading switches every row's checks off.

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
    nc = [n for n in all_nets if n.upper().startswith("NC_")]
    real = {n for n in all_nets if not n.upper().startswith("NC_")}
    desig = {c["props"]["Designator"]: c["props"]["Supplier Part"]
             for c in net.values()}
    kind = lambda p: sum(1 for d in desig if re.fullmatch(p, d))
    return {
        "components": len(net),
        "pins": pins,
        "real nets": len(real),
        "nets erc.py counts": len(real) + len(set(nc)),
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
    (r"`erc\.py` prints (\d+)", "nets erc.py counts"),
    (r"\*\*(?:Thirty|(\d+)) pins, and this list is all of them\*\*", "NC pins"),
    (r"\*\*Resistors\*\* — (\d+) parts", "resistors"),
    (r"\*\*Capacitors\*\* — (\d+) parts", "capacitors"),
    (r"\| (\d+) passives \|", "passives"),
]

WORDS = {"thirty": 30, "twenty": 20, "ten": 10}


def check_counts(doc, f):
    """Returns (findings, how many claim occurrences were actually read)."""
    """EVERY occurrence of each claim, not the first.

    The failure this tool exists for is a value corrected where it is derived
    and left standing somewhere else, so stopping at the first match would miss
    precisely the second copy.
    """
    out, total = [], 0
    for pattern, key in CLAIMS:
        seen = 0
        for m in re.finditer(pattern, doc):
            seen += 1
            raw = m.group(1)
            if raw is None:                   # the spelled-out alternative
                word = m.group(0).split()[0].strip("*").lower()
                got = WORDS.get(word)
                if got is None:
                    out.append(("unchecked", f"'{key}' written as a word this "
                                             f"tool does not know: {m.group(0)!r}"))
                    continue
            else:
                got = int(raw)
            if got != f[key]:
                out.append(("error", f"document says {got} {key} "
                                     f"(occurrence {seen}), netlist has {f[key]}"))
        if not seen:
            out.append(("unchecked", f"no claim matching /{pattern}/ for "
                                     f"'{key}' (now {f[key]})"))
        total += seen
    return out, total


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


UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
         "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
VALUE = re.compile(r"([\d.]+)\s*([pnuµmkKMG]?)\s*(F|Ω|R|H)\b", re.I)
PKG = re.compile(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b")
DIEL = re.compile(r"\b(C0G|NP0|X[5-8][RS]|Y5V|Z5U)\b", re.I)


def magnitude(text):
    m = VALUE.search(text)
    if not m:
        return None
    return float(m.group(1)) * UNITS.get(m.group(2), 1.0)


def check_values(doc):
    """The Value column against the kb record's own parameters.

    This column was checked by nothing, and that is how C7 carried the wrong
    dielectric through eleven review rounds: the C-number was right, so every
    other check passed, and the row said C0G over a part LCSC calls X7R. The
    data to catch it is in `parameters` -- Capacitance/Resistance/Inductance,
    the package, and the temperature coefficient.
    """
    out, checked = [], 0
    for header, rows in tables(doc):
        iref, ilcsc = col(header, "ref"), col(header, "lcsc")
        ival = col(header, "value")
        if iref is None or ilcsc is None or ival is None:
            # Same silent-open shape check_rows was fixed for, and reintroduced
            # here by the commit that fixed it there: renaming "Value" to "Val"
            # switched the whole check off and the only trace was a zero in the
            # summary.
            if ilcsc is not None and any(CNUM.search(c) for row in rows for c in row):
                missing = " and ".join(
                    n for n, i in (("Ref", iref), ("Value", ival)) if i is None)
                out.append(("unchecked",
                            f"a table of {len(rows)} part row(s) has no "
                            f"{missing} column — headings are {header!r}, so "
                            f"no value in it is checked"))
            continue
        for cells in rows:
            if len(cells) != len(header):
                continue
            found = CNUM.findall(cells[ilcsc])
            if len(found) != 1:
                continue
            f = PARTS / f"{found[0]}.json"
            if not f.exists():
                continue
            rec = json.loads(f.read_text(encoding="utf-8"))
            params = rec.get("parameters") or {}
            said = cells[ival]
            ref = cells[iref].strip("* `")

            for key in ("Capacitance", "Resistance", "Inductance"):
                if key not in params:
                    continue
                want, got = magnitude(str(params[key])), magnitude(said)
                if want is None or got is None:
                    out.append(("unchecked", f"{ref}: cannot compare {key} "
                                             f"{params[key]!r} with {said!r}"))
                elif abs(want - got) > max(want, got) * 0.01:
                    out.append(("error", f"{ref}: value says {said!r}, "
                                         f"{found[0]}'s {key} is {params[key]}"))
                else:
                    checked += 1

            # Voltage rating: the nearest sibling of the dielectric error this
            # function was written for, and the more consequential one. A 25 V
            # part on a 36 V bus fails short.
            v_said = re.search(r"([\d.]+)\s*V\b", said)
            v_rec = re.search(r"([\d.]+)\s*V", str(params.get("Voltage Rating", "")))
            if v_said and v_rec:
                if abs(float(v_said.group(1)) - float(v_rec.group(1))) > 1e-6:
                    out.append(("error", f"{ref}: value says {v_said.group(1)} V, "
                                         f"{found[0]} is rated "
                                         f"{params['Voltage Rating']}"))
                else:
                    checked += 1

            pkg_said, pkg_rec = PKG.search(said), PKG.search(str(rec.get("package", "")))
            if pkg_said and pkg_rec and pkg_said.group(1) != pkg_rec.group(1):
                out.append(("error", f"{ref}: value says package {pkg_said.group(1)}, "
                                     f"{found[0]} is {pkg_rec.group(1)}"))

            d_said = DIEL.search(said)
            d_rec = DIEL.search(str(params.get("Temperature Coefficient", "")))
            if d_said and d_rec:
                # NP0 and C0G are the same dielectric under two names.
                norm = lambda x: "C0G" if x.upper() in ("C0G", "NP0") else x.upper()
                if norm(d_said.group(1)) != norm(d_rec.group(1)):
                    out.append(("error", f"{ref}: value says {d_said.group(1)}, "
                                         f"{found[0]} is "
                                         f"{params['Temperature Coefficient']}"))
    return out, checked


def check_bom(doc, desig):
    """Designator -> C-number, read from the LCSC COLUMN.

    Scanning the whole row for the first C-number let a stray code in the Part
    column mask a wrong one in the LCSC column -- and this is the identity
    check, the one CLAUDE.md's first standing rule is about: a wrong C-number
    silently places a different component with nothing in the property table to
    correct it afterwards.
    """
    out, claimed = [], {}
    for header, rows in tables(doc):
        iref, ilcsc = col(header, "ref"), col(header, "lcsc")
        if iref is None or ilcsc is None:
            continue
        for cells in rows:
            if len(cells) != len(header):
                continue                       # reported by check_rows
            ref = cells[iref].strip("* `")
            if not re.fullmatch(r"[A-Z]+\d+(?:[,-][A-Z]*\d+)*", ref):
                continue
            found = CNUM.findall(cells[ilcsc])
            if len(found) != 1:
                continue                       # reported by check_rows
            for d in expand(ref):
                if d in claimed and claimed[d] != found[0]:
                    out.append(("error", f"{d}: two BOM rows disagree — "
                                         f"{claimed[d]} and {found[0]}"))
                claimed.setdefault(d, found[0])
    for d, lcsc in sorted(claimed.items()):
        if d not in desig:
            out.append(("error", f"{d} has a BOM row ({lcsc}) and is not in the netlist"))
        elif desig[d] != lcsc:
            out.append(("error", f"{d}: BOM says {lcsc}, netlist places {desig[d]}"))
    for d in sorted(set(desig) - set(claimed)):
        out.append(("error", f"{d} ({desig[d]}) is in the netlist with no BOM row "
                             f"naming its C-number"))
    return out


def tables(doc):
    """Yield (header cells, [row cells]) for every markdown table in the doc.

    Header-aware on purpose. The passive tables carry Ref/Value/LCSC/Role and
    no Qty or price at all, so checking for those columns per row produced a
    hundred "unchecked" lines about columns that are not supposed to exist --
    noise that buries the few that matter.
    """
    header, rows = None, []
    for line in doc.splitlines():
        if not line.startswith("|"):
            if header:
                yield header, rows
            header, rows = None, []
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):      # the separator row
            continue
        if header is None:
            header, rows = cells, []
        else:
            rows.append(cells)
    if header:
        yield header, rows


def col(header, *names):
    for i, h in enumerate(header):
        if h.strip("* ").lower() in names:
            return i
    return None


def check_rows(doc):
    """Qty and price per BOM row, and every row it cannot read says so.

    This used to drop an unreadable row on a bare `continue`, which made it
    fail SILENTLY OPEN: adding one column to the table switched price checking
    off for every row while the summary still said prices matched. That is the
    exact failure the tool exists to catch, so a row that should be checkable
    and is not now produces an `unchecked` line.
    """
    out, priced, qtyd = [], 0, 0
    for header, rows in tables(doc):
        iref = col(header, "ref")
        ilcsc = col(header, "lcsc")
        iqty = col(header, "qty")
        icost = col(header, "$", "price")
        if iref is None or ilcsc is None:
            # A table of designator-shaped rows whose Ref or LCSC heading this
            # tool cannot find is the silent-skip case in a different costume:
            # rename "LCSC" to "LCSC #" and every row's price and C-number stop
            # being checked, with the summary reading cleaner than the truth.
            # Complain only about a table that is unambiguously a BOM: it has
            # a Ref column AND carries C-numbers. That is exactly the dangerous
            # case -- an LCSC heading renamed, so every row stops being checked
            # while the summary reads cleaner than before. A prose table that
            # happens to cite a C-number is not one, and reporting it is noise
            # that buries the case that matters.
            # A BOM is a table carrying C-numbers in designator-shaped rows.
            # Keying this on `iref is not None` made the "no Ref column" half
            # of the message unreachable: renaming Ref to Designator produced
            # 47 errors blaming the NETLIST instead of one line naming the
            # heading, which is the wrong file to send a reader to.
            has_cnums = any(CNUM.search(c) for row in rows for c in row)
            looks_like_bom = has_cnums and (
                iref is not None
                or any(re.fullmatch(r"[A-Z]+\d+(?:[,-][A-Z]*\d+)*", c.strip("* `"))
                       for row in rows for c in row[:1]))
            if looks_like_bom:
                missing = " and ".join(
                    n for n, i in (("Ref", iref), ("LCSC", ilcsc)) if i is None)
                out.append(("unchecked",
                            f"a table of {len(rows)} part row(s) has no "
                            f"{missing} column — headings are {header!r}, so "
                            f"none of its rows are checked"))
            continue
        for cells in rows:
            if len(cells) != len(header):
                out.append(("unchecked", f"row {cells[0]!r} has {len(cells)} "
                                         f"cells against {len(header)} headings"))
                continue
            ref = cells[iref].strip("* `")
            if not re.fullmatch(r"[A-Z]+\d+(?:[,-][A-Z]*\d+)*", ref):
                continue
            # Exactly one C-number in the LCSC cell. Searching the whole row
            # would let a stray code in the Part column mask a wrong one here;
            # requiring the cell to BE a C-number skipped rows like
            # "C134462, DO NOT FIT" that carry a real number plus a note.
            found = CNUM.findall(cells[ilcsc])
            code = found[0] if len(found) == 1 else cells[ilcsc].strip("* `")
            refs = list(expand(ref))

            if iqty is not None:
                q = cells[iqty].strip("* ")
                if not re.fullmatch(r"\d+", q):
                    out.append(("unchecked", f"{ref}: Qty cell {q!r} is not a number"))
                elif int(q) != len(refs):
                    out.append(("error", f"{ref}: Qty says {q}, the reference "
                                         f"range covers {len(refs)}"))
                else:
                    qtyd += 1

            if icost is None:
                continue
            cost = cells[icost].strip("* ")
            if not CNUM.fullmatch(code):
                out.append(("unchecked", f"{ref}: LCSC cell {code!r} is not a "
                                         f"C-number, so its price is unchecked"))
                continue
            f = PARTS / f"{code}.json"
            if not f.exists():
                out.append(("unchecked", f"{ref}: {code} has no kb record, so "
                                         f"its price cell is unchecked"))
                continue
            price = json.loads(f.read_text(encoding="utf-8")).get("priceUsd")
            if price is None:
                out.append(("unchecked", f"{ref}: {code}'s record carries no "
                                         f"priceUsd"))
            elif not re.fullmatch(r"[\d.]+", cost):
                out.append(("unchecked", f"{ref}: price cell {cost!r} is not a "
                                         f"number ({code} is {price:.4f})"))
            elif abs(float(cost) - price) > 5e-5:
                out.append(("error", f"{ref}: price column says {cost}, "
                                     f"{code}'s record says {price:.4f}"))
            else:
                priced += 1
    return out, priced, qtyd


OPEN_PIN_ROW = re.compile(r"^\|\s*((?:[A-Z]+\d+[./]\s*[\w,.\s/-]*?))\s*\|")


def check_open_pins(doc, net):
    """The "Deliberately open pins" table against the netlist's NC_* pins.

    The README says this table "is all of them". The tool used to check only
    the word "Thirty" against a count, which is not the same claim: deleting a
    row left the count right and the table wrong.
    """
    want = {f"{c['props']['Designator']}.{pin}"
            for c in net.values()
            for pin, n in c["pins"].items() if n.upper().startswith("NC_")}
    block = doc.split("### Deliberately open pins")
    if len(block) != 2:
        return [("unchecked", "no 'Deliberately open pins' section found, so "
                              f"the {len(want)} open pins are unlisted")]
    block = block[1].split("###")[0]
    # Table rows only. Reading the whole section let a prose sentence naming a
    # pin stand in for the table row that was deleted.
    block = "\n".join(l for l in block.splitlines() if l.startswith("|"))
    listed = set()
    for ref in re.findall(r"\b([A-Z]+\d+)\s*\.\s*([A-Z]?\d+)", block):
        listed.add(f"{ref[0]}.{ref[1]}")
    # The table also writes ranges like "U9.11-17" and shared forms like
    # "U10/U11 .12". Expand both rather than pretend they are not there.
    for d, lo, hi in re.findall(r"\b([A-Z]+\d+)\.(\d+)-(\d+)\b", block):
        for i in range(int(lo), int(hi) + 1):
            listed.add(f"{d}.{i}")
    for a, b, pins in re.findall(r"\b([A-Z]+\d+)/([A-Z]+\d+)\s*\.([\d,.\s]+)", block):
        for n in re.findall(r"\d+", pins):
            listed.add(f"{a}.{n}")
            listed.add(f"{b}.{n}")
    missing = sorted(want - listed)
    extra = sorted(listed - want)
    out = []
    if missing:
        out.append(("error", f"open-pin table omits {len(missing)}: "
                             f"{', '.join(missing)}"))
    if extra:
        out.append(("error", f"open-pin table lists {len(extra)} pin(s) that are "
                             f"not open in the netlist: {', '.join(extra)}"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design", help="a designs/<name>/ directory")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on an unchecked claim as well as a mismatch")
    args = ap.parse_args()
    design = pathlib.Path(args.design)
    for needed in ("netlist.json", "README.md"):
        if not (design / needed).exists():
            sys.exit(f"{design}/{needed} not found — this tool compares a "
                     f"document against the netlist it describes, so it needs "
                     f"both")

    net, doc = load(design)
    f, desig = facts(net)
    rows, priced, qtyd = check_rows(doc)
    counts, counted = check_counts(doc, f)
    values, valued = check_values(doc)
    findings = (counts + check_bom(doc, desig) + rows + values
                + check_open_pins(doc, net))

    for sev, msg in findings:
        print(f"  {'ERROR' if sev == 'error' else 'unchecked'}  {msg}")
    errors = sum(1 for s, _ in findings if s == "error")
    unchecked = len(findings) - errors
    print(f"\n{design}: {errors} mismatch(es), {unchecked} unchecked claim(s)")
    # Say what was checked, not that everything was. The previous wording
    # printed "every price matches" over prices it had silently skipped.
    print(f"  {counted} count claim(s), {len(desig)} designator(s), "
          f"{priced} price cell(s), {qtyd} Qty cell(s), {valued} value "
          f"magnitude(s), and the open-pin table")
    return 1 if errors or (args.strict and unchecked) else 0


if __name__ == "__main__":
    sys.exit(main())
