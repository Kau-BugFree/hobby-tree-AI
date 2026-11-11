@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "HF_HUB_OFFLINE=1"
cd /d "%~dp0backend"
"%~dp0runtime\python\python.exe" -B -m pytest tests -q -p no:cacheprovider --basetemp="var\test-%RANDOM%-%RANDOM%" %*
exit /b %ERRORLEVEL%
