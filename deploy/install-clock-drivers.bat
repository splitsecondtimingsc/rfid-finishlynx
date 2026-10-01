@echo off
rem One-time admin install of both LED-clock USB-serial drivers.
rem Right-click -> "Run as administrator".
rem   ch340\driver   WCH CH341SER 3.9.2024.09 (newer clock), WHQL-signed,
rem                  from the Microsoft Update Catalog ("wch.cn - Ports").
rem   pl2303\driver  Prolific 3.9.6.0 (Electro Numerics clock), exported
rem                  from the proven working machine (x64 only). If the
rem                  cable then shows Code 10 "PHASED OUT", the cable is a
rem                  counterfeit swap - see docs\ledclock\old-electro\DRIVER.md.
pnputil /add-driver "%~dp0ch340\driver\CH341SER.INF" /install
pnputil /add-driver "%~dp0pl2303\driver\ser2pl_1.inf" /install
pause
