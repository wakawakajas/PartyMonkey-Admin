@echo off
rem Puts a "DuokeAI (readable)" shortcut on this PC's desktop. Macro Studio can
rem only draft replies in the background -- without clicking anything -- when
rem DuokeAI is opened from this shortcut. Close DuokeAI first, then open it from
rem the new shortcut.
set "EXE=%LOCALAPPDATA%\Programs\DuokeAI\DuokeAI.exe"
if not exist "%EXE%" (
    echo DuokeAI was not found at %EXE%
    pause
    exit /b 1
)
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\DuokeAI (readable).lnk');" ^
  "$s.TargetPath='%EXE%';" ^
  "$s.Arguments='--force-renderer-accessibility --disable-backgrounding-occluded-windows --disable-features=CalculateNativeWinOcclusion --remote-debugging-port=9223';" ^
  "$s.WorkingDirectory='%LOCALAPPDATA%\Programs\DuokeAI';" ^
  "$s.IconLocation='%EXE%,0';" ^
  "$s.Save()"
echo Done: "DuokeAI (readable)" is on the desktop. Close DuokeAI and open it from there.
pause
