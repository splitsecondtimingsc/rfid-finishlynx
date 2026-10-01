@echo off
rem Set FinishLynx up for the RFID console, in its Lynx.cfg:
rem   LapTime     entry with Device "Lynx" listening on TCP 5000
rem   Scoreboard  entry "Middleware": Example2.lss, UDP 127.0.0.1:5891, running time
rem   Folders     input = C:\Finish_Lynx_Results\RFID, output = C:\Finish_Lynx_Results
rem Close FinishLynx first (it rewrites Lynx.cfg when it exits). A backup
rem Lynx.cfg.before-rfid-<date> is kept next to it; copy it back to undo.
rem Safe to run again: settings already right are left alone.
setlocal
cd /d "%~dp0.."
rem Python 3.12 that INSTALL.bat set up (its packages live there), else PATH.
set "PY=%ProgramFiles%\Python312\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo Current FinishLynx settings:
"%PY%" -m rfid_finishlynx.lynx_cfg check
if not errorlevel 1 (
    echo.
    echo FinishLynx is already set up for the console - nothing to change.
    goto done
)
if errorlevel 2 goto done
echo.
choice /C YN /M "Change these FinishLynx settings now (FinishLynx must be closed)"
if errorlevel 2 goto done
"%PY%" -m rfid_finishlynx.lynx_cfg apply

:done
echo.
pause
