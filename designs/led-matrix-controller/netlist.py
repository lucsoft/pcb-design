#!/usr/bin/env python3
"""Generate netlist.json for the LED matrix controller.

Run it from anywhere; it writes netlist.json next to itself.

**Why a generator and not a hand-written JSON.** The importer requires the
component keys to be exactly `gge1 .. ggeN` with no gaps, and CLAUDE.md names
the consequence: deleting a component means renumbering every key after it.
That is a transcription error waiting to happen on a 200-part board. Here the
keys are assigned by position in COMPONENTS, so deleting a line is just
deleting a line. The JSON is still the committed artefact -- this only decides
what goes in it.

Two things in this file are decisions rather than transcription, and both are
argued in README.md:

- **J2/J3 are absent.** The picoMAX headers carry no LCSC part number, so the
  importer resolves them to nothing. A component with no `Supplier Part`
  imports as a part that silently is not there, which is worse than a part
  that visibly is not there. They are drawn by hand after import, along with
  TP1-TP3.
- **Every deliberately open pin gets its OWN `NC_*` net**, never a shared one.
  erc.py treats any `NC_*` name as "left open on purpose" -- which distinguishes
  it from leaving the pin out of the netlist, where the record is "nobody got
  to it". The per-pin naming is not cosmetic: a single shared `NC` puts all
  thirty on one node, and that is inert only if the IMPORTER also treats the
  name specially, which nothing here can verify. Shared, this board would have
  tied seven enabled 74AHCT541 outputs to the TPS54360B's EN pin and to the
  status chain's last DOUT. Per-pin, it is inert either way.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PARTS = ROOT / "kb" / "parts"

# (designator, lcsc, device_name, value, {pin: net})
#
# Order is schematic order -- power in, rails, MCU, peripherals, channels --
# not designator order, because the gge keys follow it and a reviewer reading
# the JSON should be able to follow the signal.
COMPONENTS = [

    # ---- USB-C inlet and the PD front end -----------------------------
    # The shield tabs are 0/1 and the mid-plate tabs 2/3; see the kb record.
    # Shell bonds straight to GND: there is no separate chassis to isolate.
    ("J1", "C3198004", "CX90B-16P", "USB-C 16P", {
        "A1": "GND", "A4": "VBUS", "A5": "CC1", "A6": "USB_DP",
        "A7": "USB_DM", "A8": "NC_USB_SBU1", "A9": "VBUS", "A12": "GND",
        "B1": "GND", "B4": "VBUS", "B5": "CC2", "B6": "USB_DP",
        "B7": "USB_DM", "B8": "NC_USB_SBU2", "B9": "VBUS", "B12": "GND",
        "0": "GND", "1": "GND", "2": "GND", "3": "GND"}),

    # D+/D- are the MCU's native USB. The two contact pairs are commoned, which
    # is what makes the receptacle orientation-independent for USB 2.0.
    ("D3", "C7420372", "H5VL10B", "ESD 5V", {"1": "USB_DP", "2": "GND"}),
    ("D4", "C7420372", "H5VL10B", "ESD 5V", {"1": "USB_DM", "2": "GND"}),
    ("D5", "C7420372", "H5VL10B", "ESD 5V", {"1": "CC1", "2": "GND"}),
    ("D6", "C7420372", "H5VL10B", "ESD 5V", {"1": "CC2", "2": "GND"}),

    ("C33", "C153036", "FS32X225K101EGG", "2.2uF 100V", {"1": "VBUS", "2": "GND"}),
    ("R2", "C17902", "1206W4F1002T5E", "10k 1206", {"1": "VBUS", "2": "GND"}),
    # SMCJ40CA, not SMAJ36CA. A 36 V standoff against a bus a compliant fixed
    # PDO may hold at 37.8 V indefinitely leaves the leakage unspecified; 40 V
    # clears it. The larger die also has a third the dynamic resistance, so it
    # stays under the 60 V parts' rating to 16.5 A where the SMA part crossed
    # at 7.8 A -- the headline 64.5 V vs 58.1 V compares them at their own
    # different rated surges and is the wrong comparison.
    ("D7", "C19077610", "SMCJ40CA", "TVS 40V", {"1": "VBUS", "2": "GND"}),

    # U1 pin 17 is the exposed pad and the ONLY ground connection.
    # pin 1/2 (D+/D-) stay open: the pair belongs to the MCU's native USB and
    # sharing it would break enumeration. pin 15 (GATE) stays open: there is
    # no external VBUS switch left for it to drive.
    ("U1", "C24833806", "HUSB238A-BB001-QN16R", "PD sink", {
        "1": "NC_PD_DP", "2": "NC_PD_DM", "3": "CC1", "4": "CC2", "5": "BUS_3V3",
        "6": "PD_DEBUG_N", "7": "PD_EN_HVDCP", "8": "PD_ADDR",
        "9": "I2C_SDA", "10": "I2C_SCL", "11": "PD_INT_N", "12": "PD_EN_N",
        "13": "PD_FAULT", "14": "GND", "15": "NC_PD_GATE", "16": "PD_VBUS_SENSE",
        "17": "GND"}),

    ("R21", "C25800", "0402WGF9103TCE", "910k", {"1": "PD_ADDR", "2": "GND"}),
    ("R22", "C25800", "0402WGF9103TCE", "910k", {"1": "PD_DEBUG_N", "2": "GND"}),
    ("R23", "C25800", "0402WGF9103TCE", "910k", {"1": "PD_EN_HVDCP", "2": "GND"}),
    ("R19", "C25744", "0402WGF1002TCE", "10k", {"1": "PD_INT_N", "2": "BUS_3V3"}),
    ("C18", "C7472948", "HGC0402R5105K500NTEJ", "1uF", {"1": "BUS_3V3", "2": "GND"}),
    ("C19", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),

    # The Figure 6 follower. Q3 is an NPN emitter follower, not the vendor's
    # BSS138 source follower: V_BE 0.6-0.8 V against a V_GS of 0.8-1.6 V is
    # what lets the 9-15 V contracts hold their percentage-of-RDO threshold.
    ("R44", "C11457", "0402WGF3241TCE", "3.24k", {"1": "VBUS", "2": "PD_BASE"}),
    ("D1", "C173421", "BZT52C27", "27V zener", {"1": "PD_BASE", "2": "GND"}),
    ("Q3", "C7420357", "MMBT5551", "NPN 160V", {
        "1": "PD_BASE", "2": "PD_VBUS_SENSE", "3": "VBUS"}),
    # D14 holds the VBUS pin up at vSafe5V, where the follower alone misses two
    # 4.0 V thresholds. Reverse-biased by 21.3 V once the follower takes over.
    ("D14", "C7502691", "RB751V-40", "schottky 40V", {
        "1": "PD_VBUS_SENSE", "2": "BUS_5V"}),
    # Do not fit at 36 V. Exists for a <=28 V build, where Q3 and D1 come out.
    ("R48", "C17168", "0402WGF0000TCE", "0R DNF", {
        "1": "VBUS", "2": "PD_VBUS_SENSE"}),

    # ---- 36 V -> 5 V --------------------------------------------------
    # EN is left open deliberately: abs max 8.4 V, and any UVLO divider off a
    # 36 V bus would sit over it. Open gives the internal 4.3 V UVLO.
    ("U4", "C524806", "TPS54360BDDAR", "buck 5V", {
        "1": "SW_BOOT", "2": "VBUS", "3": "NC_BUCK_EN", "4": "RT_SET", "5": "FB_5V",
        "6": "COMP_5V", "7": "GND", "8": "SW_5V", "9": "GND"}),
    ("C5", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "SW_BOOT", "2": "SW_5V"}),
    ("R24", "C25741", "0402WGF1003TCE", "100k", {"1": "RT_SET", "2": "GND"}),
    ("R25", "C53398", "0402WGF5362TCE", "53.6k", {"1": "BUS_5V", "2": "FB_5V"}),
    ("R26", "C11660", "0402WGF1022TCE", "10.2k", {"1": "FB_5V", "2": "GND"}),
    ("R27", "C25900", "0402WGF4701TCE", "4.7k", {"1": "COMP_5V", "2": "COMP_MID"}),
    ("C6", "C106862", "CC0402KRX7R9BB333", "33nF", {"1": "COMP_MID", "2": "GND"}),
    ("C7", "C1527", "0402B151K500NT", "150pF", {"1": "COMP_5V", "2": "GND"}),
    ("C8", "C2840282", "1210B475K101NT", "4.7uF 100V", {"1": "VBUS", "2": "GND"}),
    ("C9", "C513710", "CC0805KKX7R0BB224", "220nF 100V", {"1": "VBUS", "2": "GND"}),
    ("D2", "C2903825", "SS36", "schottky 60V", {"1": "SW_5V", "2": "GND"}),
    ("L1", "C112288", "SPM6530T-100M", "10uH", {"1": "SW_5V", "2": "BUS_5V"}),
    ("C10", "C7432781", "HGC1206R5106K500NSPJ", "10uF 50V", {"1": "BUS_5V", "2": "GND"}),
    ("C11", "C7432781", "HGC1206R5106K500NSPJ", "10uF 50V", {"1": "BUS_5V", "2": "GND"}),

    # ---- 5 V -> 3.3 V -------------------------------------------------
    ("U5", "C479074", "SY8089A1AAC", "buck 3V3", {
        "1": "SY_EN", "2": "GND", "3": "LX_3V3", "4": "BUS_5V", "5": "FB_3V3"}),
    ("R30", "C25741", "0402WGF1003TCE", "100k", {"1": "SY_EN", "2": "BUS_5V"}),
    ("R28", "C25741", "0402WGF1003TCE", "100k", {"1": "BUS_3V3", "2": "FB_3V3"}),
    ("R29", "C43473", "0402WGF2212TCE", "22.1k", {"1": "FB_3V3", "2": "GND"}),
    ("C12", "C15850", "CL21A106KAYNNNE", "10uF 25V", {"1": "BUS_5V", "2": "GND"}),
    ("C14", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_5V", "2": "GND"}),
    ("L2", "C7427146", "ANR6028T2R2M", "2.2uH", {"1": "LX_3V3", "2": "BUS_3V3"}),
    ("C13", "C45783", "CL21A226MAQNNNE", "22uF 25V", {"1": "BUS_3V3", "2": "GND"}),

    # ---- MCU ----------------------------------------------------------
    # Pins 29-37 are the module's thermal pad, broken out by the symbol as nine
    # GND pins. All nine take a net or eight pads go to layout unconnected.
    ("U2", "C5366877", "ESP32-C6-WROOM-1-N8", "MCU", {
        "1": "GND", "2": "BUS_3V3", "3": "MCU_EN",
        "4": "CH1_DI", "5": "CH2_DI", "6": "I2C_SDA", "7": "I2C_SCL",
        "8": "CH1_EN", "9": "CH2_EN", "10": "ETH_SCSN", "11": "PD_EN_N",
        "12": "INA_ALERT", "13": "USB_DM", "14": "USB_DP", "15": "MCU_BOOT",
        "16": "ETH_SCLK", "17": "ETH_MOSI", "18": "ETH_MISO",
        "19": "ETH_RSTN", "20": "ETH_INTN", "21": "PD_INT_N", "22": "NC_MCU_22",
        "23": "LED_DATA_3V3", "24": "UART_RX", "25": "UART_TX",
        "26": "VBUS_ADC", "27": "PGOOD", "28": "GND", "29": "GND",
        "30": "GND", "31": "GND", "32": "GND", "33": "GND", "34": "GND",
        "35": "GND", "36": "GND", "37": "GND"}),
    ("R39", "C25744", "0402WGF1002TCE", "10k", {"1": "MCU_EN", "2": "BUS_3V3"}),
    ("C17", "C7472948", "HGC0402R5105K500NTEJ", "1uF", {"1": "MCU_EN", "2": "GND"}),
    ("C15", "C45783", "CL21A226MAQNNNE", "22uF 25V", {"1": "BUS_3V3", "2": "GND"}),
    ("C16", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),
    ("SW1", "C720477", "TS-1088-AR02016", "BOOT", {"1": "MCU_BOOT", "2": "GND"}),
    ("SW2", "C720477", "TS-1088-AR02016", "EN", {"1": "MCU_EN", "2": "GND"}),
    # GPIO8 has no internal pull and must read high at reset; the strap pull-up
    # doubles as the Ethernet module's deselect-at-reset.
    ("R40", "C25744", "0402WGF1002TCE", "10k", {"1": "ETH_SCSN", "2": "BUS_3V3"}),
    # GPIO15 must not be high-Z. Pull-DOWN, because this is the status chain's
    # data line and a pull-up would hold DIN high through the whole boot window.
    ("R41", "C25744", "0402WGF1002TCE", "10k", {"1": "LED_DATA_3V3", "2": "GND"}),

    # ---- Ethernet module ----------------------------------------------
    ("U3", "C134462", "WIZ850io", "W5500 module", {
        "1": "GND", "2": "GND", "3": "ETH_MOSI", "4": "ETH_SCLK",
        "5": "ETH_SCSN", "6": "ETH_INTN", "7": "ETH_MISO", "8": "ETH_RSTN",
        "9": "NC_ETH_9", "10": "BUS_3V3", "11": "BUS_3V3", "12": "GND"}),
    ("R46", "C25744", "0402WGF1002TCE", "10k", {"1": "ETH_RSTN", "2": "GND"}),
    ("R47", "C25744", "0402WGF1002TCE", "10k", {"1": "ETH_INTN", "2": "BUS_3V3"}),
    ("C32", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),

    # ---- Current sense -------------------------------------------------
    # Low-side: R1 sits between LED_RTN and GND, so IN_P is the LED_RTN side.
    # A0/A1 to GND gives 0x40, which is NOT free choice -- the HUSB238A is at
    # 0x42 and that is also a legal INA226 address.
    # VBUS (pin 8) cannot float; tied to 3V3 with bus sensing done by the ADC.
    ("U6", "C49851", "INA226AIDGSR", "current sense", {
        "1": "GND", "2": "GND", "3": "INA_ALERT", "4": "I2C_SDA",
        "5": "I2C_SCL", "6": "BUS_3V3", "7": "GND", "8": "BUS_3V3",
        "9": "GND", "10": "LED_RTN"}),
    # 5 mOhm, not 10. The INA226's +-81.92 mV differential range is 16.4 A at
    # 5 mOhm and only 8.19 A at 10 -- and this board's own full-white flash is
    # 8.0 A, which would have put normal operation at 98% of full scale with
    # the two eFuses able to pass 11.1 A beyond it. Halving the shunt also
    # halves the LED_RTN ground lift and the dissipation.
    ("R1", "C393074", "RLP25FEGMR005", "5mR 2512", {"1": "LED_RTN", "2": "GND"}),
    ("R45", "C25744", "0402WGF1002TCE", "10k", {"1": "INA_ALERT", "2": "BUS_3V3"}),
    ("C20", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),
    ("R17", "C25900", "0402WGF4701TCE", "4.7k", {"1": "I2C_SDA", "2": "BUS_3V3"}),
    ("R18", "C25900", "0402WGF4701TCE", "4.7k", {"1": "I2C_SCL", "2": "BUS_3V3"}),

    # ---- Bus voltage ADC ------------------------------------------------
    ("R31", "C25790", "0402WGF4703TCE", "470k", {"1": "VBUS", "2": "VBUS_ADC"}),
    ("R32", "C25771", "0402WGF2702TCE", "27k", {"1": "VBUS_ADC", "2": "GND"}),
    ("C36", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "VBUS_ADC", "2": "GND"}),

    # ---- Status chain ---------------------------------------------------
    # OE0 (1) and OE1 (19) are active-low enables and must go to GND or every
    # output stays high-Z. The seven unused A inputs are tied; the seven unused
    # outputs are not, and say so.
    ("U9", "C84548", "74AHCT541PW,118", "level shift", {
        "1": "GND", "2": "LED_DATA_BUF", "3": "GND", "4": "GND", "5": "GND",
        "6": "GND", "7": "GND", "8": "GND", "9": "GND", "10": "GND",
        "11": "NC_BUF_Y7", "12": "NC_BUF_Y6", "13": "NC_BUF_Y5", "14": "NC_BUF_Y4", "15": "NC_BUF_Y3",
        "16": "NC_BUF_Y2", "17": "NC_BUF_Y1", "18": "LED_DATA_5V", "19": "GND",
        "20": "BUS_5V"}),
    ("C23", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_5V", "2": "GND"}),
    ("R34", "C4125", "0402WGF4990TCE", "499R", {
        "1": "LED_DATA_3V3", "2": "LED_DATA_BUF"}),
    ("R33", "C4125", "0402WGF4990TCE", "499R", {
        "1": "LED_DATA_5V", "2": "LED_D1"}),

    # ---- Channel 1 eFuse -------------------------------------------------
    # P_IN (6) connects to IN directly, per p.5. GND (9) is wired IN ADDITION
    # to the PowerPAD (21), which p.5 states outright. UVLO is the UPPER tap of
    # the divider -- swapping it with OVP gives a part that never turns on.
    # MODE open = latch off; firmware owns the retry policy.
    ("U10", "C1849461", "TPS16630PWPR", "eFuse ch1", {
        "1": "VBUS", "2": "VBUS", "3": "VBUS", "4": "NC_EFUSE1_4", "5": "NC_EFUSE1_5",
        "6": "VBUS", "7": "CH1_UVLO", "8": "CH1_OVP", "9": "GND",
        "10": "CH1_DVDT", "11": "CH1_ILIM", "12": "NC_EFUSE1_MODE", "13": "CH1_SHDN",
        "14": "NC_EFUSE1_IMON", "15": "NC_EFUSE1_FLT", "16": "PGOOD", "17": "NC_EFUSE1_17",
        "18": "CH1_36V", "19": "CH1_36V", "20": "CH1_36V", "21": "GND"}),
    ("R5", "C5159692", "FRC0402F7323TS", "732k", {"1": "VBUS", "2": "CH1_UVLO"}),
    ("R7", "C270623", "0402WGF2553TCE", "255k", {"1": "CH1_UVLO", "2": "CH1_OVP"}),
    ("R9", "C2909347", "FRC0402F3002TS", "30.0k", {"1": "CH1_OVP", "2": "GND"}),
    ("C1", "C64705", "CL10B224KB8NNNC", "220nF", {"1": "CH1_DVDT", "2": "GND"}),
    ("R3", "C11457", "0402WGF3241TCE", "3.24k", {"1": "CH1_ILIM", "2": "GND"}),
    ("C3", "C513710", "CC0805KKX7R0BB224", "220nF 100V", {"1": "VBUS", "2": "GND"}),
    ("C34", "C513710", "CC0805KKX7R0BB224", "220nF 100V", {"1": "CH1_36V", "2": "GND"}),
    # Cathode on the output. SS11.1: interrupting the current drives OUT
    # negative against a -0.3 V absolute maximum.
    ("D15", "C12790", "MBRS3100T3G", "schottky 100V", {"1": "CH1_36V", "2": "GND"}),
    ("D8", "C19077610", "SMCJ40CA", "TVS 40V", {"1": "CH1_36V", "2": "LED_RTN"}),
    ("R11", "C2906864", "FRC0402F1001TS", "1k", {"1": "CH1_EN", "2": "CH1_SHDN"}),
    ("R13", "C25744", "0402WGF1002TCE", "10k", {"1": "CH1_SHDN", "2": "GND"}),
    ("Q1", "C7420339", "BSS138", "interlock ch1", {
        "1": "PD_FAULT", "2": "GND", "3": "CH1_SHDN"}),
    # ONE 10k on the shared PD_FAULT net, not one per channel. FAULT/OUT2 is
    # push-pull and drives both interlock gates from a single output, so a
    # second resistor in parallel would halve the pull-down to 5k and load
    # the driver twice as hard for nothing. R16 is dropped; the designator
    # is left unused rather than renumbering 32 resistors after it.
    ("R15", "C25744", "0402WGF1002TCE", "10k", {"1": "PD_FAULT", "2": "GND"}),

    # ---- Channel 2 eFuse -------------------------------------------------
    ("U11", "C1849461", "TPS16630PWPR", "eFuse ch2", {
        "1": "VBUS", "2": "VBUS", "3": "VBUS", "4": "NC_EFUSE2_4", "5": "NC_EFUSE2_5",
        "6": "VBUS", "7": "CH2_UVLO", "8": "CH2_OVP", "9": "GND",
        "10": "CH2_DVDT", "11": "CH2_ILIM", "12": "NC_EFUSE2_MODE", "13": "CH2_SHDN",
        "14": "NC_EFUSE2_IMON", "15": "NC_EFUSE2_FLT", "16": "PGOOD", "17": "NC_EFUSE2_17",
        "18": "CH2_36V", "19": "CH2_36V", "20": "CH2_36V", "21": "GND"}),
    ("R6", "C5159692", "FRC0402F7323TS", "732k", {"1": "VBUS", "2": "CH2_UVLO"}),
    ("R8", "C270623", "0402WGF2553TCE", "255k", {"1": "CH2_UVLO", "2": "CH2_OVP"}),
    ("R10", "C2909347", "FRC0402F3002TS", "30.0k", {"1": "CH2_OVP", "2": "GND"}),
    ("C2", "C64705", "CL10B224KB8NNNC", "220nF", {"1": "CH2_DVDT", "2": "GND"}),
    ("R4", "C11457", "0402WGF3241TCE", "3.24k", {"1": "CH2_ILIM", "2": "GND"}),
    ("C4", "C513710", "CC0805KKX7R0BB224", "220nF 100V", {"1": "VBUS", "2": "GND"}),
    ("C35", "C513710", "CC0805KKX7R0BB224", "220nF 100V", {"1": "CH2_36V", "2": "GND"}),
    ("D16", "C12790", "MBRS3100T3G", "schottky 100V", {"1": "CH2_36V", "2": "GND"}),
    ("D9", "C19077610", "SMCJ40CA", "TVS 40V", {"1": "CH2_36V", "2": "LED_RTN"}),
    ("R12", "C2906864", "FRC0402F1001TS", "1k", {"1": "CH2_EN", "2": "CH2_SHDN"}),
    ("R14", "C25744", "0402WGF1002TCE", "10k", {"1": "CH2_SHDN", "2": "GND"}),
    ("Q2", "C7420339", "BSS138", "interlock ch2", {
        "1": "PD_FAULT", "2": "GND", "3": "CH2_SHDN"}),

    ("R20", "C25744", "0402WGF1002TCE", "10k", {"1": "PGOOD", "2": "BUS_3V3"}),

    # ---- Differential link ------------------------------------------------
    # RE_N to VCC as well as DE: RE is a CMOS input and "unused" is not a state.
    # RO is then permanently high-Z and says so.
    ("U7", "C6395158", "MAX3485CSA-JSM", "RS-485 ch1", {
        "1": "NC_RS485_RO1", "2": "BUS_3V3", "3": "BUS_3V3", "4": "CH1_DI",
        "5": "GND", "6": "CH1_A_DRV", "7": "CH1_B_DRV", "8": "BUS_3V3"}),
    ("C21", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),
    ("R42", "C25744", "0402WGF1002TCE", "10k", {"1": "CH1_DI", "2": "GND"}),
    ("R35", "C138002", "RC0402FR-0733RL", "33R", {"1": "CH1_A_DRV", "2": "CH1_A"}),
    ("R36", "C138002", "RC0402FR-0733RL", "33R", {"1": "CH1_B_DRV", "2": "CH1_B"}),
    ("D10", "C19077529", "SMAJ7.0CA", "TVS 7V", {"1": "CH1_A", "2": "LED_RTN"}),
    ("D11", "C19077529", "SMAJ7.0CA", "TVS 7V", {"1": "CH1_B", "2": "LED_RTN"}),

    ("U8", "C6395158", "MAX3485CSA-JSM", "RS-485 ch2", {
        "1": "NC_RS485_RO2", "2": "BUS_3V3", "3": "BUS_3V3", "4": "CH2_DI",
        "5": "GND", "6": "CH2_A_DRV", "7": "CH2_B_DRV", "8": "BUS_3V3"}),
    ("C22", "C131394", "CC0402KRX7R9BB104", "100nF", {"1": "BUS_3V3", "2": "GND"}),
    ("R43", "C25744", "0402WGF1002TCE", "10k", {"1": "CH2_DI", "2": "GND"}),
    ("R37", "C138002", "RC0402FR-0733RL", "33R", {"1": "CH2_A_DRV", "2": "CH2_A"}),
    ("R38", "C138002", "RC0402FR-0733RL", "33R", {"1": "CH2_B_DRV", "2": "CH2_B"}),
    ("D12", "C19077529", "SMAJ7.0CA", "TVS 7V", {"1": "CH2_A", "2": "LED_RTN"}),
    ("D13", "C19077529", "SMAJ7.0CA", "TVS 7V", {"1": "CH2_B", "2": "LED_RTN"}),

    # ---- Fan header ------------------------------------------------------
    # Do not fit by default. <=50 mA; see Cooling for why the bound is that low.
    ("J4", "C492401", "PZ254V-11-02P", "fan 5V DNF", {"1": "BUS_5V", "2": "GND"}),
]

# The status chain. Eight SK6812-EC20, pin order VDD/DOUT/GND/DIN -- which no
# other SK6812 variant shares, and getting it from the package would put VDD on
# a data pin. One 100 nF per LED; the datasheet calls it essential.
for _i in range(1, 9):
    COMPONENTS.append((
        f"LED{_i}", "C2909058", "SK6812-EC20", "RGB", {
            "1": "BUS_5V",
            "2": f"LED_D{_i + 1}" if _i < 8 else "NC_LED_CHAIN_END",
            "3": "GND",
            "4": f"LED_D{_i}"}))
    COMPONENTS.append((
        f"C{23 + _i}", "C131394", "CC0402KRX7R9BB104", "100nF",
        {"1": "BUS_5V", "2": "GND"}))


def build():
    net = {}
    for i, (desig, lcsc, device, value, pins) in enumerate(COMPONENTS, start=1):
        net[f"gge{i}"] = {
            # Only Designator and Supplier Part keep their case. Supplier Part
            # is the one field the importer resolves by; device_name and value
            # are cosmetic and are ignored entirely.
            "props": {"Designator": desig, "device_name": device,
                      "value": value, "Supplier Part": lcsc},
            "pins": pins,
        }
    return net


def selfcheck(net):
    """Catch what erc.py cannot: a pin map that disagrees with the kb record.

    erc.py checks the numbers it is given against the kb. It cannot check the
    ones that are missing from both -- a part recorded with 12 pins and wired
    with 12 is consistent even if the symbol has 20. eda.py verify covers that
    direction; this covers the designator and net hygiene that is local to
    this file.
    """
    problems = []
    seen = {}
    for key, comp in net.items():
        d = comp["props"]["Designator"]
        if d in seen:
            problems.append(f"{key}: designator {d} already used by {seen[d]}")
        seen[d] = key
        for pin, n in comp["pins"].items():
            if not n:
                problems.append(f"{d}.{pin}: empty net name")

        # device_name is cosmetic -- the importer resolves by Supplier Part
        # alone -- but it is also the string a reviewer reads off the canvas,
        # so a label naming a different manufacturer's part is a trap laid for
        # the one person most likely to catch something else. Fifteen of them
        # had drifted before this check existed.
        lcsc = comp["props"]["Supplier Part"]
        rec = PARTS / f"{lcsc}.json"
        if rec.exists():
            mpn = json.loads(rec.read_text(encoding="utf-8")).get("mpn")
            if mpn and mpn != comp["props"]["device_name"]:
                problems.append(
                    f"{d}: device_name '{comp['props']['device_name']}' is not "
                    f"{lcsc}'s part number '{mpn}'")
        else:
            problems.append(f"{d}: {lcsc} has no kb record")
    return problems


def main():
    net = build()
    problems = selfcheck(net)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    out = HERE / "netlist.json"
    if problems:
        # Do not write. The earlier version printed the problems and wrote the
        # file anyway, which fails OPEN on disk: a netlist with a duplicate
        # designator or an empty net name would sit there looking generated,
        # and the only record that it was rejected is a stderr line nobody
        # re-reads. CLAUDE.md has the matching rule for commit messages.
        print(f"error: {out.name} NOT written — fix the above first",
              file=sys.stderr)
        return 1
    out.write_text(json.dumps(net, indent=2) + "\n", encoding="utf-8")
    pins = sum(len(c["pins"]) for c in net.values())
    nets = {n for c in net.values() for n in c["pins"].values()
            if not n.upper().startswith("NC")}
    print(f"{out.relative_to(ROOT)}: {len(net)} components, {pins} pins, "
          f"{len(nets)} nets")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
