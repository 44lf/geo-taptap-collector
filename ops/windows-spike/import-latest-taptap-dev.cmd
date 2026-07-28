@echo off
setlocal
set "log=%~dp0import-latest-taptap-dev.last.log"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0import-latest-taptap-dev.ps1" > "%log%" 2>&1
set "code=%errorlevel%"
type "%log%"
echo.
if not "%code%"=="0" (
  echo DEV IMPORT FAILED. Exit code: %code%
  echo The full error was saved to:
  echo %log%
) else (
  echo DEV IMPORT WORKFLOW FINISHED. Exit code: 0
)
echo.
echo This window will stay open.
pause
exit /b %code%
