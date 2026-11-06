@echo off
setlocal enabledelayedexpansion
title Lyrics Overlay
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem Register the lyricsoverlay:// link (per-user, no admin needed) so the
rem "Start Lyrics Overlay" button in Pigu can start this exact install.
rem Redone every run: %~f0 always points at THIS file, so it stays right
rem if the folder moves. (Macro Studio's start.bat registers it too, so
rem the button works on PCs that have never opened the overlay yet.)
rem ---------------------------------------------------------------------
reg add "HKCU\Software\Classes\lyricsoverlay" /ve /d "URL:Lyrics Overlay Protocol" /f >nul 2>&1
reg add "HKCU\Software\Classes\lyricsoverlay" /v "URL Protocol" /d "" /f >nul 2>&1
reg add "HKCU\Software\Classes\lyricsoverlay\shell\open\command" /ve /d "\"%~f0\" \"%%1\"" /f >nul 2>&1

rem ---------------------------------------------------------------------
rem Get the newest version from GitHub first, so every PC runs the same
rem app without anyone sending files around. Only tracked code changes:
rem settings.json (bar position, sync fixes) is git-ignored and kept.
rem --ff-only leaves a PC alone if its code was edited by hand.
rem It is all one block because cmd reads a .bat as it runs: if the pull
rem rewrote this file, the rest is run from a fresh copy instead.
rem ---------------------------------------------------------------------
where git >nul 2>&1
if not errorlevel 1 if exist "%~dp0..\.git" (
    for /f %%h in ('git -C "%~dp0.." rev-parse HEAD 2^>nul') do set "OLD_HEAD=%%h"
    echo Checking for updates...
    set "GIT_TERMINAL_PROMPT=0"
    git -C "%~dp0.." pull --ff-only -q >nul 2>&1
    for /f %%h in ('git -C "%~dp0.." rev-parse HEAD 2^>nul') do set "NEW_HEAD=%%h"
    if not "!OLD_HEAD!"=="!NEW_HEAD!" (
        echo Updated -- starting the new version.
        start "" /min "%~f0" %*
        exit /b 0
    )
)

rem ---- first run on this PC: set it up ----
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run - setting up, this takes a minute...
    call "%~dp0Install on this PC.bat" < nul
)

rem ---- new libraries needed by an update? install them ----
fc /b requirements.txt .requirements.installed >nul 2>&1
if errorlevel 1 (
    echo Installing new parts for this version...
    ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
    if not errorlevel 1 copy /y requirements.txt .requirements.installed >nul
)

rem ---- close an older copy that is still open, then start ----
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*overlay.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
start "" ".venv\Scripts\pythonw.exe" "%~dp0overlay.py"
exit /b 0
