@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed yet. Install it from python.org and tick "Add python.exe to PATH".
  pause
  exit /b
)
if not exist venv python -m venv venv
call venv\Scripts\activate
pip install -r requirements.txt
python app.py
pause
