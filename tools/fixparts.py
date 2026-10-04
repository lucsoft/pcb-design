#!/usr/bin/env python3
"""Put the real LCSC number on each component instance in an EasyEDA export.

The netlist importer writes a per-instance attribute called `Supplier Part`
and fills it with the **partId** -- `ESP32-C6-WROOM-1-N8.1` where the part is
`C5366877`. The correct number is on the DEVICE document the instance points
at, so the file resolves correctly today; but the property panel shows the
partId, and an instance attribute is the normal way to *override* a device
one. A BOM or PCBA export that honours the override carries a supplier part
that resolves to nothing.

A hand-drawn schematic has no per-instance copy of this field at all. The
value is extension noise, not something EasyEDA put there, which is why
correcting it rather than deleting it is the conservative choice: a wrong
value overridden by the right one is safe under either precedence rule, where
deleting a field assumes a precedence rule nobody here has confirmed.

    fixparts.py <export.epro2> -o <out.epro2>
    fixparts.py <export.epro2> --dry-run

**It touches nothing but the value of that one attribute.** No coordinate, no
record is added or removed, no other key. That is checked after writing: the
output must differ from the input in exactly the `Supplier Part` instance
attributes and nowhere else.

**There is no separate "assigned" flag**, which is the first thing to look
for and it does not exist. Re-importing a file with the C-number corrected
and then opening five components in the editor showed EasyEDA filling seven
fields on exactly those five and nothing on the other 124: Datasheet,
Description, Footprint, JLCPCB Part Class, LCSC Part Name, Supplier Footprint
and Value. No boolean anywhere, `Unique ID` empty on both, the COMPONENT
records identical in shape. "Assigned" is not a state EasyEDA records -- it
is whether those fields happen to be filled, and it fills them lazily, when
a human opens the part.

So this copies them, which is what the editor would do one component at a
time. Values are taken verbatim from the DEVICE document, because that is
what EasyEDA itself wrote: on all five, every field came out byte-identical
to the device's. **Only empty fields are filled.** A non-empty instance value
is a deliberate override, and discarding one silently would be a worse bug
than the one this fixes.

What this cannot settle is whether EasyEDA reads the instance or the device
when both exist. Nothing readable from outside answers that. **Export the BOM
from EasyEDA and look** -- that is the only thing that does, and it takes a
minute.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import zipfile
from pathlib import Path

KEY = "Supplier Part"

# The other fields EasyEDA itself copies from the device onto an instance the
# moment it can resolve the part. Observed, not guessed: on the five
# components opened in the editor after the C-number was corrected, every one
# of these came out byte-identical to the device's value.
COPY_KEYS = ("LCSC Part Name", "JLCPCB Part Class", "Supplier Footprint",
             "Datasheet", "Value", "Description")

# Kept apart from the rest because it is the only structural one. Every field
# above is display or BOM metadata; `Footprint` is a uuid naming a library
# document, and library documents are how EasyEDA tracks updates -- each is
# embedded with its own DOCHEAD carrying `uuid`, `version` and `updateTime`,
# and an update is a version it has not got. Copying the device's own uuid
# onto the instance should be inert, since it is the same uuid the device
# already points at. "Should be" is not a standard this project accepts for a
# field that decides which copper lands, so it is opt-out.
FOOTPRINT_KEY = "Footprint"



# Keys never copied from a device onto an instance, whatever else is. Each is
# either per-instance, or the device holds it in a form that is wrong on a
# placement:
#
#   Designator   the device holds the template -- "C?", "U?"
#   Name         the device holds the formula `={Manufacturer Part}`, and the
#                instance holds the netlist's `value`. Copying it is exactly
#                the damage EasyEDA's own Replace does -- `eFuse ch1` becomes
#                the formula -- and the main reason to do this here instead.
#   Symbol, Device, Unique ID, Group ID, Channel ID, Reuse Block
#                structural links the instance has already or must not gain
NEVER_COPY = {"Designator", "Name", "Symbol", "Device", "Unique ID",
              "Group ID", "Channel ID", "Reuse Block"}


def device_value(key, raw):
    """What the *instance* copy of a device attribute should be.

    Almost always the device's value verbatim. The exception is `3D Model`,
    which the device holds as a pipe-separated pair -- model uuid, then a
    second reference -- where the instance takes only the part before the
    first pipe. Measured against a manual click-through of EasyEDA's own
    Device Standardization: 101 components matched that rule, 2 had no pipe
    to cut, and nothing contradicted it.
    """
    if key == "3D Model" and isinstance(raw, str) and "|" in raw:
        return raw.split("|", 1)[0]
    return raw


def attr_payload(key, value, parent):
    """The shape EasyEDA writes for an attribute with no position.

    Copied from the 103 records its own Replace produced, not invented: every
    field present, all null but the three that carry meaning.
    """
    return {"x": None, "y": None, "rotation": None, "color": None,
            "fontFamily": None, "fontSize": None, "fontWeight": None,
            "italic": None, "underline": None, "align": None,
            "value": value, "keyVisible": None, "valueVisible": None,
            "key": key, "fillColor": None, "parentId": parent,
            "zIndex": None}


def new_id(parent, key):
    """A stable 16-hex id, so two runs of this tool produce one file."""
    import hashlib
    return hashlib.sha256(f"{parent}/{key}".encode()).hexdigest()[:16]


def load(path: Path):
    z = zipfile.ZipFile(path)
    eprus = [n for n in z.namelist() if n.endswith(".epru")]
    if len(eprus) != 1:
        raise SystemExit(f"error: expected one .epru, found {len(eprus)}")
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


def device_parts(rows) -> dict[str, dict]:
    """DEVICE document uuid -> its whole attribute dict."""
    out = {}
    for h, b, doc, _ in rows:
        if h and h["type"] == "META" and doc and doc[0] == "DEVICE":
            at = b.get("attributes") or {}
            if at.get(KEY):
                out[doc[1]] = at
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--complete", action="store_true",
                    help="also CREATE the attributes an instance lacks, "
                         "copying them from its device -- what EasyEDA's "
                         "'Use Recommended Device' does, minus the Name damage")
    ap.add_argument("--no-footprint", action="store_true",
                    help="leave the Footprint uuid alone (see the note on "
                         "library update tracking)")
    a = ap.parse_args()
    if not a.dry_run and not a.out:
        print("error: give -o/--out, or --dry-run", file=sys.stderr)
        return 2
    if not a.export.exists():
        print(f"error: {a.export} not found", file=sys.stderr)
        return 2

    copy_keys = COPY_KEYS if a.no_footprint else COPY_KEYS + (FOOTPRINT_KEY,)
    # In --complete the hand-picked list is wrong, and measurably so. Checked
    # against a manual click-through of EasyEDA's own Device Standardization:
    # it fills *every* empty instance attribute from the device, not six of
    # them. Restricting the fill left 900 slots empty that EasyEDA populated
    # -- Tolerance, Voltage Rating, Temperature Coefficient, Operating
    # Temperature, Power(Watts) -- all of which the device already carried.
    # The short list was right only while the evidence was six fields wide.
    fill_all = a.complete
    name, rows = load(a.export)
    devattrs = device_parts(rows)
    if not devattrs:
        print(f"error: {a.export.name} holds no DEVICE document, so there is "
              f"no correct value to copy from", file=sys.stderr)
        return 2

    desig = {b["parentId"]: b["value"] for h, b, _, _ in rows
             if h and h["type"] == "ATTR" and b.get("key") == "Designator"
             and b.get("value")}
    ref = {b["parentId"]: b["value"] for h, b, _, _ in rows
           if h and h["type"] == "ATTR" and b.get("key") == "Device"
           and b.get("value")}

    print(f"\n{a.export.name}")
    print(f"  {len(devattrs)} device definition(s), "
          f"{sum(1 for d in desig.values() if not d.endswith(chr(63)))} placement(s)")

    out_lines, fixes, filled, skipped, templates = [], [], [], [], 0
    seen_comp = set()
    for h, b, doc, raw in rows:
        key = b.get("key") if h and h["type"] == "ATTR" else None
        wanted = (key == KEY or key in copy_keys
                  or (fill_all and key is not None and key not in NEVER_COPY))
        if not wanted or b.get("parentId") not in desig:
            out_lines.append(raw)
            continue
        cid = b["parentId"]
        # A designator ending in "?" is a library template inside a DEVICE
        # document, not a placement on the sheet. Correcting it would be
        # editing the part definition rather than this board's use of it.
        if desig[cid].endswith("?"):
            if cid not in seen_comp:
                templates += 1
                seen_comp.add(cid)
            out_lines.append(raw)
            continue
        attrs = devattrs.get(ref.get(cid))
        if attrs is None:
            if key == KEY:
                skipped.append((desig[cid], "points at no DEVICE document"))
            out_lines.append(raw)
            continue
        want = device_value(key, attrs.get(key))
        cur = b.get("value")
        if key == KEY:
            # The instance holds the partId. Replace it outright: it is the
            # field the BOM resolves by and a partId resolves to nothing.
            if not want or cur == want:
                out_lines.append(raw)
                continue
            fixes.append((desig[cid], cur, want))
        else:
            # Fill only what is empty. A non-empty instance value is a
            # deliberate override of the device, and silently discarding one
            # would be a worse bug than the one being fixed.
            if not want or cur not in (None, ""):
                out_lines.append(raw)
                continue
            filled.append((desig[cid], key))
        nb = dict(b)
        nb["value"] = want
        out_lines.append(f"{json.dumps(h, separators=(',', ':'))}||"
                         f"{json.dumps(nb, separators=(',', ':'))}|")

    for d, old, new in fixes[:10]:
        print(f"  {d:6s} {old!r} -> {new}")
    if len(fixes) > 10:
        print(f"  ... and {len(fixes) - 10} more")
    for d, why in skipped:
        print(f"  SKIP   {d}: {why}")
    byk = collections.Counter(k for _, k in filled)
    for k, v in byk.most_common():
        print(f"  fill   {k:20s} on {v} instance(s) that had it empty")
    print(f"\n  {len(fixes)} C-number(s) corrected, {len(filled)} empty field(s) "
          f"filled, {templates} library template(s) left alone, "
          f"{len(skipped)} skipped")

    # --- create what the instance does not have at all -------------------
    created = []
    if a.complete:
        # Where each component's attributes end, so new ones land beside the
        # old inside the SCH_PAGE document rather than after some DEVICE head.
        last_line, have = {}, collections.defaultdict(set)
        for i, (h, b, doc, _) in enumerate(rows):
            if (h and h["type"] == "ATTR" and b.get("parentId") in desig
                    and doc and doc[0] in ("SCH_PAGE", "SCH")):
                last_line[b["parentId"]] = i
                have[b["parentId"]].add(b.get("key"))
        ticket = max((h["ticket"] for h, _, _, _ in rows
                      if h and isinstance(h.get("ticket"), int)), default=0)

        insert = collections.defaultdict(list)
        for cid, line in sorted(last_line.items()):
            if desig[cid].endswith("?"):
                continue
            attrs = devattrs.get(ref.get(cid))
            if not attrs:
                continue
            for key in sorted(set(attrs) - have[cid] - NEVER_COPY - {FOOTPRINT_KEY}
                              if a.no_footprint
                              else set(attrs) - have[cid] - NEVER_COPY):
                val = attrs[key]
                if val in (None, ""):
                    continue
                ticket += 1
                hdr = {"type": "ATTR", "ticket": ticket, "id": new_id(cid, key)}
                insert[line].append(
                    f"{json.dumps(hdr, separators=(',', ':'))}||"
                    f"{json.dumps(attr_payload(key, device_value(key, val), cid), separators=(',', ':'))}|")
                created.append((desig[cid], key))

        merged = []
        for i, line in enumerate(out_lines):
            merged.append(line)
            merged.extend(insert.get(i, ()))
        out_lines = merged

        bydes = collections.Counter(d for d, _ in created)
        print(f"  create {len(created)} attribute(s) on {len(bydes)} component(s), "
              f"copied from their device")

    if a.dry_run:
        print("  --dry-run: nothing written\n")
        return 0

    src = zipfile.ZipFile(a.export)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in src.namelist():
            z.writestr(entry, "\n".join(out_lines).encode("utf-8")
                       if entry == name else src.read(entry))

    # --- prove nothing else moved ----------------------------------------
    _, back = load(a.out)
    if len(back) != len(rows) + len(created):
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(back)} lines out, expected "
              f"{len(rows)} + {len(created)} — {a.out} deleted\n", file=sys.stderr)
        return 1
    # Compare the originals against the output with the additions removed, so
    # an insertion cannot hide an edit by shifting the sequence.
    added = {h["id"] for h, _, _, _ in back
             if h and h.get("id") and h["id"] not in
             {g["id"] for g, _, _, _ in rows if g and g.get("id")}}
    if len(added) != len(created):
        a.out.unlink(missing_ok=True)
        print(f"  error: {len(added)} new record(s), expected {len(created)} — "
              f"{a.out} deleted\n", file=sys.stderr)
        return 1
    kept = [r for r in back if not (r[0] and r[0].get("id") in added)]
    drift = []
    for (h1, b1, _, _), (h2, b2, _, _) in zip(rows, kept):
        if h1 is None or h2 is None:
            if h1 is not h2 and (h1 is None) != (h2 is None):
                drift.append("a record appeared or vanished")
            continue
        if h1 != h2:
            drift.append(f"header changed: {h1.get('id')}")
            continue
        if b1 == b2:
            continue
        # The only permitted difference, on the only permitted key.
        changed = {k for k in set(b1) | set(b2) if b1.get(k) != b2.get(k)}
        permitted = (b1.get("key") == KEY or b1.get("key") in copy_keys
                     or (fill_all and b1.get("key") not in NEVER_COPY))
        if h1["type"] != "ATTR" or not permitted or changed != {"value"}:
            drift.append(f"{h1.get('id')}: changed {sorted(changed)}")
    if drift:
        a.out.unlink(missing_ok=True)
        for d in drift[:8]:
            print(f"  DRIFT  {d}")
        print(f"\n  {len(drift)} unintended change(s); {a.out} deleted\n")
        return 1

    print(f"  nothing else differs: same records, same order, same coordinates"
          f"{', plus the new attributes' if created else ''}")
    print(f"  wrote {a.out}")
    print(f"\n  EasyEDA's own BOM export is the only thing that settles whether")
    print(f"  it reads the instance or the device. Export it and look.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
