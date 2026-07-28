@echo off
setlocal
pushd "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collector.ps1" -Manifest "%~dp0targets.json"
set code=%errorlevel%
echo.
echo Exit code: %code%
echo Output directory: %~dp0output
pause
popd
exit /b %code%
