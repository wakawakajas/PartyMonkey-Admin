"""Central configuration for the Macro Studio agent.

Everything here is local-only: the agent binds to 127.0.0.1 and never
listens on any other interface. There is no auth because there is no
network exposure to protect against.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Network -----------------------------------------------------------
# Bind to loopback only. Do not change this to 0.0.0.0 -- nothing about
# Macro Studio is meant to be reachable from outside this machine.
HOST = "127.0.0.1"
PORT = int(os.environ.get("MACRO_STUDIO_PORT", "8756"))

# --- Paths ---------------------------------------------------------------
AGENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENT_DIR.parent
WEB_DIR = ROOT_DIR / "web"
MACROS_DIR = ROOT_DIR / "macros"
MACRO_VERSIONS_DIR = MACROS_DIR / "versions"
RUNS_DIR = ROOT_DIR / "runs"

MACROS_DIR.mkdir(exist_ok=True)
MACRO_VERSIONS_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

# --- Misc ------------------------------------------------------------
APP_NAME = "Macro Studio"
APP_VERSION = "0.1.0"
MAX_MACRO_VERSIONS = 10

# Run reports (JSON + failure screenshots) are kept forever -- they're
# small. Video recordings are the one thing in runs/ that can actually
# pile up, so those alone get auto-deleted once they're this old. The
# rest of that run's report stays intact, just without a video link.
VIDEO_RETENTION_DAYS = 5

# The web UI's <title> tag. Used later (self-exclusion) to help identify
# our own browser tab's window among all open windows.
WEB_WINDOW_TITLE_HINT = "Macro Studio"
