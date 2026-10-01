#!/usr/bin/env python3
"""Datasheet fetch, extract and search.

Datasheets are long and mostly irrelevant. Reading one as images costs
roughly 1-2k tokens per page, so ingesting a 120-page part wholesale is
both slow and wasteful. This tool makes the cheap path the default:

    ds.py fetch C25804 [--url URL]   download + extract text, cache it
    ds.py index C25804               section headings with page numbers
    ds.py find  C25804 'abs.*max'    regex search, reports page numbers
    ds.py page  C25804 12            dump page 12 as text
    ds.py pdf   C25804               print the cached PDF path

The intended loop is: `index` to see the shape of the document, `find` to
locate the section that matters, `page` to read it. Only when a table comes
out mangled or the answer is a pinout drawing is it worth opening the PDF
itself with a vision read on those specific pages — `pdf` prints the path
for exactly that. Text first, pixels only where they earn their cost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "kb" / "datasheets"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Headings worth surfacing in an index. Datasheets are not consistent, so this
# is deliberately broad; a false positive costs one line of output.
SECTION_RE = re.compile(
    r"^\s{0,12}((?:\d+(?:\.\d+)*\s+)?"
    r"(?:absolute\s+maximum|recommended\s+operating|electrical\s+character"
    r"|dc\s+character|ac\s+character|pin\s+(?:configuration|description|"
    r"assignment|definition|function)|pinout|package\s+(?:outline|information|"
    r"dimension)|ordering\s+information|application\s+(?:information|circuit)"
    r"|typical\s+application|functional\s+description|block\s+diagram"
    r"|thermal\s+(?:information|character)|timing|strapping|boot\s+mode"
    r"|power\s+(?:supply|management)|revision\s+history)"
    r"[^\n]{0,60})$",
    re.IGNORECASE | re.MULTILINE,
)


def cache_paths(part: str) -> tuple[Path, Path]:
    return CACHE / f"{part}.pdf", CACHE / f"{part}.txt"


def resolve_url(part: str) -> str | None:
    """Ask LCSC for the datasheet URL. Best effort — the endpoint is unofficial."""
    api = f"https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={part}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as exc:
        print(f"note: LCSC lookup failed ({exc})", file=sys.stderr)
        return None
    result = (data or {}).get("result") or {}
    for key in ("pdfUrl", "pdfLinkUrl", "datasheetUrl"):
        url = result.get(key) or ""
        # LCSC appends ?productCode=..., so test the path, not the whole URL.
        if url.split("?", 1)[0].lower().endswith(".pdf"):
            return url
    return None


def fetch(part: str, url: str | None, force: bool) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    pdf, txt = cache_paths(part)

    if pdf.exists() and not force:
        print(f"cached  {pdf}")
    else:
        src = url or resolve_url(part)
        if not src:
            print(f"error: no datasheet URL for {part}.\n"
                  f"       Pass it explicitly: ds.py fetch {part} --url <URL>\n"
                  f"       The jlcpcb MCP returns a datasheet URL for every part.",
                  file=sys.stderr)
            return 2
        print(f"GET     {src}")
        try:
            req = urllib.request.Request(src, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                pdf.write_bytes(resp.read())
        except Exception as exc:
            print(f"error: download failed: {exc}", file=sys.stderr)
            return 2
        print(f"saved   {pdf} ({pdf.stat().st_size // 1024} KiB)")

    # -layout keeps column structure, which is what makes datasheet tables
    # survive extraction well enough to read.
    try:
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)],
                       check=True, capture_output=True)
    except FileNotFoundError:
        print("error: pdftotext missing — run inside nix-shell", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: pdftotext failed: {exc.stderr.decode()[:200]}", file=sys.stderr)
        return 2

    pages = txt.read_text(errors="replace").count("\f") + 1
    chars = len(txt.read_text(errors="replace").strip())
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    print(f"text    {txt} ({pages} pages, {chars} chars)")
    print(f"sha256  {digest}")

    if chars < pages * 40:
        print("\nwarning: almost no extractable text — this PDF is probably scanned\n"
              "         images. Fall back to a vision read of the PDF itself:\n"
              f"         {pdf}", file=sys.stderr)
    return 0


def load_text(part: str) -> list[str] | None:
    _, txt = cache_paths(part)
    if not txt.exists():
        print(f"error: no cached text for {part} — run: ds.py fetch {part}",
              file=sys.stderr)
        return None
    return txt.read_text(errors="replace").split("\f")


def index(part: str) -> int:
    pages = load_text(part)
    if pages is None:
        return 2
    print(f"\n{part} — {len(pages)} pages\n")
    found = 0
    for n, body in enumerate(pages, 1):
        for match in SECTION_RE.finditer(body):
            print(f"  p.{n:<4} {match.group(1).strip()}")
            found += 1
    if not found:
        print("  no recognised section headings; try: ds.py find "
              f"{part} '<pattern>'")
    print()
    return 0


def find(part: str, pattern: str, context: int) -> int:
    pages = load_text(part)
    if pages is None:
        return 2
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"error: bad regex: {exc}", file=sys.stderr)
        return 2
    hits = 0
    for n, body in enumerate(pages, 1):
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if rx.search(line):
                hits += 1
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                print(f"\n── p.{n} line {i + 1} " + "─" * 40)
                for j in range(lo, hi):
                    mark = ">" if j == i else " "
                    print(f"  {mark} {lines[j].rstrip()}")
    print(f"\n{hits} match(es)\n" if hits else f"\nno match for '{pattern}'\n")
    return 0


def page(part: str, number: int) -> int:
    pages = load_text(part)
    if pages is None:
        return 2
    if not 1 <= number <= len(pages):
        print(f"error: page {number} out of range (1-{len(pages)})", file=sys.stderr)
        return 2
    print(f"── {part} p.{number}/{len(pages)} " + "─" * 40)
    print(pages[number - 1].rstrip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download and extract a datasheet")
    f.add_argument("part")
    f.add_argument("--url", help="datasheet URL (skips the LCSC lookup)")
    f.add_argument("--force", action="store_true", help="re-download if cached")

    i = sub.add_parser("index", help="list section headings with page numbers")
    i.add_argument("part")

    s = sub.add_parser("find", help="regex search the extracted text")
    s.add_argument("part")
    s.add_argument("pattern")
    s.add_argument("-C", "--context", type=int, default=3)

    p = sub.add_parser("page", help="dump one page as text")
    p.add_argument("part")
    p.add_argument("number", type=int)

    d = sub.add_parser("pdf", help="print the cached PDF path")
    d.add_argument("part")

    a = ap.parse_args()
    if a.cmd == "fetch":
        return fetch(a.part, a.url, a.force)
    if a.cmd == "index":
        return index(a.part)
    if a.cmd == "find":
        return find(a.part, a.pattern, a.context)
    if a.cmd == "page":
        return page(a.part, a.number)
    if a.cmd == "pdf":
        pdf, _ = cache_paths(a.part)
        if not pdf.exists():
            print(f"error: not cached — run: ds.py fetch {a.part}", file=sys.stderr)
            return 2
        print(pdf)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
