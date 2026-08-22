"""Macro Studio agent entry point.

Serves the web UI, exposes the recording/status/settings API, and a
websocket that streams live recording updates to connected clients.
Replay, UIA capture, and the macro library are stubbed and arrive in
later phases -- calling them now returns a clear "not implemented yet"
error instead of doing nothing silently.

Run with:  python -m agent.main
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import cdp, config, macro_store, run_reports, settings, video
from agent.macro_store import MacroNotFoundError
from agent.panic import PanicWatcher
from agent.recorder import Recorder
from agent.replay import ReplayBusyError, ReplayEngine
from agent.web_recorder import WebRecorder

START_TIME = time.time()
AGENT_PID = os.getpid()


def _is_admin() -> bool:
    """Best-effort check for whether the agent process is elevated.

    Later phases need this: if a target app runs elevated and we are not,
    UIA calls into it will silently fail or be refused. We surface that
    to the user instead of letting it look like a mysterious bug.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


IS_ADMIN = _is_admin() if platform.system() == "Windows" else False

app = FastAPI(title=config.APP_NAME)

# -- live-update transport -------------------------------------------------
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
CONNECTED_CLIENTS: set[WebSocket] = set()


async def _broadcast_async(message: dict) -> None:
    data = json.dumps(message)
    dead = []
    for ws in list(CONNECTED_CLIENTS):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CONNECTED_CLIENTS.discard(ws)


def broadcast(message: dict) -> None:
    """Thread-safe broadcast callable. The recorder calls this from
    pynput's own listener threads, not the asyncio loop, so it has to
    hop over via run_coroutine_threadsafe rather than await directly."""
    if MAIN_LOOP is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast_async(message), MAIN_LOOP)


recorder = Recorder(broadcast=broadcast)
replay_engine = ReplayEngine(broadcast=broadcast)
web_recorder = WebRecorder(broadcast=broadcast)


def _handle_panic() -> None:
    """Esc held for 1s: halt everything immediately, regardless of what's
    running. Both calls are individually safe to make when there's
    nothing to stop -- recorder.cancel() raises, replay's request_stop()
    just returns False."""
    stopped_recording = False
    try:
        recorder.cancel()
        stopped_recording = True
    except RuntimeError:
        pass
    stopped_replay = replay_engine.request_stop()
    if stopped_recording or stopped_replay:
        broadcast({"type": "panic_triggered", "stopped_recording": stopped_recording, "stopped_replay": stopped_replay})


panic_watcher = PanicWatcher(on_panic=_handle_panic)

VIDEO_CLEANUP_INTERVAL_SECONDS = 6 * 3600  # re-check a few times a day -- cheap, no need for more


def _video_cleanup_loop() -> None:
    while True:
        try:
            deleted = run_reports.cleanup_old_videos()
            if deleted:
                print(f"[cleanup] Deleted {deleted} video(s) older than {config.VIDEO_RETENTION_DAYS} days.")
        except Exception as exc:
            print(f"[cleanup] Video cleanup pass failed (will retry later): {exc}")
        time.sleep(VIDEO_CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def _on_startup() -> None:
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    panic_watcher.start()
    threading.Thread(target=_video_cleanup_loop, daemon=True).start()


@app.get("/api/status")
def get_status() -> dict:
    """Basic liveness + environment info the web UI polls on load."""
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "phase": 10,
        "pid": AGENT_PID,
        "is_admin": IS_ADMIN,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "host": config.HOST,
        "port": config.PORT,
        "started_at": datetime.fromtimestamp(START_TIME, tz=timezone.utc).isoformat(),
    }


def _not_implemented(feature: str, phase: int) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "error": "not_implemented",
            "detail": f"{feature} is not implemented yet -- it arrives in Phase {phase} of the build.",
        },
    )


def _recorder_error(exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "invalid_state", "detail": str(exc)})


@app.post("/api/recording/start")
def start_recording() -> JSONResponse:
    try:
        result = recorder.start()
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.post("/api/recording/pause")
def pause_recording() -> JSONResponse:
    try:
        result = recorder.pause()
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.post("/api/recording/resume")
def resume_recording() -> JSONResponse:
    try:
        result = recorder.resume()
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.post("/api/recording/stop")
def stop_recording() -> JSONResponse:
    try:
        result = recorder.stop()
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.post("/api/recording/cancel")
def cancel_recording() -> JSONResponse:
    """Discards the in-progress or just-stopped recording without saving."""
    try:
        result = recorder.cancel()
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.get("/api/recording/state")
def recording_state() -> dict:
    """Lets the web UI rehydrate the step list on page load/refresh."""
    return {"state": recorder.state, "steps": recorder.steps}


