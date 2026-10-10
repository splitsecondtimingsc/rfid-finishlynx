#!/usr/bin/env python3
"""Unattended range test for the LoRa link.

The remote station beacons continuously from the moment it is powered on,
so it can be carried to a new distance, plugged in, and left alone - no
network, no keyboard, nothing to start by hand. The finish line logs what
arrives; comparing the two logs afterwards gives the delivery rate at
that distance.

    # finish line, leave running
    python range_test.py listen

    # afterwards, from the recorded log - nothing needs to be running
    python range_test.py analyze

    # remote unit - started automatically at boot by rfid-range-beacon.service
    python range_test.py beacon

Each power-on is a separate run, tagged with a run id, so several
distances can be tested one after another and told apart later. The
remote records every beacon it sends in range-test-sent.log; the finish
line records every one that arrives in range-test-heard.log.
"""
import argparse
import re
import signal
import sys
import threading
import time
from datetime import datetime

from rfid_finishlynx.config import load_config
from rfid_finishlynx.transport import LoRaTransport, calculate_checksum

def stop_signal() -> threading.Event:
    """An event set by Ctrl-C or by `systemctl stop`.

    Handling SIGTERM too means a service shutdown still writes its summary
    instead of being cut off mid-run.
    """
    stopping = threading.Event()

    def handler(signum, frame):
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)
    return stopping


SENT_LOG = "range-test-sent.log"
HEARD_LOG = "range-test-heard.log"

# Full dates, so a session that runs past midnight or spans several days
# still sorts and reads correctly.
STAMP = "%Y-%m-%d %H:%M:%S.%f"

LOG_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+|\d{2}:\d{2}:\d{2}\.\d+)"
    r"\s+run=(?P<run>\S+)\s+seq=(?P<seq>\d+)"
)


def parse_log_line(line: str):
    """Return (arrival, run, seq) for a log line, else None.

    Lines written before dates were added still parse; their arrival time
    is reported as unknown rather than guessed at.
    """
    match = LOG_LINE.match(line.strip())
    if not match:
        return None
    stamp = match.group("stamp")
    try:
        arrival = datetime.strptime(stamp, STAMP) if len(stamp) > 15 else None
    except ValueError:
        arrival = None
    return arrival, match.group("run"), int(match.group("seq"))


def summarise(runs) -> list:
    """Per-run report from {run_id: [(arrival, seq), ...]}."""
    report = []
    for run in sorted(runs):
        entries = runs[run]
        seqs = {seq for _, seq in entries}
        # Measure only the stretch actually observed. A listener started
        # part-way through a run has not "lost" the beacons sent before it
        # was watching, and counting those would understate the link.
        lowest, highest = min(seqs), max(seqs)
        expected = highest - lowest + 1
        window = "" if lowest == 0 else f" (from seq {lowest})"
        report.append(f"run {run}: heard {len(seqs)}/{expected} beacons "
                      f"({100.0 * len(seqs) / expected:.0f}%){window}")

        arrivals = sorted(arrival for arrival, _ in entries if arrival)
        if arrivals:
            report.append(f"  first {arrivals[0]:%Y-%m-%d %H:%M:%S}"
                          f"   last {arrivals[-1]:%Y-%m-%d %H:%M:%S}")
            if len(arrivals) > 1:
                gaps = [(later - earlier).total_seconds()
                        for earlier, later in zip(arrivals, arrivals[1:])]
                report.append(f"  longest silence {max(gaps):.0f}s")

        missing = sorted(set(range(lowest, highest + 1)) - seqs)
        if missing:
            shown = ", ".join(str(m) for m in missing[:20])
            report.append(f"  missed {len(missing)}: {shown}"
                          f"{' ...' if len(missing) > 20 else ''}")
    return report


