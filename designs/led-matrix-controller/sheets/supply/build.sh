#!/bin/sh
# Build the hand-placed supply sheet, from the imported raw export to a
# preview PNG, and print the counters that say whether it is any good.
#
# The hand sheet is not the importer's grid: `placement.txt` carries the
# coordinates a person chose, and the chain here is what turns those into
# a titled, wired, standardised sheet. It exists as a file because the
# alternative is a shell history nobody can re-run, which is what this
# chain was until it produced a sheet worth regenerating.
#
#   nix-shell --run 'designs/led-matrix-controller/sheets/supply/build.sh'
#
# $RAW is the EasyEDA import of `netlist.json` -- re-import that file and
# export, then point RAW at the result. It is deliberately not committed:
# it is a round trip through EasyEDA and the toolchain's output, not a
# source. Every stage after it is derived from `placement.txt` and
# `netlist.json`, both of which are tracked.
#
# **A part added to the netlist needs that round trip before this can run.**
# `handplace.py` refuses a designator the export does not carry, by name,
# and writes nothing -- so the failure is loud and leaves the last good
# artefact in $SCRATCH. Adding a part without re-importing is therefore not
# a silent divergence: it stops the chain. That is why the 3V3
# feed-forward capacitor in README.md's "Waits for the next import" is
# recorded there rather than wired in.
#
# A stage that changes an existing record deletes its own output rather
# than writing a file whose diff nobody can explain, so a failure here
# leaves `$SCRATCH` with the last good artefact in it.
set -e

D=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$D/../../../.." && pwd)
cd "$ROOT"

RAW=${RAW:-/tmp/hand-supply-raw.epro2}
OUT=${1:-$D/../../figures/supply-sheet-handplaced.png}
# Which coordinates to place. The hand sheet is the default, and the
# automatic one is the same chain with `PLACEMENT` pointed at what
# `tools/autoplace.py` wrote -- which is what makes the two comparable:
# the flags, the wires and the frame are identical, so a difference
# between the two pictures is a difference in placement and nothing else.
PLACEMENT=${PLACEMENT:-$D/placement.txt}
# The one lead the importer aims along its own package rather than out of
# it -- U4's thermal pad -- because a ground symbol on the end of a lead
# that runs along the IC lands inside the IC. `autoplace.py` writes the
# coordinate it placed the pad at, as a comment on the placement it
# emitted, so the automatic sheet does not depend on where the hand sheet
# happened to put U4. A placement without that comment, which is the hand
# one, falls back to the coordinate of the hand sheet.
PAD=$(sed -n 's/^# pin-stub //p' "$PLACEMENT" | head -1)
PAD=${PAD:-GND@330,90}
# And the rectangle the preview is cut to, for the same reason: the
# automatic layout puts the same block somewhere else, and a fixed crop
# would photograph the wrong part of the sheet.
CROP=$(sed -n 's/^# crop //p' "$PLACEMENT" | head -1)
CROP=${CROP:-100,-70,900,300}
SCRATCH=${SCRATCH:-/tmp/lmc-supply-build}
mkdir -p "$SCRATCH"
S="$SCRATCH"

if [ ! -f "$RAW" ]; then
    echo "error: no raw import at $RAW" >&2
    echo "       import $D/netlist.json into EasyEDA, export it, and set" >&2
    echo "       RAW=/path/to/export.epro2" >&2
    exit 2
fi

# 1. Place the parts where placement.txt says, and nothing else.
python3 tools/handplace.py "$RAW" "$PLACEMENT" \
    -o "$S/1-placed.epro2" > "$S/1.log"

