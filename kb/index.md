# Conventions

* [Knowledge base vocabulary](VOCABULARY.md) - Pin types, provenance levels and part rules used by every record under kb/parts/.

# Assemblies

* [Assemblies](modules/README.md) - A board treated as one part, with an operating envelope derived from its components rather than read off a datasheet.

# Parts

`parts/` holds one JSON-LD document per LCSC part, keyed by C-number, and
`modules/` the assemblies derived from them. Neither is markdown, so neither is
an OKF concept document; `kb/context.jsonld` is the vocabulary they resolve
through and `tools/kb.py check` is what validates them.
