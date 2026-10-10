#!/usr/bin/env python3
"""Find, adopt and verify the LoRa ethernet gateway at a station.

    python setup_gateway.py discover              # what is on the wire?
    python setup_gateway.py adopt                 # move it to the standard address
    python setup_gateway.py check                 # radio settings + link health
    python setup_gateway.py check --json > a.json # to compare against the other station

Run `adopt` once per station when the hardware is first cabled up; run
`check` at both ends whenever the link misbehaves. A radio parameter that
differs between the two stations produces no data and no error, so `check`
is the fastest way to rule that out.
"""
import argparse
import json
import sys
import time

from rfid_finishlynx.config import load_config
from rfid_finishlynx.gateway import (
    DATA_PORT, EBYTE_OUI, POWER_LEVELS, STANDARD_IP, EbyteGateway, GatewayError,
    arp_neighbours, compare_radio, data_port_open, discover, probe,
    unreachable_hint, wired_interfaces,
)
from rfid_finishlynx.transport import parse_tcp_endpoint

BASELINE_FILE = "gateway-baseline.json"


def configured_host(args) -> str:
    """The gateway address the application is pointed at."""
    if args.host:
        return args.host
    try:
        endpoint = parse_tcp_endpoint(load_config(args.config).lora.port)
    except (OSError, ValueError):
        endpoint = None
    return endpoint[0] if endpoint else STANDARD_IP


def find_gateways(args):
    """Look through the routing table, then directly on each wired port.

    A gateway on a foreign subnet - or one whose subnet a VPN has claimed -
    is unreachable by routing but still answers on the wire it is cabled to.
    """
    extra = [args.host] if args.host else None
    found = discover(extra=extra, username=args.username, password=args.password,
                     interface=args.interface)
    if found or args.interface:
        return found

    for interface in wired_interfaces():
        found = discover(extra=extra, username=args.username,
                         password=args.password, interface=interface)
        if found:
            return found
    return []


def cmd_discover(args):
    found = find_gateways(args)
    if found:
        for gateway in found:
            via = f"  (via {gateway['interface']})" if gateway["interface"] else ""
            print(f"  {gateway['ip']:<16} {gateway['model']}  "
                  f"fw={gateway['firmware']}  sn={gateway['serial']}  {gateway['mac']}{via}")
        print(f"\n{len(found)} gateway(s) responding.")
        return 0

    print("No gateway answered on any candidate address.")
    stranded = {ip: mac for ip, mac in arp_neighbours().items()
                if mac.startswith(EBYTE_OUI)}
    if stranded:
        # Seen at the link layer but not reachable over IP: a subnet mismatch.
        for ip, mac in stranded.items():
            print(f"\nAn Ebyte device ({mac}) is cabled up at {ip} but not reachable.")
            print(unreachable_hint(ip))
    else:
        print("No Ebyte device is visible on the wire either -- check power and cabling.")
    return 1


def cmd_adopt(args):
    found = find_gateways(args)
    if not found:
        print("No gateway found to adopt. Run 'discover' for details.")
        return 1
    if len(found) > 1:
        print("More than one gateway answered; pass --host to choose:")
        for gateway in found:
            print(f"  {gateway['ip']}  {gateway['model']}  sn={gateway['serial']}")
        return 1

    gateway_info = found[0]
    interface = gateway_info["interface"]
    target = args.ip
    print(f"Found {gateway_info['model']} at {gateway_info['ip']} (sn={gateway_info['serial']})")

    if gateway_info["ip"] == target:
        print(f"Already at {target}; nothing to change.")
    else:
        print(f"Moving it to {target} and rebooting...")
        client = EbyteGateway(gateway_info["ip"], args.username, args.password, interface)
        try:
            client.set_static_ip(target, args.mask, args.router)
        except GatewayError as e:
            print(f"FAIL: {e}")
            return 1

        print("Waiting for it to come back...", end="", flush=True)
        for _ in range(30):
            if probe(target, args.username, args.password) or probe(
                    target, args.username, args.password, interface):
                print(" up.")
                break
            print(".", end="", flush=True)
        else:
            print(f"\nFAIL: {target} never answered. {unreachable_hint(target)}")
            return 1

    if not data_port_open(target):
        print(f"WARNING: radio data port {target}:{DATA_PORT} is not accepting connections.")

    client = EbyteGateway(target, args.username, args.password)
    if not probe(target, args.username, args.password):
        client = EbyteGateway(target, args.username, args.password, interface)
    try:
        radio = client.radio()
    except GatewayError as e:
        print(f"FAIL: {e}")
        return 1

    with open(args.baseline, "w") as handle:
        json.dump(radio, handle, indent=2, sort_keys=True)

    print(f"\nBaseline radio settings saved to {args.baseline}:")
    for key, value in sorted(radio.items()):
        print(f"  {key:<16} {value}")
    print(f"\nSet lora.port to {target}:{DATA_PORT} in config.yaml on this station.")
    print("Every station must show identical radio settings -- compare with the other end.")
    return 0


