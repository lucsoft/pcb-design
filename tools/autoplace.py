#!/usr/bin/env python3
"""Place a block's parts the way a person draws a schematic.

    autoplace.py <raw.epro2> <netlist.json> -o placement.txt

**The output is a `placement.txt`, not an export.** That is the whole
point of it: `handplace.py`, `netflags.py`, `schwire.py`, `fixparts.py`
and `blocktitles.py` already turn coordinates into a titled, wired,
flagged sheet, and they do it identically for a person's coordinates and
a machine's. So the automatic layout is compared against the hand one by
running both through the same chain and diffing the two sheets -- the
flags, the wires and the frame are held constant, and the *placement* is
the only variable. A layout engine that emitted its own export would be
comparing two things at once and could not say which one moved.

**What the engine knows is what a person knows.** Not wire length: a
solver minimising length packs a circuit into a blob and breaks every
convention that makes it readable -- power up, ground down, a divider
drawn so it reads as a divider. `LAYOUT-PLAN.md` measured that and lost
with it. What is encoded here instead is the handful of *forms* a supply
schematic is made of, each one a thing a reader recognises:

    divider_top  rail -> mid, with mid on a hub pin.  Stands from the
    divider_leg  rail to the ground row; the lower leg lies *along* the
                 ground row, so the tap between them is a corner and the
                 divider reads as one path out of the rail and into
                 ground rather than a column with a break in it.
    bootstrap    two hub pins bridged by a capacitor, and nothing else
                 about them -- no ground, no declared rail.  Above the
                 IC, which is where a datasheet draws it.
    series       two non-ground nets.  Lies in the row it is on, because
                 that row *is* the path, and it is what puts its far net
                 on the near net's row.
    shunt        signal or rail -> ground.  Stands between its row and
                 the ground row, supply end up.
    connector    a `J`, `P` or `X` designator.  At the end of the rail
                 row rather than in the middle of it.

**Rows come first and `x` comes second**, and that order is the design.
Every readable supply schematic is horizontal lines -- one per potential,
ground at the bottom -- with parts standing between them. So each net is
given a row, a two-pin part's row span is what decides whether it stands
or lies, and the horizontal order inside a row is settled afterwards by a
one-dimensional relaxation along that row. Getting `y` right makes the
sheet read as a circuit; getting `x` right only makes it tidy.

**Nothing is placed alone.** A part with no neighbour within `--min-gap`
would read as one somebody forgot, so the engine refuses to emit a
partition with a singleton: a part that ends up on its own is pulled to
the nearest cluster. That is the rule `ce3255d` learned by counting.

    autoplace.py raw.epro2 netlist.json --dry-run      # say, do not write
    autoplace.py raw.epro2 netlist.json --blocks '36 V -> 5 V'
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schwire import grid, load, stub_nets                  # noqa: E402
from netflags import symbol_extent                         # noqa: E402
from arrange import read_blocks, read_rail_kinds, read_rails, ROT  # noqa: E402


PITCH = 40          # the pin span of a two-pin part, and so the row gap
GROUND_ROW_GAP = 40  # a rail row to the ground row below it


# --------------------------------------------------------------------------
# geometry, read off the raw import


class Part:
    """One placed component: where it is, what it is, and what it touches.

    `pins` is symbol-local, paired with the net sitting on that pin. The
    pairing is the engine's whole input: it is what says a capacitor has
    its supply on the first pin and its ground on the second, which no
    pin *number* can be trusted to say -- `PIN` record ids are not unique
    on this board, and `LAYOUT-PLAN.md` records what reading them gives.
    A pin is paired to its net by *coordinate*: the transform is applied,
    and the stub the importer drew at that point names the net.
    """

    __slots__ = ("d", "at", "rot", "pins", "ext", "value", "wide", "stub")

    def __init__(self, d, at, rot, pins, ext, value):
        self.d, self.at, self.rot, self.pins = d, at, rot, pins
        self.stub = []
        self.ext, self.value = ext, value
        e = ext or (0, 0, 0, 0)
        self.wide = (e[2] - e[0]) > (e[3] - e[1])

    def off(self, rot=None):
        """The symbol's pins in canvas offsets at a rotation."""
        r = self.rot if rot is None else rot
        f = ROT[r % 360]
        return [(f(x, y), n) for x, y, n in self.pins]

    def box(self, x, y, rot=None):
        """The part's footprint as a box, margins included.

        Margins because the symbol extent is the *drawn* outline, and a
        designator and a value sit outside it. Two resistors can be
        disjoint by their outlines and still overlap by their text.
        """
        r = self.rot if rot is None else rot
        f = ROT[r % 360]
        pts = []
        e = self.ext
        if e is not None:
            pts += [f(e[0], e[1]), f(e[2], e[1]), f(e[0], e[3]), f(e[2], e[3])]
        # The *pins and their leads* as well as the outline. `handplace`
        # measures every coordinate a component owns and refuses the write
        # when two boxes meet, and a lead is thirty units long -- so the
        # outline alone made the engine think a transistor and the
        # resistor beside it were thirteen units apart when the tool that
        # has to accept them saw them overlapping. Seven refusals across
        # three sheets, all of it this.
        for i, (px, py, _n) in enumerate(self.pins):
            qx, qy = f(px, py)
            pts.append((qx, qy))
            if i < len(self.stub):
                sx, sy = f(*self.stub[i])
                pts.append((qx + sx, qy + sy))
        if not pts:
            return (x - 20, y - 20, x + 20, y + 20)
        pad = 10
        return (x + min(a for a, _ in pts) - pad,
                y + min(b for _, b in pts) - pad,
                x + max(a for a, _ in pts) + pad,
                y + max(b for _, b in pts) + pad)


def geometry(rows):
    """designator -> Part, with every pin paired to the net on its stub."""
    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    symref = {b["parentId"]: b["value"] for h, b, _, _ in rows
              if h and h["type"] == "ATTR" and b.get("key") == "Symbol"
              and b.get("value")}
    valueof = {}
    for h, b, _, _ in rows:
        if h and h["type"] == "ATTR" and b.get("key") in ("Name", "Value"):
            valueof.setdefault(b["parentId"], b.get("value"))
    sympins = collections.defaultdict(list)
    for h, b, doc, _ in rows:
        if h and h["type"] == "PIN" and doc and doc[0] == "SYMBOL":
            sympins[doc[1]].append((b.get("x", 0), b.get("y", 0)))
    at = stub_nets(rows)
    # Where each pin's lead runs, straight from the import. The pad of a
    # QFN is the one pin whose lead the importer aimed *along* the package
    # instead of out of it, and that is a property of the import rather
    # than of the symbol -- so it is read here, where the import is.
    lead = collections.defaultdict(list)
    for h, b, _, _ in rows:
        if h and h["type"] == "LINE" and b.get("startX") is not None:
            lead[(grid(b["startX"], b["startY"]))].append(
                (grid(b["endX"], b["endY"])))
            lead[(grid(b["endX"], b["endY"]))].append(
                (grid(b["startX"], b["startY"])))

    out = {}
    for h, b, _, _ in rows:
        if (not h or h["type"] != "COMPONENT" or not b.get("partId")
                or h["id"] not in desig or not isinstance(b.get("x"), (int, float))):
            continue
        rot = (b.get("rotation") or 0) % 360
        if rot not in ROT:
            raise SystemExit(f"error: {desig[h['id']]} is rotated {rot}°, "
                             f"which is not one of 0/90/180/270")
        f = ROT[rot]
        pins, stubs = [], []
        for (px, py) in sympins.get(symref.get(h["id"]), []):
            dx, dy = f(px, py)
            pos = grid(b["x"] + dx, b["y"] + dy)
            nets = at.get(pos)
            pins.append((px, py, sorted(nets)[0] if nets else None))
            far = [q for q in lead.get(pos, []) if q != pos]
            stubs.append((far[0][0] - pos[0], far[0][1] - pos[1])
                         if far else (0, 0))
        # The anchor is normalised to rotation 0 here and the rotation is
        # carried separately, so the engine reasons in one frame.
        part = Part(desig[h["id"]], (b["x"], b["y"]), 0, pins,
                    symbol_extent(rows, symref.get(h["id"])),
                    valueof.get(h["id"], ""))
        part.stub = stubs
        out[desig[h["id"]]] = part
    return out


