@echo off
rem Start the plate collector. Stop: press Ctrl+C or close this window.
chcp 65001 >nul
cd /d "%~dp0"
if exist plate_collector.exe (
  plate_collector.exe %*
) else (
  .venv\Scripts\python collector.py %*
)
pause
