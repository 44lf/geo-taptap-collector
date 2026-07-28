@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-dev-import-environment.ps1"
if errorlevel 1 (
  echo.
  echo Environment installation failed. Please take a screenshot of this window.
  pause
)
