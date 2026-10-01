#!/usr/bin/env python3
"""
LED clock control - protocol per "Comminucation Protocol.pdf".

Link:   USB-SERIAL CH340 (VID:PID 1A86:7523) -> COM10 @ 9600 8N1

Command frame (PC -> clock)
    0x1F  '0'(addr)  <cmd>  [ascii data]  0x09
    A leading 5x 0x00 preamble is shown in the PDF's frame diagram and is
    accepted by the clock, but is not required.

Response frame (clock -> PC), decoded from live traffic
    0xF1  0x1F 0x1F  '2'(addr)  <cmd>  <ascii data>  0x09  <checksum>
    checksum = low byte of the sum of addr + cmd + data
               (the 0x1F framing bytes and the 0x09 terminator are excluded)

Verified on the hardware 2026-09-07 - the PDF's "simple commands" do NOT all behave
as labelled:
    countup   T1<HHMMSS>  WORKS. Sets the value AND starts it running in one shot.
                          Also the way to RESUME: re-send T1 with the frozen value.
    stop      A P         WORKS - freezes the running timer.
    reset     A O         Does NOT zero the timer; it just freezes it, same as A P.
                          To zero, send `countup 000000`.
    start     A Q         Does NOTHING after a stop. Use T1 to resume instead.
    clock     A K         WORKS - returns to time-of-day mode, time is preserved.
None of the set/control commands return a response; only 'R' replies. Confirm any
state change by following it with a read.

Usage
    python ledclock.py read                 # read current timer/time value
    python ledclock.py monitor [seconds]    # poll once a second
    python ledclock.py settime now|HHMMSS   # set time / switch to time mode
    python ledclock.py show HHMMSS          # stop timer and display value
    python ledclock.py countup [HHMMSS]     # second countup   (T1, default 000000)
    python ledclock.py countdown HHMMSS     # second countdown (T2)
    python ledclock.py sw-countup [HHMMSS]  # stopwatch countup   (T3)
    python ledclock.py sw-countdown HHMMSS  # stopwatch countdown (T4)
    python ledclock.py start|stop|reset     # timer control
    python ledclock.py clock|power|brightness
    python ledclock.py second-up|second-down|stopwatch-up
    python ledclock.py beep
    python ledclock.py raw "1F 30 52 09"    # send arbitrary hex
"""
import sys, time, datetime
import serial

PORT, BAUD = "COM10", 9600
SOH, ETX, ADDR = 0x1F, 0x09, ord("0")


# --------------------------------------------------------------- framing --
def frame(cmd: str, data: str = "", preamble: bool = False) -> bytes:
    body = bytes([SOH, ADDR]) + cmd.encode("ascii") + data.encode("ascii") + bytes([ETX])
    return (b"\x00" * 5 + body) if preamble else body


def checksum(payload: bytes) -> int:
    """Low byte of the sum of addr + cmd + data."""
    return sum(payload) & 0xFF


def parse_response(raw: bytes):
    """Return (addr, cmd, data, ok) or None if no complete frame present."""
    if not raw:
        return None
    i = raw.rfind(b"\x1f")                  # start at the last SOH
    if i < 0 or ETX not in raw[i:]:
        return None
    j = raw.index(ETX, i)
    payload = raw[i + 1:j]                  # addr + cmd + data
    if len(payload) < 2:
        return None
    got = raw[j + 1] if len(raw) > j + 1 else None
    want = checksum(payload)
    return (chr(payload[0]), chr(payload[1]), payload[2:].decode("ascii", "replace"),
            got == want, got, want)


def hx(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b) if b else "(nothing)"


# ------------------------------------------------------------- transport --
class Clock:
    def __init__(self, port=PORT, baud=BAUD):
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                 timeout=0.3, write_timeout=2)

    def close(self):
        self.ser.close()

    def send(self, pkt: bytes, wait=0.6) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        self.ser.flush()
        end, buf = time.time() + wait, bytearray()
        while time.time() < end:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n)
                end = time.time() + 0.15      # let the frame finish arriving
            else:
                time.sleep(0.02)
        return bytes(buf)

    def read_timer(self):
        raw = self.send(frame("R"))
        return raw, parse_response(raw)


# -------------------------------------------------------------- commands --
SIMPLE = {"power": "E", "start": "Q", "stop": "P", "reset": "O",
          "brightness": "T", "clock": "K", "second-up": "L",
          "second-down": "M", "stopwatch-up": "N"}


def need_hhmmss(v):
    if not (len(v) == 6 and v.isdigit()):
        sys.exit(f"error: expected 6 digits HHMMSS, got {v!r}")
    return v


def fmt(data: str) -> str:
    return f"{data[0:2]}:{data[2:4]}:{data[4:6]}" if len(data) == 6 else data


def show(raw, parsed, label="clock"):
    print(f"  rx  : {hx(raw)}")
    if not parsed:
        print("  ->  no valid frame decoded")
        return
    addr, cmd, data, ok, got, want = parsed
    mark = "OK" if ok else f"BAD (got {got:02X}, want {want:02X})" if got is not None else "missing"
    print(f"  ->  addr='{addr}' cmd='{cmd}' data='{data}'  value={fmt(data)}  checksum={mark}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    op = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    c = Clock()
    try:
        if op == "read":
            raw, p = c.read_timer()
            show(raw, p)

        elif op == "monitor":
            secs = int(arg) if arg else 10
            print(f"Polling {PORT} for {secs}s (PC time on the left):")
            for _ in range(secs):
                raw, p = c.read_timer()
                pc = datetime.datetime.now().strftime("%H:%M:%S")
                val = fmt(p[2]) if p else "?"
                ck = "" if (p and p[3]) else "  <checksum!>"
                print(f"  PC {pc}   clock {val}{ck}")
                time.sleep(1)

        elif op == "settime":
            v = datetime.datetime.now().strftime("%H%M%S") if (arg or "").lower() == "now" \
                else need_hhmmss(arg or "")
            print(f"Setting clock to {fmt(v)}")
            show(c.send(frame("M", v)), None)
            time.sleep(0.4)
            raw, p = c.read_timer()
            show(raw, p)

        elif op in ("show", "countup", "countdown", "sw-countup", "sw-countdown"):
            # 'T' + mode digit + HHMMSS.  Mode digits per the PDF command table:
            #   1 second countup   2 second countdown
            #   3 stopwatch countup   4 stopwatch countdown
            modes = {"countup": "1", "countdown": "2",
                     "sw-countup": "3", "sw-countdown": "4"}
            if op == "show":
                v = need_hhmmss(arg or "")
                pkt = frame("D", v)
            else:
                v = need_hhmmss(arg) if arg else "000000"
                pkt = frame("T", modes[op] + v)
            print(f"{op} {fmt(v)}   tx: {hx(pkt)}")
            show(c.send(pkt), None)

        elif op in SIMPLE:
            pkt = frame("A", SIMPLE[op])
            print(f"{op}   tx: {hx(pkt)}")
            show(c.send(pkt), None)

        elif op == "beep":
            pkt = frame("P", "1")
            print(f"beep   tx: {hx(pkt)}")
            show(c.send(pkt), None)

        elif op == "raw":
            pkt = bytes.fromhex((arg or "").replace(" ", ""))
            print(f"raw    tx: {hx(pkt)}")
            show(c.send(pkt), parse_response(c.send(pkt, 0.3)) if False else None)

        else:
            sys.exit(f"unknown command {op!r}\n{__doc__}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
