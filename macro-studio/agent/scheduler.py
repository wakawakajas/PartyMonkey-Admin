"""Scheduling and the run queue.

Two halves that only meet at one point:

  * The **queue** is a single-file line of macros waiting to run. One
    worker thread drains it, one macro at a time, because replay drives
    the real mouse and keyboard -- two at once would fight over them.
    Anything that wants a macro run without a person watching puts it
    in the queue rather than calling replay directly.

  * The **scheduler** watches the clock and puts macros in that queue
    when their time comes. A schedule is a window ("weekdays, 09:00 to
    17:00, every 30 minutes"), not a single alarm, because the thing
    people actually ask for is "keep this going through the morning".
    A window with no end is just the one firing at its start time.

Schedules are persisted to schedules.json so they survive a restart.
The queue is deliberately not: a queue rebuilt from yesterday's line
would replay work whose moment has passed. Anything still waiting when
the agent stops is dropped, and the schedule that put it there will put
it back at its next slot.

Missed slots are NOT caught up indefinitely -- see CATCH_UP_SECONDS. An
agent started at 4pm should not fire off every 30-minute slot since 9am
in one burst.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from agent import config

SCHEDULES_PATH = config.ROOT_DIR / "schedules.json"

# How often the clock is checked. Slots have minute resolution, so this
# only needs to be comfortably under a minute.
TICK_SECONDS = 10

# A slot whose moment passed longer ago than this is skipped rather than
# fired late: the agent was off, or the machine asleep, and running a
# 09:00 job at 16:00 is worse than not running it.
CATCH_UP_SECONDS = 300

# Finished items stay on the list this long so there is something to
# read after the fact, then fall off so the panel doesn't grow forever.
HISTORY_LIMIT = 40

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleNotFoundError(KeyError):
    pass


class QueueItemNotFoundError(KeyError):
    pass


def _now_local() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# -- validation ------------------------------------------------------------
# Everything below rejects with a plain-language ValueError. These come
# straight back to the form the user is typing in, so "Start time must
# look like 09:00" beats a stack trace about strptime.

def _parse_hhmm(value: str, label: str) -> tuple[int, int]:
    text = str(value or "").strip()
    try:
        hh, mm = text.split(":")
        hours, minutes = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ValueError(f"{label} must look like 09:00.") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"{label} must be between 00:00 and 23:59.")
    return hours, minutes


def _normalize_days(days) -> list[int]:
    if days is None:
        return []
    try:
        cleaned = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        raise ValueError("Days must be numbers, Monday=0 through Sunday=6.") from None
    for d in cleaned:
        if not 0 <= d <= 6:
            raise ValueError("Days must be between 0 (Monday) and 6 (Sunday).")
    return cleaned


def _validate(payload: dict, known_macro_ids: set[str]) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Give the schedule a name.")

    macro_ids = [str(m) for m in (payload.get("macro_ids") or []) if str(m).strip()]
    if not macro_ids:
        raise ValueError("Pick at least one macro to run.")
    missing = [m for m in macro_ids if m not in known_macro_ids]
    if missing:
        raise ValueError(f"{len(missing)} of the chosen macros no longer exist -- pick them again.")

    days = _normalize_days(payload.get("days"))
    start_h, start_m = _parse_hhmm(payload.get("start_time"), "Start time")

    end_time = payload.get("end_time")
    end_h = end_m = None
    if end_time not in (None, ""):
        end_h, end_m = _parse_hhmm(end_time, "End time")
        if (end_h, end_m) < (start_h, start_m):
            raise ValueError("End time is before the start time. A window that crosses midnight "
                             "needs two schedules, one either side of it.")

    repeat = payload.get("repeat_minutes")
    repeat_minutes = None
    if repeat not in (None, "", 0, "0"):
        try:
            repeat_minutes = int(repeat)
        except (TypeError, ValueError):
            raise ValueError("Repeat every ... must be a whole number of minutes.") from None
        if repeat_minutes < 1:
            raise ValueError("Repeat every ... must be at least 1 minute.")
        if end_h is None:
            raise ValueError("Repeating needs an end time -- otherwise it would never stop.")

    return {
        "name": name,
        "macro_ids": macro_ids,
        "days": days,
        "start_time": f"{start_h:02d}:{start_m:02d}",
        "end_time": None if end_h is None else f"{end_h:02d}:{end_m:02d}",
        "repeat_minutes": repeat_minutes,
        "allow_foreground": bool(payload.get("allow_foreground")),
        "skip_if_busy": bool(payload.get("skip_if_busy", True)),
        "enabled": bool(payload.get("enabled", True)),
    }


# -- slot maths ------------------------------------------------------------

def _slots_for_day(schedule: dict, day: datetime) -> list[datetime]:
    """Every moment this schedule wants to fire on the given date, in
    order. A schedule with no end time has exactly one."""
    if schedule["days"] and day.weekday() not in schedule["days"]:
        return []
    start_h, start_m = (int(p) for p in schedule["start_time"].split(":"))
    first = day.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if not schedule.get("end_time") or not schedule.get("repeat_minutes"):
        return [first]
    end_h, end_m = (int(p) for p in schedule["end_time"].split(":"))
    last = day.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    step = timedelta(minutes=int(schedule["repeat_minutes"]))
    slots, at = [], first
    while at <= last:
        slots.append(at)
        at += step
    return slots


def due_slot(schedule: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """The slot that is due right now, or None. Looks at today and
    yesterday so a window running up to midnight isn't cut short by the
    date rolling over between ticks."""
    now = now or _now_local()
    last_fired = schedule.get("last_fired_slot")
    for day in (now, now - timedelta(days=1)):
        for slot in reversed(_slots_for_day(schedule, day)):
            if slot > now:
                continue
            if (now - slot).total_seconds() > CATCH_UP_SECONDS:
                return None  # this slot, and every earlier one, is stale
            if last_fired and last_fired >= slot.isoformat():
                return None  # already fired this one
            return slot
    return None


def next_slot(schedule: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """The next moment this schedule will fire, for display. Looks a week
    ahead, which is as far as any of these repeat."""
    now = now or _now_local()
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for slot in _slots_for_day(schedule, day):
            if slot > now:
                return slot
    return None


def describe(schedule: dict) -> str:
    """One line a person can check at a glance -- the whole point of
    which is catching a schedule that says something other than what
    was meant before it runs unattended."""
    days = schedule.get("days") or []
    if not days or len(days) == 7:
        when = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "Weekdays"
    elif days == [5, 6]:
        when = "Weekends"
    else:
        when = ", ".join(DAY_NAMES[d] for d in days)
    if schedule.get("end_time") and schedule.get("repeat_minutes"):
        return (f"{when}, {schedule['start_time']}-{schedule['end_time']}, "
                f"every {schedule['repeat_minutes']} min")
    if schedule.get("end_time"):
        return f"{when}, once between {schedule['start_time']} and {schedule['end_time']}"
    return f"{when} at {schedule['start_time']}"


def _mark_past_slots_spent(schedule: dict) -> None:
    """Points last_fired_slot at the most recent slot that has already
    passed, so nothing fires for a moment that was gone before this
    schedule existed (or while it was switched off). A schedule saved at
    16:59 for a 16:55 start waits until tomorrow, which is what saving it
    at 16:59 looks like it should do."""
    now = _now_local()
    for day in (now, now - timedelta(days=1)):
        past = [slot for slot in _slots_for_day(schedule, day) if slot <= now]
        if past:
            schedule["last_fired_slot"] = past[-1].isoformat()
            return


class Scheduler:
    """Owns the schedule list, the queue, the clock thread and the worker
    thread. One instance, created by main.py, which injects the two
    things it can't reach from here without an import cycle: how to run
    a macro, and how to tell the browser something changed."""

    def __init__(self, run_macro: Callable[[str, bool], dict],
                 get_macro_name: Callable[[str], Optional[str]],
                 broadcast: Callable[[dict], None]):
        self._run_macro = run_macro
        self._get_macro_name = get_macro_name
        self._broadcast = broadcast
        self._lock = threading.RLock()
        self._schedules: list[dict] = []
        self._queue: list[dict] = []
        self._paused = False
        self._current_id: Optional[str] = None
        self._started = False
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not SCHEDULES_PATH.exists():
            return
        try:
            data = json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # a corrupted file starts empty rather than crashing the agent
        raw = data.get("schedules") if isinstance(data, dict) else data
        if isinstance(raw, list):
            self._schedules = [s for s in raw if isinstance(s, dict) and s.get("id")]

    def _save(self) -> None:
        _atomic_write(SCHEDULES_PATH, {"schedules": self._schedules})

    # -- threads -----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._clock_loop, daemon=True, name="scheduler-clock").start()
        threading.Thread(target=self._worker_loop, daemon=True, name="queue-worker").start()

    def _clock_loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:  # a bad schedule must not kill the clock
                print(f"[scheduler] Tick failed (will retry): {exc}")
            time.sleep(TICK_SECONDS)

    def _tick(self) -> None:
        now = _now_local()
        fired: list[tuple[dict, datetime]] = []
        with self._lock:
            busy = self._current_id is not None or any(i["state"] == "queued" for i in self._queue)
            for schedule in self._schedules:
                if not schedule.get("enabled"):
                    continue
                slot = due_slot(schedule, now)
                if slot is None:
                    continue
                # Claim the slot either way: a skipped firing is still a
                # firing, or a busy stretch would fire the moment it clears.
                schedule["last_fired_slot"] = slot.isoformat()
                schedule["last_fired_at"] = _now_iso()
                if busy and schedule.get("skip_if_busy", True):
                    schedule["last_outcome"] = "skipped (something was already running)"
                    continue
                schedule["last_outcome"] = "queued"
                fired.append((schedule, slot))
            if fired:
                self._save()
        for schedule, slot in fired:
            self._enqueue_macros(
                schedule["macro_ids"],
                allow_foreground=schedule.get("allow_foreground", False),
                source="schedule",
                source_label=f"{schedule['name']} · {slot.strftime('%H:%M')}",
            )
        if fired:
            self._broadcast({"type": "schedules_changed"})

    def _worker_loop(self) -> None:
        while True:
            item = self._claim_next()
            if item is None:
                time.sleep(0.4)
                continue
            self._run_item(item)

    def _claim_next(self) -> Optional[dict]:
        with self._lock:
            if self._paused or self._current_id is not None:
                return None
            for item in self._queue:
                if item["state"] == "queued":
                    item["state"] = "running"
                    item["started_at"] = _now_iso()
                    self._current_id = item["id"]
                    self._broadcast_queue()
                    return item
        return None

    def _run_item(self, item: dict) -> None:
        from agent.replay import ReplayBusyError  # local import: replay is heavy, and only needed here

        try:
            result = self._run_macro(item["macro_id"], item["allow_foreground"])
        except ReplayBusyError:
            # Someone pressed play by hand while this was starting. Put it
            # back at the front rather than failing it -- the queue's whole
            # job is waiting for the mouse to be free.
            with self._lock:
                item["state"] = "queued"
                item["started_at"] = None
                self._current_id = None
                self._broadcast_queue()
            time.sleep(2.0)
            return
        except Exception as exc:
            self._finish_item(item, "failed", str(exc), None)
            return

        summary = result.get("summary", {}) or {}
        if summary.get("stopped"):
            state, detail = "stopped", f"Stopped after {len(result.get('results', []))} step(s)."
        elif summary.get("failed"):
            state = "failed"
            detail = f"{summary.get('passed', 0)}/{summary.get('total', 0)} steps passed, {summary['failed']} failed."
        else:
            state = "done"
            detail = f"Finished — {summary.get('passed', 0)}/{summary.get('total', 0)} steps."
        self._finish_item(item, state, detail, summary)

    def _finish_item(self, item: dict, state: str, detail: str, summary: Optional[dict]) -> None:
        with self._lock:
            item["state"] = state
            item["detail"] = detail
            item["summary"] = summary
            item["finished_at"] = _now_iso()
            self._current_id = None
            self._trim_history()
            self._broadcast_queue()

    def _trim_history(self) -> None:
        finished = [i for i in self._queue if i["state"] in ("done", "failed", "stopped", "cancelled")]
        for old in finished[: max(len(finished) - HISTORY_LIMIT, 0)]:
            self._queue.remove(old)

    # -- queue -------------------------------------------------------------

    def _broadcast_queue(self) -> None:
        self._broadcast({"type": "queue_changed"})

    def _enqueue_macros(self, macro_ids: list[str], allow_foreground: bool,
                        source: str, source_label: str) -> list[dict]:
        added = []
        with self._lock:
            for macro_id in macro_ids:
                name = self._get_macro_name(macro_id)
                if name is None:
                    continue  # deleted since the schedule was written -- skip, don't crash the run
                item = {
                    "id": uuid.uuid4().hex[:12],
                    "macro_id": macro_id,
                    "macro_name": name,
                    "allow_foreground": allow_foreground,
                    "source": source,
                    "source_label": source_label,
                    "state": "queued",
                    "enqueued_at": _now_iso(),
                    "started_at": None,
                    "finished_at": None,
                    "detail": None,
                    "summary": None,
                }
                self._queue.append(item)
                added.append(item)
            if added:
                self._broadcast_queue()
        return added

    def enqueue(self, macro_ids: list[str], allow_foreground: bool = False) -> list[dict]:
        if not macro_ids:
            raise ValueError("Pick at least one macro to queue.")
        added = self._enqueue_macros(macro_ids, allow_foreground, "manual", "Added by hand")
        if not added:
            raise ValueError("None of those macros exist any more.")
        return added

    def queue_state(self) -> dict:
        with self._lock:
            return {
                "paused": self._paused,
                "running_id": self._current_id,
                "items": [dict(i) for i in self._queue],
            }

    def set_paused(self, paused: bool) -> dict:
        """Pausing stops the NEXT item starting. Whatever is mid-run keeps
        going -- stopping that is what the Stop button is for."""
        with self._lock:
            self._paused = bool(paused)
            self._broadcast_queue()
        return self.queue_state()

    def cancel_item(self, item_id: str) -> dict:
        with self._lock:
            for item in self._queue:
                if item["id"] != item_id:
                    continue
                if item["state"] != "queued":
                    raise ValueError("That one is not waiting any more -- it has already started or finished.")
                item["state"] = "cancelled"
                item["detail"] = "Cancelled before it started."
                item["finished_at"] = _now_iso()
                self._trim_history()
                self._broadcast_queue()
                return dict(item)
        raise QueueItemNotFoundError(item_id)

    def clear_waiting(self) -> int:
        """Empties the line without touching what is already running."""
        with self._lock:
            cleared = 0
            for item in self._queue:
                if item["state"] == "queued":
                    item["state"] = "cancelled"
                    item["detail"] = "Cleared from the queue."
                    item["finished_at"] = _now_iso()
                    cleared += 1
            if cleared:
                self._trim_history()
                self._broadcast_queue()
            return cleared

    def clear_history(self) -> int:
        with self._lock:
            done = [i for i in self._queue if i["state"] in ("done", "failed", "stopped", "cancelled")]
            for item in done:
                self._queue.remove(item)
            if done:
                self._broadcast_queue()
            return len(done)

    # -- schedules ---------------------------------------------------------

    def _decorate(self, schedule: dict) -> dict:
        out = dict(schedule)
        upcoming = next_slot(schedule) if schedule.get("enabled") else None
        out["next_run"] = upcoming.isoformat() if upcoming else None
        out["summary"] = describe(schedule)
        out["macro_names"] = [self._get_macro_name(m) or "(deleted macro)" for m in schedule.get("macro_ids", [])]
        return out

    def list_schedules(self) -> list[dict]:
        with self._lock:
            return [self._decorate(s) for s in self._schedules]

    def get_schedule(self, schedule_id: str) -> dict:
        with self._lock:
            for schedule in self._schedules:
                if schedule["id"] == schedule_id:
                    return self._decorate(schedule)
        raise ScheduleNotFoundError(schedule_id)

    def create_schedule(self, payload: dict, known_macro_ids: set[str]) -> dict:
        fields = _validate(payload, known_macro_ids)
        schedule = {
            "id": uuid.uuid4().hex[:12],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_fired_slot": None,
            "last_fired_at": None,
            "last_outcome": None,
            **fields,
        }
        _mark_past_slots_spent(schedule)
        with self._lock:
            self._schedules.append(schedule)
            self._save()
            decorated = self._decorate(schedule)
        self._broadcast({"type": "schedules_changed"})
        return decorated

    def update_schedule(self, schedule_id: str, payload: dict, known_macro_ids: set[str]) -> dict:
        fields = _validate(payload, known_macro_ids)
        with self._lock:
            for schedule in self._schedules:
                if schedule["id"] != schedule_id:
                    continue
                schedule.update(fields)
                schedule["updated_at"] = _now_iso()
                _mark_past_slots_spent(schedule)
                self._save()
                decorated = self._decorate(schedule)
                break
            else:
                raise ScheduleNotFoundError(schedule_id)
        self._broadcast({"type": "schedules_changed"})
        return decorated

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict:
        with self._lock:
            for schedule in self._schedules:
                if schedule["id"] != schedule_id:
                    continue
                schedule["enabled"] = bool(enabled)
                schedule["updated_at"] = _now_iso()
                if enabled:
                    _mark_past_slots_spent(schedule)
                self._save()
                decorated = self._decorate(schedule)
                break
            else:
                raise ScheduleNotFoundError(schedule_id)
        self._broadcast({"type": "schedules_changed"})
        return decorated

    def delete_schedule(self, schedule_id: str) -> str:
        with self._lock:
            for schedule in self._schedules:
                if schedule["id"] == schedule_id:
                    self._schedules.remove(schedule)
                    self._save()
                    break
            else:
                raise ScheduleNotFoundError(schedule_id)
        self._broadcast({"type": "schedules_changed"})
        return schedule_id

    def run_schedule_now(self, schedule_id: str) -> list[dict]:
        """The "does this actually do what I think" button. Queues the
        schedule's macros immediately without touching its timing."""
        with self._lock:
            for schedule in self._schedules:
                if schedule["id"] == schedule_id:
                    macro_ids = list(schedule["macro_ids"])
                    allow_foreground = schedule.get("allow_foreground", False)
                    name = schedule["name"]
                    break
            else:
                raise ScheduleNotFoundError(schedule_id)
        added = self._enqueue_macros(macro_ids, allow_foreground, "manual", f"{name} · run now")
        if not added:
            raise ValueError("None of that schedule's macros exist any more.")
        return added