# --------------------------------------------------------------------------
# what the netlist says a part is


# --------------------------------------------------------------------------
# forms


def pin_of(part, net):
    """The symbol-local offset of the pin carrying `net`."""
    for x, y, n in part.pins:
        if n == net:
            return (x, y)
    return None


def dividers(parts, hub, kind):
    """(top_leg, bottom_leg, mid_net) for every sense divider in a block.

    Found as a *pair*, not one part at a time. The tap of a divider and
    the switch node of a buck look identical one part at a time -- both
    are "a rail reached through a mid node that the IC also holds" -- and
    reading them the same way drew L1 and D2 as a divider with the
    inductor standing on its end. What separates them is the company the
    mid node keeps: a divider's tap has exactly two parts on it, one
    reaching a rail and one reaching ground, and a switch node has the
    whole power path on it.
    """
    raillike = {n for n, k in kind.items() if k != "ground"}
    hubnets = {n for _, _, n in hub.pins if n}
    on = collections.defaultdict(list)
    for d, p in parts.items():
        for _, _, n in p.pins:
            if n:
                on[n].append(d)
    out = []
    for m in sorted(hubnets):
        here = [d for d in on.get(m, [])
                if len(parts[d].pins) == 2 and d != hub.d]
        if len(here) != 2:
            continue
        for top in here:
            nts = [n for _, _, n in parts[top].pins if n and n != m]
            if not nts or nts[0] not in raillike:
                continue
            for bot in here:
                if bot == top:
                    continue
                bts = [n for _, _, n in parts[bot].pins if n and n != m]
                if bts and kind.get(bts[0]) == "ground":
                    out.append((top, bot, m))
    return out


def forms(parts, hub, kind):
    """What each part is, in the vocabulary a supply schematic is drawn in.

    Each form decides whether a part stands between two rows or lies along
    one, and that decision is the difference between a sheet that reads as
    a circuit and a sheet that reads as a list of parts.
    """
    hubnets = {n for _, _, n in hub.pins if n}
    div = dividers(parts, hub, kind)
    res = {}
    for top, bot, m in div:
        res[top] = ("divider_top", m)
        res[bot] = ("divider_leg", m)
    out = {}
    for d, p in parts.items():
        if d == hub.d:
            out[d] = ("hub", None)
            continue
        if d in res:
            out[d] = res[d]
            continue
        nets = [n for _, _, n in p.pins if n]
        if len(nets) != 2 or len(p.pins) != 2:
            out[d] = ("other", None)
            continue
        a, b = nets
        ka, kb = kind.get(a, "sig"), kind.get(b, "sig")
        # The bootstrap: both ends on the IC, and neither of them ground
        # or a rail it already knows how to reach. The "neither" half is
        # load-bearing -- without it every capacitor from a rail to ground
        # is a bootstrap, because the IC has a pin on both of them, and
        # the whole block came out with seven parts stacked above the IC.
        if (a in hubnets and b in hubnets and "ground" not in (ka, kb)
                and "supply" not in (ka, kb)):
            out[d] = ("bootstrap", None)
            continue
        if "ground" in (ka, kb):
            # A connector stands at the end of the rail it serves rather
            # than in the middle of it. Designator prefix is the whole
            # test: J, P and X are the references a schematic uses for a
            # header, and nothing in the netlist can tell a two-pin fan
            # header from a two-pin capacitor.
            out[d] = (("connector" if d[:1] in "JPX" else "shunt"),
                      b if ka == "ground" else a)
            continue
        out[d] = ("series", (a, b))
    return out


# --------------------------------------------------------------------------
# the row plan: one horizontal line per potential


class Band:
    """One hub pin's company, and the two rows it is drawn on.

    A block is not one ladder of rows. It is as many ladders as the IC has
    pins with parts on them, and they are drawn beside each other: the
    chain that carries the power runs along the IC's own row, and the rest
    drop below and share a ground line. That is what the hand sheet does
    -- the output chain at y=110 with its ground at 150, the input and
    compensation bands below with a common ground at 265 -- and it is why
    a divider's two rows come out adjacent instead of being pulled apart
    by every other net in the block.
    """

    __slots__ = ("pin", "nets", "parts", "chain", "side", "rows", "ys",
                 "origin_x", "off")

    def __init__(self, pin, side):
        self.pin, self.side = pin, side
        self.nets, self.parts, self.chain = set(), set(), False
        self.rows, self.ys = {}, {}
        self.origin_x, self.off = 0.0, 0.0