# 2. Rail flags. One per node, so a rail that is one node on the sheet is
#    not flagged end to end; the 60-unit lead is the shortest that keeps
#    the flag clear of the pin it lands on. Ground also gets a bus: the
#    parts of a stage drop their ground leads to a common height, and one
#    line joins them so one symbol marks the node instead of one per part.
#    220 units clears the 140 the 5 V row needs and stops well short of
#    the 375 that would run C12's ground across a stage to C13's.
#
#    The two feedback nets are flagged for the opposite reason: each is
#    *two* nodes far apart -- U4's pin and the R25/R26 divider, U5's and
#    R28/R29 -- and schwire draws a net it cannot draw locally from end to
#    end. Measured with these two left out of --nets, FB_5V comes out 1690
#    units and FB_3V3 895; FB_5V crosses the 5 V rail row getting there,
#    and BUS_5V pays for the detour round it, 825 units against 565 with
#    the flags on. A flag at each node leaves only the wire joining a
#    divider's own two pins -- 175 units and 160 -- and BUS_5V comes down
#    to 565. This is the idiom the net wants anyway -- a sense net is
#    labelled, not drawn the length of the sheet. The +5V bar is borrowed
#    for its shape only: the flag draws `Global Net Name`, so it reads
#    FB_5V, the same way BUS_3V3 and VBUS borrow it and read their own
#    names.
#    FB_5V's flag also has to leave the pin row rather than run along it.
#    A straight lead keeps the flag's bar in the row at y=140, and the
#    corridor down to C7 -- which COMP_5V has to reach -- comes down
#    between that pin's lead and the catch diode's body. A lead beside the
#    pin walls that corridor, and COMP_5V comes back "no clean path";
#    `--lead-turn` sends the lead square off the stub end instead, so the
#    flag sits below the row and the corridor stays open.
#
#    U4's thermal pad gets its lead re-aimed. The importer draws a lead
#    by the pin's side, which is right for the eight pins along U4's edges
#    and wrong for the one on its top -- a vertical pin whose lead came
#    out sideways, thirty units along the package. The symbol then has
#    nowhere to sit that is not to the side of the pin it belongs to, and
#    a ground triangle rooted at the stub end draws its bars into the IC.
#    Leaving the pad as a reported gap was the honest answer to that for
#    several rounds; re-aiming the lead is the better one, because the
#    lead is *moved* and not dropped, so the node's `NET` attribute and
#    `epro.py diff`'s one-per-pin count both survive untouched. The pad
#    then reads as one line from the pin up to the bars.
#
#    The flag lead is ten units, not the sixty it was. Sixty was measured
#    when the parts stood far apart and a short lead left three labels
#    reading as ambiguous; the row has since closed up by more than half,
#    so the measurement is stale and was re-taken. Swept from eight to
#    sixty on the compact layout: every length reports zero ambiguous
#    labels, zero labels on a foreign wire and no text overlap, so the
#    shortest is taken. A shorter lead is what closes the space between
#    FB_5V's flag and the COMP_5V run beside it.
#
#    The divider's flag lands on R26's own stub end rather than R25's,
#    because --one-per-node groups the two and takes the better-ranked of
#    them. Both are the tap between the resistors; the wire reaches R25's
#    pin either way, and its own stub goes dead and is dropped with the
#    other twenty-five.
python3 tools/netflags.py "$S/1-placed.epro2" \
    --donor 'designs/led-matrix-controller/reference/Module V3 schematic_2026-10-04.epro2' \
    --nets 'GND,BUS_5V=+5V,BUS_3V3=+5V,VBUS=+5V,FB_5V=+5V,FB_3V3=+5V' \
    --one-per-part --one-per-node 200 --strict-flags \
    --flag-clearance 15 --flag-lead 10 --ground-bus 220 \
    --lead-turn FB_5V --pin-stub "$PAD" \
    -o "$S/2-flagged.epro2" > "$S/2.log"

# 3. Draw the local wires. 200 units is the node threshold, and it is the
#    same number netflags was given above: the two tools have to agree on
#    what one node is, or one draws a rail the other has flagged in pieces.
python3 tools/schwire.py "$S/2-flagged.epro2" "$D/netlist.json" \
    --max-span 1200 --detour 45 --clearance 3 --end-clearance 4 \
    --labels-outward --skip-flagged --local-rail 200 \
    -o "$S/3-wired.epro2" > "$S/3.log"

# 4. The real LCSC number and the device's own attributes on every
#    placement, which is what Device Standardization would do by hand.
python3 tools/fixparts.py "$S/3-wired.epro2" --complete \
    -o "$S/4-standard.epro2" > "$S/4.log"

