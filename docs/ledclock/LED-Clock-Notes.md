# LED Clock — Connection, Protocol, and Controls

Working notes for continuing this work in a new session.
Written 2026-09-07. Companion file: `ledclock.py` (in this folder).

---

## 1. START HERE: it is NOT a network device

The vendor spec is `Comminucation Protocol.pdf` (note the misspelling — that is the
actual filename, at the root of this USB drive). It describes a **serial** protocol
only, and that is exactly what the clock is.

The cable running to the clock looks like Ethernet, but **the PC end is a USB-to-serial
adapter that enumerates as a COM port.** There is no IP address and no network socket.

Do not repeat this dead end — it was already searched exhaustively:

- Full TCP port scan (all 65535 ports) of the wired `192.168.0.0/24` subnet found only
  two hosts: `192.168.0.180`, a Windows PC (ports 135/139/445/5040/5357 + RPC ephemerals),
  and `192.168.0.239`, a NETGEAR GS108PEv3 switch (firmware 2.06.16).
- A layer-2 ARP sweep across 19 candidate subnets (`192.168.0/1/2/3/4/8/10/16/100/101/
  123/169.x`, `10.0.0/10.0.1/10.1.1/10.10.10.x`, `172.16.0/172.16.1.x`, `192.0.2.x`)
  found no third device on the wire at all.

**Go straight to the COM port.**

---

## 2. Physical connection

| Property | Value |
| --- | --- |
| Adapter | USB-SERIAL CH340 |
| USB VID:PID | `1A86:7523` |
| Driver service | `CH341SER_A64` (manufacturer `wch.cn`) |
| Instance ID | `USB\VID_1A86&PID_7523\5&8002C9A&0&1` |
| Port | `COM10` *(as enumerated on 2026-09-07)* |
| Baud / framing | **9600 8N1**, no flow control |

On open, `cts/dsr/ri/cd` all read False — there is no hardware handshaking. Don't wait
on modem status lines.

> **The COM port number can change between plug-ins.** Always confirm first:
> ```
> python -m serial.tools.list_ports -v
> ```
> Look for the `1A86:7523` hwid. If the number differs, either pass it in or edit the
> `PORT` constant at the top of `ledclock.py`.

Requires `pyserial`: `python -m pip install pyserial`

---

## 3. Frame formats

### Command (PC → clock)

```
0x1F   '0'    <cmd>   [ascii data]   0x09
SOH    addr                          ETX
```

The PC's sender address is ASCII `'0'` (0x30). The PDF's frame diagram also shows a
leading five-byte `0x00 0x00 0x00 0x00 0x00` preamble — the clock **accepts it but does
not require it**. Both forms were tested and both work. `ledclock.py` omits it by default.

### Response (clock → PC) — *not documented in the PDF, derived from live traffic*

```
0xF1  0x1F 0x1F   '2'    <cmd>   <ascii data>   0x09   <checksum>
                  addr                          ETX
```

The clock answers on address ASCII `'2'`.

**Checksum** = low byte of the sum of `addr + cmd + data`.
The `0x1F` framing bytes and the trailing `0x09` are **excluded** from the sum.
(The PDF labels a "Check Field" but never defines it. This was reverse-engineered.)

Confirmed against three live frames:

| Frame payload | Sum | Checksum byte |
| --- | --- | --- |
| `2R190010` | 0x1AF | `AF` ✅ |
| `2R190012` | 0x1B1 | `B1` ✅ |
| `2R200109` | 0x1B0 | `B0` ✅ |

Worked example:

```
tx: 1F 30 52 09                                     ("R" — read)
rx: F1 1F 1F 32 52 31 39 30 30 31 30 09 AF
            '2' 'R'  1  9  0  0  1  0     checksum   → 19:00:10
```

### ⚠ Only `R` ever replies

Every set and control command returns **complete silence**. "No response" is normal and
is *not* a failure signal. The only way to confirm a state change is to follow it with an
`R` read — which is what `ledclock.py` does automatically after `settime`.

---

## 4. Command reference

All times are 6 ASCII digits, `HHMMSS`.

### Value commands

| Command | Bytes | Meaning | Status |
| --- | --- | --- | --- |
| `M` + HHMMSS | `1F 30 4D … 09` | Set time / switch to time mode | ✅ **Verified** |
| `R` | `1F 30 52 09` | Read current value | ✅ **Verified** |
| `D` + HHMMSS | `1F 30 44 … 09` | Stop timer and display value | ⬜ Untested |
| `T` + `1` + HHMMSS | `1F 30 54 31 … 09` | Second count**up** | ✅ **Verified** |
| `T` + `2` + HHMMSS | `1F 30 54 32 … 09` | Second count**down** | ⬜ Untested |
| `T` + `3` + HHMMSS | `1F 30 54 33 … 09` | Stopwatch countup | ⬜ Untested |
| `T` + `4` + HHMMSS | `1F 30 54 34 … 09` | Stopwatch countdown | ⬜ Untested |

### Simple commands — `1F 30 41 <code> 09`

