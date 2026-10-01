# USB cable driver — Prolific PL2303

The clock connects through a **Prolific PL2303** USB-to-serial cable. On Windows this is
the single most likely thing to go wrong when moving to a new computer, so read this
before assuming the clock or the cable is dead.

---

## The cable

```
Hardware ID   USB\VID_067B&PID_2303
Shows up as   Prolific USB-to-Serial Comm Port (COMxx)
Device class  Ports
Service       ser2pl_1
```

## Driver proven to work with this cable

```
Provider        Prolific
Version         3.9.6.0
Driver date     17 June 2024
INF             ser2pl_1.inf   (published on the source machine as oem70.inf)
Signed by       Microsoft Windows Hardware Compatibility Publisher
Architecture    x64 only  (the INF has only an NTAMD64 section)
```

The exact package is in `driver/`, exported straight from the working machine with
`pnputil /export-driver`:

```
driver/ser2pl_1.inf      the INF
driver/ser2pl_1.cat      the signature catalog  -- keep it next to the INF
driver/ser2pl64_1.sys    the kernel driver
driver/ser2pl64_1.dll    the port property-page provider
```

**Architecture note:** this package is x64 only. On an ARM64 or 32-bit Windows machine
it will not install; get an appropriate build from Prolific.

## Installing on a new machine

Windows 11 usually installs a working PL2303 driver automatically over Windows Update —
try plugging the cable in first and check Device Manager. If that yields nothing, or a
device with a warning icon, install the exported package from an **elevated** prompt:

```powershell
pnputil /add-driver "driver\ser2pl_1.inf" /install
```

Then re-plug the cable and confirm:

```powershell
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.DeviceID -like '*VID_067B&PID_2303*' } |
  Select-Object Name, Status, DeviceID | Format-List
```

You want `Status : OK` and a `(COMxx)` in the name. That COM number goes into
`--port`.

To check which driver version actually bound:

```powershell
Get-CimInstance Win32_PnPSignedDriver |
  Where-Object { $_.DeviceID -like '*VID_067B&PID_2303*' } |
  Select-Object DeviceName, DriverVersion, DriverDate, InfName | Format-List
```

---

## The counterfeit-chip trap

This is the failure mode that eats afternoons.

Counterfeit PL2303 chips are extremely common in cheap USB-serial cables. **Prolific's
modern drivers detect them and deliberately refuse to run them.** The symptom is
distinctive and misleading:

- The driver installs cleanly with no error.
- Device Manager shows the device with a yellow bang.
- The status reads **"This device cannot start. (Code 10)"**, and the port name may
  render as *"PL2303HXA PHASED OUT SINCE 2012. PLEASE CONTACT YOUR SUPPLIER."*
- No COM port is usable.

**This cable is genuine.** It runs on driver 3.9.6.0, which post-dates the counterfeit
lockout by years — a clone would be rejected by it. So if you hit a Code 10 after moving
to a new machine, the cable was swapped for a different one; it is not a regression in
this setup.

If you are stuck with a counterfeit cable, the options are, in order of preference:

1. **Buy a genuine cable.** Cheapest fix in real terms.
2. **Use a different chipset entirely.** An FTDI FT232 or CH340 cable works fine — the
   clock only cares about 9600 8N1 on the wire, not what generates it. The CH340 clock
   on this same setup proves CH340 cables work on this machine.
3. Install a legacy Prolific driver (3.3.x, circa 2008) that predates the lockout. This
   works but is unsigned-adjacent, fragile across Windows updates, and Windows Update
   will try to replace it. Treat as a last resort.

Prolific driver downloads come from Prolific's own site (prolific.com.tw) under their
support/download section — go there rather than trusting a third-party driver mirror,
which is a common malware vector.

---

## Other things that look like driver problems but aren't

- **Port opens, frames send, clock stays dark.** The clock is not in **"rd"** mode. This
  is a clock-side setting and produces exactly the same symptom as a broken cable. Check
  it first — it costs ten seconds.

- **`PermissionError` / "Access is denied" opening the port.** Another process owns it,
  almost always FinishLynx (`Lynx.exe`) with the scoreboard armed. Note that Lynx.exe
  running is *not* by itself enough to block you — it only claims the port while armed.

- **COM number changed by itself.** Normal. Windows assigns per USB port, so moving the
  cable to a different physical socket gives it a different number. Re-detect rather
  than hard-coding.

- **Frames send but digits are garbage.** Check the line settings, not the driver — 8N1
  at 9600. A parity mismatch renders as plausible-looking wrong characters. Note that
  scoreboard 1 in the FinishLynx config uses `9600,8,E,1` for a *different* board; do
  not copy that setting onto this clock.
