---
type: Playbook
title: PCB design workflow
description: Turning a board idea into an EasyEDA-importable schematic built from verified LCSC parts.
tags: [workflow, erc, lcsc, easyeda, provenance]
status: stable
generated: { by: human:lucsoft, at: 2026-10-03T08:18:20+02:00 }
---

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

**Check the whole BOM at once before ordering, not part by part.**

    ./tools/stock.py designs/<name>/README.md    # every C-number the doc names
    ./tools/stock.py --kb                        # every part in the knowledge base
    ./tools/stock.py --refresh <file>            # also write stock back into kb/

Exit status is 1 if anything is at zero, so it gates a BOM the way `erc.py`
gates a netlist. This exists because a sourcing pass that trusted the mirror put
**five dead part numbers into a BOM at once** — including the 100 nF 0402 used in
eighteen places, which the mirror listed at six figures and the live endpoint at
zero. One part checked by hand proves nothing about the other fifty.

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

**The pin *number* comes from the EasyEDA symbol, not from the datasheet.**
This is not a detail — the netlist keys `pins` by the number the library uses,
and that is what the importer resolves against. The two agree on most parts and
disagree without warning on connectors and modules:

    ./tools/eda.py pins C3198004        # number -> name, as the symbol has it
    ./tools/eda.py pins C3198004 --kb   # emit a kb.py set-pins command
    ./tools/eda.py verify <file>|--kb   # check kb numbers against the library

Four parts on one board disagreed, and in each case the datasheet reads as
obviously right: a module header numbered 12→7 rather than 7→12, a USB-C
receptacle whose shield tabs are pins **0** and **1**, a four-leg tactile switch
the library gives two pads, and an MCU module whose one `EPAD` is nine GND pins.
A key the symbol does not have places a pin that silently goes nowhere, and
nothing downstream reports it. Run `eda.py verify` on the whole BOM before
writing a netlist, the way `stock.py` is run before ordering one.

Where the two numbering schemes differ, record both: `--pin "1:GND:power_in:contact=J1-1"`
keeps the silkscreen designation that assembly reads. A single pin whose
provenance is weaker than the rest of the map takes `source=` of its own —
`erc.py` prefers it over `provenance.pins`, so one inferred pin does not drag
eleven datasheet-backed ones down to warning severity.

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
  easier to renumber while editing than to debug afterwards. Past about thirty
  components, write a generator instead and commit both it and its output —
  `designs/led-matrix-controller/netlist.py` assigns the keys from list
  position, so deleting a part is deleting a line.

Alongside it, `designs/<name>/design.yaml` declares the power rails and their
voltages. Without that the overvoltage check cannot run. The same file takes
an `ignore:` list — suppress a rule there with a comment saying why, rather
than working around it in the netlist. Write `rule@where` rather than a bare
`rule` wherever the exception is local: a bare id silences the rule across the
whole board, which is how a check that was doing real work quietly stops.

A part that is placed so its pad exists and must **not be fitted** goes in
`design.yaml`'s `dnf:` mapping, designator to reason — the reason is printed as
the finding's hint, so write it for whoever reads the ERC output. The netlist format has no field for it — `value`
is cosmetic and the importer ignores it — so this is the only machine-readable
form. Do not write it as an `ignore:` entry instead: a suppression deletes the
finding, where `dnf:` keeps it reported at info severity with the reason beside
it, and reads identically on the variant that *does* fit the part.

A pin that is open **on purpose** goes to a net named `NC_<something>` — one
**per pin**, never a shared `NC`. The checker treats any `NC_*` name as a
recorded decision, where a pin left out of the netlist is reported as
forgotten; both are right and only you know which. Give each its own name
because a shared `NC` puts every open pin on one node, and that is harmless
only if the *importer* also treats the name specially — which nothing here can
verify. On the LED matrix controller the shared form would have tied seven
enabled buffer outputs to a buck converter's enable pin.

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

