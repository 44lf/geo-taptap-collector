@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-minio-uploader.ps1"
if errorlevel 1 (
  echo.
  echo The setup failed. Please take a screenshot of this window.
  pause
)
