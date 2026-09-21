@echo off
REM Daily interview check. Point Windows Task Scheduler at this file.
REM Writes a dated report and keeps a rolling log.

cd /d "%~dp0"
if not exist reports mkdir reports

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set STAMP=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%

python run.py --format markdown --out "reports\%STAMP%.md" >> "reports\monitor.log" 2>&1
exit /b %errorlevel%
