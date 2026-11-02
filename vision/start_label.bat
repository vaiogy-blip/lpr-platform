@echo off
rem Open the labeling web page (this computer only: http://127.0.0.1:8790). Stop: close this window.
chcp 65001 >nul
cd /d "%~dp0"
if exist plate_collector.exe (
  plate_collector.exe --label %*
) else (
  .venv\Scripts\python label_server.py %*
)
pause