Export the document from EasyEDA and diff it against the JSON that went in:

    ./tools/epro.py diff <export.epro2> designs/<name>/netlist.json

Do not trust the canvas or an API success message: the failure modes that
matter are invisible on screen — pins that look wired but carry no net port,
and separate nets silently merged into one. The diff counts **pin references**
rather than nets, because a net that keeps its name while losing half its pins
is exactly what a name-only comparison passes. Exit status is 1 on any
discrepancy and **2 if the export carried no nets at all** — an empty export
must not read as a clean one.

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
    ├── index.md               OKF bundle root; declares okf_version
    ├── kb/
    │   ├── index.md           OKF directory listing
    │   ├── context.jsonld     JSON-LD vocabulary, extends schema.org
    │   ├── VOCABULARY.md      pin types, provenance levels, part rules
    │   ├── parts/C*.json      one document per LCSC part
    │   ├── modules/           assemblies: envelopes derived from their parts
    │   └── datasheets/        cached PDFs and extracted text
    ├── designs/
    │   ├── index.md           OKF directory listing
    │   └── <name>/
    │       ├── README.md      decisions, rationale, open questions
    │       ├── design.yaml    rails, buses, suppressed rules
    │       ├── netlist.json   the EasyEDA import artefact
    │       ├── figures.py     regenerates the figures
    │       └── figures/       generated SVG+PNG, light and dark
    ├── rules/README.md        ERC rule catalogue and its limits
    ├── tools/
    │   ├── kb.py              knowledge base: add, set-pins, check, list
    │   ├── ds.py              datasheet fetch, index, find, page
    │   ├── eda.py             EasyEDA library: symbol pin numbers, verify
    │   ├── erc.py             electrical rule check
    │   ├── epro.py            read an EasyEDA .epro2 (types, bom, nets, diff)
    │   ├── plot.py            chart helper: palette and house style
    │   ├── stale.py           find superseded values after a numeric change
    │   ├── consistency.py     check a document against the netlist it describes
    │   ├── xref.py            check a document's "see X" pointers resolve
    │   ├── stock.py           check a BOM against live LCSC stock
    │   ├── okf.py             check the docs against OKF v0.2 conformance
    │   └── jlcpcb-mcp.sh      MCP launcher (supplies the nix-shell)
    └── tests/                 regression tests: erc, xref, okf, kb

## Conventions

Written in English, including comments and documentation, regardless of the
language the conversation happens in — matching the convention in the
home-manager repository.

**European units and standards, not American ones.** Cable cross-sections in
mm² per IEC 60228, never AWG. Lengths in mm and m, temperatures in °C, mass in
g. Where a standard has both an IEC/EN and a US form, cite the IEC/EN one.

Two exceptions, because the industry has no European alternative in practice:
**IPC-2221** for PCB trace sizing, and **oz copper** for plating weight — but
state the metric value alongside (1 oz = 35 µm), and give IPC results in mm
rather than mils.

This is not cosmetic. AWG in a German project means every reader converts in
their head before they can sanity-check a number, and conversions are where
mistakes hide.

## Documentation format

The prose in this repository is an **Open Knowledge Format (OKF) v0.2**
bundle — markdown with YAML frontmatter, specified by
`GoogleCloudPlatform/knowledge-catalog` in `okf/SPEC.md`. The point is not
tidiness: §5 gives frontmatter fields for provenance, verification and
staleness, which is the discipline this project already enforced by hand in
`kb/parts/` and had no way to express about its *documents*.

    ./tools/okf.py                  # the repository root is the bundle
    ./tools/okf.py --strict         # warnings fail too
    ./tools/okf.py --json           # machine-readable, for CI
    python3 tests/test_okf.py

Exit status is 1 on any error, so it gates a build the way `erc.py` does.

