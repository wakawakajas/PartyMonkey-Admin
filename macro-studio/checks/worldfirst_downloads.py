"""Checks that a WorldFirst download run actually produced its PDFs.

A run that "passed" can still leave a folder short a file or holding a
98-byte error page, and neither shows up in the step report -- the click
worked, the file landed, the step went green. This looks at the files
themselves: one folder per order number from the sheet, the expected
number of PDFs in it, and every one of them a real, non-blank PDF.

Double-click Check.bat and pick it from the menu, or run it directly:

    python -m checks.worldfirst_downloads
    python -m checks.worldfirst_downloads --expect 3

Exits 0 when everything checks out, 1 when anything is missing or bad,
so it can be chained after a run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import sheets  # noqa: E402

# The one line the menu shows for this check. Every file in this folder
# has one; that is all it takes for a new check to appear in Check.bat.
TITLE = "WorldFirst PDFs -- did every order download, and is any of them blank?"

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = Path.home() / "Desktop" / "For Macro"
MACRO_NAME = "YC WorldFirst download"

# A one-page statement is ~98 KB. Anything under this is a session-expired
# page or a truncated transfer wearing a .pdf name.
MIN_BYTES = 5_000


def expected_per_order(default: int = 2) -> int:
    """How many PDFs a single order should end up with, read off the macro
    itself so the two never drift apart when a download step is added."""
    for path in (ROOT / "macros").glob("*.json"):
        try:
            macro = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if macro.get("name") != MACRO_NAME:
            continue

        def count(steps) -> int:
            total = 0
            for step in steps:
                if step.get("type") in ("web_download", "web_print_pdf"):
                    total += 1
                total += count(step.get("body_steps") or [])
            return total

        found = count(macro.get("steps") or [])
        if found:
            return found
    return default


def newest_sheet(folder: Path) -> Path | None:
    books = sorted(folder.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return books[0] if books else None


def text_bytes(raw: bytes) -> int:
    """Roughly, how much drawn text the file contains.

    The strings are glyph indexes into a subset font, so they can't be read
    back as the order number -- but a page with no Tj/TJ at all is blank,
    and that is the failure worth catching."""
    drawn = 0
    for match in re.finditer(b"stream", raw):
        start, end = match.end(), raw.find(b"endstream", match.end())
        if end < 0:
            continue
        for skip in (1, 2, 3):
            try:
                chunk = zlib.decompress(raw[start + skip:end])
            except zlib.error:
                continue
            drawn += chunk.count(b"Tj") + chunk.count(b"TJ")
            break
    return drawn


def inspect(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    raw = path.read_bytes()
    if not raw:
        return False, "empty file (0 bytes)"
    if len(raw) < MIN_BYTES:
        return False, f"only {len(raw):,} bytes -- too small to be a statement"
    if not raw.startswith(b"%PDF-"):
        return False, f"not a PDF (starts {raw[:8]!r})"
    if b"%%EOF" not in raw[-2048:]:
        return False, "no %%EOF -- the download was cut short"
    pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    if pages < 1:
        counts = [int(n) for n in re.findall(rb"/Count\s+(\d+)", raw)]
        pages = max(counts) if counts else 0
    if pages < 1:
        return False, "no pages"
    if text_bytes(raw) < 5:
        return False, f"{pages} page(s) but no text drawn -- blank"
    return True, f"{len(raw):,} bytes, {pages} page(s)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", default=str(DEFAULT_FOLDER),
                    help="folder holding the sheet and the per-order folders")
    ap.add_argument("--expect", type=int, default=None,
                    help="PDFs expected per order (default: read from the macro)")
    ap.add_argument("--sheet", default=None, help="workbook to read order numbers from")
    ap.add_argument("--column", default="A")
    ap.add_argument("--first-row", type=int, default=1)
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f'There is no folder at "{folder}".')
        return 1

    book = Path(args.sheet) if args.sheet else newest_sheet(folder)
    if not book or not book.is_file():
        print(f'No .xlsx to read order numbers from in "{folder}".')
        return 1

    orders = [o for o in sheets.read_column(str(book), args.column, "", args.first_row, 500) if o]
    if not orders:
        print(f'"{book.name}" column {args.column} has no order numbers in it.')
        return 1

    want = args.expect if args.expect else expected_per_order()
    print(f'Sheet   : {book.name}  ({len(orders)} order(s))')
    print(f'Expect  : {want} PDF(s) per order -> {want * len(orders)} file(s) total')
    print()

    seen: dict[str, str] = {}
    bad = 0

    for order in orders:
        here = folder / order
        pdfs = sorted(here.glob("*.pdf")) if here.is_dir() else []
        print(f"{order}")
        if not here.is_dir():
            print("   FAIL  no folder -- this order never downloaded")
            bad += 1
            print()
            continue

        for pdf in pdfs:
            ok, note = inspect(pdf)
            digest = hashlib.md5(pdf.read_bytes()).hexdigest()
            if ok and digest in seen:
                ok, note = False, f"byte-identical to {seen[digest]}"
            seen.setdefault(digest, pdf.name)
            print(f'   {"ok  " if ok else "FAIL"}  {pdf.name}  --  {note}')
            bad += 0 if ok else 1

        if len(pdfs) < want:
            print(f"   FAIL  {len(pdfs)} of {want} PDF(s) -- {want - len(pdfs)} never arrived")
            bad += want - len(pdfs)
        elif len(pdfs) > want:
            print(f"   note  {len(pdfs)} PDFs, more than the {want} expected")
        print()

    if bad:
        print(f"{bad} problem(s). Re-run those orders.")
        return 1
    print(f"All {len(orders) * want} PDF(s) present and readable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
