@echo off
rem Opens the same Chrome that Macro Studio's "Web:" steps drive -- the one
rem on the debugging port, using its own profile folder. Use it to sign in
rem to a site, to look up selectors with F12, or just to watch a macro run.
rem
rem Safe to double-click any time: if that Chrome is already open this just
rem adds a window to it. Your normal Chrome is untouched either way, since
rem the two use different profile folders.
rem
rem Optional: pass a URL, e.g.  open-cdp-chrome.bat https://www.bigseller.com

setlocal
set "PORT=9222"
if not "%MACRO_STUDIO_CDP_PORT%"=="" set "PORT=%MACRO_STUDIO_CDP_PORT%"
set "PROFILE=%LocalAppData%\MacroStudio\ChromeProfile"

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not defined CHROME (
    echo Chrome wasn't found in its usual install locations.
    echo Install it from https://www.google.com/chrome/ and run this again.
    pause
    exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo Opening Chrome on debugging port %PORT%
echo Profile: %PROFILE%
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check %1
