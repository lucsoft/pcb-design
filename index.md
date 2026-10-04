---
okf_version: "0.2"
---

# Workflow

* [PCB design workflow](CLAUDE.md) - Turning a board idea into an EasyEDA-importable schematic built from verified LCSC parts.

# Designs

* [designs/](designs/) - One directory per board: the design record, its netlist, and the figures that argue for its numbers.

# Knowledge base

* [kb/](kb/) - One record per LCSC part, each fact traceable to where it came from.

# Checkers

* [ERC rule catalogue](rules/README.md) - Every rule tools/erc.py can report, every severity each one emits, and what the checker does not cover.

The checkers themselves live in `tools/` and are not markdown, so they are
outside this bundle: `erc.py` gates a netlist, `stock.py` a BOM, `okf.py` this
bundle, `consistency.py` a document against the netlist it describes.