def bands(parts, form, hub, kind):
    """The block split by hub pin: one Band per pin that has parts on it.

    The split is a breadth-first walk out of the IC, one front per pin,
    and a part joins the *first* front that reaches it. The order the
    fronts are advanced in is the only thing that decides the close
    calls, and it is the pin's own `y`: the topmost pin sweeps first, so
    the chain that leaves the top of the IC owns the rails it makes and a
    pin further down cannot take them from it.
    """
    hubnets = []
    for x, y, n in hub.pins:
        if n and n not in hubnets and kind.get(n) != "ground":
            hubnets.append(n)
    hubpin = {}
    for x, y, n in hub.pins:
        hubpin.setdefault(n, (x, y))
    raillike = {n for n, k in kind.items() if k == "supply"}
    fronts = sorted(hubnets, key=lambda n: (hubpin[n][1], n))
    out = []
    for n in fronts:
        b = Band(n, 1 if hubpin[n][0] >= 0 else -1)
        b.nets.add(n)
        out.append(b)
    owner = {}
    progressed = True
    while progressed:
        progressed = False
        for b in out:
            for d in sorted(parts, key=_desig_key):
                if d == hub.d or d in owner:
                    continue
                p = parts[d]
                if form[d][0] == "bootstrap":
                    continue
                ns = [a for _, _, a in p.pins if a]
                if not (set(ns) & b.nets):
                    continue
                # A part that bridges two pins of the IC belongs to the
                # *signal* one. The enable pull-up is the case: it bridges
                # BUS_5V and EN, and claiming it from the rail puts it a
                # sheet away from the pin it serves -- on the far side of
                # the IC, so the wire has to go round it, and it does not
                # go round. The hand sheet draws it in the enable run.
                others = [a for a in ns if a not in hubnets]
                if (len(ns) == 2 and all(a in hubnets for a in ns)
                        and others == [] and b.pin in raillike
                        and any(kind.get(a) != "supply" for a in ns)):
                    e2 = [a for a in ns if kind.get(a) != "supply"]
                    if e2 and e2[0] != b.pin:
                        continue
                owner[d] = b
                b.parts.add(d)
                progressed = True
                for other in ns:
                    if other in hubnets or kind.get(other) == "ground":
                        continue
                    b.nets.add(other)
                    if form[d][0] == "series":
                        b.chain = True
    # A divider belongs to the band that holds the rail it divides, not to
    # the pin it senses with. Left on the sense pin it forms a band of its
    # own, and the divider then drops below the sheet instead of standing
    # on the output it taps -- which is the one place a reader looks for
    # it.
    for top, leg, _mid in dividers(parts, hub, kind):
        rail = [n for _, _, n in parts[top].pins if n and kind.get(n) != "ground"]
        home = None
        for b in out:
            if set(rail) & b.nets and b.pin != _mid:
                home = b
                break
        if home is None:
            continue
        for d in (top, leg):
            old = owner.get(d)
            if old is not None:
                old.parts.discard(d)
            owner[d] = home
            home.parts.add(d)
        home.nets.add(_mid)

    # Nothing is left behind. A part the walk never reached -- a
    # three-pin transistor, a resistor between two signals the IC holds
    # neither of -- used to fall out of `bands` and then out of `_emit`,
    # which raised `KeyError` on the designator rather than placing it.
    # On the outputs sheet that was Q1, R15 and R7 of one eFuse block.
    #
    # It joins the band it shares the most nets with, and failing that
    # the biggest one: a part with no tie to anything is still part of
    # the block, and the block is where a reader looks for it. A
    # *silently dropped* part is the failure this project names
    # everywhere else -- `read_blocks` refuses a designator with no
    # block rather than placing it somewhere by default, and this is the
    # same rule one stage later.
    # The bootstrap is not an orphan: it is drawn above the IC and belongs
    # to no band by design. Sweeping it up put it in the output row, on
    # top of the inductor.
    left = [d for d in sorted(parts, key=_desig_key)
            if d != hub.d and d not in owner
            and form[d][0] != "bootstrap"]
    for d in left:
        ns = {a for _, _, a in parts[d].pins if a}
        best = None
        for b in out:
            k = (len(ns & b.nets), len(b.parts))
            if best is None or k > best[0]:
                best = (k, b)
        if best is None:
            continue
        best[1].parts.add(d)
        owner[d] = best[1]
        for other in ns:
            if other not in hubnets and kind.get(other) != "ground":
                best[1].nets.add(other)
    return [b for b in out if b.parts]


def band_rows(b, parts, form, hub, kind, order):
    """The two rows a band is drawn on, and then their y."""
    gnd = [n for n in b.nets if kind.get(n) == "ground"]
    # The row's own order is the *chain* order, not the power tree's: the
    # net the IC pin sits on is leftmost, then whatever the part lying on
    # that row reaches, and so on outward. L1 puts BUS_5V on SW_5V's row
    # and to its right, which is the only order in which the row reads as
    # a path.
    top, todo = [b.pin], [b.pin]
    while todo:
        m = todo.pop(0)
        for d in sorted(b.parts):
            if form[d][0] != "series":
                continue
            a, c = form[d][1]
            if m in (a, c):
                other = c if a == m else a
                if other in b.nets and other not in top:
                    top.append(other)
                    todo.append(other)
    # A divider's tap is a *third* thing: the leg that reaches it from the
    # rail stands down, and the leg that reaches ground lies along the
    # ground row, so the tap sits on the ground row itself.
    mid = sorted({form[d][1] for d in b.parts
                  if form[d][0] == "divider_top" and form[d][1] in b.nets})
    b.rows = {"top": top, "mid": mid, "gnd": gnd}
    # How deep the band is, from the *parts* rather than from the net list.
    # The walk stops at ground, so `b.nets` never holds it and every band
    # fell back to a constant -- which is why a band of 30-unit capacitors
    # was drawn as deep as a band of 40-unit ones, and why their ground
    # pins ended ten units apart with a symbol each. `netflags
    # --ground-bus` groups by the *pin*, so a longer lead does not help;
    # the pitch of the parts standing in the band is the only thing that
    # does.
    # Anything not on the top row and not a divider's tap is on the ground
    # row -- which is where it is drawn, whether or not `b.nets` names it.
    def rowof(net):
        return ("top" if net in top else "mid" if mid and net in mid
                else "gnd")
    span_top = 0
    for d in b.parts:
        p = parts[d]
        if len(p.pins) != 2:
            continue
        ns = [a for _, _, a in p.pins if a]
        if len(ns) != 2 or rowof(ns[0]) == rowof(ns[1]):
            continue
        span_top = max(span_top, int(round(pin_dist(p, ns[0], ns[1]))))
    # A divider's tap is on the *ground row*, not one pitch above it. That
    # is what makes the lower leg lie along the ground line with its far
    # end on it -- the tap is a corner and the divider reads as one path
    # out of the rail and into ground. Giving the tap a row of its own
    # pushes the ground line a whole pitch down and the band with it.
    b.ys = {"top": 0,
            "mid": span_top if mid else 0,
            "gnd": span_top or GROUND_ROW_GAP}
    if not mid:
        b.ys["mid"] = b.ys["gnd"]
    return b


def pad_spec(part, at, rot):
    """`NET@x,y` for the pin the importer led along the package, or None.

    A QFN's thermal pad is a pin on the top or bottom edge, and the lead
    the importer draws for it runs *sideways* -- thirty units along the
    package -- so a ground symbol placed on its end lands inside the IC.
    `netflags --pin-stub` re-aims exactly one such lead, and this is where
    its coordinate comes from: the engine placed the part, so the engine
    is the only thing that knows where the pad ended up. Handing the build
    a fixed coordinate would make the automatic sheet depend on the hand
    one.
    """
    e = part.ext
    if e is None or not part.pins:
        return None
    f = ROT[rot]
    for i, (px, py, net) in enumerate(part.pins):
        if not net or i >= len(part.stub):
            continue
        sx, sy = part.stub[i]
        if sx == 0 and sy == 0:
            continue
        # Down the middle of the package rather than on a side, and led
        # *along* it instead of out of it. Both halves are needed: the
        # first alone catches any pin between two others, and the second
        # alone catches every pin on the top and bottom edges.
        if not (e[0] + 5 <= px <= e[2] - 5):
            continue
        if abs(sx) <= abs(sy):
            continue
        dx, dy = f(px, py)
        return f"{net}@{at[0] + dx},{at[1] + dy}"
    return None


def hub_rot(hub, kind, order):
    """The rotation that puts the IC's *input* rail on its left.

    The power path has to read left to right, and which pin the input is
    on is a property of the symbol rather than of the netlist -- U4's
    VBUS already sits on its left at rotation 0, and U5's BUS_5V sits on
    its *right*, which is why the hand sheet turns that one over and why
    the automatic one read backwards until it did too. The rail order in
    design.yaml is what says which end is the input: the earliest rail
    the IC touches.
    """
    rank = {r: i for i, r in enumerate(order)}
    cands = [(rank[n], x, y) for x, y, n in hub.pins if n in rank]
    if not cands:
        return 0
    lowest = min(c[0] for c in cands)
    best = None
    for rot in (0, 90, 180, 270):
        f = ROT[rot]
        px = min(f(x, y)[0] for r, x, y in cands if r == lowest)
        if best is None or px < best[0]:
            best = (px, rot)
    return best[1]