class CdpLaunchRequest(BaseModel):
    port: int = cdp.DEFAULT_PORT
    url: str = ""
    user_data_dir: str = ""


@app.post("/api/cdp/launch")
def cdp_launch(body: CdpLaunchRequest) -> JSONResponse:
    """Opens (or surfaces) the Chrome that the Web: steps drive -- the
    same thing open-cdp-chrome.bat does, for people who are already
    looking at the web UI."""
    try:
        message = cdp.launch(port=body.port, user_data_dir=body.user_data_dir, url=body.url)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "cdp_launch_failed", "detail": str(exc)})
    return JSONResponse(content={"detail": message, "port": body.port})


@app.post("/api/cdp/close")
def cdp_close(body: CdpLaunchRequest) -> JSONResponse:
    """Closes the debugging Chrome -- the counterpart to the launch button,
    for when it's in the way or a run left it somewhere odd."""
    try:
        message = cdp.close_browser(port=body.port)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "cdp_close_failed", "detail": str(exc)})
    return JSONResponse(content={"detail": message, "port": body.port})


@app.post("/api/cdp/hide")
def cdp_hide(body: CdpLaunchRequest) -> JSONResponse:
    """Parks the debugging browser off-screen. Minimising it would stop it
    rendering, and a page that isn't rendering can't open a hover menu."""
    try:
        moved = cdp.park_offscreen(port=body.port)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "cdp_hide_failed", "detail": str(exc)})
    return JSONResponse(content={"detail": f"Parked {moved} window(s) off-screen." if moved
                                 else "Nothing to park -- no debugging browser is open, or it's parked already.",
                                 "port": body.port})


@app.post("/api/cdp/show")
def cdp_show(body: CdpLaunchRequest) -> JSONResponse:
    try:
        shown = cdp.show_windows(port=body.port)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "cdp_show_failed", "detail": str(exc)})
    return JSONResponse(content={"detail": f"Brought {shown} window(s) back on screen." if shown
                                 else "Nothing to bring back -- no debugging browser is open, or it's on screen already.",
                                 "port": body.port})


@app.post("/api/cdp/reload")
def cdp_reload(body: CdpLaunchRequest) -> JSONResponse:
    """Reloads whichever tab the debugging browser is showing -- the
    button version of the reload step, for when a page has gone stale
    while you were building a macro against it."""
    try:
        page = cdp.find_page(body.port, body.url)
        url = cdp.reload(page)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "cdp_reload_failed", "detail": str(exc)})
    return JSONResponse(content={"detail": f"Reloaded {url[:90]}", "port": body.port})


class WebRecordingRequest(BaseModel):
    port: int = cdp.DEFAULT_PORT
    url: str = ""
    tab_match: str = ""
    seed: bool = True


class WebRecordingStopRequest(BaseModel):
    adopt: bool = True


@app.post("/api/web-recording/start")
def start_web_recording(body: WebRecordingRequest) -> JSONResponse:
    if recorder.state in ("recording", "paused"):
        return _recorder_error(RuntimeError("Stop the input recording first -- one recorder at a time."))
    try:
        result = web_recorder.start(port=body.port, url=body.url, tab_match=body.tab_match,
                                    seed=body.seed)
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content={"error": "web_recording_failed", "detail": str(exc)})
    return JSONResponse(content=result)


@app.post("/api/web-recording/stop")
def stop_web_recording(body: WebRecordingStopRequest = WebRecordingStopRequest()) -> JSONResponse:
    """Hands the captured steps to the input recorder's buffer so the
    existing Save/Discard flow covers browser recordings too -- unless the
    caller is the step editor, which has already collected them live and
    would rather the unsaved-recording buffer stayed untouched."""
    try:
        result = web_recorder.stop()
        if body.adopt:
            recorder.adopt_steps(result["steps"])
    except RuntimeError as exc:
        return _recorder_error(exc)
    return JSONResponse(content=result)


@app.post("/api/web-recording/cancel")
def cancel_web_recording() -> JSONResponse:
    return JSONResponse(content=web_recorder.cancel())


@app.get("/api/web-recording/state")
def web_recording_state() -> dict:
    return web_recorder.snapshot()


class HotkeyUpdate(BaseModel):
    keys: list[str]


@app.get("/api/settings/hotkey")
def get_hotkey() -> dict:
    keys = settings.get_stop_hotkey()
    return {"keys": keys, "display": "+".join(k.title() for k in keys)}


