@echo off
REM ============================================================
REM  Reels Maker - Run (web UI)   << double click me
REM
REM  Starts the local server with the project venv and opens
REM  http://127.0.0.1:5000 in your browser.
REM  Keep this window open. Close it (or Ctrl+C) to stop.
REM  Change the port:  set RACCOON_WEB_PORT=5001  then run.bat
REM
REM  ASCII only: do not add Korean text to this file.
REM ============================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"
set "VENV=%PROJECT%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" (
  echo ============================================================
  echo  [ERROR] The virtual environment was not found:
  echo          %PY%
  echo.
  echo  Run install.bat once first, then run.bat again.
  echo ============================================================
  echo.
  pause
  exit /b 1
)

set "PORT=5000"
if not "%RACCOON_WEB_PORT%"=="" set "PORT=%RACCOON_WEB_PORT%"
set "URL=http://127.0.0.1:%PORT%"
title Reels Maker - Web UI (%PORT%)

echo ============================================================
echo  Reels Maker - Web UI
echo  Folder : %PROJECT%
echo  Python : %PY%
echo  URL    : %URL%
echo ============================================================
echo.

REM keep config.json paths valid even if the folder was moved
"%PY%" "%PROJECT%\bootstrap.py" >nul 2>&1

REM already running? just open the browser
"%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(1.0); sys.exit(0 if s.connect_ex(('127.0.0.1', %PORT%)) == 0 else 1)"
if not errorlevel 1 (
  echo [INFO] The server is already running on %URL%
  echo [INFO] Opening the browser only. Close this window when done.
  start "" "%URL%"
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

echo [1/2] Starting the web server... keep this window open.
start "" /b "%PY%" -c "import time, webbrowser; time.sleep(4); webbrowser.open('%URL%')"
echo [2/2] The browser will open at %URL%
echo.
"%PY%" "%PROJECT%\web_pipeline.py"
set "RC=%ERRORLEVEL%"

echo.
echo [STOPPED] Web server exit code %RC%
echo.
pause
endlocal & exit /b %RC%
