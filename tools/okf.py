#!/usr/bin/env python3
"""Open Knowledge Format (OKF) v0.2 conformance check.

    okf.py [path]        # defaults to the repository root as the bundle

OKF is an open standard for knowledge as markdown with YAML frontmatter,
published by GoogleCloudPlatform/knowledge-catalog. §11 makes three things
normative and everything else soft guidance, so this tool reports them at
different severities: the three conformance rules are errors, the optional
trust/provenance/lifecycle families are checked only where a document opts
into them.

Exits 1 if any error survives, so it gates a build the way erc.py does.

Only `.md` files are in scope. The JSON-LD knowledge base under kb/parts/
is not markdown and is therefore outside OKF entirely -- kb.py check is
what validates that.

Three outcomes, never two. A document this tool could not read is counted
as UNCHECKED and named, not quietly passed: a file nobody validated must
not read as a file that was validated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent

# §3.1 -- these filenames have a defined meaning and are not concept documents.
RESERVED = {"index.md", "log.md"}

# §5.4
STATUSES = {"draft", "stable", "deprecated"}

SPEC_VERSION = "0.2"

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# §7. `<producer>/<version>` for agents and tools, `human:<id>` for a person,
# `process:<id>` for an automated process. The `human:` prefix is the one that
# matters most: §5.3 derives the highest trust tier from it and nothing else,
# so a hand-authored document that writes its author any other way silently
# reads as machine-confirmed.
ACTOR_PREFIXED = re.compile(r"^(?:human|process):[\w.@-]+$")
ACTOR_VERSIONED = re.compile(r"^[\w.-]+/[\w.-]+$")

# Directories that are not part of the bundle.
SKIP_DIRS = {".git", "node_modules", "__pycache__"}


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


def split_frontmatter(text: str):
    """Return (raw_frontmatter, body, error).

    §4: the block is delimited by `---` on its own line at the start of the
    file and a closing `---` on its own line. A file that does not open with
    the delimiter has no frontmatter at all, which is different from one that
    opens it and never closes it -- the second is a truncated document and
    says so.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text, None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]), None
    return None, text, "opens a `---` frontmatter block that is never closed"


