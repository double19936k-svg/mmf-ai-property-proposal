@echo off
cd /d "%~dp0"
title MMF Desktop
echo Starting MMF Desktop...
echo If this is the first run, setup may take one or two minutes.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch_mmf.ps1"
if errorlevel 1 (
  echo.
  echo Start failed. See runtime\launcher_status.log
  pause
  exit /b 1
)
exit /b 0
