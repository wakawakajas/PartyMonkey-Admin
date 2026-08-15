"""Persisted run reports: one JSON file plus any failure screenshots per
run, under runs/<run_id>/. This is what the web UI's full run report and
the "screenshot of the screen at failure" requirement read back.

Screenshot capture is best-effort -- if Pillow isn't installed or a grab
fails, callers get None back and just don't attach a screenshot, rather
than the whole run failing over a nice-to-have.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent import config

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None  # surfaced via capture_failure_screenshot returning None


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]


def _run_dir(run_id: str) -> Path:
    d = config.RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_failure_screenshot(run_id: str, seq) -> Optional[str]:
    """Full-screen capture (all monitors) at the moment of a step
    failure. Returns a web-servable path under /runs/, or None."""
    if ImageGrab is None:
        return None
    try:
        img = ImageGrab.grab(all_screens=True)
        filename = f"step_{seq}_failure.png"
        img.save(_run_dir(run_id) / filename, "PNG")
        return f"/runs/{run_id}/{filename}"
    except Exception:
        return None


def save_report(run_id: str, macro_id: Optional[str], macro_name: str, report: dict) -> None:
    data = dict(report)
    data["run_id"] = run_id
    data["macro_id"] = macro_id
    data["macro_name"] = macro_name
    (_run_dir(run_id) / "report.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_report(run_id: str) -> dict:
    path = config.RUNS_DIR / run_id / "report.json"
    if not path.exists():
        raise FileNotFoundError(run_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports(limit: int = 50) -> list[dict]:
    reports = []
    if not config.RUNS_DIR.exists():
        return reports
    for d in sorted((p for p in config.RUNS_DIR.iterdir() if p.is_dir()), reverse=True):
        report_path = d / "report.json"
        if not report_path.exists():
            continue
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        reports.append({
            "run_id": data.get("run_id", d.name),
            "macro_id": data.get("macro_id"),
            "macro_name": data.get("macro_name"),
            "started_at": data.get("started_at"),
            "summary": data.get("summary"),
        })
        if len(reports) >= limit:
            break
    return reports


def cleanup_old_videos(retention_days: int = config.VIDEO_RETENTION_DAYS) -> int:
    """Deletes recording.mp4 files older than retention_days, updating
    that run's report.json so the UI stops linking to a file that no
    longer exists. The rest of the report (results, screenshots) is left
    alone -- only the video itself is disposable. Returns how many were
    deleted."""
    if not config.RUNS_DIR.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for video_path in config.RUNS_DIR.glob("*/recording.mp4"):
        try:
            if video_path.stat().st_mtime >= cutoff:
                continue
            video_path.unlink()
            deleted += 1
        except OSError:
            continue

        report_path = video_path.parent / "report.json"
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                data["video"] = None
                data["video_error"] = f"Recording deleted after {retention_days} days (auto-cleanup)."
                report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass
    return deleted
