# Boards

* [LED matrix controller](led-matrix-controller/README.md) - USB PD EPR controller driving chained LED matrix modules over a high-voltage bus, with an ESP32-C6 reachable over Ethernet or USB.
* [LED matrix controller: original brief](led-matrix-controller/BRIEF.md) - What the controller board has to do, as specified at the outset: USB PD EPR, UART over the same cable, W5500 Ethernet, and a power budget the firmware adapts to.
* [LED matrix controller: schematic layout plan](led-matrix-controller/LAYOUT-PLAN.md) - Staged plan for making the imported schematic read as a circuit: rail-role rows first, rotation only where it is unambiguous, rail lines only once the record model is settled.

# Worked examples

* [demo-ldo/](demo-ldo/) - A 3.3 V LDO stage that passes the ERC, used to sanity-check the toolchain after changing it.

`demo-ldo/` carries no design record on purpose: it exists to be run, not read.