**Only `.md` files are in scope.** `kb/parts/*.json` and `kb/modules/*.json`
are JSON-LD, not markdown, and OKF says nothing about them — `kb.py check`
and `kb/context.jsonld` remain what validates those. Adopting OKF did not
and must not turn the machine-read knowledge base into prose.

Every non-reserved `.md` opens with frontmatter whose only required key is
`type`. Beyond that this bundle uses:

| Field | What it carries here |
|---|---|
| `title`, `description` | what `index.md` entries are generated from |
| `status` | `draft` / `stable` / `deprecated`; absent means `stable` |
| `generated` | `{ by: <actor>, at: <datetime> }` — who produced the content |
| `verified` | a list of `{ by, at }` confirmations, independent of `generated` |
| `sources` | what the document derives from, each with a `resource` |
| `stale_after` | an absolute instant after which the content needs re-reading |

**`verified` is a claim, not decoration.** §5.3 derives the highest trust
tier — *human-reviewed* — from a `human:` actor and nothing else, so writing
one in because a document looks finished is the documentation equivalent of
inventing an LCSC part number. Record a machine confirmation as
`process:erc` or `process:consistency` when that tool has actually been run
and is green; leave `verified` off entirely when nobody has checked. Absence
is a meaningful state in OKF and must stay an honest one.

**`stale_after` is for content that decays on a clock, not for everything.**
A design record carrying a BOM gets one, because `stock` and `priceUsd` go
stale and `stock.py` is the thing that re-confirms them; the convention here
is three months from the `retrieved` date in `kb/`. A vocabulary or a rule
catalogue gets none — it is wrong when the code changes, not when a date
passes. Moving `stale_after` forward without re-reading the content is how
the field stops meaning anything.

`index.md` and `log.md` are reserved (§3.1). This bundle has `index.md` at
the root, in `kb/` and in `designs/`, and the root one carries
`okf_version: "0.2"` — §8 makes that the only frontmatter key permitted in
any index. There is no `log.md` anywhere, deliberately: git already records
what changed and why, and a hand-maintained duplicate of it in prose is the
same mistake as retiring a value in a document instead of a commit message.

The rules, and every severity each one emits:

| id | severity | fires when |
|---|---|---|
| `O1-no-frontmatter` | error | a concept document has no parseable frontmatter block, or opens one it never closes (§11.1) |
| `O2-no-type` | error | frontmatter carries no non-empty `type` (§11.2) |
| `O3-index-structure` | error, warning, info | an `index.md` carries frontmatter it may not, declares an unknown `okf_version`, or has no section heading (§8) |
| `O4-log-structure` | error | a `log.md` date heading is not ISO `YYYY-MM-DD`, or it carries frontmatter (§9) |
| `O5-actor-format` | warning | `generated.by` or `verified[].by` is not `human:<id>`, `process:<id>` or `<producer>/<version>` (§7) |
| `O6-timestamp-format` | warning | a timestamp has no explicit UTC offset, or is a bare date (§5) |
| `O7-source-no-resource` | error, warning | a `sources` entry has no `resource`, or `sources` is not a list (§5.1) |
| `O8-status-unknown` | warning | `status` is not one of the three in §5.4 |
| `O9-stale` | warning | `now >= stale_after` (§5.5) |
| `O10-footnote-unmatched` | info | a footnote label matches no `sources[].id`, in a document that declares some (§5.1) |

What it deliberately does **not** report, because §11 forbids a consumer
from rejecting a bundle over them: unknown `type` values, unknown extra
frontmatter keys, missing optional families, broken cross-links, and a
directory with no `index.md`. A checker that flagged those would be
non-conformant itself, and would fire constantly on correct documents.

## Version control

**Commit after every meaningful change, without being asked.** The premise of a
text netlist plus a JSON knowledge base is that changes are diffable; that is
only worth anything if the history exists.

This is not bookkeeping. A whole section of a design document was once silently
deleted by a careless string replacement here, while two cross-references still
pointed at it — and it took an external review to notice. `git diff` would have
shown it in the same second it happened. The ERC checks netlists, not prose, so
version control is the only thing watching the documents.

