@echo off
chcp 65001 >nul
title Lyrics Overlay - install
cd /d "%~dp0"
echo.
echo  Setting up Lyrics Overlay on this PC...
echo.

rem ---- 1. find Python 3.12 (install it if missing) ----
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
  where py >nul 2>nul && for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)
if not exist "%PY%" (
  echo  Python not found - installing Python 3.12 ^(takes a minute^)...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not exist "%PY%" (
  echo.
  echo  Could not install Python automatically.
  echo  Please install Python 3.12 from https://www.python.org/downloads/ then run this again.
  pause
  exit /b 1
)

rem ---- 2. private copy of the libraries in this folder ----
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul || rmdir /s /q .venv
)
if not exist ".venv\Scripts\python.exe" "%PY%" -m venv .venv
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo  Library install failed - check the internet connection and run this again.
  pause
  exit /b 1
)
copy /y requirements.txt .requirements.installed >nul

rem ---- 3. desktop shortcut -> Start Lyrics Overlay.bat (gets updates from GitHub, then opens the app) ----
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Lyrics Overlay.lnk'); $s.TargetPath='%~dp0Start Lyrics Overlay.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.IconLocation='%SystemRoot%\System32\imageres.dll,103'; $s.Save()"

echo.
echo  Done! Open "Lyrics Overlay" from your desktop.
echo  If Windows asks about network access, click Allow (needed to Join colleagues).
echo  Then right-click the lyrics bar ^> Join a colleague's lyrics.
echo.
pause
