@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo 패키지 설치에 실패했습니다. Python 3.11 이상이 설치돼 있는지 확인하세요.
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
py -3 -m daytrading
if errorlevel 1 pause
