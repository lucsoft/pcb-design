#!/usr/bin/env python3
"""Electrical rule check for EasyEDA Pro JSON netlists.

Reads a netlist in the format consumed by the EasyEDA Pro extension
`eext-generate-schematic-from-netlist`, cross-references every component
against the local knowledge base in kb/parts/, and reports violations.

    erc.py designs/<name>/netlist.json

Exits 1 if any error-severity finding survives, so it can gate a build.

The checks deliberately overlap: a single mistake (a typo'd net name, say)
usually trips two or three of them, which is what makes the report readable
even when the underlying cause is not obvious.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
KB_PARTS = ROOT / "kb" / "parts"

# Pin types that actively drive a net.
DRIVERS = {"output", "power_out"}
# Pin types that may drive a net some of the time.
WEAK_DRIVERS = {"bidirectional", "tri_state", "open_collector", "open_emitter"}
# Pin types that consume but never drive.
SINKS = {"input", "power_in"}
# Provenance markers that mean "nobody checked this against a datasheet".
UNVERIFIED = {"inferred", "assumed", "guess", ""}

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def first_word(source) -> str:
    """The provenance marker at the head of a source string.

    Sources are written for people — "inferred - by elimination, the only SPI
    signal left" — so matching the whole string against UNVERIFIED silently
    treats every annotated guess as datasheet-backed. Only the first word
    carries the claim.

    Taking everything up to the first non-letter rather than splitting on a
    space, because the punctuation people actually write is unbounded:
    "inferred.", "assumed;", "(inferred)" and "inferred-by-elimination" all
    have to resolve to the marker. Getting this wrong fails in the direction
    that raises errors on guesses, which is the trust the ERC exists to
    protect.
    """
    m = re.search(r"[a-z]+", str(source or "").lower())
    return m.group(0) if m else ""


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    where: str = ""
    hint: str = ""

    def render(self, use_colour: bool) -> str:
        tag = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}[self.severity]
        if use_colour:
            colour = {"error": "\033[31m", "warning": "\033[33m", "info": "\033[36m"}
            tag = f"{colour[self.severity]}{tag}\033[0m"
        loc = f" [{self.where}]" if self.where else ""
        out = f"  {tag} {self.rule}{loc}: {self.message}"
        if self.hint:
            out += f"\n         → {self.hint}"
        return out


@dataclass
class Component:
    key: str           # gge1, gge2, ...
    designator: str    # R1, U2, ...
    lcsc: str          # C25804
    props: dict
    pins: dict         # pin number -> net name
    kb: dict | None = None

    @property
    def category(self) -> str:
        return (self.kb or {}).get("category", "").lower()

    def kb_pins(self) -> dict:
        """Pin number -> KB pin record."""
        return {str(p["number"]): p for p in (self.kb or {}).get("pins", [])}

    def pin_name(self, number: str) -> str:
        rec = self.kb_pins().get(str(number))
        return rec.get("name", number) if rec else number


@dataclass
class Design:
    rails: dict = field(default_factory=dict)   # net -> {voltage, type}
    buses: list = field(default_factory=list)
    ignore: set = field(default_factory=set)    # rule ids to suppress

    @classmethod
    def load(cls, path: Path) -> "Design":
        if not path.exists():
            return cls()
        if yaml is None:
            print(f"note: pyyaml missing, ignoring {path.name}", file=sys.stderr)
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        return cls(
            rails=data.get("rails", {}) or {},
            buses=data.get("buses", []) or [],
            ignore=set(data.get("ignore", []) or []),
        )

    def rail_voltage(self, net: str) -> float | None:
        rec = self.rails.get(net)
        if isinstance(rec, dict):
            v = rec.get("voltage")
            return float(v) if v is not None else None
        if isinstance(rec, (int, float)):
            return float(rec)
        return None

    def is_ground(self, net: str) -> bool:
        rec = self.rails.get(net)
        if isinstance(rec, dict) and rec.get("type") == "ground":
            return True
        return net.upper() in {"GND", "AGND", "DGND", "VSS", "GNDA"}


class Check:
    """Container for the netlist under test plus everything derived from it."""

    def __init__(self, netlist: dict, design: Design, kb: dict):
        self.design = design
        self.findings: list[Finding] = []
        self.components: dict[str, Component] = {}
        self.nets: dict[str, list[tuple[Component, str]]] = defaultdict(list)
        self.raw = netlist

        for key, body in netlist.items():
            props = body.get("props", {}) or {}
            lcsc = str(props.get("Supplier Part", "")).strip()
            comp = Component(
                key=key,
                designator=str(props.get("Designator", "")).strip(),
                lcsc=lcsc,
                props=props,
                pins={str(k): str(v) for k, v in (body.get("pins", {}) or {}).items()},
                kb=kb.get(lcsc),
            )
            self.components[key] = comp
            for pin, net in comp.pins.items():
                if net:
                    self.nets[net].append((comp, pin))

    def add(self, rule, severity, message, where="", hint=""):
        if rule in self.design.ignore:
            return
        self.findings.append(Finding(rule, severity, message, where, hint))

    # -- helpers ---------------------------------------------------------

    def pin_type(self, comp: Component, pin: str) -> str:
        rec = comp.kb_pins().get(str(pin))
        if rec:
            return rec.get("type", "unspecified")
        # Two-pin passives without a KB entry are safe to assume passive.
        if len(comp.pins) == 2 and comp.category in {"resistor", "capacitor", "inductor"}:
            return "passive"
        return "unspecified"

    def unverified(self, comp: Component, field_name: str, pin: str = None) -> bool:
        """Is this field's provenance too weak to raise an error on?

        A pin map is usually one claim, but not always: the W5500 module's
        MISO is identified by elimination while its other eleven pins come
        straight off the datasheet. Recording that as `inferred` for the whole
        map would downgrade eleven good pins; recording it as datasheet-backed
        over-trusts the one guess. So a pin may carry its own `source`, and it
        wins over `provenance.pins` for rules about that pin.
        """
        if pin is not None:
            for rec in (comp.kb or {}).get("pins", []):
                if str(rec.get("number")) == str(pin) or rec.get("name") == pin:
                    own = rec.get("source")
                    if own is not None:
                        return first_word(own) in UNVERIFIED
                    break
        prov = (comp.kb or {}).get("provenance", {})
        return first_word(prov.get(field_name, "")) in UNVERIFIED

    def nets_touching(self, comp: Component) -> set:
        return {n for n in comp.pins.values() if n}

    # -- structural checks ----------------------------------------------

    def check_structure(self):
        keys = list(self.raw.keys())
        expected = [f"gge{i}" for i in range(1, len(keys) + 1)]
        if keys != expected:
            self.add(
                "S1-key-sequence", "error",
                "component keys must be exactly gge1..ggeN in order",
                hint=f"got {keys[:4]}{'...' if len(keys) > 4 else ''}, "
                     f"expected {expected[:4]}{'...' if len(expected) > 4 else ''}; "
                     "the EasyEDA importer indexes on this and fails silently otherwise",
            )

        seen_desig: dict[str, str] = {}
        for comp in self.components.values():
            if not comp.designator:
                self.add("S2-missing-designator", "error",
                         "props.Designator is empty", where=comp.key)
            elif comp.designator in seen_desig:
                self.add("S3-duplicate-designator", "error",
                         f"designator {comp.designator} reused by "
                         f"{seen_desig[comp.designator]} and {comp.key}",
                         where=comp.designator)
            else:
                seen_desig[comp.designator] = comp.key

            if not comp.lcsc:
                self.add("S4-missing-lcsc", "error",
                         "props['Supplier Part'] is empty",
                         where=comp.designator or comp.key,
                         hint="the importer resolves parts by LCSC C-number only; "
                              "an empty value imports nothing")
            elif not re.fullmatch(r"C\d+", comp.lcsc):
                self.add("S4-malformed-lcsc", "error",
                         f"'{comp.lcsc}' is not a valid LCSC C-number",
                         where=comp.designator or comp.key)

            # The importer is case-sensitive and silently drops unknown keys.
            for prop in comp.props:
                if prop in {"Designator", "Supplier Part"}:
                    continue
                if prop.lower() != prop and prop not in {"Value", "DeviceName"}:
                    self.add("S5-prop-case", "warning",
                             f"prop '{prop}' is not lowercase",
                             where=comp.designator or comp.key,
                             hint="the importer expects lowercase keys such as "
                                  "'device_name' and 'value'; others are ignored")

    # -- knowledge-base coverage ----------------------------------------

    def check_kb_coverage(self):
        for comp in self.components.values():
            if not comp.lcsc:
                continue
            if comp.kb is None:
                self.add("K1-unknown-part", "error",
                         f"{comp.lcsc} is not in the knowledge base",
                         where=comp.designator,
                         hint=f"run: tools/kb.py add {comp.lcsc} "
                              "— without a pin map this part cannot be checked at all")
                continue

            kb_pins = comp.kb_pins()
            if not kb_pins:
                self.add("K2-no-pin-map", "warning",
                         f"{comp.lcsc} has no pin map in the knowledge base",
                         where=comp.designator,
                         hint="electrical checks are skipped for this part")
                continue

            for pin in comp.pins:
                if pin not in kb_pins:
                    self.add("K3-unknown-pin", "error",
                             f"pin {pin} does not exist on {comp.lcsc} "
                             f"({len(kb_pins)} pins known)",
                             where=f"{comp.designator}.{pin}",
                             hint="a wrong pin number silently miswires the import")

            for num, rec in kb_pins.items():
                if num in comp.pins and comp.pins[num]:
                    continue
                ptype = rec.get("type", "unspecified")
                name = rec.get("name", num)
                if ptype == "nc":
                    continue
                severity = "error" if ptype in {"power_in", "power_out"} else "warning"
                self.add("K4-unconnected-pin", severity,
                         f"pin {num} ({name}, {ptype}) is not connected",
                         where=f"{comp.designator}.{num}",
                         hint="connect it, or set it to a net named NC to "
                              "record the decision" if severity == "warning" else
                              "a floating supply pin will not work")

            for num, rec in kb_pins.items():
                if rec.get("type") == "nc" and comp.pins.get(num):
                    self.add("K5-nc-connected", "warning",
                             f"pin {num} is marked not-connected but is wired to "
                             f"'{comp.pins[num]}'",
                             where=f"{comp.designator}.{num}",
                             hint="some parts use NC pins internally; check the datasheet")

    # -- connectivity ----------------------------------------------------

    def check_connectivity(self):
        for net, conns in sorted(self.nets.items()):
            if net.upper() == "NC":
                continue
            if len(conns) == 1:
                comp, pin = conns[0]
                self.add("C1-single-pin-net", "error",
                         f"net '{net}' connects only {comp.designator}.{pin} "
                         f"({comp.pin_name(pin)})",
                         where=net,
                         hint="a net with one pin is almost always a typo in the "
                              "net name, or a forgotten connection")

        # Net names that differ only in punctuation are nearly always one net
        # the author meant to be a single node.
        groups: dict[str, list[str]] = defaultdict(list)
        for net in self.nets:
            groups[re.sub(r"[^A-Z0-9]", "", net.upper())].append(net)
        for norm, names in groups.items():
            if len(names) > 1:
                self.add("C2-net-name-collision", "error",
                         "nets differ only in punctuation or case: "
                         + ", ".join(f"'{n}'" for n in sorted(names)),
                         where=sorted(names)[0],
                         hint="these are separate nets in the netlist but read as "
                              "one; pick a single spelling")

        # A one-pin net one edit away from a well-connected net is a typo.
        busy = [n for n, c in self.nets.items() if len(c) >= 2]
        for net, conns in self.nets.items():
            if len(conns) != 1 or net.upper() == "NC":
                continue
            for other in busy:
                if _edit_distance_within_one(net.upper(), other.upper()):
                    self.add("C3-probable-typo", "error",
                             f"net '{net}' has one pin and is one character from "
                             f"'{other}' ({len(self.nets[other])} pins)",
                             where=net,
                             hint=f"did you mean '{other}'?")
                    break

    # -- electrical ------------------------------------------------------

    def check_shorted_parts(self):
        """A two-terminal part with the same net on both pins is a wire.

        Catches the DEGENERATE case only: both of a two-terminal part's pins
        carrying the same net literally. It does NOT catch the likelier form
        of the low-side-shunt mistake, where the output connectors' return is
        called GND and the shunt therefore sits between two legitimately
        different nets - that is a connectivity fact about a different
        component, and no rule inspects it. Nothing else in the checker looks
        at a part's own pins as a set, which is why this one exists.
        """
        SHORTABLE = {"resistor", "inductor", "diode", "capacitor", "fuse"}
        for comp in self.components.values():
            if comp.category not in SHORTABLE:
                continue
            nets = [n for n in comp.pins.values() if n and n.upper() != "NC"]
            if len(nets) < 2 or len(set(nets)) != 1:
                continue
            net = nets[0]
            # A capacitor across one net is a different (and also wrong) thing,
            # but a resistor or inductor shorted out is the one that silently
            # changes what a measurement means.
            sev = "error" if comp.category in ("resistor", "inductor") else "warning"
            self.add("S6-shorted-two-terminal", sev,
                     f"{comp.designator} ({comp.category}) has net '{net}' on "
                     f"every pin, so it is shorted out",
                     where=comp.designator,
                     hint="give each terminal its own net. A sense resistor in a "
                          "ground return needs the load side on its own net, "
                          "joined to ground only at the resistor")

    def check_electrical(self):
        for net, conns in sorted(self.nets.items()):
            if net.upper() == "NC":
                continue
            types = [(c, p, self.pin_type(c, p)) for c, p in conns]
            strong = [(c, p) for c, p, t in types if t in DRIVERS]
            weak = [(c, p) for c, p, t in types if t in WEAK_DRIVERS]
            sinks = [(c, p) for c, p, t in types if t in SINKS]
            passives = [(c, p) for c, p, t in types if t == "passive"]
            unknown = any(t == "unspecified" for _, _, t in types)

            if len(strong) > 1:
                who = ", ".join(f"{c.designator}.{p} ({c.pin_name(p)})"
                                for c, p in strong)
                ground_or_rail = self.design.is_ground(net) or \
                    self.design.rail_voltage(net) is not None
                # Two regulators feeding one rail is a deliberate topology
                # often enough that it is worth a softer verdict.
                sev = "warning" if ground_or_rail else "error"
                self.add("E1-driver-contention", sev,
                         f"net '{net}' is driven by {len(strong)} outputs: {who}",
                         where=net,
                         hint="two push-pull drivers on one node fight each other; "
                              "use open-drain plus a pull-up, or remove one driver")

            if sinks and not strong and not weak and not passives and not unknown:
                who = ", ".join(f"{c.designator}.{p}" for c, p in sinks)
                self.add("E2-no-driver", "warning",
                         f"net '{net}' has only inputs ({who}) and nothing driving it",
                         where=net,
                         hint="floating inputs read as noise; tie it to a rail, "
                              "a driver, or add a pull resistor")

            oc = [(c, p) for c, p, t in types if t == "open_collector"]
            if oc and not passives and not self.design.rail_voltage(net):
                self.add("E3-missing-pullup", "warning",
                         f"net '{net}' has open-drain pins but no pull-up resistor",
                         where=net,
                         hint="open-drain outputs can only pull low; without a "
                              "pull-up the high level never appears")

    def check_voltage(self):
        for net, conns in sorted(self.nets.items()):
            v = self.design.rail_voltage(net)
            if v is None:
                continue
            for comp, pin in conns:
                rec = comp.kb_pins().get(str(pin))
                if not rec:
                    continue
                vmax = rec.get("vMax")
                if vmax is not None and v > float(vmax) + 1e-9:
                    sev = "warning" if self.unverified(comp, "pins", pin) else "error"
                    self.add("P1-overvoltage", sev,
                             f"{comp.designator}.{pin} ({rec.get('name', pin)}) is "
                             f"rated {vmax} V max but sits on '{net}' at {v} V",
                             where=f"{comp.designator}.{pin}",
                             hint="this exceeds the absolute maximum rating"
                                  + (" (pin data is unverified, confirm against "
                                     "the datasheet)" if sev == "warning" else ""))
                vmin = rec.get("vMin")
                if vmin is not None and v < float(vmin) - 1e-9:
                    self.add("P1-undervoltage", "warning",
                             f"{comp.designator}.{pin} needs at least {vmin} V but "
                             f"'{net}' is {v} V",
                             where=f"{comp.designator}.{pin}")

    def check_decoupling(self):
        caps_on: dict[str, int] = defaultdict(int)
        for comp in self.components.values():
            if comp.category != "capacitor" or len(comp.pins) != 2:
                continue
            nets = list(comp.pins.values())
            if any(self.design.is_ground(n) for n in nets):
                for n in nets:
                    if not self.design.is_ground(n):
                        caps_on[n] += 1

        supply_pins: dict[str, list[str]] = defaultdict(list)
        for comp in self.components.values():
            for pin, net in comp.pins.items():
                if self.pin_type(comp, pin) == "power_in" and not self.design.is_ground(net):
                    supply_pins[net].append(f"{comp.designator}.{pin}")

        for net, pins in sorted(supply_pins.items()):
            have, need = caps_on.get(net, 0), len(pins)
            if have == 0:
                self.add("P2-no-decoupling", "error",
                         f"rail '{net}' feeds {need} supply pin(s) with no "
                         f"capacitor to ground",
                         where=net,
                         hint="add 100nF per supply pin, placed close to the pin")
            elif have < need:
                self.add("P2-thin-decoupling", "warning",
                         f"rail '{net}' has {have} capacitor(s) for {need} supply "
                         f"pin(s): {', '.join(pins)}",
                         where=net,
                         hint="the usual rule is one 100nF per supply pin, plus one "
                              "bulk capacitor per rail")

    def check_buses(self):
        for bus in self.design.buses:
            if bus.get("type") != "i2c":
                continue
            for line in ("sda", "scl"):
                net = bus.get(line)
                if not net:
                    continue
                if net not in self.nets:
                    self.add("B1-missing-bus-net", "error",
                             f"design.yaml declares I2C {line.upper()} as '{net}' "
                             f"but no pin connects to it", where=net)
                    continue
                has_r = any(c.category == "resistor" for c, _ in self.nets[net])
                if not has_r:
                    self.add("B2-i2c-no-pullup", "error",
                             f"I2C line '{net}' has no pull-up resistor",
                             where=net,
                             hint="I2C is open-drain; without pull-ups (2.2k-10k to "
                                  "the bus rail) the bus never idles high")

    def check_part_rules(self):
        for comp in self.components.values():
            for rule in (comp.kb or {}).get("rules", []):
                rid = rule.get("id", "unnamed")
                req = rule.get("requires")
                sev = rule.get("severity", "warning")
                note = rule.get("note", "")
                names = {n.upper() for n in rule.get("appliesTo", [])}
                kb_pins = comp.kb_pins()
                targets = [num for num, rec in kb_pins.items()
                           if rec.get("name", "").upper() in names or num in names]
                for num in targets:
                    net = comp.pins.get(num)
                    where = f"{comp.designator}.{num}"
                    if req == "decoupling":
                        if net and self.nets.get(net):
                            has_cap = any(c.category == "capacitor"
                                          for c, _ in self.nets[net])
                            if not has_cap:
                                self.add(f"R-{rid}", sev,
                                         f"{where} requires a decoupling capacitor",
                                         where=where, hint=note)
                    elif req == "pullup":
                        if net and not any(c.category == "resistor"
                                           for c, _ in self.nets.get(net, [])):
                            self.add(f"R-{rid}", sev,
                                     f"{where} requires a pull-up resistor",
                                     where=where, hint=note)
                    elif req == "not-pulled-low":
                        if net and self.design.is_ground(net):
                            self.add(f"R-{rid}", sev,
                                     f"{where} must not be tied to ground",
                                     where=where, hint=note)
                    elif req == "connected":
                        if not net:
                            self.add(f"R-{rid}", sev,
                                     f"{where} must be connected",
                                     where=where, hint=note)

    def check_sourcing(self):
        extended = []
        for comp in self.components.values():
            kb = comp.kb or {}
            if kb.get("tier") == "extended":
                extended.append(f"{comp.designator} ({comp.lcsc})")
            stock = kb.get("stock")
            if isinstance(stock, int) and stock < 100:
                self.add("Q2-low-stock", "warning",
                         f"{comp.lcsc} has {stock} in stock at LCSC",
                         where=comp.designator,
                         hint="pick an alternative before this goes out of stock")
        if extended:
            self.add("Q1-extended-parts", "info",
                     f"{len(extended)} extended-tier part(s): {', '.join(extended)}",
                     hint="JLCPCB charges a one-off feeder fee per extended part; "
                          "basic-tier equivalents avoid it")

    def run(self):
        self.check_structure()
        self.check_kb_coverage()
        self.check_connectivity()
        self.check_shorted_parts()
        self.check_electrical()
        self.check_voltage()
        self.check_decoupling()
        self.check_buses()
        self.check_part_rules()
        self.check_sourcing()
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule, f.where))
        return self.findings


def _edit_distance_within_one(a: str, b: str) -> bool:
    """True if a and b differ by at most one insert, delete, or substitution."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def load_kb() -> dict:
    kb = {}
    for path in sorted(KB_PARTS.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"warning: {path.name} is not valid JSON ({exc})", file=sys.stderr)
            continue
        key = data.get("lcscId") or path.stem
        kb[key] = data
    return kb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", type=Path)
    ap.add_argument("--design", type=Path,
                    help="design.yaml with rails and buses "
                         "(default: alongside the netlist)")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    args = ap.parse_args()

    if not args.netlist.exists():
        print(f"error: {args.netlist} not found", file=sys.stderr)
        return 2
    netlist = json.loads(args.netlist.read_text())
    design_path = args.design or args.netlist.parent / "design.yaml"
    design = Design.load(design_path)

    findings = Check(netlist, design, load_kb()).run()

    if args.json:
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        use_colour = sys.stdout.isatty()
        counts = defaultdict(int)
        for f in findings:
            counts[f.severity] += 1
        ncomp = len(netlist)
        nnets = len({n for c in netlist.values()
                     for n in (c.get("pins", {}) or {}).values() if n})
        print(f"\nERC {args.netlist}  —  {ncomp} components, {nnets} nets")
        if design_path.exists():
            print(f"    rails from {design_path.name}: "
                  f"{', '.join(design.rails) or 'none'}")
        print()
        if not findings:
            print("  no findings\n")
        else:
            for f in findings:
                print(f.render(use_colour))
            print()
        print(f"  {counts['error']} error(s), {counts['warning']} warning(s), "
              f"{counts['info']} info\n")

    failed = any(f.severity == "error" for f in findings) or (
        args.strict and any(f.severity == "warning" for f in findings))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