def pin_dist(part, na, nb):
    """The symbol-local distance between the pins carrying two nets."""
    pa, pb = pin_of(part, na), pin_of(part, nb)
    if pa is None or pb is None:
        return 0
    return math.hypot(pa[0] - pb[0], pa[1] - pb[1])


# --------------------------------------------------------------------------
# orientation: which end is up


def upright_rot(part, kind, want_up):
    """The rotation that puts the pin carrying `want_up` above the other.

    Measured, not tabulated. All four rotations are tried and the one
    whose two pins differ in y with the supply end smaller -- smaller y
    is higher on this canvas -- is taken. A table would have to know how
    each symbol places its pins; this asks the symbol itself.
    """
    up = pin_of(part, want_up)
    if up is None:
        return 0
    others = [n for _, _, n in part.pins if n and n != want_up]
    if not others:
        return 0
    other = pin_of(part, others[0])
    for r in (90, 270, 0, 180):
        f = ROT[r]
        a, b = f(*up), f(*other)
        if a[1] != b[1] and a[1] < b[1]:
            return r
    return 0


def inline_rot(part, kind):
    """The rotation that lays a part across, its two pins level in y."""
    ns = [n for _, _, n in part.pins if n]
    if len(ns) < 2:
        return 0
    pa, pb = pin_of(part, ns[0]), pin_of(part, ns[1])
    for r in (0, 180, 90, 270):
        f = ROT[r]
        a, b = f(*pa), f(*pb)
        if a[1] == b[1] and a[0] != b[0]:
            return r
    return 0


# --------------------------------------------------------------------------
# overlap: the sweep that closes a row


# --------------------------------------------------------------------------
# the plan


def main_of(bs):
    """The band that carries the power, by the same rule `plan_block` uses."""
    return sorted(bs, key=lambda b: (-len(b.parts), not b.chain,
                                     b.side < 0, b.pin))[0] if bs else None


def order_in_band(b, parts, form, kind, hubpin):
    """The band's parts left to right, as the path runs.

    `tier` is how far along the band's top row a part stands: the IC's own
    pin net is 0, whatever the series part on it reaches is 1, and so on.
    Within a tier the *kind* decides, and the order it gives is the order
    the eye expects -- the part carrying the current, then the ones that
    store it, then the divider on the output, then the connector.
    """
    tier = {n: i for i, n in enumerate(b.rows["top"])}
    if b.rows["mid"]:
        for n in b.rows["mid"]:
            tier[n] = len(b.rows["top"]) - 1
    sub = {"shunt": 0, "series": 1, "divider_leg": 2, "divider_top": 3,
           "connector": 4, "bootstrap": 5}
    out = []
    for d in sorted(b.parts, key=_desig_key):
        f, det = form[d]
        ns = [n for _, _, n in parts[d].pins if n]
        ts = [tier[n] for n in ns if n in tier]
        # A band of shunts is ordered by the IC pin each one hangs off,
        # not by anything of its own: the part on the *lowest* pin comes
        # first, which puts it nearest the IC, and the row then reads as
        # the IC's own pin column read downward. That is the order the
        # hand sheet has -- R24 against the IC, then C9, then C8 -- and
        # the chain order cannot produce it, because none of the three is
        # on the chain at all.
        pin = max((hubpin.get(n, (0, 0))[1] for n in ns), default=0)
        head = (min(ts) if ts else len(tier)) if b.chain else -pin
        out.append(((head, sub.get(f, 9), _desig_key(d)), d, f))
    return sorted(out)


def plan_block(parts, kind, order, opts, origin):
    """designator -> (x, y, rotation) for one block, relative to origin."""
    hub = max(parts.values(), key=lambda p: (len(p.pins), p.d))
    hrot = hub_rot(hub, kind, order)
    hub = _turned(hub, hrot)
    form = forms(parts, hub, kind)
    bs = [band_rows(b, parts, form, hub, kind, order)
          for b in bands(parts, form, hub, kind)]
    # Two bands that want the same rows are one band. The input capacitors
    # and the RT resistor are two nets and three parts, and the hand sheet
    # draws them in one row: a row is a horizontal *line*, not a net, and
    # giving each of them its own band queued them apart and cost 145
    # units of width for nothing. Crystallisation, in the small: a
    # partition that cannot justify itself is absorbed by the one beside
    # it that looks the same.
    merged: list = []
    for b in bs:
        for m in merged:
            if (m.side == b.side and m.ys == b.ys and not b.chain
                    and not m.chain and id(b) is not id(main_of(bs))):
                m.nets |= b.nets
                m.parts |= b.parts
                break
        else:
            merged.append(b)
    bs = merged
    if not bs:
        # A block with nothing but its own hub -- the fan header is one
        # part and one pin. There is no band to draw and no chain to
        # order, so the part simply stands.
        return {d: (origin[0], origin[1],
                    hrot if d == hub.d else inline_rot(parts[d], kind))
                for d in parts}

    # The band that carries the power runs along the IC's own row; every
    # other band drops below and they share one ground line. That is the
    # hand sheet's shape and it is also the only way a divider's two legs
    # stay adjacent -- give every band its own rows at their natural pitch
    # and the divider is dragged apart by every net between them.
    main = sorted(bs, key=lambda b: (-len(b.parts), not b.chain,
                                     b.side < 0, b.pin))[0]

    hubpin = {}
    for x, y, n in hub.pins:
        hubpin.setdefault(n, (x, y))
    half = max(abs(hubpin.get(b.pin, (50, 0))[0]) for b in bs) or 50
    # How far below the main row the other bands start. Measured from the
    # IC's own lowest pin, because the IC is anchored on the pin that
    # starts the row and its body then hangs below that row by exactly what
    # its symbol says. A constant here is what left the bands sixty-five
    # units further down than they need to be when the IC moved -- far
    # enough that SY_EN lost its corridor and stopped being drawn.
    _toppin = min(b for _, b, _ in hub.pins)
    _botpin = max(b for _, b, _ in hub.pins)
    _hubrow = float(opts.hub_drop)
    _mainpin = None
    for _x, _y, _n in hub.pins:
        if _n == main.pin:
            _mainpin = _y
    if _mainpin is not None:
        _hubrow = float(_toppin - _mainpin)
    lower = _hubrow + (_botpin - _toppin) + opts.band_gap
    off = {id(b): 0 if b is main else lower - b.ys["gnd"] for b in bs}

    # --- where each band starts in x --------------------------------------
    # The main band grows out of the IC's own pin. Every other band hangs
    # below and they queue up: two bands on the same side would otherwise
    # land on the same columns, and the below-bands of a buck are the input
    # group and the compensation group, which are drawn side by side.
    origin_x = {}
    for b in bs:
        if b is main:
            origin_x[id(b)] = float(hubpin.get(b.pin, (half, 0))[0])
    for side in (1, -1):
        group = [b for b in bs if b is not main and b.side == side]
        group.sort(key=lambda b: (-hubpin.get(b.pin, (0, 0))[1], b.pin))
        cursor = float(side * half)
        for b in group:
            origin_x[id(b)] = cursor
            cursor += side * (len(b.parts) * opts.pitch + opts.band_gap)
    for b in bs:
        b.origin_x = origin_x[id(b)]
        b.off = off[id(b)]

    # --- the hub, and the bootstrap above it ------------------------------
    drawn = {}
    drawn[hub.d] = ("hub", 0, 0)
    for d, _p in sorted(parts.items()):
        if form[d][0] != "bootstrap":
            continue
        drawn[d] = ("bootstrap", hub.d, 0)

    if getattr(opts, "explain", False):
        print(f"    hub {hub.d} at (0,{opts.hub_drop}); main band "
              f"{main.pin}; lower ground {lower}")
        for b in bs:
            print(f"      band pin={b.pin:10} side={b.side:+d} "
                  f"chain={int(b.chain)} ys={b.ys} off={off[id(b)]} "
                  f"x0={origin_x[id(b)]:6.0f} "
                  f"parts={sorted(b.parts)}")
        for d in sorted(parts, key=_desig_key):
            f_, det_ = form[d]
            ns_ = [n for _, _, n in parts[d].pins if n]
            print(f"      {d:5} {f_:12} {det_}  nets={ns_}")
    for b in bs:
        base = origin_x[id(b)]
        seq = order_in_band(b, parts, form, kind, hubpin)
        for i, (_k, d, _f) in enumerate(seq):
            drawn[d] = ("band", b, i, base + (i + 1) * opts.pitch * b.side)

    # Open item: the IC is anchored on its *topmost* pin, which for a QFN
    # is the thermal pad. Its switch pin then sits below the row it drives
    # and SW_5V is drawn down to the pin and back up, enclosing a
    # rectangle with nothing in it -- 2.2% of the sheet, where the hand
    # sheet has none. Anchoring the IC on the pin that starts the row is
    # the right idea and was tried twice; it brought COMP_5V back as "no
    # clean path" and cost a drawn net both times, so it is off with the
    # reason beside it.
    # The IC is anchored on the pin that starts its row. `anchor` puts a
    # part's *highest* pin on the row it is given, and for a QFN that is
    # the thermal pad -- so the switch pin hangs below the row it drives
    # and SW_5V is drawn down to it and back up: eight corners, a
    # rectangle of nothing, and a detour of 1.38 for a net that is a
    # straight line. Six attempts at this failed because the bands below
    # did not move with the IC; that is fixed above.
    return _emit(drawn, parts, kind, form, hub, off, opts, origin, hrot,
                 None if _mainpin is None else _hubrow)


