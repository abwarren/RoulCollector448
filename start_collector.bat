@echo off
REM RoulCollector448 — Windows collector launcher (equivalent of the systemd
REM roulette-collector2.service). Run from Task Scheduler at logon, or double-click.
REM Credentials: set SUNBET_USER / SUNBET_PASS env vars, or create
REM %USERPROFILE%\.roulette2\roulette2_collector.env  (KEY=VALUE lines, chmod/ACL-protect it).
REM Data dir (override with RC_DATA_DIR): %USERPROFILE%\.roulette2

cd /d "%~dp0.."
set "RC_DATA_DIR=%USERPROFILE%\.roulette2"
if not exist "%RC_DATA_DIR%" mkdir "%RC_DATA_DIR%"

echo [%date% %time%] RoulCollector448 collector starting (Windows) >> "%RC_DATA_DIR%\collector.log"
".venv\Scripts\python.exe" -u collector\roulette2_collector.py >> "%RC_DATA_DIR%\collector.log" 2>&1
