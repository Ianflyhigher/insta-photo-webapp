@echo off
chcp 65001 >nul
title 인스타 사진 다운로더
cd /d "%~dp0"
set PYTHONUTF8=1

echo.
echo ============================================================
echo    인스타 사진 다운로더
echo    - 이 창을 닫으면 프로그램이 꺼집니다.
echo ============================================================
echo.

rem ---------- Python 찾기 ----------
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [문제] Python 이 설치되어 있지 않습니다.
  echo.
  echo   1. https://www.python.org/downloads/ 에 접속해 Python 을 내려받아 설치하세요.
  echo   2. 설치 첫 화면 아래쪽의 "Add python.exe to PATH" 를 꼭 체크하세요.
  echo   3. 설치가 끝나면 이 창을 닫고 실행하기.bat 을 다시 더블클릭하세요.
  echo.
  pause
  exit /b 1
)

rem ---------- 처음 한 번만: 가상환경 만들기 ----------
if not exist ".venv\Scripts\python.exe" (
  echo 처음 실행이라 준비 중입니다. 1~2분 정도 걸립니다...
  %PY% -m venv .venv
  if errorlevel 1 goto venvfail
)

rem ---------- 처음 한 번만: 필요한 패키지 설치 ----------
if not exist ".venv\.packages_ok" (
  echo 필요한 프로그램을 설치합니다. 잠시만 기다려 주세요...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
  echo ok> ".venv\.packages_ok"
)

rem ---------- 실행 ----------
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto runfail
goto end

:venvfail
echo.
echo [문제] 가상환경(.venv)을 만들지 못했습니다.
echo   - Python 을 다시 설치하면서 "Add python.exe to PATH" 를 체크해 보세요.
echo   - 이 폴더를 바탕화면처럼 한글 경로가 없는 곳에 두면 더 안전합니다.
echo.
pause
exit /b 1

:pipfail
echo.
echo [문제] 필요한 프로그램 설치에 실패했습니다.
echo   - 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
echo   - 회사/학교 네트워크에서는 설치가 막힐 수 있습니다.
echo.
pause
exit /b 1

:runfail
echo.
echo [문제] 프로그램이 비정상적으로 끝났습니다.
echo   - 위쪽의 오류 내용을 확인해 주세요.
echo   - 5000 번 포트를 다른 프로그램이 쓰고 있으면 자동으로 5001 로 실행됩니다.
echo.
pause
exit /b 1

:end
echo.
echo 프로그램을 종료했습니다. 창을 닫아 주세요.
pause
