@echo off
rem Starts PoopWatch (dashboard + camera watcher) and opens it in the browser.
cd /d "%~dp0"
start "PoopWatch" /min cmd /c ".venv\Scripts\python -m poopwatch serve"
timeout /t 8 /nobreak >nul
start "" http://127.0.0.1:8420
