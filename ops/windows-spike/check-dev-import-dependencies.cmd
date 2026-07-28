@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-dev-import-dependencies.ps1"
if errorlevel 1 (
  echo.
  echo Dependency check reported missing packages.
  pause
)
