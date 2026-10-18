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
                        if key in taken:
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

    added = []
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
