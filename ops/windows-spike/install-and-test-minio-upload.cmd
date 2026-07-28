@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-and-test-minio-upload.ps1"
if errorlevel 1 (
  echo.
  echo Install or upload test failed. Please take a screenshot of this window.
  pause
)
