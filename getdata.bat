@echo off
cd /d "%~dp0whv-job-tracker"
python run_scheduler.py --run-once
pause
