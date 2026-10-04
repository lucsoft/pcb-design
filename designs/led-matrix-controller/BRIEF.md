---
type: Brief
title: "LED matrix controller: original brief"
description: What the controller board has to do, as specified at the outset -- USB PD EPR, UART over the same cable, W5500 Ethernet, and a power budget the firmware adapts to.
tags: [led-matrix, requirements]
status: stable
generated: { by: claude-code/claude-opus-5, at: 2026-10-04T13:03:49Z }
sources:
  - id: conversation
    resource: the design conversation of 2026-10-01 and its attachments
    title: Where the requirements were stated
  - id: module-v3
    resource: /kb/modules/led-matrix-module-v3.json
    title: The module the controller has to drive
---

# LED matrix controller — brief

A controller and driver board for a modular LED matrix system, driven from
TouchDesigner. It should connect to a laptop over a single USB cable for
quick work, or run from a standalone supply with control over Ethernet once
the installation is built.

The modules themselves exist and work. What is missing is the board that
powers and drives them: today they run off a lab supply with an ESP32-C6
dev kit, which is fine on a bench and neither portable nor usable at a rave.

## The modules this has to drive

Each module is a 6×6 array of WS2812D-F8 fed in parallel, with its own
XL1509-5.0E1 buck converter stepping the bus down to 5 V at up to 2 A. The
converter takes a bus anywhere from 4.5 V to 40 V, so the controller can run
the bus high and let each module drop it locally. Modules are chained.

The current module revision is recorded in `kb/modules/led-matrix-module-v3.json`.

## Requirements

- **USB PD, following the EPR 180 W specification.** This is what makes the
  single-cable case worth building, and it is also what makes the design
  harder than a 5 V board.
- **UART over the same USB cable as power delivery**, so the PD controller
  and a UART bridge share one connector and both reach the ESP32-C6.
- **Ethernet over SPI**, using a W5500.
- **An ESP32-C6** as the host, which needs a 3.3 V rail available at boot
  before anything else is negotiated.
- **An eFuse**, if it fits the budget.
- **A power budget the firmware adapts to.** The board should negotiate
  whatever the attached supply offers and cap brightness and module count
  to match: plugged into an old laptop, fewer LEDs light; on a full supply,
  all of them do. Making this software-defined is what keeps the module
  system flexible rather than fixed to one power source.
- **UDP control** when running from a standalone supply rather than USB.

## Context and constraints

Layout is done in EasyEDA Pro. The core ICs look settled — HUSB238A-BB001-QN16R
is the candidate for PD — but EPR makes the surrounding design more involved
than a basic PD sink, and the plan for how the parts fit together is the part
that needs working out rather than the part selection.

One detail worth carrying forward: the PD controller exposes a pin for cutting
power, so a supply that overheats and shuts down can drop the MOSFET rather
than browning out the board.

The modules and everything built so far have been manageable. This board is
the first one that is not, which is why it gets written down before it gets
drawn.

## References

Two Discord attachments were shared with the original brief: a DXF export of
the v3 module PCB, and a screenshot of the PD controller's pinout showing the
power-cut pin. Both are signed CDN links and have since expired, so they are
not reproduced here — `kb/modules/led-matrix-module-v3.json` carries what was
read off the first, and the second is superseded by the HUSB238A datasheet in
`kb/parts/`.
