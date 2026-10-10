#!/usr/bin/env python3
"""End-to-end LoRa link test between the finish line and a remote unit.

Run on both machines. One side listens, the other sends; each read is
checked for checksum validity, correct reader ID and round-trip latency.

    # finish line (this laptop)
    python test_lora_link.py listen

    # remote unit (the Pi)
    python test_lora_link.py send --reader-id SPLIT1 --count 10

Use --port to override the link from config.yaml (device path or host:port).

The "latency" printed by listen is the receiver's clock minus the sender's
stamp: with two unsynced clocks it is clock difference plus delay. For real
delays, calibrate on the bench with both gateways on one machine:

    # both gateways on this PC's switch: A = finish's, B = a station's
    python test_lora_link.py bench --a 192.168.0.60:8886 --b 192.168.0.61:8886

One clock times every frame, so each direction is measured on its own: the
GUN broadcast's one-way delay (what lora.air_latency_sec stands for) and
PING frames both ways (how symmetric the console's clock check can assume
the link is). Stop rfid-remote / the console first: a gateway serves one
TCP client.
"""
import argparse
import dataclasses
import statistics
import sys
import threading
import time
from datetime import datetime

from rfid_finishlynx.config import load_config
from rfid_finishlynx.remote_protocol import (
    ManagementMessage, MessageType, format_gun, format_ping, restamp_line,
)
from rfid_finishlynx.transport import (
    LoRaTransport, format_lora_message, parse_any_lora_message,
    parse_tcp_endpoint,
)


def build_transport(args, on_receive=None):
    config = load_config(args.config).lora
    if args.port:
        config.port = args.port
    config.enabled = True

    kind = "gateway" if parse_tcp_endpoint(config.port) else "serial"
    print(f"link: {config.port} ({kind})")

    transport = LoRaTransport(config, on_receive=on_receive)
    if not transport.connect():
        sys.exit(f"FAIL: could not open LoRa link at {config.port}")
    return transport


def listen(args):
    received = []

    def on_receive(data: bytes):
        arrived = datetime.now()
        raw = data.decode("ascii", "replace").strip()
        message = parse_any_lora_message(data)
        if message is None:
            print(f"  BAD  {raw}   (checksum or format rejected)")
            received.append(False)
            return
        bib = getattr(message, "bib", None)
        if bib is None:
            print(f"  ---  {raw}   (management message)")
            return
        latency = (arrived - message.timestamp).total_seconds() * 1000
        seq = "" if message.seq is None else f" seq={message.seq:02X}"
        print(f"  OK   bib={bib:<6} from={message.reader_id:<8}{seq} "
              f"latency={latency:7.1f} ms")
        received.append(True)

    transport = build_transport(args, on_receive=on_receive)
    print(f"listening for {args.timeout:.0f}s -- Ctrl-C to stop early\n")
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    transport.disconnect()

    good = sum(1 for r in received if r)
    print(f"\n{good}/{len(received)} messages parsed cleanly")
    return 0 if good and good == len(received) else 1


def send(args):
    transport = build_transport(args)
    print(f"sending {args.count} reads as {args.reader_id}\n")
    sent = 0
    for i in range(args.count):
        bib = f"{100 + i}"
        message = format_lora_message(bib, datetime.now(), args.reader_id, seq=i % 256)
        if transport.send(message):
            print(f"  sent bib={bib} seq={i % 256:02X}")
            sent += 1
        else:
            print(f"  FAIL bib={bib} -- transport rejected the write")
        time.sleep(args.interval)
    transport.disconnect()

    print(f"\n{sent}/{args.count} transmitted")
    return 0 if sent == args.count else 1