**After changing any number, hunt the old one before committing.**

    ./tools/stale.py '32.4 k' '4.58 A' '18 W'

Six consecutive reviews of `designs/led-matrix-controller` caught the same
failure and nothing else: a value corrected in the section that derives it and
left standing in the BOM, the thermal table, an open question, or a KB record.
The worst instance put a superseded resistor and capacitor set in the **bill of
materials** -- the artefact you order from -- pointing at the section that
disagreed with it.

Prose has no type checker, so this is the substitute. It greps `designs/`,
`kb/` and `rules/` and prints every hit with context. A hit is not automatically
a bug -- the same digits appear legitimately elsewhere. Adjudicate each one;
require zero unexplained hits. **Read the hits in full.** One sweep here was
done through `cut -c1-240` and the stale values were past the cut, which put a
false completeness claim into the commit message.

**Numbers that describe the netlist should not be maintained by hand at all.**

    ./tools/consistency.py designs/<name>

A section rename leaves every pointer at it aimed at nothing, which
`./tools/xref.py <file>` catches. A reference resolves when some **leading
phrase** of it is exactly a heading or one of that heading's leading clauses —
the part before a comma, colon or bracket — so "see Recovery and debug for what
that leaves" matches without the sentence having to stop there, and `Recovery`
stays distinguishable from `Recovery and debug`. The first version matched the
whole captured string, found that false, and answered it with a whitelist entry
whose comment claimed the target was not a heading in the file. It was. Fix the
matcher, never the list.

It also reports **two headings where one opens the other**, because no matcher
can report a rename of the longer one while the shorter survives: the reference
contains it as a leading phrase of itself. That is unfixable in a checker and
trivial in a document — two here pointed at a heading that had
never existed, with the material three sections away, so a reader could not
tell whether they had failed to find it or it was gone.

`consistency.py` derives the component, pin, net, open-pin and passive counts from
`netlist.json`, reads the BOM tables **by their headers** and cross-checks each
row's C-number, Qty, price and Value — magnitude, package and dielectric —
against the netlist and `kb/`, expands the
"deliberately open pins" table and set-differences it against the real `NC_*`
pins, and exits 1 on a mismatch. This exists because nine
consecutive review rounds of `designs/led-matrix-controller` found the same
thing and nothing else: a good fix applied to the artefact and carried into
some but not all of the prose describing it. `stale.py` finds a value you
changed; this finds one you *should* have changed and did not. An unparseable
claim is reported as **unchecked** rather than passed — a number nobody
verified must not read as a number that was verified. **Every** skip is
reported, and the summary line names what was checked rather than asserting
that everything was: the first version of this tool dropped unreadable rows
silently and then printed "every price matches", which is the failure it was
written to prevent, inside the tool.

**Retire a value in the commit message, not in the document.** An earlier
version of this file said the opposite: that

> An earlier version of this section said 100 W, which was wrong by 1.4x because
> it mistook the BV_DSS wall for the power line.

belonged in the prose, because it teaches the next reader why the obvious
reading is wrong. It does teach that, and it cost more than it was worth. Over
five review cycles this design accumulated more than twenty such notes, and the
effect is that **every corrected value appears twice** -- once as the current
figure and once as the retired one. `stale.py` flags both, a reviewer reading
for contradictions finds both, and the document grew 15% while the signal got
worse rather than better.

So: the document states the current value and nothing else. The *why it is not
the obvious value* goes in the commit body, which is already the right length
for it and is what `git log -S '<value>'` searches. The one exception is a
correction a reader would otherwise re-make while using the document -- a
datasheet whose own prose contradicts its equation tags, say. That is not
history, it is a live trap, and it stays.