| Name in PDF | Code | Actual behaviour | Status |
| --- | --- | --- | --- |
| Stop timer | `P` (0x50) | Freezes the running timer | ✅ Works as documented |
| Clock display | `K` (0x4B) | Returns to time-of-day; time preserved | ✅ Works as documented |
| **Reset timer** | `O` (0x4F) | **Only freezes — does NOT zero** | ❌ **PDF is wrong** |
| **Start timer** | `Q` (0x51) | **Does nothing at all** | ❌ **PDF is wrong** |
| Power on/off | `E` (0x45) | — | ⬜ Untested |
| Brightness | `T` (0x54) | — | ⬜ Untested |
| Second countup | `L` (0x4C) | — | ⬜ Untested |
| Second countdown | `M` (0x4D) | — | ⬜ Untested |
| Stopwatch countup | `N` (0x4E) | — | ⬜ Untested |

Beep 1 second is its own command, not an `A` code: `1F 30 50 31 09` (`P` + `1`). Untested.

---

## 5. The important gotcha: there is no working start or reset

Hardware testing showed `A Q` (start) and `A O` (reset) do not do what the PDF claims:

```
=== STOP ===        1F 30 41 50 09
  clock 00:00:13 / 00:00:13 / 00:00:13      ← froze correctly

=== START ===       1F 30 41 51 09
  clock 00:00:13 / 00:00:13 / 00:00:13      ← did NOT resume

=== RESET ===       1F 30 41 4F 09
  clock 00:00:13 / 00:00:13 / 00:00:13      ← did NOT zero
```

Sent mid-run, `A O` merely froze the timer at `00:00:04` instead of zeroing it.

### Work around it with `T1`

`T1<HHMMSS>` **sets the value and starts it running in a single command.** That one
primitive covers start, restart, and resume:

- **Start / reset to zero** → `T1` + `000000`
- **Resume from a stop** → re-send `T1` with the frozen value.
  Verified: frozen at `00:00:04`, sent `T1 000004`, it continued 4 → 5 → 7 → 8.

---

## 6. Driver usage — `ledclock.py`

```
python ledclock.py read                 # read current value
python ledclock.py monitor [seconds]    # poll once a second vs. PC clock
python ledclock.py settime now|HHMMSS   # set time / time mode
python ledclock.py show HHMMSS          # stop timer, display value  (D)
python ledclock.py countup [HHMMSS]     # second countup   (T1, default 000000)
python ledclock.py countdown HHMMSS     # second countdown (T2)
python ledclock.py sw-countup [HHMMSS]  # stopwatch countup   (T3)
python ledclock.py sw-countdown HHMMSS  # stopwatch countdown (T4)
python ledclock.py stop|start|reset     # see the caveats in section 5
python ledclock.py clock|power|brightness
python ledclock.py second-up|second-down|stopwatch-up
python ledclock.py beep
python ledclock.py raw "1F 30 52 09"    # arbitrary hex
```

`monitor` prints the PC clock beside the device value, which is the quickest way to
confirm the link and check drift:

```
PC 20:01:14   clock 20:01:14
PC 20:01:16   clock 20:01:15
PC 20:01:17   clock 20:01:16
```

A ±1s wobble between rows is sampling jitter, not drift — each poll is a serial round
trip at 9600 baud, so the two clocks are sampled a fraction of a second apart.

### Race sequence

```
python ledclock.py countup 000000    # arm + run
python ledclock.py stop              # freeze at the finish
python ledclock.py read              # read off the final time
python ledclock.py clock             # back to time-of-day
```

---

## 7. Clock accuracy

On first contact the clock read **58 minutes 06 seconds slow** (19:01:36 vs. PC 19:59:42).
Its *rate* was accurate — it tracked the PC second-for-second — so this was a pure offset,
not drift. Corrected with `settime now` and verified in sync afterwards.

**Unknown: whether the RTC is battery-backed.** If it comes up wrong after a power cycle,
it isn't holding time, and `settime now` should be part of the pre-meet setup.

---

## 8. Open questions for the next session

1. **Does `T3` (stopwatch countup) give finer resolution than `T1`?** The PDF lists
   stopwatch as a separate mode from "second" mode, which hints at hundredths. This
   matters for sprint timing. Wired up in the driver as `sw-countup`, never run.
2. `T2` countdown, and `D` (stop-and-display) — both untested.
3. What do `A E` (power), `A T` (brightness) actually do? Brightness may cycle levels
   rather than take a parameter. Power may blank the display — test when it is safe to.
4. Is the RTC battery-backed (see section 7)?
5. Does the address byte matter? The PC sends `'0'`, the clock answers as `'2'`. Whether
   multiple clocks can be chained on one line was never explored.
6. `R` returns `HHMMSS` — does it report anything different while in stopwatch mode?

## 9. What has actually been done to the device

Read commands, one `settime`, and the count-up / stop / clock-mode tests above.
The clock was left **in time-of-day mode showing the correct time.** Nothing was
configured persistently and no settings were changed.
