#!/usr/bin/env python3
"""Put real ground and supply symbols on a schematic, instead of text.

The netlist importer labels every pin with its net name. On a board whose
GND has 124 pins that means 124 pieces of floating text, and a reader does
not see a ground connection — they see a word. The convention is a ground
symbol: three shortening bars under the pin.

A flag symbol is **not** something to draw. It is a placed component, like
any other, pointing at a `SYMBOL` and a `DEVICE` document:

    COMPONENT  partId pid8a0e77bacb214e, at the pin's coordinate
    ATTR       Symbol  -> the SYMBOL document's uuid
    ATTR       Device  -> the DEVICE document's uuid
    ATTR       Name    -> "GND"

So this copies those two documents out of a file that already has them —
the hand-drawn module reference does — and places an instance on every pin
of the chosen nets. Nothing is invented; the geometry is the one EasyEDA
itself put in the donor.

    netflags.py <target.epro2> --donor <hand-drawn.epro2> -o out.epro2
    netflags.py <target.epro2> --donor <hand-drawn.epro2> --dry-run

**Flags carry no Designator and no Supplier Part**, which is what keeps them
out of the BOM and out of `epro.py diff`'s part check. They add no wire —
and `--ground-bus` adds one only without a `NET` attribute, which is what
keeps the net diff's pin connections untouched either way. Both are checked
after writing rather than assumed.

A flag's own pin lands exactly on the pin it flags, so it connects the way
every other coincidence in this format does. That also means placing one
where the net is wrong would be a short — so the net is read off the stub
already at that coordinate, never guessed.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import sys
import zipfile
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from schwire import bodies as schwire_bodies          # noqa: E402

FLAG_PART = "pid8a0e77bacb214e"


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru in {path.name}, "
                         f"found {len(eprus)}")
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


def device_netname(rows, uuid):
    """The `Global Net Name` a DEVICE document hands to its instances."""
    for h, b, doc, _ in rows:
        if h and h["type"] == "META" and doc and doc[0] == "DEVICE" \
                and doc[1] == uuid:
            return (b.get("attributes") or {}).get("Global Net Name")
    return None


def find_flags(rows):
    """name -> (symbol uuid, device uuid) for every flag the donor places."""
    name = {b["parentId"]: b["value"] for h, b, _, _ in rows
            if h and h["type"] == "ATTR" and b.get("key") == "Name"
            and b.get("value")}
    sym = {b["parentId"]: b["value"] for h, b, _, _ in rows
           if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
           and b.get("value")}
    dev = {b["parentId"]: b["value"] for h, b, _, _ in rows
           if h and h["type"] == "ATTR" and b.get("key") == "Device"
           and b.get("value")}
    out = {}
    for h, b, _, _ in rows:
        if (h and h["type"] == "COMPONENT" and b.get("partId") == FLAG_PART
                and h["id"] in name and h["id"] in sym and h["id"] in dev):
            out.setdefault(name[h["id"]], (sym[h["id"]], dev[h["id"]]))
    return out


def document(rows, kind, uuid):
    """Every raw line of one embedded document, header included."""
    return [raw for h, b, doc, raw in rows
            if doc and doc[0] == kind and doc[1] == uuid]


ROT = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}


def pin_coords(rows):
    """Every component pin's canvas coordinate.

    Needed to tell a stub's two ends apart. A stub runs from the component
    pin to a free end, and the ground symbol belongs on the free end --
    putting one on each end would place 241 flags for 124 pins and stack a
    symbol on top of every component lead.
    """
    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    sp = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and h["type"] == "PIN" and doc and doc[0] == "SYMBOL":
            sp[doc[1]].append(b)
    out = {}
    for h, b, _, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig):
            continue
        r = (b.get("rotation") or 0) % 360
        if r not in ROT:
            continue
        for q in sp.get(symref.get(h["id"]), []):
            lx = -q.get("x", 0) if b.get("isMirror") else q.get("x", 0)
            dx, dy = ROT[r](lx, q.get("y", 0))
            out[(round(b["x"] + dx), round(b["y"] + dy))] = desig[h["id"]]
    return out


def stub_nets(rows):
    """(x, y) -> {(net, wire id)} for every stub ending there.

    The wire id travels with the net because hiding a name has to be tied
    to the wire the flag landed on. Matching on nearby coordinates instead
    hid 296 names where 233 flags were placed -- the extra 63 were labels
    on pins with no flag, which would have deleted information rather than
    replaced it.
    """
    netof = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "NET"
             and b.get("value")}
    at = {}
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof:
            n = netof[b["lineGroup"]]
            for x, y in ((b["startX"], b["startY"]), (b["endX"], b["endY"])):
                at.setdefault((round(x), round(y)), set()).add((n, b["lineGroup"]))
    return at


R4Q = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
       180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}


def flag_hits(tip, ext, bx):
    """Would a symbol of this extent land inside bx *whichever way up*?

    Both ways, not either: the flag is turned to suit its lead, so one
    clear orientation is enough and asking for both refuses positions the
    drawing would have been happy with. Asking for either cost the pad
    ten units of lead -- the way that reaches back into the package is the
    one the turn rule exists to avoid.

    Module level, not a closure: the lead re-aim runs before the rest of
    `main` has defined anything, and it is what asks this.
    """
    if ext is None:
        return False
    for r in (0, 180):
        cs = [R4Q[r](x, y) for x, y in
              ((ext[0], ext[1]), (ext[2], ext[1]),
               (ext[0], ext[3]), (ext[2], ext[3]))]
        x0 = tip[0] + min(x for x, _ in cs)
        x1 = tip[0] + max(x for x, _ in cs)
        y0 = tip[1] + min(y for _, y in cs)
        y1 = tip[1] + max(y for _, y in cs)
        if not (x0 < bx[2] and bx[0] < x1 and y0 < bx[3] and bx[1] < y1):
            return False
    return True


def symbol_extent(rows, uuid):
    """A symbol's drawn extent, relative to its own anchor.

    Only the graphic primitives -- RECT, POLY, ARC -- because that is
    what a reader sees as the part. Returns None when the symbol draws
    nothing, which is a third answer and not a synonym for "it is a
    point": treating it as zero-sized would quietly pass every overlap
    test it should have failed.
    """
    pts = []
    for h, b, doc, _ in rows:
        if not h or not doc or doc[0] != "SYMBOL" or doc[1] != uuid:
            continue
        if h["type"] == "RECT":
            pts += [(b.get("dotX1"), b.get("dotY1")),
                    (b.get("dotX2"), b.get("dotY2"))]
        elif h["type"] == "ARC":
            pts += [(b.get("startX"), b.get("startY")),
                    (b.get("endX"), b.get("endY"))]
        elif h["type"] == "POLY" and b.get("points"):
            pts += [(q.get("x"), q.get("y")) for q in b["points"]]
    pts = [(x, y) for x, y in pts
           if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if not pts:
        return None
    return (min(x for x, _ in pts), min(y for _, y in pts),
            max(x for x, _ in pts), max(y for _, y in pts))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--donor", type=Path, required=True,
                    help="a schematic that already places the flag symbols")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--nets", default="GND",
                    help="comma separated. `NET` uses the donor flag of the "
                         "same name; `NET=FLAG` borrows another flag's "
                         "symbol and labels it NET -- which is how LED_RTN "
                         "gets a ground symbol and BUS_5V a supply one "
                         "without the donor having to know those names "
                         "(default GND)")
    ap.add_argument("--keep-text", action="store_true",
                    help="leave the net name visible on a pin that now has "
                         "a flag (default: hide it)")
    ap.add_argument("--ground-every", action="store_true",
                    help="a ground symbol on every ground pin, not one "
                         "per part; a rail net ending in text on one pin "
                         "and a symbol on the next is a reader's problem")
    ap.add_argument("--flag-lead", type=int, default=0,
                    help="carry a crowded flag this many units further out "
                         "along its own stub, extending the stub, instead "
                         "of dropping it or drawing it across a neighbour")
    ap.add_argument("--lead-turn", default="",
                    help="comma-separated nets whose crowded flag is led "
                         "across its stub rather than along it. The straight "
                         "lead is shorter but runs in the row beside the "
                         "part's other pins, where it walls off the space a "
                         "neighbouring net needs to get past; a turn leaves "
                         "the row. Both perpendicular sides are tried and "
                         "the first that clears wins, the down side first")
    ap.add_argument("--pin-stub", default="",
                    help="semicolon-separated NET@x,y naming pins whose lead "
                         "is moved, optionally as NET@x,y=length. Without a "
                         "length the lead is re-aimed square off the pin: the "
                         "importer aims a lead by the pin's side, which is "
                         "right for the pins along an edge and wrong for one "
                         "on it, and U4's thermal pad is a vertical pin on the "
                         "top edge whose lead came out sideways, thirty units "
                         "along the package. With a length the lead keeps its "
                         "direction and is cut to that many units, which is "
                         "what a part lying *across* a row needs: thirty units "
                         "each end make a hundred-unit footprint, and nothing "
                         "can come within a hundred of it. The lead is moved "
                         "in both forms and never dropped -- the node's NET "
                         "attribute lives on its wire, and epro.py diff counts "
                         "one of those per pin")
    ap.add_argument("--flag-clearance", type=int, default=0,
                    help="with --strict-flags, drop a flag whose root has a "
                         "foreign stub end within this many units; the "
                         "symbol cannot be attributed by eye at that pitch")
    ap.add_argument("--strict-flags", action="store_true",
                    help="also drop a flag whose symbol would be drawn "
                         "across a wire carrying another net; the pin keeps "
                         "its net name, which is what names the net")
    ap.add_argument("--one-per-node", type=int, default=0,
                    help="one flag per node rather than per part, grouping "
                         "a rail's pins within this many units; a net drawn "
                         "as a wire needs one name, not one at every pin")
    ap.add_argument("--one-per-part", action="store_true",
                    help="one flag per component and net, not per pin. A "
                         "ten-pin ground on one IC otherwise stacks ten "
                         "symbols on the pin pitch; the pins that lose "
                         "their flag keep their net label")
    ap.add_argument("--ground-bus", type=int, default=0,
                    help="join a ground net's free ends that sit in one "
                         "line, consecutive gaps up to this many units, with "
                         "a bus wire and one ground symbol; a rail's parts "
                         "that each drop their lead to the same height "
                         "otherwise repeat the symbol once per part")
    ap.add_argument("--rotation", type=int, default=0,
                    help="rotation added to every placed flag, on top of "
                         "the turn each one may need to keep its own lead "
                         "off the bar it draws (default 0)")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    for q in (a.target, a.donor):
        if not q.exists():
            print(f"error: {q} not found", file=sys.stderr)
            return 2

    _, drows = load(a.donor)
    flags = find_flags(drows)
    name, rows = load(a.target)
    # --- a stub that runs along the symbol rather than out of it ---------
    # `--pin-stub` names a pin whose lead the importer aimed along the
    # package instead of away from it. The lead is *moved*, never dropped:
    # the node's NET attribute lives on that wire and `epro.py diff`
    # counts one of those per pin, so deleting the record would break the
    # count where moving it breaks nothing. Everything downstream -- the
    # label, the flag, the router -- then sees the lead where a person
    # would have drawn it.
    restub = {}
    if a.pin_stub:
        _pc = pin_coords(rows)
        _anch = {}
        for _h, _b, _, _ in rows:
            if _h and _h["type"] == "COMPONENT" and _b.get("partId") \
                    and isinstance(_b.get("x"), (int, float)):
                _anch[_h["id"]] = (_b["x"], _b["y"])
        _at_of = {}
        for _h, _b, _, _ in rows:
            if _h and _h["type"] == "ATTR" and _b.get("key") == "Designator" \
                    and _b.get("parentId") in _anch:
                _at_of[_b["value"]] = _anch[_b["parentId"]]
        # The boxes a symbol has to keep out of, and the extent of the
        # flag that will sit on the new end -- both readable from the raw
        # records, so this runs before anything else builds them.
        _symref = {_b["parentId"]: _b["value"] for _h, _b, _, _ in rows
                   if _h and _h["type"] == "ATTR"
                   and _b.get("key") == "Symbol" and _b.get("value")}
        _boxes = []
        for _h, _b, _d, _ in rows:
            if not _h or _h["type"] != "COMPONENT" or not _b.get("partId"):
                continue
            _e = symbol_extent(rows, _symref.get(_h["id"]))
            if _e is None:
                continue
            _r = (_b.get("rotation") or 0) % 360
            if _r not in R4Q:
                continue
            _cs = [R4Q[_r](x, y) for x, y in
                   ((_e[0], _e[1]), (_e[2], _e[1]),
                    (_e[0], _e[3]), (_e[2], _e[3]))]
            _boxes.append((_b["x"] + min(x for x, _ in _cs),
                           _b["y"] + min(y for _, y in _cs),
                           _b["x"] + max(x for x, _ in _cs),
                           _b["y"] + max(y for _, y in _cs)))
        # A spec with no `@` names a *designator*, and shortens every lead
        # that part owns. That is the form to use for a part lying across a
        # row: its leads are short because of what it is, not because of
        # where it happens to be, and a coordinate would have to be
        # rewritten every time the part moved -- which is the coupling that
        # makes placing these by hand tedious in the first place.
        _specs = []
        for _spec in (a.pin_stub or "").split(";"):
            _spec = _spec.strip()
            if not _spec:
                continue
            if "@" in _spec:
                _specs.append((_spec, None))
                continue
            _d, _, _ln = _spec.partition("=")
            for _h, _b, _dd, _ in rows:
                if _h and _h["type"] == "LINE" and _b.get("lineGroup"):
                    for _e in ((round(_b["startX"]), round(_b["startY"])),
                               (round(_b["endX"]), round(_b["endY"]))):
                        if _pc.get(_e) == _d.strip():
                            _specs.append((f"{_d.strip()}@{_e[0]},{_e[1]}"
                                           f"={_ln.strip() or 15}",
                                           _d.strip()))
        for _spec, _of in _specs:
            _spec = _spec.strip()
            if not _spec:
                continue
            _net, _, _co = _spec.partition("@")
            try:
                _q = tuple(int(v) for v in _co.split(","))
            except ValueError:
                _q = ()
            _own = _pc.get(_q) if len(_q) == 2 else None
            if _own is None:
                print(f"error: --pin-stub wants NET@x,y naming a pin, not "
                      f"{_spec!r}", file=sys.stderr)
                return 2
            _anchor = _at_of.get(_own, _q)
            _done = False
            for _i, (_h, _b, _d, _r) in enumerate(rows):
                if _done or not _h or _h["type"] != "LINE":
                    continue
                _e0 = (round(_b["startX"]), round(_b["startY"]))
                _e1 = (round(_b["endX"]), round(_b["endY"]))
                if _q not in (_e0, _e1):
                    continue
                _free = _e1 if _e0 == _q else _e0
                if _free in _pc:
                    continue                     # not a stub
                _n = abs(_free[0] - _q[0]) + abs(_free[1] - _q[1])
                if _free[1] == _q[1]:
                    _dirs = [(0, -1), (0, 1)]
                elif _free[0] == _q[0]:
                    _dirs = [(-1, 0), (1, 0)]
                else:
                    continue
                _fe = flags.get(_net) or next(iter(flags.values()), None)
                _ext = symbol_extent(drows, _fe[0]) if _fe else None
                # Away from the body first -- and no longer than the symbol
                # fits in. U4's pad has C5 fifty units above it, so a
                # thirty-unit lead puts the bars inside the capacitor
                # whichever way they are turned; twenty is clear.
                _dirs.sort(key=lambda d: (d[0] * (_q[0] - _anchor[0])
                                          + d[1] * (_q[1] - _anchor[1])),
                           reverse=True)
                _away = None
                for _d in _dirs:
                    for _ln in (5, 10, _n // 2, _n * 2 // 3, _n):
                        _t = (_q[0] + _d[0] * _ln, _q[1] + _d[1] * _ln)
                        if not any(flag_hits(_t, _ext, _bx) for _bx in _boxes):
                            _away = _t
                            break
                    if _away is not None:
                        break
                if _away is None:
                    continue
                _nb = dict(_b)
                if _e0 == _free:
                    _nb["startX"], _nb["startY"] = _away
                else:
                    _nb["endX"], _nb["endY"] = _away
                rows[_i] = (_h, _nb, _d,
                            f"{json.dumps(_h, separators=(',', ':'))}||"
                            f"{json.dumps(_nb, separators=(',', ':'))}|")
                restub[_h["id"]] = (_free, _away)
                _done = True
            if not _done:
                print(f"error: --pin-stub found no stub on the pin at {_q}",
                      file=sys.stderr)
                return 2
        print(f"  stub   {len(restub)} lead(s) re-aimed away from their "
              f"symbol")

    print(f"\n{a.donor.name} offers: {', '.join(sorted(flags)) or 'nothing'}")
    if not flags:
        print("  error: the donor places no flag components, so there is "
              "nothing to copy\n", file=sys.stderr)
        return 1

    at = stub_nets(rows)
    pins = pin_coords(rows)
    wanted, using = [], {}
    for spec in (s.strip() for s in a.nets.split(",") if s.strip()):
        net, _, flag = spec.partition("=")
        wanted.append(net)
        using[net] = flag or net
    missing = [n for n in wanted if using[n] not in flags]
    if missing:
        print(f"  error: the donor has no flag for "
              f"{', '.join(using[n] for n in missing)} — "
              f"it has {', '.join(sorted(flags))}\n", file=sys.stderr)
        return 1

    # --- documents to carry across ---------------------------------------
    have = {d[1] for _, _, d, _ in rows if d}
    carry = []
    for n in wanted:
        s, d = flags[using[n]]
        for kind, uuid in (("SYMBOL", s), ("DEVICE", d)):
            if uuid in have:
                continue
            lines = document(drows, kind, uuid)
            if not lines:
                print(f"  error: the donor references {kind} {uuid[:12]} for "
                      f"{n} but does not contain it\n", file=sys.stderr)
                return 1
            carry.append((kind, uuid, lines))
            have.add(uuid)
    for kind, uuid, lines in carry:
        print(f"  copy   {kind} {uuid[:12]}  ({len(lines)} records)")

    # --- where to place them ----------------------------------------------
    spots = collections.Counter()
    place, onwire = [], set()
    # Which component each stub reaches, so --one-per-part can keep one
    # flag where a part has several pins on one rail. U9 has ten GND
    # pins and got ten ground symbols stacked on its pin pitch, their
    # bars overlapping into one black ladder -- the ugliest thing on the
    # sheet once the wires were sorted out.
    stub_owner = {}
    for h, b, _, _ in rows:
        if not h or h["type"] != "LINE" or not b.get("lineGroup"):
            continue
        a_ = (round(b["startX"]), round(b["startY"]))
        b_ = (round(b["endX"]), round(b["endY"]))
        if a_ in pins and b_ not in pins:
            stub_owner[b_] = pins[a_]
        elif b_ in pins and a_ not in pins:
            stub_owner[a_] = pins[b_]

    # Which stub ends lie inside a symbol body. A ground triangle drawn
    # there is unreadable and reads as a fault: U4's thermal pad sits at
    # symbol-local (0, -30), inside its own outline, so --one-per-part
    # picking the first candidate in coordinate order put the flag in the
    # middle of the IC and left the pin that actually faces outward
    # carrying a bare name -- which the switch-node wire then ran across.
    # Preferring a stub end that is outside every body fixes both at once.
    # What the flag actually draws, not where its root sits. U4's thermal
    # pad stub ends just *above* the package outline, so a root-only test
    # called it clear -- and the ground triangle, which hangs below its
    # root, landed in the middle of the IC anyway. The first version of
    # this check tested the root and changed nothing.
    symref_t = {b["parentId"]: b["value"] for h, b, _, _ in rows
                if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
                and b.get("value")}
    partbox = []
    for h, b, doc, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or doc is None or doc[0] not in ("SCH", "SCH_PAGE")):
            continue
        ext = symbol_extent(rows, symref_t.get(h["id"]))
        if ext is None:
            continue
        r_ = (b.get("rotation") or 0) % 360
        if r_ not in R4Q:
            continue
        cs = [R4Q[r_](x, y) for x, y in
              ((ext[0], ext[1]), (ext[2], ext[1]),
               (ext[0], ext[3]), (ext[2], ext[3]))]
        partbox.append((b["x"] + min(x for x, _ in cs),
                        b["y"] + min(y for _, y in cs),
                        b["x"] + max(x for x, _ in cs),
                        b["y"] + max(y for _, y in cs)))

    flagext = {}
    for n in wanted:
        e = symbol_extent(drows, flags[using[n]][0])
        if e is not None:
            flagext[n] = e

    # Every stub segment, with the net it carries, so a flag can be asked
    # whether its symbol would be drawn across somebody else's wire. A
    # reviewer reading this sheet took a VBUS flag whose bar crossed the
    # SW_BOOT stub to mean the 36 V input was unconnected and the
    # bootstrap node was a supply -- both wrong, and the worst misreading
    # the sheet offered. The flags are all electrically on their stubs;
    # it is only the drawing that says otherwise, which is precisely the
    # kind of fault no net diff can see.
    netname = {b["parentId"]: b["value"] for h, b, _, _ in rows
               if h and h["type"] == "ATTR" and b.get("key") == "NET"
               and b.get("value")}
    wireseg = []
    for h, b, doc, _ in rows:
        if (h and h["type"] == "LINE" and b.get("lineGroup")
                and doc and doc[0] in ("SCH", "SCH_PAGE")):
            wireseg.append((min(b["startX"], b["endX"]),
                            min(b["startY"], b["endY"]),
                            max(b["startX"], b["endX"]),
                            max(b["startY"], b["endY"]),
                            netname.get(b["lineGroup"])))

    def _gdir(n):
        """Which side of its own pin a flag's graphic is drawn on.

        Read off the drawing, not named per net: in the donor file GND's
        triangle lies at +y and the +5V bar at -y, so a table of net
        names would be one more thing to keep in step with the library.
        """
        e = flagext.get(n)
        if e is None:
            return None
        cx = (e[0] + e[2]) / 2.0
        cy = (e[1] + e[3]) / 2.0
        if abs(cx) >= abs(cy):
            return (1 if cx > 0 else -1, 0)
        return (0, 1 if cy > 0 else -1)

    def _turn(n, tip, prev):
        """0, or 180 where the graphic would point back along its lead.

        A flag reached from above is the case that needs it. The supply
        symbol draws its bar on the far side of its pin from the wire it
        feeds, so leading a flag *down* to sit below a pin row puts the
        bar squarely on the incoming wire -- the wire's last ten units
        stick out past it and the flag reads as a crossbar threaded on a
        line rather than a flag. Only that case is turned: a bar
        perpendicular to its wire is what every other flag here draws.
        """
        g = _gdir(n)
        if g is None:
            return 0
        if prev is None:
            return 0
        dx, dy = tip[0] - prev[0], tip[1] - prev[1]
        if dx == 0 and dy == 0:
            return 0
        if abs(dy) >= abs(dx):
            d = (0, 1 if dy > 0 else -1)
        else:
            d = (1 if dx > 0 else -1, 0)
        off = 180 if g == (-d[0], -d[1]) else 0
        # ...and the one other reason to turn: the symbol would be drawn
        # into a part and turning it over puts it clear. U4's thermal pad
        # is that case. Its stub runs *right* out of the pin row, so the
        # bars hang down into the package from a stub end that is itself
        # clear of it -- and every check that asks about the root says
        # the position is fine. Turned over they sit in the gap between
        # the pad and the bootstrap loop above it, which is where a
        # person draws them, and nothing had to move to get there.
        #
        if _turn_clears(tip, n, off):
            return (off + 180) % 360
        return off

    def _turn_clears(tip, n, off=0):
        """Would turning this flag over take it out of a part?

        Asked of the rotation it would otherwise take, not of 0: a flag
        already turned because its graphic points back along its lead is
        clear or not clear the other way round.
        """
        return (_in_part(tip, n, (a.rotation + off) % 360) and
                not _in_part(tip, n, (a.rotation + off + 180) % 360))

    def _in_part(tip, n, r):
        """Would n's symbol at rotation r, rooted at tip, land in a part?

        Written without `_flagbox`, which asks `_turn`, which asks this --
        a ring of three that would recurse.
        """
        e = flagext.get(n)
        if e is None or r not in R4Q:
            return False
        cs = [R4Q[r](x, y) for x, y in ((e[0], e[1]), (e[2], e[1]),
                                        (e[0], e[3]), (e[2], e[3]))]
        x0 = tip[0] + min(x for x, _ in cs)
        x1 = tip[0] + max(x for x, _ in cs)
        y0 = tip[1] + min(y for _, y in cs)
        y1 = tip[1] + max(y for _, y in cs)
        return any(x0 < bx[2] and bx[0] < x1 and y0 < bx[3] and bx[1] < y1
                   for bx in partbox)

    def _flagbox(pt, n, prev=None):
        e = flagext.get(n)
        if e is None:
            return None
        r = (a.rotation + _turn(n, pt, prev)) % 360
        if r not in R4Q:
            return None
        cs = [R4Q[r](x, y) for x, y in ((e[0], e[1]), (e[2], e[1]),
                                        (e[0], e[3]), (e[2], e[3]))]
        return (pt[0] + min(x for x, _ in cs), pt[1] + min(y for _, y in cs),
                pt[0] + max(x for x, _ in cs), pt[1] + max(y for _, y in cs))

    def _draws_into(pt, n, prev=None):
        """Would a flag for n, rooted at pt, be drawn over a part?"""
        f = _flagbox(pt, n, prev)
        if f is None:
            return False
        return any(f[0] < bx[2] and bx[0] < f[2]
                   and f[1] < bx[3] and bx[1] < f[3] for bx in partbox)

    def _draws_over_wire(pt, n, prev=None):
        """... or over a wire carrying some other net?"""
        f = _flagbox(pt, n, prev)
        if f is None:
            return False
        for x0, y0, x1, y1, wn in wireseg:
            if wn == n or wn is None:
                continue
            if f[0] < x1 and x0 < f[2] and f[1] < y1 and y0 < f[3]:
                return True
        return False

    # Points a *drawn* wire already reaches -- a line meeting them that is
    # not the stub they sit on. U4 grounds twice: its pin's stub end,
    # which the hand-drawn wire to the catch diode already covers, and the
    # thermal pad's, which nothing reaches. Ranking the pin's first put
    # the part's one flag there, and the rule against a second mark on a
    # wired node then took it away, so U4 ended up with no ground symbol
    # at all and the pad unmarked. A point a wire reaches does not need
    # the mark; a point it does not is the one that does.
    stubwid = {pt: {w for _, w in ns} for pt, ns in at.items()}
    wired_pt = set()
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("startX") is not None:
            for q in ((round(b["startX"]), round(b["startY"])),
                      (round(b["endX"]), round(b["endY"]))):
                if q in at and b.get("lineGroup") not in stubwid.get(q, set()):
                    wired_pt.add(q)

    inbody = {pt for pt in at
              if any(_draws_into(pt, n) for n, _ in at[pt] if n in wanted)}
    onwire_pt = {pt for pt in at
                 if any(_draws_over_wire(pt, n)
                        for n, _ in at[pt] if n in wanted)}

    # A flag symbol is 10 to 20 units across and the pins of an IC are
    # ten apart, so a flag on one pin of a dense row cannot be told from
    # a flag on its neighbour -- an experienced reader took U4's VBUS
    # flag for one on SW_BOOT and read the 36 V input as unconnected.
    # Every flag here is on the right net, measured; the drawing is what
    # is ambiguous, and no net check can see that. Where the row is too
    # tight for the symbol to be attributable, the pin keeps its name,
    # which says the same thing and cannot be misread by one pitch.
    crowded = set()
    if a.flag_clearance:
        ends = [pt for pt, nets in at.items() for n, _ in nets]
        for pt, nets in at.items():
            mine = {n for n, _ in nets}
            for q, qn in at.items():
                if q == pt or {n for n, _ in qn} & mine:
                    continue
                if (abs(q[0] - pt[0]) + abs(q[1] - pt[1])) <= a.flag_clearance:
                    crowded.add(pt)
                    break
    # Which proximity cluster each stub end belongs to, per net. A rail
    # that schwire will draw as a wire needs one flag for the node, not
    # one per pin: four +5V flags along a drawn 5 V rail say the same
    # thing four times, and one of them sat on a stub that then ended
    # free below the line and read as an open connection. The grouping
    # is the same single-linkage schwire uses for --local-rail, so the
    # two agree on what a node is.
    node_of = {}
    ground_nets = {n for n in wanted if using.get(n, n).upper() == "GND"}
    if a.one_per_node:
        bynet_ = collections.defaultdict(list)
        for pt_, nets_ in at.items():
            for n_, _w in nets_:
                if n_ in wanted and pt_ not in pins:
                    bynet_[n_].append(pt_)
        for n_, pts_ in bynet_.items():
            groups_ = []
            if n_ in ground_nets:
                # Ground is never drawn as a wire here -- it is the one
                # net that stays labels everywhere -- so collapsing it
                # to one symbol per node leaves most ground pins with
                # nothing. One flag for the whole sheet is what that
                # looked like.
                continue
            for q_ in sorted(pts_):
                for g_ in groups_:
                    if any(abs(q_[0] - r_[0]) + abs(q_[1] - r_[1])
                           <= a.one_per_node for r_ in g_):
                        g_.append(q_)
                        break
                else:
                    groups_.append([q_])
            merged_ = True
            while merged_:
                merged_ = False
                for i_ in range(len(groups_)):
                    for j_ in range(i_ + 1, len(groups_)):
                        if any(abs(u_[0] - v_[0]) + abs(u_[1] - v_[1])
                               <= a.one_per_node
                               for u_ in groups_[i_] for v_ in groups_[j_]):
                            groups_[i_] += groups_.pop(j_)
                            merged_ = True
                            break
                    if merged_:
                        break
            for k_, g_ in enumerate(groups_):
                for q_ in g_:
                    node_of[(n_, q_)] = k_

    best, why = {}, {}
    if a.one_per_part:
        for pt, nets in sorted(at.items()):
            for n, wid in sorted(nets):
                if n not in wanted or pt in pins:
                    continue
                owner_ = stub_owner.get(pt)
                if owner_ is None:
                    continue
                # Ground gets a symbol on every pin, not one per part.
                # The one-per-part rule exists because U9's ten ground
                # pins stacked into a black ladder -- but that was
                # before a crowded flag could be led out of the pin
                # field, and the ladder is exactly what a lead
                # separates. A rail net that ends in text on one pin and
                # a symbol on the next is a reader's problem, not a
                # tidiness question: U4's thermal pad read as the one
                # ground on the sheet that might mean something else.
                if a.one_per_node and (n, pt) in node_of:
                    key = ("node", n, node_of[(n, pt)])
                else:
                    key = (owner_, n,
                           pt if (a.ground_every and n in ground_nets)
                           else None)
                # The sort key and the three questions the skip rules
                # ask, kept apart on purpose: reading them back out of
                # `rank` by position survived exactly as long as it took
                # to insert a component in front of them.
                why[pt] = (pt in inbody, pt in onwire_pt, pt in crowded)
                rank = (pt in wired_pt,) + why[pt] + (pt,)
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, pt)
    # Where *every* candidate would draw into a part, place none. J4's
    # two pins are one pin pitch apart and its stubs point back at its
    # own body, so a flag symbol -- wider than the stub is long -- lands
    # on the connector whichever pin it picks. The pin then keeps its
    # net name, which is what names the net anyway; a flag is a nicety
    # and a symbol drawn across a part is a reported fault.
    # A part must not be left with nothing. Skipping a flag is right
    # when the pin keeps a net name a reader can follow, and wrong when
    # it was the part's only mark: J4, LED8, R36, R7, R8 and R12 came
    # out of the first version of this rule with no flag, no wire and no
    # visible tie of any kind -- six components adrift, bought with six
    # flags that would have been drawn a little too close to something.
    # A symbol slightly crowded beats a part that reads as unconnected.
    # --- lead a crowded flag out of the pin field -------------------------
    # An IC's pins are ten units apart and a ground symbol is twenty
    # across, so a flag at the stub end of a dense row *must* touch its
    # neighbours: U4's ground sat on COMP_5V and FB_5V, U5's on SY_EN,
    # and the last reads as "enable tied to ground" -- a dead regulator.
    # Dropping the flag loses the symbol; keeping it loses the reading.
    #
    # The third answer is the one a human draws: carry the net a little
    # way out of the pin field and put the symbol at the end of that
    # lead. The lead is wire on the net it already belongs to, added to
    # the stub's own line group, so it is the same safety class as the
    # wires schwire draws -- and it is refused if it would come near
    # anything carrying a different net.
    lead = {}
    led_boxes = []
    led_segs = []

    def _crosses(p, q, r, s):
        """Do segments pq and rs meet, endpoints included?"""
        def side(o, u, v):
            return ((u[1] - o[1]) * (v[0] - o[0])
                    - (u[0] - o[0]) * (v[1] - o[1]))
        def on(p_, u_, v_):
            return (min(u_[0], v_[0]) <= p_[0] <= max(u_[0], v_[0])
                    and min(u_[1], v_[1]) <= p_[1] <= max(u_[1], v_[1]))
        d1, d2 = side(p, q, r), side(p, q, s)
        d3, d4 = side(r, s, p), side(r, s, q)
        if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
            return True
        return (d1 == 0 and on(r, p, q) or d2 == 0 and on(s, p, q)
                or d3 == 0 and on(p, r, s) or d4 == 0 and on(q, r, s))

    if a.flag_lead:
        turn_first = {s.strip() for s in (a.lead_turn or "").split(",") if s.strip()}
        stubgeo = {}
        for h, b, _, _ in rows:
            if h and h["type"] == "LINE" and b.get("lineGroup") in netname:
                stubgeo.setdefault(b["lineGroup"], []).append(
                    ((round(b["startX"]), round(b["startY"])),
                     (round(b["endX"]), round(b["endY"]))))
        pinset = set(pins)
        # Points whose flag would be drawn *into a part* count too. U4's
        # thermal pad stub ends just above the package outline, so its
        # ground symbol would land in the middle of the IC and was
        # dropped -- leaving a stub ending in the word GND with no
        # symbol, on a sheet where every other ground pin has one. A
        # lead carries it clear of the body exactly as it carries one
        # clear of a neighbouring pin.
        # A flag that turning over takes out of a part needs no lead:
        # whichever corner of the pin field it is in, the cheaper answer
        # is to turn it. U4's pad is the case -- and a lead out of there
        # runs right along the pin row, into the corridor SW_5V has to
        # come down through, and blocks that net's route outright.
        for pt in sorted(crowded | onwire_pt | inbody):
            if any(_turn_clears(pt, n_) for n_, _ in at.get(pt, ())):
                continue
            grp = next((g for g, segs in stubgeo.items()
                        for s in segs if pt in s), None)
            if grp is None or pt in pinset:
                continue
            mine = netname.get(grp)
            other = next((s[0] if s[1] == pt else s[1]
                          for s in stubgeo[grp] if pt in s), None)
            if other is None or other == pt:
                continue
            ux = (pt[0] - other[0])
            uy = (pt[1] - other[1])
            n = max(abs(ux), abs(uy)) or 1
            ux, uy = ux // n, uy // n
            tip = (pt[0] + ux * a.flag_lead, pt[1] + uy * a.flag_lead)
            # Clear along the **new** stretch only. Testing from the
            # stub end outward included the very crowding the lead is
            # there to escape, so every candidate was rejected and the
            # feature did nothing. The flag's own symbol reaches about
            # ten units each way, so the test starts a symbol's width
            # out and covers the tip.
            foreign = [q for q, ns in at.items() if mine not in {m for m, _ in ns}]
            start = (pt[0] + ux * 14, pt[1] + uy * 14)
            def _clear(a_, b_):
                lo_ = (min(a_[0], b_[0]) - 12, min(a_[1], b_[1]) - 12)
                hi_ = (max(a_[0], b_[0]) + 12, max(a_[1], b_[1]) + 12)
                if any(lo_[0] <= q[0] <= hi_[0]
                       and lo_[1] <= q[1] <= hi_[1] for q in foreign):
                    return False
                # A lead already put down for another flag is wire on *its*
                # net, and there is no junction dot to say whether a
                # crossing is one node or two -- in a geometric schematic
                # it is two, and it reads as an ambiguity. The lead this
                # was written for turned across its neighbour's: U4's
                # FB_5V, blocked straight out by U4's ground flag, turned
                # up at x=430 and cut the ground lead lying at y=120 on
                # its way past. Clearing the foreign *stub ends* did not
                # see it, because the obstruction was a lead this loop had
                # added a moment earlier.
                for (sx0, sy0), (sx1, sy1), sn_ in led_segs:
                    if sn_ == mine:
                        continue
                    if _crosses(a_, b_, (sx0, sy0), (sx1, sy1)):
                        return False
                return True

            def _record(pt_, pts_):
                prev_ = pt_
                for nxt_ in pts_:
                    led_segs.append((prev_, nxt_, mine))
                    prev_ = nxt_

            # The tip must also be somewhere the symbol may be drawn.
            # U4's thermal pad stub lies *above* its package, so the
            # straight lead is clear of every foreign point and the flag
            # at its end still hangs into the IC -- the one thing the
            # lead was supposed to escape.
            #
            # And it must clear the symbols the previous leads put down.
            # Two pins one pitch apart, both crowded, are led the same
            # distance along parallel stubs and their symbols land on
            # each other: U4's ground at (410,120) and its FB_5V at
            # (410,140) both reached x=470 and overlapped. The pins are
            # ten apart, so a lead that clears the *stubs* still does
            # not clear the *flags*, and the clash this tool prints at
            # the end is the one it just created. Points are walked in
            # order, so the earlier flag's box is known here; a tip
            # landing in it is refused and the turn below is tried.
            def _free(t, prev=None):
                box = _flagbox(t, mine, prev)
                return box is None or not any(
                    box[0] < q[2] and q[0] < box[2]
                    and box[1] < q[3] and q[1] < box[3] for q in led_boxes)

            # A symbol landing on the one above it is one answer, and
            # turning away is another -- but the shortest is to stop
            # short: the neighbour has already vacated the near half of
            # the row, so a lead of half the length lands its own symbol
            # where nothing is. U4's FB_5V takes that one; longer did
            # not help, because at ninety units the tip is inside D2.
            # A straight lead is shorter, but it lies in the pin row; where
            # a neighbouring net has to cross that row to reach its own
            # parts, the flag walls it off. FB_5V on U4 is the case: its
            # straight lead runs at y=140 from the FB pin to (440, 140),
            # and with the FB stub it seals y=140 from x=380 to 440 --
            # which is the only line COMP_5V could cross to get from its
            # pin down to C7 and R27. The pin row is exactly the wrong
            # place for the flag, and a turn leaves it.
            took = None if mine in turn_first else ([
                t_ for d_ in (a.flag_lead, a.flag_lead // 2, a.flag_lead * 3 // 2)
                if (t_ := (pt[0] + ux * d_, pt[1] + uy * d_)) != pt
                and _clear(start, t_) and not _draws_into(t_, mine, pt)
                and _free(t_, pt)][:1] or None)
            if took is not None:
                lead[pt] = (took, grp)
                _record(pt, took)
                if (bx := _flagbox(took[-1], mine, pt)) is not None:
                    led_boxes.append(bx)
                continue
            # Straight out is blocked -- U5's ground has L2's stub end
            # sitting in the only space it could use. Turn instead: a
            # short run along the stub, then the same distance across
            # it. Still wire on the net it belongs to, still refused if
            # the new stretch comes near anything foreign, and it does
            # not need the rest of the sheet moved out of the way.
            # A net in --lead-turn leaves the row *square*, straight off the
            # stub end, which is where the flag at (440, 140) should have
            # been all along: perpendicular and clear of the pin row. The
            # knee form below runs twenty units along the row first, and
            # for FB_5V that lands the flag twelve units from COMP_5V's own
            # stub end -- refused, correctly, as two symbols a reader cannot
            # tell apart. Trimmed fourteen units out like the straight test,
            # so the clearance is measured on the new stretch and not on the
            # crowding it is leaving.
            px_, py_ = -uy, ux
            for s_ in (1, -1):
                dx_, dy_ = px_ * s_, py_ * s_
                far = (pt[0] + dx_ * a.flag_lead, pt[1] + dy_ * a.flag_lead)
                st_ = (pt[0] + dx_ * 14, pt[1] + dy_ * 14)
                if mine not in turn_first or not _clear(st_, far) \
                        or _draws_into(far, mine, pt) or not _free(far, pt):
                    continue
                lead[pt] = ([far], grp)
                _record(pt, [far])
                if (bx := _flagbox(far, mine, pt)) is not None:
                    led_boxes.append(bx)
                break
            else:
                knee = (pt[0] + ux * 20, pt[1] + uy * 20)
                for s_ in (1, -1):
                    far = (knee[0] - uy * a.flag_lead * s_,
                           knee[1] + ux * a.flag_lead * s_)
                    if _clear(start, knee) and _clear(knee, far) \
                            and not _draws_into(far, mine, knee) \
                            and _free(far, knee):
                        lead[pt] = ([knee, far], grp)
                        _record(pt, [knee, far])
                        if (bx := _flagbox(far, mine, knee)) is not None:
                            led_boxes.append(bx)
                        break
        if lead:
            print(f"  lead   {len(lead)} crowded flag(s) carried "
                  f"{a.flag_lead} units out of the pin field on their own "
                  f"net, rather than dropped or drawn across a neighbour")

    own_flags = collections.Counter(k[0] for k in best)
    # Which of the wanted nets are drawn with the ground symbol. Taken
    # from the donor flag each net borrows, not from its name: a design
    # may call its return AGND or LED_RTN and still draw it as ground.
    skipped_clash = skipped_wire = kept_last = kept_crowded = 0
    for key, (rank, pt) in sorted(best.items()):
        inb, onw, crd = why[pt]
        if own_flags[key[0]] <= 1 and (inb or onw or crd):
            kept_last += 1
            continue
        if inb:
            # Before the lead check, deliberately. Looking for a lead
            # first was tried, to rescue U4's thermal pad: its stub
            # points *into* the package -- free end at (330, 90) with
            # the body spanning 265 to 395 -- so a lead along it stays
            # over the package and the symbol still lands inside. It
            # gained nothing and cost a flag bar on a foreign stub.
            del best[key]
            own_flags[key[0]] -= 1
            skipped_clash += 1
        elif pt in lead:
            # carried out of the pin field instead; nothing to skip
            pass
        elif key[1] in ground_nets and crd and not onw:
            # Ground keeps its symbol even when crowded. DIN EN 60617-02
            # has one, and a reader who meets it on fourteen pins and
            # then reads the word "GND" on the fifteenth has to decide
            # whether that pin means something else. Three of this
            # sheet's ground pins came out as text -- both of U4's and
            # one of U5's, which is exactly where the ground return
            # matters. An inconsistent convention costs more than a bar
            # drawn a pin pitch from its neighbour.
            kept_crowded += 1
        elif (onw or crd) and a.strict_flags:
            del best[key]
            own_flags[key[0]] -= 1
            skipped_wire += 1
    if kept_crowded:
        print(f"  keep   {kept_crowded} crowded ground flag(s); the symbol "
              f"is the one a reader is certain of, and using it on some "
              f"pins and not others is worse than using it close")
    if kept_last:
        print(f"  keep   {kept_last} crowded flag(s) that are their part's "
              f"only mark; a part with neither flag nor wire reads as "
              f"unconnected")
    if skipped_clash:
        print(f"  skip   {skipped_clash} flag(s) that would be drawn across a "
              f"part whichever pin they took; those pins keep their net name")
    if skipped_wire:
        print(f"  skip   {skipped_wire} flag(s) whose symbol would cross a "
              f"foreign net's wire; those pins keep their net name")
    chosen = {v[1] for v in best.values()}

    # --- which stub ends wire already joins -------------------------------
    # A union-find over every LINE in the file, which is how a hand-drawn
    # wire -- or a wire schwire will add, if this is re-run on a wired
    # sheet -- becomes visible here at all. Two things ask it the same
    # question: whether a pin is already on a node that carries a symbol,
    # and whether a pin has nothing reaching it.
    uf = {}

    def _find(q):
        uf.setdefault(q, q)
        r_ = q
        while uf[r_] != r_:
            r_ = uf[r_]
        while uf[q] != r_:
            uf[q], q = r_, uf[q]
        return r_

    def _join(q, r_):
        a_, b_ = _find(q), _find(r_)
        if a_ != b_:
            uf[a_] = b_

    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("startX") is not None:
            _join((round(b["startX"]), round(b["startY"])),
                  (round(b["endX"]), round(b["endY"])))

    # Points that needed no lead come first, so that where a wire joins
    # two of them the symbol that survives is the one already sitting
    # cleanly. A lead is only drawn because the pin field was crowded --
    # U4's ground was carried 60 units out to get clear of its own pins --
    # and once a wire covers the node that carried-out symbol is the one
    # to give up, not the one on the stub end that never needed moving.
    taken, placed_pts = set(), set()
    for pt, nets in sorted(at.items(), key=lambda kv: (kv[0] in lead, kv[0])):
        for n, wid in sorted(nets):
            # Only where the stub already names this net. A flag put on the
            # wrong coordinate is a short, and the coordinate is read, never
            # worked out.
            if n in wanted and pt not in pins:
                if a.one_per_part:
                    key = (stub_owner.get(pt), n)
                    if key[0] is not None:
                        if key in taken or pt not in chosen:
                            # The flag goes, the *name* stays. Hiding it
                            # too left 53 pins with a bare stub ending in
                            # nothing -- J1 ten of them, U2 ten, U9 nine
                            # -- and a stub that carries neither symbol
                            # nor name reads as not connected. "The part
                            # has a flag a few pins along" is not a
                            # drawing argument: proximity carries no net.
                            # Overlapping text was the lesser fault and
                            # schwire's stagger is where it belongs.
                            continue
                        taken.add(key)
                        # One symbol marks the node. A wire already
                        # reaching this point from a symbol placed above
                        # makes this one a second mark on one node -- on
                        # this sheet J4's ground, drawn down to the
                        # stage's own ground line, and U4's, drawn to the
                        # catch diode's. Which of the two survives is the
                        # sort above, which is deterministic.
                        if any(_find(pt) == _find(q) for q in placed_pts):
                            continue
                place.append((pt, n))
                placed_pts.add(pt)
                onwire.add(wid)
                spots[n] += 1
    if not place:
        print("\n  nothing to place\n")
        return 1

    ticket = max((h["ticket"] for h, _, _, _ in rows
                  if h and isinstance(h.get("ticket"), int)), default=0)
    last_sch = max(i for i, (h, _, doc, _) in enumerate(rows)
                   if h and doc and doc[0] in ("SCH_PAGE", "SCH"))

    # The lead moves the flag's coordinate in `place` itself, before
    # anything derives from it. Adjusting it inside the emission loop
    # left the verification pass -- which recomputes each flag's id from
    # `place` -- looking for a component that no longer existed, and it
    # reported the flag as renaming its net. The check was right; the
    # id was wrong.
    added = []
    # Each flag entry's *pin*, recovered before the lead below moves the
    # entry's coordinate off the stub end and onto the flag's own tip. The
    # ground bus needs the pins rather than those ends -- see below.
    pin_of_end = {}
    for h, b, _, _ in rows:
        if not h or h["type"] != "LINE" or not b.get("lineGroup"):
            continue
        a_ = (round(b["startX"]), round(b["startY"]))
        b_ = (round(b["endX"]), round(b["endY"]))
        if a_ in pins and b_ not in pins:
            pin_of_end[b_] = a_
        elif b_ in pins and a_ not in pins:
            pin_of_end[a_] = b_
    orig_free = [pt for pt, _ in place]
    # Each flag's rotation, decided where its final wire segment is known.
    # A flag on a stub end is reached from its pin, which `pin_of_end` has
    # from the stub itself; the lead loop below overwrites that with the
    # direction of the lead it just drew.
    rots = []
    for (x, y), n in place:
        rots.append(_turn(n, (x, y), pin_of_end.get((x, y))))
    for i_, ((x, y), n) in enumerate(list(place)):
        if (x, y) in lead:
            pts_, grp = lead[(x, y)]
            prev = (x, y)
            for k_, nxt in enumerate(pts_):
                ticket += 1
                added.append((
                    {"type": "LINE", "ticket": ticket,
                     "id": hashlib.sha256(
                         f"lead/{n}/{x}/{y}/{k_}".encode()).hexdigest()[:16]},
                    {"lineGroup": grp, "startX": prev[0], "startY": prev[1],
                     "endX": nxt[0], "endY": nxt[1], "strokeColor": None,
                     "strokeStyle": None, "fillColor": None,
                     "strokeWidth": 1, "fillStyle": None}))
                prev = nxt
            rots[i_] = _turn(n, prev, pts_[-2] if len(pts_) > 1 else (x, y))
            place[i_] = (prev, n)

    # --- one ground bus where several pins stand in a row -----------------
    # A rail's parts each drop their ground lead to a symbol of its own,
    # and where their *pins* stand on one line a symbol per part states the
    # same connection once per part. Joined by one line through those pins
    # the node reads as one node and one symbol marks it.
    #
    # Through the pins, not through the stub ends. A bar drawn at the end
    # of the leads is a comb: three cables dropping to a line that meets
    # nothing the reader can name, and the drop is the whole fault. At the
    # pin row it is one line with the ground triangle on the end of it.
    #
    # Collinear, not merely near: proximity cascades -- C7's ground is
    # within reach of R24's, R24's of C9's -- and a "node" that swallows
    # half the sheet's ground pins is exactly why --one-per-node exempts
    # ground above. A run shares a coordinate and every gap in it is
    # small, which is what makes it a bus on the page.
    #
    # The bus wire carries no `NET` attribute, deliberately. The net is
    # already said by each pin's own stub, and `epro.py diff` counts one
    # ATTR key=NET per pin: a wire of its own would lift the export's
    # count above the netlist's and read as a discrepancy that is not one.
    # The line is drawing over a connection that already exists.
    #
    # The members after the first keep their stubs and their labels and
    # lose nothing but the duplicate symbols; their stubs are the ones
    # schwire drops, because the bus now reaches the pin each of them led
    # to. The first member's stub stays: it is where the ground triangle
    # hangs, and a run with its symbol deleted is a bus to nowhere.
    bus_segs, bus_dropped, bus_reached = [], 0, set()
    if a.ground_bus:
        def _crosses(s, t):
            (x1, y1), (x2, y2) = s
            (x3, y3), (x4, y4) = t

            def _o(ax, ay, bx, by, cx, cy):
                v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
                return (v > 0) - (v < 0)

            o1, o2 = _o(x1, y1, x2, y2, x3, y3), _o(x1, y1, x2, y2, x4, y4)
            o3, o4 = _o(x3, y3, x4, y4, x1, y1), _o(x3, y3, x4, y4, x2, y2)
            if o1 != o2 and o3 != o4:
                return True
            if o1 == o2 == o3 == o4 == 0:
                return (max(min(x1, x2), min(x3, x4))
                        <= min(max(x1, x2), max(x3, x4))
                        and max(min(y1, y2), min(y3, y4))
                        <= min(max(y1, y2), max(y3, y4)))
            return False

        pinpts = set(pins)
        wire_segs = [((b["startX"], b["startY"]), (b["endX"], b["endY"]))
                     for h, b, _, _ in rows
                     if h and h["type"] == "LINE"
                     and b.get("startX") is not None]

        # (pin, index into `place`, net) for every ground flag whose stub
        # could be followed back to a pin. An entry with no pin is left out
        # of the bus entirely rather than placed by a guessed coordinate.
        entries = []
        for i_, (pt, n) in enumerate(place):
            if n not in ground_nets:
                continue
            pin = pin_of_end.get(orig_free[i_])
            if pin is not None:
                entries.append((pin, i_, n))

        def _runs(fixed, along):
            """Ground flags whose *pins* stand on one line, in runs."""
            lines = collections.defaultdict(list)
            for e in entries:
                lines[fixed(e[0])].append(e)
            out = []
            for grp in lines.values():
                if len(grp) < 2:
                    continue
                grp.sort(key=lambda e: along(e[0]))
                run = [grp[0]]
                for e in grp[1:]:
                    if along(e[0]) - along(run[-1][0]) <= a.ground_bus:
                        run.append(e)
                    else:
                        if len(run) > 1:
                            out.append(run)
                        run = [e]
                if len(run) > 1:
                    out.append(run)
            return out

        drop = set()
        # A row first, then a column; a point claimed by one run is not
        # offered to the next.
        for run in (_runs(lambda p: p[1], lambda p: p[0])
                    + _runs(lambda p: p[0], lambda p: p[1])):
            if any(e[1] in drop for e in run):
                continue
            end_a, end_b = run[0][0], run[-1][0]
            s = (end_a, end_b)
            horizontal = end_a[1] == end_b[1]
            lo, hi = sorted((end_a[0], end_b[0]) if horizontal
                            else (end_a[1], end_b[1]))
            fixed_at = end_a[1] if horizontal else end_a[0]
            # A pin *inside* the span would be a connection the run did not
            # ask for -- the run's own members excepted, since a three-pin
            # run has one in the middle by construction. A wire crossing the
            # span anywhere but at a member is the same fault; the members'
            # own stubs meet it at their pins and are the crossings that are
            # wanted.
            # The members' pins *and* their stub ends. A wire meeting the
            # bus at either is the connection the bus is for -- it comes
            # off a lead of the same net -- and counting only the pins put
            # a hand-drawn ground wire onto R26's stub end and had the bus
            # refuse its own run, which cost three symbols rather than
            # saving one. A wire crossing the span anywhere else is the
            # short this guard exists for.
            mem = {e[0] for e in run} | {orig_free[e[1]] for e in run}
            if horizontal:
                intruder = any(py == fixed_at and lo < px < hi
                               and (px, py) not in mem for px, py in pinpts)
            else:
                intruder = any(px == fixed_at and lo < py < hi
                               and (px, py) not in mem for px, py in pinpts)
            hit = any(_crosses(s, t) and not (set(t) & mem)
                      for t in wire_segs)
            if intruder or hit:
                continue
            bus_segs.append((end_a, end_b))
            drop |= {e[1] for e in run[1:]}
        if bus_segs:
            bus_dropped = len(drop)
            # The bus is wire this run drew, and the flags it took away
            # are exactly the pins it reaches -- the drop *means* the pin
            # is joined to the one that kept the symbol. Recorded here
            # rather than inferred from the geometry, because in a
            # geometric schematic a bus also joins whatever its span
            # passes through, and that is the rule that took them away.
            bus_reached = {orig_free[i_] for i_ in drop}
            place = [e for i_, e in enumerate(place) if i_ not in drop]
            rots = [r for i_, r in enumerate(rots) if i_ not in drop]
            orig_free = [q for i_, q in enumerate(orig_free) if i_ not in drop]

    # --- a pin on a flagged net left with no mark of its own --------------
    # `--one-per-part` puts one symbol on a part that has several pins on
    # the net, and that is right when those pins stand in a row: ten ground
    # symbols down one pin pitch is the black ladder the rule exists for.
    # It is wrong when nothing joins them, because then one pin reads as
    # grounded and its twin reads as a bare stub ending in the word GND.
    # U4 is the case: its ground return and its thermal pad are on opposite
    # edges of the package, so one flag sits at (380, 120) and the pad's
    # stub ends at (360, 90) with nothing but the word GND on it -- on the
    # pin where the ground return matters most. Every other ground pin on
    # this sheet ends in a symbol, so that one asks a reader to decide
    # whether it means something else, which is the reading the
    # crowded-ground rule above was already written to prevent.
    #
    # Reach is what decides it, not "does a flag sit here" -- most of a
    # rail has no flag of its own by design. Four things reach a pin, and
    # the placement model above knows all four: the flag on its own stub
    # end; a flag on another stub end of the same node, where
    # --one-per-node groups them; a bus this run drew and then took the
    # symbol away from, since the drop means the pin is joined to the one
    # that kept it; and wire in the file. Reading only the first reported
    # 16 pins here, six of them the 5 V pins the node rule had marked.
    #
    # The last one is the union-find built above, which reads every LINE in
    # the file rather than a table of what this run drew -- so a wire
    # schwire adds in the next stage counts too, and re-running this on a
    # wired sheet stops warning about a pin that has since been joined.

    # The *stub ends* that carry a flag, not the pins behind them: the
    # comparison below is between stub ends, and a set of pins would make
    # every group look unmarked and silence the check. A stub end and the
    # pin behind it are one component of the union-find, so a flag's own
    # pin still tests as reached.
    marked = {orig_free[i_] for i_, (_, n_) in enumerate(place)
              if n_ in wanted}
    on_net = collections.defaultdict(list)
    for pt_, nets_ in at.items():
        if pt_ in pins:
            continue
        for n_, _w in nets_:
            if n_ in wanted and stub_owner.get(pt_) is not None:
                on_net[(stub_owner[pt_], n_)].append(pt_)

    def _covered(n_, q):
        if q in marked or q in bus_reached:
            return True
        if (n_, q) in node_of:
            k_ = node_of[(n_, q)]
            if any(node_of.get((n_, m)) == k_ for m in marked):
                return True
        # A wire reaching a bus member counts, and that is not academic:
        # the hand-drawn ground wire from J4 lands on a stub end whose own
        # flag the bus took away, and reading only the surviving flags
        # called J4 bare when the bus was holding its symbol.
        return any(_find(q) == _find(m) for m in marked | bus_reached)

    bare = []
    for (who_, n_), pts_ in sorted(on_net.items()):
        covered = [q for q in pts_ if _covered(n_, q)]
        if len(covered) == len(pts_):
            continue
        # Two shapes, told apart because the fix differs. A part whose
        # *other* pin on the net carries the symbol wants a wire joining
        # the two. A part the skip rules stripped of every flag wants a
        # symbol, or an honest reason it has none -- and that one the skip
        # lines report only as a count, so it is named here as well.
        for q in pts_:
            if q not in covered:
                bare.append((n_, q, who_, bool(covered)))

    spots = collections.Counter(n for _, n in place)
    for n in wanted:
        print(f"  place  {spots[n]:3d} x {n}")
    if bus_segs:
        print(f"  bus    {len(bus_segs)} ground run(s) joined by one wire "
              f"each, {bus_dropped} symbol(s) fewer than one per lead")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    for i_, ((x, y), n) in enumerate(place):
        cid = hashlib.sha256(f"flag/{n}/{x}/{y}".encode()).hexdigest()[:16]
        ticket += 1
        added.append(({"type": "COMPONENT", "ticket": ticket, "id": cid},
                      {"locked": False, "zIndex": 1, "partId": FLAG_PART,
                       "groupId": "", "x": x, "y": y,
                       "rotation": (a.rotation + rots[i_]) % 360,
                       "isMirror": False, "attrs": {}}))
        s, d = flags[using[n]]
        # `Global Net Name` is what decides the net, and the instance's
        # overrides the device's -- which is exactly how the donor's +24V
        # flag differs from its +5V one, sharing a device whose value is
        # "+5V". Setting only `Name`, as the first version did, would have
        # left all six supply rails inheriting "+5V" and merged them into
        # one net, and put the eight LED_RTN flags on GND. LED_RTN and GND
        # meeting anywhere but the shunt makes the current sense read zero;
        # design.yaml says so in its longest comment.
        # `Global Net Name` is the one the reader sees. The donor's symbols
        # draw it -- `valueVisible: true` on both Ground-GND and Power-5V --
        # and hiding it along with the rest put 101 identical bare supply
        # bars on the sheet: BUS_3V3, VBUS and CH1_36V indistinguishable,
        # and LED_RTN rendering as a plain GND triangle. Combined with
        # hiding the net text, that took 116 rail pins from labelled to
        # unidentifiable, which is worse than the text it replaced.
        # The ground triangle already says GND, so its text is 90 strings
        # that carry nothing -- an unlabelled ground symbol means GND by
        # convention. Every other net keeps its name, and LED_RTN
        # especially: it uses the same ground symbol and is a *different*
        # net, which meeting GND anywhere but the shunt makes the current
        # sense read zero.
        visible = set() if n == "GND" else {"Global Net Name"}
        for key, value in (("Symbol", s), ("Device", d), ("Name", n),
                           ("Global Net Name", n), ("Relevance", "[]")):
            ticket += 1
            added.append((
                {"type": "ATTR", "ticket": ticket,
                 "id": hashlib.sha256(f"fa/{n}/{x}/{y}/{key}".encode()).hexdigest()[:16]},
                {"groupId": "", "locked": False, "zIndex": 1, "parentId": cid,
                 "key": key, "value": value, "keyVisible": False,
                 "valueVisible": key in visible, "x": None, "y": None,
                 "rotation": 0,
                 "color": None, "fillColor": None, "fontFamily": None,
                 "fontSize": None, "strikeout": None, "underline": None,
                 "italic": None, "fontWeight": None, "align": None,
                 "version": "2.0"}))

    for end_a, end_b in bus_segs:
        wid = hashlib.sha256(f"bus/{end_a}/{end_b}".encode()).hexdigest()[:16]
        lid = hashlib.sha256(f"busline/{end_a}/{end_b}".encode()).hexdigest()[:16]
        ticket += 1
        added.append(({"type": "WIRE", "ticket": ticket, "id": wid},
                      {"zIndex": 99}))
        ticket += 1
        added.append(({"type": "LINE", "ticket": ticket, "id": lid},
                      {"fillColor": None, "fillStyle": None,
                       "strokeColor": None, "strokeStyle": None,
                       "strokeWidth": None, "startX": end_a[0],
                       "startY": end_a[1], "endX": end_b[0],
                       "endY": end_b[1], "lineGroup": wid}))

    # Hide the net name wherever a flag now says it. The symbol is the
    # whole point: leaving the text as well means 233 strings still on the
    # sheet saying what 233 symbols already say, and the reader is no
    # better off. Visibility only -- the ATTR stays, so the record count
    # and `epro.py diff` are untouched.
    hidden = 0
    out_lines = []
    for i, (h, b, doc, raw) in enumerate(rows):
        if (not a.keep_text and h and h["type"] == "ATTR"
                and b.get("key") == "NET" and b.get("valueVisible") is not False):
            if b.get("parentId") in onwire:
                nb = dict(b)
                nb["valueVisible"] = False
                raw = (f"{json.dumps(h, separators=(',', ':'))}||"
                       f"{json.dumps(nb, separators=(',', ':'))}|")
                hidden += 1
        out_lines.append(raw)
        if i == last_sch:
            out_lines += [f"{json.dumps(hh, separators=(',', ':'))}||"
                          f"{json.dumps(bb, separators=(',', ':'))}|"
                          for hh, bb in added]
    for kind, uuid, lines in carry:
        out_lines += lines

    src = zipfile.ZipFile(a.target)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    # --- prove the existing records are untouched -------------------------
    _, back = load(a.out)
    new_ids = {h["id"] for h, _ in added}
    kept = [r for r in back if not (r[0] and r[0].get("id") in new_ids)]
    carried = sum(len(l) for _, _, l in carry)
    if len(kept) != len(rows) + carried:
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(kept)} original lines out, expected "
              f"{len(rows)} + {carried} — {a.out} deleted\n", file=sys.stderr)
        return 1
    for (h1, b1, _, _), (h2, b2, _, _) in zip(rows, kept):
        if (h1, b1) == (h2, b2):
            continue
        # The only permitted change to an existing record.
        changed = {k for k in set(b1 or {}) | set(b2 or {})
                   if (b1 or {}).get(k) != (b2 or {}).get(k)}
        # ...or the one other permitted change: a lead this run was
        # asked to re-aim, and only its ends moved.
        moved = (h1 and h1.get("id") in restub and h1["type"] == "LINE"
                 and changed <= {"startX", "startY", "endX", "endY"})
        if h1 != h2 or (not moved and (b1.get("key") != "NET"
                                       or changed != {"valueVisible"})):
            a.out.unlink(missing_ok=True)
            print(f"  error: an existing record changed — {a.out} deleted\n",
                  file=sys.stderr)
            return 1

    # --- every flag must name the net it sits on -------------------------
    # The net diff cannot see this: a flag carries no NET attribute, so
    # merging six rails into one would leave 441 pin connections intact and
    # the report clean. This is the only check that looks.
    gnn = {}
    for h, b, _, _ in load(a.out)[1]:
        if h and h["type"] == "ATTR" and b.get("key") == "Global Net Name":
            gnn[b.get("parentId")] = b.get("value")
    wrong = []
    for (x, y), n in place:
        cid = hashlib.sha256(f"flag/{n}/{x}/{y}".encode()).hexdigest()[:16]
        eff = gnn.get(cid, device_netname(drows, flags[using[n]][1]))
        if eff != n:
            wrong.append((n, eff, (x, y)))
    if wrong:
        a.out.unlink(missing_ok=True)
        for n, eff, pt in wrong[:6]:
            print(f"  SHORT  a flag on {n} at {pt} would name the net {eff!r}")
        print(f"\n  {len(wrong)} flag(s) would rename their net — {a.out} "
              f"deleted\n", file=sys.stderr)
        return 1
    print(f"  every flag names the net it sits on")
    if bare:
        for n_, q, who_, partial in bare[:6]:
            why_ = ("the part's other pin on this net carries a symbol and "
                    "nothing joins the two" if partial else
                    "the part carries no symbol on this net at all")
            print(f"  WARN   {who_} on {n_} is left bare at ({q[0]}, {q[1]}) "
                  f"— {why_}")
        print(f"  {len(bare)} pin(s) on a flagged net keep only their net "
              f"name — the net reads as a symbol everywhere else, so these "
              f"read as a different net. Give each its own flag, or draw "
              f"the wire that joins it to a pin that has one")
    else:
        print(f"  no pin on a flagged net is left without a symbol or a wire")

    # Where the far end of each flag's pin graphic lands. The flag is
    # connected at its pin's *root* -- measured, not assumed: all 226
    # roots sit on a stub endpoint of their own net, and none on a
    # foreign one. The tip is the drawn bar. Whether EasyEDA joins a net
    # along a pin's graphic or only at its root is not decidable from
    # outside this file, so a tip landing on a foreign stub's endpoint is
    # reported rather than passed over. It cannot be placed away either:
    # the root has to stay on the stub or the flag connects to nothing.
    rows2 = load(a.out)[1]
    desig2 = {b["parentId"] for h, b, _, _ in rows2
              if h and h["type"] == "ATTR" and b.get("key") == "Designator"
              and b.get("value")}
    symref2 = {b["parentId"]: b["value"] for h, b, _, _ in rows2
               if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
               and b.get("value")}
    pins2 = collections.defaultdict(list)
    for h, b, doc, _ in rows2:
        if h and doc and doc[0] == "SYMBOL" and h["type"] == "PIN":
            pins2[doc[1]].append((b.get("x", 0), b.get("y", 0),
                                  b.get("length", 10) or 10,
                                  b.get("rotation") or 0))
    netof2 = {b["parentId"]: b["value"] for h, b, _, _ in rows2
              if h and h["type"] == "ATTR" and b.get("key") == "NET"
              and b.get("value")}
    ends2 = collections.defaultdict(set)
    for h, b, _, _ in rows2:
        if h and h["type"] == "LINE" and b.get("lineGroup") in netof2:
            n2 = netof2[b["lineGroup"]]
            ends2[(round(b["startX"]), round(b["startY"]))].add(n2)
            ends2[(round(b["endX"]), round(b["endY"]))].add(n2)
    R4 = {0: lambda x, y: (x, y), 90: lambda x, y: (-y, x),
          180: lambda x, y: (-x, -y), 270: lambda x, y: (y, -x)}
    touching = []
    for h, b, _, _ in rows2:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] in desig2):
            continue
        n2 = gnn.get(h["id"])
        if n2 is None:
            continue
        r2 = (b.get("rotation") or 0) % 360
        for px, py, L, prot in pins2.get(symref2.get(h["id"]), ()):
            ang = math.radians(prot)
            tx = px + L * math.cos(ang)
            ty = py + L * math.sin(ang)
            dx, dy = R4[r2](tx, ty)
            pt = (round(b["x"] + dx), round(b["y"] + dy))
            foreign = ends2.get(pt, set()) - {n2}
            if foreign:
                touching.append((n2, sorted(foreign)[0], pt))
    # Two flags of different nets drawn into each other, or a flag drawn
    # onto a component body. Both read as an electrical fault and neither
    # is one -- but a reader cannot know that, and the flag cannot be
    # moved out of the way because its root has to stay on the stub. So
    # it is reported with coordinates, the way the bar check is.
    def _box(x0, y0, x1, y1):
        return (x0, y0, x1, y1)

    dmap2 = {b["parentId"]: b["value"] for h, b, _, _ in rows2
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    boxes2, owner2 = [], []
    prim2 = collections.defaultdict(list)
    for h, b, doc, _ in rows2:
        if h and doc and doc[0] == "SYMBOL" and h["type"] in ("RECT", "POLY",
                                                              "ARC"):
            prim2[doc[1]].append((h["type"], b))
    for h, b, _, _ in rows2:
        if not h or h["type"] != "COMPONENT" or not b.get("partId"):
            continue
        who = (dmap2.get(h["id"])
               or (f"flag {gnn[h['id']]}" if h["id"] in gnn else None))
        if who is None:
            continue
        pts2 = []
        for kind, g in prim2.get(symref2.get(h["id"]), []):
            if kind == "RECT":
                pts2 += [(g["dotX1"], g["dotY1"]), (g["dotX2"], g["dotY2"])]
            elif kind == "ARC":
                pts2 += [(g["startX"], g["startY"]), (g["endX"], g["endY"])]
            elif g.get("points"):
                pts2 += [(q["x"], q["y"]) for q in g["points"]]
        if not pts2:
            continue
        r3 = (b.get("rotation") or 0) % 360
        tp = [R4[r3](-x if b.get("isMirror") else x, y) for x, y in pts2]
        boxes2.append(_box(b["x"] + min(x for x, _ in tp),
                           b["y"] + min(y for _, y in tp),
                           b["x"] + max(x for x, _ in tp),
                           b["y"] + max(y for _, y in tp)))
        owner2.append(who)

    def _ov(u, v):
        return u[0] < v[2] and v[0] < u[2] and u[1] < v[3] and v[1] < u[3]

    clash = []
    for i in range(len(boxes2)):
        if not owner2[i].startswith("flag "):
            continue
        for j in range(len(boxes2)):
            if i == j or owner2[i] == owner2[j]:
                continue
            if _ov(boxes2[i], boxes2[j]):
                clash.append((owner2[i], owner2[j],
                              (int(boxes2[i][0]), int(boxes2[i][1]))))
    clash = sorted({(a, b, c) for a, b, c in clash})
    if clash:
        for a_, b_, pt in clash[:6]:
            print(f"  CLASH  {a_} is drawn into {b_} at {pt}")
        print(f"  {len(clash)} flag(s) overlap something they are not on — "
              f"reads as a fault and is not one; the root has to stay on "
              f"the stub, so this is reported rather than moved")
    else:
        print(f"  no flag is drawn into another symbol")

    if touching:
        for n2, f2, pt in touching[:6]:
            print(f"  TIP    the {n2} flag's bar ends on {f2}'s stub at {pt}")
        print(f"  {len(touching)} flag bar(s) end on a foreign stub's "
              f"endpoint — connected at the root, so this is drawing and "
              f"not wiring unless EasyEDA joins along a pin graphic. "
              f"Re-export and diff to settle it.")
    else:
        print(f"  no flag bar ends on a foreign stub")
    if hidden:
        print(f"  {hidden} net name(s) hidden, now said by a symbol instead")

    print(f"  {len(added)} record(s) added, every existing one untouched")
    print(f"  wrote {a.out}")
    print(f"\n  prove it:  ./tools/epro.py diff {a.out} <netlist.json>\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