def _turned(part, rot):
    """A copy of the hub with its pin offsets turned, and nothing else.

    The bands are cut by which *side* of the IC a pin is on, so they have
    to be cut against the orientation the IC is actually placed in. Doing
    it to a copy keeps the symbol's own frame intact for everything that
    reads the symbol rather than the placement.
    """
    if rot == 0:
        return part
    f = ROT[rot]
    q = Part(part.d, part.at, 0, [(x, y, n) for x, y, n in part.pins],
             part.ext, part.value)
    q.pins = [(f(a, b)[0], f(a, b)[1], n) for a, b, n in part.pins]
    q.stub = [(f(sx, sy)[0], f(sx, sy)[1]) if (sx or sy) else (0, 0)
              for sx, sy in part.stub]
    return q


def _emit(drawn, parts, kind, form, hub, off, opts, origin, hrot=0,
          hub_row=None):
    """Turn band membership into coordinates, orient, then settle.

    A band is a cluster and it is placed as one. That is not tidiness for
    its own sake: the first version settled individual parts against each
    other, and a part pushed along its row takes the row with it -- the
    output chain came out with the inductor left of the diode and the
    divider's two legs swapped, because a coordinate decided and the
    coordinates were still moving. Resolving *inside* a band and then
    moving whole bands keeps every row a row.
    """
    rots, rowy = {}, {}

    # --- orientation, from the rows and the pin roles ---------------------
    for d, v in drawn.items():
        p = parts[d]
        if v[0] == "hub":
            rots[d] = hrot
            continue
        if v[0] == "bootstrap":
            rots[d] = inline_rot(p, kind)
            continue
        b = v[1]
        ns = [n for _, _, n in p.pins if n]
        gndn = [n for n in ns if kind.get(n) == "ground"]
        supn = [n for n in ns if kind.get(n) != "ground"]
        if len(ns) == 2 and gndn and supn:
            rots[d] = upright_rot(p, kind, supn[0])
        elif len(ns) == 2:
            rots[d] = inline_rot(p, kind)
        else:
            rots[d] = 0

    # --- local layout: one band at a time ---------------------------------
    def row_of(b, d):
        p = parts[d]
        ns = [n for _, _, n in p.pins if n]
        up = [n for n in ns if kind.get(n) != "ground"]
        if not up:
            return b.ys["top"]
        if all(n in b.rows["mid"] or n not in b.rows["top"] for n in up):
            return b.ys["mid"] if b.rows["mid"] else b.ys["top"]
        return b.ys["top"]

    local, orderb = {}, {}
    for d, v in drawn.items():
        if v[0] != "band":
            continue
        b, i = v[1], v[2]
        orderb.setdefault(id(b), []).append((i, d))
        rowy[d] = row_of(b, d)
        local[d] = (float(i + 1) * opts.pitch, float(rowy[d]))

    def lbox(d):
        # `Part.box`, not a second box model. This computed its own from
        # the symbol outline alone, so the in-band resolver measured a
        # transistor as 26 units wide where the tool that has to accept
        # the result measures 100 -- the outline plus two thirty-unit
        # leads. Two boxes, two answers, and the one that refused the
        # write was the one not being solved against.
        x, y = local[d]
        return parts[d].box(x, y, rots[d])

    for d, v in drawn.items():
        if v[0] != "hub":
            continue
        rowy[d] = float(opts.hub_drop)

    # A row inside a band: parts on it push each other apart, and nothing
    # else moves. Two parts on *different* rows of the same band are
    # stacked, which is what a shunt standing over a divider looks like.
    for b in [v[1] for v in drawn.values() if v[0] == "band"]:
        ds = [d for i, d in sorted(orderb.get(id(b), []))]
        for _ in range(80):
            moved = False
            for i in range(len(ds)):
                for j in range(i + 1, len(ds)):
                    a, c = ds[i], ds[j]
                    ba, bc = lbox(a), lbox(c)
                    # Boxes, not row numbers. Comparing `rowy` exactly
                    # skipped every pair whose rows differ by a unit, and
                    # a part that is not a line -- a three-pin transistor,
                    # an eight-pin LED -- spans several rows and was
                    # therefore compared with nobody. 58 overlapping pairs
                    # on the control sheet, 5 on input-and-pd, 2 on
                    # outputs, and `handplace` refused all three.
                    if not (ba[1] < bc[3] and bc[1] < ba[3]):
                        continue
                    if ba[0] < bc[2] and bc[0] < ba[2]:
                        push = ba[2] + opts.gap - bc[0]
                        if push > 0:
                            local[c] = (local[c][0] + push, local[c][1])
                            moved = True
            if not moved:
                break

    # --- band boxes, and where each band goes -----------------------------
    def bbox(ds):
        bs = [lbox(d) for d in ds]
        return (min(q[0] for q in bs), min(q[1] for q in bs),
                max(q[2] for q in bs), max(q[3] for q in bs))

    members = collections.defaultdict(list)
    for d, v in drawn.items():
        if v[0] == "band":
            members[id(v[1])].append(d)
    box = {k: bbox(v) for k, v in members.items()}
    origin_b = {}
    for d, v in drawn.items():
        if v[0] == "band":
            origin_b[id(v[1])] = (v[1].origin_x, v[1].off)

    # --- final coordinates ------------------------------------------------
    pos = {}
    for d, v in drawn.items():
        if v[0] == "hub":
            pos[d] = (0.0, float(opts.hub_drop) if hub_row is None
                      else hub_row)
        elif v[0] == "bootstrap":
            pos[d] = (0.0, (float(opts.hub_drop - opts.boot_gap)
                             if hub_row is None else hub_row - opts.boot_gap))
        else:
            ox, oy = origin_b[id(v[1])]
            lx, ly = local[d]
            pos[d] = (ox + lx * v[1].side, oy + ly)

    def anchor(d):
        x, y = pos[d]
        f = ROT[rots[d]]
        cs = [f(a, c) for a, c, _ in parts[d].pins]
        if not cs:
            return (int(round(x)), int(round(y)))
        return (int(round(x)), int(round(y - min(c for _, c in cs))))

    final = {d: anchor(d) for d in parts}

    def gbox(d):
        x, y = final[d]
        p = parts[d]
        e = p.ext or (0, 0, 0, 0)
        f = ROT[rots[d]]
        cs = [f(e[0], e[1]), f(e[2], e[1]), f(e[0], e[3]), f(e[2], e[3])]
        return (x + min(a for a, _ in cs) - 12, y + min(q for _, q in cs) - 12,
                x + max(a for a, _ in cs) + 12, y + max(q for _, q in cs) + 12)

    bandof = {d: v[1] for d, v in drawn.items() if v[0] == "band"}

    # Bands against each other. Only whole bands move here -- a part moved
    # on its own is the fault this function exists to avoid -- and the hub
    # never does, so everything settles away from it.
    # Every pair, not the first one found. Taking the first and starting
    # over meant a pair the loop could not move -- one of them the IC,
    # which never moves -- was found again on the next pass and again on
    # the one after, sixty times, and the loop then gave up *silently*
    # with every other overlap on the sheet still in place. That is the
    # fail-open shape this project warns about: the counter said nothing
    # and `handplace` refused the write one stage later. 58 overlapping
    # pairs on the control sheet were this one `continue`.
    seen = set()
    for _ in range(400):
        bx = {d: gbox(d) for d in parts}
        ks = sorted(parts, key=lambda d: (final[d][1], final[d][0]))
        got = None
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = bx[ks[i]], bx[ks[j]]
                if not (a[0] < b[2] and b[0] < a[2]
                        and a[1] < b[3] and b[1] < a[3]):
                    continue
                lo = ks[j] if final[ks[i]][1] < final[ks[j]][1] else ks[i]
                if lo == hub.d or (ks[i], ks[j]) in seen:
                    # The IC does not move, so the other one does; a pair
                    # already tried does not get a second identical push.
                    lo = ks[i] if lo == ks[j] else ks[j]
                    if lo == hub.d:
                        continue
                got = (ks[i], ks[j], lo)
                break
            if got:
                break
        if got is None:
            break
        a, b, lo = got
        seen.add((a, b))
        hi = a if lo == b else b
        push = bx[hi][3] + opts.gap - bx[lo][1]
        if push <= 0:
            continue
        # The *band* moves, not the part. A part moved on its own takes
        # its row with it -- and the row is what the whole design is
        # built on: resolving one collision part-by-part is what put the
        # inductor left of the diode and left the divider's legs swapped.
        move = {d for d, v in drawn.items()
                if v[0] == "band" and id(v[1]) == id(bandof.get(lo))}
        for d in (move or {lo}):
            final[d] = (final[d][0], final[d][1] + push)

    xs = [final[d][0] for d in parts]
    ys = [final[d][1] for d in parts]
    dx = origin[0] - min(xs)
    dy = origin[1] - min(ys)
    return {d: (int(grid(final[d][0] + dx, 0)[0]),
                int(grid(0, final[d][1] + dy)[1]), rots[d]) for d in parts}


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="the .epro2 from EasyEDA")
    ap.add_argument("netlist", type=Path)
    ap.add_argument("--design", type=Path,
                    help="design.yaml (default: beside the netlist)")
    ap.add_argument("-o", "--out", type=Path, default=Path("-"))
    ap.add_argument("--blocks", default="",
                    help="comma-separated block names; default is all of them")
    ap.add_argument("--gap", type=int, default=8,
                    help="clearance the sweep keeps between footprints")
    ap.add_argument("--pitch", type=int, default=55,
                    help="the column pitch: how far one step along the run is")
    ap.add_argument("--hub-drop", type=int, default=35,
                    help="how far the IC's anchor sits below its own row")
    ap.add_argument("--band-gap", type=int, default=90,
                    help="room between one band and the next")
    ap.add_argument("--boot-gap", type=int, default=45,
                    help="how far above the IC's anchor the bootstrap sits")
    ap.add_argument("--beside-gap", type=int, default=60,
                    help="room between a rail block and a load beside it")
    ap.add_argument("--block-gap", type=int, default=210,
                    help="room between one stacked block and the next")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", action="store_true",
                    help="print the forms and rows the engine decided")
    a = ap.parse_args()

    for q in (a.export, a.netlist):
        if not q.exists():
            print(f"error: {q} not found", file=sys.stderr)
            return 2

    global KINDS
    yml = a.design or a.netlist.with_name("design.yaml")
    KINDS = read_rail_kinds(yml)
    order = [r for r in read_rails(yml) if KINDS.get(r) != "ground"]

    design = json.loads(a.netlist.read_text(encoding="utf-8"))
    pinsof = {c["props"]["Designator"]: c["pins"] for c in design.values()}
    want = set(pinsof)
    blocks = read_blocks(a.netlist, want)
    keep = [b.strip() for b in a.blocks.split(",") if b.strip()] or None

    name, rows = load(a.export)
    geo = geometry(rows)
    missing = sorted(want - set(geo))
    if missing:
        print(f"error: {len(missing)} designator(s) are in the netlist and "
              f"not in the export: {', '.join(missing[:12])}",
              file=sys.stderr)
        return 2

    byname = collections.defaultdict(dict)
    for d in want:
        byname[blocks[d]][d] = geo[d]

    print(f"{a.export.name}: {len(want)} part(s) in "
          f"{len(byname)} block(s)")

    # A block that *makes* a rail goes in the chain; one that only draws
    # on a rail somebody else made goes beside that block rather than
    # below it. The fan header runs off the 5 V stage 1 makes, and a stage
    # below put it at the far end of the sheet from its own supply -- the
    # review that found it measured same-band reversals against the power
    # tree at 3 of 27 and took them to 0 by moving it.
    rank = {r: i for i, r in enumerate(order)}
    info = {}
    for bname, parts in byname.items():
        rs = [rank[n] for pr in parts.values() for _, _, n in pr.pins
              if n in rank]
        info[bname] = (min(rs), max(rs)) if rs else (-1, -1)
    made = [b for b in byname if info[b][1] > info[b][0]]
    drawn_on = [b for b in byname if b not in made]
    made.sort(key=lambda b: (info[b][0], b))

    def pins_box(placed):
        """The placed pins of a block, as a box.

        The *anchors* are not the drawing: a part hangs below its anchor by
        its own pin offset, and a ground symbol hangs below that. Laying
        blocks out on the anchors puts the next block's title on the
        previous block's ground symbols, which is what happened.
        """
        xs, ys = [], []
        for d, (x, y, r) in placed.items():
            f = ROT[r]
            for a, b, _n in geo[d].pins:
                dx, dy = f(a, b)
                xs.append(x + dx)
                ys.append(y + dy)
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)

    out, boxes = {}, {}
    first = set()
    second_top = None
    cursor = 0
    for bname in made:
        parts = {d: _clone(q) for d, q in byname[bname].items()}
        placed = plan_block(parts, KINDS, order, a, (0, cursor))
        if cursor:
            _pb = pins_box(placed)
            second_top = _pb[1] if second_top is None else second_top
        xs = [x for x, _, _ in placed.values()]
        ys = [y for _, y, _ in placed.values()]
        pb = pins_box(placed)
        boxes[bname] = (min(xs), min(ys), max(xs), max(ys))
        if cursor == 0:
            first |= set(parts)
            first_box = pb
        print(f"  {bname:28} {len(parts):3d} part(s)  "
              f"{max(xs) - min(xs):5d} x {max(ys) - min(ys):5d} units")
        out.update(placed)
        cursor = pb[3] + a.block_gap
    for bname in sorted(drawn_on, key=lambda b: (info[b][0], b)):
        parts = {d: _clone(q) for d, q in byname[bname].items()}
        want_rail = info[bname][0]
        host = None
        for m in made:
            if info[m][1] == want_rail:
                host = m
        if host is None and made:
            host = made[-1]
        if host is None:
            origin = (0, cursor)
        else:
            # Beside means *beside*: the fan header is one connector at the
            # end of the rail stage 1 makes, and the hand sheet puts it 36
            # units past the divider. `block_gap` is the room a stacked
            # block needs for its title, which is three times as much.
            bx = boxes[host]
            origin = (bx[2] + a.beside_gap, bx[1])
        placed = plan_block(parts, KINDS, order, a, origin)
        xs = [x for x, _, _ in placed.values()]
        ys = [y for _, y, _ in placed.values()]
        # The *pins*, not the anchors: the box is what the next block is
        # placed against, and an anchor is not where the drawing ends.
        boxes[bname] = pins_box(placed)
        if host is not None and info[host][0] == info[made[0]][0]:
            first |= set(parts)
        print(f"  {bname:28} {len(parts):3d} part(s)  "
              f"{max(xs) - min(xs):5d} x {max(ys) - min(ys):5d} units "
              f"(beside {host})")
        out.update(placed)

    lines = ["# Generated by tools/autoplace.py -- do not edit by hand."]
    # The rectangle the preview is cut to. It is the first block and
    # whatever is drawn beside it -- the block the hand sheet's own
    # reference picture shows -- measured from the parts rather than
    # copied from the hand sheet, because the automatic layout puts them
    # somewhere else and a fixed rectangle would crop the wrong drawing.
    if first:
        # The bottom stops above the *next* block's title rather than at a
        # fixed distance: the title of a block that is not in the picture
        # is worse than a tight margin, and how far below the first block
        # it lands is a property of the layout.
        bottom = first_box[3] + 45
        if second_top is not None:
            bottom = min(bottom, second_top - 110)
        lines.append("# crop %d,%d,%d,%d"
                     % (first_box[0] - 65, first_box[1] - 110,
                        first_box[2] + 420, bottom))
    for d, (x, y, r) in sorted(out.items(), key=lambda kv: _desig_key(kv[0])):
        spec = pad_spec(geo[d], (x, y), r)
        if spec:
            lines.append(f"# pin-stub {spec}")
            break
    lines += ["", "stub   *     5", ""]
    # The ground leads of a band do not all reach the same line, and it is
    # not the parts' fault: a standing part is anchored on the row it
    # starts from, and a 30-unit capacitor then hangs ten units less far
    # than a 40-unit one, so the two bands' ground leads end ten units
    # apart -- close enough to look like one line and too far for
    # `netflags --ground-bus` to see one, which leaves the compensation
    # network carrying a ground symbol of its own. The lead is the thing
    # that reaches the line, so the lead is what gets longer: `stub`
    # states a length by designator, and the alternative -- moving the
    # part -- puts its top pin into the band above and shorts four nets.
    leads = {}
    for d, (x, y, r) in sorted(out.items(), key=lambda kv: _desig_key(kv[0])):
        g = [n for _, _, n in geo[d].pins if n and KINDS.get(n) == "ground"]
        if not g or len(geo[d].pins) != 2:
            continue
        f = ROT[r]
        pts = [f(a, b) for a, b, _ in geo[d].pins]
        gy = y + max(b for _, b in pts)
        leads[d] = (gy, y, r, g[0])
    order = sorted(leads, key=lambda d: leads[d][0])
    i = 0
    while i < len(order):
        j = i
        while (j + 1 < len(order)
               and leads[order[j + 1]][0] - leads[order[j]][0] <= 15):
            j += 1
        if j > i:
            target = max(leads[order[k]][0] for k in range(i, j + 1)) + 5
            for k in range(i, j + 1):
                d = order[k]
                need = target - leads[d][0]
                if need > 5:
                    lines.append(f"stub   {d:5} {need:5d}")
        i = j + 1
    # The IC's own ground is *drawn*, and that is not decoration. Left to
    # a symbol it gets one beside the pin field -- and the flag's box sits
    # in the corridor the next pin needs. On this board the compensation
    # pin is at (265,85): right of it is the flag, ten below is the
    # feedback pin, and beyond the flag is the catch diode's body. The pin
    # is trapped, schwire reports "no clean path", and the compensation
    # network is drawn with no wire to the IC at all. `netflags` places no
    # symbol on a node a wire already reaches, so the wire is the fix, and
    # it is what the hand sheet draws.
    grounds = []
    joined = set()
    for _bn, _mem in sorted(byname.items()):
        _hub = max(_mem, key=lambda d: (len(geo[d].pins), d))
        # Only where the IC has *two* pins on the same rail. That is the
        # case the wire exists for: the two are one node, `netflags` gives
        # the node one symbol, and a symbol beside the pin field is what
        # traps the next pin. An IC with one ground pin is a different
        # case -- the hand sheet gives U5 its own symbol and U5's lead
        # ends above its own enable stub -- and wiring it instead drags a
        # lead the width of the block, straight across the enable run.
        _own = collections.Counter(
            n for _a, _b, n in geo[_hub].pins if n and KINDS.get(n) == "ground")
        if not any(c > 1 for c in _own.values()):
            continue
        for _px, _py, _net in geo[_hub].pins:
            if not _net or KINDS.get(_net) != "ground":
                continue
            hp = abs_pin(out, geo, _hub, (_px, _py))
            if hp is None:
                continue
            # The wire starts at the *lead's end*, not at the pin, and it
            # is not a detail: `netflags` puts a rail's symbol on the
            # lead's end, so a wire that stops at the pin leaves that end
            # free and the symbol is placed anyway -- still beside the pin
            # field, still in the corridor the next pin needs. The lead is
            # five units, because the engine says so on the same file.
            sd = (0, 0)
            for _i, _q in enumerate(geo[_hub].pins):
                if _q == (_px, _py, _net) and _i < len(geo[_hub].stub):
                    # Turned with the part: the lead direction is stored
                    # in the symbol's own frame, and a hub that is placed
                    # at 180 has it pointing the other way.
                    sd = ROT[out[_hub][2]](*geo[_hub].stub[_i])
                    break
            _nn = max(abs(sd[0]), abs(sd[1]), 1)
            se = (int(round(hp[0] + sd[0] * 5 / _nn)),
                  int(round(hp[1] + sd[1] * 5 / _nn)))
            if se != hp:
                hp = se
            best = None
            for e in sorted(out, key=_desig_key):
                for ax, ay, an in geo[e].pins:
                    if an != _net or (e == _hub and (ax, ay) == (_px, _py)):
                        continue
                    qp = abs_pin(out, geo, e, (ax, ay))
                    if qp is None:
                        continue
                    # The IC's *own* other ground pin wins, and it is not
                    # a tie-break: joining the two costs a short wire
                    # beside the package, where joining either to a
                    # neighbour crosses the corridor between the pin
                    # column and the catch diode -- and two wires doing
                    # that box the switch pin in and it stops being drawn.
                    gap = (abs(hp[0] - qp[0]) + abs(hp[1] - qp[1])) // (
                        4 if e == _hub else 1)
                    if best is None or gap < best[0]:
                        best = (gap, qp, e)
            if best is None or best[0] < 5:
                continue
            # A pin already joined is joined. Two ground pins of one IC
            # wire to each other, and the second pass would draw the same
            # wire backwards.
            if (hp, best[1]) in joined or (best[1], hp) in joined:
                continue
            tx, ty = best[1]
            # A step *out of the pin row* before turning, and it is the
            # whole of the difficulty. The IC's four right-hand pins have
            # leads five units long and their ends ten apart, so a route
            # that leaves the row vertically passes through the next lead
            # end -- `handplace` refuses it, correctly. One step along the
            # lead's own direction clears the row and every shape after it
            # is clean.
            # Inward, not outward. The leads of an IC's right-hand pins
            # all point the same way, and stepping *along* them puts the
            # wire in the corridor the next net needs -- SW_5V lost its
            # route the first time this was tried. Stepping back toward
            # the package keeps the whole wire on the package's own side
            # of the pin column, where nothing else wants to be.
            ax_, ay_ = out[_hub][0], out[_hub][1]
            ux = (sd[0] > 0) - (sd[0] < 0)
            uy = (sd[1] > 0) - (sd[1] < 0)
            if ux and (ax_ - _px) * ux > 0:
                ux = -ux
            elif uy and (ay_ - _py) * uy > 0:
                uy = -uy
            # The step is the wire's *second* point, never its first: an
            # end has to land on the net, and fifteen units past the lead
            # is not on it. `handplace` refused four wires that did it the
            # other way, which is the check doing its job.
            jog = (hp[0] + ux * 15, hp[1] + uy * 15) if (ux or uy) else hp
            midx, midy = (hp[0] + tx) // 2, (hp[1] + ty) // 2
            # Arriving along the neighbour's *row* comes first: the
            # right-then-up L ends by running up the neighbour's own pin
            # column, and a wire lying along that column is in every other
            # net's way.
            for pts in ((hp, jog, (jog[0], ty), best[1]),
                        (hp, jog, (midx, jog[1]), (midx, ty), best[1]),
                        (hp, jog, (jog[0], midy), (tx, midy), best[1]),
                        (hp, jog, (tx, jog[1]), best[1]),
                        (hp, (hp[0], ty), best[1]),
                        (hp, (midx, hp[1]), (midx, ty), best[1]),
                        (hp, (hp[0], midy), (tx, midy), best[1]),
                        (hp, (tx, hp[1]), best[1])):
                if route_clear(geo, out, pts, {_hub, best[2]}, _net):
                    grounds.append((_net, pts))
                    joined.add((hp, best[1]))
                    break
    for _net, pts in grounds:
        lines.append("wire    %-10s %s"
                     % (_net, "  ".join("%d,%d" % q for q in pts)))
    lines += ["", "# designator  x     y    rotation"]
    for d in sorted(out, key=_desig_key):
        x, y, r = out[d]
        lines.append(f"{d:6} {x:5d} {y:5d} {r:5d}")
    text = "\n".join(lines) + "\n"
    if a.dry_run or str(a.out) == "-":
        sys.stdout.write(text)
        return 0
    a.out.write_text(text, encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0


def abs_pin(placed, geo, d, local):
    """Where a part's symbol-local pin ended up, or None."""
    if d not in placed:
        return None
    x, y, r = placed[d]
    px, py = ROT[r](local[0], local[1])
    return (int(round(x + px)), int(round(y + py)))


def _on_route(pts, q):
    for u, v in zip(pts, pts[1:]):
        if (min(u[0], v[0]) <= q[0] <= max(u[0], v[0])
                and min(u[1], v[1]) <= q[1] <= max(u[1], v[1])):
            return True
    return False


def route_clear(geo, placed, pts, ends, net=None):
    """Is this route clear of every pin and every body but the two it joins?

    The check `handplace.py` makes, made here first: a wire that lands on
    a stranger's pin is a short and the build refuses to write, and a wire
    across a symbol is the first thing a reader notices. The pins of the
    two parts are checked *too* -- only the two the wire ends on are
    excused; excusing both parts whole let a wire run down the IC's own
    pin column and across its switch; pin, which `handplace` refused one
    stage later.
    """
    a, b = pts[0], pts[-1]
    for d in placed:
        for ax, ay, _n in geo[d].pins:
            q = abs_pin(placed, geo, d, (ax, ay))
            if q is None or q in (a, b) or _n == net:
                # A pin on the wire's *own* net is a junction, not a
                # short -- and passing through one is how the wire gets
                # back inside the pin row.
                continue
            if _on_route(pts, q):
                return False
        if d in ends:
            continue
        box = geo[d].box(placed[d][0], placed[d][1], placed[d][2])
        for u, v in zip(pts, pts[1:]):
            if (box[0] < max(u[0], v[0]) and min(u[0], v[0]) < box[2]
                    and box[1] < max(u[1], v[1]) and min(u[1], v[1]) < box[3]):
                return False
    return True


def order_of(bname, order, members):
    """Where a block sits in the power flow, for stacking order."""
    ranks = []
    for p in members.values():
        for _, _, n in p.pins:
            if n in order:
                ranks.append(order.index(n))
    return min(ranks) if ranks else len(order)


def _clone(p):
    q = Part(p.d, p.at, p.rot, list(p.pins), p.ext, p.value)
    return q


def _desig_key(d):
    m = re.match(r"^([A-Za-z]+)(\d+)$", d)
    return (m.group(1), int(m.group(2))) if m else (d, 0)


if __name__ == "__main__":
    sys.exit(main())