**Verify the commit body against the diff before committing.** Three times in
this project a commit has described an edit the diff did not contain, and each
time the cause was the same: a script that applies several replacements, asserts
on each, and writes the file at the end. When one assertion fails the file is
never written -- so the edit fails *closed* on disk and *open* in the message
that was already drafted. Apply replacements one at a time, write after each,
and report misses instead of aborting; then grep the finished file for every
value the message claims.

Commit messages follow the nixpkgs convention used in the home-manager
repository:

    <scope>: <imperative summary>

    <body explaining WHY, wrapped at 72 columns>

`<scope>` is lowercase and names the thing changed — a part (`husb238a`), a tool
(`erc`), an area (`docs`, `kb`), or a design (`led-matrix`). The summary is
imperative and lowercase after the colon, no trailing period, under ~60
characters. The body explains why; the diff already shows what.

Good points to commit: after recording a part, after a design decision lands,
after a review's findings are applied, before any large edit to a document.
Especially before a large edit — that is the one that bites.

`.gitignore` excludes the datasheet cache (large, re-fetchable with
`ds.py fetch`), Python bytecode, and any credentials file. The `.epro2`
reference exports are tracked: they are binaries that do not diff, but they are
the only record of the module design.

## Reading an existing EasyEDA project

    ./tools/epro.py types <file.epro2>     record types and counts
    ./tools/epro.py bom   <file.epro2>     devices with LCSC numbers
    ./tools/epro.py nets  <file.epro2>     net names
    ./tools/epro.py diff  <file.epro2> <netlist.json>   exported nets vs input
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
2. Add a case to `tests/test_erc.py` — a minimal netlist that trips it, **and
   one that does not**. A rule with only a positive case can be loosened to
   nothing without failing the suite; that has happened here twice.
3. Add a row to the table in `rules/README.md`, listing **every** severity the
   rule can emit. A rule that reports at three and documents one tells a reader
   the fail-closed branch does not exist.
4. Re-run the suite **and `erc.py designs/demo-ldo`**. A rule that
   touches a document rather than a netlist re-runs `okf.py` too.

**Two probes, not one.** A *rename probe* (break the thing and require a
report) exercises the matching; an *inverse probe* (inject a shape the tool has
never seen and require a report) exercises the **capture**, which the first
cannot reach by construction — a reference the regex never matches is invisible
to a rename. `tools/xref.py` had three fail-open regressions in three
consecutive commits, and each time the rename probe passed. `tests/test_xref.py`
runs both. A new rule that fires on
   the good fixture is a false positive, and false positives are how a checker
   gets ignored. `Q3-tier-unknown` was demoted from warning to info for exactly
   this.

**Give the rule three answers where the data can be absent.** Yes, no, and
"the record does not say" are different, and collapsing the third into "no" is
how every check added in this project has first failed: it fails *open*, and
the report gets cleaner. `P3-rail-bridge` keyed on a field `kb.py add`
overwrites, so a routine refresh would have deleted it silently; it now warns
instead. `consistency.py` reports every row it cannot read for the same reason.

**Validate the things that switch a rule off.** `appliesTo`, `requires`,
`ignore:` and `dnf:` are all data that silently disables checking when it is
wrong — a typo in `appliesTo` makes a part rule match no pin and never run.
`kb.py check` and `K6`/`K7` cover these; anything new of that kind needs the
same. `kb/context.jsonld` is one more: a term it does not define is dropped
by a JSON-LD reader and by nothing else, so the data goes on reading fine
while the linked-data claim stops being true. `kb.py check` reports any key
the context does not cover, and `@json` is the escape hatch that must not
be used to silence a field that really is vocabulary.

Rule ids are prefixed by area — `S` structural, `K` knowledge base,
`C` connectivity, `E` electrical, `P` power, `B` bus, `Q` sourcing,
`R-` part-specific, `O` OKF conformance. The `O` rules are catalogued under
Documentation format; `rules/README.md` is the ERC catalogue and stays that.

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