@app.put("/api/settings/hotkey")
def set_hotkey(body: HotkeyUpdate) -> JSONResponse:
    try:
        keys = settings.set_stop_hotkey(body.keys)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": "invalid_hotkey", "detail": str(exc)})
    return JSONResponse(content={"keys": keys, "display": "+".join(k.title() for k in keys)})


class ReplayRequest(BaseModel):
    allow_foreground: bool = False


def _run_replay(steps: list[dict], allow_foreground: bool, macro_id: Optional[str] = None,
                 macro_name: str = "(unsaved recording)", video_config: Optional[dict] = None) -> JSONResponse:
    """Shared by "replay the current recording" and "replay a saved
    macro" -- same guards, same engine, same error shapes."""
    if recorder.state in ("recording", "paused"):
        return JSONResponse(status_code=409, content={
            "error": "recording_in_progress", "detail": "Stop the current recording before replaying.",
        })
    if not steps:
        return JSONResponse(status_code=400, content={
            "error": "no_steps", "detail": "This macro has no steps to replay.",
        })
    try:
        result = replay_engine.run(steps, allow_foreground=allow_foreground, macro_id=macro_id,
                                    macro_name=macro_name, video_config=video_config)
    except ReplayBusyError as exc:
        return JSONResponse(status_code=409, content={"error": "replay_busy", "detail": str(exc)})
    return JSONResponse(content=result)


@app.post("/api/replay/last")
def replay_last(body: ReplayRequest) -> JSONResponse:
    """Replays the current (not-yet-saved) recording buffer -- lets you
    test a run before deciding whether it's worth saving."""
    return _run_replay(recorder.steps, body.allow_foreground)


@app.post("/api/replay/stop")
def stop_replay() -> dict:
    """The live status panel's Stop button. Cooperative: the run checks
    for this between (and inside long waits within) steps, so it halts
    promptly but not necessarily mid-instruction."""
    return {"stopped": replay_engine.request_stop()}


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return run_reports.list_reports()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    try:
        return JSONResponse(content=run_reports.get_report(run_id))
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "run_not_found", "detail": f"No run report with id {run_id}."})


# -- macro library (Phase 5) -----------------------------------------------

class MacroCreate(BaseModel):
    name: str


class MacroRename(BaseModel):
    name: str


class MacroDuplicate(BaseModel):
    name: Optional[str] = None


class MacroIds(BaseModel):
    ids: list[str]


class MacroDeleteAllConfirm(BaseModel):
    confirm: str


DELETE_ALL_PHRASE = "DELETE ALL"


def _macro_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, MacroNotFoundError):
        return JSONResponse(status_code=404, content={"error": "macro_not_found", "detail": f"No macro with id {exc}."})
    return JSONResponse(status_code=400, content={"error": "invalid_macro", "detail": str(exc)})


@app.get("/api/macros")
def list_macros() -> list[dict]:
    return macro_store.list_macros()


@app.post("/api/macros")
def save_macro(body: MacroCreate) -> JSONResponse:
    """Saves the current stopped recording as a named macro, then clears
    the recording buffer -- this is the "Save" step after Stop."""
    if recorder.state != "stopped":
        return JSONResponse(status_code=400, content={
            "error": "nothing_to_save", "detail": "Stop a recording before saving it.",
        })
    if not recorder.steps:
        return JSONResponse(status_code=400, content={
            "error": "no_steps", "detail": "Nothing was captured -- nothing to save.",
        })
    try:
        macro = macro_store.create_macro(body.name, recorder.steps)
    except ValueError as exc:
        return _macro_error(exc)
    recorder.cancel()  # consumed into a saved macro -- reset the buffer
    return JSONResponse(content=macro)


@app.get("/api/macros/{macro_id}")
def get_macro(macro_id: str) -> JSONResponse:
    try:
        return JSONResponse(content=macro_store.get_macro(macro_id))
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)


@app.put("/api/macros/{macro_id}/rename")
def rename_macro(macro_id: str, body: MacroRename) -> JSONResponse:
    try:
        return JSONResponse(content=macro_store.rename_macro(macro_id, body.name))
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)


class MacroVideoSettings(BaseModel):
    enabled: bool = False
    mode: str = "fullscreen"  # fullscreen | window | region
    fps: int = 10
    window_title: Optional[str] = None
    region: Optional[dict] = None


@app.put("/api/macros/{macro_id}/video")
def update_macro_video(macro_id: str, body: MacroVideoSettings) -> JSONResponse:
    try:
        return JSONResponse(content=macro_store.update_video_settings(macro_id, body.model_dump()))
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)


