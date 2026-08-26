"""Background macro replay.

Priority per step, matching the build spec exactly:
  1. UIA invoke/toggle/select -- no cursor movement, works on unfocused
     and partially covered windows. Default path.
  2. PostMessage to the window (or specific child control), for controls
     with no UIA pattern. Still no cursor movement.
  3. Physical cursor/keyboard control -- last resort only, and only when
     the caller explicitly passes allow_foreground=True. The web UI only
     does that after warning the user, never automatically.

Self-exclusion applies on replay exactly as it does on recording: a step
is refused, not executed, if it targets a Macro Studio window.

Key/hotkey steps carry no window of their own (position-based UIA
capture doesn't apply to keystrokes), so they target whichever window
the run's most recent click step resolved to -- never whatever the user
happens to have focused live, since replay runs in the background while
they keep working. A recording that starts with a keystroke before any
click fails that step clearly rather than guessing.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent import actions, cdp, config, run_reports, self_exclusion, sheets, toast, uia, video, winapi

_replay_lock = threading.Lock()

MODIFIER_KEYS = ("ctrl", "alt", "shift", "win")
POLL_INTERVAL = 0.2


class ReplayBusyError(RuntimeError):
    pass


def _compare(actual, operator: str, expected: str) -> bool:
    """Shared comparison logic for both conditional and until-loop
    steps. Unknown operators are treated as never matching rather than
    raising -- a typo in a hand-edited macro shouldn't crash a run."""
    actual_s = str(actual)
    if operator == "equals":
        return actual_s == expected
    if operator == "not_equals":
        return actual_s != expected
    if operator == "contains":
        return expected in actual_s
    if operator == "regex":
        try:
            return re.search(expected, actual_s) is not None
        except re.error:
            return False
    if operator in ("greater_than", "less_than"):
        try:
            a, b = float(actual_s), float(expected)
        except (TypeError, ValueError):
            return False
        return a > b if operator == "greater_than" else a < b
    return False


def _call_ok(fn) -> bool:
    """Runs an attempt callable and normalizes its outcome to a bool: a
    call that raises is a failure; one that returns None (most win32
    calls) or True is a success; one that explicitly returns False
    (a uia.try_* pattern call reporting "pattern not available") is not."""
    try:
        result = fn()
        return True if result is None else bool(result)
    except Exception:
        return False


def _is_self(hwnd: int) -> bool:
    return self_exclusion.is_own_window(winapi.window_pid(hwnd), winapi.window_title(hwnd))


def _find_window(window_info: dict) -> Optional[int]:
    """Re-find the window a recorded step targeted. HWNDs (and even
    titles) can change since recording, so this matches on the best
    available signal: exact title, falling back to class name. Never
    matches one of Macro Studio's own windows, even if generically
    similar -- e.g. Macro Studio running in Chrome shouldn't shadow some
    other Chrome window a step is trying to find."""
    if not window_info:
        return None
    title = (window_info.get("title") or "").strip()
    class_name = (window_info.get("class_name") or "").strip()
    if not title and not class_name:
        return None

    same_class = None
    for hwnd in winapi.enum_top_level_windows():
        if _is_self(hwnd):
            continue
        if title and winapi.window_title(hwnd) == title:
            return hwnd
        if class_name and same_class is None and winapi.window_class_name(hwnd) == class_name:
            same_class = hwnd
    return same_class


def _find_window_by_title_fragment(fragment: str) -> Optional[int]:
    """Case-insensitive substring match against visible top-level window
    titles, for the hand-typed "window title" field on Phase 7 steps --
    exact matching is impractical to type by hand. Never matches one of
    Macro Studio's own windows. If more than one window matches, the
    first found wins (ambiguous on purpose rather than guessing further)."""
    fragment = fragment.strip().lower()
    if not fragment:
        return None
    for hwnd in winapi.enum_top_level_windows():
        if _is_self(hwnd):
            continue
        if fragment in winapi.window_title(hwnd).lower():
            return hwnd
    return None


def _resolve_click_target(step: dict) -> dict:
    """Fresh hwnd/rect/pid for the window a click step targeted, plus a
    live UIA element if the recorded semantic target can be re-matched."""
    window_info = step.get("window") or {}
    hwnd = _find_window(window_info)
    resolved = {"hwnd": hwnd, "rect": None, "pid": None, "element": None}
    if not hwnd:
        return resolved
    resolved["rect"] = dict(zip(("left", "top", "right", "bottom"), winapi.window_rect(hwnd)))
    resolved["pid"] = winapi.window_pid(hwnd)

    target = (step.get("semantic") or {}).get("target")
    if target and (target.get("name") or target.get("automation_id")):
        root = uia.element_from_handle(hwnd)
        resolved["element"] = uia.find_descendant(root, target)
    return resolved


def _coords_for(step: dict, resolved: dict) -> tuple[int, int]:
    """Coordinate fallback re-derived from the window's CURRENT position
    plus the recorded window-relative offset -- not the stale absolute
    screen coordinates, which break if the window has moved."""
    rect = resolved.get("rect")
    if rect and step.get("relative_x") is not None:
        return rect["left"] + step["relative_x"], rect["top"] + step["relative_y"]
    return step.get("x", 0), step.get("y", 0)


def sub_label(step: dict) -> str:
    """What to call the thing that was clicked, when the page didn't say."""
    return step.get("text") or step.get("selector") or "it"


def _row_text(value) -> str:
    """A row as one line, for the run report and for {{row}} itself.

    A dict can't be substituted into a URL sensibly, and str(dict) in a
    report is unreadable -- but the cells with something in them, in
    order, reads exactly like the row does in the spreadsheet."""
    if isinstance(value, dict):
        if sheets.ROW_TEXT in value:
            return str(value[sheets.ROW_TEXT])
        return " | ".join(str(v) for v in value.values() if str(v))
    return str(value)


_WHOLE_VARIABLE = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_]+)\s*\}\}\s*$")


def _path_list(raw, variables: dict) -> list:
    """One field, one or several files.

    A File search step stores its matches as a list, and {{files}} written
    inside a sentence flattens that to "a.jpg, b.jpg" -- readable, useless
    as a path, and not safely splittable back since a filename may itself
    contain a comma. So when the field is nothing but the variable, the
    list is taken as it stands and never stringified. Anything else is one
    path per line."""
    if isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        text = str(raw or "")
        whole = _WHOLE_VARIABLE.match(text)
        value = variables.get(whole.group(1)) if whole else None
        if isinstance(value, list):
            parts = [str(item) for item in value]
        else:
            parts = actions.substitute(text, variables).splitlines()
    return [actions.expand_path(part.strip().strip('"')) for part in parts if part.strip()]


