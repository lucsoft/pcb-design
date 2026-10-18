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
out of the BOM and out of `epro.py diff`'s part check. They add no wire, so
the net diff's 441 pin connections are untouched. Both are checked after
writing rather than assumed.

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
    ap.add_argument("--rotation", type=int, default=0,
                    help="rotation for the placed flag (default 0, bars down)")
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
    print(f"\n{a.donor.name} offers: {', '.join(sorted(flags)) or 'nothing'}")
    if not flags:
        print("  error: the donor places no flag components, so there is "
              "nothing to copy\n", file=sys.stderr)
        return 1

    name, rows = load(a.target)
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

    def _flagbox(pt, n):
        e = flagext.get(n)
        if e is None:
            return None
        return (pt[0] + e[0], pt[1] + e[1], pt[0] + e[2], pt[1] + e[3])

    def _draws_into(pt, n):
        """Would a flag for n, rooted at pt, be drawn over a part?"""
        f = _flagbox(pt, n)
        if f is None:
            return False
        return any(f[0] < bx[2] and bx[0] < f[2]
                   and f[1] < bx[3] and bx[1] < f[3] for bx in partbox)

    def _draws_over_wire(pt, n):
        """... or over a wire carrying some other net?"""
        f = _flagbox(pt, n)
        if f is None:
            return False
        for x0, y0, x1, y1, wn in wireseg:
            if wn == n or wn is None:
                continue
            if f[0] < x1 and x0 < f[2] and f[1] < y1 and y0 < f[3]:
                return True
        return False

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

    best = {}
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
                rank = (pt in inbody, pt in onwire_pt, pt in crowded, pt)
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
    if a.flag_lead:
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
        for pt in sorted(crowded | onwire_pt | inbody):
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
            lo = (min(start[0], tip[0]) - 12, min(start[1], tip[1]) - 12)
            hi = (max(start[0], tip[0]) + 12, max(start[1], tip[1]) + 12)
            def _clear(a_, b_):
                lo_ = (min(a_[0], b_[0]) - 12, min(a_[1], b_[1]) - 12)
                hi_ = (max(a_[0], b_[0]) + 12, max(a_[1], b_[1]) + 12)
                return not any(lo_[0] <= q[0] <= hi_[0]
                               and lo_[1] <= q[1] <= hi_[1] for q in foreign)

            # The tip must also be somewhere the symbol may be drawn.
            # U4's thermal pad stub lies *above* its package, so the
            # straight lead is clear of every foreign point and the flag
            # at its end still hangs into the IC -- the one thing the
            # lead was supposed to escape.
            if _clear(start, tip) and not _draws_into(tip, mine):
                lead[pt] = ([tip], grp)
                continue
            # Straight out is blocked -- U5's ground has L2's stub end
            # sitting in the only space it could use. Turn instead: a
            # short run along the stub, then the same distance across
            # it. Still wire on the net it belongs to, still refused if
            # the new stretch comes near anything foreign, and it does
            # not need the rest of the sheet moved out of the way.
            knee = (pt[0] + ux * 20, pt[1] + uy * 20)
            for s_ in (1, -1):
                far = (knee[0] - uy * a.flag_lead * s_,
                       knee[1] + ux * a.flag_lead * s_)
                if _clear(start, knee) and _clear(knee, far) \
                        and not _draws_into(far, mine):
                    lead[pt] = ([knee, far], grp)
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
        if own_flags[key[0]] <= 1 and (rank[0] or rank[1] or rank[2]):
            kept_last += 1
            continue
        if rank[0]:
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
        elif key[1] in ground_nets and rank[2] and not rank[1]:
            # Ground keeps its symbol even when crowded. DIN EN 60617-02
            # has one, and a reader who meets it on fourteen pins and
            # then reads the word "GND" on the fifteenth has to decide
            # whether that pin means something else. Three of this
            # sheet's ground pins came out as text -- both of U4's and
            # one of U5's, which is exactly where the ground return
            # matters. An inconsistent convention costs more than a bar
            # drawn a pin pitch from its neighbour.
            kept_crowded += 1
        elif (rank[1] or rank[2]) and a.strict_flags:
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

    taken = set()
    for pt, nets in sorted(at.items()):
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
                place.append((pt, n))
                onwire.add(wid)
                spots[n] += 1
    for n in wanted:
        print(f"  place  {spots[n]:3d} x {n}")
    if not place:
        print("\n  nothing to place\n")
        return 1

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

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
            place[i_] = (prev, n)
    for (x, y), n in place:
        cid = hashlib.sha256(f"flag/{n}/{x}/{y}".encode()).hexdigest()[:16]
        ticket += 1
        added.append(({"type": "COMPONENT", "ticket": ticket, "id": cid},
                      {"locked": False, "zIndex": 1, "partId": FLAG_PART,
                       "groupId": "", "x": x, "y": y,
                       "rotation": a.rotation, "isMirror": False, "attrs": {}}))
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
        if (h1 != h2 or b1.get("key") != "NET"
                or changed != {"valueVisible"}):
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
