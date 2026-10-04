---
type: Vocabulary
title: Knowledge base vocabulary
description: Pin types, provenance levels and part rules used by every record under kb/parts/.
tags: [knowledge-base, provenance, pin-types]
status: stable
generated: { by: human:lucsoft, at: 2026-10-01T22:49:49+02:00 }
sources:
  - id: context
    resource: /kb/context.jsonld
    title: The JSON-LD context these terms resolve through
---

# Knowledge base vocabulary

Every part in `kb/parts/` is a JSON-LD document keyed by its LCSC C-number.
The `@context` extends schema.org, so the whole KB is valid linked data: it can
be loaded into any RDF/JSON-LD tool, merged, or queried with SPARQL. It is also
just plain JSON, so `jq` and the ERC checker read it directly without a library.

## Pin types

Drives the electrical-contention checks in `tools/erc.py`. Vocabulary follows
KiCad's, because it is the one most datasheet readers already know.

| type            | meaning                                      |
|-----------------|----------------------------------------------|
| `power_in`      | supply pin, consumes power (VDD, VSS, GND)   |
| `power_out`     | regulator/supply output, drives a rail       |
| `input`         | high-impedance digital/analog input          |
| `output`        | push-pull driver                             |
| `bidirectional` | driven both ways (GPIO, data bus)            |
| `tri_state`     | push-pull with a high-Z state                |
| `open_collector`| open-drain/open-collector, needs a pull-up   |
| `passive`       | no direction (resistors, caps, connectors)   |
| `nc`            | not internally connected — must stay unwired |
| `unspecified`   | unknown; suppresses contention checks        |

## Confidence

Every fact carries provenance. `provenance` maps a field name to where the
value came from, so a later reader can tell a datasheet-backed number from a
guess:

- `"datasheet p.12 Table 7"` — read from the PDF, cite page and table
- `"lcsc-api"` — pulled from the LCSC catalogue
- `"inferred"` — derived, not stated; treat as unverified
- `"assumed"` — a default the agent chose; the weakest claim

`tools/erc.py` downgrades any rule whose inputs are `inferred` or `assumed`
from error to warning, so unverified data cannot fail a build on its own.

## Rules

A part may carry its own ERC rules — this is where datasheet knowledge becomes
machine-checkable instead of something to re-read each time:

```json
"rules": [
  { "id": "vdd-decoupling", "appliesTo": ["VDD"], "requires": "decoupling",
    "value": "100nF", "severity": "error",
    "note": "datasheet p.9 §6.2: 100nF within 5mm of each VDD pin" },
  { "id": "gpio9-strapping", "appliesTo": ["GPIO9"], "requires": "not-pulled-low",
    "severity": "error",
    "note": "datasheet p.25 Table 3-3: low at reset enters download mode" }
]
```

Supported `requires` values are listed in `rules/README.md`.

## Per-pin overrides

Two keys sit on a pin rather than on the record, and both exist because the
record-level answer is wrong for some pins:

- `contact` — the designation printed on the part or given in its datasheet,
  where that differs from the number the **EasyEDA symbol** uses. The netlist
  keys `pins` by the symbol's number; assembly reads the silkscreen. The
  WIZ850io numbers its header 1–12 and silkscreens it `J1-1 … J2-6`, in
  opposite directions on the J2 side, so recording only one of the two makes
  either the netlist or the build wrong.
- `source` — provenance for *this* pin, overriding `provenance.pins`.
  `tools/erc.py` prefers it, which is what lets one inferred pin sit in an
  otherwise datasheet-backed map without dragging the other eleven down to
  warning severity.

Both go through `kb.py set-pins`:

    --pin "1:GND:power_in:contact=J1-1:source=datasheet p.4 Fig 2"

## The context

`kb/context.jsonld` defines every term a record may use. A key it does not
define is **dropped by a JSON-LD reader** and by nothing else: the file keeps
it, `jq` keeps it, every tool here keeps it, and only the claim that this
knowledge base is valid linked data quietly stops being true.

`tools/kb.py check` reports any key the context does not cover. Two kinds of
term stop it descending, because their contents are data rather than
vocabulary:

- `"@container": "@index"` — the keys are the index, as in `parameters`,
  where LCSC names the fields.
- `"@type": "@json"` — the value survives verbatim as a JSON literal. The
  assembly sections in `kb/modules/` use this: `envelope`, `systemLimits`,
  `thermalLimit` and the rest hold worked derivations whose shape is argued
  in prose, not modelled as vocabulary.

Reaching for `@json` to silence a finding on a field that *is* vocabulary is
how the check stops working. Define the term instead.
