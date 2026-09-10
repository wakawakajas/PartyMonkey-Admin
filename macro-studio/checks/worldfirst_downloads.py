"""Checks that an order download run actually produced its PDFs.

A run that "passed" can still leave a folder short a file, holding a
98-byte error page, or holding a perfectly good PDF for somebody else's
order -- and none of that shows up in the step report -- the click
worked, the file landed, the step went green. This looks at the files
themselves: one folder per order number from the sheet, the expected
number of PDFs in it, every one of them a real, non-blank PDF, and every
one of them opened and read to confirm it names the order it is filed
under. That last one is the failure that looks identical to success.

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
TITLE = "Order PDFs -- did every order download, is any blank, and does each name its own order?"

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = Path.home() / "Desktop" / "For Macro"

# A one-page statement is ~98 KB. Anything under this is a session-expired
# page or a truncated transfer wearing a .pdf name.
MIN_BYTES = 5_000


def expected_per_order(default: int = 3) -> int:
    """How many PDFs a single order should end up with, read off the macros
    themselves so the two never drift apart when a download step is added.

    More than one macro fills these folders -- the WorldFirst statements and
    the 1688 order detail today, whatever comes next tomorrow -- so what
    counts is not which macro a step belongs to but where it writes: a
    destination naming {{row}} is a file that lands in one order's folder."""
    total = 0
    for path in (ROOT / "macros").glob("*.json"):
        try:
            macro = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def count(steps) -> int:
            found = 0
            for step in steps:
                # A print step names one "destination"; a download step
                # names a "folder" and a "save_as". Either way, {{row}}
                # somewhere in where it writes means one file per order.
                where = " ".join(str(step.get(key) or "")
                                 for key in ("destination", "folder", "save_as"))
                if step.get("type") in ("web_download", "web_print_pdf") and "{{row}}" in where:
                    found += 1
                for key in ("body_steps", "then_steps", "else_steps"):
                    if isinstance(step.get(key), list):
                        found += count(step[key])
            return found

        total += count(macro.get("steps") or [])
    return total or default


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


def can_read_text() -> bool:
    """Whether the order number can be confirmed at all on this machine."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def page_text(raw: bytes) -> str:
    """What the file actually says -- every page of it.

    The drawn strings are glyph indexes into a subset font, so they can't
    be read straight out of the content stream -- but each file carries
    its own ToUnicode map, and a PDF reader will apply it. Without pypdf
    installed there is no way to ask, so the answer is an honest blank
    rather than a guess, and the checks that need it stand down.

    All pages, not just the first: the 1688 order detail runs to two or
    three when the order has enough lines on it, and a check that only
    ever opened page one would be reading a different file than the one
    the eye is asking about."""
    try:
        import io
        import pypdf
    except ImportError:
        return ""
    try:
        pages = pypdf.PdfReader(io.BytesIO(raw)).pages
        return " ".join(" ".join(page.extract_text() or "" for page in pages).split())
    except Exception:
        return ""


# The order number as the file writes it: 19 digits here, but bounded on
# both sides so the 31-digit WorldFirst "Transaction ID" can't have a
# stretch of itself mistaken for an order.
ORDER_LIKE = re.compile(r"(?<!\d)\d{15,22}(?!\d)")
# What the two kinds of file call it, in the two languages they say it in.
ORDER_LABEL = re.compile(r"(?:Merchant transaction No|交易订单号|订单号)[:：]?\s*(\d{6,})")


def unspaced(text: str) -> str:
    """The text with every space taken out.

    The 1688 statement is drawn a character at a time, so its text comes
    back as "交 易 订 单 号 ： 3316...". The digits happen to survive
    together today; a font subset that splits them tomorrow would turn a
    good file into a failure, and dropping the spaces before looking costs
    nothing to be safe about it."""
    return re.sub(r"\s+", "", text)


def mentions_order(text: str, order: str) -> bool:
    """Is this order number actually printed on the file?"""
    return bool(order) and order in unspaced(text)


def order_named(text: str) -> str:
    """The order number the file itself says it is for.

    Everything else here checks that *a* file arrived. This checks it is
    the right one, which is the failure that looks identical to success:
    a full-size, non-blank, perfectly readable PDF for somebody else's
    order. Read the label where there is one, and where there isn't --
    a layout change, a page this doesn't know the wording of -- fall back
    to the first order-shaped number on the page, which is enough to say
    whose file this is instead."""
    found = ORDER_LABEL.search(unspaced(text)) or ORDER_LABEL.search(text)
    if found:
        return found.group(1)
    found = ORDER_LIKE.search(unspaced(text))
    return found.group(0) if found else ""


def statement_id(text: str) -> str:
    """What makes two statements the same statement.

    Byte comparison can't answer this: every download stamps its own
    "Download time", so the same statement fetched twice is two different
    files by a handful of bytes -- which is exactly what a step that
    silently re-downloaded the row it already had produces. Drop the
    stamp and the pair becomes identical again."""
    if not text:
        return ""
    return re.sub(r"Download time:.*$", "", text).strip()


def inspect(path: Path, order: str = "") -> tuple[bool, str, str]:
    """Says whether the file is a real statement, what it looks like, and
    which statement it is -- the third answer being how the caller spots
    the same one saved twice under two names."""
    if not path.is_file():
        return False, "missing", ""
    raw = path.read_bytes()
    if not raw:
        return False, "empty file (0 bytes)", ""
    if len(raw) < MIN_BYTES:
        return False, f"only {len(raw):,} bytes -- too small to be a statement", ""
    if not raw.startswith(b"%PDF-"):
        return False, f"not a PDF (starts {raw[:8]!r})", ""
    if b"%%EOF" not in raw[-2048:]:
        return False, "no %%EOF -- the download was cut short", ""
    pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    if pages < 1:
        counts = [int(n) for n in re.findall(rb"/Count\s+(\d+)", raw)]
        pages = max(counts) if counts else 0
    if pages < 1:
        return False, "no pages", ""
    if text_bytes(raw) < 5:
        return False, f"{pages} page(s) but no text drawn -- blank", ""
    text = page_text(raw)
    same = statement_id(text) or hashlib.md5(raw).hexdigest()
    size = f"{len(raw):,} bytes, {pages} page(s)"
    if not order:
        return True, size, same
    if not text:
        # Nothing was read, so nothing is claimed. Said out loud rather
        # than passed over, because "ok" here means less than it usually does.
        return True, f"{size} -- text unreadable, order not confirmed", same
    if not mentions_order(text, order):
        named = order_named(text)
        whose = f"order {named}'s" if named and named != order else "another order's"
        return False, f"{order} is nowhere in it -- this is {whose} PDF", same
    return True, f"{size}, says {order}", same


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
    if not can_read_text():
        print('Note    : pypdf is not installed, so the PDFs cannot be opened and read.')
        print('          Everything else still checks; the order number does not.')
        print('          Run start.bat once to install it, or: pip install pypdf')
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
            ok, note, same = inspect(pdf, order)
            if ok and same and same in seen:
                ok, note = False, f"the same statement as {seen[same]}"
            if same:
                seen.setdefault(same, pdf.name)
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
    print(f"All {len(orders) * want} PDF(s) present, readable, and each one names its own order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
