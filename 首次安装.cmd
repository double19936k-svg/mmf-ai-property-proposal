@echo off
cd /d "%~dp0"
title MMF Desktop Install
echo Installing MMF Desktop. Please wait...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1"
if errorlevel 1 (
  echo.
  echo Install failed. See runtime\environment_check.json
  pause
  exit /b 1
)
echo.
echo Install finished. Starting MMF...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch_mmf.ps1"
if errorlevel 1 (
  echo.
  echo Start failed. See runtime\launcher_status.log
  pause
  exit /b 1
)
pause
