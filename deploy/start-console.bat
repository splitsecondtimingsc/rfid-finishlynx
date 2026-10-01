@echo off
rem Finish-line console launcher for the Windows timing PC.
rem Put a shortcut to this file in shell:startup (or a Task Scheduler
rem "At log on" task) so the console is up before you sit down.
rem The loop restarts the app if it ever exits - same job systemd does
rem for the Pi stations.
cd /d %~dp0\..

rem LED-clock serial adapter driver checks. Signed packages ride along in
rem deploy\ch340 (newer clock) and deploy\pl2303 (Electro Numerics clock)
rem so an offline race site can still provision a fresh laptop. Installs
rem need admin once; after that these checks are no-ops.
set CLOCKDRVFAIL=
reg query "HKLM\SYSTEM\CurrentControlSet\Services\CH341SER_A64" >nul 2>&1
if not errorlevel 1 goto ch340ok
reg query "HKLM\SYSTEM\CurrentControlSet\Services\CH341SER" >nul 2>&1
if not errorlevel 1 goto ch340ok
echo CH340 serial driver (LED clock) not installed - installing...
pnputil /add-driver "%~dp0ch340\driver\CH341SER.INF" /install
if errorlevel 1 set CLOCKDRVFAIL=1
:ch340ok
reg query "HKLM\SYSTEM\CurrentControlSet\Services\ser2pl_1" >nul 2>&1
if not errorlevel 1 goto pl2303ok
echo PL2303 serial driver (Electro clock) not installed - installing...
pnputil /add-driver "%~dp0pl2303\driver\ser2pl_1.inf" /install
if errorlevel 1 set CLOCKDRVFAIL=1
:pl2303ok
if defined CLOCKDRVFAIL (
    echo.
    echo   *** Clock driver install failed - likely needs admin. ***
    echo   Right-click deploy\install-clock-drivers.bat and
    echo   "Run as administrator", then plug the clock back in.
    echo.
)

rem Python 3.12 that INSTALL.bat set up (its packages live there), else PATH.
set "PY=%ProgramFiles%\Python312\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

:loop
rem After a self-update: undo it if the new version keeps failing to start.
if exist "%~dp0boot_guard.py" "%PY%" "%~dp0boot_guard.py"
"%PY%" -m rfid_finishlynx.main
echo Console exited - restarting in 5 seconds (Ctrl+C twice to stop)
timeout /t 5 /nobreak >nul
goto loop
