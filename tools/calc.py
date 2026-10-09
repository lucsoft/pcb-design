#!/usr/bin/env python3
"""Hold a design's derived numbers in one place, and check the prose against it.

    ./tools/calc.py eval     designs/led-matrix-controller
    ./tools/calc.py check    designs/led-matrix-controller
    ./tools/calc.py affected designs/led-matrix-controller V_BUS
    ./tools/calc.py render   designs/led-matrix-controller

The design phase of this project burned more time on arithmetic than on
anything else. One design session ran 854 throwaway Python scripts; 250 of
them restated the 36 V bus, 55 the buck efficiency, 47 the eFuse R_DS(on).
Across 29 review rounds the same quantities came back round after round --
the shunt in 24 of 37 review reports, the emitter follower in 18, the UVLO
divider in 12 -- because a reviewer handed a document full of derived
numbers has no way to check one except to derive it again. And when an input
moved, nothing could answer "which of the hundred numbers in the README did
that invalidate?". One commit here is literally titled *recompute every value
the UVLO change invalidated*.

So the numbers live in `designs/<name>/calc.yaml`: inputs with a source,
derived values as expressions over them. The prose cites the model instead of
restating it, two ways:

    **165 ms**<!--calc:t_dvdt-->          an anchored value, checked
    <!-- calc:table rail-budget -->       a block, regenerated
    ...
    <!-- calc:end -->

`check` verifies every anchor against the model at the precision the prose
displays, and every generated block against a fresh render. `affected` walks
the dependency graph the other way: change an input, and it names the values
and the document lines that have to follow.

Three things it deliberately does not do:

  It does not pass a claim it could not read. An anchor whose number will not
  parse, a cell whose expression will not evaluate, a unit string it does not
  know -- all are reported as UNCHECKED. A number nobody verified must not
  read as a number that was verified.

  It does not accept an input without a `source`. That is the same rule the
  pin maps live under: data good enough to raise errors on has to say where it
  came from.

  It does not trust a declared unit. Dimensions are inferred through the
  expression and compared with what the entry claims, which is what catches a
  division that should have been a multiplication -- the exact shape of the
  bus-current error this design made once. A disagreement is a warning, not an
  error, because a bare constant in an expression is dimensionless to the
  checker and need not be wrong.

Exit status is 1 on any error, so it gates a document the way erc.py gates a
netlist.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import pathlib
import re
import sys

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - the nix-shell supplies it
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent

PROVENANCE = ("datasheet", "measured", "calculated", "chosen",
              "inferred", "assumed")
WEAK = ("inferred", "assumed")


# --------------------------------------------------------------------------
# units
#
# Dimensions are kept over electrical bases rather than SI ones: volts and
# amperes instead of kilograms and seconds-cubed. Every quantity this project
# handles is expressible that way, and the exponents stay readable.
# --------------------------------------------------------------------------

BASE = ("V", "A", "s", "m", "g", "K")

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
          "m": 1e-3, "c": 1e-2, "d": 1e-1, "k": 1e3, "M": 1e6, "G": 1e9}

# symbol -> (scale, dimensions). Anything derived from the bases is spelled
# out here once; prefixes are applied on top.
UNIT = {
    "": (1.0, {}),
    "V": (1.0, {"V": 1}),
    "A": (1.0, {"A": 1}),
    "s": (1.0, {"s": 1}),
    "m": (1.0, {"m": 1}),
    "g": (1.0, {"g": 1}),
    "K": (1.0, {"K": 1}),
    "W": (1.0, {"V": 1, "A": 1}),
    "VA": (1.0, {"V": 1, "A": 1}),
    "ohm": (1.0, {"V": 1, "A": -1}),
    "Ω": (1.0, {"V": 1, "A": -1}),
    "F": (1.0, {"A": 1, "s": 1, "V": -1}),
    "H": (1.0, {"V": 1, "s": 1, "A": -1}),
    "C": (1.0, {"A": 1, "s": 1}),
    "Ah": (3600.0, {"A": 1, "s": 1}),
    "J": (1.0, {"V": 1, "A": 1, "s": 1}),
    "Wh": (3600.0, {"V": 1, "A": 1, "s": 1}),
    "Hz": (1.0, {"s": -1}),
    "bps": (1.0, {"s": -1}),
    "fps": (1.0, {"s": -1}),
    "min": (60.0, {"s": 1}),
    "h": (3600.0, {"s": 1}),
    "degC": (1.0, {"K": 1}),      # a temperature DIFFERENCE; offsets are not
    "°C": (1.0, {"K": 1}),   # modelled, and a rise is what gets derived
    "%": (0.01, {}),
    "x": (1.0, {}),
    "×": (1.0, {}),
    "ppm": (1e-6, {}),
}

# A symbol that is a unit in its own right is looked up before the prefix rule
# is tried, so "min" cannot become milli-inches. UNIT is therefore the whole
# defence, and anything ambiguous belongs in it rather than in a second list.


class UnitError(ValueError):
    pass


def dim_mul(a, b, sign=1):
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + sign * v
        if out[k] == 0:
            del out[k]
    return out


def dim_str(d):
    if not d:
        return "1"
    pos = "".join(k + (f"^{v}" if v != 1 else "") for k, v in sorted(d.items()) if v > 0)
    neg = "".join(k + (f"^{-v}" if v != -1 else "") for k, v in sorted(d.items()) if v < 0)
    return (pos or "1") + (f"/{neg}" if neg else "")


def unit_atom(sym: str, extra=None):
    """One symbol with an optional prefix -> (scale, dims)."""
    if extra and sym in extra:
        return extra[sym]
    if sym in UNIT:
        return UNIT[sym]
    # mm2 and friends: a trailing integer is an exponent on the symbol
    m = re.fullmatch(r"(.+?)(\d+)", sym)
    if m:
        scale, dims = unit_atom(m.group(1), extra)
        e = int(m.group(2))
        return scale ** e, {k: v * e for k, v in dims.items()}
    if len(sym) > 1 and sym[0] in PREFIX:
        rest = sym[1:]
        if rest in UNIT:
            scale, dims = UNIT[rest]
            return scale * PREFIX[sym[0]], dims
    raise UnitError(sym)


def parse_unit(text: str, extra=None):
    """'ohm*mm2/m' -> (scale, dims). Raises UnitError on anything unknown.

    `extra` is a design's own table of count labels -- "modules", "LEDs" --
    declared under meta.units. They are dimensionless and exist so the prose
    can write what it means without the checker having to guess which trailing
    words are units and which are nouns.
    """
    text = (text or "").strip()
    if text in ("", "1", "-"):
        return 1.0, {}
    scale, dims = 1.0, {}
    # split keeping the operator that preceded each term
    terms = re.split(r"([*/·])", text.replace(" ", ""))
    op = "*"
    for tok in terms:
        if tok in ("*", "/", "·"):
            op = "/" if tok == "/" else "*"
            continue
        if not tok:
            continue
        m = re.fullmatch(r"([^\^]+)(?:\^(-?\d+))?", tok)
        if not m:
            raise UnitError(tok)
        s, d = unit_atom(m.group(1), extra)
        e = int(m.group(2) or 1)
        if op == "/":
            e = -e
        scale *= s ** e
        dims = dim_mul(dims, {k: v * e for k, v in d.items()})
    return scale, dims


# --------------------------------------------------------------------------
# expressions
# --------------------------------------------------------------------------

FUNCS = {"min": min, "max": max, "abs": abs, "sqrt": math.sqrt,
         "log": math.log, "log10": math.log10, "exp": math.exp,
         "floor": math.floor, "ceil": math.ceil, "round": round,
         "pi": math.pi}

_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load,
                  ast.Constant, ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div,
                  ast.Pow, ast.USub, ast.UAdd, ast.Mod, ast.Compare, ast.Lt,
                  ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.IfExp, ast.Tuple)


def parse_expr(src: str) -> ast.Expression:
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"{type(node).__name__} is not allowed in an expression")
    return tree


def expr_names(src: str):
    return {n.id for n in ast.walk(parse_expr(src))
            if isinstance(n, ast.Name) and n.id not in FUNCS}


def eval_expr(src: str, env: dict):
    return eval(compile(parse_expr(src), "<calc>", "eval"),  # noqa: S307
                {"__builtins__": {}}, dict(FUNCS, **env))


class BareLiteral(Exception):
    """Raised when a dimension cannot be inferred because a constant is bare."""


def infer_dims(node, dims_of, bare):
    """Dimensions of an expression, or raise BareLiteral / UnitError."""
    if isinstance(node, ast.Expression):
        return infer_dims(node.body, dims_of, bare)
    if isinstance(node, ast.Constant):
        if node.value not in (0, 1, 2, 0.5, -1):
            bare.append(node.value)
        return {}
    if isinstance(node, ast.Name):
        if node.id in dims_of:
            return dims_of[node.id]
        raise BareLiteral(node.id)
    if isinstance(node, ast.UnaryOp):
        return infer_dims(node.operand, dims_of, bare)
    if isinstance(node, ast.BinOp):
        left = infer_dims(node.left, dims_of, bare)
        right = infer_dims(node.right, dims_of, bare)
        if isinstance(node.op, ast.Mult):
            return dim_mul(left, right)
        if isinstance(node.op, ast.Div):
            return dim_mul(left, right, -1)
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mod)):
            if left != right:
                raise UnitError(f"{dim_str(left)} + {dim_str(right)}")
            return left
        if isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant):
                raise BareLiteral("variable exponent")
            e = node.right.value
            if e == int(e):
                return {k: int(v * e) for k, v in left.items()}
            if all((v * e) == int(v * e) for v in left.values()):
                return {k: int(v * e) for k, v in left.items()}
            raise BareLiteral("fractional power")
    if isinstance(node, ast.Call):
        fn = node.func.id if isinstance(node.func, ast.Name) else ""
        args = [infer_dims(a, dims_of, bare) for a in node.args]
        if fn in ("min", "max", "abs", "round", "floor", "ceil"):
            if args and any(a != args[0] for a in args):
                raise UnitError(f"{fn}({', '.join(dim_str(a) for a in args)})")
            return args[0] if args else {}
        if fn == "sqrt":
            d = args[0]
            if all(v % 2 == 0 for v in d.values()):
                return {k: v // 2 for k, v in d.items()}
            raise BareLiteral("sqrt of odd dimension")
        if fn in ("log", "log10", "exp"):
            if args[0]:
                raise UnitError(f"{fn}({dim_str(args[0])})")
            return {}
    if isinstance(node, ast.IfExp):
        return infer_dims(node.body, dims_of, bare)
    if isinstance(node, ast.Compare):
        return {}
    raise BareLiteral(type(node).__name__)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

class Finding:
    def __init__(self, level, where, message, hint=""):
        self.level, self.where, self.message, self.hint = level, where, message, hint

    def as_dict(self):
        return {"level": self.level, "where": self.where,
                "message": self.message, "hint": self.hint}


class Model:
    """A design's quantities: inputs, derived values, and generated tables."""

    def __init__(self, design: pathlib.Path):
        self.design = design
        self.path = design / "calc.yaml"
        self.findings: list[Finding] = []
        self.values: dict[str, float] = {}
        self.dims: dict[str, dict] = {}
        self.entries: dict[str, dict] = {}
        self.deps: dict[str, set] = {}
        self.order: list[str] = []
        self.tables: dict[str, dict] = {}
        self.units: dict[str, tuple] = {}
        self.documents: list[str] = ["README.md"]
        self._load()

    # -- loading ----------------------------------------------------------

    def add(self, level, where, message, hint=""):
        self.findings.append(Finding(level, where, message, hint))

    def _load(self):
        if yaml is None:
            raise SystemExit("pyyaml missing; run inside nix-shell")
        if not self.path.exists():
            raise SystemExit(f"no calc.yaml in {self.design}")
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        meta = data.get("meta") or {}
        if meta.get("documents"):
            self.documents = list(meta["documents"])
        self.tables = data.get("tables") or {}
        for label, as_unit in (meta.get("units") or {}).items():
            try:
                self.units[label] = parse_unit(as_unit or "")
            except UnitError as e:
                self.add("error", f"meta.units.{label}",
                         f"is declared as '{as_unit}', which is not a unit ({e})")

        for name, spec in (data.get("inputs") or {}).items():
            spec = dict(spec or {})
            spec["kind"] = "input"
            self.entries[name] = spec
        for name, spec in (data.get("derived") or {}).items():
            spec = dict(spec or {})
            spec["kind"] = "derived"
            if name in self.entries:
                self.add("error", name, "declared as both an input and derived")
            self.entries[name] = spec

        self._validate()
        self._evaluate()

    def _validate(self):
        for name, spec in self.entries.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                self.add("error", name, "not a usable identifier")
            if name in FUNCS:
                self.add("error", name, "shadows a built-in function")
            if spec["kind"] == "input":
                if "value" not in spec:
                    self.add("error", name, "input has no value")
                if not str(spec.get("source", "")).strip():
                    self.add("error", name, "input has no source",
                             "say where the number came from -- datasheet page, "
                             "measurement, or 'chosen' with the reason")
                prov = spec.get("provenance", "chosen")
                if prov not in PROVENANCE:
                    self.add("error", name,
                             f"provenance '{prov}' is not one of {', '.join(PROVENANCE)}")
            else:
                if not str(spec.get("expr", "")).strip():
                    self.add("error", name, "derived value has no expr")
            scale, dims = self._unit_of(name, spec)
            spec["_scale"], spec["_dims"] = scale, dims

    def unit(self, text):
        return parse_unit(text, self.units)

    def _unit_of(self, name, spec):
        try:
            return self.unit(spec.get("unit", ""))
        except UnitError as e:
            self.add("error", name, f"unit '{spec.get('unit')}' is not understood ({e})",
                     "spell it from V A s m g K and the usual derived symbols, "
                     "or add it to UNIT in tools/calc.py")
            return 1.0, None

    # -- evaluation -------------------------------------------------------

    def _evaluate(self):
        for name, spec in self.entries.items():
            if spec["kind"] == "derived":
                try:
                    self.deps[name] = expr_names(spec["expr"])
                except (SyntaxError, ValueError) as e:
                    self.add("error", name, f"expr does not parse: {e}")
                    self.deps[name] = set()
            else:
                self.deps[name] = set()

        for name, deps in self.deps.items():
            for d in sorted(deps):
                if d not in self.entries:
                    self.add("error", name, f"expr references unknown '{d}'",
                             "declare it as an input with a source, or fix the name")

        # Kahn, so a cycle is reported rather than hit as recursion
        ready = [n for n, d in self.deps.items() if not (d & self.entries.keys())]
        seen, order = set(ready), list(ready)
        while ready:
            cur = ready.pop(0)
            for name, deps in self.deps.items():
                if name in seen or not (deps & self.entries.keys()) <= seen:
                    continue
                seen.add(name)
                order.append(name)
                ready.append(name)
        for name in self.entries:
            if name not in seen:
                self.add("error", name, "is part of a dependency cycle")
        self.order = order

        for name in order:
            spec = self.entries[name]
            if spec["kind"] == "input":
                try:
                    self.values[name] = float(spec.get("value", 0)) * spec["_scale"]
                except (TypeError, ValueError):
                    self.add("error", name, f"value {spec.get('value')!r} is not a number")
                    continue
            else:
                try:
                    self.values[name] = float(eval_expr(spec["expr"], self.values))
                except Exception as e:  # noqa: BLE001 - any failure is a finding
                    self.add("error", name, f"expr failed: {type(e).__name__}: {e}")
                    continue
            if spec["_dims"] is not None:
                self.dims[name] = spec["_dims"]

        self._check_dimensions()
        self._check_limits()
        self._check_provenance()

    def _check_dimensions(self):
        for name in self.order:
            spec = self.entries[name]
            if spec["kind"] != "derived" or spec["_dims"] is None:
                continue
            bare = []
            try:
                got = infer_dims(parse_expr(spec["expr"]), self.dims, bare)
            except BareLiteral:
                continue
            except (UnitError, SyntaxError, ValueError) as e:
                self.add("warning", name, f"dimensions do not combine: {e}",
                         "an addition needs both sides in the same unit")
                continue
            if got != spec["_dims"]:
                hint = ("check the expression before the unit -- a division "
                        "where a multiplication belongs reads exactly like this")
                if bare:
                    hint = (f"the expression carries bare constants ({', '.join(str(b) for b in bare[:3])}); "
                            "declare them as inputs with their own units and this "
                            "check becomes exact")
                self.add("warning", name,
                         f"declares {spec.get('unit') or '1'} ({dim_str(spec['_dims'])}) "
                         f"but the expression gives {dim_str(got)}", hint)

    def _check_limits(self):
        for name in self.order:
            lim = self.entries[name].get("limit")
            if not lim or name not in self.values:
                continue
            for bound, cmp in (("max", lambda v, l: v <= l), ("min", lambda v, l: v >= l)):
                if bound not in lim:
                    continue
                try:
                    limit = float(eval_expr(str(lim[bound]), self.values))
                except Exception as e:  # noqa: BLE001
                    self.add("unchecked", name, f"limit {bound} does not evaluate: {e}")
                    continue
                v = self.values[name]
                if not cmp(v, limit):
                    self.add("error", name,
                             f"{self.show(name)} violates {bound} "
                             f"{self.show(name, limit)}"
                             + (f" -- {lim['why']}" if lim.get("why") else ""))

    def _check_provenance(self):
        """A derived value is only as good as its weakest input."""
        for name in self.order:
            spec = self.entries[name]
            if spec["kind"] != "derived":
                continue
            weak = sorted(self.weakest(name))
            if weak:
                spec["_weak"] = weak

    def weakest(self, name, seen=None):
        seen = seen or set()
        out = set()
        for d in self.deps.get(name, ()):
            if d in seen or d not in self.entries:
                continue
            seen.add(d)
            spec = self.entries[d]
            if spec["kind"] == "input":
                if spec.get("provenance", "chosen") in WEAK:
                    out.add(d)
            else:
                out |= self.weakest(d, seen)
        return out

    # -- display ----------------------------------------------------------

    def display(self, name):
        spec = self.entries.get(name, {})
        d = spec.get("display") or {}
        unit = d.get("unit", spec.get("unit", ""))
        digits = int(d.get("digits", 3))
        try:
            scale, _ = self.unit(unit)
        except UnitError:
            scale, unit = 1.0, spec.get("unit", "")
        return unit, digits, scale

    def show(self, name, value=None):
        unit, digits, scale = self.display(name)
        v = self.values.get(name) if value is None else value
        if v is None:
            return "?"
        return fmt(v / scale, digits) + suffix(unit)

    # -- dependency graph -------------------------------------------------

    def dependents(self, name):
        """Everything downstream of `name`, transitively."""
        out, frontier = set(), {name}
        while frontier:
            nxt = set()
            for cand, deps in self.deps.items():
                if cand not in out and deps & frontier:
                    out.add(cand)
                    nxt.add(cand)
            frontier = nxt
        return out


