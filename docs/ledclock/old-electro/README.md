# Old LED Clock — Electro Numerics Raceclock

Everything needed to drive the 6-digit Electro Numerics Raceclock over serial, from
scratch, on a machine that has never seen it before.

**Status: working.** Connection established and verified against real hardware on
2026-09-07. Every claim in this folder was confirmed on the device, not inferred.

---

## TL;DR

```
Cable      Prolific PL2303 USB-to-serial   (USB\VID_067B&PID_2303)
Port       COM5 on the original machine — re-detect on any other machine
Line       9600 baud, 8 data bits, No parity, 1 stop bit, no flow control
Clock mode must be set to "rd" (remote display) on the clock itself
Frame      b"c" + <exactly 6 ASCII chars> + b"\r\n"
Init       b"c\r"  (send once before the first data frame)
```

Light all six digits:

```
python tool\raceclock.py --port COM5 show 888888
```

---

## What's in this folder

| Path | What it is |
|---|---|
| `README.md` | This file — start here |
| `PROTOCOL.md` | Full wire protocol + how it was derived from the FinishLynx script |
| `DRIVER.md` | USB cable driver: install, version traps, counterfeit-chip warning |
| `tool/raceclock.py` | Working Python driver (pyserial). Self-contained |
| `driver/` | The exact PL2303 driver package exported from the working machine |
| `lss/` | The original FinishLynx scoreboard scripts (the protocol's source of truth) |
| `reference/finishlynx-scoreboard3.txt` | The known-good FinishLynx config block |

---

## Bring-up on a new computer

**1. Find the port.** Do not assume COM5 — the number is assigned per-machine.
Unplug the cable, run the baseline, plug it in, run it again and diff:

```powershell
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.PNPClass -eq 'Ports' -or $_.Name -match 'COM\d+' } |
  Select-Object Name, DeviceID, PNPClass, Status | Format-Table -AutoSize
```

The clock's cable shows up as `Prolific USB-to-Serial Comm Port (COMxx)` with hardware
ID `USB\VID_067B&PID_2303`. If it appears with a yellow bang / `Status` other than `OK`,
read `DRIVER.md` before anything else.

**2. Install the driver if needed.** See `DRIVER.md`. The package in `driver/` is the
one proven to work with this exact cable.

**3. Put the clock in "rd" mode.** This is a setting on the clock itself, not something
sent over the wire. The clock ignores serial data in any other mode — a silent failure
that looks identical to a dead cable.

**4. Test.**

```powershell
pip install pyserial
python tool\raceclock.py --port COM5 show 888888     # all segments lit
python tool\raceclock.py --port COM5 test            # full digit + position walk
python tool\raceclock.py --port COM5 run --seconds 30
```

If `888888` lights every segment of all six digits, you are done.

---

## Driving it from FinishLynx instead

The clock's protocol originates from FinishLynx, and FinishLynx can drive it directly —
no Python involved. Copy `lss/Electro-led.lss` into the FinishLynx program directory
(`C:\Lynx\` on the original machine), then in FinishLynx add a scoreboard with:

```
Name        Timing_Clock
Script      Electro-led.lss
Serial Port <COM port>, 9600, 8, N, 1
```

`reference/finishlynx-scoreboard3.txt` is the full known-good settings block, including
the non-obvious ones (`TimeMode=1`, `ResultsIfArmed=0`, `TimePause=3000`).

### About `Electro-led_Todd.lss`

`lss/` contains a local edit of the stock script. Its **only** difference from stock is
the TimeRunning trailer line code, `\00` → `\01`. Because that line contains no `%s`
format specifier, the requested variable is never substituted and the emitted bytes are
byte-for-byte identical to stock. **It is functionally the same file.** Use either.
Noted here so a future reader doesn't waste time hunting for a difference that isn't
there.

---

## Traps worth knowing

- **The port number is not stable.** It follows the USB port and the machine. Always
  re-detect.

- **FinishLynx can be running without holding the COM port.** Lynx.exe only claims the
  port while the scoreboard is actively armed, so `raceclock.py` and FinishLynx coexist
  fine — but do not let both drive the clock at once. If the port opens with
  `PermissionError` / "Access is denied", FinishLynx has it; disarm the scoreboard or
  close Lynx.exe.

- **Frames must be exactly 6 payload characters.** Short frames are not padded by the
  clock. `raceclock.py` right-justifies and space-pads for you and raises on overflow.

- **Spaces blank a digit** — that is how the running-clock mode fills its unused sixth
  position rather than showing a stray zero.

- **A wrong clock mode is indistinguishable from a wrong cable.** Both are total
  silence. Confirm "rd" mode before debugging anything electrical.

- **PL2303 counterfeits are rampant.** See `DRIVER.md`. Symptom is a Code 10 in Device
  Manager with a driver that installed cleanly.

---

## Related but different hardware

There is a second, unrelated clock on this setup: a **CH340**-based LED clock on COM10,
driven by `C:\Users\toddl\ledclock\ledclock.py`. Different chip, different protocol,
different port. Do not cross-wire the two toolchains.