def cmd_set(args):
    if args.power is None and args.air_rate is None and args.channel is None:
        print("Nothing to set. Pass --power, --air-rate or --channel.")
        return 1

    found = find_gateways(args)
    if not found:
        print("No gateway found. Run 'discover' for details.")
        return 1

    gateway_info = found[0]
    client = EbyteGateway(gateway_info["ip"], args.username, args.password,
                          gateway_info["interface"])
    print(f"Changing radio on {gateway_info['ip']} ({gateway_info['mac']}) and rebooting...")
    try:
        client.set_radio(args.power, args.air_rate, args.channel)
    except GatewayError as e:
        print(f"FAIL: {e}")
        return 1

    for _ in range(30):
        if probe(gateway_info["ip"], args.username, args.password,
                 gateway_info["interface"]):
            break
        time.sleep(1)
    else:
        print(f"FAIL: {gateway_info['ip']} did not come back.")
        return 1

    radio = client.radio()
    with open(args.baseline, "w") as handle:
        json.dump(radio, handle, indent=2, sort_keys=True)
    for key, value in sorted(radio.items()):
        print(f"  {key:<16} {value}")
    print(f"\nBaseline updated ({args.baseline}). Apply the SAME settings at the other station.")
    return 0


def cmd_check(args):
    host = configured_host(args)
    client = EbyteGateway(host, args.username, args.password, args.interface)
    try:
        identity = client.identify()
        radio = client.radio()
    except GatewayError as e:
        if not args.json:
            print(f"FAIL: no gateway at {host} ({e})\n")
            print(unreachable_hint(host))
        else:
            json.dump({"host": host, "reachable": False}, sys.stdout, indent=2)
        return 1

    link_up = data_port_open(host, interface=args.interface)
    report = {
        "host": host,
        "reachable": True,
        "model": identity.get("static_moudle"),
        "serial": (identity.get("static_SN") or "").strip(),
        "data_port_open": link_up,
        "radio": radio,
    }

    baseline = None
    try:
        with open(args.baseline) as handle:
            baseline = json.load(handle)
    except (OSError, json.JSONDecodeError):
        pass
    if baseline:
        report["drift"] = compare_radio(radio, baseline)

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0 if link_up and not report.get("drift") else 1

    print(f"{report['model']} at {host} (sn={report['serial']})")
    print(f"  data port {DATA_PORT}: {'open' if link_up else 'CLOSED'}")
    print("  radio settings:")
    for key, value in sorted(radio.items()):
        print(f"    {key:<16} {value}")

    if baseline is None:
        print(f"\nNo {args.baseline} to compare against; run 'adopt' to record one.")
    elif report["drift"]:
        print("\nDRIFT from baseline -- the link will not carry data:")
        for difference in report["drift"]:
            print(f"    {difference}")
        return 1
    else:
        print("\nRadio settings match the recorded baseline.")

    return 0 if link_up else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "adopt", "check", "set"])
    parser.add_argument("--host", help="gateway address (default: probe likely ones)")
    parser.add_argument("--ip", default=STANDARD_IP,
                        help=f"address to adopt the gateway to (default {STANDARD_IP})")
    parser.add_argument("--mask", default="255.255.255.0")
    parser.add_argument("--router", help="gateway address for the gateway itself")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--baseline", default=BASELINE_FILE)
    parser.add_argument("--power", choices=sorted(POWER_LEVELS),
                        help="transmit power (both stations must match)")
    parser.add_argument("--air-rate", help="air rate in kbps, e.g. 2.4 or 9.6")
    parser.add_argument("--channel", type=int, help="radio channel")
    parser.add_argument("--interface",
                        help="probe directly on this wired interface, ignoring routes")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    return {"discover": cmd_discover, "adopt": cmd_adopt, "check": cmd_check,
            "set": cmd_set}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