# Prose writes "70%" and "1.19x" closed up and "36 V" with a space, so the
# rendered form has to as well -- otherwise regenerating a table rewrites its
# typography, and the diff stops being about the numbers.
TIGHT = {"%", "x", "×", "°C"}


def suffix(unit):
    if not unit:
        return ""
    return unit if unit in TIGHT else " " + unit


def fmt(value, digits):
    """Round to a significant-figure count and print it the way prose does.

    %g turns 487 at two figures into "4.9e+02", which no document writes and
    no comparison against a document should produce. Round at the figure
    count, then print with the decimals that leaves.
    """
    if value == 0:
        return "0"
    if abs(value) >= 1e6 or abs(value) < 1e-4:
        return f"{value:.{digits}g}".replace("-", "−")
    places = digits - 1 - math.floor(math.log10(abs(value)))
    rounded = round(value, places)
    # rounding 9.99 up can carry into the next decade, so settle the exponent
    # on the rounded value rather than the original
    if rounded and math.floor(math.log10(abs(rounded))) != math.floor(math.log10(abs(value))):
        places = digits - 1 - math.floor(math.log10(abs(rounded)))
        rounded = round(value, places)
    # U+2212, because that is the minus sign this project's prose uses and a
    # regenerated table has to match the typography around it
    return f"{rounded:.{max(0, places)}f}".replace("-", "−")


