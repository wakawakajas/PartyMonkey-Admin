@echo off
setlocal enabledelayedexpansion
title Macro Studio
cd /d "%~dp0"

echo ============================================
echo   Macro Studio
echo ============================================
echo.

rem ---------------------------------------------------------------------
rem Step 0: register the macrostudio:// link handler (per-user, no admin
rem needed) so a "Launch Macro Studio" link elsewhere -- e.g. a button in
rem another app -- can start this exact install with one click. Cheap and
rem idempotent, so it's fine to redo on every run; %~f0 always points at
rem THIS start.bat, so it stays correct if the folder is moved or copied
rem to another machine.
rem ---------------------------------------------------------------------
reg add "HKCU\Software\Classes\macrostudio" /ve /d "URL:Macro Studio Protocol" /f >nul 2>&1
reg add "HKCU\Software\Classes\macrostudio" /v "URL Protocol" /d "" /f >nul 2>&1
reg add "HKCU\Software\Classes\macrostudio\shell\open\command" /ve /d "\"%~f0\" \"%%1\"" /f >nul 2>&1

rem ---------------------------------------------------------------------
rem Step 0b: if an agent is already up (this was launched again while one
rem is already running -- e.g. via the macrostudio:// link), don't spawn a
rem second one, which would just fail to bind the port. Just open the UI.
rem ---------------------------------------------------------------------
curl -s -o nul -m 1 "http://127.0.0.1:8756/api/status"
if not errorlevel 1 (
    echo Agent is already running -- opening the web UI.
    start "" "http://127.0.0.1:8756/"
    exit /b 0
)

rem ---------------------------------------------------------------------
rem Step 1: find a usable Python (3.9+). Installs it via winget if needed.
rem ---------------------------------------------------------------------
call :find_python
if not defined PYTHON_EXE (
    echo Python 3.9+ was not found on this machine.
    where winget >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ERROR: winget is not available, so Python can't be installed automatically.
        echo Please install Python 3.10 or later yourself from:
        echo     https://www.python.org/downloads/
        echo IMPORTANT: on the installer's first screen, tick "Add python.exe to PATH".
        echo Then close this window and double-click start.bat again.
        pause
        exit /b 1
    )

    echo Installing Python via winget - this may take a few minutes...
    winget install --id Python.Python.3.12 -e --source winget --scope user --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo ERROR: Automatic Python install via winget failed.
        echo Please install Python 3.10+ manually from https://www.python.org/downloads/
        echo ^(tick "Add python.exe to PATH" during install^), then re-run start.bat.
        pause
        exit /b 1
    )

    echo.
    echo Python installed. Re-checking...
    call :find_python
    if not defined PYTHON_EXE (
        echo.
        echo Python was installed, but this window's PATH doesn't have it yet AND it
        echo wasn't found in the usual install folders either.
        echo Please CLOSE this window and double-click start.bat again -- a fresh window
        echo will pick up the updated PATH.
        pause
        exit /b 1
    )
)

echo Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version

rem ---------------------------------------------------------------------
rem Step 2: virtual environment
rem ---------------------------------------------------------------------
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo.
    echo Creating virtual environment...
    "%PYTHON_EXE%" -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create the virtual environment. See the output above.
        pause
        exit /b 1
    )
)
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

rem ---------------------------------------------------------------------
rem Step 3: dependencies
rem ---------------------------------------------------------------------
echo.
echo Checking dependencies...
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies. See the output above for the real cause
    echo ^(often a network problem or a blocked pip download^).
    pause
    exit /b 1
)

rem ---------------------------------------------------------------------
rem Step 4: launch the agent in its own window, wait for it to come up,
rem then open the browser.
rem ---------------------------------------------------------------------
echo.
echo Starting the Macro Studio agent...
start "Macro Studio Agent" "%VENV_PY%" -m agent.main

set "HEALTH_URL=http://127.0.0.1:8756/api/status"
set /a TRIES=0
:wait_for_agent
set /a TRIES+=1
curl -s -o nul -m 1 "%HEALTH_URL%"
if not errorlevel 1 goto agent_ready
if %TRIES% GEQ 20 goto agent_slow
timeout /t 1 /nobreak >nul
goto wait_for_agent

:agent_ready
echo Agent is up. Opening the web UI...
start "" "http://127.0.0.1:8756/"
goto done

:agent_slow
echo.
echo The agent hasn't responded yet after ~20 seconds. Opening the browser anyway --
echo if the page fails to load, check the "Macro Studio Agent" window for errors,
echo then refresh the page once it's ready.
start "" "http://127.0.0.1:8756/"
goto done

:done
echo.
echo Macro Studio is running in the "Macro Studio Agent" window.
echo Close that window to stop the agent.
pause
exit /b 0

rem =======================================================================
:find_python
rem Sets PYTHON_EXE to a python.exe of version 3.9+ if one can be found,
rem leaves it undefined otherwise. Tries, in order: PATH's "python", the
rem "py" launcher, then a direct scan of the usual install folders. The
rem last step matters because a just-installed Python's PATH update
rem doesn't show up in this already-open cmd window -- scanning the
rem filesystem directly means we don't have to ask the user to reopen it.
set "PYTHON_EXE="

for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_EXE call :check_python_candidate "%%P"

if not defined PYTHON_EXE (
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%A in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE call :check_python_candidate "%%A"
    )
)

if not defined PYTHON_EXE (
    set "PF86=%ProgramFiles(x86)%"
    for /d %%V in ("%LocalAppData%\Programs\Python\Python3*") do if not defined PYTHON_EXE if exist "%%~V\python.exe" call :check_python_candidate "%%~V\python.exe"
    for /d %%V in ("%ProgramFiles%\Python3*") do if not defined PYTHON_EXE if exist "%%~V\python.exe" call :check_python_candidate "%%~V\python.exe"
    for /d %%V in ("%PF86%\Python3*") do if not defined PYTHON_EXE if exist "%%~V\python.exe" call :check_python_candidate "%%~V\python.exe"
)
exit /b 0

:check_python_candidate
set "CAND=%~1"
"%CAND%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_EXE=%CAND%"
exit /b 0
