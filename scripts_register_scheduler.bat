@echo off
REM =====================================================
REM  Reels Maker - Register Windows Task Scheduler jobs
REM   - ReelsMaker_Daily  : every day 17:30
REM   - ReelsMaker_Backup : every day 23:00
REM
REM  Remove them with:
REM    schtasks /delete /tn "ReelsMaker_Daily" /f
REM    schtasks /delete /tn "ReelsMaker_Backup" /f
REM
REM  ASCII only: do not add Korean text to this file.
REM =====================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\\" set "PROJECT=%PROJECT:~0,-1%"
set "VBS=%PROJECT%\scripts_run_hidden.vbs"
set "DAILY=%PROJECT%\scripts_run_daily.bat"

echo ===================================================
echo   Reels Maker - Task Scheduler
echo   Folder : %PROJECT%
echo ===================================================
echo.

if not exist "%DAILY%" (
  echo [ERROR] scripts_run_daily.bat was not found next to this file.
  echo         Unzip the whole project folder, then run this file again.
  pause
  exit /b 1
)
if not exist "%VBS%" (
  echo [ERROR] scripts_run_hidden.vbs was not found next to this file.
  pause
  exit /b 1
)

echo [1/3] Register ReelsMaker_Daily (every day 17:30) ...
schtasks /create /tn "ReelsMaker_Daily" /tr "wscript.exe ""%VBS%"" ""%DAILY%""" /sc daily /st 17:30 /f
if errorlevel 1 (
  echo [ERROR] Could not register the daily task.
  echo         Right click this file, choose "Run as administrator" and retry.
  pause
  exit /b 1
)
echo [OK] ReelsMaker_Daily registered
echo.

echo [2/3] Register ReelsMaker_Backup (every day 23:00) ...
schtasks /create /tn "ReelsMaker_Backup" /tr "wscript.exe ""%VBS%"" ""%DAILY%""" /sc daily /st 23:00 /f
if errorlevel 1 (
  echo [ERROR] Could not register the backup task.
  pause
  exit /b 1
)
echo [OK] ReelsMaker_Backup registered
echo.

echo [3/3] Current registration ...
schtasks /query /tn "ReelsMaker_Daily" /fo LIST | findstr /i "TaskName Status Next"
schtasks /query /tn "ReelsMaker_Backup" /fo LIST | findstr /i "TaskName Status Next"
echo.
echo [DONE] Daily 17:30 and backup 23:00 are registered.
echo        Test the pipeline right now with scripts_run_daily.bat
echo.
echo        Remove later with:
echo          schtasks /delete /tn "ReelsMaker_Daily" /f
echo          schtasks /delete /tn "ReelsMaker_Backup" /f
echo.
pause
endlocal
exit /b 0
