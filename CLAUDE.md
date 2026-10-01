# PCB design workflow

Turning a board idea into a schematic that imports into EasyEDA Pro, built
entirely from LCSC parts, with every claim about a component checked against
its datasheet and every netlist checked before it is imported.

The division of labour: **this toolchain produces connectivity, EasyEDA does
placement and layout.** Nothing here tries to route a board.

## Standing rules

Three mistakes here are expensive and two of them are irreversible, so they
are worth stating before anything else.

**Never invent an LCSC part number.** The EasyEDA importer resolves components
by `Supplier Part` and nothing else. A plausible-looking C-number that belongs
to a different part imports that different part, silently, with no field in
the property table to correct it. Every C-number comes from a catalogue
search, never from memory.

**Never infer a pin number.** Read the pin table. The XC6206 in SOT-23 is
VSS / VOUT / VIN on pins 1 / 2 / 3 — VIN is not pin 1, and nothing about the
package suggests otherwise. Package type, pin count, and what is "usual" for
the function are all unreliable.

**Record where every fact came from.** `--source` on a pin map is not
paperwork. The ERC trusts datasheet-backed data enough to raise errors on it
and downgrades `inferred` or `assumed` data to warnings. Overstating
confidence is what makes a checker turn into noise people stop reading.

When something cannot be verified, say so and leave it unverified. An honest
gap is cheap; a confident wrong value costs a board spin.

## Setup

Everything runs inside the project shell — `python3`, `node`, and `pdftotext`
are not on the system PATH:

    nix-shell                       # interactive
    nix-shell --run './tools/erc.py ...'   # one-shot

The JLCPCB/LCSC MCP server is registered at project scope in `.mcp.json` and
launches through `tools/jlcpcb-mcp.sh`, which supplies the nix-shell.

## The loop

### 1. Source parts from LCSC

Prefer **basic-tier** parts: extended-tier parts carry a one-off JLCPCB feeder
fee each.

**LCSC's own search endpoint is blocked** (HTTP 403 on `/ftps/wm/search/global`),
though the per-part detail endpoint works. For keyword search use the public
jlcsearch mirror:

    curl -sL 'https://jlcsearch.tscircuit.com/components/list.json?search=W5500' | jq

**Confirm stock against the live detail endpoint before selecting anything.**
jlcsearch's index is cached and can be badly stale — it reported 2500 units of
INA238 and 1046 of INA228 when the live figure for both was **zero**. That
discrepancy changed a design decision here (it forced current sensing low-side).
One part, one check:

    curl -s -A 'Mozilla/5.0' \
      'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C49851' \
      | jq '.result | {productModel, stockNumber}'

`tools/kb.py add` reads the live endpoint, so a freshly added record carries
true stock; `kb.py list` then shows it. Stock in jlcsearch output is a hint, not
a fact.

The **`jlcpcb` MCP server is the right tool for parametric search** — filter by
voltage, current, package, stock, basic-tier. The keyword APIs above cannot do
that, and relying on them means guessing part numbers: doing so here produced a
$4.38 MOSFET where parametric search found a better one at $0.40. Use the MCP
first; fall back to jlcsearch only for keyword lookups it cannot express.

Its local catalogue (~52k parts) builds on first search, taking a few minutes.

### 2. Record each part in the knowledge base

    ./tools/kb.py add C5446

This pulls description, package, parametric specs, stock, price and the
datasheet URL straight from LCSC. What it cannot pull is the thing the ERC
most needs: the **pin map**.

Re-running `add` on a known part refreshes it. What that does and does not
touch is deliberate:

- **Preserved:** `pins`, `rules`, `notes`, `tier`, `provenance`. Hand-written
  knowledge is never clobbered. (`--force-pins` discards the pin map, for when
  a part was recorded wrongly.)
- **Overwritten:** description, package, parameters, stock, price — LCSC owns
  these.
