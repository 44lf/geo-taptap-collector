@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0export-uploader-credentials.ps1"
if errorlevel 1 (
  echo.
  echo Export failed. Please take a screenshot of this window.
  pause
)
