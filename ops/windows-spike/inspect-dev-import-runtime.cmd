@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0inspect-dev-import-runtime.ps1"
if errorlevel 1 (
  echo.
  echo Runtime check failed. Please take a screenshot of this window.
  pause
)