def sigfigs(text: str) -> int:
    """Significant figures in a number as the prose writes it."""
    t = text.strip().lstrip("+-−")
    t = re.sub(r"[eE][-+]?\d+$", "", t)
    if "." in t:
        whole, frac = t.split(".", 1)
        whole = whole.lstrip("0")
        return len(whole) + len(frac) if whole else len(frac.lstrip("0")) or 1
    return len(t.strip("0")) or 1


# --------------------------------------------------------------------------
# the document side: anchored values and generated blocks
# --------------------------------------------------------------------------

ANCHOR = re.compile(r"<!--\s*calc:([A-Za-z_][A-Za-z0-9_]*(?:\.\.[A-Za-z_][A-Za-z0-9_]*)?)\s*-->")
TABLE_OPEN = re.compile(r"<!--\s*calc:table\s+([A-Za-z0-9_.-]+)\s*-->")
TABLE_END = re.compile(r"<!--\s*calc:end\s*-->")

# A unit as the prose writes it: letters, the symbols that are not letters,
# and a trailing digit for mm2. Kept short so it cannot swallow the next word.
U = r"[A-Za-zΩµμ°%×/·^²³]{1,9}\d?"
TRAILING = re.compile(
    r"(?P<num>[-+−]?\d[\d  ]*(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*(?P<unit>" + U + r")?"
    r"[\s*`~)\]\"']*$")
