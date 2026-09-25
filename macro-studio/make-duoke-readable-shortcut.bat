@echo off
rem Puts a "DuoKe (readable)" shortcut on this PC's desktop. Macro Studio can
rem only draft replies in the background -- without clicking anything -- when
rem DuoKe is opened from this shortcut. Close DuoKe first, then open it from
rem the new shortcut.
set "EXE=%LOCALAPPDATA%\Programs\Duoke\Duoke.exe"
if not exist "%EXE%" (
    echo DuoKe was not found at %EXE%
    pause
    exit /b 1
)
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\DuoKe (readable).lnk');" ^
  "$s.TargetPath='%EXE%';" ^
  "$s.Arguments='--force-renderer-accessibility --disable-backgrounding-occluded-windows --disable-features=CalculateNativeWinOcclusion --remote-debugging-port=9223';" ^
  "$s.WorkingDirectory='%LOCALAPPDATA%\Programs\Duoke';" ^
  "$s.IconLocation='%EXE%,0';" ^
  "$s.Save()"
echo Done: "DuoKe (readable)" is on the desktop. Close DuoKe and open it from there.
pause
