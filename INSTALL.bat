@echo off
rem ============================================================
rem  RFID FinishLynx Middleware - one-click Windows installer
rem  Double-click me. Everything is bundled; no internet needed.
rem  Installs to C:\rfid_finishlynx, then:
rem    - Python 3.12 (silent, skipped if already present)
rem    - all Python packages (offline, from bundled wheels)
rem    - LED-clock USB-serial drivers (CH340 + PL2303)
rem    - C:\Finish_Lynx_Results\RFID, a firewall rule for the local network
rem    - checks: this PC on the reader's network, Wireshark for camera starts
rem    - optional: FinishLynx's LapTime / scoreboard / folder settings
rem    - "RFID Console" shortcuts on the Desktop and in Startup
rem    - launches the console and opens http://localhost:8080/
rem  Re-running it is safe: it upgrades files and skips what is done.
rem  The Quick Start guide (QUICK-START.html) walks through all of it.
rem ============================================================
setlocal EnableExtensions

rem ---- self-elevate (drivers + Python need admin) ----
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "TARGET=C:\rfid_finishlynx"
set "RESULTS=C:\Finish_Lynx_Results"
set "SRC=%~dp0"

echo.
echo === [1/9] Copying files to %TARGET% ===
if /i "%SRC%"=="%TARGET%\" goto skipcopy
rem Per-machine runtime files are never copied over an existing install
rem (config.yaml is created from config.example.yaml on first run).
robocopy "%SRC%." "%TARGET%" /e /nfl /ndl /njh /njs /xf config.yaml races-state.json PREVIEW_ONLY PROMOTIONS.log /xd .git .venv-lab >nul
if errorlevel 8 goto fail
:skipcopy
rem A zip from the internet marks every file "blocked"; unblock the install
rem so the console never stops at a security prompt on a meet morning.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%TARGET%' -Recurse -File | Unblock-File" >nul 2>&1

echo.
echo === [2/9] Python ===
set "PYEXE="
rem 3.12 only: the bundled wheels are built for it (gevent, pyyaml, ...). Another
rem Python on the PC is left alone; 3.12 installs next to it.
for %%P in ("%ProgramFiles%\Python312\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe") do (
    if not defined PYEXE if exist %%P set "PYEXE=%%~P"
)
if defined PYEXE (
    echo Found Python: %PYEXE%
    goto havepython
)
echo Installing Python 3.12 silently - takes a minute or two...
"%TARGET%\python\python-3.12.10-amd64.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
set "PYEXE=%ProgramFiles%\Python312\python.exe"
if not exist "%PYEXE%" goto fail
:havepython

echo.
echo === [3/9] Python packages (offline, bundled wheels) ===
"%PYEXE%" -m pip install --no-warn-script-location --no-index --find-links "%TARGET%\wheels" -r "%TARGET%\requirements.txt"
if not errorlevel 1 goto depsok
echo Offline install failed - retrying from the internet...
"%PYEXE%" -m pip install --no-warn-script-location -r "%TARGET%\requirements.txt"
if errorlevel 1 goto fail
:depsok

echo.
echo === [4/9] LED-clock USB-serial drivers ===
pnputil /add-driver "%TARGET%\deploy\ch340\driver\CH341SER.INF" /install >nul 2>&1
pnputil /add-driver "%TARGET%\deploy\pl2303\driver\ser2pl_1.inf" /install >nul 2>&1
echo Done (already-installed drivers are skipped automatically).

echo.
echo === [5/9] Results folder and firewall ===
if not exist "%RESULTS%\RFID" mkdir "%RESULTS%\RFID"
echo %RESULTS%\RFID  - MeetPro writes start lists here, the console writes .lif files here
rem The console listens on the local network (web page 8080, FinishLynx
rem scoreboard feed 5891). Allowing it up front avoids the Windows Firewall
rem pop-up; only this subnet can reach it.
netsh advfirewall firewall delete rule name="RFID Console" >nul 2>&1
netsh advfirewall firewall add rule name="RFID Console" dir=in action=allow program="%PYEXE%" remoteip=localsubnet profile=any enable=yes >nul
echo Firewall: RFID Console allowed from the local network.

pushd "%TARGET%"

echo.
echo === [6/9] Network: this PC and the RFID reader ===
"%PYEXE%" -m rfid_finishlynx.setup_check --network 192.168.0.128
if not errorlevel 1 goto netok
echo.
echo   The reader (factory address 192.168.0.128) is only reachable when this
echo   PC's wired adapter - the one on the camera/reader switch - has an address
echo   192.168.0.x, e.g. 192.168.0.200, mask 255.255.255.0 (no gateway needed).
echo   Quick Start guide, section "Network", shows where to set it.
choice /C YN /M "Open Network Connections now to set it"
if errorlevel 2 goto netok
start "" ncpa.cpl
:netok

echo.
echo === [7/9] Wireshark (for start times straight from the camera) ===
"%PYEXE%" -m rfid_finishlynx.setup_check --wireshark
if not errorlevel 1 goto wsok
echo.
echo   Optional. With Wireshark installed the console hears the start sensor
echo   through the FinishLynx camera; without it you set each race's gun from
echo   the pane (Gun = now, or FinishLynx's clock). Its licence does not allow
echo   bundling it here: install it from wireshark.org and TICK "Npcap".
choice /C YN /M "Open the Wireshark download page now"
if errorlevel 2 goto wsok
explorer.exe "https://www.wireshark.org/download.html"
:wsok

echo.
echo === [8/9] FinishLynx settings (LapTime, scoreboard, folders) ===
"%PYEXE%" -m rfid_finishlynx.lynx_cfg check
if errorlevel 2 (
    echo   FinishLynx was not found. Install it with the LapTime plug-in, then
    echo   run %TARGET%\deploy\configure-finishlynx.bat.
    goto flok
)
if not errorlevel 1 (
    echo   FinishLynx is already set up for the console.
    goto flok
)
echo.
echo   The console can make these changes for you (a backup of Lynx.cfg is
echo   kept). FinishLynx must be CLOSED. Or set them by hand from the guide.
choice /C YN /M "Change FinishLynx's settings now"
if errorlevel 2 goto flok
"%PYEXE%" -m rfid_finishlynx.lynx_cfg apply
:flok

popd

echo.
echo === [9/9] Shortcuts (Desktop + auto-start at logon) and launch ===
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; foreach ($d in @([Environment]::GetFolderPath('Desktop'), $ws.SpecialFolders('Startup'))) { $s = $ws.CreateShortcut((Join-Path $d 'RFID Console.lnk')); $s.TargetPath = 'C:\rfid_finishlynx\deploy\start-console.bat'; $s.WorkingDirectory = 'C:\rfid_finishlynx'; $s.Description = 'RFID FinishLynx middleware console'; $s.Save() }"
rem explorer.exe launches it de-elevated (as the normal user)
explorer.exe "%TARGET%\deploy\start-console.bat"
timeout /t 8 /nobreak >nul
explorer.exe "http://localhost:8080/"

echo.
echo ============================================================
echo  Install complete.
echo   - Console page:      http://localhost:8080/
echo   - Desktop shortcut:  "RFID Console" (also runs at logon)
echo   - Is everything set? Click the "Console" chip at the top of the
echo     page: its Setup check lists anything left, with the fix.
echo   - MeetPro:           F.A.T. directory = %RESULTS%\RFID
echo   - Guide:             %TARGET%\QUICK-START.html
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo *** INSTALL FAILED at the step above - see messages. ***
pause
exit /b 1