- **Re-guessed:** `category`, unless you set `provenance.category` to
  `"manual"`, which pins your correction. LCSC's taxonomy is coarse and
  sometimes wrong, so the override has to stick.

`stock` and `priceUsd` go stale. Each record carries a `retrieved` date;
refresh before committing to a BOM, not when you first pick the part.

### 3. Read the datasheet — text first

    ./tools/ds.py fetch C5446      # download + extract to text
    ./tools/ds.py index C5446      # section headings with page numbers
    ./tools/ds.py find  C5446 'absolute maximum' -C 10
    ./tools/ds.py page  C5446 2    # read one page

Datasheets run to hundreds of pages. Reading one as images costs roughly
1–2k tokens per page, so a 117-page part is ~200k tokens to ingest whole.
`index` then `find` then `page` costs a fraction of that and is greppable.

**Open the PDF with a vision read only when text extraction is not enough** —
`./tools/ds.py pdf C5446` prints the path, and the `Read` tool takes a `pages`
range. That is worth it for:

- pinout *drawings* (a package diagram has no text to extract)
- tables that come out visibly scrambled
- **any value whose sign matters.** `pdftotext` drops some Unicode minus
  signs: the STM32F103 datasheet's `VSS − 0.3` extracts as `VSS 0.3`. Confirm
  negative limits against the page image before recording them.

If `fetch` warns that there is almost no extractable text, the PDF is scanned
images and vision is the only option.

**Rendering a page for a vision read.** The `Read` tool cannot open a PDF
directly here: it shells out to `pdftoppm`, which lives in the nix-shell and
not on the system PATH. Render the page yourself first, then read the PNG:

    nix-shell --run 'pdftoppm -png -r 200 -f 3 -l 3 kb/datasheets/C139126.pdf /tmp/p'
    # then Read /tmp/p-3.png

This came up on the WS2812D-F8: its per-channel drive current lives in a table
that is an embedded image, so `find` and `page` both return nothing useful
while the number sits there in plain sight on the rendered page. A heading
that `index` finds but `page` shows as empty is the signature of this case.

### 4. Record the pin map, with provenance

    ./tools/kb.py set-pins C5446 \
      --source "datasheet p.2 PIN ASSIGNMENT (SOT-23 column)" \
      --pin "1:VSS:power_in" \
      --pin "2:VOUT:power_out:vmax=7.0" \
      --pin "3:VIN:power_in:vmax=7.0"

Pin types are listed in `kb/VOCABULARY.md`. `--source` is not optional
bookkeeping: `tools/erc.py` downgrades rules built on `inferred` or `assumed`
data from error to warning, so an honest provenance keeps the checker's
errors trustworthy.

Turn datasheet requirements into part rules while the datasheet is open — a
required decoupling capacitor, a strapping pin that must not be pulled low.
That is the whole point of the knowledge base: **read it once, check it
forever.** See `rules/README.md` for the supported `requires` values.

### 5. Write the netlist

Create `designs/<name>/netlist.json` in the format the EasyEDA Pro extension
`eext-generate-schematic-from-netlist` consumes:

```json
{
  "gge1": {
    "props": { "Designator": "U1", "device_name": "XC6206P332MR",
               "value": "3.3V", "Supplier Part": "C5446" },
    "pins": { "1": "GND", "2": "VCC_3V3", "3": "VBUS" }
  }
}
```

Three constraints the importer enforces silently:

- keys must be `gge1`, `gge2`, … in order
- field names other than `Designator` and `Supplier Part` are lowercase
- **`Supplier Part` is the only field used to resolve the component.** The
  importer calls `eda.lib_Device.getByLcscIds()`; manufacturer part numbers
  and footprint names are ignored entirely. A wrong C-number silently places
  the wrong part, and the property table has no symbol or footprint field to
  correct it afterwards. Get it right before import, not after.

Two smaller traps worth knowing:

- `device_name` and `value` are **cosmetic**. They show on the schematic and
  are ignored during resolution, so a correct-looking `device_name` is no
  evidence the right part will land.
