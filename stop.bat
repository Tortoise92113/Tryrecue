@echo off
REM Kill all WHV Tracker server background processes (pythonw web\app.py).
REM Killing only the netstat LISTENING PID is not enough: Windows SO_REUSEADDR
REM lets repeated start.bat runs spawn extra processes that never truly bind
REM the port but stay alive as zombies, so kill by command line instead.
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*web\app.py*' }; if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('killed PID ' + $_.ProcessId) } } else { Write-Host 'server not running' }"
timeout /t 2 /nobreak >nul