RANGE = re.compile(
    r"(?P<lo>[-+−]?\d[\d  ]*(?:[.,]\d+)?)\s*(?P<lounit>" + U + r")?\s*(?:[-–—]|to|and)\s*"
    r"(?P<hi>[-+−]?\d[\d  ]*(?:[.,]\d+)?)\s*(?P<unit>" + U + r")?"
    r"[\s*`~)\]\"']*$")


def to_number(text: str) -> float:
    t = text.strip().replace(" ", "").replace(" ", "").replace("−", "-")
    t = re.sub(r",(\d{3})\b", r"\1", t)
    return float(t.replace(",", "."))


def clean_unit(text: str | None) -> str:
    if not text:
        return ""
    return (text.replace("²", "2").replace("³", "3")
                .replace("μ", "µ").replace("·", "*").strip())


class Document:
    def __init__(self, path: pathlib.Path, root: pathlib.Path):
        self.path = path
        self.rel = path.relative_to(root) if root in path.parents else path
        self.text = path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()


def check_documents(model: Model, write=False):
    """Check every anchor and every generated block. Returns files rewritten."""
    cited, rewritten = set(), []
    for rel in model.documents:
        path = model.design / rel
        if not path.exists():
            model.add("error", rel, "listed under meta.documents and not present")
            continue
        doc = Document(path, ROOT)
        cited |= check_anchors(model, doc)
        new, used = check_tables(model, doc, write)
        cited |= used
        if new is not None and new != doc.text:
            if write:
                path.write_text(new, encoding="utf-8")
                rewritten.append(str(doc.rel))

    for name in model.order:
        if name in cited or model.entries[name].get("uncited"):
            continue
        model.add("info", name, "is in the model and cited nowhere in the document",
                  "cite it, or drop it if nothing depends on it; "
                  "set uncited: true when it exists only as an intermediate")
    return rewritten