class ReplayEngine:
    def __init__(self, broadcast):
        self._broadcast = broadcast
        self._stop_event = threading.Event()
        self._video_recorder: Optional[video.VideoRecorder] = None

    def run(self, steps: list[dict], allow_foreground: bool = False,
            macro_id: Optional[str] = None, macro_name: str = "(unsaved recording)",
            video_config: Optional[dict] = None) -> dict:
        if not _replay_lock.acquire(blocking=False):
            raise ReplayBusyError("A replay is already in progress -- wait for it to finish or stop it first.")
        self._stop_event.clear()
        try:
            return self._run_locked(steps, allow_foreground, macro_id, macro_name, video_config)
        finally:
            _replay_lock.release()

    def request_stop(self) -> bool:
        """Called from the Stop button (or the panic hotkey). Returns
        False if nothing was running to stop."""
        if _replay_lock.locked():
            self._stop_event.set()
            return True
        return False

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleeps in small increments, checking for a stop request.
        Returns False if interrupted early."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.time())))
        return not self._stop_event.is_set()

    def _run_locked(self, steps: list[dict], allow_foreground: bool, macro_id: Optional[str], macro_name: str,
                     video_config: Optional[dict] = None) -> dict:
        run_id = run_reports.new_run_id()
        self._run_id = run_id  # read by _run_step_list for failure screenshots, incl. from nested blocks
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[dict] = []
        context = {"hwnd": None, "variables": {}}
        exec_counter = [0]  # shared mutable counter -- results are numbered by actual execution
        self._broadcast({"type": "run_state", "state": "running", "step_count": len(steps), "run_id": run_id})

        video_path, video_error = self._start_video(run_id, video_config)
        if video_error:
            self._broadcast({"type": "video_error", "message": video_error})

        completed = self._run_step_list(steps, allow_foreground, context, results, exec_counter)
        stopped = not completed

        if self._video_recorder is not None:
            video_path = self._video_recorder.stop()
            self._video_recorder = None

        passed = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        summary = {"total": len(results), "passed": passed, "failed": failed, "stopped": stopped}
        finished_at = datetime.now(timezone.utc).isoformat()

        video_url = f"/runs/{run_id}/{Path(video_path).name}" if video_path else None
        report = {
            "results": results, "summary": summary, "started_at": started_at, "finished_at": finished_at,
            "video": video_url, "video_error": video_error,
        }
        run_reports.save_report(run_id, macro_id, macro_name, report)

        outcome_word = "stopped" if stopped else ("failed" if failed else "succeeded")
        toast.notify(
            f"Macro Studio -- {macro_name}",
            f"Run {outcome_word}: {passed}/{len(results)} steps succeeded" + (f", {failed} failed" if failed else ""),
            is_error=failed > 0 and not stopped,
        )

        self._broadcast({"type": "run_state", "state": "finished", "summary": summary, "run_id": run_id, "video": video_url})
        return {**report, "run_id": run_id}

    def _start_video(self, run_id: str, video_config: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
        """Returns (video_path placeholder, error message). video_path is
        always None here -- the real path only exists once stop()
        finalizes the file; this just starts capture (or explains why it
        didn't) and stores the recorder on self for _run_locked to stop."""
        self._video_recorder = None
        if not video_config or not video_config.get("enabled"):
            return None, None
        output_path = config.RUNS_DIR / run_id / "recording.mp4"
        mode = video_config.get("mode", "fullscreen")
        window_title = video_config.get("window_title")
        if mode == "cdp_window":
            # Resolved per run, not stored: this window's title is whatever
            # page it is showing, which is different every time.
            window_title = cdp.browser_window_title(int(video_config.get("port") or cdp.DEFAULT_PORT))
            if window_title:
                mode = "window"
            else:
                mode = "fullscreen"
                self._broadcast({"type": "video_error", "message":
                                 "The CDP browser window wasn't open, so this run was recorded full screen."})
        recorder = video.VideoRecorder(
            output_path,
            mode=mode,
            fps=video_config.get("fps", video.DEFAULT_FPS),
            region=video_config.get("region"),
            window_title=window_title,
        )
        try:
            recorder.start()
        except RuntimeError as exc:
            return None, str(exc)
        self._video_recorder = recorder
        return None, None

    def _run_step_list(self, steps: list[dict], allow_foreground: bool, context: dict,
                        results: list[dict], exec_counter: list[int]) -> bool:
        """Runs a flat list of steps -- the top-level run, or a
        conditional/loop's nested block -- appending each leaf step's
        result to `results` in actual execution order (not list position,
        since loops repeat and conditionals skip) and broadcasting it
        live. Returns False if a stop was requested, in which case the
        caller should unwind without running anything further."""
        for i, step in enumerate(steps):
            if self._stop_event.is_set():
                return False

            step_type = step.get("type")
            if step_type == "conditional":
                if not self._run_conditional(step, allow_foreground, context, results, exec_counter):
                    return False
                continue
            if step_type == "loop":
                if not self._run_loop(step, allow_foreground, context, results, exec_counter):
                    return False
                continue

            expect_window_title = self._lookahead_window_title(steps, i)
            result = self._run_step(step, allow_foreground, context, expect_window_title)
            result["seq"] = exec_counter[0]
            exec_counter[0] += 1

            if result["status"] == "failed":
                screenshot = run_reports.capture_failure_screenshot(self._run_id, result["seq"])
                if screenshot:
                    result["screenshot"] = screenshot

            results.append(result)
            self._broadcast({"type": "run_step_result", "result": result})

            # A failed step normally doesn't end the run -- most macros do
            # several unrelated things and one miss shouldn't cancel the
            # rest. But some steps are the reason the next ones make sense:
            # if the wait for a page's content times out, saving a PDF of it
            # produces a file that looks fine and isn't. Marking such a step
            # "stop the run if this fails" says so.
            if result["status"] == "failed" and step.get("stop_on_fail"):
                self._broadcast({"type": "run_halted", "reason": result.get("reason", ""),
                                 "step_id": step.get("id")})
                return False

            if self._stop_event.is_set():
                return False
            delay = min(step.get("delay_ms", 0) / 1000.0, 5.0)  # cap so one long idle gap can't hang a run
            if delay > 0 and not self._interruptible_sleep(delay):
                return False
        return True

    def _emit_meta_result(self, results: list[dict], exec_counter: list[int], step: dict, reason: str) -> None:
        """A non-leaf progress marker (a conditional's branch choice, a
        loop's iteration count) -- shown in the report like any other
        step so the flow is legible, but always "success" since deciding
        a branch/iteration isn't itself something that can fail."""
        result = {
            "status": "success", "tier": None, "reason": reason,
            "step_id": step.get("id"), "seq": exec_counter[0], "duration_ms": 0,
        }
        exec_counter[0] += 1
        results.append(result)
        self._broadcast({"type": "run_step_result", "result": result})

    def _run_conditional(self, step: dict, allow_foreground: bool, context: dict,
                          results: list[dict], exec_counter: list[int]) -> bool:
        var_name = step.get("variable", "")
        operator = step.get("operator", "equals")
        compare_value = actions.substitute(step.get("value", ""), context["variables"])
        actual_value = context["variables"].get(var_name, "")
        condition_met = _compare(actual_value, operator, compare_value)

        branch_name = "then" if condition_met else "else"
        # The variable being tested is often a whole page read into one
        # string -- printing it in full turns the run report into a wall
        # of text, and the first line is the part that identifies it.
        shown = repr(actual_value)
        if len(shown) > 70:
            shown = shown[:70] + "..."
        self._emit_meta_result(
            results, exec_counter, step,
            f'If {var_name!r} {operator} {compare_value!r}: {shown} -> {condition_met} (running "{branch_name}").',
        )
        branch = step.get("then_steps" if condition_met else "else_steps", [])
        return self._run_step_list(branch, allow_foreground, context, results, exec_counter)

    def _run_loop(self, step: dict, allow_foreground: bool, context: dict,
                  results: list[dict], exec_counter: list[int]) -> bool:
        mode = step.get("mode", "count")
        body = step.get("body_steps", [])
        max_iterations = min(max(1, step.get("max_iterations", 100)), 1000)
        items: list = []
        if mode == "each":
            # The list a File search or a spreadsheet read left behind. A
            # count loop can't do this job: the number of rows isn't known
            # when the macro is written, and it's different tomorrow.
            source = context["variables"].get(step.get("list_variable", ""), [])
            items = list(source) if isinstance(source, list) else [source]
            iterations = min(len(items), max_iterations)
        elif mode == "count":
            iterations = min(max(0, step.get("count", 0)), max_iterations)
        else:
            iterations = max_iterations

        if mode == "each" and not iterations:
            self._emit_meta_result(results, exec_counter, step,
                                    f'Loop: {step.get("list_variable", "")!r} is empty -- nothing to repeat over.')
            return True

        item_as = (step.get("item_as") or "item").strip() or "item"
        for i in range(iterations):
            if self._stop_event.is_set():
                return False

            if mode == "each":
                item = items[i]
                # A row arrives as a dict of cells. The body needs them one
                # at a time -- {{row_A}} to type into a search box,
                # {{row_C}} to check against what the page says -- so each
                # cell becomes its own variable for this iteration, under
                # both its column letter and its heading.
                if isinstance(item, dict):
                    for key, cell in item.items():
                        if key != sheets.ROW_TEXT:
                            context["variables"][f"{item_as}_{key}"] = cell
                context["variables"][item_as] = _row_text(item)
                context["variables"][item_as + "_number"] = i + 1

            if mode == "until":
                var_name = step.get("variable", "")
                operator = step.get("operator", "equals")
                compare_value = actions.substitute(step.get("value", ""), context["variables"])
                actual_value = context["variables"].get(var_name, "")
                if _compare(actual_value, operator, compare_value):
                    self._emit_meta_result(results, exec_counter, step,
                                            f'Loop: {var_name!r} {operator} {compare_value!r} already true -- stopping after {i} iteration(s).')
                    break

            note = f"Loop iteration {i + 1}/{iterations}."
            if mode == "each":
                note += f' {{{{{item_as}}}}} = "{_row_text(items[i])}"'
            self._emit_meta_result(results, exec_counter, step, note)
            if not self._run_step_list(body, allow_foreground, context, results, exec_counter):
                return False
        return True

    @staticmethod
    def _lookahead_window_title(steps: list[dict], i: int) -> Optional[str]:
        """If the next click/double_click step targets a different window
        than this one, this step is expected to open/switch to it -- e.g.
        a "Save As..." menu click, or pressing Enter in a search box to
        launch an app. Used to verify the step actually did something
        instead of trusting a call that merely didn't error. For a
        key/hotkey step (which has no window of its own), "this window"
        is whatever the most recent preceding click targeted."""
        step = steps[i]
        step_type = step.get("type")
        if step_type not in ("click", "double_click", "key", "hotkey", "keyboard_shortcut", "find_click_text"):
            return None
        if i + 1 >= len(steps):
            return None
        nxt = steps[i + 1]
        if nxt.get("type") not in ("click", "double_click"):
            return None
        nxt_title = (nxt.get("window") or {}).get("title", "")
        if not nxt_title:
            return None

        if step_type in ("click", "double_click"):
            cur_title = (step.get("window") or {}).get("title", "")
        else:
            cur_title = None
            for j in range(i - 1, -1, -1):
                if steps[j].get("type") in ("click", "double_click"):
                    cur_title = (steps[j].get("window") or {}).get("title", "")
                    break
        return nxt_title if nxt_title != cur_title else None

    def _run_step(self, step: dict, allow_foreground: bool, context: dict, expect_window_title: Optional[str] = None) -> dict:
        start = time.time()
        step_type = step.get("type")
        try:
            if step_type in ("click", "double_click"):
                outcome, resolved = self._run_click(step, allow_foreground, expect_window_title)
                if resolved and resolved.get("hwnd"):
                    context["hwnd"] = resolved["hwnd"]
                    context["focus_element"] = resolved.get("element")  # may be None -- fine, gates itself
            elif step_type in ("key", "hotkey", "keyboard_shortcut"):
                outcome = self._run_key(step, allow_foreground, context, expect_window_title)
            elif step_type == "scroll":
                outcome = {"status": "skipped", "tier": None, "reason": "Scroll replay isn't implemented yet."}
            elif step_type == "wait":
                outcome = self._run_wait(step)
            elif step_type == "wait_for_element":
                outcome = self._run_wait_for_element(step, context)
            elif step_type == "wait_for_text":
                outcome = self._run_wait_for_text(step, context)
            elif step_type == "find_click_text":
                outcome = self._run_find_click_text(step, context, expect_window_title)
            elif step_type == "open_url":
                outcome = self._run_open_url(step, context)
            elif step_type == "open_file":
                outcome = self._run_open_file(step, context)
            elif step_type == "sheet_read":
                outcome = self._run_sheet_read(step, context)
            elif step_type == "file_search":
                outcome = self._run_file_search(step, context)
            elif step_type == "file_op":
                outcome = self._run_file_op(step, context)
            elif step_type == "file_wait":
                outcome = self._run_file_wait(step, context)
            elif step_type == "clipboard":
                outcome = self._run_clipboard(step, context)
            elif step_type == "get_cursor_position":
                outcome = self._run_get_cursor_position(step, context)
            elif step_type == "read_control_value":
                outcome = self._run_read_control_value(step, context)
            elif step_type in ("cdp_launch", "web_goto", "web_click", "web_hover",
                               "web_download",
                               "web_wait_for", "web_type", "web_upload", "web_drop_files",
                               "web_read", "web_print_pdf",
                               "web_switch_tab", "web_close_tab", "cdp_close",
                               "web_wait_loaded", "web_reload"):
                outcome = self._run_web_step(step_type, step, context)
            else:
                outcome = {"status": "skipped", "tier": None, "reason": f"Unknown step type '{step_type}'."}
        except Exception as exc:
            outcome = {"status": "failed", "tier": None, "reason": f"Unexpected error: {exc}"}
        outcome["step_id"] = step.get("id")
        outcome["seq"] = step.get("seq")
        outcome["duration_ms"] = round((time.time() - start) * 1000)
        return outcome

    def _run_wait(self, step: dict) -> dict:
        """An explicit Wait step (added in the step editor, not recorded)
        -- separate from delay_ms's idle-gap replay, which stays capped at
        5s regardless. This is deliberate user intent, so it gets a more
        generous cap."""
        requested_ms = max(0, step.get("duration_ms", 0))
        actual_ms = min(requested_ms, 30_000)
        if not self._interruptible_sleep(actual_ms / 1000.0):
            return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
        reason = None
        if actual_ms < requested_ms:
            reason = f"Capped at {actual_ms}ms (requested {requested_ms}ms)."
        return {"status": "success", "tier": None, "reason": reason}

    def _wait_for_window(self, title: str, timeout: float = 6.0) -> bool:
        """Substring match, not exact -- a live page's title can drift
        slightly from what was recorded (unread counts, trailing state),
        and this is a "did something plausible happen" check, not a
        precise re-identification (that's what _find_window is for)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if _find_window_by_title_fragment(title):
                return True
            time.sleep(0.1)
        return False

    def _resolve_window_for_step(self, step: dict, context: dict) -> Optional[int]:
        """Steps that reference a window explicitly (window_title) use
        that; otherwise they target whatever the run's most recent click
        resolved to, same rule as key/hotkey steps."""
        window_title = (step.get("window_title") or "").strip()
        if window_title:
            return _find_window_by_title_fragment(window_title)
        return context.get("hwnd")

    # -- Phase 7: extra action types --------------------------------------
    def _run_wait_for_element(self, step: dict, context: dict) -> dict:
        hwnd = self._resolve_window_for_step(step, context)
        if not hwnd:
            return {"status": "failed", "tier": None,
                    "reason": "No window to search in -- set window_title, or click into a window first in this run."}
        target = step.get("target", {})
        timeout = min(max(0, step.get("timeout_ms", 5000)) / 1000.0, 60.0)
        deadline = time.time() + timeout
        while True:
            if self._stop_event.is_set():
                return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
            root = uia.element_from_handle(hwnd)
            if uia.find_descendant(root, target) is not None:
                return {"status": "success", "tier": "uia", "reason": "Element appeared."}
            if time.time() >= deadline:
                return {"status": "failed", "tier": None,
                        "reason": f"Timed out after {round(timeout*1000)}ms waiting for element (name={target.get('name')!r})."}
            time.sleep(0.2)

    def _run_wait_for_text(self, step: dict, context: dict) -> dict:
        hwnd = self._resolve_window_for_step(step, context)
        if not hwnd:
            return {"status": "failed", "tier": None, "reason": "No window to search in -- set window_title, or click into a window first in this run."}
        target = step.get("target", {})
        expected = actions.substitute(step.get("expected", ""), context["variables"])
        is_regex = bool(step.get("is_regex"))
        timeout = min(max(0, step.get("timeout_ms", 5000)) / 1000.0, 60.0)
        deadline = time.time() + timeout
        pattern = re.compile(expected) if is_regex else None
        last_value = None
        while True:
            if self._stop_event.is_set():
                return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
            root = uia.element_from_handle(hwnd)
            element = uia.find_descendant(root, target)
            if element is not None:
                last_value = uia.get_current_value(element)
                if last_value is not None:
                    matched = pattern.search(last_value) if is_regex else last_value == expected
                    if matched:
                        return {"status": "success", "tier": "uia", "reason": f"Matched: {last_value!r}"}
            if time.time() >= deadline:
                return {"status": "failed", "tier": None,
                        "reason": f"Timed out after {round(timeout*1000)}ms; last value was {last_value!r}, expected {expected!r}."}
            time.sleep(0.2)

    def _run_find_click_text(self, step: dict, context: dict, expect_window_title: Optional[str] = None) -> dict:
        hwnd = self._resolve_window_for_step(step, context)
        if not hwnd:
            return {"status": "failed", "tier": None, "reason": "No window to search in -- set window_title, or click into a window first in this run."}
        if self_exclusion.is_own_window(winapi.window_pid(hwnd), winapi.window_title(hwnd)):
            return {"status": "failed", "tier": None, "reason": "Refusing to replay against Macro Studio's own window."}

        elevation_reason = self._elevation_block_reason(winapi.window_pid(hwnd))
        if elevation_reason:
            return {"status": "failed", "tier": None, "reason": elevation_reason}

        text = actions.substitute(step.get("text", ""), context["variables"])
        if not text:
            return {"status": "failed", "tier": None, "reason": "No text given to search for."}
        root = uia.element_from_handle(hwnd)
        candidates = uia.find_all_by_text(root, text, exact=bool(step.get("exact")))
        if not candidates:
            return {"status": "failed", "tier": None, "reason": f'No element with text "{text}" found in this window.'}

        # The outermost match is often a wrapper with no patterns on it
        # (a nav <li> around the real link), so try each match in turn
        # and click the first one that actually responds.
        element = None
        for candidate in candidates:
            if _call_ok(lambda c=candidate: uia.try_invoke(c) or uia.try_select(c) or uia.try_toggle(c)):
                element = candidate
                break
        if element is None:
            plural = "match" if len(candidates) == 1 else "matches"
            return {"status": "failed", "tier": None, "reason": f'Found {len(candidates)} {plural} for "{text}" but none had an invokable UIA pattern (no PostMessage fallback for text-based targeting yet).'}
        if expect_window_title and not self._wait_for_window(expect_window_title):
            return {"status": "failed", "tier": None, "reason": f'Clicked "{text}" without error, but "{expect_window_title}" never appeared.'}

        context["hwnd"] = hwnd
        context["focus_element"] = element
        return {"status": "success", "tier": "uia"}

    # -- CDP web steps -----------------------------------------------------
    # UIA can't click a custom web widget that exposes no invokable
    # pattern, and it can't see a background tab at all. These steps drive
    # Chrome's own debugging protocol instead, which has neither limit.
    # The port and the tab match live on each step so a macro can drive
    # more than one browser or tab without extra machinery.
    def _run_web_step(self, step_type: str, step: dict, context: dict) -> dict:
        sub = lambda key, default="": actions.substitute(step.get(key, default), context["variables"])
        port = int(step.get("port") or cdp.DEFAULT_PORT)
        try:
            if step_type == "cdp_launch":
                message = cdp.launch(
                    port=port,
                    user_data_dir=actions.expand_path(sub("user_data_dir")),
                    url=sub("url"),
                    wait_seconds=min(max(1, int(step.get("timeout_ms", 20000))) / 1000.0, 60.0),
                )
                context["cdp_port"] = port
                # A window the user minimised can't render, and a page that
                # can't render can't open a menu. Park it off-screen instead,
                # which is just as out of the way and still alive.
                parked = cdp.park_offscreen(port, only_minimized=True)
                if parked:
                    message += f" Un-minimised {parked} window(s) -- parked off-screen so they keep rendering."
                return {"status": "success", "tier": "cdp", "reason": message}

            if step_type == "cdp_close":
                message = cdp.close_browser(port)
                context["cdp_tab"] = ""
                context["cdp_seen_tabs"] = []
                return {"status": "success", "tier": "cdp", "reason": message}

            timeout_ms = min(max(0, int(step.get("timeout_ms", 10000))), 120_000)
            match = sub("tab_match")
            seen = context.setdefault("cdp_seen_tabs", [])

            def remember(page_entry: dict) -> dict:
                tab_id = page_entry.get("id", "")
                if tab_id and tab_id not in seen:
                    seen.append(tab_id)
                context["cdp_tab"] = tab_id or context.get("cdp_tab", "")
                context["cdp_port"] = port
                return page_entry

            if step_type == "web_switch_tab":
                if step.get("mode") == "match" and match:
                    page = cdp.find_page(port, match, timeout_ms=timeout_ms)
                else:
                    page = cdp.wait_for_new_page(port, seen, match, timeout_ms=timeout_ms or 15000)
                remember(page)
                return {"status": "success", "tier": "cdp",
                        "reason": f'Following that tab: {page.get("url", "")[:90]}'}

            if step_type == "web_close_tab":
                page = cdp.find_page(port, match or context.get("cdp_tab", ""), timeout_ms=timeout_ms)
                closed_url = page.get("url", "")
                cdp.close_page(port, page.get("id", ""))
                if page.get("id") in seen:
                    seen.remove(page.get("id"))
                # Hand the run back to the last tab it was using, so the
                # steps after this one aren't left pointing at nothing.
                context["cdp_tab"] = seen[-1] if seen else ""
                return {"status": "success", "tier": "cdp",
                        "reason": f'Closed that tab ({closed_url[:70]}).'}

            if step_type == "web_goto":
                url = sub("url")
                if not url:
                    return {"status": "failed", "tier": None, "reason": "No URL given."}
                # A brand-new tab is the sane default for "go to a page":
                # reusing whatever tab happened to be first would clobber
                # something the user may still be looking at.
                if step.get("new_tab", True) and not match:
                    page = cdp.open_tab(port, url)
                    reason = f"Opened {url} in a new tab."
                else:
                    page = cdp.find_page(port, match or context.get("cdp_tab", ""), timeout_ms=timeout_ms)
                    cdp.navigate(page, url)
                    reason = f"Navigated that tab to {url}."
                # Steps after this one act on the tab by id, and they must
                # not start until the new document is the live one.
                page = cdp.wait_ready(port, page.get("id", ""), url, timeout_ms=timeout_ms or 20000)
                remember(page)
                context["cdp_port"] = port
                return {"status": "success", "tier": "cdp", "reason": reason}

            page = remember(cdp.find_page(port, match or context.get("cdp_tab", ""), timeout_ms=timeout_ms))

            match_index = max(0, int(step.get("match_index") or 0))

            if step_type == "web_click":
                button = str(step.get("button") or "left")
                until_selector, until_text = sub("until_selector"), sub("until_text")
                press = lambda: cdp.through_navigation(port, page, timeout_ms or 8000, lambda p: cdp.click(
                    p, selector=sub("selector"), text=sub("text"),
                    exact=bool(step.get("exact")), timeout_ms=timeout_ms or 8000,
                    match_index=match_index, button=button,
                    hover_selector=sub("hover_selector"), hover_text=sub("hover_text"),
                    hover_exact=bool(step.get("hover_exact", True))))

                if until_selector or until_text:
                    outcome = self._click_until(step, context, page, press, until_selector,
                                                until_text, timeout_ms or 15000)
                    if outcome is not None:
                        return outcome
                result = press()
                label = result.get("label") or sub("text") or sub("selector")
                verb = "Right-clicked" if button.lower().startswith("r") else "Clicked"
                note = ""
                if result.get("covered"):
                    # Worth saying out loud: a page can tell this apart from
                    # a real press, and the ones that gate a download on a
                    # real gesture will have ignored it.
                    note = (" Something was over it, so the click was dispatched on the element"
                            " rather than pressed -- if nothing happened, close whatever is"
                            " covering it first.")
                return {"status": "success", "tier": "cdp",
                        "reason": f'{verb} <{result.get("tag")}> "{label}".{note}'}

            if step_type == "web_print_pdf":
                destination = actions.expand_path(sub("destination"))
                if not destination:
                    return {"status": "failed", "tier": None,
                            "reason": "No file path given to save the PDF as."}
                saved = cdp.through_navigation(port, page, timeout_ms or 10000, lambda p: cdp.print_to_pdf(
                    p, destination,
                    landscape=bool(step.get("landscape")),
                    paper=step.get("paper") or "A4",
                    scale=step.get("scale") or 1.0,
                    background=step.get("background", True),
                    margin_inches=step.get("margin_inches", 0.4),
                ))
                store_as = step.get("store_as")
                if store_as:
                    context["variables"][store_as] = saved
                layout = "landscape" if step.get("landscape") else "portrait"
                if str(saved).lower().endswith(".pdf") and cdp.is_pdf_document(page):
                    return {"status": "success", "tier": "cdp",
                            "reason": f'That tab was already a PDF -- saved it as-is to "{saved}".'}
                return {"status": "success", "tier": "cdp",
                        "reason": f'Saved {layout} PDF to "{saved}".'}

            if step_type == "web_reload":
                url = cdp.reload(page, ignore_cache=bool(step.get("ignore_cache")))
                page = remember(cdp.wait_ready(port, page.get("id", ""), "", timeout_ms=timeout_ms or 20000))
                how = "Hard-reloaded" if step.get("ignore_cache") else "Reloaded"
                return {"status": "success", "tier": "cdp", "reason": f"{how} {url[:80]}"}

            if step_type == "web_wait_loaded":
                info = cdp.through_navigation(port, page, timeout_ms or 30000, lambda p: cdp.wait_loaded(
                    p, quiet_ms=int(step.get("quiet_ms") or 800), timeout_ms=timeout_ms or 30000,
                    min_ms=int(step.get("min_ms") or 0)))
                still = "" if info.get("state") == "complete" else \
                    f' It is still fetching something (readyState {info.get("state")}), but nothing has changed.'
                return {"status": "success", "tier": "cdp",
                        "reason": f'Page settled after {info.get("waited")}ms '
                                  f'({info.get("resources")} resources).{still}'}

            if step_type == "web_hover":
                result = cdp.through_navigation(port, page, timeout_ms or 8000, lambda p: cdp.hover(
                    p, selector=sub("selector"), text=sub("text"),
                    exact=bool(step.get("exact")), timeout_ms=timeout_ms or 8000,
                    match_index=match_index))
                label = result.get("label") or sub("text") or sub("selector")
                return {"status": "success", "tier": "cdp",
                        "reason": f'Hovering <{result.get("tag")}> "{label}".'}

            if step_type == "web_wait_for":
                cdp.through_navigation(port, page, timeout_ms or 10000, lambda p: cdp.wait_for(
                    p, selector=sub("selector"), text=sub("text"),
                    exact=bool(step.get("exact")), timeout_ms=timeout_ms or 10000))
                return {"status": "success", "tier": "cdp", "reason": "Element appeared."}

            if step_type == "web_type":
                selector = sub("selector")
                if not selector:
                    return {"status": "failed", "tier": None, "reason": "No CSS selector given to type into."}
                cdp.through_navigation(port, page, timeout_ms or 10000, lambda p: cdp.type_text(
                    p, selector, sub("value"), submit=bool(step.get("submit"))))
                return {"status": "success", "tier": "cdp", "reason": f'Typed into "{selector}".'}

            if step_type == "web_download":
                folder = actions.expand_path(sub("folder"))
                if not folder:
                    return {"status": "failed", "tier": None,
                            "reason": "No folder given for the download to go to."}
                result = cdp.download_by_clicking(
                    port, page, folder, selector=sub("selector"), text=sub("text"),
                    exact=bool(step.get("exact")), match_index=match_index,
                    timeout_ms=timeout_ms or 60000,
                    hover_selector=sub("hover_selector"), hover_text=sub("hover_text"),
                    hover_exact=bool(step.get("hover_exact", True)))
                landed = Path(result["file"])
                save_as = actions.expand_path(sub("save_as"))
                if save_as:
                    # The site names the file; the macro renames it. An
                    # account number and today's date twice is not a name
                    # anyone can find an order by.
                    wanted = Path(save_as)
                    if not wanted.is_absolute():
                        wanted = landed.parent / wanted
                    if wanted.suffix == "":
                        wanted = wanted.with_suffix(landed.suffix)
                    wanted.parent.mkdir(parents=True, exist_ok=True)
                    if wanted != landed:
                        if wanted.exists():
                            wanted.unlink()
                        landed = Path(shutil.move(str(landed), str(wanted)))
                store_as = step.get("store_as")
                if store_as:
                    context["variables"][store_as] = str(landed)
                size = landed.stat().st_size
                return {"status": "success", "tier": "cdp",
                        "reason": f'Downloaded "{landed.name}" ({size:,} bytes) to "{landed.parent}".'}

            if step_type == "web_upload":
                paths = _path_list(step.get("files", ""), context["variables"])
                if not paths:
                    return {"status": "failed", "tier": None, "reason": "No file given to upload."}
                missing = [p for p in paths if not Path(p).is_file()]
                if missing:
                    return {"status": "failed", "tier": None,
                            "reason": f'There is no file at "{missing[0]}" -- nothing was attached.'}
                result = cdp.through_navigation(port, page, timeout_ms or 10000, lambda p: cdp.upload_files(
                    p, paths, selector=sub("selector"), timeout_ms=timeout_ms or 10000,
                    match_index=match_index))
                names = result.get("names") or [Path(p).name for p in paths]
                shown = ", ".join(f'"{n}"' for n in names[:3])
                more = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
                return {"status": "success", "tier": "cdp",
                        "reason": f"Attached {len(names)} file(s): {shown}{more}."}

            if step_type == "web_drop_files":
                paths = _path_list(step.get("files", ""), context["variables"])
                if not paths:
                    return {"status": "failed", "tier": None, "reason": "No file given to drop."}
                missing = [p for p in paths if not Path(p).is_file()]
                if missing:
                    return {"status": "failed", "tier": None,
                            "reason": f'There is no file at "{missing[0]}" -- nothing was dropped.'}
                result = cdp.through_navigation(port, page, timeout_ms or 10000, lambda p: cdp.drop_files(
                    p, paths, selector=sub("selector"), text=sub("text"),
                    exact=bool(step.get("exact")), timeout_ms=timeout_ms or 10000,
                    match_index=match_index))
                names = [Path(p).name for p in paths]
                shown = ", ".join(f'"{n}"' for n in names[:3])
                more = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
                where = result.get("label") or sub("selector") or sub("text")
                return {"status": "success", "tier": "cdp",
                        "reason": f'Dropped {len(names)} file(s) on "{where}": {shown}{more}.'}

            # web_read
            selector = sub("selector")
            if not selector:
                return {"status": "failed", "tier": None, "reason": "No CSS selector given to read."}
            value = cdp.through_navigation(port, page, timeout_ms or 10000,
                                           lambda p: cdp.read_text(p, selector))
            store_as = step.get("store_as")
            if store_as:
                context["variables"][store_as] = value
            preview = value if len(value) <= 60 else value[:60] + "..."
            return {"status": "success", "tier": "cdp",
                    "reason": f'Read "{preview}"' + (f" into {{{{{store_as}}}}}." if store_as else ".")}
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}

    def _click_until(self, step: dict, context: dict, page: dict, press, until_selector: str,
                     until_text: str, timeout_ms: int):
        """Presses until the thing the press was for shows up.

        The expectation is checked before each press as well as after: a
        click that worked but took its time would otherwise be repeated,
        and a second press on a button now behind a modal is at best
        wasted."""
        port = int(step.get("port") or cdp.DEFAULT_PORT)
        exact = bool(step.get("until_exact"))
        deadline = time.time() + min(max(1000, timeout_ms), 120_000) / 1000.0
        wanted = f'"{until_selector or until_text}"'
        presses, last = 0, None

        def appeared(window_ms: int) -> bool:
            try:
                cdp.through_navigation(port, page, window_ms, lambda p: cdp.wait_for(
                    p, selector=until_selector, text=until_text, exact=exact,
                    timeout_ms=window_ms))
                return True
            except RuntimeError:
                return False

        while True:
            if self._stop_event.is_set():
                return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
            if appeared(400 if not presses else 250):
                label = (last or {}).get("label") or sub_label(step)
                tries = "" if presses <= 1 else f" (took {presses} presses)"
                return {"status": "success", "tier": "cdp",
                        "reason": f'Clicked "{label}" and {wanted} appeared{tries}.'}
            if time.time() >= deadline:
                if last is None:
                    return None  # never got as far as pressing; let the caller report the miss
                covered = " Something was covering it, so the press was dispatched rather than"\
                          " real -- which some pages ignore." if last.get("covered") else ""
                return {"status": "failed", "tier": None,
                        "reason": f'Clicked "{last.get("label") or sub_label(step)}" {presses} time(s) '
                                  f'but {wanted} never appeared.{covered}'}
            try:
                last = press()
            except RuntimeError as exc:
                return {"status": "failed", "tier": None, "reason": str(exc)}
            presses += 1
            remaining = max(0.0, deadline - time.time())
            appeared(int(min(2500, remaining * 1000)))

    def _run_open_url(self, step: dict, context: dict) -> dict:
        url = actions.substitute(step.get("url", ""), context["variables"])
        if not url:
            return {"status": "failed", "tier": None, "reason": "No URL given."}
        try:
            new_window = bool(step.get("new_window"))
            actions.open_url_in_chrome(url, new_window=new_window)
            where = "in a new Chrome window" if new_window else "in Chrome"
            return {"status": "success", "tier": None, "reason": f"Opened {url} {where}."}
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}

    def _run_open_file(self, step: dict, context: dict) -> dict:
        """Opens the file, then waits for the window it opened in and
        points the run's keystrokes at it.

        Key and shortcut steps type into "the window the last click landed
        in", which is a problem for an app that wasn't running a moment
        ago -- there is nothing to click at yet, and clicking Excel's grid
        by coordinate would be exactly the fragile thing this replaces. So
        opening a file counts as establishing the window, the same way a
        click does. The title is matched, not the process: two workbooks
        open at once are two windows, and the one this step opened is the
        one named after the file."""
        paths = _path_list(step.get("path", ""), context["variables"])
        if not paths:
            return {"status": "failed", "tier": None, "reason": "No file given to open."}
        # A File search's variable is a list even when it found one match,
        # and opening five workbooks at once is nobody's intent -- the
        # newest-first + keep 1 pairing means the first is the one meant.
        path = paths[0]
        try:
            opened = actions.open_file(path)
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}

        # An app's title bar is the file's name without its extension --
        # "Sales - Excel", not "Sales.xlsx - Excel" -- so that, and not the
        # filename, is what a blank field looks for.
        title = actions.substitute(step.get("window_title", ""), context["variables"]).strip() or Path(opened).stem
        timeout = min(max(0, int(step.get("timeout_ms", 20000))) / 1000.0, 120.0)
        if not timeout:
            return {"status": "success", "tier": None, "reason": f'Opened "{opened}".'}

        deadline = time.time() + timeout
        while True:
            if self._stop_event.is_set():
                return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
            hwnd = _find_window_by_title_fragment(title)
            if hwnd:
                context["hwnd"] = hwnd
                # The element the last click resolved to belongs to another
                # window entirely, and typing into it now would put the
                # keystrokes somewhere nobody is looking.
                context["focus_element"] = None
                return {"status": "success", "tier": None,
                        "reason": f'Opened "{opened}" -- typing goes to "{winapi.window_title(hwnd)[:60]}" from here.'}
            if time.time() >= deadline:
                return {"status": "failed", "tier": None,
                        "reason": f'Opened "{opened}", but no window with "{title}" in its title appeared '
                                  f"within {round(timeout * 1000)}ms."}
            time.sleep(0.3)

    def _run_sheet_read(self, step: dict, context: dict) -> dict:
        paths = _path_list(step.get("path", ""), context["variables"])
        if not paths:
            return {"status": "failed", "tier": None, "reason": "No spreadsheet given to read."}
        column = actions.substitute(step.get("column", "A"), context["variables"])
        common = dict(
            sheet=actions.substitute(step.get("sheet", ""), context["variables"]),
            first_row=max(1, int(step.get("first_row") or 1)),
            limit=max(1, int(step.get("limit") or 500)),
            encoding=(step.get("encoding") or "").strip(),
        )
        # "A" is one column and gives a list of values; "A-G" is a row and
        # gives a list of rows. Same field, because the difference is
        # visible in what was typed and asking for a mode as well would be
        # asking twice.
        whole_rows = not column.strip() or any(mark in column for mark in (",", "-", ":"))
        try:
            values = (sheets.read_rows(paths[0], columns=column, **common) if whole_rows
                      else sheets.read_column(paths[0], column=column, **common))
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}
        store_as = step.get("store_as")
        if store_as:
            context["variables"][store_as] = values
        what = f"columns {column}" if whole_rows else f"column {column}"
        if not values:
            return {"status": "failed", "tier": None,
                    "reason": f'{what.capitalize()} of "{Path(paths[0]).name}" is empty from that row down.'}
        shown = [_row_text(v) for v in values[:3]]
        preview = ", ".join(f'"{v}"' for v in shown)
        more = f" (+{len(values) - 3} more)" if len(values) > 3 else ""
        into = f" into {{{{{store_as}}}}}" if store_as else ""
        unit = "row(s)" if whole_rows else "value(s)"
        return {"status": "success", "tier": None,
                "reason": f"Read {len(values)} {unit} from {what}{into}: {preview}{more}."}

    def _run_file_search(self, step: dict, context: dict) -> dict:
        folder = actions.expand_path(actions.substitute(step.get("folder", ""), context["variables"]))
        pattern = actions.substitute(step.get("pattern", "*"), context["variables"])
        try:
            matches = actions.search_files(
                folder, pattern,
                recursive=bool(step.get("recursive")),
                limit=max(0, int(step.get("limit") or 500)),
                newest_first=bool(step.get("newest_first")),
            )
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}
        store_as = step.get("store_as")
        if store_as:
            context["variables"][store_as] = matches
        return {"status": "success", "tier": None, "reason": f"Found {len(matches)} match(es)."}

    def _run_file_op(self, step: dict, context: dict) -> dict:
        source = actions.expand_path(actions.substitute(step.get("source", ""), context["variables"]))
        destination = step.get("destination")
        destination = actions.expand_path(actions.substitute(destination, context["variables"])) if destination else None
        try:
            message = actions.file_op(step.get("operation", ""), source, destination, overwrite=bool(step.get("overwrite")))
            return {"status": "success", "tier": None, "reason": message}
        except (RuntimeError, OSError) as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}

    def _run_file_wait(self, step: dict, context: dict) -> dict:
        """Waits for a file to arrive in a folder, and to stop growing.

        A download is not a step that finishes -- the click that starts it
        returns immediately and the file appears some seconds later, so
        anything that touches it next is racing. Chrome writes to
        `name.pdf.crdownload` until it's done, which a `*.pdf` pattern
        already skips, but a small file can appear complete while it is
        still being written: the size has to hold still before this
        answers."""
        folder = actions.expand_path(actions.substitute(step.get("folder", ""), context["variables"]))
        pattern = actions.substitute(step.get("pattern", "*"), context["variables"]) or "*"
        if not folder:
            return {"status": "failed", "tier": None, "reason": "No folder given to watch."}
        timeout = min(max(1, int(step.get("timeout_ms") or 30000)) / 1000.0, 300.0)
        deadline = time.time() + timeout
        sizes: dict = {}
        while True:
            if self._stop_event.is_set():
                return {"status": "stopped", "tier": None, "reason": "Stopped by user."}
            try:
                matches = actions.search_files(folder, pattern, newest_first=True, limit=1)
            except RuntimeError:
                matches = []  # the folder may not exist until the download creates it
            if matches:
                found = Path(matches[0])
                try:
                    size = found.stat().st_size
                except OSError:
                    size = -1
                if size > 0 and sizes.get(str(found)) == size:
                    store_as = step.get("store_as")
                    if store_as:
                        context["variables"][store_as] = str(found)
                    into = f" into {{{{{store_as}}}}}" if store_as else ""
                    return {"status": "success", "tier": None,
                            "reason": f'"{found.name}" arrived ({size:,} bytes){into}.'}
                sizes[str(found)] = size
            if time.time() >= deadline:
                return {"status": "failed", "tier": None,
                        "reason": f'Nothing matching "{pattern}" finished downloading into '
                                  f'"{folder}" within {round(timeout * 1000)}ms.'}
            time.sleep(0.4)

    def _run_clipboard(self, step: dict, context: dict) -> dict:
        mode = step.get("mode", "write")
        try:
            if mode == "write":
                value = actions.substitute(step.get("value", ""), context["variables"])
                actions.clipboard_write(value)
                return {"status": "success", "tier": None, "reason": f"Wrote {len(value)} character(s) to the clipboard."}
            value = actions.clipboard_read()
            store_as = step.get("store_as")
            if store_as:
                context["variables"][store_as] = value
            return {"status": "success", "tier": None, "reason": f"Read {len(value)} character(s) from the clipboard."}
        except RuntimeError as exc:
            return {"status": "failed", "tier": None, "reason": str(exc)}

    def _run_get_cursor_position(self, step: dict, context: dict) -> dict:
        x, y = winapi.get_cursor_pos()
        store_as = step.get("store_as")
        if store_as:
            context["variables"][store_as] = {"x": x, "y": y}
        return {"status": "success", "tier": None, "reason": f"Cursor at ({x}, {y})."}

    def _run_read_control_value(self, step: dict, context: dict) -> dict:
        hwnd = self._resolve_window_for_step(step, context)
        if not hwnd:
            return {"status": "failed", "tier": None, "reason": "No window to search in -- set window_title, or click into a window first in this run."}
        target = step.get("target", {})
        root = uia.element_from_handle(hwnd)
        element = uia.find_descendant(root, target)
        if element is None:
            return {"status": "failed", "tier": None, "reason": f"Element not found (name={target.get('name')!r})."}
        value = uia.get_current_value(element)
        if value is None:
            return {"status": "failed", "tier": None, "reason": "Element has no readable value (no ValuePattern)."}
        store_as = step.get("store_as")
        if store_as:
            context["variables"][store_as] = value
        return {"status": "success", "tier": "uia", "reason": f"Read: {value!r}"}

    # -- click/double-click ------------------------------------------------
    def _run_click(self, step: dict, allow_foreground: bool, expect_window_title: Optional[str] = None) -> tuple[dict, Optional[dict]]:
        resolved = _resolve_click_target(step)
        hwnd = resolved.get("hwnd")

        if hwnd is None:
            window_title = (step.get("window") or {}).get("title", "")
            return {"status": "failed", "tier": None,
                    "reason": f'Could not find the target window "{window_title}" -- it may be closed.'}, None

        if self_exclusion.is_own_window(resolved.get("pid"), winapi.window_title(hwnd)):
            return {"status": "failed", "tier": None,
                    "reason": "Refusing to replay against Macro Studio's own window."}, resolved

        elevation_reason = self._elevation_block_reason(resolved.get("pid"))
        if elevation_reason:
            return {"status": "failed", "tier": None, "reason": elevation_reason}, resolved

        x, y = _coords_for(step, resolved)
        cx, cy = winapi.screen_to_client(hwnd, x, y)
        target_hwnd = winapi.child_window_from_point(hwnd, cx, cy)
        element = resolved.get("element")
        button = step.get("button", "left")
        double = step_is_double(step)

        attempts: list[tuple[str, object]] = []
        if element is not None:
            attempts.append(("uia", lambda: uia.try_invoke(element) or uia.try_select(element) or uia.try_toggle(element)))
        attempts.append(("postmessage", lambda: winapi.post_click(target_hwnd, cx, cy, button, double=double)))
        if allow_foreground:
            attempts.append(("cursor", lambda: winapi.physical_move_and_click(x, y, button, double=double)))

        tier_notes = {
            "postmessage": "No UIA pattern was available; sent as a posted click. Double-check this control actually reacted.",
            "cursor": "Used physical cursor control (foreground).",
        }

        for tier, action in attempts:
            if not _call_ok(action):
                continue
            if expect_window_title and not self._wait_for_window(expect_window_title):
                continue  # call didn't error, but the expected window never showed up -- escalate
            return {"status": "success", "tier": tier, "reason": tier_notes.get(tier)}, resolved

        if expect_window_title:
            if not allow_foreground:
                return {"status": "failed", "tier": "cursor_required",
                        "reason": f'Ran without error but "{expect_window_title}" never appeared -- may need real mouse input. Re-run with foreground execution allowed.'}, resolved
            return {"status": "failed", "tier": None,
                    "reason": f'Tried UIA, posted click, and physical click, but "{expect_window_title}" never appeared -- this app may need more time, or the recording needs redoing here.'}, resolved

        if not allow_foreground:
            return {"status": "failed", "tier": "cursor_required",
                    "reason": "Needs to take over your mouse (no UIA pattern or posted click worked) -- re-run with foreground execution allowed."}, resolved
        return {"status": "failed", "tier": None, "reason": "Physical click was attempted but still failed."}, resolved

    # -- key/hotkey ----------------------------------------------------------
    def _run_key(self, step: dict, allow_foreground: bool, context: dict, expect_window_title: Optional[str] = None) -> dict:
        hwnd = context.get("hwnd")
        if not hwnd:
            return {"status": "failed", "tier": None,
                    "reason": "No prior click in this run established which window to type into."}

        if self_exclusion.is_own_window(winapi.window_pid(hwnd), winapi.window_title(hwnd)):
            return {"status": "failed", "tier": None, "reason": "Refusing to replay against Macro Studio's own window."}

        elevation_reason = self._elevation_block_reason(winapi.window_pid(hwnd))
        if elevation_reason:
            return {"status": "failed", "tier": None, "reason": elevation_reason}

        keys = step.get("keys", [])

        # Prefer UIA SetValue on the most recently clicked element over
        # synthetic keystrokes, per spec -- far more reliable than posted
        # or physical keys for standard edit controls (including most
        # Windows common-dialog text fields, e.g. a Save As filename box).
        # Skipped entirely when this key is expected to open a new window
        # (e.g. Enter in a search box) -- SetValue can't do that.
        focus_element = context.get("focus_element")
        if focus_element is not None and not expect_window_title:
            current = uia.get_current_value(focus_element)
            if current is not None:
                new_value = None
                if len(keys) == 1 and len(keys[0]) == 1:
                    new_value = current + keys[0]
                elif keys == ["backspace"] and current:
                    new_value = current[:-1]
                if new_value is not None and uia.try_set_value(focus_element, new_value):
                    return {"status": "success", "tier": "uia", "reason": "Set via UIA ValuePattern instead of synthetic keystrokes."}

        attempts: list[tuple[str, object]] = [("postmessage", lambda: self._post_keys(hwnd, keys))]
        if allow_foreground:
            attempts.append(("cursor", lambda: self._physical_keys(keys)))

        tier_notes = {
            "postmessage": "Sent as posted key events. Some apps ignore posted keys for shortcuts/accelerators.",
            "cursor": "Used physical keyboard input (foreground).",
        }

        for tier, action in attempts:
            if not _call_ok(action):
                continue
            if expect_window_title and not self._wait_for_window(expect_window_title):
                continue  # didn't error, but the expected window never showed up -- escalate
            return {"status": "success", "tier": tier, "reason": tier_notes.get(tier)}

        if expect_window_title:
            if not allow_foreground:
                return {"status": "failed", "tier": "cursor_required",
                        "reason": f'Ran without error but "{expect_window_title}" never appeared -- may need real keyboard input. Re-run with foreground execution allowed.'}
            return {"status": "failed", "tier": None,
                    "reason": f'Tried posted and physical keys, but "{expect_window_title}" never appeared -- this app may need more time, or the recording needs redoing here.'}

        if not allow_foreground:
            return {"status": "failed", "tier": "cursor_required",
                    "reason": "Needs to send real keystrokes (posted keys didn't work) -- re-run with foreground execution allowed."}
        return {"status": "failed", "tier": None, "reason": "Physical keys were attempted but still failed."}

    def _post_keys(self, hwnd: int, keys: list[str]) -> None:
        modifiers = [k for k in keys if k in MODIFIER_KEYS]
        main_keys = [k for k in keys if k not in MODIFIER_KEYS]
        for mod in modifiers:
            vk = winapi.vk_for(mod)
            if vk:
                winapi.post_key_down(hwnd, vk)
        for k in main_keys:
            if len(k) == 1 and not modifiers:
                winapi.post_char(hwnd, k)
            else:
                vk = winapi.vk_for(k)
                if vk:
                    winapi.post_key_down(hwnd, vk)
                    winapi.post_key_up(hwnd, vk)
        for mod in reversed(modifiers):
            vk = winapi.vk_for(mod)
            if vk:
                winapi.post_key_up(hwnd, vk)

    def _physical_keys(self, keys: list[str]) -> None:
        modifiers = [k for k in keys if k in MODIFIER_KEYS]
        main_keys = [k for k in keys if k not in MODIFIER_KEYS]
        for mod in modifiers:
            vk = winapi.vk_for(mod)
            if vk:
                winapi.physical_key(vk, key_up=False)
        for k in main_keys:
            vk = winapi.vk_for(k)
            if vk:
                winapi.physical_key(vk, key_up=False)
                winapi.physical_key(vk, key_up=True)
        for mod in reversed(modifiers):
            vk = winapi.vk_for(mod)
            if vk:
                winapi.physical_key(vk, key_up=True)

    def _elevation_block_reason(self, pid: Optional[int]) -> Optional[str]:
        if pid is None:
            return None
        target_elevated = winapi.is_process_elevated(pid)
        if target_elevated and not winapi.is_self_elevated():
            return ("Target app appears to be running elevated (as Administrator) but Macro Studio is not -- "
                    "restart Macro Studio as Administrator to control it.")
        return None


def step_is_double(step: dict) -> bool:
    return step.get("type") == "double_click"
