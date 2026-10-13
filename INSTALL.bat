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
rem    - meet sharing with the team's other PCs (NAS repo or Google Drive)
rem    - "RFID Console" shortcuts on the Desktop and in Startup
rem    - launches the console and opens http://localhost:8080/
rem  Re-running it is safe: it upgrades files and skips what is done. It
rem  never copies over this PC's settings, meets or race state, and leaves
rem  a console that is already running alone.
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
echo === [1/10] Copying files to %TARGET% ===
if /i "%SRC%"=="%TARGET%\" goto skipcopy
rem Per-machine runtime files are never copied over an existing install
rem (config.yaml is created from config.example.yaml on first run). Same
rem list as tools\promote.py, so a used copy (a dev clone, last season's
rem folder) can't overwrite this PC's meets, closed-meet archives or race
rem state (2026-10-05). The meet store is excluded by full path: the
rem rfid_finishlynx\meet code folder shares its name and must be copied.
rem The per-PC meet folder and camera settings files too (audit 2026-10-13):
rem the same list on both editions, so a copy made from a camera-edition
rem PC can't put that PC's paths or camera settings on this one.
robocopy "%SRC%." "%TARGET%" /e /nfl /ndl /njh /njs ^
    /xf config.yaml config.yaml.tmp races-state.json races-state.json.tmp PREVIEW_ONLY PROMOTIONS.log lif_mirror.err *.log *.mirror *.bak *.orig *.pyc ^
        meet-folders.json meet-folders.json.tmp camera-prefs.json camera-prefs.json.tmp ^
        camera-profiles.json camera-profiles.json.tmp camera-seen.json camera-seen.json.tmp ^
    /xd .git .venv .venv-lab venv __pycache__ .pytest_cache closed-meets lif-exports edgeprof dist "%SRC%meet" "%SRC%meet-sync" "%SRC%bench\results" >nul
if errorlevel 8 goto fail
:skipcopy
rem A zip from the internet marks every file "blocked"; unblock the install
rem so the console never stops at a security prompt on a meet morning.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%TARGET%' -Recurse -File | Unblock-File" >nul 2>&1

echo.
echo === [2/10] Python ===
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
echo === [3/10] Python packages (offline, bundled wheels) ===
"%PYEXE%" -m pip install --no-warn-script-location --no-index --find-links "%TARGET%\wheels" -r "%TARGET%\requirements.txt"
if not errorlevel 1 goto depsok
echo Offline install failed - retrying from the internet...
"%PYEXE%" -m pip install --no-warn-script-location -r "%TARGET%\requirements.txt"
if errorlevel 1 goto fail
:depsok

echo.
echo === [4/10] LED-clock USB-serial drivers ===
pnputil /add-driver "%TARGET%\deploy\ch340\driver\CH341SER.INF" /install >nul 2>&1
pnputil /add-driver "%TARGET%\deploy\pl2303\driver\ser2pl_1.inf" /install >nul 2>&1
echo Done (already-installed drivers are skipped automatically).

echo.
echo === [5/10] Results folder and firewall ===
if not exist "%RESULTS%\RFID" mkdir "%RESULTS%\RFID"
echo %RESULTS%\RFID  - FinishLynx start lists go here (from the console's Meet menu, or MeetPro);
echo                 the console writes .lif files here
rem The console listens for FinishLynx's scoreboard feed (UDP 5891) on every
rem interface; its web page (8080) answers on this PC only since 2026-10-04.
rem Allowing it up front avoids the Windows Firewall pop-up; only this
rem subnet can reach it.
netsh advfirewall firewall delete rule name="RFID Console" >nul 2>&1
netsh advfirewall firewall add rule name="RFID Console" dir=in action=allow program="%PYEXE%" remoteip=localsubnet profile=any enable=yes >nul
echo Firewall: RFID Console allowed from the local network.

pushd "%TARGET%"

echo.
echo === [6/10] Network: this PC and the RFID reader ===
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
echo === [7/10] Wireshark (for start times straight from the camera) ===
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
echo === [8/10] FinishLynx settings (LapTime, scoreboard, folders) ===
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

echo.
echo === [9/10] Meet sharing with this team's other PCs ===
rem From the package's EDITION.json (tools/build_package.py --edition): the
rem fleet's private repo on the NAS, or a Google Drive for desktop folder.
rem A PC that already shares is left as it is. (2026-10-07)
"%PYEXE%" -m rfid_finishlynx.meet_sync --install

popd

echo.
echo === [10/10] Shortcuts (Desktop + auto-start at logon) and launch ===
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; foreach ($d in @([Environment]::GetFolderPath('Desktop'), $ws.SpecialFolders('Startup'))) { $s = $ws.CreateShortcut((Join-Path $d 'RFID Console.lnk')); $s.TargetPath = 'C:\rfid_finishlynx\deploy\start-console.bat'; $s.WorkingDirectory = 'C:\rfid_finishlynx'; $s.Description = 'RFID FinishLynx middleware console'; $s.Save() }"
rem Already running (a re-run to upgrade): don't start a second copy, and
rem don't restart it here - a race may be running. (2026-10-05)
set "RUNNING="
netstat -ano -p tcp | findstr /r /c:":8080 .*LISTENING" >nul && set "RUNNING=1"
if defined RUNNING (
    echo   The console is already running on the old version. When no race is
    echo   running, close its window and start it again from the Desktop
    echo   "RFID Console" shortcut to load the new one.
    explorer.exe "http://localhost:8080/"
    goto launched
)
rem explorer.exe launches it de-elevated (as the normal user)
explorer.exe "%TARGET%\deploy\start-console.bat"
timeout /t 8 /nobreak >nul
explorer.exe "http://localhost:8080/"
:launched

echo.
echo ============================================================
echo  Install complete.
echo   - Console page:      http://localhost:8080/
echo   - Desktop shortcut:  "RFID Console" (also runs at logon)
echo   - Is everything set? Click the "Console" chip at the top of the
echo     page: its Setup check lists anything left, with the fix.
echo   - Meets:             Meet menu in the console (MileSplit entries, bibs,
echo                        start lists, results). Using MeetPro instead?
echo                        Its F.A.T. directory = %RESULTS%\RFID
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