def check_anchors(model: Model, doc: Document):
    cited = set()
    for lineno, line in enumerate(doc.lines, 1):
        for m in ANCHOR.finditer(line):
            where = f"{doc.rel}:{lineno}"
            if m.group(1) == "end":      # the closing marker of a table block
                continue
            names = m.group(1).split("..")
            for n in names:
                if n not in model.entries:
                    model.add("error", where, f"anchor cites unknown '{n}'",
                              "add it to calc.yaml, or correct the name")
                    break
            else:
                cited |= set(names)
                before = line[:m.start()]
                if len(names) == 2:
                    check_range(model, where, before, names)
                else:
                    check_value(model, where, before, names[0])
    return cited


def check_value(model: Model, where, before, name):
    m = TRAILING.search(before.rstrip())
    if not m:
        model.add("unchecked", where,
                  f"anchor for '{name}' has no number in front of it: "
                  f"...{before[-40:].strip()!r}",
                  "put the anchor straight after the value it names")
        return
    try:
        shown = to_number(m.group("num"))
    except ValueError:
        model.add("unchecked", where, f"cannot read the number in front of '{name}'")
        return
    compare(model, where, name, shown, m.group("unit"), m.group("num"))


def check_range(model: Model, where, before, names):
    m = RANGE.search(before.rstrip())
    if not m:
        model.add("unchecked", where,
                  f"anchor for '{names[0]}..{names[1]}' has no range in front of it",
                  "write it as 'lo-hi unit' immediately before the anchor")
        return
    unit = m.group("unit")
    for raw, name, u in ((m.group("lo"), names[0], m.group("lounit") or unit),
                         (m.group("hi"), names[1], unit)):
        try:
            compare(model, where, name, to_number(raw), u, raw)
        except ValueError:
            model.add("unchecked", where, f"cannot read '{raw}' for '{name}'")