@app.get("/api/ffmpeg/status")
def ffmpeg_status() -> dict:
    path = video.find_ffmpeg()
    return {"available": path is not None, "path": path, "download_url": video.FFMPEG_DOWNLOAD_URL}


class MacroStepsUpdate(BaseModel):
    steps: list[dict]


@app.put("/api/macros/{macro_id}/steps")
def update_macro_steps(macro_id: str, body: MacroStepsUpdate) -> JSONResponse:
    """The step editor's Save -- a version snapshot is taken automatically
    before the overwrite (see macro_store), so a bad edit can be undone
    by hand from macros/versions/<id>/."""
    try:
        return JSONResponse(content=macro_store.update_macro_steps(macro_id, body.steps))
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)


@app.post("/api/macros/{macro_id}/duplicate")
def duplicate_macro(macro_id: str, body: MacroDuplicate) -> JSONResponse:
    try:
        return JSONResponse(content=macro_store.duplicate_macro(macro_id, body.name))
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)


@app.delete("/api/macros/{macro_id}")
def delete_macro(macro_id: str) -> JSONResponse:
    try:
        macro_store.delete_macro(macro_id)
    except MacroNotFoundError as exc:
        return _macro_error(exc)
    return JSONResponse(content={"deleted": macro_id})


@app.post("/api/macros/delete-selected")
def delete_selected_macros(body: MacroIds) -> dict:
    return {"deleted": macro_store.delete_macros(body.ids)}


@app.post("/api/macros/delete-all")
def delete_all_macros(body: MacroDeleteAllConfirm) -> JSONResponse:
    if body.confirm != DELETE_ALL_PHRASE:
        return JSONResponse(status_code=400, content={
            "error": "confirmation_mismatch",
            "detail": f'Type exactly "{DELETE_ALL_PHRASE}" to confirm deleting every macro.',
        })
    return JSONResponse(content={"deleted": macro_store.delete_all_macros()})


@app.post("/api/macros/{macro_id}/replay")
def replay_macro(macro_id: str, body: ReplayRequest) -> JSONResponse:
    try:
        macro = macro_store.get_macro(macro_id)
    except (MacroNotFoundError, ValueError) as exc:
        return _macro_error(exc)
    response = _run_replay(macro.get("steps", []), body.allow_foreground, macro_id=macro_id,
                            macro_name=macro.get("name", macro_id), video_config=macro.get("video"))
    if response.status_code == 200:
        result = json.loads(response.body)
        macro_store.record_run_result(macro_id, result["summary"])
    return response


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Live-update channel: recording state changes, new steps, and
    heartbeats so the web UI can tell a slow agent from a dead one."""
    await websocket.accept()
    CONNECTED_CLIENTS.add(websocket)
    await websocket.send_text(json.dumps({
        "type": "connected",
        "pid": AGENT_PID,
        "message": "Connected to Macro Studio agent.",
    }))
    try:
        while True:
            try:
                # Don't block forever on recv -- send heartbeats even if
                # the client never sends anything.
                await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({
                    "type": "heartbeat",
                    "ts": time.time(),
                }))
    except WebSocketDisconnect:
        pass
    finally:
        CONNECTED_CLIENTS.discard(websocket)


# Failure screenshots and run report assets -- served directly so the web
# UI can just <img src="/runs/..."> them.
app.mount("/runs", StaticFiles(directory=str(config.RUNS_DIR)), name="runs")

# Static web UI -- mounted last so it doesn't shadow the /api and /ws
# routes above. html=True serves index.html for "/".
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    print(f"{config.APP_NAME} v{config.APP_VERSION} (Phase 10)")
    print(f"  PID:        {AGENT_PID}")
    print(f"  Elevated:   {IS_ADMIN}")
    print(f"  Python:     {platform.python_version()}")
    print(f"  Stop hotkey: {'+'.join(k.title() for k in settings.get_stop_hotkey())}")
    ffmpeg_path = video.find_ffmpeg()
    print(f"  ffmpeg:     {ffmpeg_path or 'NOT FOUND -- video recording steps will fail with a clear error'}")
    print(f"  Serving on: http://{config.HOST}:{config.PORT}")
    if IS_ADMIN:
        print("  NOTE: running elevated. Only elevated target apps will be controllable.")
    print()

    try:
        uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
    except OSError as exc:
        print(f"\nERROR: could not bind to {config.HOST}:{config.PORT} -- {exc}")
        print("Another program (maybe another Macro Studio agent) is probably already using this port.")
        print("Set MACRO_STUDIO_PORT to a different port and try again, or close the other process.")
        sys.exit(1)


if __name__ == "__main__":
    main()
