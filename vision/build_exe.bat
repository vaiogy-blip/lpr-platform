@echo off
rem Build on the dev laptop:
rem   build_out\plate_collector\  collector + label web page + lane test (small, no PyTorch)
rem   build_out\plate_trainer\    training + exam (large, includes PyTorch)
cd /d "%~dp0"
.venv\Scripts\python -m PyInstaller --noconfirm --onedir --console --name plate_collector ^
  --distpath build_out --workpath build_out\work --specpath build_out ^
  --collect-data rapidocr_onnxruntime --add-data "%~dp0label_page.html;." ^
  --hidden-import label_server --hidden-import lane_test --exclude-module torch collector.py
if errorlevel 1 exit /b 1
for %%f in (config.example.json start_collector.bat start_label.bat README.md) do copy /y %%f build_out\plate_collector\ >nul
if "%1"=="collector" exit /b 0
.venv\Scripts\python -m PyInstaller --noconfirm --onedir --console --name plate_trainer ^
  --distpath build_out --workpath build_out\work --specpath build_out ^
  --collect-data rapidocr_onnxruntime --add-data "%~dp0models;models" ^
  --hidden-import evaluate train.py
if errorlevel 1 exit /b 1
copy /y weekly_train.bat build_out\plate_trainer\ >nul
