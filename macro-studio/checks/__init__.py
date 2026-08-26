"""After-the-run checks -- "did that actually work?", one file per answer.

A macro run reports on its steps: the click landed, the file arrived, the
step went green. What it cannot tell you is whether the result is any
good -- an order silently short a PDF, a 98-byte session-expired page
saved under a .pdf name. That is what lives here.

To add one, drop a .py file in this folder with two things in it:

    TITLE = "One line the menu shows"

    def main() -> int:
        ...
        return 0    # 0 = all good, anything else = problems found

It appears in Check.bat's menu the next time anyone opens it. No list to
update, no registration -- the menu reads this folder. Name the file
after what it checks (worldfirst_downloads, picklist_pdfs), not after the
macro that produced it, because a check usually outlives the macro.
"""
