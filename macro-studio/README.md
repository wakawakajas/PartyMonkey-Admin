# Macro Studio

Record clicks/keystrokes once, replay them in the background, control it all from a
local web UI. Windows-only, single user, no cloud, no auth, nothing leaves this machine.

## Run it

Double-click [`start.bat`](start.bat). That's it.

First run: if Python isn't installed, `start.bat` installs it via `winget` (per-user,
no admin needed), then creates a virtual environment in `.venv/` and installs
dependencies from `requirements.txt`. Every later run just launches the agent and
opens your browser to it — takes a couple seconds.

The agent runs in its own console window titled **"Macro Studio Agent"**. Close that
window to stop it. It only binds to `127.0.0.1:8756` (change the port with the
`MACRO_STUDIO_PORT` environment variable if 8756 is taken).

Each run of `start.bat` also registers a `macrostudio://` link handler for the
current user (per-user registry, no admin needed) pointing at this exact install.
Any link to `macrostudio://launch` — e.g. a button in another app — will start
the agent if it isn't running and open the UI, one click, no browser tab juggling.
A PC that's never run `start.bat` has no handler yet; the browser's own "no app
found for this link" message is the signal to run it once first.

If `winget` isn't available, `start.bat` tells you so and points you to
https://www.python.org/downloads/ — install Python 3.10+, tick "Add python.exe to
PATH", then re-run `start.bat`.

## Status: Phase 10 of 10 — all phases complete

Each macro can now optionally record video of its own runs (Edit → Video Recording):
full screen, the CDP browser window, a specific window (by title), or a screen region,
at a configurable framerate (default 10fps). Capture and H.264 MP4 encoding are both
handled by ffmpeg's `gdigrab` input device — the agent just launches/stops the process.
"The CDP browser window" resolves that window itself at the start of every run: its
title is whatever page it happens to be showing, so there's nothing stable to type, and
asking Chrome over the debugging port which processes are its own is what tells it apart
from your normal Chrome. Macro Studio's own windows are excluded from full-screen/region
capture via `SetWindowDisplayAffinity` (Windows 10 2004+), the same API
screen-capture-blocking apps use. If ffmpeg isn't on PATH, the video settings panel and
the agent's startup banner say so plainly with a download link — the macro's actual
steps still run normally, only the video is skipped. Finished recordings are saved
alongside the run report and linked from the run result.

Videos older than `config.VIDEO_RETENTION_DAYS` (default 5) are auto-deleted by a
background check every 6 hours -- the rest of that run's report (results, screenshots)
stays intact, just without a video link.

