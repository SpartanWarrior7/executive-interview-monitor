@echo off
setlocal

REM Run the safe GitHub-main updater from this project's root directory.
REM Example: .\droplet-update.cmd -Force
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\deploy-main-to-droplet.ps1" %*
exit /b %ERRORLEVEL%