def format_beacon(run_id: str, seq: int, timestamp: datetime) -> bytes:
    """$RANGE,<run_id>,<seq>,<HH:MM:SS.mmm>*<checksum>"""
    time_str = timestamp.strftime("%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}"
    payload = f"RANGE,{run_id},{seq},{time_str}"
    return f"${payload}*{calculate_checksum(payload)}\n".encode("ascii")


def parse_beacon(line: bytes):
    """Return (run_id, seq, time_str) for a valid beacon, else None."""
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith("$RANGE,") or "*" not in text:
        return None

    payload_end = text.rfind("*")
    payload = text[1:payload_end]
    if text[payload_end + 1:].upper() != calculate_checksum(payload):
        return None

    parts = payload.split(",")
    if len(parts) != 4:
        return None
    try:
        return parts[1], int(parts[2]), parts[3]
    except ValueError:
        return None


def open_link(args):
    config = load_config(args.config).lora
    if args.port:
        config.port = args.port
    config.enabled = True

    transport = LoRaTransport(config, on_receive=args.on_receive)
    transport.connect()  # keeps retrying in the background if the radio is late
    return transport, config


RUN_COUNTER = "range-test-run.txt"


def next_run_id() -> str:
    """A new id for every power-on.

    The Pi has no real-time clock, so with no network its clock resumes
    near whatever it was at shutdown - useless for telling runs apart.
    A counter on disk does not care what time it is.
    """
    try:
        with open(RUN_COUNTER) as handle:
            number = int(handle.read().strip()) + 1
    except (OSError, ValueError):
        number = 1
    try:
        with open(RUN_COUNTER, "w") as handle:
            handle.write(str(number))
    except OSError:
        pass
    return f"r{number}"


def cmd_beacon(args):
    args.on_receive = None
    transport, config = open_link(args)
    run = args.run_id or next_run_id()

    print(f"run {run}: beaconing every {args.interval:.0f}s for {args.duration / 60:.0f} min "
          f"via {config.port}", flush=True)

    deadline = time.monotonic() + args.duration
    stopping = stop_signal()
    seq = 0
    with open(args.log, "a", buffering=1) as log:
        log.write(f"# run {run} started {datetime.now():%Y-%m-%d %H:%M:%S} "
                  f"link={config.port}\n")
        while time.monotonic() < deadline and not stopping.is_set():
            now = datetime.now()
            transport.send(format_beacon(run, seq, now))
            log.write(f"{now:{STAMP}} run={run} seq={seq}\n")
            print(f"  sent run={run} seq={seq}", flush=True)
            seq += 1
            stopping.wait(args.interval)
        log.write(f"# run {run} ended {datetime.now():%Y-%m-%d %H:%M:%S} sent={seq}\n")

    transport.disconnect()
    print(f"\nrun {run}: {seq} beacons sent", flush=True)
    return 0


def cmd_listen(args):
    heard = {}          # run_id -> [(arrival, seq), ...]
    log = open(args.log, "a", buffering=1)

    def on_receive(data: bytes):
        beacon = parse_beacon(data)
        if beacon is None:
            return
        run, seq, sent_at = beacon
        entries = heard.setdefault(run, [])
        seen = {existing for _, existing in entries}
        duplicate = seq in seen
        arrived = datetime.now()
        entries.append((arrived, seq))
        seen.add(seq)

        log.write(f"{arrived:{STAMP}} run={run} seq={seq} sent={sent_at}\n")
        expected = max(seen) - min(seen) + 1
        rate = 100.0 * len(seen) / expected
        print(f"  run={run} seq={seq:<4} sent={sent_at}  "
              f"heard {len(seen)}/{expected} ({rate:.0f}%)"
              f"{'  DUPLICATE' if duplicate else ''}", flush=True)

    args.on_receive = on_receive
    transport, config = open_link(args)
    print(f"listening on {config.port} -- Ctrl-C to stop\n", flush=True)

    stopping = stop_signal()
    while not stopping.wait(0.5):
        pass

    transport.disconnect()

    print("\n--- summary ---")
    if not heard:
        print("nothing heard")
        log.close()
        return 1

    report = summarise(heard)
    for line in report:
        print(line)
    # Keep the summary with the data, so closing this window loses nothing.
    log.write(f"# summary {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    for line in report:
        log.write(f"#   {line}\n")
    log.close()
    return 0


def cmd_analyze(args):
    """Report on a log written earlier, with nothing running."""
    runs = {}
    try:
        with open(args.log) as handle:
            for line in handle:
                if line.startswith("#"):
                    continue
                parsed = parse_log_line(line)
                if parsed:
                    arrival, run, seq = parsed
                    runs.setdefault(run, []).append((arrival, seq))
    except FileNotFoundError:
        print(f"No log yet at {args.log} -- run 'range_test.py listen' first.")
        return 1
    except OSError as e:
        print(f"cannot read {args.log}: {e}")
        return 1

    if not runs:
        print(f"no beacons recorded in {args.log}")
        return 1

    print(f"{args.log}:")
    for line in summarise(runs):
        print(line)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=["beacon", "listen", "analyze"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", help="override the LoRa link (device path or host:port)")
    parser.add_argument("--run-id", help="label for this run (default: a per-boot counter)")
    parser.add_argument("--interval", type=float, default=6.0,
                        help="seconds between beacons (default 6, above the radio's window)")
    parser.add_argument("--duration", type=float, default=3600.0,
                        help="seconds to keep beaconing (default 3600)")
    parser.add_argument("--log", help="log file (defaults per role)")
    args = parser.parse_args()

    if not args.log:
        args.log = SENT_LOG if args.role == "beacon" else HEARD_LOG

    return {"beacon": cmd_beacon, "listen": cmd_listen,
            "analyze": cmd_analyze}[args.role](args)


if __name__ == "__main__":
    sys.exit(main())
