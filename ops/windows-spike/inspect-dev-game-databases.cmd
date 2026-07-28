@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0inspect-dev-game-databases.ps1"
if errorlevel 1 (
  echo.
  echo Database inventory failed. Please take a screenshot of this window.
  pause
)
