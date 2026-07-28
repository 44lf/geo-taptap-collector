@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0export-dev-taptap-target.ps1"
if errorlevel 1 (
  echo.
  echo DEV target export failed. Please take a screenshot of this window.
  pause
)
