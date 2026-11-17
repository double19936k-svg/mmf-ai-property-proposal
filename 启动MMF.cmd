@echo off
setlocal
title MMF Desktop
if exist "%~dp0dist\MMF-006E-portable-v0.1\tools\launch_mmf.ps1" (
  set "MMF_DIST=%~dp0dist\MMF-006E-portable-v0.1"
) else (
  set "MMF_DIST=%~dp0"
)
set "MMF_LAUNCHER=%MMF_DIST%\tools\launch_mmf.ps1"

if not exist "%MMF_LAUNCHER%" (
  echo Production launcher not found:
  echo %MMF_LAUNCHER%
  pause
  exit /b 1
)

powershell.exe -NoProfile -Command "try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:3042/api/health' -TimeoutSec 2; if($h.runtime_source -eq 'dist'){ Start-Process 'http://127.0.0.1:3042/'; exit 0 }; exit 1 } catch { exit 1 }"
if not errorlevel 1 exit /b 0

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%MMF_LAUNCHER%" -Port 3042
if errorlevel 1 pause
exit /b %errorlevel%