- Deleting a component means **renumbering every `gge` key after it**. The
  sequence has no gaps; `S1-key-sequence` catches a violation, but it is
  easier to renumber while editing than to debug afterwards.

Alongside it, `designs/<name>/design.yaml` declares the power rails and their
voltages. Without that the overvoltage check cannot run. The same file takes
an `ignore:` list of rule ids — suppress a rule there with a comment saying
why, rather than working around it in the netlist.

### 6. Check before importing

    ./tools/erc.py designs/<name>/netlist.json

Iterate until clean. Rules and their severities are catalogued in
`rules/README.md`, including an explicit list of what the checker does *not*
cover. Exit status is 1 on any error, so it gates a build.

Useful flags:

    --json      machine-readable findings, for scripting or CI
    --strict    treat warnings as errors
    --design    point at a design.yaml somewhere other than alongside the netlist

    ./tools/kb.py check          # validate the knowledge base itself
    python3 tests/test_erc.py    # regression tests for the checker

`designs/demo-ldo/` is a small worked example that passes — a 3.3 V LDO stage
with both capacitors and a power LED. Use it to sanity-check the toolchain
after changing anything.

### 7. Import into EasyEDA Pro

Install the extension `eext-generate-schematic-from-netlist` from the EasyEDA
marketplace once, then **Netlist Rebuild** and select the JSON. Placement and
routing are yours from here.

### 8. Verify after importing

Export the netlist from EasyEDA and diff it against the JSON that went in.
Do not trust the canvas or an API success message: the failure modes that
matter are invisible on screen — pins that look wired but carry no net port,
and separate nets silently merged into one.

EasyEDA's DRC does not substitute for this. It checks geometry, not intent;
a clean DRC says nothing about whether the board is connected correctly.

## Simulation

The ERC is static analysis — it is where most schematic bugs die, and it is
cheap enough to run on every edit. It cannot tell you whether an analogue
circuit *behaves*.

For that, use EasyEDA Pro's built-in **ngspice** (Simulation tab; it also
ships a real-time engine, Simulide). Worth running on a regulator, filter,
amplifier, or level shifter. Not worth running on a digital board as a whole —
SPICE has no idea what firmware does, so a "passing" simulation of an MCU
board means nothing.

Neither one is evidence the board works. That takes a physical board.

## Layout

    .
    ├── shell.nix              dev shell (python+matplotlib, node, poppler, jq)
    ├── .mcp.json              JLCPCB/LCSC MCP registration
    ├── kb/
    │   ├── context.jsonld     JSON-LD vocabulary, extends schema.org
    │   ├── VOCABULARY.md      pin types, provenance levels, part rules
    │   ├── parts/C*.json      one document per LCSC part
    │   ├── modules/           assemblies: envelopes derived from their parts
    │   └── datasheets/        cached PDFs and extracted text
    ├── designs/<name>/
    │   ├── README.md          decisions, rationale, open questions
    │   ├── design.yaml        rails, buses, suppressed rules
    │   ├── netlist.json       the EasyEDA import artefact
    │   ├── figures.py         regenerates the figures
    │   └── figures/           generated SVG+PNG, light and dark
    ├── rules/README.md        ERC rule catalogue and its limits
    ├── tools/
    │   ├── kb.py              knowledge base: add, set-pins, check, list
    │   ├── ds.py              datasheet fetch, index, find, page
    │   ├── erc.py             electrical rule check
    │   ├── epro.py            read an EasyEDA .epro2 (types, bom, nets)
    │   ├── plot.py            chart helper: palette and house style
    │   └── jlcpcb-mcp.sh      MCP launcher (supplies the nix-shell)
    └── tests/test_erc.py      regression tests

## Conventions

Written in English, including comments and documentation, regardless of the
language the conversation happens in — matching the convention in the
home-manager repository.

This directory is not under version control yet. It probably should be: the
whole premise of a text netlist plus a JSON knowledge base is that changes are
diffable, and `git init` costs nothing. `.gitignore` already excludes the
datasheet cache (large, re-fetchable) and any credentials file.

