# RadioLynx receiver as the middleware's gun

> (2026-09-27) The code this describes (`start_input.py`, `lif_compare.py`, `tests/test_start_input.py`) was in the
> retired `rfid_timing` fork and never landed in this repo. Treat it as research notes.

RadioLynx is a rebadged **Microgate LinkGate** system: EncRadio transmitter
(starter's end, start sensor on green/black banana plugs) and DecRadio
receiver (timing end). What is publicly known:

| Fact | Source |
|---|---|
| Receiver output is **serial data**, not a contact closure; FinishLynx treats it as a "Serial Wireless" plug-in device | RadioLynx QSG (`RadioLynx_qsg.pdf`), parts 2B/3B |
| Camera-back connection: **DB9 serial** to the camera's SERIAL port + **red banana = 12 V DC 0.1 A** from the camera | QSG part 2B |
| The camera configures that port at cold boot as **9600 8N1** (`0x15 (2,3,9600,8,0,10)`) | `docs/etherlynx/PROTOCOL.md`, "RadioLynx reconnect behavior" |
| Signal type is a hex nibble: **0 = START, 1-E = LAP, F = STOP**; channel is a DIP setting (4 switches, must match both units); FinishLynx "Id 000" = channel 0 + type STA | Microgate REI2 manual figure; QSG |
| Impulses are **time-stamped by the transmitter** (±0.4 ms, 32768 Hz RTC), sent multiple times, and can be re-sent minutes or hours later | `RadioLynx_Datasheet.pdf` |
| FinishLynx shows "number of attempts the latest signal was sent before it was received" | finishlynx.com support pages |
| DecRadio has a 5-pole Nucletron connector; the Lynx cable adapts it to DB9 + power | Microgate REI2 manual |

**Not known: the byte format on the DB9.** No public document has it and
the EtherLynx captures only recorded the camera's port setup, never the
receiver's bytes (starts were never fired during those sessions).

## Wiring for parallel operation

The goal is FinishLynx keeping its own gun while the middleware hears the
same one. Two options:

1. **Y-tap the receiver's TX line** (true parallel, one receiver). RS-232
   receivers are high-impedance, so one DecRadio TX pin can feed the
   camera's RX *and* a USB-RS232 adapter's RX over a short run. Cable:
   DB9 breakout in the receiver cable; wire DecRadio TX (camera-side RX)
   and GND to the PC adapter's RX (pin 2) and GND (pin 5). Nothing from
   the PC side is driven back into the receiver. Camera keeps supplying
   the 12 V.
2. **Second receiver on the PC** (Lynx sells DecRadios; one transmitter
   feeds any number). Needs its own 12 V 0.1 A supply on the red banana
   and a USB-RS232 adapter (DB9). If the adapter shows nothing, try a
   null-modem (the receiver may be wired as DTE).

Either way, the middleware side is a plain USB-serial device. Set
`start_input.port` explicitly (`COM5`, `/dev/ttyUSB1`) when more than one
non-clock USB-serial device is attached; `auto` picks the first one that is
not an LED clock chip.

## How the middleware uses it today (undecoded)

`rfid_finishlynx/start_input.py`:

- opens the port 9600 8N1, reconnects forever;
- splits the byte stream into **bursts** (silence > `burst_gap_sec`);
- each burst = one signal, timestamped at its **first byte** on the host
  clock, minus `start_input.latency_sec`;
- journals every burst raw (`start-signals.log`: ISO time, hex, gun
  estimate) - this is the capture that will let us decode the format;
- routes it to `RaceManager.record_start_signal`: the race checking in,
  else the first race in schedule order without a gun, else a
  re-fire candidate on the running race. Every signal is listed in the
  Gun picker regardless.

Limitations until decoded:

- **Latency.** The radio frame takes roughly 0.5-1.5 s to transmit and
  decode before the first byte appears. Calibrate: fire a test start with
  FinishLynx armed on the same race, evaluate in FinishLynx, press
  **vs FL** on the pane - the "gun delta" is the correction; put it in
  `latency_sec`. Expect it to be stable to a few ms for a given channel.
- **Repeats.** A signal re-sent later (transmitter REPEAT, or automatic
  retries after interference) shows up as another candidate on the
  running race. The first one is the gun; the picker shows the rest.
- **Type/channel filtering.** LAP/STOP signals or another channel's
  transmitter look the same as a start. Keep the transmitter on type STA
  and a channel nobody else uses, as FinishLynx setups already require.

## Decoding session (one afternoon, no camera needed)

1. Receiver on the PC (either wiring above), `start_input.enabled: true`,
   console open, `tail -f start-signals.log`.
2. Fire the transmitter's test button (top-left) 5 times, ~10 s apart.
   Note each press to the second. Expect one journal line per press.
3. Then: press REPEAT (if the unit has it) a minute later; switch type to
   LAP (1) and fire; switch channel and fire; let the batteries-low
   triple beep happen if convenient.
4. Diff the hex across lines. Expect fixed framing, a channel nibble, the
   type nibble, a counter/attempt field, a checksum, and a time field
   that *grows* between a press and its REPEAT by the elapsed seconds
   (that field is `age_sec`; its resolution should be 1/10000 s or
   1/1000 s given the ±0.4 ms accuracy).
5. Implement `decode_burst()` in `start_input.py` returning
   `{"age_sec", "channel", "signal_type"}`; add the captured lines as
   test vectors in `tests/test_start_input.py`. Nothing else changes:
   `StartSignal.gun` prefers the decoded age, `source` names the type.
6. Set `latency_sec` back to 0 once decoded.

If the port stays silent: wrong pins (try null-modem), receiver unpowered
(red banana), or baud. Try 1200 and 4800 too - Microgate's radio modem
side runs 1200 baud and the DecRadio manual lists 300-4800 for the module.
