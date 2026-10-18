#!/bin/sh
# Build the automatically placed supply sheet, and print it beside the
# hand-placed one.
#
#   nix-shell --run 'designs/led-matrix-controller/sheets/supply/autoplace.sh'
#
# This is the whole comparison, and it is short on purpose. `build.sh`
# takes the coordinates it places from `PLACEMENT`, and everything after
# that -- the flags, the ground buses, the drawn wires, the LCSC numbers,
# the frame and the cross-sheet list -- is the same chain with the same
# arguments for both. So a difference between the two pictures is a
# difference in *placement* and in nothing else, which is the only way a
# comparison of two schematics can mean anything.
#
# `placement-auto.txt` is committed. It is generated, and the convention
# here is to commit a generator's output as well as the generator --
# `netlist.py` and `netlist.json` are the same pair -- so that a change
# in the engine shows up as a diff in the thing it placed rather than only
# as a change in the code that placed it.
set -e

D=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$D/../../../.." && pwd)
cd "$ROOT"

RAW=${RAW:-/tmp/hand-supply-raw.epro2}
AUTO=$D/placement-auto.txt
SHOT=$D/../../figures

# 1. The engine. It reads the raw import for symbol geometry -- which pin
#    of a part carries which net, how long its lead is, how big its
#    outline is -- and writes coordinates in the form `handplace.py`
#    already consumes.
python3 tools/autoplace.py "$RAW" "$D/netlist.json" -o "$AUTO"

# 2. Both sheets, through the same chain, cropped to the same rectangle.
PLACEMENT="$AUTO" SCRATCH=${SCRATCH:-/tmp/lmc-auto-build} \
    "$D/build.sh" "$SHOT/supply-sheet-auto.png" > /tmp/lmc-auto-counters.txt
SCRATCH=${SCRATCH_HAND:-/tmp/lmc-supply-build} \
    "$D/build.sh" "$SHOT/supply-sheet-handplaced.png" > /tmp/lmc-hand-counters.txt

# 3. The two pictures one above the other, which is what the review
#    grades. Same crop of the same block out of both, so the only thing
#    that differs between the two halves is where the engine put things.
python3 - "$SHOT/supply-sheet-handplaced.png" "$SHOT/supply-sheet-auto.png" \
    "$SHOT/supply-sheet-compare.png" <<'PY'
import sys
from PIL import Image, ImageDraw
hand = Image.open(sys.argv[1]).convert("RGB")
auto = Image.open(sys.argv[2]).convert("RGB")
out = sys.argv[3]
w = max(hand.width, auto.width)
gap, band = 40, 34
img = Image.new("RGB", (w, hand.height + auto.height + gap + 2 * band), "white")
d = ImageDraw.Draw(img)
d.text((10, 10), "HAND-PLACED", fill="#111111")
img.paste(hand, (0, band))
d.text((10, band + hand.height + gap // 2), "AUTOMATIC", fill="#111111")
img.paste(auto, (0, band + hand.height + gap))
img.save(out)
print(f"  {out}")
PY

# 4. The numbers.
printf 'auto\n'
cat /tmp/lmc-auto-counters.txt | sed -n '1,17p' | sed 's/^/  /'
printf '\nhand\n'
cat /tmp/lmc-hand-counters.txt | sed -n '1,17p' | sed 's/^/  /'

printf '\nhand vs auto\n'
for pair in "hand:/tmp/lmc-supply-build" "auto:/tmp/lmc-auto-build"; do
    what=${pair%%:*}
    dir=${pair#*:}
    python3 tools/layoutmetrics.py "$dir/5-titled.epro2" "$D/netlist.json" \
        --json 2>/dev/null | python3 -c '
import json, sys
m = json.load(sys.stdin)
print("  %-5s sheet %4d x %4d   detour %s/%s over 1.3, %s with 3+ corners"
      "   span %s/%s   reversal %s/%s   enclosed %.1f%%   contact %s"
      % (sys.argv[1], m["sheet"][0], m["sheet"][1],
         m["detour"]["over_1_3"], m["detour"]["drawn"],
         m["detour"]["three_or_more_corners"],
         m["span"]["over_0_4"], m["span"]["nets"],
         m["reversal"]["pairs"], m["reversal"]["compared"],
         m["enclosed"]["fraction"] * 100, m["contact"]["pairs"]))
' "$what"
done