def compare(model: Model, where, name, shown, shown_unit, raw):
    if name not in model.values:
        model.add("unchecked", where, f"'{name}' has no value to compare against")
        return
    spec = model.entries[name]
    unit_text = clean_unit(shown_unit)
    model_unit, digits, scale = model.display(name)
    if unit_text:
        try:
            scale, dims = model.unit(unit_text)
        except UnitError:
            model.add("unchecked", where,
                      f"'{name}' is shown in '{shown_unit}', which is not a unit "
                      f"this checker knows",
                      "add it to UNIT in tools/calc.py, or write the value in a "
                      "unit it has")
            return
        if spec["_dims"] is not None and dims != spec["_dims"]:
            model.add("error", where,
                      f"'{name}' is {spec.get('unit') or 'dimensionless'} and the "
                      f"document shows it in {shown_unit}")
            return
    value = model.values[name]
    sig = sigfigs(raw)
    tol = spec.get("tol")
    if tol is not None:
        ok = abs(shown * scale - value) <= abs(value) * float(tol)
    else:
        ok = fmt(value / scale, sig) == fmt(shown, sig)
    if not ok:
        model.add("error", where,
                  f"document says {raw}{' ' + shown_unit if shown_unit else ''} "
                  f"for '{name}', the model gives "
                  f"{fmt(value / scale, max(sig, digits))}"
                  f"{' ' + (unit_text or model_unit) if (unit_text or model_unit) else ''}",
                  f"{spec.get('expr', '')}".strip() or spec.get("source", ""))


CELL = re.compile(r"^=\s*(?P<expr>.+?)(?:\s*@\s*(?P<unit>[^,]*?)\s*(?:,\s*(?P<digits>\d+))?)?$")
INLINE = re.compile(r"\{([^{}]+)\}")


