@echo off
cd /d "%~dp0whv-job-tracker\web"
start "WHV Tracker" python app.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5000/dashboard
