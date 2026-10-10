# FinishLynx scoreboard scripts

Copied from `C:\Lynx\` on the working machine. These are the **source of truth** for the
clock's protocol — `PROTOCOL.md` was derived from them by hand.

| File | Notes |
|---|---|
| `Electro-led.lss` | **Stock script, use this one.** Dated 2006-05-19. Drives the 6-digit LED raceclock. Its comment header (lines 38–90) documents the entire Lynx scoreboard-script opcode set. |
| `Electro-led_Todd.lss` | Local edit. Only difference from stock is the TimeRunning trailer line code `\00` → `\01`. That line has no `%s`, so no variable is ever substituted and **the bytes on the wire are identical to stock.** Functionally the same file. |
| `Electro-flip.lss` | For the *flip-disc* Electro Numerics display, not the LED one. Included for completeness only — do not use it with this clock. |

To use: copy into the FinishLynx program directory (`C:\Lynx\`), then point a scoreboard
at it. See `../reference/finishlynx-scoreboard3.txt` for the full known-good settings.

The clock does not require FinishLynx. `../tool/raceclock.py` speaks the protocol
directly and is the better option for testing and for anything non-race.
