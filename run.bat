@echo off
cd /d "%~dp0"
set "PY=py -3"
py -3.14 -c "import sys" >nul 2>&1 && set "PY=py -3.14"
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo 패키지 설치에 실패했습니다. Python 3.11 이상이 설치돼 있는지 확인하세요.
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
%PY% -m daytrading
if errorlevel 1 pause
