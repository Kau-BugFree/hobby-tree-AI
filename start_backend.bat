@echo off
chcp 65001 >nul
setlocal
rem 하비트리 RAG 챗봇 백엔드를 켜고, 준비되면 Swagger 화면(/docs)을 브라우저로 엽니다.
rem 더블클릭으로 실행합니다. 서버를 끄려면 이 창에서 Ctrl+C를 누르거나 창을 닫습니다.
set "PORT=8002"
set "URL=http://127.0.0.1:%PORT%"
if "%~1"==":open-docs" goto open_docs

cd /d "%~dp0backend"
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0..\..\rag 챗봇연결 v1\2026-11-03\backend\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [오류] backend\.venv가 없습니다. README의 '설치와 실행'대로 먼저 설치하세요.
  pause
  exit /b 1
)
if not exist ".env" echo [안내] backend\.env가 없어 Claude 답변 없이 검색만 동작합니다.

curl -s -f -o nul "%URL%/health" && (
  echo 서버가 이미 실행 중입니다. Swagger 화면을 엽니다.
  start "" "%URL%/docs"
  exit /b 0
)

echo 서버를 시작합니다. 모델을 GPU에 올리는 데 시간이 걸리며, 준비되면 %URL%/docs 가 열립니다.
start "" /b cmd /c ""%~f0" :open-docs"
"%PYTHON%" -m uvicorn app.main:app --port %PORT%
echo 서버가 종료되었습니다.
pause
exit /b

:open_docs
rem 모델 로딩과 벡터 갱신이 끝나야 /health가 응답하므로 최대 5분 기다린다.
for /l %%i in (1,1,300) do (
  curl -s -f -o nul "%URL%/health" && (start "" "%URL%/docs" & exit /b 0)
  ping -n 2 127.0.0.1 >nul
)
echo [안내] 5분 안에 서버가 준비되지 않았습니다. 위 로그를 확인하세요.
exit /b 1

