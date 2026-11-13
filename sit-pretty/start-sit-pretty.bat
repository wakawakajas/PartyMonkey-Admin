@echo off
setlocal enabledelayedexpansion
title Sit Pretty
cd /d "%~dp0"
echo ============================================
echo   Sit Pretty - return evidence videos
echo ============================================
echo.

rem --- let Pigu's "Start Sit Pretty" button (sitpretty://) start this file ----
reg add "HKCU\Software\Classes\sitpretty" /ve /d "URL:Sit Pretty Protocol" /f >nul 2>&1
reg add "HKCU\Software\Classes\sitpretty" /v "URL Protocol" /d "" /f >nul 2>&1
reg add "HKCU\Software\Classes\sitpretty\shell\open\command" /ve /d "\"%~f0\" \"%%1\"" /f >nul 2>&1
set "FROM_PIGU="
echo %~1 | find /i "sitpretty:" >nul && set "FROM_PIGU=1"

rem --- already running on this PC? just open it ---------------------------
curl -s -o nul -m 1 "http://127.0.0.1:8787/api/state"
if not errorlevel 1 (
    echo Sit Pretty is already running.
    if not defined FROM_PIGU start "" "http://localhost:8787/"
    exit /b 0
)

rem --- get the latest version from GitHub (same as Macro Studio) ----------
where git >nul 2>&1
if not errorlevel 1 if exist "%~dp0..\.git" (
    echo Checking for updates...
    set "GIT_TERMINAL_PROMPT=0"
    git -C "%~dp0.." pull --ff-only -q >nul 2>&1
)

rem --- Python ---------------------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo Python is not installed. Installing it now - this takes a few minutes...
    winget install --id Python.Python.3.12 -e --source winget --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PY (
    echo.
    echo Could not install Python. Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then double-click this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Sit Pretty for the first time...
    %PY% -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Could not install Sit Pretty's add-ons. Check the internet connection and try again.
    pause
    exit /b 1
)

rem --- ffmpeg ---------------------------------------------------------------
set "HAVE_FF="
where ffmpeg >nul 2>&1 && set "HAVE_FF=1"
if not defined HAVE_FF for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do set "HAVE_FF=1"
if not defined HAVE_FF for /d %%D in ("%~dp0..\ffmpeg-*") do set "HAVE_FF=1"
if not defined HAVE_FF (
    echo Installing ffmpeg ^(the video tool^)...
    winget install --id Gyan.FFmpeg -e --source winget --scope user --accept-package-agreements --accept-source-agreements
)

rem --- this PC's camera logins (never uploaded) -----------------------------
if not exist "sitpretty-secrets.txt" (
    copy /y "sitpretty-secrets.example.txt" "sitpretty-secrets.txt" >nul
    echo.
    echo FIRST TIME ON THIS PC: type the camera logins into the file that opens now,
    echo save it, then close Notepad. Sit Pretty starts after that.
    notepad "sitpretty-secrets.txt"
)

rem --- the save folder must be reachable ------------------------------------
if not exist "N:\Yong Chuan\Dispute" (
    echo.
    echo WARNING: N:\Yong Chuan\Dispute is not reachable on this PC.
    echo Map the NAS drive N: first, or videos cannot be saved.
)

echo.
echo Sit Pretty is running. Keep this window open.
echo On this PC:  http://localhost:8787
echo Other PCs and phones:  http://%COMPUTERNAME%:8787   ^(this PC's name, it never changes^)
powershell -nop -c "$ip=(Get-NetIPAddress -AddressFamily IPv4 | ? IPAddress -like '192.168.10.*' | select -First 1).IPAddress; if($ip){'On a phone:  http://'+$ip+':8787'}"
if not defined FROM_PIGU start "" "http://localhost:8787/"
".venv\Scripts\python.exe" server.py
pause
