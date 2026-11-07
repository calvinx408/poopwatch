@echo off
rem Stops any running PoopWatch server, then starts it again.
cd /d "%~dp0"
echo Stopping PoopWatch...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'poopwatch serve' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 3 /nobreak >nul
echo Starting PoopWatch...
call "%~dp0start-poopwatch.bat"
