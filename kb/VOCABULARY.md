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