def render_cell(model: Model, raw, where, used: set):
    """A table cell: '=expr [@ unit,digits]', or text with {name} in it."""
    if not isinstance(raw, str):
        return str(raw)
    text = raw.strip()
    if text.startswith("\\="):            # a cell whose text really does start with =
        return text[1:]
    m = CELL.match(text)
    if not m:
        def sub(mm):
            inner = mm.group(1).strip()
            return render_cell(model, "=" + inner if not inner.startswith("=") else inner,
                               where, used)
        return INLINE.sub(sub, text)
    expr, unit, digits = m.group("expr"), m.group("unit"), m.group("digits")
    used |= {n for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr) if n in model.entries}
    if unit is None and expr.strip() in model.entries:
        return model.show(expr.strip())
    try:
        value = float(eval_expr(expr, model.values))
    except Exception as e:  # noqa: BLE001
        # A cell opening with "=" is a promise that it is an expression, so a
        # failure here is a broken model rather than something unknowable.
        # Literal text that starts with "=" is escaped as "\=".
        model.add("error", where, f"cell '{raw}' does not evaluate: {e}",
                  "escape it as '\\=' if the cell is meant to read as text")
        return text
    unit = clean_unit(unit)
    # "@ [mA],3" scales to mA and prints the bare number, for a table whose
    # column heading already carries the unit.
    silent = unit.startswith("[") and unit.endswith("]")
    if silent:
        unit = unit[1:-1]
    try:
        scale, _ = model.unit(unit)
    except UnitError:
        model.add("unchecked", where, f"cell '{raw}' names an unknown unit '{unit}'")
        return "?"
    return fmt(value / scale, int(digits or 3)) + ("" if silent else suffix(unit))


def render_table(model: Model, key, where, used: set):
    spec = model.tables.get(key)
    if spec is None:
        model.add("error", where, f"no table '{key}' in calc.yaml")
        return None
    cols = list(spec.get("columns") or [])
    align = list(spec.get("align") or ["left"] * len(cols))
    rows = [[render_cell(model, c, where, used) for c in row]
            for row in (spec.get("rows") or [])]
    bad = [i for i, r in enumerate(rows) if len(r) != len(cols)]
    if bad:
        model.add("error", where,
                  f"table '{key}' has {len(cols)} columns and row "
                  f"{bad[0] + 1} has {len(rows[bad[0]])}")
        return None
    sep = {"left": "---", "right": "---:", "center": ":---:"}
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join(sep.get(a, "---") for a in align) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def check_tables(model: Model, doc: Document, write: bool):
    """Regenerate every calc:table block; report or rewrite a difference."""
    out, used, i, changed = [], set(), 0, False
    while i < len(doc.lines):
        line = doc.lines[i]
        m = TABLE_OPEN.search(line)
        out.append(line)
        i += 1
        if not m:
            continue
        key = m.group(1)
        start = i
        while i < len(doc.lines) and not TABLE_END.search(doc.lines[i]):
            i += 1
        if i >= len(doc.lines):
            model.add("error", f"{doc.rel}:{start}",
                      f"table '{key}' is opened and never closed",
                      "close it with <!-- calc:end -->")
            return None, used
        where = f"{doc.rel}:{start}"
        body = render_table(model, key, where, used)
        present = [l for l in doc.lines[start:i]]
        if body is None:
            out += present
        else:
            if [l.rstrip() for l in present if l.strip()] != body:
                changed = True
                if not write:
                    model.add("error", where,
                              f"table '{key}' does not match the model",
                              "run ./tools/calc.py render to regenerate it")
            out += body
        out.append(doc.lines[i])
        i += 1
    text = "\n".join(out) + ("\n" if doc.text.endswith("\n") else "")
    return (text if changed else doc.text), used


# --------------------------------------------------------------------------
# API, for figures.py and anything else that needs the numbers
# --------------------------------------------------------------------------

def values(design) -> dict:
    """The design's quantities, in SI base units, by name."""
    path = pathlib.Path(design)
    if not path.exists():
        path = ROOT / "designs" / str(design)
    model = Model(path)
    errors = [f for f in model.findings if f.level == "error"]
    if errors:
        raise ValueError(f"{path.name}/calc.yaml has {len(errors)} error(s): "
                         f"{errors[0].where}: {errors[0].message}")
    return dict(model.values)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

LEVELS = ("error", "warning", "unchecked", "info")