Build order (see the project's task list for live status):

1. **Agent skeleton + web UI shell + start.bat** — done
2. **Global input hooks + self-exclusion** — done
3. **UIA element capture at click time** — done
4. **Background replay via UIA invoke, coordinate fallback** — done
5. **Macro save/load + full library UI** — done
6. **Step editor (edit/reorder/delete steps, add wait)** — done
7. **Extra action types (waits, text targeting, URL, file ops, clipboard, variables)** — done
8. **Notifications and run reports (toast, screenshots, Stop, panic hotkey)** — done
9. **Conditionals and loops** — done
10. **Video encoding** — done

## Web automation (CDP)

UI Automation is the right engine for desktop apps and the wrong one for
web pages. Chrome only exposes the *active* tab's accessibility tree, and a
site built from custom widgets hands back elements with no Invoke, Select,
Toggle, or DoDefaultAction pattern -- nothing UIA can click. The only way
out on that side is a physical mouse click, which defeats background replay.

So browser work goes through Chrome's own DevTools Protocol instead
(`agent/cdp.py`, six `Web:` step types). It clicks the DOM node directly, so
custom widgets, icon glyphs and wrapper elements all work, in background
tabs, with no window focus. It's local and free -- the protocol DevTools
itself uses, no API key, no per-run cost, nothing leaving the machine.

- **Launch Chrome (CDP)** starts (or reuses) a Chrome on `--remote-debugging-port`,
  and **Close Chrome (CDP)** quits it again -- the graceful `Browser.close`, so the
  profile isn't left looking crashed and the next launch doesn't offer to restore
  tabs. Both have a button in the Browser panel too. Neither can touch your normal
  Chrome: they work through the debugging port, which only this browser listens on
- **Web: go to / click / hover / wait for / type / read** act on a tab, matched
  by a URL or title fragment; blank means "the tab this run is already using"
- A click or hover takes a "match #" when several things share the same
  selector or label -- 0 is the first, 1 the second, which is how you tick one
  row's checkbox out of a list of identical ones
- A click on a menu item carries what opens that menu ("open menu" fields, `>>`
  between levels of a nested one) and re-hovers on every attempt. Hovering once in
  a separate step works until a re-render moves the trigger out from under the
  pointer -- which is why such a step passes one run and fails the next
- A click can be a right-click (`button: right`), recorded and replayed the same
  way. It reaches a site's own context menu, which is part of the page; Chrome's
  grey menu is drawn by the browser and nothing can drive an entry in it
- Clicks go through CDP's Input domain -- browser-level pointer events, not
  dispatched DOM events, so they carry `isTrusted` and settle hover state
  first. **Web: hover** exists for the same reason: a menu that opens on CSS
  `:hover` cannot be opened by a synthetic mouseover, because the browser,
  not the page, decides what is hovered. Still no OS cursor: nothing moves on
  screen and the tab needn't be focused or frontmost.
- Before pressing, the target point is checked against `elementFromPoint`; if
  something covers it or the menu is still animating, the click is dispatched
  on the node itself instead of at coordinates
- Steps mix freely with UIA steps -- one macro can drive File Explorer and a
  web page in sequence

**Recording a web macro.** The Browser panel's "Record clicks" watches that
Chrome from inside the page: a capture-phase listener is injected into the
tab, and every click becomes a `Web: click` step with both a generated CSS
selector and the element's visible label, since on replay those two narrow
each other. Clicking an item inside a dropdown also emits the `Web: hover`
step that opens it -- without which the item wouldn't exist to click. The
listener is re-injected on every poll, so a recording survives navigation,
which destroys the JS context along with everything in it. Stop, then Save
with the same buttons a recorded input macro uses.

The step editor has the same button, as "Record into this macro": it attaches to
whatever that browser is already showing and appends what you click to the end of
the list being edited, without the launch/go-to steps a fresh recording starts with.
Adding three steps to a working macro shouldn't mean recording it from the top
again. Drag them into place, then Save Changes.

The OS-level Record button is still the right tool for desktop apps, and the
wrong one here: against a browser it captures screen coordinates, which is
not what a web step needs.

To open that Chrome yourself -- to sign in, to look up a selector with F12, or
to watch a macro run -- double-click [`open-cdp-chrome.bat`](open-cdp-chrome.bat),
optionally with a URL. It reuses the debugging Chrome if one is already up, and
never touches your normal Chrome, which keeps its own separate profile.

Minimised is the one state a macro cannot work in, and it is not a matter of
throttling: Chrome marks a minimised window's page hidden, requestAnimationFrame
stops, and a menu that mounts through rAF -- ant's, and most component libraries'
-- never opens. Off-screen is different: the page stays visible in the sense the
renderer cares about while being just as far out of the way. So a run un-minimises
what it finds minimised and parks it at x=-32000, through Win32 rather than
`Browser.setWindowBounds`, because a plain restore takes the foreground and a
maximised window ignores bounds changes. **Park off-screen** and **Bring back** in
the Browser panel do it by hand.

For the same reason a macro's page opens as a background *window* rather than a
background tab: a background tab is hidden, with no rAF, so its menus never open
either.

That browser is launched with occlusion detection and background throttling
disabled, and macro tabs are created with `Target.createTarget`'s `background`
flag rather than the HTTP endpoint that raises the window. Both matter for the
same reason: Chrome stops rendering a window it believes nobody is looking at,
and a page that isn't rendering doesn't update hover state -- so a macro that
worked in front would fail the moment the window was minimised, while opening a
tab would yank the window in front of whatever you were doing. Flags only apply
at launch, so a Chrome started before this needs closing and reopening once.

Since Chrome 136 the debugging port is refused on the default user-data-dir,
so these steps run against a Chrome with its own profile folder (default
`%LocalAppData%\MacroStudio\ChromeProfile`). That profile starts signed out:
sign in to a site once in that window and the session persists from then on.

A page that redirects mid-step (a login gate, an SPA route change) destroys
the JS execution context underneath the call. Those errors are retried
against the re-resolved tab until the step's own timeout rather than failing
the run, since "the page moved" isn't the same as "this didn't work".

## When a step fails

A failed step doesn't end the run. Most macros do several unrelated things and one
miss shouldn't cancel the rest -- but some steps are the reason the ones after them
make sense. A wait that times out and then saves a PDF of a half-drawn page
produces a file that looks fine and isn't.

Tick **stop run if this fails** on such a step. The run halts there, the report
shows why, and nothing downstream gets the chance to write something wrong. Waits
are the obvious candidates; so is anything whose failure means the page isn't where
the next step thinks it is.

## When a click opens a tab

A PDF, a print preview, a report: sites open them in a new tab, and every step
after that click means the new tab, not the old one. **Web: follow new tab** waits
for a tab the run hasn't touched and points the rest of the macro at it. Which tab
is "new" isn't answerable by URL or title -- there may be three tabs on the same
site -- so it's answered by identity: the run remembers every tab it has used, and
anything else appeared because of what just happened. **Web: close tab** shuts it
again and hands the run back to the tab before it.

The recorder does this on its own. When a tab opens mid-recording it writes the
follow step and moves with you, because otherwise it would sit watching the old tab
and silently capture nothing you did in the new one.

## Printing

Chrome's Ctrl+P dialog -- destination, layout, the folder picker -- is browser UI,
not page content. No click reaches it, from here or anywhere else. **Web: save page
as PDF** skips it: `Page.printToPDF` is the same rendering path that dialog uses,
so destination, orientation, paper size, margins, scale and the folder all become
arguments, and the file lands exactly where the step says instead of wherever the
last download went.

The path takes the same tokens as everything else, including the bare form
`{{DD.MM}}`, so
`C:\Orders\Picklist {{date:DD-MM-YYYY}}.pdf` names itself.

Backgrounds are off by default, the same tick Chrome's own dialog leaves clear. An
app screen paints its page a colour; with backgrounds on, every inch of paper the
content doesn't cover comes out as a slab of it. Images and inline colour print
either way -- the setting only governs CSS backgrounds.

**Web: wait until loaded** goes before it for a page that assembles itself: it
waits for the document to finish, then for nothing to be fetched and nothing in
the DOM to change for a quiet stretch. Quiet has one blind spot it cannot reason
its way out of -- a page that has not started looks exactly like one that has
finished -- so there is a "wait at least" for content that arrives on a timer. The
check that actually knows the difference is **Web: wait for** on the content
itself, and it is the better habit.

If the tab is already a PDF rather than a printable page, the step downloads the
real file instead, fetched from inside the page so it carries the session that was
allowed to open it. Printing that tab would otherwise capture Chrome's PDF viewer,
which is not the document anyone wanted.

What comes out is whatever the page prints. An app screen that renders nothing
under print CSS produces a blank PDF -- correctly, and silently. Point the step at
the print-preview tab a site opens for that purpose, not at the list you clicked
Print from, and check the file the first time.

## Naming a file after today

Any text field takes `{{variable}}`, and `{{date}}`, `{{time}}` and `{{now}}` are
always there without a step having to produce them -- naming a file after today is
the commonest thing a macro needs that it can't click its way to. A format goes
after a colon, in tokens anyone can read: `{{date:DD-MM-YYYY}}`,
`{{now:YYYY-MM-DD HH-mm}}`. Raw strftime codes pass through untouched for anyone
who prefers them.

Renaming whatever a download just produced is two steps:

1. **File Explorer search** -- folder `C:\Users\you\Downloads`, pattern `*.xlsx`,
   *newest first* ticked, *keep* 1, store as `file`
2. **File operation** -- rename, source `{{file}}`, destination
   `C:\Users\you\Downloads\Summary {{date:DD-MM-YYYY}}.xlsx`

Newest-first sorts the whole match list before applying the limit, since otherwise
"newest" would mean "newest of whichever ones the filesystem happened to list
first". Office lock files (`~$name.xlsx`, written while a workbook is open) are
skipped -- they are newer than the download and never what anyone means.

## Architecture

Two pieces, per the design brief — a browser tab can't touch the OS directly:

- **Agent** (`agent/`) — Python/FastAPI service on `127.0.0.1`. Does all recording,
  replay, and OS interaction (UI Automation, global input hooks, file ops, ffmpeg).
- **Web UI** (`web/`) — plain HTML/CSS/JS, no build step, no CDN dependencies. Served
  directly by the agent's static file mount. Talks to the agent over HTTP for
  commands and WebSocket (`/ws`) for live recording feedback and run status.

## Design decisions locked in from the brief

- **Two targeting modes per step**: a semantic UIA target (name, control_type,
  automation_id, class_name, ancestor path) tried first on replay, falling back to
  window-relative coordinates with a logged warning and a fragile-step marker in the
  editor. Semantic capture done in Phase 3 (talks to UIAutomationCore directly via
  `comtypes`, not pywinauto, to keep the dependency footprint small); replay lands in
  Phase 4.
- **Background execution priority**: UIA invoke → PostMessage/SendMessage → physical
  cursor as an explicitly-warned last resort (the web UI only allows it via a separate,
  clearly-labeled confirmation click after a normal replay reports it's needed — never
  automatically). Same priority for keystrokes: posted key events before physical.
  Key/hotkey steps target whichever window the run's most recent click resolved to,
  never whatever the user has focused live, since replay runs in the background while
  they keep working. Implemented in Phase 4.
- **Self-exclusion**: the agent's PID and the Macro Studio browser tab's window are
  never recorded *or replayed against*; only one replay (and one recording) runs at a
  time as the practical circuit breaker until macro-calling-macro exists in a later
  phase. Implemented in Phases 2 and 4.
- **Elevation mismatch**: before a replay step touches a target process, its token
  elevation is checked against the agent's own — if the target is elevated and the
  agent isn't, the step fails with that reason instead of a cryptic access-denied error.
  Implemented in Phase 4.
- **Storage**: macros as individual JSON files under `macros/`, last 10 versions each
  kept under `macros/versions/`. Implemented in Phase 5.

## Data layout

```
macro-studio/
  agent/          Python backend (FastAPI)
  web/            Static web UI (HTML/CSS/JS, no build step)
  macros/         Saved macros as JSON (gitignored — this is your data)
  macros/versions/  Last 10 versions per macro (gitignored)
  runs/           Run logs, failure screenshots, videos (gitignored)
  requirements.txt
  start.bat
```

## Notes

- File operation delete only removes single files, never folders -- guards against a
  bad macro turning into an accidental mass-delete.
- Open URL in Chrome launches `chrome.exe` directly with the URL; Chrome's own
  single-instance behavior means this reuses an existing window as a new tab rather
  than needing us to detect one.
- Clipboard read/write goes through PowerShell (`Get-Clipboard`/`Set-Clipboard`)
  rather than raw Win32 clipboard APIs -- simpler and more robust, at the cost of a
  short per-call process-launch delay.
- Elevated target apps: if a target app runs as Administrator, the agent needs to run
  elevated too, or Windows will silently block the interaction. The agent detects and
  reports its own elevation state on the status panel; Phase 4 adds an explicit
  mismatch warning when a target app turns out to be elevated and we're not.
- Chrome: controlled via command line / CDP rather than clicking through its UI, since
  Chrome's own UI is poorly exposed to UI Automation (Phase 7).
