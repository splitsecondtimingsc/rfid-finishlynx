# Raceclock wire protocol

Electro Numerics 6-digit Raceclock, "rd" (remote display) mode.

There is no vendor documentation for this. The protocol below was recovered by
hand-executing the edit opcodes in FinishLynx's `Electro-led.lss` scoreboard script,
then confirmed against the physical clock. Both the derivation and the confirmation are
recorded here so a future reader can check the work instead of trusting it.

---

## 1. Physical layer

```
9600 baud, 8 data bits, no parity, 1 stop bit
No flow control (no RTS/CTS, no DSR/DTR, no XON/XOFF)
```

Source: the header comment of `Electro-led.lss` (`; Defaults: 9600,8,N,1`) and the
FinishLynx config string `\Scoreboards\3\SerialPort:String,1=5,9600,8,N,1.0`, where the
leading `5` is the COM number. Confirmed working.

The clock is **write-only in normal use**. It sent nothing back during any test. The
driver still drains and reports the RX buffer after each command, so if a future unit
does talk, you will see it rather than silently discarding it.

## 2. Frames

Two frames exist. That's the entire protocol.

```
Init    b"c\r"                                    -- 0x63 0x0D
Data    b"c" + <exactly 6 ASCII chars> + b"\r\n"  -- 0x63 .. 0x0D 0x0A
```

- `c` (0x63) is the frame lead-in.
- The 6 payload characters map **left to right** onto the 6 digits.
- **A space (0x20) blanks that digit.** This is load-bearing — see §4.
- The payload is always exactly 6 characters. There is no short-frame form.

Init is emitted once by the script's `;;Initialization` section, which is two lines:
`\00c` (emits `c`) followed by `\00\0d` (emits CR). Sections concatenate, so the frame
on the wire is `c\r`.

## 3. Semantic formats

The three time modes are all the same 6-character frame; only the digit meaning differs.

| Mode | Payload | Display |
|---|---|---|
| Time of day | `HHMMSS` | `10:35:42` |
| Time stopped | `MMSShh` | `mm:ss.hh` (hundredths) |
| Time running | `MMSSt` + `' '` | `mm:ss.t` (tenths) |

The clock has fixed separator positions; you send digits only.

## 4. Derivation from the .lss script

FinishLynx hands the script a **12-character formatted time**, `HH:MM:SS.mmm`. The
script's job is to delete the separators. It does so with cursor/backspace opcodes
documented in the script header:

```
\Bn   delete n characters BEFORE the cursor
\Ln   move the cursor n positions LEFT
```

The format line `c%12.12s` produces `c` + 12 chars = 13 characters, cursor at the end
(position 14). Label the time characters p1..p12:

```
        1   2  3  4  5  6  7  8  9  10 11 12 13
        c   p1 p2 p3 p4 p5 p6 p7 p8 p9 p10 p11 p12
             H  H  :  M  M  :  S  S  .  m   m   m
```

### `;;TimeOfDay` — `\01\=0c%12.12s\B4\L2\B1\L2\B1`

(`\=0` sets register[0]=1, which throttles the section to once per second.)

```
start          c10:35:42.123      cursor 14
\B4  del 4     c10:35:42          cursor 10   -- kills ".123"
\L2            c10:35:42          cursor 8
\B1  del ':'   c10:3542           cursor 7
\L2            c10:3542           cursor 5
\B1  del ':'   c103542            cursor 4
                ^^^^^^ HHMMSS
```

### `;;TimeStopped` — `\01c%12.12s\B1\L2\B1\L2\B1\L2\B3`

```
start          c p1..p12          cursor 14
\B1            c p1..p11          cursor 13   -- drops p12 (thousandths)
\L2                               cursor 11
\B1            drops p9  '.'      cursor 10
\L2                               cursor 8
\B1            drops p6  ':'      cursor 7
\L2                               cursor 5
\B3            drops p1 p2 p3     -- the "HH:" hours field
result         c p4 p5 p7 p8 p10 p11
                 M  M  S  S  m    m   = MMSShh
```

### `;;TimeRunning` — `\01c%12.12s\B2\L1\B1\L2\B1\L2\B3`

```
start          c p1..p12          cursor 14
\B2            c p1..p10          cursor 12   -- drops p11 p12
\L1                               cursor 11
\B1            drops p9  '.'      cursor 10
\L2                               cursor 8
\B1            drops p6  ':'      cursor 7
\L2                               cursor 5
\B3            drops p1 p2 p3     -- hours
result         c p4 p5 p7 p8 p10
                 M  M  S  S  t    = MMSSt  -- only FIVE digits
```

**This is the subtle part.** TimeRunning yields 5 digits, not 6. The section's second
line, `\00 \0d\0a`, emits a literal **space** before CRLF — supplying the missing sixth
character. So the frame is still `c` + 6 chars + CRLF, with the hundredths position
blanked while the clock is running. The invariant holds across all three modes, which is
why `frame()` in the driver can enforce a flat 6-character payload.

## 5. Confirmation on hardware

Performed 2026-09-07 against the physical clock on COM5.

| Sent | Observed |
|---|---|
| `63 38 38 38 38 38 38 0d 0a` (`c888888`) | all six digits fully lit |
| `c000000` … `c999999` | each digit 0–9 rendered correctly, all positions |
| `c8     ` → `c     8` sweep | single lit digit walked left→right across all 6 positions |
| 10 Hz `run` for 15 s, ending `c001500` | smooth count-up, final read 00:15.00 |

The position sweep is the important one: it proves the left-to-right mapping and that
spaces blank rather than zero-fill.

## 6. Reproducing the derivation

The opcode reference lives in the comment header of `lss/Electro-led.lss` (lines 38–90),
which documents the full embedded command set — registers, arithmetic, checksums,
output/scan formatting, and the cursor ops used above. If a future model needs to decode
a *different* Lynx scoreboard script (there are ~150 in `C:\Lynx\*.lss` for other
brands), that header is the Rosetta stone and the method above is the recipe: take the
12-char `HH:MM:SS.mmm` input, apply the opcodes literally, read off the surviving
characters.
