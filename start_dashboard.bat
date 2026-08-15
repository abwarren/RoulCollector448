@echo off
REM RoulCollector448 — Windows dashboard launcher (equivalent of the systemd
REM roulette-dashboard.service). Serves the read-only API + frontend on :4480.
REM Open http://127.0.0.1:4480

cd /d "%~dp0.."
set "RC_DATA_DIR=%USERPROFILE%\.roulette2"
if not exist "%RC_DATA_DIR%" mkdir "%RC_DATA_DIR%"

echo [%date% %time%] RoulCollector448 dashboard starting (Windows) >> "%RC_DATA_DIR%\dashboard.log"
".venv\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port 4480 >> "%RC_DATA_DIR%\dashboard.log" 2>&1
