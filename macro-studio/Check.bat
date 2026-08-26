@echo off
rem Double-click after a macro run to check the result was actually any
rem good. Shows a numbered menu of every check in the checks folder.
rem
rem   Check.bat                        menu
rem   Check.bat worldfirst_downloads   run that one straight away
rem   Check.bat all                    run every one of them
title Check a macro's results
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo Macro Studio hasn't been set up on this machine yet.
    echo Double-click start.bat once first, then come back here.
    echo.
    pause
    exit /b 1
)

"%VENV_PY%" -m checks %*

echo.
pause