## Reading an existing EasyEDA project

    ./tools/epro.py types <file.epro2>     record types and counts
    ./tools/epro.py bom   <file.epro2>     devices with LCSC numbers
    ./tools/epro.py nets  <file.epro2>     net names
    ./tools/epro.py dump  <file.epro2> META -n 3

An `.epro2` is a zip holding `project2.json` and one or more `.epru` documents.
The `.epru` is **line-oriented**, and each line is two JSON objects:

    {"type":"FILL","ticket":22,"id":"e5"}||{"groupId":0,"netName":"",...}|

header, `||`, payload, trailing `|`. The easy mistake — the one made here first
— is to split the whole file on `||`, which yields two chunks and looks like an
unparseable proprietary blob. Split on newlines first, then on `||` within each
line, and it reads cleanly.

Two record types carry the BOM:

- **META** — the library device definition, with an `attributes` dict holding
  `Supplier Part`, `Manufacturer Part`, `JLCPCB Part Class`, `Designator`
- **ATTR** — per-instance attribute placements on the canvas, grouped by
  `parentId`, carrying the real designator

Component geometry is under `PART`, which holds only a bounding box and title —
useless for a BOM. Note that designators repeat across documents (schematic and
PCB each place the same part), so instance counts need de-duplicating.

This is read-only and reverse-engineered. For a netlist to check, still **export
one from EasyEDA Pro directly** — that is the supported path.

## Figures and diagrams

Three kinds of thing, three right answers. ASCII art is never one of them.

| Content | Use | Not |
|---|---|---|
| How a quantity varies | a rendered chart (`tools/plot.py`) | a table of eight rows |
| Topology, signal flow, a power tree | a **Mermaid** block | ASCII boxes and arrows |
| Discrete comparison — part A vs B, ratings, stock | a markdown table | a chart |

**Quantitative relationships get a rendered chart.** A table of rows is how a
curve gets hidden: the reader has to reconstruct the shape in their head, and
the interesting part is usually where two lines cross or where a curve meets a
limit. Both are obvious in a plot and invisible in text.

**Topology gets Mermaid.** ` ```mermaid ` + `flowchart LR` renders in GitHub,
VS Code and most markdown viewers, survives editing, and reflows — none of
which an ASCII arrow chain does. Use it for the power tree, the protection
chain, bus structure, anything with boxes and connections.

**Render Mermaid too, don't just write it.** Layout is emergent and frequently
bad; the only way to know is to look. mermaid-cli renders every block in a
markdown file in one pass:

    nix-shell -p nodejs chromium --run '
      export PUPPETEER_SKIP_DOWNLOAD=1
      export PUPPETEER_EXECUTABLE_PATH=$(command -v chromium)
      npx -y @mermaid-js/mermaid-cli -i designs/<name>/README.md -o /tmp/check.md'

It writes one SVG per block alongside the output, and fails loudly on a syntax
error — so it doubles as a lint. Two layout habits that came out of doing this:

- **Group parallel things in a `subgraph` with `direction TB`.** Without it the
  two output channels landed at opposite ends of the diagram with their edges
  crossing everything between.
- **Split power from control.** A single diagram covering both always crosses
  itself, because the MCU connects to every block. Two diagrams, each telling
  one story, beat one that tells both badly.
- Use thick edges (`==>`) for the high-current path so it reads apart from
  control and sense wiring.

**A figure that is not embedded does not exist.** Generating a PNG into
`figures/` and describing it in prose means nobody sees it. Every generated
figure goes at the point in the document where it makes its argument — not
collected at the end.

`build()` writes a light and a dark variant, so embed both with a `<picture>`
element rather than a bare `![]()`, which would leave the dark file unused:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="figures/name-dark.svg">
  <img alt="What the figure shows" src="figures/name.svg">
</picture>
```

Reference the **SVG** — it scales and stays sharp. The PNG is for reading back
during review, since the image tooling renders PNG and not SVG.