def report(model: Model, as_json=False, extra=""):
    if as_json:
        print(json.dumps([f.as_dict() for f in model.findings], indent=2))
    else:
        for level in LEVELS:
            for f in (x for x in model.findings if x.level == level):
                print(f"  {level:9s} {f.where}: {f.message}")
                if f.hint:
                    print(f"            -> {f.hint}")
        counts = {l: sum(1 for f in model.findings if f.level == l) for l in LEVELS}
        print()
        if extra:
            print(f"  {extra}")
        print("  " + ", ".join(f"{counts[l]} {l}" for l in LEVELS))
    return 1 if any(f.level == "error" for f in model.findings) else 0


def cmd_eval(model: Model, args):
    width = max((len(n) for n in model.entries), default=4)
    print(f"  {'quantity'.ljust(width)}  {'value':>14}  source / expression")
    print("  " + "-" * (width + 50))
    for name in model.order:
        spec = model.entries[name]
        rhs = spec.get("expr") if spec["kind"] == "derived" else spec.get("source", "")
        flag = ""
        if spec["kind"] == "input" and spec.get("provenance", "chosen") in WEAK:
            flag = f" [{spec['provenance']}]"
        elif spec.get("_weak"):
            flag = f" [rests on {', '.join(spec['_weak'])}]"
        print(f"  {name.ljust(width)}  {model.show(name):>14}  {str(rhs)[:60]}{flag}")
    return report(model, args.json,
                  f"{len(model.entries)} quantities, "
                  f"{sum(1 for s in model.entries.values() if s['kind'] == 'input')} inputs")


def cmd_check(model: Model, args):
    check_documents(model)
    anchors = sum(len(ANCHOR.findall((model.design / d).read_text(encoding='utf-8')))
                  for d in model.documents if (model.design / d).exists())
    tables = sum(len(TABLE_OPEN.findall((model.design / d).read_text(encoding='utf-8')))
                 for d in model.documents if (model.design / d).exists())
    return report(model, args.json,
                  f"checked {anchors} anchored value(s) and {tables} generated "
                  f"table(s) in {', '.join(model.documents)} "
                  f"against {len(model.entries)} quantities")


def cmd_render(model: Model, args):
    rewritten = check_documents(model, write=True)
    return report(model, args.json,
                  f"rewrote {len(rewritten)} file(s): {', '.join(rewritten) or 'none'}")


def cmd_affected(model: Model, args):
    unknown = [n for n in args.names if n not in model.entries]
    for n in unknown:
        print(f"  unknown quantity: {n}", file=sys.stderr)
    if unknown:
        return 1
    hit = set()
    for n in args.names:
        hit |= {n} | model.dependents(n)
    print(f"  changing {', '.join(args.names)} moves {len(hit) - len(args.names)} "
          f"other value(s):")
    for name in model.order:
        if name in hit:
            mark = "*" if name in args.names else " "
            print(f"   {mark} {name:<24} {model.show(name):>14}  "
                  f"{str(model.entries[name].get('expr', '')) [:46]}")
    print()
    places = []
    for rel in model.documents:
        path = model.design / rel
        if not path.exists():
            continue
        doc = Document(path, ROOT)
        for lineno, line in enumerate(doc.lines, 1):
            names = {n for m in ANCHOR.finditer(line) for n in m.group(1).split("..")}
            if names & hit:
                places.append((f"{doc.rel}:{lineno}", ", ".join(sorted(names & hit)),
                               line.strip()[:72]))
        for key, spec in model.tables.items():
            cells = " ".join(str(c) for row in (spec.get("rows") or []) for c in row)
            if {n for n in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cells)} & hit:
                for lineno, line in enumerate(doc.lines, 1):
                    m = TABLE_OPEN.search(line)
                    if m and m.group(1) == key:
                        places.append((f"{doc.rel}:{lineno}", f"table {key}", line.strip()))
    print(f"  and {len(places)} place(s) in the document:")
    for where, which, text in places:
        print(f"    {where:<28} {which}")
        print(f"      {text}")
    print("\n  Generated tables follow from ./tools/calc.py render; anchored values "
          "need the sentence around them re-read.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("eval", "check", "render", "affected"))
    ap.add_argument("design", help="designs/<name>, or the name alone")
    ap.add_argument("names", nargs="*", help="for affected: the quantities that moved")
    ap.add_argument("--json", action="store_true", help="machine-readable findings")
    args = ap.parse_args()

    path = pathlib.Path(args.design)
    if not path.exists():
        path = ROOT / "designs" / args.design
    if not path.exists():
        raise SystemExit(f"no such design: {args.design}")

    model = Model(path)
    if args.command == "affected" and not args.names:
        raise SystemExit("affected needs at least one quantity name")
    return {"eval": cmd_eval, "check": cmd_check,
            "render": cmd_render, "affected": cmd_affected}[args.command](model, args)


if __name__ == "__main__":
    sys.exit(main())
