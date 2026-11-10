@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "HF_HOME=%~dp0backend\var\hf_cache"
cd /d "%~dp0backend"
echo HobbyTree V3: http://127.0.0.1:8003/docs
echo Stop: Ctrl+C
"%~dp0runtime\python\python.exe" -B -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8003 %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
