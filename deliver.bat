@echo off
cd /d "%~dp0whv-job-tracker\web"
set FLASK_DEBUG=0
set AUTO_SHUTDOWN=1
start "WHV Tracker" pythonw app.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5000/dashboard
