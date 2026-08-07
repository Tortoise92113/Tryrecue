@echo off
REM 開發模式：自動重載 (reloader) 開啟，無自動關閉。改前端後重新整理瀏覽器即可。
REM 視窗會顯示 log；按 Ctrl+C 或關閉視窗即停止。
cd /d "%~dp0whv-job-tracker"
echo DEV mode - auto-reload ON, no auto-shutdown. Open http://127.0.0.1:5000  (Ctrl+C to stop)
python web\app.py