After generating figures, grep for them:

    for f in designs/*/figures/*.svg; do
      grep -rq "$(basename "$f")" --include='*.md' . || echo "ORPHAN: $f"
    done

If a file in `figures/` has no reference in any `.md`, either embed it or delete
it. This check is how the first batch here was caught: twelve files generated,
zero referenced.

    from plot import figure, save, build, limit_line, annotate   # tools/plot.py

`tools/plot.py` carries the palette and the house style, so charts stay
consistent without per-script configuration. Figures live in
`designs/<name>/figures/` and are generated by a committed
`designs/<name>/figures.py`, so they regenerate when the numbers change rather
than going stale. `build()` writes light and dark SVG and PNG in one call.

Rules the helper enforces, worth knowing because they are easy to violate by
hand:

- **One y-axis. Never two.** Two measures on different scales go in two figures.
  A dual-axis chart puts the crossing point wherever the scaling happens to put
  it, which reads as a finding and is an artefact. Brightness and frame rate in
  this project are a live example — they look tempting to overlay and must not be.
- **Categorical hues in fixed order, never cycled.** Take slots 0, 1, 2… from
  `PALETTE`. The order is validated for colour-vision-deficient separation; a
  ninth series is not a new hue.
- **A legend whenever there are two or more series**, so identity is never
  carried by colour alone. One series needs no legend — the title names it.
- **`limit_line()` for a constraint** (a rating, a target, a ceiling). Most
  findings in this project are "where does the curve meet the limit", and the
  line makes that readable. Pass `side="left"` or `"right"` to put the label on
  whichever edge the legend and the data are not using.

**Render it and look at it before calling it done.** The style rules cover colour
and weight, not layout — label collisions and overflow only show up in the
output. Read the PNG. The first version of the brightness chart here had its
reference-line label running straight through the legend, and nothing but
looking at it would have caught that.

## Maintenance

### The LCSC endpoint is unofficial and moves

`tools/kb.py` and `tools/ds.py` both read:

    https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=<C-number>

This is a scraped endpoint, not a published API, and it has already moved once
— it used to be `/wmsc/product/detail`, which now answers HTTP 200 with
`{"code":404,"msg":"The static resource is unavailable"}`.

**Symptom:** `kb.py add` reports "LCSC request failed" or "LCSC says: ...", or
`ds.py fetch` cannot find a datasheet URL, for every part rather than one.

**Fix:** find the current path and update `API` in `tools/kb.py` and
`resolve_url()` in `tools/ds.py`. The `jlcpcb` MCP server is a separate
implementation reading a different source, so if one breaks the other is
likely still working — and `ds.py fetch --url` always accepts a URL directly.

### Adding an ERC rule

1. Write a `check_*` method on `Check` in `tools/erc.py` and call it from
   `run()`. Use `self.add(rule_id, severity, message, where, hint)`; the hint
   should say what to *do*, not restate the problem.
2. Add a case to `tests/test_erc.py` — a minimal netlist that trips it.
3. Add a row to the table in `rules/README.md`.
4. Re-run the suite. `clean design produces no errors` is the case that
   matters most: a new rule that fires on the good fixture is a false positive,
   and false positives are how a checker gets ignored.

Rule ids are prefixed by area — `S` structural, `K` knowledge base,
`C` connectivity, `E` electrical, `P` power, `B` bus, `Q` sourcing,
`R-` part-specific.

### Keeping severities honest

Error means "this board will not work". Warning means "look at this". If a
rule starts firing on designs that turn out to be fine, demote it rather than
leaving it to be routinely ignored — the value of the error level is that it
is never ignored.

## Safety

The MCP server exposes `jlcpcb_pcb_create_order` and `jlcpcb_tdp_create_order`,
which place **real, paid orders**. It is registered with no credentials, so
those fail closed. Adding credentials to `.mcp.json` lets an agent spend money
— do that only deliberately, and never commit them.
