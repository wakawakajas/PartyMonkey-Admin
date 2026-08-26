"""Checks that a pick list run left all of its PDFs on the desktop.

The two prints happen in tabs BigSeller opens for them, and a tab that
loaded slowly, or came up asking for a login, still prints -- to a PDF of
whatever was on it. So "the step went green" and "there is a pick list to
work from" are different claims. This makes the second one.

The file names come off the macro itself rather than being typed in here,
so renaming a print step doesn't quietly leave this checking for a file
nothing writes any more.

Double-click Check.bat and pick it from the menu, or run it directly:

    python -m checks.picklist_pdfs
    python -m checks.picklist_pdfs --date 22.08     (check an earlier day)

Exits 0 when every PDF is there and readable, 1 when any is not.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions  # noqa: E402
from checks.worldfirst_downloads import inspect as inspect_pdf  # noqa: E402

TITLE = "Pick lists and bundles -- are today's PDFs there, and not blank?"

ROOT = Path(__file__).resolve().parent.parent
# Followed by id, not by name. It was "Morning PickList", it is now
# "PickList and Bundle", and a check that silently stops checking because
# somebody renamed something is worse than no check at all. The name is
# only used to say which macro this is talking about.
MACRO_ID = "739aa3a4797f46b2a33418a55a57dacf"
macro_name = "the pick list macro"


CLOCK_TOKEN = re.compile(r"\{\{(?:time|now)(?::[^}]*)?\}\}")
# Stands in for the clock while the rest of the name is resolved. A NUL can't
# occur in a filename, so nothing else can be mistaken for it.
MARK = "\x00"


def print_targets(day: str | None) -> list[tuple[Path, re.Pattern, str]]:
    """What each print step aims at: (folder, name to match, name to show).

    The date resolves the way a real run resolves it. The *time* must not:
    the names carry the hour the run happened, and a check run at ten past
    ten looking for a nine o'clock file would call a perfectly good morning
    a failure. So the clock becomes a pattern instead, and whatever matched
    gets printed -- which says what time it ran, for free.

    The pattern is one unspaced word rather than "anything", because the
    two sheets differ by a "PT" in the middle: a greedy wildcard swallows
    that and reports the same file twice, once under each name."""
    variables = {"date": day} if day else {}
    out: list[tuple[Path, re.Pattern, str]] = []

    def walk(steps) -> None:
        for step in steps:
            if step.get("type") == "web_print_pdf" and step.get("destination"):
                marked = CLOCK_TOKEN.sub(MARK, step["destination"])
                path = Path(actions.expand_path(actions.substitute(marked, variables)))
                parts = path.name.split(MARK)
                # Windows filenames are case-insensitive, and the macro's
                # spelling of "Bundle" won't always match the site's.
                rx = re.compile("[^ ]+".join(re.escape(p) for p in parts), re.IGNORECASE)
                out.append((path.parent, rx, path.name.replace(MARK, "<time>")))
            for key in ("body_steps", "then_steps", "else_steps"):
                if isinstance(step.get(key), list):
                    walk(step[key])

    for path in (ROOT / "macros").glob("*.json"):
        try:
            macro = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if macro.get("id") == MACRO_ID or path.stem == MACRO_ID:
            global macro_name
            macro_name = macro.get("name") or macro_name
            walk(macro.get("steps") or [])
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, metavar="DD.MM",
                    help="check the run from a particular day instead of today")
    args = ap.parse_args()

    wanted = print_targets(args.date)
    if not wanted:
        print(f'No macro with id {MACRO_ID[:8]} and print steps in it -- nothing to check.')
        return 1

    day = args.date or "today"
    print(f"Macro   : {macro_name}")
    print(f"Looking : {len(wanted)} PDF(s) from {day}'s run")
    print()

    bad = 0
    for folder, rx, shown in wanted:
        here = sorted(folder.iterdir()) if folder.is_dir() else []
        matches = [f for f in here if f.is_file() and rx.fullmatch(f.name)]
        if not matches:
            print(f"   FAIL  {shown}  --  nothing like this is there -- it never printed")
            bad += 1
            continue
        for path in matches:
            ok, note = inspect_pdf(path)
            print(f'   {"ok  " if ok else "FAIL"}  {path.name}  --  {note}')
            bad += 0 if ok else 1

    print()
    if bad:
        print(f'{bad} problem(s) in "{wanted[0][0]}". Re-run the macro.')
        return 1
    print(f"All {len(wanted)} PDF(s) present and readable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
