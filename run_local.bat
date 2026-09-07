@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "HUNTER_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%HUNTER_PYTHON%" (
  echo ERROR: Existing virtual environment was not found at .venv\Scripts\python.exe
  exit /b 1
)

echo Starting Freelance Hunter on http://127.0.0.1:8765
echo HUNTER_MODE is loaded from .env; keep it manual until the live gate is approved.
"%HUNTER_PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --log-level info
exit /b %ERRORLEVEL%
