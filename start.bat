@echo off
cd /d "%~dp0whv-job-tracker"
start "" python web\app.py
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5000"