# 5. The frame: title block, zone grid, block titles, cross-sheet list.
python3 tools/blocktitles.py "$S/4-standard.epro2" "$D/netlist.json" \
    --sheet-title 'LED Matrix Controller — Supply' --doc-number LMC-SCH-001 \
    --organisation 'lucsoft' --doc-type 'Schematic diagram' \
    --sheet '2 of 4' --revision A --date 2026-10-11 \
    --drawn-by 'pcb-design toolchain' \
    --line-step 20 --note-width 26 --note-dx 60 \
    --iso-sheet --sheet-size A3 \
    --xref-sheets designs/led-matrix-controller/sheets --this-sheet supply \
    -o "$S/5-titled.epro2" > "$S/5.log"

# 6. Prove the nets, then look at it.
python3 tools/epro.py diff "$S/5-titled.epro2" "$D/netlist.json" > "$S/6.log"

# The preview is the 36 V -> 5 V block and the fan header, cut out of the
# sheet rather than the whole of it. The 5 V -> 3.3 V stage is real and
# stays in the netlist, the placement and the ERC; it is only this picture
# that leaves it out. What the picture is for is the reference an automatic
# placement is judged against, and the top block is the one that reference
# is being built from. The rectangle runs from above the block title down
# to below the stage's ground row and stops short of the cross-sheet list,
# which sits in the band between the two stages. `--region` measures in
# canvas units, where one unit is 10 mil.
python3 tools/schpreview.py "$S/5-titled.epro2" -o "$OUT" --dpi 200 \
    --region="$CROP" > /dev/null

printf 'sheet      %s\n' "$(grep -oE '[0-9]+ x [0-9]+ units = .*' "$S/1.log" | head -1)"
printf 'paper      %s\n' "$(grep -oE '[0-9]+ x [0-9]+ mm of content' "$S/5.log" | tail -1)"
printf 'frame      %s\n' "$(grep -oE '[0-9]+ do-not-fit line[^,]*|[0-9]+ block title\(s\)[^,]*|[0-9]+-line title block[^,]*' "$S/5.log" | tr '\n' ';')"
printf 'nets       %s\n' "$(grep -oE '[0-9]+ net\(s\) routed' "$S/3.log")"
printf 'ground bus %s\n' "$(grep -oE '[0-9]+ ground run\(s\)[^,]*' "$S/2.log" | head -1)"
printf 'dead stubs %s\n' "$(grep -oE '[0-9]+ +stub\(s\) dropped[^,]*' "$S/3.log" | head -1)"
printf 'on a wire  %s\n' "$(grep -oE '[0-9]+ net label\(s\) lie on a wire[^.]*' "$S/3.log" | head -1)"
printf 'ambiguous  %s\n' "$(grep -oE '[0-9]+ net label\(s\) sit nearer[^-]*' "$S/3.log" | head -1)"
printf 'text/text  %s\n' "$(grep -oE '[0-9]+ pair\(s\) of visible text overlap' "$S/3.log")"
printf 'flags      %s overlap(s), %s bar(s) on a foreign stub\n' \
    "$(grep -cE '^  CLASH' "$S/2.log")" "$(grep -cE '^  TIP' "$S/2.log")"
# A pin on a flagged net that no symbol reaches. U4's thermal pad is the
# one on this sheet, and it is left that way on purpose: the pad's stub
# points into the package, so a symbol at its end lands in the middle of
# the IC and the lead that would carry it clear has nowhere to go. It is
# counted here so it stays a known number rather than a silence.
printf 'bare pins  %s\n' \
    "$(grep -oE '[0-9]+ pin\(s\) on a flagged net' "$S/2.log" | head -1 \
       || echo '0 pin(s) on a flagged net')"
printf 'netdiff    %s\n' "$(tail -2 "$S/6.log" | head -1 | sed 's/^ *//')"
python3 tools/layoutmetrics.py "$S/5-titled.epro2" "$D/netlist.json" \
    --design "$D/design.yaml" | tail -n +3 | grep -E 'detour|span|reversal|enclosed'
