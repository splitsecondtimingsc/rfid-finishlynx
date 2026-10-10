#!/usr/bin/env python3
"""
Electro-Numerics Raceclock driver (6-digit LED, "rd" remote-display mode).

Protocol reverse-engineered from FinishLynx C:\\Lynx\\Electro-led.lss, cross-checked
against C:\\Lynx\\Lynx.cfg scoreboard 3 ("Timing_Clock", Electro-led_Todd.lss,
SerialPort 5,9600,8,N,1.0).

Wire format -- 9600 8N1, no flow control:

    init frame   b"c\\r"
    data frame   b"c" + <6 ASCII chars> + b"\\r\\n"

The 6 payload characters map left-to-right onto the 6 digits. The .lss script
builds them by taking FinishLynx's 12-char formatted time "HH:MM:SS.mmm" and
deleting the separators with cursor/backspace ops:

    TimeOfDay  \\B4 \\L2 \\B1 \\L2 \\B1        -> HHMMSS   (drops ".mmm", both ':')
    TimeStopped \\B1 \\L2\\B1 \\L2\\B1 \\L2\\B3 -> MMSShh   (mm:ss.hh)
    TimeRunning \\B2 \\L1\\B1 \\L2\\B1 \\L2\\B3 -> MMSSt    (mm:ss.t, + ' ' pad)

TimeRunning yields only 5 digits, and the script's trailer line "\\00 \\0d\\0a"
supplies a trailing space -- so every frame on the wire is exactly 6 payload
characters. That invariant is enforced here in frame().
"""

import argparse
import sys
import time

import serial

BAUD = 9600
WIDTH = 6

INIT = b"c\r"


def frame(payload):
    """Build one display frame. Payload is right-justified, space-padded, to 6 chars."""
    s = str(payload)
    if len(s) > WIDTH:
        raise ValueError("payload %r is %d chars, max %d" % (s, len(s), WIDTH))
    return b"c" + s.rjust(WIDTH).encode("ascii") + b"\r\n"


def open_port(port):
    return serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
        write_timeout=2.0,
        rtscts=False,
        dsrdtr=False,
        xonxoff=False,
    )


def send(ser, data, echo=True):
    ser.write(data)
    ser.flush()
    if echo:
        print("TX %-24s %s" % (data.decode("ascii", "replace").replace("\r", "\\r").replace("\n", "\\n"),
                               data.hex(" ")))


def drain(ser, label="RX"):
    """The clock is display-only in rd mode, but report anything it sends back."""
    time.sleep(0.15)
    n = ser.in_waiting
    if n:
        data = ser.read(n)
        print("%s %s  %r" % (label, data.hex(" "), data))
        return data
    return b""


# ---------------------------------------------------------------- commands

def cmd_init(ser, args):
    send(ser, INIT)
    drain(ser)


def cmd_show(ser, args):
    if args.init:
        send(ser, INIT)
        time.sleep(0.1)
    send(ser, frame(args.text))
    drain(ser)
    if args.hold:
        print("holding %.1fs (refreshing)..." % args.hold)
        end = time.time() + args.hold
        while time.time() < end:
            ser.write(frame(args.text))
            ser.flush()
            time.sleep(0.2)


def cmd_test(ser, args):
    """Walk every segment/digit position so all 6 digits can be eyeballed."""
    send(ser, INIT)
    time.sleep(0.2)

    print("\n-- all digits 0-9 --")
    for d in range(10):
        send(ser, frame(str(d) * WIDTH))
        time.sleep(0.6)

    print("\n-- position walk (single digit sweeps left->right) --")
    for pos in range(WIDTH):
        s = " " * pos + "8" + " " * (WIDTH - pos - 1)
        send(ser, frame(s))
        time.sleep(0.5)

    print("\n-- ramp --")
    send(ser, frame("123456"))
    time.sleep(1.0)
    send(ser, frame("000000"))
    drain(ser)


def cmd_tod(ser, args):
    """Time of day, HHMMSS -- mirrors the .lss ;;TimeOfDay section."""
    send(ser, INIT)
    time.sleep(0.2)
    print("Sending time of day. Ctrl-C to stop.")
    last = None
    try:
        while True:
            t = time.localtime()
            hh = t.tm_hour if args.hour24 else (t.tm_hour % 12 or 12)
            s = "%02d%02d%02d" % (hh, t.tm_min, t.tm_sec)
            if s != last:
                send(ser, frame(s), echo=args.verbose)
                last = s
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_run(ser, args):
    """Count up like a running race clock: MMSSt at 10 Hz, per ;;TimeRunning."""
    send(ser, INIT)
    time.sleep(0.2)
    print("Counting up for %.1fs. Ctrl-C to stop." % args.seconds)
    t0 = time.time()
    try:
        while True:
            el = time.time() - t0
            if el >= args.seconds:
                break
            mm = int(el // 60)
            ss = int(el % 60)
            tenth = int((el * 10) % 10)
            # 5 significant digits + trailing space, exactly as the script emits
            send(ser, frame("%02d%02d%d " % (mm, ss, tenth)), echo=args.verbose)
            time.sleep(0.1)
        # final "stopped" frame carries hundredths: MMSShh
        el = args.seconds
        mm, ss = int(el // 60), int(el % 60)
        hh = int((el * 100) % 100)
        send(ser, frame("%02d%02d%02d" % (mm, ss, hh)))
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_raw(ser, args):
    data = bytes.fromhex(args.hex.replace(",", " "))
    send(ser, data)
    drain(ser)


def cmd_blank(ser, args):
    send(ser, INIT)
    time.sleep(0.1)
    send(ser, frame(""))
    drain(ser)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="COM5", help="serial port (default COM5)")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every frame")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="send the initialization frame")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("show", help="display up to 6 characters")
    sp.add_argument("text")
    sp.add_argument("--hold", type=float, default=0.0, help="keep refreshing for N seconds")
    sp.add_argument("--init", action="store_true", help="send init frame first")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("test", help="digit + position walk to verify the display")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("tod", help="continuously display time of day")
    sp.add_argument("--hour24", action="store_true")
    sp.set_defaults(func=cmd_tod)

    sp = sub.add_parser("run", help="simulate a running race clock")
    sp.add_argument("--seconds", type=float, default=30.0)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("raw", help="send raw hex bytes")
    sp.add_argument("hex")
    sp.set_defaults(func=cmd_raw)

    sp = sub.add_parser("blank", help="blank the display")
    sp.set_defaults(func=cmd_blank)

    args = p.parse_args()

    try:
        ser = open_port(args.port)
    except serial.SerialException as e:
        print("Cannot open %s: %s" % (args.port, e), file=sys.stderr)
        return 1

    print("Opened %s @ %d 8N1" % (args.port, BAUD))
    with ser:
        args.func(ser, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
