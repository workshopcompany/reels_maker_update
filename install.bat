@echo off
REM ============================================================
REM  Reels Maker - Installer  (double click once)
REM
REM  1) checks Python 3.10+       2) creates venv\
REM  3) pip install -r requirements.txt
REM  4) checks / installs FFmpeg  5) creates .env from .env.example
REM  6) writes local paths into config.json
REM  7) downloads the Whisper tiny model (optional)
REM
REM  Works from any folder on Windows 10 / 11 (x64).
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
title Reels Maker - Installer

echo ============================================================
echo  Reels Maker - Installer
echo  Folder : %PROJECT%
echo ============================================================
echo.

REM ---------- 1/7 : Python ----------
echo [1/7] Looking for Python 3.10+ ...
set "LAUNCH=py -3"
%LAUNCH% -c "import sys" >nul 2>&1
if errorlevel 1 set "LAUNCH=python"
%LAUNCH% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python was not found.
  echo.
  echo   Install Python 3.10 - 3.12 from:
  echo     https://www.python.org/downloads/windows/
  echo   During setup, tick "Add python.exe to PATH", then run install.bat again.
  echo.
  pause
  exit /b 1
)
%LAUNCH% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [WARN] Python 3.10 or newer is recommended. Detected:
  %LAUNCH% -c "import sys; print(sys.version)"
)
for /f "delims=" %%v in ('%LAUNCH% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [OK] Python %PYVER%

REM ---------- 2/7 : virtual environment ----------
echo.
echo [2/7] Preparing the virtual environment ...
if exist "%PY%" (
  echo [OK] Already exists: %VENV%
) else (
  %LAUNCH% -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] Could not create the virtual environment.
    pause
    exit /b 1
  )
  echo [OK] Created: %VENV%
)
if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  pause
  exit /b 1
)

REM ---------- 3/7 : packages ----------
echo.
echo [3/7] Installing Python packages (a few minutes on the first run) ...
"%PY%" -m pip install --upgrade pip --disable-pip-version-check
"%PY%" -m pip install -r "%PROJECT%\requirements.txt" --disable-pip-version-check
if errorlevel 1 (
  echo [WARN] Some packages failed to install. See the pip messages above.
  echo        The core steps may still work; run install.bat again to retry.
) else (
  echo [OK] Packages installed
)

REM ---------- 4/7 : FFmpeg ----------
echo.
echo [4/7] Checking FFmpeg ...
ffmpeg -version >nul 2>&1
if not errorlevel 1 (
  echo [OK] FFmpeg is available
  goto ffmpeg_done
)
echo [MISSING] FFmpeg not found. Trying winget ...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
ffmpeg -version >nul 2>&1
if not errorlevel 1 (
  echo [OK] FFmpeg installed
  goto ffmpeg_done
)
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
  echo [OK] FFmpeg installed by winget - a new window may be needed for PATH
  goto ffmpeg_done
)
echo [WARN] FFmpeg is still missing. Recording and 10-step assembly need it.
echo        Download: https://www.gyan.dev/ffmpeg/builds/
echo        Unzip it, then add its bin folder to PATH and reopen this window.
:ffmpeg_done

REM ---------- 5/7 : .env ----------
echo.
echo [5/7] Preparing .env ...
if exist "%PROJECT%\.env" (
  echo [OK] .env already exists - left untouched
) else (
  copy /y "%PROJECT%\.env.example" "%PROJECT%\.env" >nul
  echo [OK] .env created. Fill in your API keys (optional) with:
  echo        notepad "%PROJECT%\.env"
)

REM ---------- 6/7 : config paths ----------
echo.
echo [6/7] Writing local paths into config.json ...
"%PY%" "%PROJECT%\bootstrap.py"
if errorlevel 1 echo [WARN] bootstrap.py reported a problem - see the messages above.

REM ---------- 7/7 : Whisper model ----------
echo.
echo [7/7] Whisper model (step 8 - speech timing)
if exist "%PROJECT%\models\whisper\tiny\model.bin" (
  echo [OK] Already downloaded
  goto summary
)
set "DL="
set /p "DL=Download the tiny model now (about 80MB)? [Y/n] : "
if /i "%DL%"=="n" goto summary
if /i "%DL%"=="no" goto summary
"%PY%" -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8', download_root=r'%PROJECT:\=/%/models/whisper/tiny'); print('[OK] whisper tiny ready')"
if errorlevel 1 echo [WARN] Download failed. Step 8 will retry it automatically when it runs.

:summary
echo.
echo ============================================================
echo  [DONE] Installation finished
echo ============================================================
echo  1. Add API keys to .env  (optional but recommended)
echo       notepad "%PROJECT%\.env"
echo  2. Start the program
echo       run.bat  (or double click it)
echo  3. The browser opens at http://127.0.0.1:5000
echo.
echo  Optional GPU steps (scene images / motion) and YouTube upload:
echo       see README.md
echo ============================================================
echo.
pause
endlocal
exit /b 0
