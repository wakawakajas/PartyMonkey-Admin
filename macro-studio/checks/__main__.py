"""The menu behind Check.bat: lists every check in this folder and runs one.

    python -m checks                      -> pick from a numbered menu
    python -m checks worldfirst_downloads -> run that one straight away
    python -m checks all                  -> run every check, one after another
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE.parent))


def discover() -> list[tuple[str, str]]:
    """Every check module, as (name, title). Ordered by name so the menu
    numbers don't shuffle around under a colleague between runs."""
    found = []
    for info in sorted(pkgutil.iter_modules([str(PACKAGE)]), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"checks.{info.name}")
        except Exception as exc:  # a broken check shouldn't hide the others
            found.append((info.name, f"(could not load: {exc})"))
            continue
        if hasattr(module, "main"):
            found.append((info.name, getattr(module, "TITLE", info.name)))
    return found


def run(name: str, argv: list[str]) -> int:
    module = importlib.import_module(f"checks.{name}")
    sys.argv = [f"checks/{name}.py", *argv]
    return int(module.main() or 0)


def main() -> int:
    checks = discover()
    if not checks:
        print("There are no checks in the checks folder yet.")
        return 1

    args = sys.argv[1:]
    if args and args[0] == "all":
        worst = 0
        for name, title in checks:
            print(f"\n=== {title} ===")
            worst = max(worst, run(name, args[1:]))
        return worst
    if args:
        names = [n for n, _ in checks]
        if args[0] not in names:
            print(f'There is no check called "{args[0]}". There is: {", ".join(names)}')
            return 1
        return run(args[0], args[1:])

    print("What do you want to check?\n")
    for i, (_, title) in enumerate(checks, 1):
        print(f"  {i}. {title}")
    print(f"\n  {len(checks) + 1}. All of them")
    print("  0. Nothing, close this\n")

    try:
        picked = input("Type a number and press Enter: ").strip()
    except EOFError:
        return 0
    if not picked.isdigit():
        print("That wasn't a number.")
        return 1
    picked = int(picked)
    if picked == 0:
        return 0
    if picked == len(checks) + 1:
        worst = 0
        for name, title in checks:
            print(f"\n=== {title} ===")
            worst = max(worst, run(name, []))
        return worst
    if not 1 <= picked <= len(checks):
        print("That number isn't on the list.")
        return 1
    print()
    return run(checks[picked - 1][0], [])


if __name__ == "__main__":
    raise SystemExit(main())