def bench_report(delays, sizes, kbps):
    """Lines summarising one-way delays (seconds) per (kind, direction)."""
    lines = []
    for (kind, way), values in delays.items():
        bits_ms = sizes[kind] * 8 / kbps
        if not values:
            lines.append(f"{kind:<4} {way}  no frames arrived")
            continue
        lines.append(f"{kind:<4} {way}  n={len(values):<3} min {min(values) * 1000:6.0f} ms  "
                     f"median {statistics.median(values) * 1000:6.0f} ms  max {max(values) * 1000:6.0f} ms"
                     f"   ({sizes[kind]} bytes = {bits_ms:.0f} ms of bits at {kbps} kbps)")
    gun = delays.get(("GUN", "A->B"))
    if gun:
        lines.append(f"\nlora.air_latency_sec for the stations: {min(gun):.2f} "
                     f"(the fastest GUN: a station keeps its fastest sample; default 0.3)")
    ab, ba = delays.get(("PING", "A->B")), delays.get(("PING", "B->A"))
    if ab and ba:
        asym = min(ab) - min(ba)
        lines.append(f"PING asymmetry A->B minus B->A: {asym * 1000:+.0f} ms, so a clock check "
                     f"reads up to {abs(asym) * 500:.0f} ms off")
    return lines


def bench(args):
    config = load_config(args.config).lora
    config.enabled = True
    delays = {("GUN", "A->B"): [], ("PING", "A->B"): [], ("PING", "B->A"): []}
    arrived = threading.Event()

    def receiver(way):
        def on_receive(data: bytes):
            now = time.time()
            msg = parse_any_lora_message(data)
            if not isinstance(msg, ManagementMessage):
                return
            if msg.msg_type == MessageType.GUN and msg.fields.get("sent_at"):
                delays[("GUN", way)].append(now - msg.fields["sent_at"])
            elif msg.msg_type == MessageType.PING:
                delays[("PING", way)].append(now - msg.fields["t1"])
            else:
                return
            arrived.set()
        return on_receive

    links = {}
    for name, port, way in (("A", args.a, "B->A"), ("B", args.b, "A->B")):
        cfg = dataclasses.replace(config, port=port)
        links[name] = LoRaTransport(cfg, on_receive=receiver(way))
        if not links[name].connect():
            sys.exit(f"FAIL: could not open gateway {name} at {port}")
    print(f"A = {args.a}, B = {args.b}; {args.count} rounds of GUN A->B, PING A->B, PING B->A\n")

    try:
        for i in range(args.count):
            for kind, sender in (("GUN", "A"), ("PING", "A"), ("PING", "B")):
                arrived.clear()
                frame = (format_gun(time.time() - 60, "12", sent_at=0.0) if kind == "GUN"
                         else format_ping("BENCH", i % 256))
                links[sender].send(frame)
                ok = arrived.wait(args.timeout)
                print(f"  round {i + 1}: {kind} from {sender} {'ok' if ok else 'LOST'}")
    except KeyboardInterrupt:
        pass
    for link in links.values():
        link.disconnect()

    sizes = {"GUN": len(restamp_line(format_gun(time.time() - 60, "12", sent_at=0.0), time.time())),
             "PING": len(format_ping("BENCH", 0))}
    print()
    for line in bench_report(delays, sizes, args.air_kbps):
        print(line)
    return 0 if all(delays.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=["listen", "send", "bench"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", help="override LoRa link (device path or host:port)")
    parser.add_argument("--reader-id", default="SPLIT1")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between sends (default 2.0)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="listen duration in seconds (default 60)")
    parser.add_argument("--a", default="192.168.0.60:8886", help="bench: the finish's gateway")
    parser.add_argument("--b", default="192.168.0.61:8886", help="bench: a station's gateway")
    parser.add_argument("--air-kbps", type=float, default=2.4, help="bench: radio air rate")
    args = parser.parse_args()

    if args.role == "bench":
        args.timeout = args.timeout or 20.0      # per frame
        return bench(args)
    args.timeout = args.timeout or 60.0
    return listen(args) if args.role == "listen" else send(args)


if __name__ == "__main__":
    sys.exit(main())
