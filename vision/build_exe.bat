@echo off
rem Build the collector into vision\build_out\plate_collector\ (run on the dev laptop).
cd /d "%~dp0"
.venv\Scripts\python -m PyInstaller --noconfirm --onedir --console --name plate_collector ^
  --distpath build_out --workpath build_out\work --specpath build_out ^
  --collect-data rapidocr_onnxruntime collector.py
copy /y config.example.json build_out\plate_collector\ >nul
copy /y start_collector.bat build_out\plate_collector\ >nul
copy /y README.md build_out\plate_collector\ >nul