def as_aware_datetime(value):
    """Return (datetime|None, reason).

    PyYAML resolves an ISO timestamp into a datetime before this code sees it,
    and a date-only scalar into a `date`. Both arrive as objects rather than
    strings, so checking the string form alone would miss every one of them.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None, "has no UTC offset"
        return value, None
    if isinstance(value, date):
        return None, "is a date, not a datetime"
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, "is not an ISO 8601 datetime"
        if parsed.tzinfo is None:
            return None, "has no UTC offset"
        return parsed, None
    return None, f"is a {type(value).__name__}, not a datetime"


class Check:
    def __init__(self, root: Path, now: datetime):
        self.root = root
        self.now = now
        self.findings: list[Finding] = []
        self.unchecked: list[tuple[str, str]] = []
        self.concepts = 0
        self.reserved_seen = 0

    def add(self, rule, severity, message, where="", hint=""):
        self.findings.append(Finding(rule, severity, message, where, hint))

    def skip(self, where, reason):
        self.unchecked.append((where, reason))

    # ------------------------------------------------------------------ scan

    def documents(self):
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root)
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
                continue
            yield path, rel

    def run(self):
        if yaml is None:
            # Fail closed. Without a parser every §11 rule is unanswerable,
            # and answering "no violations" would be a lie that reads exactly
            # like a clean report.
            for _, rel in self.documents():
                self.skip(str(rel), "pyyaml is missing, so no frontmatter was parsed")
            return self.findings

        for path, rel in self.documents():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.skip(str(rel), f"unreadable: {exc}")
                continue
            if path.name in RESERVED:
                self.reserved_seen += 1
                self.check_reserved(path, rel, text)
            else:
                self.concepts += 1
                self.check_concept(rel, text)
        return self.findings

    # --------------------------------------------------------- §11 rules 1-2

    def check_concept(self, rel, text):
        where = str(rel)
        raw, body, err = split_frontmatter(text)
        if err:
            self.add("O1-no-frontmatter", "error", err, where,
                     "close the block with `---` on its own line")
            return
        if raw is None:
            self.add("O1-no-frontmatter", "error",
                     "no YAML frontmatter block", where,
                     "§11.1: every non-reserved .md file needs one. Open the "
                     "file with `---`, a `type:` line, and a closing `---`")
            return
        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            first = str(exc).split("\n")[0]
            self.add("O1-no-frontmatter", "error",
                     f"frontmatter is not parseable YAML: {first}", where,
                     "§11.1 requires a parseable block")
            return
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            self.add("O1-no-frontmatter", "error",
                     f"frontmatter is a {type(meta).__name__}, not a mapping",
                     where, "§4.1 frontmatter is a YAML mapping of keys")
            return

        kind = meta.get("type")
        if not isinstance(kind, str) or not kind.strip():
            shown = "missing" if "type" not in meta else repr(kind)
            self.add("O2-no-type", "error",
                     f"no non-empty `type` field ({shown})", where,
                     "§11.2: `type` is the only always-required key. A short "
                     "string naming the kind of concept, e.g. `Design Record`")

        self.check_families(meta, body, where)

    # ---------------------------------------------------- §5 optional families

    def check_families(self, meta, body, where):
        """Checked only where a document opts in. Absence is never a finding:
        §11 says a concept missing an optional family must not be rejected."""

        if "generated" in meta:
            gen = meta["generated"]
            if not isinstance(gen, dict):
                self.add("O5-actor-format", "warning",
                         "`generated` is not a mapping", where,
                         "§5.2: `generated: { by: <actor>, at: <datetime> }`")
            else:
                if "by" not in gen:
                    self.add("O5-actor-format", "warning",
                             "`generated` has no `by`", where,
                             "§5.2: `by` is required within `generated`")
                else:
                    self.check_actor(gen["by"], f"{where} generated.by")
                if "at" in gen:
                    self.check_timestamp(gen["at"], f"{where} generated.at")

        if "verified" in meta:
            entries = meta["verified"]
            # §5.2/§11: a bare mapping MUST be read as a one-element list.
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                self.add("O5-actor-format", "warning",
                         "`verified` is neither a list nor a mapping", where,
                         "§5.2: a list of `{ by, at }` events")
                entries = []
            for i, ev in enumerate(entries):
                tag = f"{where} verified[{i}]"
                if not isinstance(ev, dict):
                    self.add("O5-actor-format", "warning",
                             "verification event is not a mapping", tag)
                    continue
                if "by" not in ev:
                    self.add("O5-actor-format", "warning",
                             "verification event has no `by`", tag,
                             "§5.2: each event needs `by` and `at`")
                else:
                    self.check_actor(ev["by"], f"{tag}.by")
                if "at" in ev:
                    self.check_timestamp(ev["at"], f"{tag}.at")

        if "sources" in meta:
            self.check_sources(meta["sources"], body, where)

        if "status" in meta:
            status = meta["status"]
            if status not in STATUSES:
                self.add("O8-status-unknown", "warning",
                         f"status {status!r} is not one of "
                         f"{', '.join(sorted(STATUSES))}", where,
                         "§5.4: absent means `stable`; use one of the three")

        if "stale_after" in meta:
            when, reason = as_aware_datetime(meta["stale_after"])
            if when is None:
                self.add("O6-timestamp-format", "warning",
                         f"`stale_after` {reason}", where,
                         "§5: an ISO 8601 datetime with an explicit UTC "
                         "offset, e.g. 2027-01-02T00:00:00Z")
            elif self.now >= when:
                self.add("O9-stale", "warning",
                         f"stale since {when.date().isoformat()}", where,
                         "re-check the content against its sources, then move "
                         "`stale_after` forward -- moving it without "
                         "re-reading is how the field stops meaning anything")

    def check_sources(self, sources, body, where):
        if not isinstance(sources, list):
            self.add("O7-source-no-resource", "warning",
                     "`sources` is not a list", where,
                     "§5.1: a list of entries, each with a `resource`")
            return
        ids = set()
        for i, entry in enumerate(sources):
            tag = f"{where} sources[{i}]"
            if not isinstance(entry, dict):
                self.add("O7-source-no-resource", "warning",
                         "source entry is not a mapping", tag)
                continue
            res = entry.get("resource")
            if not isinstance(res, str) or not res.strip():
                self.add("O7-source-no-resource", "error",
                         "source entry has no `resource`", tag,
                         "§5.1: `resource` is REQUIRED within an entry -- a "
                         "URL, a bundle-relative path, or a scope descriptor")
            if isinstance(entry.get("id"), str):
                ids.add(entry["id"])
            if "last_modified" in entry:
                self.check_timestamp(entry["last_modified"], f"{tag}.last_modified")
            if "author" in entry:
                self.check_actor(entry["author"], f"{tag}.author")
            if "usage_count" in entry and not isinstance(entry["usage_count"], int):
                self.add("O6-timestamp-format", "warning",
                         "`usage_count` is not an integer", tag,
                         "§5.1: a count of exercises over `usage_window`")

        # §5.1 per-claim attribution: a footnote label is the join key into
        # `sources`. Reported at info, never higher -- a footnote is ordinary
        # markdown and a document is free to use one for something else. The
        # check only runs where the document declares source ids at all, so it
        # cannot fire on a document that never opted into attribution.
        if ids:
            for label in sorted(set(re.findall(r"^\[\^([^\]]+)\]:", body, re.M))):
                if label not in ids:
                    self.add("O10-footnote-unmatched", "info",
                             f"footnote [^{label}] matches no sources[].id",
                             where,
                             "§5.1: consumers resolve attribution through the "
                             "matching entry, not the footnote prose")

    def check_actor(self, value, where):
        if not isinstance(value, str):
            self.add("O5-actor-format", "warning",
                     f"actor is a {type(value).__name__}, not a string", where)
            return
        if ACTOR_PREFIXED.match(value) or ACTOR_VERSIONED.match(value):
            return
        self.add("O5-actor-format", "warning",
                 f"actor {value!r} does not follow the §7 convention", where,
                 "`human:<id>`, `process:<id>`, or `<producer>/<version>`. "
                 "§5.3 keys the highest trust tier off the `human:` prefix, "
                 "so a hand-written document without it reads as machine work")

    def check_timestamp(self, value, where):
        when, reason = as_aware_datetime(value)
        if when is None:
            self.add("O6-timestamp-format", "warning",
                     f"timestamp {reason}", where,
                     "§5: every timestamp-valued key is an ISO 8601 datetime "
                     "with an explicit UTC offset, e.g. 2026-10-04T12:00:00Z")

    # ------------------------------------------------------- §11 rule 3, §8/§9

    def check_reserved(self, path, rel, text):
        where = str(rel)
        if path.name == "index.md":
            self.check_index(rel, text, is_root=(path.parent == self.root))
        else:
            self.check_log(where, text)

    def check_index(self, rel, text, is_root):
        where = str(rel)
        raw, body, err = split_frontmatter(text)
        if err:
            self.add("O3-index-structure", "error", err, where)
            return
        if raw is not None:
            # §8: index files carry no frontmatter, with exactly one exception.
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                self.add("O3-index-structure", "error",
                         f"frontmatter is not parseable YAML: "
                         f"{str(exc).splitlines()[0]}", where)
                return
            if not isinstance(meta, dict):
                self.add("O3-index-structure", "error",
                         "frontmatter is not a mapping", where)
                return
            extra = sorted(k for k in meta if k != "okf_version")
            if not is_root:
                self.add("O3-index-structure", "error",
                         "an index.md outside the bundle root carries "
                         f"frontmatter ({', '.join(sorted(meta)) or 'empty'})",
                         where,
                         "§8: only a bundle-root index.md may have one, and "
                         "only for `okf_version`")
            elif extra:
                self.add("O3-index-structure", "error",
                         f"root index.md frontmatter carries {', '.join(extra)}",
                         where, "§8/§12: `okf_version` is the only key permitted")
            declared = meta.get("okf_version")
            if declared is not None and str(declared) != SPEC_VERSION:
                self.add("O3-index-structure", "warning",
                         f"bundle declares okf_version {declared!r}, this "
                         f"check implements {SPEC_VERSION}", where,
                         "§12: best-effort consumption, but the rules below "
                         "are the ones this tool knows")
        elif is_root:
            self.add("O3-index-structure", "info",
                     "root index.md declares no `okf_version`", where,
                     f'§12: add `okf_version: "{SPEC_VERSION}"` so a consumer '
                     "knows which revision the bundle targets")

        if not re.search(r"^#+ \S", body, re.M):
            self.add("O3-index-structure", "warning",
                     "no section heading", where,
                     "§8: the body uses one or more sections, each grouping "
                     "concepts under a heading")
        entries = re.findall(r"^\s*[*-] (.+)$", body, re.M)
        for entry in entries:
            if not re.match(r"\[[^\]]+\]\([^)]+\)", entry.strip()):
                self.add("O3-index-structure", "info",
                         f"entry is not a markdown link: {entry.strip()[:50]}",
                         where,
                         "§8: `* [Title](relative-url) - short description`")

    def check_log(self, where, text):
        raw, body, err = split_frontmatter(text)
        if err:
            self.add("O4-log-structure", "error", err, where)
            return
        if raw is not None:
            self.add("O4-log-structure", "error",
                     "log.md carries frontmatter", where,
                     "§9: a log is a flat list of date-grouped entries")
        headings = re.findall(r"^##+ (.+)$", body, re.M)
        for heading in headings:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", heading.strip()):
                self.add("O4-log-structure", "error",
                         f"date heading {heading.strip()!r} is not ISO 8601",
                         where, "§9: date headings MUST use `YYYY-MM-DD`")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(ROOT),
                    help="bundle root (default: the repository root)")
    ap.add_argument("--json", action="store_true", help="machine-readable findings")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--no-colour", action="store_true")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    check = Check(root, datetime.now(timezone.utc))
    findings = check.run()
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule, f.where))

    if args.json:
        print(json.dumps({
            "bundle": str(root),
            "okfVersion": SPEC_VERSION,
            "concepts": check.concepts,
            "reserved": check.reserved_seen,
            "findings": [asdict(f) for f in findings],
            "unchecked": [{"where": w, "reason": r} for w, r in check.unchecked],
        }, indent=2))
    else:
        use_colour = sys.stdout.isatty() and not args.no_colour
        print(f"\nOKF {SPEC_VERSION}  {root}  —  {check.concepts} concept "
              f"document(s), {check.reserved_seen} reserved file(s)\n")
        for f in findings:
            print(f.render(use_colour))
        for where, reason in check.unchecked:
            print(f"  unchecked  {where}: {reason}")
        if not findings and not check.unchecked:
            print("  no findings")
        counts = {s: sum(1 for f in findings if f.severity == s)
                  for s in SEVERITY_ORDER}
        # Name what was checked rather than asserting everything was.
        print(f"\n  {counts['error']} error(s), {counts['warning']} warning(s), "
              f"{counts['info']} info, {len(check.unchecked)} unchecked")
        print(f"  checked §11 conformance on {check.concepts} concept "
              f"document(s) and the structure of {check.reserved_seen} "
              f"reserved file(s)\n")

    failed = any(f.severity == "error" for f in findings) or (
        args.strict and any(f.severity == "warning" for f in findings))
    # An unchecked document is not a pass. It cannot fail the build on its own
    # -- that would make a missing dependency look like a broken bundle -- but
    # it is always named above.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
