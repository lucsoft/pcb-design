---
type: Reference
title: Assemblies
description: A board treated as one part, with an operating envelope derived from its components rather than read off a datasheet.
tags: [knowledge-base, assemblies]
status: stable
generated: { by: human:lucsoft, at: 2026-10-01T22:49:49+02:00 }
sources:
  - id: module-v3
    resource: /kb/modules/led-matrix-module-v3.json
    title: The assembly record this directory describes
---

# Assemblies

`kb/parts/` holds facts about single components, each traceable to one
datasheet. This directory holds the level above: a board or sub-board treated
as one thing, with an operating envelope **derived** from its parts.

The derived numbers are the point. No datasheet states that an LED matrix
module tops out near 70% sustained brightness — that falls out of the LED's
per-channel current, the buck's thermal resistance, and the bus voltage
together. Working it out is slow, and re-deriving it every time something
changes is how mistakes get in.

Each assembly records:

- `composedOf` — the parts and quantities, linking back to `kb/parts/`
- `envelope` — voltage, current and power limits of the whole
- `thermalLimit` — where heat binds before power does
- `failureModes` — what is protected, what is not, and what fails first
- `provenance` — per field, same discipline as parts

`failureModes` has earned its place: an IC with thermal shutdown is safe, but
the unprotected catch diode next to it is not, and a reader who only skims the
IC datasheet will not notice.

Mark anything modelled rather than measured as such. Several fields here are
calculated from datasheet figures and have never been checked against
hardware; `verification` says so explicitly, and should be updated once
someone puts a probe on a real board.
