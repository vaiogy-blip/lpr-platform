@echo off
rem Weekly training + exam. Put plate_trainer next to plate_collector (same parent folder)
rem so both use ..\plate_collector\config.json and its data folder.
chcp 65001 >nul
cd /d "%~dp0"
set CFG=..\plate_collector\config.json
if exist plate_trainer.exe (
  plate_trainer.exe train --minutes 180 --config "%CFG%"
) else (
  .venv\Scripts\python train.py train --minutes 180 %*
)
