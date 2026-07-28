@echo off
setlocal
set "log=%~dp0collect-and-upload-dev-bundle.last.log"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect-and-upload-dev-bundle.ps1" > "%log%" 2>&1
set "code=%errorlevel%"
type "%log%"
echo.
if not "%code%"=="0" (
  echo COLLECT OR UPLOAD FAILED. Exit code: %code%
  echo The full error was saved to:
  echo %log%
) else (
  echo WORKFLOW FINISHED. Exit code: 0
)
echo.
echo This window will stay open.
pause
exit /b %code%
