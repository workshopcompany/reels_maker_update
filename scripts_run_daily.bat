@echo off
REM ============================================================
REM  Reels Maker - Daily pipeline launcher
REM   - Double click : run the full pipeline now (manual)
REM   - /scheduled   : skip if today's build already finished
REM   - All work is done by pipeline_runner.py
REM     (same state file as the web UI)
REM
REM  ASCII only: do not add Korean text to this file.
REM ============================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"
set "VENV=%PROJECT%\venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "LOGDIR=%PROJECT%\scripts\logs"
set "LOGFILE=%LOGDIR%\build.log"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYTHON%" (
  echo [ERROR] venv not found: %PYTHON%
  echo         Run install.bat first.
  pause
  exit /b 1
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not exist "%PROJECT%\output" mkdir "%PROJECT%\output"

REM keep config.json paths valid (folder may have moved)
"%PYTHON%" "%PROJECT%\bootstrap.py" >nul 2>&1

set "SCHEDULED="
if /i "%~1"=="/scheduled" set "SCHEDULED=--scheduled"

echo =====================================================
echo  Reels Maker pipeline
echo  Project : %PROJECT%
echo  Python  : %PYTHON%
echo  Log     : %LOGFILE%
echo =====================================================
echo.

powershell -NoProfile -Command "$ErrorActionPreference='Continue'; & '%PYTHON%' '%PROJECT%\pipeline_runner.py' --auto %SCHEDULED% 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append; exit $LASTEXITCODE"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [DONE] Pipeline finished. Check output\final and the CapCut draft.
) else (
  echo [FAILED] Exit code %RC%. See %LOGFILE%
)

if not defined SCHEDULED (
  echo.
  pause
)

endlocal & exit /b %RC%
