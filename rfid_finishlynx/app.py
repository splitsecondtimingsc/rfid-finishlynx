"""Application controller for RFID-FinishLynx middleware."""

import concurrent.futures
import logging
import os
import re
import socket
import subprocess
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import yaml

from . import __version__
from .camera_start import START_CODE, CameraStartListener
from .config import Config
from .finishlynx import FinishLynxOutput, ScoreboardListener
from .hw_guard import HardwareLock, preview_requested
from .lora_receiver import LoRaReceiver
from .remote_agent import RemoteAgent
from .remote_manager import RemoteManager
from .rfid_reader import RFIDReader, TagRead, create_reader, default_port_for
from .transport import LoRaTransport, ReliableReadSender
from .lif_generator import generate_lif, generate_filename

logger = logging.getLogger(__name__)

# How long a successful "test connection" click counts as reachable on the dashboard
REACHABLE_TTL_SEC = 30.0

# FinishLynx streams running time at ~10 Hz while its clock runs; after this
# long without a packet we treat the clock as stopped.
FL_CLOCK_FRESH_SEC = 3.0

# A camera start with no race pane to land in drives the LED clocks this long
# (a race never runs longer); "Time of day" on a clock chip clears it sooner.
CAMERA_GUN_TTL_SEC = 2 * 3600.0


def get_local_network() -> Optional[Tuple[str, str]]:
    """Get the local IP address and subnet.

    Returns:
        Tuple of (ip_address, network_prefix) or None if unable to determine.
    """
    try:
        # Create a socket to determine our local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        # Assume /24 subnet (most common for local networks)
        parts = local_ip.split(".")
        network_prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        return local_ip, network_prefix
    except Exception:
        return None


def get_all_interface_subnets() -> List[Tuple[str, str, str]]:
    """Get all network interfaces and their subnets (cross-platform).

    Returns:
        List of tuples: (interface_name, ip_address, network_prefix)
    """
    interfaces = []

    # Try Linux 'ip' command first
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            current_iface = None
            for line in result.stdout.splitlines():
                iface_match = re.match(r'^\d+:\s+(\w+):', line)
                if iface_match:
                    current_iface = iface_match.group(1)
                    continue
                inet_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', line)
                if inet_match and current_iface and current_iface != 'lo':
                    ip = inet_match.group(1)
                    parts = ip.split(".")
                    prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
                    interfaces.append((current_iface, ip, prefix))
            if interfaces:
                return interfaces
    except FileNotFoundError:
        pass  # Not Linux, try Windows
    except Exception as e:
        logger.debug(f"Linux interface enumeration failed: {e}")

    # Try Windows 'ipconfig' command
    try:
        result = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            current_iface = None
            for line in result.stdout.splitlines():
                # Match adapter line
                adapter_match = re.match(r'^(\w.*?):', line)
                if adapter_match and "adapter" in line.lower():
                    current_iface = adapter_match.group(1).strip()
                    continue
                # Match IPv4 Address line
                ip_match = re.search(r'IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)', line)
                if ip_match and current_iface:
                    ip = ip_match.group(1)
                    if not ip.startswith("127."):
                        parts = ip.split(".")
                        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
                        interfaces.append((current_iface, ip, prefix))
            if interfaces:
                return interfaces
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Windows interface enumeration failed: {e}")

    # Fallback: use socket to find default interface
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # Connect to a public IP (doesn't actually send data)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        interfaces.append(("default", ip, prefix))
        logger.info(f"Using default interface: {ip}")
    except Exception as e:
        logger.debug(f"Socket fallback failed: {e}")

    return interfaces


# Known Impinj readers (hardcoded for reliable discovery)
KNOWN_IMPINJ_READERS = [
    {
        "hostname": "SpeedwayR-14-C8-1D",
        "mac": "00:16:25:14:c8:1d",
        "static_ip": "192.168.0.59",  # Configured static IP
        "link_local_ip": "169.254.200.29",  # Calculated from MAC: c8=200, 1d=29
    },
    {
        "hostname": "SpeedwayR-10-7D-12",
        "mac": "00:16:25:10:7d:12",
        "static_ip": "192.168.0.50",  # Configured static IP
        "link_local_ip": "169.254.125.18",  # Calculated from MAC: 7d=125, 12=18
    },
]


def mac_to_link_local(mac: str) -> str:
    """Convert MAC address to IPv4 link-local address.

    Impinj readers use 169.254.X.Y where X and Y are the last two MAC octets.

    Args:
        mac: MAC address in format "00:16:25:14:c8:1d"

    Returns:
        Link-local IP like "169.254.200.29"
    """
    octets = mac.split(":")
    return f"169.254.{int(octets[4], 16)}.{int(octets[5], 16)}"


def mac_to_ipv6_link_local(mac: str) -> str:
    """Convert MAC address to IPv6 link-local address (EUI-64 format).

    Args:
        mac: MAC address in format "00:16:25:14:c8:1d"

    Returns:
        IPv6 link-local address like "fe80::216:25ff:fe14:c81d"
    """
    octets = [int(x, 16) for x in mac.split(":")]
    # Flip universal/local bit (bit 1 of first octet)
    octets[0] ^= 0x02
    # Insert ff:fe in the middle (EUI-64)
    return f"fe80::{octets[0]:x}{octets[1]:02x}:{octets[2]:02x}ff:fe{octets[3]:02x}:{octets[4]:02x}{octets[5]:02x}"


def ipv6_to_mac(ipv6: str) -> Optional[str]:
    """Extract MAC address from IPv6 link-local address.

    IPv6 link-local encodes MAC as: fe80::XXYY:XXff:feYY:ZZZZ
    where the MAC is XX:YY:XX:YY:ZZ:ZZ (with ff:fe inserted)

    Args:
        ipv6: IPv6 address like "fe80::216:25ff:fe14:c81d"

    Returns:
        MAC address like "00:16:25:14:c8:1d" or None if not parseable
    """
    try:
        # Remove scope if present (e.g., %enp5s0)
        ipv6 = ipv6.split("%")[0]

        # Check if it's a link-local address
        if not ipv6.lower().startswith("fe80::"):
            return None

        # Extract the interface identifier part
        suffix = ipv6[6:]  # Remove "fe80::"

        # Parse the groups (e.g., "216:25ff:fe14:c81d")
        groups = suffix.split(":")
        if len(groups) != 4:
            return None

        # Reconstruct MAC from EUI-64 format
        # Groups are: [0]16, [1]25ff, [2]fe14, [3]c81d
        # MAC octets: 00:16:25:14:c8:1d
        b0 = int(groups[0][0], 16) if len(groups[0]) >= 1 else 0
        b1 = int(groups[0][1:], 16) if len(groups[0]) >= 2 else int(groups[0], 16)
        b2 = int(groups[1][:2], 16)
        # groups[1][2:] is "ff" and groups[2][:2] is "fe" - skip these (EUI-64 insertion)
        b3 = int(groups[2][2:], 16)
        b4 = int(groups[3][:2], 16)
        b5 = int(groups[3][2:], 16)

        # Flip the universal/local bit (bit 1 of first octet)
        b0 ^= 0x02

        return f"{b0:02x}:{b1:02x}:{b2:02x}:{b3:02x}:{b4:02x}:{b5:02x}"
    except Exception:
        return None


def discover_impinj_mdns_zeroconf() -> List[Dict]:
    """Discover Impinj readers via mDNS using zeroconf (cross-platform).

    Returns:
        List of dicts with 'host', 'port', 'hostname', 'mac', and 'type' keys.
    """
    readers = []
    found_macs = set()

    try:
        from zeroconf import Zeroconf, ServiceBrowser
        import socket

        class SpeedwayListener:
            def __init__(self):
                self.services = []

            def add_service(self, zc, service_type, name):
                if "SpeedwayR-" in name or "Speedway-" in name:
                    info = zc.get_service_info(service_type, name)
                    if info:
                        self.services.append((name, info))

            def remove_service(self, zc, service_type, name):
                pass

            def update_service(self, zc, service_type, name):
                pass

        zc = Zeroconf()
        listener = SpeedwayListener()

        # Browse for HTTP services (Impinj advertises _http._tcp.local.)
        browser = ServiceBrowser(zc, "_http._tcp.local.", listener)

        # Wait for discovery
        time.sleep(3)

        for name, info in listener.services:
            # Extract hostname from service name (e.g., "SpeedwayR-14-C8-1D._http._tcp.local.")
            hostname = name.split("._http._tcp")[0]

            # Get IP addresses
            resolved_ip = None
            mac = None

            # Try to find in known readers first
            for known in KNOWN_IMPINJ_READERS:
                if known["hostname"] == hostname:
                    mac = known["mac"]
                    break

            # Get IPv4 address from service info
            if info.addresses:
                for addr in info.addresses:
                    try:
                        ip = socket.inet_ntoa(addr)
                        if not ip.startswith("169.254.") and not ip.startswith("127."):
                            resolved_ip = ip
                            break
                    except Exception:
                        pass

            # If no good IPv4, try parsed_addresses (zeroconf 0.80+)
            if not resolved_ip and hasattr(info, 'parsed_addresses'):
                for ip in info.parsed_addresses():
                    if not ip.startswith("fe80:") and not ip.startswith("169.254.") and not ip.startswith("127."):
                        resolved_ip = ip
                        break
                    elif ip.startswith("fe80:"):
                        # Extract MAC from IPv6 link-local
                        extracted_mac = ipv6_to_mac(ip)
                        if extracted_mac:
                            mac = extracted_mac

            # If we have a MAC but no reachable IP, use the resolved IP anyway
            if not resolved_ip and info.addresses:
                for addr in info.addresses:
                    try:
                        resolved_ip = socket.inet_ntoa(addr)
                        break
                    except Exception:
                        pass

            if resolved_ip and (mac is None or mac not in found_macs):
                if mac:
                    found_macs.add(mac)
                readers.append({
                    "host": resolved_ip,
                    "port": 5084,
                    "hostname": hostname,
                    "mac": mac,
                    "type": "Impinj LLRP Reader (mDNS)",
                    "verified": False,
                })
                logger.info(f"Found Impinj reader via zeroconf: {hostname} at {resolved_ip}")

        zc.close()

    except ImportError:
        logger.debug("zeroconf not available")
    except Exception as e:
        logger.debug(f"zeroconf discovery error: {e}")

    return readers


def discover_impinj_mdns_avahi() -> List[Dict]:
    """Discover Impinj readers via mDNS using avahi (Linux only).

    Returns:
        List of dicts with 'host', 'port', 'hostname', 'mac', and 'type' keys.
    """
    readers = []
    found_macs = set()

    try:
        # Browse for HTTP services (Impinj advertises _http._tcp)
        browse_result = subprocess.run(
            ["avahi-browse", "-t", "-p", "_http._tcp"],
            capture_output=True,
            text=True,
            timeout=10
        )

        # Parse output for Speedway readers
        speedway_entries = []
        for line in browse_result.stdout.splitlines():
            if "SpeedwayR-" in line or "Speedway-" in line:
                parts = line.split(";")
                if len(parts) >= 4:
                    hostname = parts[3]
                    interface = parts[1] if len(parts) > 1 else None
                    if hostname not in [e[0] for e in speedway_entries]:
                        speedway_entries.append((hostname, interface))

        # Resolve each Speedway reader
        for hostname, interface in speedway_entries:
            reader_info = None
            resolved_ip = None
            mac = None

            for known in KNOWN_IMPINJ_READERS:
                if known["hostname"] == hostname:
                    reader_info = known
                    mac = known["mac"]
                    break

            # Try IPv4 resolution
            try:
                resolve_result = subprocess.run(
                    ["avahi-resolve", "-4", "-n", f"{hostname}.local"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                for line in resolve_result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and not parts[1].startswith("fe80"):
                        resolved_ip = parts[1]
                        break
            except Exception:
                pass

            # Try IPv6 resolution
            if not resolved_ip:
                try:
                    resolve_result = subprocess.run(
                        ["avahi-resolve", "-6", "-n", f"{hostname}.local"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    for line in resolve_result.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].startswith("fe80"):
                            ipv6 = parts[1]
                            extracted_mac = ipv6_to_mac(ipv6)
                            if extracted_mac:
                                mac = extracted_mac
                                resolved_ip = mac_to_link_local(extracted_mac)
                            break
                except Exception:
                    pass

            if not resolved_ip and reader_info:
                resolved_ip = reader_info["link_local_ip"]
                mac = reader_info["mac"]

            if resolved_ip and mac not in found_macs:
                found_macs.add(mac)
                readers.append({
                    "host": resolved_ip,
                    "port": 5084,
                    "hostname": hostname,
                    "mac": mac,
                    "type": "Impinj LLRP Reader (mDNS)",
                    "verified": False,
                })
                logger.info(f"Found Impinj reader via avahi: {hostname} at {resolved_ip}")

    except FileNotFoundError:
        logger.debug("avahi-browse not available")
    except subprocess.TimeoutExpired:
        logger.debug("mDNS browse timed out")
    except Exception as e:
        logger.debug(f"avahi discovery error: {e}")

    return readers


def discover_impinj_mdns() -> List[Dict]:
    """Discover Impinj readers via mDNS (cross-platform).

    Tries avahi first (Linux), then falls back to zeroconf (cross-platform).
    Also tries direct resolution of known reader hostnames.

    Returns:
        List of dicts with 'host', 'port', 'hostname', 'mac', and 'type' keys.
    """
    readers = []
    found_hostnames = set()

    # Try avahi first (Linux)
    avahi_readers = discover_impinj_mdns_avahi()
    for r in avahi_readers:
        if r["hostname"] not in found_hostnames:
            found_hostnames.add(r["hostname"])
            readers.append(r)

    # Try zeroconf (cross-platform, works on Windows)
    if not readers:
        zc_readers = discover_impinj_mdns_zeroconf()
        for r in zc_readers:
            if r["hostname"] not in found_hostnames:
                found_hostnames.add(r["hostname"])
                readers.append(r)

    # Also try direct socket resolution of known reader hostnames
    if not readers:
        import socket
        for known in KNOWN_IMPINJ_READERS:
            hostname = known["hostname"]
            if hostname in found_hostnames:
                continue
            try:
                # Try to resolve the .local hostname
                ip = socket.gethostbyname(f"{hostname}.local")
                readers.append({
                    "host": ip,
                    "port": 5084,
                    "hostname": hostname,
                    "mac": known["mac"],
                    "type": "Impinj LLRP Reader (mDNS)",
                    "verified": False,
                })
                found_hostnames.add(hostname)
                logger.info(f"Found Impinj reader via socket resolution: {hostname} at {ip}")
            except socket.gaierror:
                pass
            except Exception as e:
                logger.debug(f"Socket resolution failed for {hostname}: {e}")

    return readers


def discover_known_readers() -> List[Dict]:
    """Try to find known Impinj readers by their expected IPs.

    Checks static IPs first, then link-local addresses for the hardcoded readers.

    Returns:
        List of dicts with reader info for any readers found.
    """
    readers = []

    for known in KNOWN_IMPINJ_READERS:
        hostname = known["hostname"]
        mac = known["mac"]
        link_local = known["link_local_ip"]
        static_ip = known.get("static_ip")

        # IPs to try for this reader (static IP first, then link-local)
        candidate_ips = []
        if static_ip:
            candidate_ips.append(static_ip)
        candidate_ips.append(link_local)

        # Try each candidate IP
        for ip in candidate_ips:
            if scan_port(ip, 5084, timeout=1.0):
                readers.append({
                    "host": ip,
                    "port": 5084,
                    "hostname": hostname,
                    "mac": mac,
                    "type": "Impinj LLRP Reader (known)",
                    "verified": True,
                })
                logger.info(f"Found known reader {hostname} at {ip}")
                break  # Found this reader, move to next

    return readers


def get_local_ip_for_interface(interface: str = None) -> Optional[str]:
    """Get the local IP address, optionally for a specific interface.

    Args:
        interface: Specific interface name (e.g., 'eth0'). If None, returns primary IP.

    Returns:
        Local IP address string or None.
    """
    if interface:
        interfaces = get_all_interface_subnets()
        for iface_name, ip, prefix in interfaces:
            if iface_name == interface:
                return ip
        return None
    else:
        # Get primary IP (the one used for default route)
        network_info = get_local_network()
        return network_info[0] if network_info else None


def find_free_ip_on_subnet(prefix: str, start: int = 50, end: int = 99) -> Optional[str]:
    """Find an unused IP address on a subnet.

    Args:
        prefix: Network prefix (e.g., "192.168.0")
        start: Starting host number to check
        end: Ending host number to check

    Returns:
        Free IP address or None if none found.
    """
    for i in range(start, end + 1):
        ip = f"{prefix}.{i}"
        # IP is only free if nothing answers ping and no LLRP/SSH port is open
        if _ping_host(ip) or scan_port(ip, 5084, timeout=0.2) or scan_port(ip, 22, timeout=0.2):
            continue
        return ip
    return None


def _ping_host(ip: str, timeout_sec: int = 1) -> bool:
    """Return True if the host answers a single ping."""
    import platform
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_sec * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_sec), ip]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_sec + 2)
        return result.returncode == 0
    except Exception:
        return False


def ssh_exec_command_paramiko(host: str, command: str, password: str = "impinj", timeout: int = 15) -> Tuple[bool, str]:
    """Execute SSH command using paramiko (cross-platform).

    Args:
        host: Hostname or IP (can include %interface for IPv6 link-local)
        command: Command to execute
        password: SSH password
        timeout: Connection timeout

    Returns:
        Tuple of (success, output)
    """
    try:
        import paramiko
        import socket

        # Handle IPv6 link-local with scope (e.g., fe80::...%eth0)
        if "%" in host:
            # For paramiko, we need to resolve the address ourselves
            addr_part, scope = host.split("%", 1)
            # Get the scope ID for the interface
            try:
                # Try to get address info for the link-local address
                addr_info = socket.getaddrinfo(
                    addr_part, 22,
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    socket.AI_NUMERICHOST
                )
                if addr_info:
                    # Use the first result
                    sockaddr = addr_info[0][4]
                    # Get interface index
                    try:
                        scope_id = socket.if_nametoindex(scope)
                        sockaddr = (sockaddr[0], sockaddr[1], sockaddr[2], scope_id)
                    except (OSError, AttributeError):
                        # if_nametoindex may not be available on all platforms
                        pass
            except Exception as e:
                logger.debug(f"IPv6 scope handling error: {e}")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # For IPv6 link-local, try connecting with the full address
        connect_host = host.split("%")[0] if "%" in host else host

        try:
            client.connect(
                connect_host,
                port=22,
                username="root",
                password=password,
                timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except Exception as e:
            # If direct connection fails for IPv6 link-local, try socket approach
            if "%" in host:
                addr_part, scope = host.split("%", 1)
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                try:
                    scope_id = socket.if_nametoindex(scope)
                    sock.connect((addr_part, 22, 0, scope_id))
                    client.connect(
                        addr_part,
                        port=22,
                        username="root",
                        password=password,
                        timeout=timeout,
                        look_for_keys=False,
                        allow_agent=False,
                        sock=sock,
                    )
                except Exception as e2:
                    logger.debug(f"IPv6 socket connection failed: {e2}")
                    raise e
            else:
                raise

        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="replace")
        err_output = stderr.read().decode("utf-8", errors="replace")
        client.close()

        return ("Success" in output, output + err_output)

    except ImportError:
        logger.debug("paramiko not available")
        return (False, "paramiko not installed")
    except Exception as e:
        logger.debug(f"paramiko SSH error: {e}")
        return (False, str(e))


def ssh_exec_command_sshpass(host: str, command: str, password: str = "impinj", timeout: int = 15) -> Tuple[bool, str]:
    """Execute SSH command using sshpass (Linux).

    Args:
        host: Hostname or IP
        command: Command to execute
        password: SSH password
        timeout: Connection timeout

    Returns:
        Tuple of (success, output)
    """
    try:
        result = subprocess.run(
            ["sshpass", "-p", password, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10",
             f"root@{host}", command],
            capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        success = "Success" in result.stdout
        return (success, output)
    except FileNotFoundError:
        return (False, "sshpass not installed")
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    except Exception as e:
        return (False, str(e))


def ssh_exec_command(host: str, command: str, password: str = "impinj", timeout: int = 15) -> Tuple[bool, str]:
    """Execute SSH command (cross-platform).

    Tries sshpass first (Linux), then paramiko (cross-platform).

    Returns:
        Tuple of (success, output)
    """
    # Try sshpass first (faster on Linux)
    success, output = ssh_exec_command_sshpass(host, command, password, timeout)
    if success:
        return (success, output)

    # Fall back to paramiko on any sshpass failure (not installed, timeout, error)
    logger.info(f"sshpass SSH failed ({output.strip()[:80]}), falling back to paramiko")
    return ssh_exec_command_paramiko(host, command, password, timeout)


def configure_reader_ip_via_ssh(
    reader_hostname: str,
    new_ip: str,
    new_mask: str = "255.255.255.0",
    new_gateway: str = None,
    ssh_password: str = "impinj",
    mac: str = None,
    interface: str = None,
) -> bool:
    """Configure an Impinj reader's IP address via SSH (cross-platform).

    Tries multiple connection methods:
    1. IPv6 link-local (if MAC and interface provided) - works even on different subnets
    2. mDNS .local hostname (requires same subnet or working mDNS routing)

    Args:
        reader_hostname: mDNS hostname (e.g., "SpeedwayR-14-C8-1D.local")
        new_ip: New IP address to assign
        new_mask: Subnet mask (default 255.255.255.0)
        new_gateway: Gateway IP (default: first IP on subnet)
        ssh_password: SSH password (default: impinj)
        mac: MAC address of the reader (optional, enables IPv6 link-local)
        interface: Network interface for IPv6 link-local (optional)

    Returns:
        True if configuration successful and reader rebooted.
    """
    if not new_gateway:
        parts = new_ip.split(".")
        new_gateway = f"{parts[0]}.{parts[1]}.{parts[2]}.1"

    # Build list of SSH targets to try
    ssh_targets = []

    # Try IPv6 link-local first (works across subnets)
    if mac and interface:
        ipv6_ll = mac_to_ipv6_link_local(mac)
        ssh_targets.append(f"{ipv6_ll}%{interface}")
        logger.info(f"Will try IPv6 link-local: {ipv6_ll}%{interface}")

    # Fallback to mDNS hostname
    if not reader_hostname.endswith(".local"):
        reader_hostname = f"{reader_hostname}.local"
    ssh_targets.append(reader_hostname)

    # Try each SSH target
    connected_target = None
    config_cmd = f"config network ip static {new_ip} {new_mask} {new_gateway}"

    for target in ssh_targets:
        logger.info(f"Trying SSH to {target}...")
        success, output = ssh_exec_command(target, config_cmd, ssh_password, timeout=20)

        if success:
            connected_target = target
            logger.info(f"Successfully configured via {target}")
            break
        elif "timeout" in output.lower() or "timed out" in output.lower():
            logger.warning(f"Connection timed out to {target}, trying next...")
            continue
        else:
            logger.warning(f"SSH failed to {target}: {output[:100]}, trying next...")
            continue

    if not connected_target:
        logger.error("Could not connect to reader via any method")
        return False

    logger.info("IP configured, rebooting reader...")

    # Reboot the reader (may timeout, that's OK)
    ssh_exec_command(connected_target, "reboot", ssh_password, timeout=10)

    # Wait for reader to come back up at new IP
    logger.info(f"Waiting for reader to reboot at {new_ip}...")
    time.sleep(20)

    # Verify the reader is accessible at the new IP
    for attempt in range(15):
        if scan_port(new_ip, 5084, timeout=2.0):
            logger.info(f"Reader now accessible at {new_ip}")
            return True
        logger.debug(f"Waiting for reader... attempt {attempt + 1}/15")
        time.sleep(2)

    logger.warning(f"Reader not responding at {new_ip} after reboot")
    return False


def auto_configure_reader(hostname: str = None) -> Optional[Dict]:
    """Automatically find and configure an Impinj reader to be on the local subnet.

    This function:
    1. Discovers readers via mDNS
    2. If a reader is found but not reachable via IP, determines local subnet
    3. Assigns a compatible static IP to the reader via SSH

    Args:
        hostname: Specific reader hostname to configure (e.g., "SpeedwayR-14-C8-1D").
                  If None, configures the first unreachable reader found.

    Returns:
        Dict with reader info if successful, None otherwise.
    """
    logger.info("Auto-configuring reader...")

    # Get local network info
    interfaces = get_all_interface_subnets()
    if not interfaces:
        logger.error("No network interfaces found")
        return None

    # Prefer non-docker, non-virtual interfaces
    preferred_interface = None
    for iface_name, ip, prefix in interfaces:
        if not any(x in iface_name.lower() for x in ['docker', 'br-', 'veth', 'lo']):
            preferred_interface = (iface_name, ip, prefix)
            break

    if not preferred_interface:
        preferred_interface = interfaces[0]

    local_iface, local_ip, local_prefix = preferred_interface
    logger.info(f"Local network: {local_ip} on {local_iface} (prefix: {local_prefix})")

    # Discover readers via mDNS
    mdns_readers = discover_impinj_mdns()

    if not mdns_readers:
        logger.warning("No readers found via mDNS")
        return None

    # Find a reader to configure
    target_reader = None
    for reader in mdns_readers:
        reader_hostname = reader.get("hostname", "")

        # If specific hostname requested, match it
        if hostname and hostname.lower() not in reader_hostname.lower():
            continue

        # Check if reader is already reachable
        if reader.get("verified"):
            logger.info(f"Reader {reader_hostname} already reachable at {reader['host']}")
            if not hostname:  # If no specific hostname, skip already-working readers
                continue
            return reader

        target_reader = reader
        break

    if not target_reader:
        logger.info("No unreachable readers found to configure")
        return None

    reader_hostname = target_reader.get("hostname")
    logger.info(f"Configuring reader: {reader_hostname}")

    # Find a free IP on the local subnet
    new_ip = find_free_ip_on_subnet(local_prefix, start=50, end=99)
    if not new_ip:
        logger.error(f"No free IP found on subnet {local_prefix}.0/24")
        return None

    logger.info(f"Assigning IP {new_ip} to {reader_hostname}")

    # Get MAC address for IPv6 link-local connection
    reader_mac = target_reader.get("mac")
    if not reader_mac:
        # Try to extract MAC from hostname (e.g., "SpeedwayR-14-C8-1D" -> "00:16:25:14:c8:1d")
        for known in KNOWN_IMPINJ_READERS:
            if known["hostname"] == reader_hostname:
                reader_mac = known["mac"]
                break

    # Configure the reader (uses IPv6 link-local if MAC available)
    if configure_reader_ip_via_ssh(reader_hostname, new_ip, mac=reader_mac, interface=local_iface):
        # Update known readers list with new IP
        for known in KNOWN_IMPINJ_READERS:
            if known["hostname"] == reader_hostname:
                known["static_ip"] = new_ip

        return {
            "host": new_ip,
            "port": 5084,
            "hostname": reader_hostname,
            "mac": target_reader.get("mac"),
            "type": "Impinj LLRP Reader (auto-configured)",
            "verified": True,
        }

    return None


def scan_subnet_for_llrp(network_prefix: str, timeout: float = 0.3, max_workers: int = 50) -> List[str]:
    """Scan a specific /24 subnet for LLRP port 5084.

    Args:
        network_prefix: First three octets (e.g., "192.168.100")
        timeout: Connection timeout per host.
        max_workers: Maximum parallel scan threads.

    Returns:
        List of IP addresses with LLRP port open.
    """
    found = []
    ips_to_scan = [f"{network_prefix}.{i}" for i in range(1, 255)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ip = {executor.submit(scan_port, ip, 5084, timeout): ip for ip in ips_to_scan}
        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                if future.result():
                    found.append(ip)
            except Exception:
                pass

    return found


def scan_port(ip: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a port is open on a given IP.

    Args:
        ip: IP address to scan.
        port: Port number to check.
        timeout: Connection timeout in seconds.

    Returns:
        True if port is open.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
        finally:
            sock.close()
        return result == 0
    except Exception:
        return False


def scan_network_for_port(port: int, timeout: float = 0.5, max_workers: int = 50) -> List[str]:
    """Scan local network for devices with a specific port open.

    Args:
        port: Port number to scan for.
        timeout: Connection timeout per host.
        max_workers: Maximum parallel scan threads.

    Returns:
        List of IP addresses with the port open.
    """
    network_info = get_local_network()
    if not network_info:
        return []

    local_ip, network_prefix = network_info
    found = []

    # Generate list of IPs to scan (1-254)
    ips_to_scan = [f"{network_prefix}.{i}" for i in range(1, 255)]

    # Scan in parallel for speed
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ip = {executor.submit(scan_port, ip, port, timeout): ip for ip in ips_to_scan}

        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                if future.result():
                    found.append(ip)
            except Exception:
                pass

    return sorted(found, key=lambda x: [int(p) for p in x.split(".")])


def discover_impinj_readers(configured_host: Optional[str] = None, scan_192_168: bool = True) -> List[Dict]:
    """Discover Impinj RFID readers on the network.

    Uses multiple discovery methods:
    1. mDNS/Avahi (fastest, finds readers by hostname)
    2. Direct check of configured host
    3. Scan all local interface subnets
    4. Scan common 192.168.x.x subnets (Impinj default range)

    Args:
        configured_host: The configured reader IP to also check directly.
        scan_192_168: Whether to scan common 192.168.x.x subnets.

    Returns:
        List of dicts with 'host', 'port', 'hostname' (if known), and 'type' keys.
    """
    logger.info("Discovering Impinj readers...")
    results = []
    found_ips = set()
    found_macs = set()

    # Method 0: Try known readers' link-local IPs first (fastest for known hardware)
    logger.info("Checking known reader link-local addresses...")
    known_readers = discover_known_readers()
    for reader in known_readers:
        if reader["host"] not in found_ips:
            found_ips.add(reader["host"])
            if reader.get("mac"):
                found_macs.add(reader["mac"])
            results.append(reader)

    # Method 1: Try mDNS discovery (finds readers by hostname, calculates IPs)
    logger.info("Trying mDNS discovery...")
    mdns_readers = discover_impinj_mdns()
    for reader in mdns_readers:
        # Skip if we already found this reader by MAC or IP
        if reader.get("mac") in found_macs:
            continue
        if reader["host"] not in found_ips:
            # Try to verify the reader is reachable
            if scan_port(reader["host"], 5084, timeout=1.0):
                reader["verified"] = True
                logger.info(f"Verified reader {reader.get('hostname')} at {reader['host']}")
            else:
                reader["verified"] = False
                logger.info(f"Found reader {reader.get('hostname')} via mDNS at {reader['host']} (not yet reachable)")

            found_ips.add(reader["host"])
            if reader.get("mac"):
                found_macs.add(reader["mac"])
            results.append(reader)

    # Method 2: Check configured host directly
    if configured_host and configured_host not in found_ips:
        logger.info(f"Checking configured host {configured_host}...")
        if scan_port(configured_host, 5084, timeout=1.0):
            found_ips.add(configured_host)
            results.append({
                "host": configured_host,
                "port": 5084,
                "hostname": None,
                "type": "Impinj LLRP Reader",
            })
            logger.info(f"Found reader at configured host {configured_host}")

    # Method 3: Scan all local interface subnets
    logger.info("Scanning local interface subnets...")
    interfaces = get_all_interface_subnets()
    scanned_prefixes = set()

    for iface_name, iface_ip, prefix in interfaces:
        if prefix in scanned_prefixes:
            continue
        scanned_prefixes.add(prefix)
        logger.debug(f"Scanning {prefix}.0/24 on {iface_name}...")

        for ip in scan_subnet_for_llrp(prefix, timeout=0.3):
            if ip not in found_ips:
                found_ips.add(ip)
                results.append({
                    "host": ip,
                    "port": 5084,
                    "hostname": None,
                    "type": "Impinj LLRP Reader",
                })
                logger.info(f"Found reader at {ip} on {iface_name}")

    # Method 4: Scan common 192.168.x.x subnets (Impinj often defaults here)
    if scan_192_168:
        # Common subnets Impinj readers might be on
        common_subnets = [
            # 192.168.0.x through 192.168.10.x
            "192.168.0", "192.168.1", "192.168.2", "192.168.3", "192.168.4",
            "192.168.5", "192.168.6", "192.168.7", "192.168.8", "192.168.9",
            "192.168.10",
            # Other common subnets
            "192.168.100", "192.168.200",
            "10.0.0", "10.0.1",
        ]

        # Only scan subnets we haven't already scanned
        subnets_to_scan = [s for s in common_subnets if s not in scanned_prefixes]

        if subnets_to_scan:
            logger.info(f"Scanning {len(subnets_to_scan)} common subnets for Impinj readers...")

            # Scan all common subnets in parallel
            all_ips_to_scan = []
            for prefix in subnets_to_scan:
                all_ips_to_scan.extend([f"{prefix}.{i}" for i in range(1, 255)])

            with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
                future_to_ip = {
                    executor.submit(scan_port, ip, 5084, 0.3): ip
                    for ip in all_ips_to_scan
                }
                for future in concurrent.futures.as_completed(future_to_ip):
                    ip = future_to_ip[future]
                    try:
                        if future.result() and ip not in found_ips:
                            found_ips.add(ip)
                            results.append({
                                "host": ip,
                                "port": 5084,
                                "hostname": None,
                                "type": "Impinj LLRP Reader",
                            })
                            logger.info(f"Found reader at {ip}")
                    except Exception:
                        pass

    # Also scan configured reader's subnet if different
    if configured_host:
        parts = configured_host.split(".")
        if len(parts) == 4:
            reader_prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
            if reader_prefix not in scanned_prefixes:
                logger.info(f"Scanning configured reader subnet {reader_prefix}.0/24...")
                for ip in scan_subnet_for_llrp(reader_prefix, timeout=0.3):
                    if ip not in found_ips:
                        found_ips.add(ip)
                        results.append({
                            "host": ip,
                            "port": 5084,
                            "hostname": None,
                            "type": "Impinj LLRP Reader",
                        })
                        logger.info(f"Found reader at {ip}")

    if not results:
        logger.warning("No Impinj readers found on the network")
    else:
        logger.info(f"Discovery complete: found {len(results)} reader(s)")

    # Add 'ip' key for backward compatibility (alias for 'host')
    for result in results:
        result["ip"] = result["host"]

    return results


def discover_finishlynx(configured_host: Optional[str] = None, configured_port: int = 5000) -> List[Dict]:
    """Discover FinishLynx systems on the network.

    Scans for devices with LapTime port open. Also checks the configured
    host directly and its subnet if different from local.

    Args:
        configured_host: The configured FinishLynx IP to check directly.
        configured_port: The configured port (default 5000).

    Returns:
        List of dicts with 'ip' and 'port' keys.
    """
    logger.info(f"Scanning network for FinishLynx (port {configured_port})...")
    found_ips = set()

    # Check the configured host directly first
    if configured_host and scan_port(configured_host, configured_port, timeout=1.0):
        found_ips.add(configured_host)

    # Scan the local subnet
    local_ips = scan_network_for_port(configured_port, timeout=0.3)
    found_ips.update(local_ips)

    # Also scan the configured host's subnet if it differs from local
    if configured_host:
        parts = configured_host.split(".")
        if len(parts) == 4:
            host_prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
            network_info = get_local_network()
            local_prefix = network_info[1] if network_info else None
            if host_prefix != local_prefix:
                logger.info(f"Also scanning FinishLynx subnet {host_prefix}.0/24...")
                ips_to_scan = [f"{host_prefix}.{i}" for i in range(1, 255)]
                with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                    future_to_ip = {executor.submit(scan_port, ip, configured_port, 0.3): ip for ip in ips_to_scan}
                    for future in concurrent.futures.as_completed(future_to_ip):
                        ip = future_to_ip[future]
                        try:
                            if future.result():
                                found_ips.add(ip)
                        except Exception:
                            pass

    results = []
    for ip in sorted(found_ips, key=lambda x: [int(p) for p in x.split(".")]):
        results.append({
            "ip": ip,
            "port": configured_port,
            "type": "FinishLynx LapTime",
        })
        logger.info(f"Found potential FinishLynx at {ip}:{configured_port}")

    return results


def discover_serial_ports() -> List[Dict]:
    """Discover available serial ports for LoRa devices.

    Returns:
        List of dicts with port information.
    """
    import serial.tools.list_ports

    logger.info("Scanning for serial ports...")
    ports = serial.tools.list_ports.comports()

    results = []
    for port in ports:
        # Determine if this might be a LoRa device based on description
        description = port.description or ""
        manufacturer = port.manufacturer or ""

        # Common USB-serial chips used by LoRa modules
        is_likely_lora = any(x in description.lower() or x in manufacturer.lower() for x in [
            "ch340", "ch341",  # Common Chinese USB-serial chip
            "cp210", "cp2102",  # Silicon Labs
            "ftdi", "ft232",  # FTDI chips
            "pl2303",  # Prolific
            "usb serial", "usb-serial",
            "uart",
        ])

        results.append({
            "port": port.device,
            "description": port.description or "Unknown",
            "manufacturer": port.manufacturer or "Unknown",
            "hwid": port.hwid or "",
            "is_likely_lora": is_likely_lora,
        })
        logger.info(f"Found serial port: {port.device} - {port.description}")

    # Sort with likely LoRa devices first
    results.sort(key=lambda x: (not x["is_likely_lora"], x["port"]))

    return results


class Application:
    """Main application controller with web interface support."""

    # Defaults for objects built without __init__ (tests use Application.__new__)
    hardware_allowed: bool = True
    preview_reason: Optional[str] = None
    meet_service = None  # MeetService (meet_service.py); None = legacy mode only

    def __init__(self, config: Config, config_path: Optional[str] = None):
        """Initialize application.

        Args:
            config: Application configuration.
            config_path: Path to config file for saving changes.
        """
        self.config = config
        self.config_path = config_path

        # Hardware ownership (hw_guard.py): exactly one process per machine
        # may open the reader / clocks / radio / camera / FinishLynx. This
        # one either owns them from here on or runs as a PREVIEW (web UI
        # only) - decided once, at construction, so nothing in start() or
        # the restart handlers can ever open a device by mistake.
        self.hw_lock = HardwareLock()
        self.preview_reason: Optional[str] = preview_requested(config_path)
        if self.preview_reason is None and not self.hw_lock.acquire():
            owner = self.hw_lock.owner_pid
            self.preview_reason = (f"another middleware (pid {owner}) owns the hardware on this machine"
                                   if owner else "another middleware owns the hardware on this machine")
        self.hardware_allowed: bool = self.preview_reason is None
        # read FinishLynx's window title for its open event (fl_window.py);
        # set here only, so preview and test instances never look. Finish
        # line only: a station has no FinishLynx (and no fl_window.py).
        self.watch_finishlynx_window: bool = self.hardware_allowed and config.mode == "finish_line"
        if not self.hardware_allowed:
            logger.warning(f"PREVIEW MODE - no hardware will be touched: {self.preview_reason}")

        # Stations panel "Publishing" switch (2026-09-26): runtime only, ON
        # at every boot on purpose - a forgotten OFF must not survive a
        # restart into the next meet. See set_publishing().
        self.publishing: bool = True
        self._publish_off_at: Optional[datetime] = None
        self._flushed_notes: Dict[int, str] = {}   # read id -> what _flush_unsent did

        # Components
        self.finishlynx: Optional[FinishLynxOutput] = None
        self.scoreboard_listener: Optional[ScoreboardListener] = None
        self.reader: Optional[RFIDReader] = None
        self.lora_receiver: Optional[LoRaReceiver] = None
        self.lora_transport: Optional[LoRaTransport] = None
        self.remote_manager: Optional[RemoteManager] = None
        self.remote_agent: Optional[RemoteAgent] = None
        self.read_sender: Optional[ReliableReadSender] = None

        # State
        self._running = False
        self._reads: List[Dict] = []
        self._reads_lock = threading.Lock()
        self._unique_bibs: set = set()
        self._last_read_time: Optional[str] = None

        # Reachability flags (set by test functions)
        # Timestamps of last successful reachability test (0.0 = never);
        # treated as "reachable" only for REACHABLE_TTL_SEC afterwards
        self._reader_reachable_at = 0.0
        self._finishlynx_reachable_at = 0.0

        # Lap tracking
        self._bib_laps: Dict[str, List[Dict]] = {}  # bib -> list of lap entries

        # Auto-reconnect and send queue
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_running = False
        self._pending_sends: List[Dict] = []  # Buffered sends for when FinishLynx reconnects
        self._remote_last_sent: Dict[str, float] = {}  # bib -> monotonic time of last LoRa transmit
        self._supervisor_thread: Optional[threading.Thread] = None
        self._supervisor_stop = threading.Event()
        self._reader_outages: List[Dict] = []
        self._reader_was_connected: Optional[bool] = None
        self._lora_seen_up = False  # see lora_link_seen_up()
        self.race_manager = None  # RaceManager when config.races.enabled (finish_line mode)
        if config.mode == "finish_line" and config.races.enabled:
            from . import web
            from .races import RaceManager
            self.race_manager = RaceManager(
                athlete_lookup=web.get_athlete_info,
                on_change=self._on_races_changed,
                chute_reader=config.races.chute_reader,
                finish_readers=set(config.races.finish_readers),
                point_labels=config.races.point_labels,
                point_distances=config.races.point_distances,
                state_path=config.races.state_file,
                reader_points=config.races.reader_points,
                pass_window_sec=config.races.pass_window_sec,
                fastest_pace_sec_per_mile=config.races.fastest_pace_sec_per_mile,
            )
            logger.info("Race manager initialized")
            for reader, pts in config.races.reader_points.items():
                logger.info(f"Reader {reader} stands at {' then '.join(pts)} (passes counted)")
        # Meet management (2026-10-03, docs/MEET-API.md): meet/ beside the
        # race state file, like closed-meets/. With no meet open (meet/ACTIVE
        # absent) it changes nothing: legacy .evt/.ppl as before. Finish line
        # only; the station package leaves the meet code out.
        self.meet_service = None
        if config.mode == "finish_line":
            try:
                from .meet_service import MeetService
                base = os.path.dirname(os.path.abspath(config.races.state_file or "races-state.json"))
                self.meet_service = MeetService(os.path.join(base, "meet"), self)
            except Exception:
                logger.exception("Meet management unavailable")
        # Meets shared between machines (meet_sync.py, 2026-10-07): only the
        # copy that owns the hardware syncs (a preview copy never does).
        self.meet_sync = None
        if self.meet_service is not None and self.hardware_allowed:
            self.start_meet_sync()
        # Check-in timers and schedules (2026-10-04): start times from the
        # meet; only the copy that owns the hardware opens/closes check-ins
        if self.race_manager:
            self.race_manager.start_time_of = self._race_start_time
            if self.hardware_allowed:
                threading.Thread(target=self._checkin_loop, daemon=True).start()
        self._pending_lock = threading.Lock()
        self._config_save_lock = threading.Lock()

        # Web callbacks
        self._on_tag_read_callback = None
        self._on_remote_unit_change_callback = None
        self._process_for_finishlynx_callback = None  # For relay mode processing

        # Operation mode: "current_reads" (passthrough) or "finishlynx" (smart mode)
        self.operation_mode: str = "finishlynx"  # Default to FinishLynx smart mode

        # Race reads for FinishLynx mode (separate from _reads which is for display)
        self._race_reads: List[Dict] = []
        self._race_reads_lock = threading.Lock()

        # Manual race safety flag
        self.manual_race_in_progress: bool = False

        # LED race clock (finish + split stations)
        self.led_clock_service = None
        self.remote_gun: Optional[datetime] = None  # gun received over LoRa (remote_lap)
        # Split station: how far this box's clock runs ahead of the finish
        # line's, learned from the send time in each GUN broadcast
        # (see set_remote_gun). Samples are (local time, offset) pairs.
        self.remote_clock_offset: float = 0.0
        self._remote_clock_samples: List[Tuple[float, float]] = []
        self._clock_step_failed: bool = False
        self._gun_broadcast_epoch: Optional[float] = None
        self._gun_broadcast_hour_format: Optional[str] = None
        self._gun_broadcast_at: float = 0.0
        # Gun implied by FinishLynx's own running clock (scoreboard packets),
        # kept even when no race pane matches so the LED clocks can follow
        # FinishLynx directly - e.g. right after a restart, before the event's
        # start list has been re-sent, or when the operator never made a pane.
        self._fl_clock_gun: Optional[datetime] = None
        self._fl_clock_at: float = 0.0
        # Operator override from the console clock chip: None = automatic,
        # {"mode": "gun", "gun": dt} = count up from a chosen time,
        # {"mode": "tod"} = force time-of-day.
        self._clock_override: Optional[Dict] = None
        self._started_at = time.time()
        # Start gun sniffed from the camera (camera_start.py). Feeds the race
        # panes; when no pane is waiting it drives the LED clocks directly.
        self.camera_start: Optional[CameraStartListener] = None
        self._camera_gun: Optional[datetime] = None
        self._camera_gun_at: float = 0.0
        self._antenna_reads: Dict[int, int] = {}   # antenna port -> reads since start/clear
        # Every start signal heard today (camera or FinishLynx clock), newest
        # last, so a pane created after the gun can still pick its start.
        self._recent_starts: List[Dict] = []

    def set_tag_read_callback(self, callback):
        """Set callback for tag reads (used by web interface)."""
        self._on_tag_read_callback = callback

    def set_process_for_finishlynx_callback(self, callback):
        """Set callback for processing reads before sending to FinishLynx.

        Callback signature: callback(bib, timestamp) -> (identifier, should_send, extra_data)
        - identifier: What to send to FinishLynx (bib or lane number)
        - should_send: Whether to send this read to FinishLynx
        - extra_data: Dict with additional info (team_name, leg, etc.) for UI
        """
        self._process_for_finishlynx_callback = callback

    def _get_finishlynx_identifier(self, bib: str, timestamp) -> tuple:
        """Get the identifier to send to FinishLynx (may be lane number for relays).

        Returns:
            Tuple of (identifier, should_send, extra_data)
        """
        if self._process_for_finishlynx_callback:
            return self._process_for_finishlynx_callback(bib, timestamp)
        # Default: send bib as-is
        return (bib, True, {})

    def _track_lap(self, bib: str, timestamp, reader_id: str, manual: bool = False) -> Dict:
        """Track a lap for a bib and compute position.

        Returns the read_data dict with lap info included.
        """
        time_str = timestamp.strftime('%H:%M:%S.') + f'{timestamp.microsecond // 1000:03d}'

        # Add to bib's lap list
        with self._reads_lock:
            if bib not in self._bib_laps:
                self._bib_laps[bib] = []
            lap_entry = {
                'timestamp': timestamp,
                'reader_id': reader_id,
                'manual': manual,
                'synthetic': False,
            }
            self._bib_laps[bib].append(lap_entry)
            lap_number = len(self._bib_laps[bib])
            position = self._compute_position(bib, lap_number)

            read_data = {
                'bib': bib,
                'time': time_str,
                'timestamp': timestamp.isoformat(),
                'reader_id': reader_id,
                'manual': manual,
                'lap_number': lap_number,
                'total_laps': self.config.timing.total_laps,
                'position': position,
            }
            self._reads.insert(0, read_data)
            self._unique_bibs.add(bib)
            self._last_read_time = time_str

        return read_data

    def _compute_position(self, bib: str, lap_number: int) -> int:
        """Compute race position for a bib at a given lap count.

        Position = how many bibs reached this lap count before (or at same time),
        ordered by timestamp. Must be called with _reads_lock held.
        """
        laps = self._bib_laps.get(bib, [])
        if len(laps) < lap_number:
            return 1
        target_time = laps[lap_number - 1]['timestamp']

        position = 1
        for other_bib, laps in self._bib_laps.items():
            if other_bib == bib:
                continue
            if len(laps) >= lap_number:
                if laps[lap_number - 1]['timestamp'] < target_time:
                    position += 1
        return position

    def _on_tag_read(self, tag: TagRead):
        """Handle tag read from RFID reader with mode-aware routing.

        Args:
            tag: Tag read data.
        """
        # Per-antenna tally: the only practical way to see a dead port or an
        # unplugged cable is that it stops contributing reads.
        try:
            ant = int(getattr(tag, "antenna", 0) or 0)
            with self._reads_lock:
                self._antenna_reads[ant] = self._antenna_reads.get(ant, 0) + 1
        except (TypeError, ValueError):
            pass

        # Race panes classify every read in parallel with the FinishLynx
        # path - additive, never blocks or filters the existing pipeline.
        if self.race_manager:
            try:
                res = self.race_manager.ingest(tag.bib, tag.timestamp, tag.reader_id,
                                               antenna=int(getattr(tag, "antenna", 0) or 0))
                # A reader standing at several split points labels the read
                # by pass number; carry that to the live feed (reader_id
                # itself stays as heard - the read log and FinishLynx path
                # never see the remap).
                point = res.get("point")
                # The panes' verdict travels with the read: with
                # finishlynx.feed "all", a split read reaches LapTime only
                # when a pane took it (see _feeds_finishlynx).
                tag.pane_state = res.get("state")
                tag.pane_reason = res.get("reason") or ""
                tag.pane_race = res.get("race")
                tag.pane_early = bool(res.get("early"))
                tag.pane_crossing = res.get("crossing") or ""
                tag.read_id = res.get("read_id")
                if res.get("state") == "ignored" and str(res.get("reason", "")).startswith("pass "):
                    tag.point = "-"        # a "not a split" crossing (loop course)
                elif point and point != tag.reader_id:
                    tag.point = point
                # Where the read went, one line per read: the log alone can
                # answer "why is bib X not in the pane?" (2026-10-01)
                race = self.race_manager.races.get(res.get("race"))
                name = f"'{race.name}'" if race else "no race"
                state = res.get("state")
                where = (f"-> {name}" + (f" as {point}" if point and point != tag.reader_id else "")
                         if state == "assigned" else
                         f"-> checked in for {name}" if state == "roster" else
                         f"-> {state}{' (' + name + ')' if race else ''}: {res.get('reason') or 'no reason'}")
                logger.info(f"Read {tag.bib} at {tag.reader_id}"
                            f"{' A' + str(tag.antenna) if getattr(tag, 'antenna', 0) else ''} {where}")
                # Finish-mat reads go to FinishLynx corrected by the film
                # offset learned this meet (the read log keeps the raw time).
                corr = float(res.get("finish_offset") or 0.0)
                if corr:
                    tag.fl_timestamp = tag.timestamp - timedelta(seconds=corr)
                    logger.info(f"Read {tag.bib} at {tag.reader_id}: film offset {corr:+.3f} s applied "
                                f"-> {tag.fl_timestamp.strftime('%H:%M:%S.%f')[:-3]} to FinishLynx")
            except Exception:
                logger.exception("Race manager ingest failed")

        # Plain-text backup of every read, whatever happens to it next
        self._backup_read(tag.reader_id, tag.bib, tag.timestamp,
                          antenna=int(getattr(tag, "antenna", 0) or 0),
                          point=getattr(tag, "point", None) or tag.reader_id,
                          state=getattr(tag, "pane_state", None) or "",
                          reason=getattr(tag, "pane_reason", "") or "")

        if self.operation_mode == "finishlynx":
            self._on_tag_read_finishlynx_mode(tag)
        else:
            # Default: current_reads mode (passthrough)
            self._on_tag_read_current_reads(tag)

    # ---------------------------------------------------------- read backup

    BACKUP_HEADER = "date\tclock\treader\tbib\tantenna\tpoint\tpane\treason\n"

    def backup_path(self) -> str:
        """reads-YYYY-MM-DD.txt in the RFID folder (the one MeetPro reads),
        "" when there is no meet folder."""
        try:
            rfid_dir = self.lif_dirs()[1]
        except Exception:
            return ""
        if not rfid_dir:
            return ""
        return os.path.join(rfid_dir, f"reads-{datetime.now():%Y-%m-%d}.txt")

    def _backup_read(self, reader_id: str, bib: str, ts: datetime, antenna: int = 0,
                     point: str = "", state: str = "", reason: str = ""):
        """Append one line per read to a plain-text file beside the merged
        .lif files (2026-09-26, user's ask): if publishing was left off, a
        reader mislabelled, or the console lost, the raw reads with their
        clock times are still there to rebuild from. Never in preview."""
        if not getattr(self, "hardware_allowed", False):
            return
        path = self.backup_path()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write(self.BACKUP_HEADER)
                f.write(f"{ts:%Y-%m-%d}\t{ts:%H:%M:%S}.{ts.microsecond // 1000:03d}\t{reader_id}\t{bib}\t"
                        f"{antenna or ''}\t{point}\t{state}\t{reason}\n")
        except OSError as e:
            if not getattr(self, "_backup_warned", False):
                self._backup_warned = True
                logger.warning(f"Read backup file not writable ({path}): {e}")

    # ---------------------------------------------------------- publishing

    def set_publishing(self, on: bool, flush: bool = False) -> dict:
        """The Stations panel's Publishing switch. Off: reads are still
        heard, classified and backed up, but nothing is sent to FinishLynx
        and no merged .lif is written (warm-ups, tag checks, a test start).
        On again: the .lif files catch up by themselves; reads kept out of
        the LapTime feed meanwhile are sent only with flush=True (the
        operator is asked - warm-up crossings must not become finishes)."""
        on = bool(on)
        was = self.publishing
        self.publishing = on
        out = {"publishing": on, "sent": 0, "kept_back": 0}
        if on:
            self._publish_off_at = None
            if was != on:
                logger.info("Publishing ON")
            if flush:
                out["sent"] = self._flush_unsent("off")
            elif self.race_manager:
                # Declined: the reads placed in panes meanwhile stay out of
                # FinishLynx for good (they remain in the panes and the
                # .lif). Held ones keep their mark, so a read the operator
                # later places by hand still goes (send_restored_read).
                for r in self.race_manager.unsent_reads("off"):
                    self.race_manager.mark_sent(r.id)
                    logger.info(f"Read {r.id}: bib {r.bib} at {r.point} left out of FinishLynx (publishing was off)")
            out["kept_back"] = len(self.race_manager.unsent_reads("off")) if self.race_manager else 0
            try:
                self.refresh_merged_lifs()
            except Exception:
                logger.exception("Merged .lif refresh after publishing on failed")
        else:
            if was != on:
                self._publish_off_at = datetime.now()
                logger.warning("Publishing OFF - nothing goes to FinishLynx or the .lif files until it is switched on")
        try:
            from . import web
            web.emit_races_update()
        except Exception:
            pass
        return out

    @property
    def _notes(self) -> Dict[int, str]:
        return self.__dict__.setdefault("_flushed_notes", {})

    def _flush_unsent(self, why: str) -> int:
        """Send the assigned reads that were kept out of the LapTime feed for
        `why` ("early": a finish the panes first held as too early and
        then took; "off": publishing was off). Finishes go film-corrected
        with the offset in force now; splits only under feed "all". A
        split is skipped when the athlete's finish already went (LapTime
        fills lap slots in arrival order). Returns how many were sent."""
        mgr = self.race_manager
        if not mgr or not self.finishlynx:
            return 0
        sent = 0
        feed_all = getattr(self.config.finishlynx, "feed", "finish") == "all"
        for read in mgr.unsent_reads(why):
            if mgr.is_finish_read(read):
                # Same rule as at ingest: FinishLynx files a LapTime read under
                # the event it has open, so a finish placed (by hand, from Held)
                # in any other race stays out - MeetPro gets it through the
                # pane's "Save to .lif" instead (2026-09-30).
                other = self._finish_for_another_race(SimpleNamespace(
                    bib=read.bib, pane_state=read.state, pane_race=read.race))
                if other:
                    if read.unsent != "other":
                        mgr.mark_unsent(read.id, "other")
                    self._notes[read.id] = f"not sent to FinishLynx: {other}"
                    logger.info(f"Read {read.id}: finish kept out of FinishLynx - {other}")
                    continue
                corr = mgr.film_correction(read, read.race) if not read.manual else 0.0
                self._send_or_queue_finishlynx(read.bib, read.eff_ts - timedelta(seconds=corr))
                mgr.mark_sent(read.id, corr)
                self._notes[read.id] = f"sent to FinishLynx as bib {read.bib}"
                logger.info(f"Read {read.id}: bib {read.bib} finish ({why}) sent to FinishLynx now")
                sent += 1
                continue
            if not feed_all:
                mgr.mark_sent(read.id)          # splits never go under feed "finish"
                continue
            finished = any(r.state == "assigned" and r.race == read.race and r.bib == read.bib
                           and r.id != read.id and mgr.is_finish_read(r)
                           for r in mgr.reads.values())
            if finished:
                mgr.mark_sent(read.id)
                self._notes[read.id] = (f"not sent to FinishLynx: bib {read.bib} already has a "
                                                f"finish there and LapTime fills laps in arrival order")
                logger.warning(f"Read {read.id} ({read.point}) {self._notes[read.id]}")
                continue
            self._send_or_queue_finishlynx(read.bib, read.eff_ts)
            mgr.mark_sent(read.id)
            self._notes[read.id] = f"sent to FinishLynx as bib {read.bib}"
            logger.info(f"Read {read.id}: bib {read.bib} at {read.point} ({why}) sent to FinishLynx now")
            sent += 1
        return sent

    def _feeds_finishlynx(self, reader_id: str, tag: Optional[TagRead] = None) -> bool:
        """Should this read go to FinishLynx's LapTime plug-in?

        finishlynx.feed "finish": only the finish-line reader(s) - the
        LapTime packet has no split number and FinishLynx fills lap slots in
        arrival order, so a Mile 1 read can become a final time. Splits go
        to MeetPro through the .lif instead.

        "all": finish reads as above, plus every split read a race pane
        accepted. Reads the panes held or ignored (wrong gender for the open
        race, not on the roster, too early, reader not in use) stay out:
        on 2026-09-26 boys warming up over the Mile 1 mat during a girls'
        race all landed in FinishLynx because the LapTime path never asked
        the panes. Finish reads are not gated by the panes - FinishLynx is
        the record for finishes and must get them even when nobody opened a
        pane - with one exception: a finish the panes hold as TOO EARLY
        (a loop-course mat crossed mid-race, a tester) is kept back, since
        with Total Laps 1 it would become the athlete's final time; it goes
        the moment a pane takes it (_flush_unsent). So is a crossing that is
        not the one counting at its station (Reads before / after, a repeat,
        a walk back - 2026-10-09). Publishing off keeps everything back
        (set_publishing).
        """
        read_id = getattr(tag, "read_id", None) if tag is not None else None
        if not getattr(self, "publishing", True):
            if read_id is not None and self.race_manager and (
                    reader_id == "USER" or self.reader_is_finish(reader_id)
                    or getattr(self.config.finishlynx, "feed", "finish") == "all"):
                self.race_manager.mark_unsent(read_id, "off")
            logger.info(f"Read {getattr(tag, 'bib', '?')} at {reader_id}: not sent to FinishLynx - publishing is off")
            return False
        if reader_id == "USER":
            return True
        if self.reader_is_finish(reader_id):
            if tag is not None and getattr(tag, "pane_crossing", ""):
                # Not the crossing that counts at this station (2026-10-09:
                # a loop crossing skipped by Reads before, the same crossing
                # read again, a walk back after the finish): FinishLynx gets
                # one read per runner, like the pane. It goes if the
                # numbering later makes it the one that counts, or the
                # operator places it (_flush_unsent).
                if read_id is not None and self.race_manager:
                    self.race_manager.mark_unsent(read_id, "early")
                logger.info(f"Read {tag.bib} at {reader_id}: finish not sent to FinishLynx - "
                            f"{getattr(tag, 'pane_reason', '') or 'not the crossing that counts'}")
                return False
            if tag is not None and getattr(tag, "pane_early", False):
                if read_id is not None and self.race_manager:
                    self.race_manager.mark_unsent(read_id, "early")
                logger.info(f"Read {tag.bib} at {reader_id}: finish not sent to FinishLynx - "
                            f"held by the panes ({getattr(tag, 'pane_reason', '') or 'too early'}); "
                            f"it goes if a pane takes it")
                return False
            other = self._finish_for_another_race(tag) if tag is not None else None
            if other:
                if read_id is not None and self.race_manager:
                    self.race_manager.mark_unsent(read_id, "other")
                logger.warning(f"Read {tag.bib} at {reader_id}: finish not sent to FinishLynx - {other}")
                return False
            return True
        if getattr(self.config.finishlynx, "feed", "finish") != "all":
            return False
        state = getattr(tag, "pane_state", None) if tag is not None else None
        if state is None:
            return True                  # panes off: every read, as before
        if state == "assigned":
            return True
        logger.info(f"Read {tag.bib} at {reader_id}: not sent to FinishLynx - "
                    f"{state} by the panes ({getattr(tag, 'pane_reason', '') or 'no reason'})")
        return False

    def _race_start_time(self, race) -> Optional[datetime]:
        """A pane's scheduled start from the open meet (meet.json), or None."""
        meet = self.meet_service.meet if self.meet_service else None
        mr = meet.race(race.event, race.round or 1, race.heat or 1) if meet else None
        if not mr or not mr.start_time:
            return None
        try:
            day = date.fromisoformat(meet.date)
        except (TypeError, ValueError):
            day = date.today()
        hh, mm = mr.start_time.split(":")
        return datetime.combine(day, dtime(int(hh), int(mm)))

    CHECKIN_TICK_SEC = 5.0

    def _checkin_loop(self):
        while True:
            time.sleep(self.CHECKIN_TICK_SEC)
            try:
                if self.race_manager:
                    self.race_manager.tick_checkins()
            except Exception:
                logger.exception("Check-in timer failed")

    def finishlynx_open_event(self):
        """(event, round, heat, name) from FinishLynx's window title, or None
        (fl_window.py) - the scoreboard feed on this rig never names it.
        Only the real console looks (not preview, not tests): the desktop's
        FinishLynx must not steer a process that does not feed it."""
        if not getattr(self, "watch_finishlynx_window", False):
            return None
        from . import fl_window
        return fl_window.open_event()

    def finishlynx_event(self) -> int:
        """Event number FinishLynx is timing: the scoreboard feed's when it
        names one, else the open event in FinishLynx's window; 0 = unknown."""
        sb = getattr(self, "scoreboard_listener", None)
        armed = int(getattr(sb, "event", 0) or 0) if sb else 0
        if not armed:
            win = self.finishlynx_open_event()
            armed = win[0] if win else 0
        return armed

    def _finish_for_another_race(self, tag: TagRead) -> Optional[str]:
        """Why this finish-line read must NOT go to FinishLynx, or None.

        LapTime packets carry no event: FinishLynx files every one under the
        event it has armed. On 2026-09-30 a Men's 5000 straggler (bib 1190,
        61 min) crossed while the Women's 5000 ran; the panes put him in the
        closed Men's race, but the read still went to FinishLynx and landed
        in the girls' results. So a finish goes only when it belongs to the
        race being timed now:
          - placed in a race FinishLynx does not have armed (event known
            from the scoreboard feed), or in a race that is not running
            while another one is -> kept out;
          - not placed (held/ignored), and the athlete's gender (from the
            .ppl) fits none of the running races -> kept out.
        The armed event comes from the scoreboard feed or, when that says
        nothing (as on this rig), FinishLynx's window title - on 9/30 both
        races were still running (the Men's kept open for stragglers), so
        only the open event could tell them apart.
        Panes off, nothing running, an unknown athlete: sent as before
        (FinishLynx is the record for finishes). Kept-out reads stay in their
        pane and the merged .lif; the pane flags them "not in FinishLynx"."""
        mgr = self.race_manager
        if not mgr or getattr(tag, "pane_state", None) is None:
            return None
        racing = [r for r in list(mgr.races.values()) if r.status == "racing" and r.gun]
        armed = self.finishlynx_event()
        own =mgr.races.get(getattr(tag, "pane_race", None)) if getattr(tag, "pane_state", None) == "assigned" else None
        if own is not None:
            if armed and own.event and own.event != armed:
                return (f"bib {tag.bib} belongs to {own.name} (event {own.event}) but FinishLynx has "
                        f"event {armed} armed; it stays in {own.name}'s pane and .lif")
            others = [r for r in racing if r.key != own.key]
            if own.status == "racing" or not others:
                return None
            return (f"bib {tag.bib} finished in {own.name} ({own.status}) while "
                    f"{', '.join(r.name for r in others)} is running - FinishLynx would file it there; "
                    f"it stays in {own.name}'s pane and .lif")
        if not racing:
            return None
        athlete = mgr.athlete_lookup(tag.bib) or {}
        g = str(athlete.get("gender") or "").upper()[:1]
        if g in ("M", "F") and all(r.gender in ("M", "F") and r.gender != g for r in racing):
            return (f"bib {tag.bib} ({g}) fits none of the running races "
                    f"({', '.join(r.name for r in racing)}); held in the console")
        return None

    def _send_or_queue_finishlynx(self, identifier: str, ts: datetime):
        """Send one LapTime packet, or queue it while FinishLynx is away."""
        event_num, round_num, heat_num = self._get_current_event_info()
        # A send can be the one that finds the link broken: queue it too, not
        # only reads that arrive once it is known to be down (2026-10-04 review)
        if self.finishlynx.is_connected and \
                self.finishlynx.send_split(identifier, ts, event_num, round_num, heat_num):
            return
        with self._pending_lock:
            self._pending_sends.append({
                'bib': identifier, 'timestamp': ts,
                'event': event_num, 'round': round_num, 'heat': heat_num
            })
            logger.debug(f"Queued {identifier} for FinishLynx (not sent, {len(self._pending_sends)} pending)")

    def send_restored_read(self, read_id: int) -> Optional[str]:
        """The operator put a held/ignored read into a race by hand. With
        feed "all" a split read that was kept out at ingest goes to
        FinishLynx now, so FinishLynx matches the pane - unless the athlete's
        finish is already recorded: LapTime fills lap slots in arrival
        order, so a split sent after the finish would become the final
        time. Returns a note for the console, or None when nothing applies."""
        mgr = self.race_manager
        if not mgr or not self.finishlynx:
            return None
        # A read kept out of the feed at ingest (too-early finish, publishing
        # off) that the operator has now placed: it went in the flush that
        # the assignment triggered (_on_races_changed), or goes now.
        note = self._notes.pop(read_id, None)
        if note:
            return note
        read = mgr.reads.get(read_id)
        if read is None or read.state != "assigned" or read.manual:
            return None
        if read.unsent:
            self._flush_unsent(read.unsent)
            return self._notes.pop(read_id, None)
        if getattr(self.config.finishlynx, "feed", "finish") != "all":
            return None
        if mgr.is_finish_read(read):
            return None                  # finishes always went at ingest
        finished = any(r.state == "assigned" and r.race == read.race and r.bib == read.bib
                       and r.id != read.id and mgr.is_finish_read(r)
                       for r in mgr.reads.values())
        if finished:
            note = (f"not sent to FinishLynx: bib {read.bib} already has a finish there and "
                    f"LapTime fills laps in arrival order")
            logger.warning(f"Restored read {read.id} ({read.point}) {note}")
            return note
        self._send_or_queue_finishlynx(read.bib, read.eff_ts)
        logger.info(f"Restored read {read.id}: bib {read.bib} at {read.point} sent to FinishLynx")
        return f"sent to FinishLynx as bib {read.bib}"

    def reader_is_finish(self, reader_id: str) -> bool:
        """Does this reader stand at the finish line? Decided by what its
        reads count as (races.reader_points), not by its id: a split
        station mapped to the finish point is the finish line, and the
        finish mat mapped to a split is not."""
        finish = set(self.config.races.finish_readers) | {"FINISH"}
        pts = self.config.races.reader_points.get(reader_id)
        if pts:
            return any(p in finish for p in pts)      # ["off"] -> False
        return reader_id in finish

    def reader_role(self, reader_id: str) -> str:
        """What this reader's reads count as, for log lines: 'the finish',
        'Mile 1', 'not in use' ..."""
        cfg = getattr(self, "config", None)          # bare test doubles have none
        if cfg is None:
            return "the finish"
        pts = cfg.races.reader_points.get(reader_id)
        if pts == ["off"]:
            return "not in use"
        if self.reader_is_finish(reader_id):
            return "the finish"
        labels = self.config.races.point_labels
        return ", then ".join("not a split" if p == "-" else labels.get(p, p) for p in (pts or [reader_id]))

    def finish_reader_ids(self) -> List[str]:
        """Every reader id (local mat, split stations) whose reads are
        finishes right now; the console warns when it is empty."""
        rp = self.config.races.reader_points
        ids = [r for r in rp if self.reader_is_finish(r)]
        ids += [p for p in sorted(set(self.config.races.finish_readers) | {"FINISH"})
                if p not in rp and p not in ids]
        return ids

    def _on_tag_read_current_reads(self, tag: TagRead):
        """Handle tag read in Current Reads mode (passthrough behavior).

        This is the original behavior: process through validation/relay logic
        and send everything to FinishLynx.

        Args:
            tag: Tag read data.
        """
        # Check relay mode processing first
        identifier, should_send, extra_data = self._get_finishlynx_identifier(tag.bib, tag.timestamp)

        if not should_send:
            # Read should be ignored (e.g., retired bib in relay)
            logger.debug(f"Ignoring read for bib {tag.bib}: {extra_data.get('reason', 'filtered')}")
            return

        read_data = self._track_lap(tag.bib, tag.timestamp, tag.reader_id)

        # Merge extra data (relay info) into read_data
        if extra_data:
            read_data['relay_info'] = extra_data

        # Send to FinishLynx (queue if disconnected)
        # For relays, 'identifier' is the lane number; otherwise it's the bib
        # Include event/round/heat for exact matching in FinishLynx
        fl_ts = getattr(tag, "fl_timestamp", None) or tag.timestamp
        if self.finishlynx and self._feeds_finishlynx(tag.reader_id, tag):
            self._send_or_queue_finishlynx(identifier, fl_ts)

        # Notify web interface
        if self._on_tag_read_callback:
            self._on_tag_read_callback(tag.bib, tag.timestamp, tag.reader_id,
                                       read_data.get('lap_number'), read_data.get('position'),
                                       dict(extra_data or {}, split_point=getattr(tag, 'point', None)))

        if extra_data.get('team_name'):
            logger.info(f"Tag read: Bib {tag.bib} -> {extra_data['team_name']} Lane {identifier} at {read_data['time']}")
        else:
            logger.info(f"Tag read: Bib {tag.bib} at {read_data['time']} (Lap {read_data['lap_number']})")

    def _on_tag_read_finishlynx_mode(self, tag: TagRead):
        """Handle tag read in FinishLynx mode (smart processing).

        In this mode:
        - Check running time > 5.0 seconds before processing
        - Track events: lookup bib -> lane number
        - XC events: gender filter, send bib if matches

        Args:
            tag: Tag read data.
        """
        # Import web module for event data access
        from . import web

        # Note: FinishLynx's built-in Start Mask handles ignoring reads at race start
        # We just need to filter by event/heat (only allow bibs entered in current race)

        # Get current event info
        event_info = web.get_current_heat_info()

        # Determine event type and process
        if self._is_xc_event(event_info):
            self._process_xc_read(tag, event_info)
        else:
            self._process_track_read(tag, event_info)

    def _is_xc_event(self, event_info: Optional[Dict]) -> bool:
        """Check if current event is a cross-country event."""
        if not event_info:
            return False
        event_name = event_info.get('event_name', '').lower()
        return 'xc' in event_name or 'cross' in event_name or 'country' in event_name

    def _process_xc_read(self, tag: TagRead, event_info: Optional[Dict]):
        """Process XC event read.

        If the event has entries in the .evt file, only process bibs in those entries.
        Otherwise, fall back to gender filtering only.
        Sends bib numbers (not lanes).

        Args:
            tag: Tag read data.
            event_info: Current heat/event info.
        """
        from . import web

        # Get athlete info
        athlete = web.get_athlete_info(tag.bib)

        # Check if event has entries loaded from .evt file
        event_bibs = set()
        if event_info:
            event_bibs = set(event_info.get('bibs', []))

        if event_bibs:
            # Entries exist - only process bibs that are in the entries
            if tag.bib not in event_bibs:
                logger.debug(f"XC: Ignoring bib {tag.bib} - not in event entries")
                return
        else:
            # No entries loaded - fall back to gender filtering
            event_gender = event_info.get('gender', '') if event_info else ''
            if event_gender and athlete:
                athlete_gender = athlete.get('gender', '')
                if athlete_gender and athlete_gender != event_gender:
                    logger.debug(f"XC: Bib {tag.bib} gender mismatch ({athlete_gender} vs event {event_gender})")
                    return

        # Send bib number for XC (no lane lookup)
        self._send_and_track_finishlynx_read(tag, tag.bib, athlete)

    def _process_track_read(self, tag: TagRead, event_info: Optional[Dict]):
        """Process track event read with validation.

        For relay events: sends lane number to FinishLynx.
        For individual events: sends bib number to FinishLynx (after validating bib is in heat).

        Args:
            tag: Tag read data.
            event_info: Current heat/event info.
        """
        from . import web

        # Check if this is a relay event
        if event_info and event_info.get('is_relay'):
            # Relay event - use lane as identifier
            relay_result = web.process_relay_read(tag.bib, tag.timestamp)
            if relay_result.get('ignore'):
                logger.debug(f"Track relay: Ignoring bib {tag.bib}: {relay_result.get('reason')}")
                return
            identifier = str(relay_result.get('lane') or tag.bib)
            athlete = web.get_athlete_info(tag.bib)
        else:
            # Individual track event - use bib as identifier (not lane)
            # Lane lookup is only used to validate bib is in current heat
            lane = self._lookup_lane_for_bib(tag.bib, event_info)
            if lane:
                # Bib is in current heat - send bib (not lane) for individual events
                identifier = tag.bib
            elif event_info and event_info.get('bib_lanes'):
                # Heat info exists with entries, but this bib not in it - filter out
                logger.debug(f"Track: Ignoring bib {tag.bib} - not in current heat entries (heat has {len(event_info.get('bib_lanes', {}))} entries)")
                return
            elif web.has_evt_loaded() and web.has_current_event():
                # .evt is loaded and the scoreboard named an event that is not
                # in it - filter out. (Only when an event was actually named:
                # with no announcement yet we must pass everything through.)
                logger.debug(f"Track: Ignoring bib {tag.bib} - announced event/heat not in .evt")
                return
            else:
                # No .evt loaded at all - pass bib through as fallback
                logger.debug(f"Track: No .evt loaded - passing bib {tag.bib} through unfiltered")
                identifier = tag.bib
            athlete = web.get_athlete_info(tag.bib)

        self._send_and_track_finishlynx_read(tag, identifier, athlete)

    def _lookup_lane_for_bib(self, bib: str, event_info: Optional[Dict]) -> Optional[str]:
        """Look up lane number for a bib in current heat.

        Args:
            bib: Bib number.
            event_info: Heat/event info with lane assignments.

        Returns:
            Lane number as string, or None if not found.
        """
        from . import web
        return web.get_lane_for_bib(bib)

    def _get_current_event_info(self) -> tuple:
        """Get current event/round/heat from scoreboard listener.

        Returns:
            Tuple of (event_num, round_num, heat_num) or (None, None, None) if not available.
        """
        if self.scoreboard_listener:
            return (
                self.scoreboard_listener.event or None,
                self.scoreboard_listener.round or None,
                self.scoreboard_listener.heat or None
            )
        return (None, None, None)

    def _send_and_track_finishlynx_read(self, tag: TagRead, identifier: str, athlete: Optional[Dict]):
        """Send read to FinishLynx and track for race results.

        Args:
            tag: Original tag read.
            identifier: What to send to FinishLynx (bib or lane).
            athlete: Athlete info if available.
        """
        # Track lap for display
        read_data = self._track_lap(tag.bib, tag.timestamp, tag.reader_id)

        # Store for race results (for LIF generation on event change)
        with self._race_reads_lock:
            self._race_reads.append({
                'bib': tag.bib,
                'identifier': identifier,
                'timestamp': tag.timestamp,
                'time': read_data['time'],  # TOD
                'athlete': athlete,
            })

        # Send to FinishLynx with event/round/heat for exact matching
        event_num, round_num, heat_num = self._get_current_event_info()
        fl_ts = getattr(tag, "fl_timestamp", None) or tag.timestamp
        if self.finishlynx and self._feeds_finishlynx(tag.reader_id, tag):
            self._send_or_queue_finishlynx(identifier, fl_ts)

        # Notify web interface
        if self._on_tag_read_callback:
            self._on_tag_read_callback(tag.bib, tag.timestamp, tag.reader_id,
                                       read_data.get('lap_number'), read_data.get('position'),
                                       {'identifier': identifier, 'athlete_info': athlete,
                                        'split_point': getattr(tag, 'point', None)})

        logger.info(f"FinishLynx mode: Bib {tag.bib} -> ID {identifier} at {read_data['time']} (E{event_num}R{round_num}H{heat_num})")

    def set_operation_mode(self, mode: str) -> bool:
        """Set operation mode.

        Args:
            mode: Either "current_reads" or "finishlynx"

        Returns:
            True if mode was changed successfully
        """
        if mode not in ("current_reads", "finishlynx"):
            logger.warning(f"Invalid operation mode: {mode}")
            return False

        # Don't allow mode change during manual race
        if self.manual_race_in_progress:
            logger.warning("Cannot change mode: manual race in progress")
            return False

        old_mode = self.operation_mode
        self.operation_mode = mode

        # Clear race reads when switching modes
        if old_mode != mode:
            with self._race_reads_lock:
                self._race_reads.clear()

        logger.info(f"Operation mode set to: {mode}")
        return True

    def _handle_event_change(self, old_event: int, old_heat: int, new_event: int, new_heat: int):
        """Handle event/heat change from FinishLynx.

        Called when FinishLynx navigates to a different event or heat.
        Auto-saves race results as LIF file and clears the UI read log.

        Args:
            old_event: Previous event number.
            old_heat: Previous heat number.
            new_event: New event number.
            new_heat: New heat number.
        """
        from . import web

        # Sync the middleware's current heat with FinishLynx
        # This allows bib filtering to work based on what's loaded in FinishLynx
        web.set_current_heat(str(new_event), str(new_heat))
        logger.info(f"Event change: {old_event}/{old_heat} -> {new_event}/{new_heat}")

        # Only do additional processing in FinishLynx mode
        if self.operation_mode != "finishlynx":
            logger.debug(f"Event change ignored for LIF save: not in FinishLynx mode")
            return

        # Skip if manual race is in progress (user needs to decide)
        if self.manual_race_in_progress:
            logger.info(f"Event change detected but manual race in progress - user must save/clear first")
            return

        # Get reads to save
        with self._race_reads_lock:
            reads_to_save = list(self._race_reads)
            self._race_reads.clear()

        # Reset deduplication state for new race
        if self.reader:
            self.reader.reset_dedup()

        # Save the LIF file if there are reads
        if reads_to_save:
            logger.info(f"Event change: auto-saving {len(reads_to_save)} reads for Event {old_event} Heat {old_heat}")
            self._save_race_lif(old_event, old_heat, reads_to_save)

        # Clear the UI read log for the new event
        self.clear_reads()

    def _handle_tdp_disconnect(self):
        """Handle TDP (FinishLynx) disconnection.

        Note: This is for the LapTime connection, not the scoreboard.
        We keep reading bibs even if LapTime is down (RFID as backup).
        """
        logger.info("FinishLynx LapTime disconnected - continuing to read bibs as backup")

    def _save_race_lif(self, event_num: int, heat_num: int, reads: List[Dict]):
        """Save race results as LIF file.

        Args:
            event_num: Event number.
            heat_num: Heat number.
            reads: List of read dictionaries with bib, identifier, timestamp, time.
        """
        from . import web

        # Get meet directory
        meet_dir = web.get_meet_directory()
        if not meet_dir:
            logger.warning("Cannot save LIF: no meet directory set")
            return

        # Create RFID Events subdirectory
        output_dir = os.path.join(meet_dir, "RFID Events")
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create RFID Events directory: {e}")
            return

        # Get event details and participant data
        event_details = web.get_event_info(event_num, heat_num)
        participant_data = web.get_participant_data()

        # Get round number from event details or scoreboard
        round_num = 1
        if event_details:
            round_num = event_details.get('round', 1)
        elif self.scoreboard_listener:
            round_num = self.scoreboard_listener.round or 1

        # Generate LIF content - pass actual event/round/heat from scoreboard
        lif_content = generate_lif(
            event_details=event_details,
            participant_data=participant_data,
            reads=reads,
            start_time=None,  # Will be calculated from running_time data
            event_num_override=event_num,
            round_num_override=round_num,
            heat_num_override=heat_num,
        )

        # Generate filename and write (format: event-round-heat)
        filename = generate_filename(event_num, round_num, heat_num)
        filepath = os.path.join(output_dir, filename)

        try:
            with open(filepath, 'w', newline='') as f:
                f.write(lif_content)
            logger.info(f"Saved race results to {filepath} ({len(reads)} reads)")
        except IOError as e:
            logger.error(f"Failed to write LIF file: {e}")

    def save_manual_race(self, event_num: int, heat_num: int) -> bool:
        """Save pending race reads after TDP disconnect.

        Called by user to save race results when TDP disconnected mid-race.

        Args:
            event_num: Event number for the race.
            heat_num: Heat number for the race.

        Returns:
            True if saved successfully.
        """
        if not self.manual_race_in_progress:
            logger.warning("No manual race in progress to save")
            return False

        # Get and clear reads
        with self._race_reads_lock:
            reads_to_save = list(self._race_reads)
            self._race_reads.clear()

        if not reads_to_save:
            logger.warning("No reads to save for manual race")
            self.manual_race_in_progress = False
            return False

        # Save LIF
        self._save_race_lif(event_num, heat_num, reads_to_save)

        # Clear flag
        self.manual_race_in_progress = False
        logger.info(f"Manual race saved: Event {event_num}, Heat {heat_num}")
        return True

    def clear_manual_race(self) -> bool:
        """Discard pending race reads after TDP disconnect.

        Called by user to discard race results when TDP disconnected mid-race.

        Returns:
            True if cleared successfully.
        """
        if not self.manual_race_in_progress:
            logger.warning("No manual race in progress to clear")
            return False

        # Clear reads
        with self._race_reads_lock:
            count = len(self._race_reads)
            self._race_reads.clear()

        # Clear flag
        self.manual_race_in_progress = False
        logger.info(f"Manual race cleared: {count} reads discarded")
        return True

    def process_accepted_bib(self, bib: str, timestamp):
        """Process a bib that was accepted by user after being pending.

        This sends the read to FinishLynx and tracks the lap.

        Args:
            bib: The accepted bib number.
            timestamp: The original timestamp of the read.
        """
        from . import web

        # Determine identifier based on operation mode
        if self.operation_mode == "finishlynx":
            event_info = web.get_current_heat_info()
            if self._is_xc_event(event_info):
                # XC event - use bib
                identifier = bib
            elif event_info and event_info.get('is_relay'):
                # Relay event - use lane
                lane = self._lookup_lane_for_bib(bib, event_info)
                if lane:
                    identifier = str(lane)
                else:
                    logger.warning(f"Accepted bib: No lane assignment for bib {bib} in relay - sending bib instead")
                    identifier = bib
            else:
                # Individual track event - use bib (not lane)
                identifier = bib
            extra_data = {}
        else:
            # Current reads mode - use existing relay/validation logic
            identifier, should_send, extra_data = self._get_finishlynx_identifier(bib, timestamp)
            if not should_send:
                # Still filtered (shouldn't happen if properly accepted)
                logger.warning(f"Accepted bib {bib} still filtered: {extra_data.get('reason')}")
                return

        # Track the lap
        read_data = self._track_lap(bib, timestamp, "ACCEPTED")

        # Merge extra data
        if extra_data:
            read_data['relay_info'] = extra_data

        # Track for LIF generation in FinishLynx mode
        if self.operation_mode == "finishlynx":
            with self._race_reads_lock:
                self._race_reads.append({
                    'bib': bib,
                    'identifier': identifier,
                    'timestamp': timestamp,
                    'time': read_data['time'],
                })

        # Send to FinishLynx with event/round/heat (an accepted read is a
        # finish-mat read: film-corrected like any other)
        fl_ts = timestamp - timedelta(seconds=self.film_correction())
        if self.finishlynx:
            self._send_or_queue_finishlynx(identifier, fl_ts)
            logger.info(f"Accepted bib {bib} (ID {identifier}) for FinishLynx")

    def start(self) -> bool:
        """Start reading.

        Returns:
            True if started successfully.
        """
        logger.info("=== Application.start() called ===")
        logger.info(f"start: Current state - _running={self._running}, mode={self.config.mode}")
        if self._running:
            logger.info("start: Already running, returning True")
            return True

        # Reload .evt file if configured (ensures event data is loaded after restart)
        if self.config.event_directory and self.config.evt_file:
            try:
                from . import web
                import pathlib
                evt_path = pathlib.Path(web.start_list_dir(self.config.event_directory)) / self.config.evt_file
                if evt_path.exists():
                    logger.info(f"start: Reloading .evt file from {evt_path}")
                    content = evt_path.read_text(encoding='utf-8', errors='replace')
                    # Parse the .evt file to populate event data
                    heats = web.parse_all_heat_entries(content)
                    with web._event_data_lock:
                        web._event_data['heats'] = heats
                    logger.info(f"start: Loaded {len(heats)} heats from .evt file")
                    # Race panes are restored from races-state.json, never
                    # auto-created here: the operator picks which .evt races
                    # to time from the console's "Races from .evt" picker.
                else:
                    logger.warning(f"start: Configured .evt file not found: {evt_path}")
            except Exception as e:
                logger.error(f"start: Error reloading .evt file: {e}")

        if not self.hardware_allowed:
            # Web UI only. No reader, clocks, radio, camera or FinishLynx.
            logger.warning(f"start: PREVIEW MODE ({self.preview_reason}) - "
                           "no reader, clocks, radio, camera or FinishLynx link")
            self._running = True
            if not self._supervisor_thread or not self._supervisor_thread.is_alive():
                self._supervisor_stop.clear()
                self._supervisor_thread = threading.Thread(
                    target=self._hardware_supervisor_loop, daemon=True)
                self._supervisor_thread.start()
            return True

        # Initialize FinishLynx output. Finish line only: a split station
        # never talks to FinishLynx itself - its reads travel by radio to
        # the finish console - so it neither connects nor keeps retrying.
        if self._uses_finishlynx():
            logger.info("start: Initializing FinishLynx output")
            if not self.finishlynx:
                logger.info("start: Creating new FinishLynxOutput instance")
                self.finishlynx = FinishLynxOutput(self.config.finishlynx)

            logger.info(f"start: FinishLynx is_connected={self.finishlynx.is_connected}")
            if not self.finishlynx.is_connected:
                logger.info("start: Attempting to connect to FinishLynx")
                if self.finishlynx.connect():
                    logger.info("start: Successfully connected to FinishLynx")
                    self._flush_pending_sends()
                else:
                    logger.warning("Failed to connect to FinishLynx - continuing anyway")
        else:
            logger.info("start: split station - FinishLynx output not used")

        # Initialize RFID reader
        logger.info(f"start: Mode is '{self.config.mode}'")
        if self.config.mode == "finish_line":
            # Initialize and start Scoreboard Listener (receives event/heat info from FinishLynx)
            logger.info(f"start: Initializing Scoreboard Listener, listener exists={self.scoreboard_listener is not None}")
            if not self.scoreboard_listener:
                logger.info(f"start: Creating ScoreboardListener - host={self.config.scoreboard.host}, port={self.config.scoreboard.port}")
                self.scoreboard_listener = ScoreboardListener(
                    self.config.scoreboard.host,
                    self.config.scoreboard.port
                )
            logger.info("start: Starting Scoreboard Listener")
            self.scoreboard_listener.start()
            logger.info("start: Scoreboard Listener started")

            # Wire up event change callbacks for FinishLynx mode
            logger.info("start: Setting up event change callbacks")
            self.scoreboard_listener.set_on_event_change_callback(self._handle_event_change)
            self.scoreboard_listener.set_on_tdp_disconnect_callback(self._handle_tdp_disconnect)
            self.scoreboard_listener.set_on_running_time_callback(self._handle_running_time)

            logger.info(f"start: Initializing RFID reader, reader exists={self.reader is not None}")
            if not self.reader:
                logger.info(f"start: Creating new RFIDReader instance - host={self.config.reader.host}, port={self.config.reader.port}")
                self.reader = create_reader(
                    self.config.reader,
                    self.config.timing,
                    on_tag_read=self._on_tag_read,
                    reader_id="FINISH",
                )
                logger.info("start: RFIDReader instance created")

            logger.info(f"start: RFID reader is_connected={self.reader.is_connected}")
            if not self.reader.is_connected:
                logger.info("start: Attempting to connect to RFID reader")
                if self.reader.connect():
                    logger.info("start: Successfully connected to RFID reader")
                else:
                    # Non-fatal: the hardware supervisor keeps retrying, so
                    # the box comes up in whatever order its parts power on.
                    logger.warning("RFID reader not responding yet; supervisor will keep trying")

            # Initialize LoRa receiver if enabled
            if self.config.lora.enabled:
                if not self.lora_receiver:
                    self.lora_receiver = LoRaReceiver(
                        self.config.lora,
                        on_message=self._on_lora_message,
                        on_management_message=self._on_management_message,
                        journal_path="lora-reads-received.log",
                    )
                if not self.lora_receiver.is_running:
                    self.lora_receiver.start()

                # Start remote manager after receiver is running
                if not self.remote_manager and self.lora_receiver.transport:
                    self.remote_manager = RemoteManager(
                        self.lora_receiver.transport,
                        self.config.lora,
                        on_unit_change=self._on_remote_unit_change,
                    )
                    self.remote_manager.start()

        elif self.config.mode == "remote_lap":
            # Initialize LoRa transport for sending
            if not self.lora_transport:
                self.lora_transport = LoRaTransport(
                    self.config.lora,
                    on_receive=self._on_remote_lap_receive,
                )

            if not self.lora_transport.is_connected:
                if not self.lora_transport.connect():
                    # Non-fatal: the transport reconnects in the background,
                    # so a station that boots before its radio still comes up.
                    logger.warning("LoRa link not up yet; retrying in background")

            # Start remote agent for management message handling
            if not self.remote_agent:
                self.remote_agent = RemoteAgent(self, self.lora_transport)
                self.remote_agent.start()

            # Reliable read delivery (retransmit until finish line ACKs)
            if not self.read_sender:
                self.read_sender = ReliableReadSender(
                    self.lora_transport, self.config.lora.reader_id,
                    journal_path="lora-reads-sent.log",
                )
            self.read_sender.start()

            if not self.reader:
                self.reader = create_reader(
                    self.config.reader,
                    self.config.timing,
                    on_tag_read=self._on_remote_tag_read,
                    reader_id=self.config.lora.reader_id,
                )

            if not self.reader.is_connected:
                if not self.reader.connect():
                    # Non-fatal: the hardware supervisor keeps retrying, so
                    # the box comes up in whatever order its parts power on.
                    logger.warning("RFID reader not responding yet; supervisor will keep trying")

        # LED race clock: finish console shows the current race's gun clock,
        # split stations show the gun received over LoRa
        if self.config.led_clock.enabled and not self.led_clock_service:
            from .led_clock import LEDClockService
            get_gun = self._current_gun if self.config.mode == "finish_line" \
                else (lambda: self.remote_gun)
            # Never probe the LoRa gateway's COM port as if it were a clock.
            exclude = []
            if self.config.lora.enabled and self.config.lora.port and ":" not in self.config.lora.port:
                exclude.append(self.config.lora.port)
            self.led_clock_service = LEDClockService(
                get_gun, port=self.config.led_clock.port,
                clock_type=self.config.led_clock.type,
                clocks=self.config.led_clock.clocks,       # mutated in place, saved below
                exclude_ports=exclude,
                on_change=lambda entries: self._save_clock_entries(),
                hour12=(self.config.led_clock.hour_format == "12"))
        if self.led_clock_service:
            self.led_clock_service.start()

        # Start gun straight from the camera (finish line only)
        if self.config.mode == "finish_line" and self.config.camera_start.enabled:
            if not self.camera_start:
                cs = self.config.camera_start
                self.camera_start = CameraStartListener(
                    camera_host=cs.camera_host, camera_port=cs.camera_port,
                    interface=cs.interface, tshark_path=cs.tshark_path,
                    on_event=self._on_camera_event)
            self.camera_start.start()

        logger.info("start: Setting _running = True")
        self._running = True

        # Hardware supervisor: reconnects the RFID reader forever. The LoRa
        # transport already self-heals; this covers the other half so a
        # field box recovers from any single-device power cycle unattended.
        if not self._supervisor_thread or not self._supervisor_thread.is_alive():
            self._supervisor_stop.clear()
            self._supervisor_thread = threading.Thread(
                target=self._hardware_supervisor_loop, daemon=True)
            self._supervisor_thread.start()

        # Start FinishLynx auto-reconnect thread (finish line only)
        if self._uses_finishlynx() and not self._reconnect_running:
            logger.info("start: Starting FinishLynx auto-reconnect thread")
            self._reconnect_running = True
            self._reconnect_thread = threading.Thread(target=self._finishlynx_reconnect_loop, daemon=True)
            self._reconnect_thread.start()

        logger.info("=== Application started successfully ===")
        return True

    def _on_remote_tag_read(self, tag: TagRead):
        """Handle tag read in remote lap mode - send via LoRa."""
        now = time.monotonic()
        last = self._remote_last_sent.get(tag.bib)
        window = self.config.timing.remote_repeat_suppress_sec
        if last is not None and now - last < window:
            logger.info(
                f"Suppressed repeat read: bib {tag.bib} within {window:.0f}s of last transmit"
            )
            return
        self._remote_last_sent[tag.bib] = now

        read_data = self._track_lap(tag.bib, tag.timestamp, tag.reader_id)

        # Send via LoRa with delivery tracking (retransmit until ACKed).
        # Queued even while the link is down - the transport's outbox holds
        # the read and the retry loop keeps it alive until the ACK arrives.
        # The time goes out in the finish console's clock: this box's clock
        # minus the offset learned from the GUN broadcasts (0 once the OS
        # clock has been stepped to match), so a split is race time at the
        # finish line, not at whatever this Pi believes.
        if self.read_sender:
            ts = tag.timestamp - timedelta(seconds=self.remote_clock_offset)
            self.read_sender.send_read(tag.bib, ts)

        # Notify web interface
        if self._on_tag_read_callback:
            self._on_tag_read_callback(tag.bib, tag.timestamp, tag.reader_id,
                                       read_data.get('lap_number'), read_data.get('position'))

        logger.info(f"Tag read (LoRa TX): Bib {tag.bib} at {read_data['time']} (Lap {read_data['lap_number']})")

    def _hardware_supervisor_loop(self):
        """Reconnect the RFID reader forever while the app runs.

        A split box gets powered on, carried out, and left alone; nobody
        is there to press anything when a device boots slowly, browns out,
        or gets swapped. connect() includes the auto-configure attempt, so
        a factory-fresh replacement reader is adopted automatically.
        """
        RETRY_SEC = 15.0
        CHECK_SEC = 2.0          # notice a dropout fast (the console alarms on it)
        last_attempt = 0.0
        while self._running and not self._supervisor_stop.is_set():
            try:
                connected = bool(self.reader and self.reader.is_connected)
                self._note_reader_state(connected)
                if self.reader and not connected and time.time() - last_attempt >= RETRY_SEC:
                    last_attempt = time.time()
                    if not self.reader.connect():
                        self._try_adopt_other_reader_type()
                    self._note_reader_state(bool(self.reader and self.reader.is_connected))
                self._broadcast_gun()  # periodic re-send for late-boot stations (rate-limited)
            except Exception:
                logger.exception("Hardware supervisor: reconnect attempt failed")
            try:
                # FinishLynx's saved .lif files teach the mat-vs-film offset
                self.learn_film_offsets()
            except Exception:
                logger.exception("Film offset scan failed")
            try:
                # ...and are merged into the RFID folder MeetPro reads
                self.refresh_merged_lifs()
            except Exception:
                logger.exception("Merged .lif refresh failed")
            self._supervisor_stop.wait(CHECK_SEC)

    def lif_dirs(self):
        """(FinishLynx results folder, RFID folder MeetPro reads)."""
        from .lif_merge import meet_dirs
        return meet_dirs(self.config.event_directory or "", self.config.races.lif_dir or "",
                         self.config.races.rfid_subdir or "RFID")

    def learn_film_offsets(self) -> bool:
        """Scan the meet folder's FinishLynx .lif files for evaluated finish
        times (see film_offset.py). Cheap: only re-read files whose mtime
        changed."""
        if not self.race_manager:
            return False
        return self.race_manager.learn_film_offsets(self.lif_dirs()[0])

    def refresh_merged_lifs(self, race_keys=None):
        """Rebuild the merged .lif of every race pane (or just race_keys)
        in the RFID folder; a file is rewritten only when its content
        changed. A preview instance never writes where MeetPro reads, and
        nothing is written while publishing is off."""
        if not self.race_manager or not self.hardware_allowed or not getattr(self, "publishing", True):
            return
        lynx_dir, rfid_dir = self.lif_dirs()
        if not lynx_dir or not os.path.isdir(lynx_dir):
            return
        keys = list(race_keys) if race_keys is not None else list(self.race_manager.order)
        for key in keys:
            try:
                self.race_manager.write_merged_lif(key, lynx_dir, rfid_dir)
            except KeyError:
                pass                                   # pane deleted meanwhile
            except Exception:
                logger.exception(f"Merged .lif write failed for race {key}")

    def wipe_merged_lifs(self, archive_dir=None):
        """Close meet: archive, then delete, the merged .lif files."""
        from .lif_merge import wipe_merged_lifs
        return wipe_merged_lifs(self.lif_dirs()[1], archive_dir)

    def film_correction(self, race_key: Optional[int] = None) -> float:
        """Seconds subtracted from a mat finish in `race_key` right now (0
        when off, or for a race with nothing evaluated yet)."""
        return self.race_manager.film.correction(0, race_key) if self.race_manager else 0.0

    # Finish-reader dropouts, kept for the console: an outage during a race
    # means finishes went unrecorded (2026-09-17 JV: the first two finishers
    # crossed inside a 24-s dropout). [{"start": iso, "end": iso|None}]
    MAX_OUTAGES = 50

    def _note_reader_state(self, connected: bool, now: Optional[datetime] = None):
        now = now or datetime.now()
        outages = self._reader_outages
        was = self._reader_was_connected
        if connected == was:
            return
        if connected:
            if outages and outages[-1]["end"] is None:
                outages[-1]["end"] = now.isoformat()
                secs = (now - datetime.fromisoformat(outages[-1]["start"])).total_seconds()
                logger.warning(f"Hardware supervisor: RFID reader is back after {secs:.0f} s offline")
            else:
                logger.info("Hardware supervisor: RFID reader is up")
        else:
            outages.append({"start": now.isoformat(), "end": None})
            del outages[:-self.MAX_OUTAGES]
            role = self.reader_role("FINISH")
            if role == "not in use":
                logger.info("Hardware supervisor: RFID reader DOWN (not in use per the Stations "
                            "panel, so nothing is lost); retrying every 15 s")
            elif role == "the finish":
                logger.warning("Hardware supervisor: RFID reader DOWN - finishes are not being "
                               "recorded; retrying every 15 s")
            else:
                logger.warning(f"Hardware supervisor: RFID reader DOWN - {role} reads are not "
                               "being recorded; retrying every 15 s")
        self._reader_was_connected = connected

    def reader_outages(self) -> List[Dict]:
        out = []
        for o in self._reader_outages:
            end = datetime.fromisoformat(o["end"]) if o["end"] else None
            start = datetime.fromisoformat(o["start"])
            out.append({"start": o["start"], "end": o["end"],
                        "seconds": round(((end or datetime.now()) - start).total_seconds(), 1)})
        return out

    def _current_gun(self) -> Optional[datetime]:
        """Gun time the LED clocks should count from (None = time of day)."""
        return self._current_gun_with_source()[0]

    def _current_gun_with_source(self):
        """(gun, source) where source is one of manual | manual_tod | race |
        finishlynx | idle. Priority: operator override, then the latest gun
        among racing panes, then FinishLynx's own running clock."""
        ov = self._clock_override
        mgr = self.race_manager
        if ov:
            if ov.get("mode") == "gun":
                return ov["gun"], "manual"
            if ov.get("mode") == "race":
                # Clocks pinned to one pane's gun (a straggler from an
                # earlier race still on the course while the next race runs).
                # The pin follows that pane's gun if it is corrected, and
                # releases itself when the pane is closed or deleted.
                race = mgr.races.get(ov["key"]) if mgr else None
                if race is not None and race.status != "final" and race.gun is not None:
                    return race.gun, "pinned"
                logger.info("LED clock pin released: pane closed or gone - back to automatic")
                self._clock_override = None
            else:
                return None, "manual_tod"
        if mgr:
            races = list(mgr.races.values())
            # A camera start only drives the clocks until a pane takes it;
            # from then on that pane's status decides, so closing the race
            # returns the clocks to time of day instead of counting on for
            # up to CAMERA_GUN_TTL_SEC (2026-09-30). FinishLynx's version of
            # the same gun is ~0.1 s away, hence the 2-s match (_note_start).
            if self._camera_gun is not None and any(
                    r.gun is not None and abs((r.gun - self._camera_gun).total_seconds()) < 2.0
                    for r in races):
                self._camera_gun = None
            guns = [r.gun for r in races if r.status == "racing" and r.gun is not None]
            if guns:
                return max(guns), "race"
        if self._camera_gun is not None and time.time() - self._camera_gun_at < CAMERA_GUN_TTL_SEC:
            return self._camera_gun, "camera"
        fl = self._finishlynx_clock_gun()
        if fl is not None:
            return fl, "finishlynx"
        return None, "idle"

    def _note_start(self, ts: datetime, source: str):
        """Remember a start signal for the 'Start time' picker. Signals within
        2 s of one already noted are the same gun heard twice; the camera's
        version wins because it is the more precise one."""
        for entry in self._recent_starts:
            if abs((entry["ts"] - ts).total_seconds()) < 2.0:
                if source.startswith("camera") and not entry["source"].startswith("camera"):
                    entry["ts"], entry["source"] = ts, source
                return
        self._recent_starts.append({"ts": ts, "source": source})
        del self._recent_starts[:-40]

    def recent_starts(self) -> List[Dict]:
        return [{"ts": e["ts"].isoformat(), "source": e["source"]}
                for e in reversed(self._recent_starts)]

    def offer_recent_starts(self, race_key: int, hours: float = 3.0):
        """List today's start signals in a new pane's Gun picker (unarmed)."""
        if not self.race_manager:
            return
        cutoff = datetime.now() - timedelta(hours=hours)
        for e in self._recent_starts:
            if e["ts"] >= cutoff:
                self.race_manager.add_gun_candidate(race_key, e["ts"], e["source"], arm=False)

    def _pane_awaiting_start(self) -> Optional[int]:
        """Pane a start signal should land in when the event is unknown: the
        first open pane without a gun, else the pane currently racing (so a
        second signal appears in its Gun picker), else a lone pending pane."""
        mgr = self.race_manager
        if not mgr:
            return None
        with mgr._lock:
            open_keys = [k for k in mgr.order
                         if mgr.races[k].status in ("checkin", "racing", "pending")]
            waiting = [k for k in open_keys
                       if mgr.races[k].gun is None and mgr.races[k].status != "pending"]
            racing = [k for k in open_keys if mgr.races[k].status == "racing"]
            pending = [k for k in open_keys if mgr.races[k].status == "pending"]
        if waiting:
            return waiting[0]
        if racing:
            return racing[-1]
        if len(pending) == 1:
            return pending[0]
        return None

    def _route_start(self, gun: datetime, source: str) -> List[int]:
        """Put a start signal in front of the operator.

        The pane FinishLynx has armed (event/heat on the scoreboard feed)
        takes it and arms; otherwise the pane awaiting a start does. When
        neither exists - FinishLynx silent and several pending panes, the
        usual state right after set-up - the signal is offered UNARMED to
        every open pane without a gun, so it is in each pane's Gun picker
        and the operator chooses. Before 2026-09-15 it went to no pane at
        all in that case. Returns the pane keys it reached.
        """
        mgr = self.race_manager
        if not mgr:
            return []
        key = None
        if self.scoreboard_listener and self.scoreboard_listener.event:
            key = mgr.find_by_event(self.scoreboard_listener.event, self.scoreboard_listener.heat)
        if key is None:
            key = self._pane_awaiting_start()
        if key is not None:
            mgr.add_gun_candidate(key, gun, source)
            return [key]
        with mgr._lock:
            waiting = [k for k in mgr.order
                       if mgr.races[k].status in ("pending", "checkin")
                       and mgr.races[k].gun is None]
        for k in waiting:
            mgr.add_gun_candidate(k, gun, source, arm=False)
        if waiting:
            # FinishLynx's running clock re-implies the same gun 10 times a
            # second; log each distinct start once, not every packet.
            last = getattr(self, "_offer_logged", None)
            if last is None or last[1] != tuple(waiting) \
                    or abs((gun - last[0]).total_seconds()) >= 2.0:
                self._offer_logged = (gun, tuple(waiting))
                logger.info(f"{source} at {gun.strftime('%H:%M:%S.%f')[:-3]} offered to "
                            f"{len(waiting)} pane(s) without a gun (no event armed)")
        return waiting

    def _on_camera_event(self, ev):
        """Camera input event from the sniffer (reader thread)."""
        if ev.code != START_CODE:
            logger.info(f"camera input 0x{ev.code:04x} at {ev.wall.strftime('%H:%M:%S.%f')[:-3]} (not the start sensor)")
            return
        gun = ev.wall
        logger.info(f"GUN from camera at {gun.strftime('%H:%M:%S.%f')[:-3]}")
        self._note_start(gun, "camera start")
        self._route_start(gun, "camera start")
        # Also remembered as the LED clocks' gun until a pane takes over.
        self._camera_gun = gun
        self._camera_gun_at = time.time()
        if self._clock_override and self._clock_override.get("mode") == "tod":
            # The operator parked every clock (finish + stations) on time of
            # day; that holds until they click Auto. (Until 2026-09-22 a
            # camera start silently cancelled it, which read as "the Time of
            # day button does nothing" - the clocks re-armed within a minute.)
            logger.info("camera start while the clocks are parked on time of day - "
                        "override kept; click Auto in the Clocks chip to follow it")
        try:
            self._broadcast_gun()
        except Exception:
            logger.debug("GUN broadcast after camera start failed", exc_info=True)

    def _preview_refusal(self) -> Optional[Dict]:
        """The answer every hardware (re)start handler gives in preview mode."""
        if self.hardware_allowed:
            return None
        return {"success": False, "message": f"preview mode - hardware is off limits: {self.preview_reason}"}

    def restart_camera_start(self) -> Dict:
        if self._preview_refusal():
            return self._preview_refusal()
        if self.camera_start:
            self.camera_start.stop()
            self.camera_start = None
        if not self.config.camera_start.enabled:
            return {"success": True, "message": "camera start listener disabled"}
        if self.config.mode != "finish_line":
            return {"success": False, "message": "finish line only"}
        cs = self.config.camera_start
        self.camera_start = CameraStartListener(
            camera_host=cs.camera_host, camera_port=cs.camera_port,
            interface=cs.interface, tshark_path=cs.tshark_path,
            on_event=self._on_camera_event)
        self.camera_start.start()
        return {"success": True, "message": "camera start listener restarted"}

    def set_clock_override(self, mode: str, elapsed_seconds: Optional[float] = None,
                           race_key: Optional[int] = None) -> Dict:
        """Console clock chip: 'elapsed' counts up from elapsed_seconds,
        'now' starts at 0:00, 'tod' forces time of day, 'race' pins the
        clocks to one pane's gun (race_key) without touching any pane,
        'auto' clears."""
        if mode == "auto":
            self._clock_override = None
        elif mode == "tod":
            self._clock_override = {"mode": "tod"}
        elif mode == "race":
            mgr = self.race_manager
            race = mgr.races.get(race_key) if (mgr and race_key is not None) else None
            if race is None:
                return {"success": False, "message": f"no race pane {race_key}"}
            if race.gun is None:
                return {"success": False, "message": f"{race.name} has no gun yet"}
            if race.status == "final":
                return {"success": False, "message": f"{race.name} is closed - reopen it first"}
            self._clock_override = {"mode": "race", "key": race.key}
        elif mode in ("elapsed", "now"):
            secs = float(elapsed_seconds or 0.0) if mode == "elapsed" else 0.0
            if secs < 0 or secs > 99 * 3600:
                return {"success": False, "message": "elapsed time out of range"}
            self._clock_override = {"mode": "gun",
                                    "gun": datetime.now() - timedelta(seconds=secs)}
        else:
            return {"success": False, "message": f"unknown clock mode {mode!r}"}
        gun, source = self._current_gun_with_source()
        logger.info(f"LED clock override -> {mode} (source now {source}, gun {gun})")
        # The operator clicked because a clock looks wrong: re-send what it
        # should show right now rather than waiting for the next spot-check
        # (2026-09-19: three "time of day" clicks did nothing for 33 s while
        # the clock still counted yesterday's race after a lost set).
        svc = getattr(self, "led_clock_service", None)
        if svc:
            try:
                svc.resync_time_of_day()
            except Exception:
                logger.exception("LED clock resync after override failed")
        try:
            self._broadcast_gun()
        except Exception:
            logger.debug("GUN broadcast after override failed", exc_info=True)
        return {"success": True, "source": source,
                "gun": gun.isoformat() if gun else None}

    def on_operator_gun(self, race_key: int):
        """The operator set a pane's gun by hand (Gun dialog, New race start
        time). That is a deliberate "this race is running": a Time-of-day
        park is released so the clocks follow it (2026-09-30: the clocks sat
        on time of day after a gun was set, until "Show on clocks"). Starts
        that arrive by themselves (camera, FinishLynx) still leave the park
        alone, and a pin or manual count-up is the operator's choice too."""
        if self._clock_override and self._clock_override.get("mode") == "tod":
            logger.info(f"gun set by hand on race {race_key} - time-of-day park released")
            self.set_clock_override("auto")

    def set_clock_hour_format(self, hour_format: str) -> Dict:
        """Time-of-day on the LED clocks in '12' or '24' hour form. Takes
        effect on the next idle refresh (within a second) and persists."""
        if hour_format not in ("12", "24"):
            return {"success": False, "message": f"hour_format must be '12' or '24', not {hour_format!r}"}
        self.config.led_clock.hour_format = hour_format
        if self.led_clock_service:
            self.led_clock_service.set_hour12(hour_format == "12")
        try:
            self.save_config()
        except Exception:
            logger.exception("Could not persist LED clock hour format")
        logger.info(f"LED clock time-of-day format -> {hour_format}-hour")
        try:
            self._broadcast_gun()        # split stations mirror it
        except Exception:
            logger.debug("GUN broadcast after hour format change failed", exc_info=True)
        return {"success": True, "hour_format": hour_format}

    def _save_clock_entries(self):
        """The clock service edited config.led_clock.clocks in place (port
        renumbered, label set, type chosen) - persist it."""
        try:
            self.save_config()
        except Exception:
            logger.exception("Could not persist LED clock settings")

    def update_clock(self, port: str, label: Optional[str] = None,
                     enabled: Optional[bool] = None, kind: Optional[str] = None) -> Dict:
        """Rename, enable/disable or re-type one clock. The service owns the
        config entries (so labels follow the adapter across COM numbers) and
        calls back to save."""
        if kind is not None and kind not in ("auto", "none", "ch340", "electro"):
            return {"success": False, "message": f"unknown clock type {kind!r}"}
        if not self.led_clock_service:
            return {"success": False, "message": "LED clock service not running"}
        self.led_clock_service.update_slot(port, label=label, enabled=enabled, kind=kind)
        entry = next((c for c in self.config.led_clock.clocks
                      if str(c.get("port", "")).upper() == port.upper()), None)
        msg = {"none": "Marked as not a clock", "auto": "Type back to automatic",
               "ch340": "Driving as LED clock (CH340)", "electro": "Driving as Electro clock"}.get(kind)
        return {"success": True, "clock": entry, "message": msg or "Saved"}

    def retry_clock(self, port: str) -> Dict:
        if not self.led_clock_service:
            return {"success": False, "message": "LED clock service not running"}
        self.led_clock_service.retry(port)
        return {"success": True, "message": f"Retrying {port}"}

    def reconnect_finishlynx(self) -> Dict:
        """Drop and re-dial the LapTime link (after a FinishLynx options change)."""
        if self._preview_refusal():
            return self._preview_refusal()
        if not self.finishlynx:
            self.finishlynx = FinishLynxOutput(self.config.finishlynx)
        else:
            self.finishlynx.disconnect()
            self.finishlynx = FinishLynxOutput(self.config.finishlynx)
        ok = self.finishlynx.connect()
        if ok:
            self._flush_pending_sends()
        return {"success": ok,
                "message": "Reconnected to FinishLynx" if ok
                else f"FinishLynx not answering on {self.config.finishlynx.host}:{self.config.finishlynx.port}"}

    def restart_lora(self) -> Dict:
        """Re-open the LoRa gateway with the current config (finish line only)."""
        if self._preview_refusal():
            return self._preview_refusal()
        if self.config.mode != "finish_line":
            return {"success": False, "message": "LoRa restart is only for the finish line"}
        self.lora_link_seen_up()    # note an open link before its transport goes away
        # Stop the old receiver even while it is still waiting for the gateway
        # (is_running is False then) - its transport thread would otherwise
        # keep reconnecting and feeding a receiver nobody references.
        if self.lora_receiver:
            self.lora_receiver.stop()
        self.lora_receiver = None
        # The station manager is bound to the old transport (or never
        # existed when LoRa was off at boot): without rebuilding it, every
        # ANNOUNCE is dropped, "Remote stations" stays empty and CMD/CFGQ
        # go nowhere even though reads still arrive.
        if self.remote_manager:
            try:
                self.remote_manager.stop()
            except Exception:
                logger.exception("Remote manager stop failed")
            self.remote_manager = None
        if not self.config.lora.enabled:
            return {"success": True, "message": "LoRa disabled"}
        if not self._running:
            return {"success": True, "message": "LoRa will start with the console"}
        self.lora_receiver = LoRaReceiver(
            self.config.lora,
            on_message=self._on_lora_message,
            on_management_message=self._on_management_message,
            journal_path="lora-reads-received.log",
        )
        self.lora_receiver.start()
        if self.lora_receiver.transport:
            self.remote_manager = RemoteManager(
                self.lora_receiver.transport,
                self.config.lora,
                on_unit_change=self._on_remote_unit_change,
            )
            self.remote_manager.start()
        return {"success": True, "message": f"LoRa restarted on {self.config.lora.port}"}

    def lora_link_seen_up(self) -> bool:
        """True once the split-station radio link has been open at any point
        since this console started. A gateway that has not answered since the
        restart (switched off, not used at this meet) is not an outage, so the
        console's "split-station radio link down" alarm waits for this. Kept
        on the application, not the transport, so a Restart from the console
        (which builds a new transport) does not reset it."""
        if not self._lora_seen_up:
            transports = (self.lora_receiver.transport if self.lora_receiver else None,
                          self.lora_transport)
            self._lora_seen_up = any(getattr(t, "ever_connected", False) for t in transports if t)
        return self._lora_seen_up

    def _finishlynx_clock_gun(self) -> Optional[datetime]:
        """Gun derived from FinishLynx running-time packets, or None once they
        stop (FinishLynx switches to time-of-day when the clock is stopped)."""
        if self._fl_clock_gun is not None and \
                time.time() - self._fl_clock_at < FL_CLOCK_FRESH_SEC:
            return self._fl_clock_gun
        return None

    def _uses_finishlynx(self) -> bool:
        """Only the finish line talks to FinishLynx; split stations radio
        their reads to the finish console instead."""
        return self.config.mode == "finish_line"

    # Split-station clock discipline. A Pi has no real-time clock and no
    # time source on a station network, so after a power cycle it wakes
    # believing it is whenever it was last shut down. Every GUN broadcast
    # carries the finish console's clock; the station keeps a short window
    # of "my clock minus theirs" samples and, because radio latency only
    # ever makes a sample larger, takes the smallest as the true offset.
    REMOTE_CLOCK_WINDOW_SEC = 300.0
    REMOTE_CLOCK_STEP_SEC = 1.0     # further off than this: step the system clock

    def _learn_remote_clock(self, sent_at: float) -> None:
        now = time.time()
        samples = [s for s in self._remote_clock_samples
                   if now - s[0] < self.REMOTE_CLOCK_WINDOW_SEC]
        # sent_at is stamped as the frame leaves the finish console's
        # gateway (transport restamps it), so what remains between it and
        # our receive is the radio's own one-way delay - a known constant.
        air = float(getattr(self.config.lora, "air_latency_sec", 0.0) or 0.0)
        samples.append((now, now - sent_at - air))
        offset = min(s[1] for s in samples)
        if abs(offset) > self.REMOTE_CLOCK_STEP_SEC and self._step_system_clock(sent_at + air, offset):
            samples = []            # measured against the old clock
            offset = 0.0
            # The LED clocks were set from the old OS time and free-run from
            # it; without this they show the stale time-of-day for up to an
            # hour after the step.
            if self.led_clock_service:
                try:
                    self.led_clock_service.resync_time_of_day()
                except Exception:
                    logger.exception("LED clock resync after clock step failed")
        self._remote_clock_samples = samples
        if abs(offset - self.remote_clock_offset) > 0.5:
            logger.info(f"Clock offset from finish line: {offset:+.2f} s (gun shifted to match)")
        self.remote_clock_offset = offset

    def _step_system_clock(self, sent_at: float, offset: float) -> bool:
        """Set the OS clock to the finish line's time (Linux stations only).

        Everything time-of-day on the box - the read timestamps it radios
        back, the idle display on its clocks, its logs - then agrees with
        the finish line without any per-value correction. Needs
        passwordless sudo for `date`, which the station image grants; if
        that is missing the offset is still applied to the gun in software.
        """
        if os.name != "posix":
            return False
        try:
            result = subprocess.run(
                ["sudo", "-n", "date", "-u", "-s", f"@{sent_at:.2f}"],
                capture_output=True, text=True, timeout=5)
        except Exception as e:
            if not self._clock_step_failed:
                logger.warning(f"Could not step the system clock ({e}); "
                               f"correcting the gun in software instead")
                self._clock_step_failed = True
            return False
        if result.returncode != 0:
            if not self._clock_step_failed:
                logger.warning("Could not step the system clock "
                               f"({result.stderr.strip() or 'sudo refused'}); "
                               f"correcting the gun in software instead")
                self._clock_step_failed = True
            return False
        logger.warning(f"System clock stepped to the finish line's time "
                       f"(this box was {offset:+.1f} s off)")
        return True

    def set_remote_gun(self, epoch: float, hour_format: Optional[str] = None,
                       sent_at: Optional[float] = None):
        """Apply a LoRa GUN broadcast on a split station: the gun (0 = no
        race, clocks show time of day) and, when the finish console sends
        it, its 12/24-hour time-of-day format - the station's clocks mirror
        whatever the finish line shows. The format persists so a reboot
        keeps it until the next broadcast says otherwise. sent_at, the
        finish console's clock at send time, disciplines this box's clock
        (see _learn_remote_clock); the gun is expressed in local time so
        the LED clocks read right even before the OS clock is corrected."""
        if sent_at:
            self._learn_remote_clock(sent_at)
        self.remote_gun = datetime.fromtimestamp(epoch + self.remote_clock_offset) if epoch > 0 else None
        if hour_format in ("12", "24") and hour_format != self.config.led_clock.hour_format:
            self.config.led_clock.hour_format = hour_format
            if self.led_clock_service:
                self.led_clock_service.set_hour12(hour_format == "12")
            logger.info(f"LED clock time-of-day format -> {hour_format}-hour (mirrored from finish)")
            try:
                self.save_config()
            except Exception:
                logger.exception("Could not persist mirrored LED clock hour format")

    GUN_REBROADCAST_SEC = 30.0

    def _broadcast_gun(self):
        """Push what the finish clocks show to the split stations
        (fire-and-forget): the gun, or 0 for time of day, plus the 12/24-hour
        format. Sent on any change and re-sent every 30 s regardless, so a
        station that boots mid-race - or between races - mirrors the finish
        line within half a minute.
        """
        transport = self.lora_receiver.transport if self.lora_receiver else None
        if not transport:
            return
        gun = self._current_gun()
        epoch = gun.timestamp() if gun else 0.0
        hour_format = self.config.led_clock.hour_format
        now = time.time()
        changed = self._gun_broadcast_epoch is None \
            or abs(epoch - self._gun_broadcast_epoch) > 0.01 \
            or hour_format != self._gun_broadcast_hour_format
        if changed or now - self._gun_broadcast_at > self.GUN_REBROADCAST_SEC:
            from .remote_protocol import format_gun
            # sent_at = this console's clock, so clock-less stations can
            # line themselves up with the finish line
            transport.send(format_gun(epoch, hour_format, sent_at=now))
            self._gun_broadcast_epoch = epoch
            self._gun_broadcast_hour_format = hour_format
            self._gun_broadcast_at = now

    def _try_adopt_other_reader_type(self) -> bool:
        """Probe for the other vendor's reader and switch to it if present.

        Any-hardware-any-station: a box whose config says impinj but has a
        Vanch plugged in (or vice versa) adopts whatever answers. Each
        driver's connect() already handles finding the reader's IP.
        """
        import dataclasses

        if not self.hardware_allowed:
            return False          # preview: never go looking for a reader
        other = "vanch" if self.config.reader.type == "impinj" else "impinj"
        cfg = dataclasses.replace(
            self.config.reader,
            type=other,
            port=default_port_for(other),
            antennas=list(self.config.reader.antennas),
        )
        callback = self._on_tag_read if self.config.mode == "finish_line" else self._on_remote_tag_read
        reader_id = "FINISH" if self.config.mode == "finish_line" else self.config.lora.reader_id
        trial = create_reader(cfg, self.config.timing, on_tag_read=callback, reader_id=reader_id)
        if not trial.connect():
            trial.disconnect()
            return False

        logger.info(f"Hardware supervisor: adopted {other} reader at {cfg.host}:{cfg.port}")
        old = self.reader
        self.reader = trial
        self.config.reader = cfg
        if old:
            old.disconnect()
        try:
            self.save_config()
        except Exception:
            logger.exception("Could not persist adopted reader type")
        return True

    def _handle_running_time(self, seconds: float):
        """A FinishLynx running-time packet implies gun = now - race clock."""
        if seconds < 0:
            return
        gun = datetime.now() - timedelta(seconds=seconds)
        # Always remember it for the LED clocks (see _finishlynx_clock_gun);
        # a 0.0 packet is FinishLynx arming with the clock not yet started.
        if seconds > 0:
            self._fl_clock_gun = gun
            self._fl_clock_at = time.time()
            self._note_start(gun, "FinishLynx clock")
        if not self.race_manager or not self.scoreboard_listener or seconds <= 0:
            return
        # FinishLynx only announces event/heat on the scoreboard feed when
        # "results if armed" is on, and never after a middleware restart
        # mid-event; _route_start falls back to the waiting pane(s).
        self._route_start(gun, "FinishLynx clock")

    def _on_races_changed(self, race_keys):
        """Stream updated .lif files for MeetPro and push the UI."""
        # A finish first held as too early that a pane has now taken (the
        # operator placed it, or the pace/gender changed) is owed to FinishLynx.
        if getattr(self, "publishing", True):
            try:
                self._flush_unsent("early")
            except Exception:
                logger.exception("Sending held-back finishes failed")
        # Merged with FinishLynx's own file, into the RFID folder MeetPro
        # reads (see lif_merge.py); FinishLynx's folder is never written.
        self.refresh_merged_lifs(race_keys)
        # Meet mode: drop cached live results, nudge MileSplit Live (debounced
        # by its pusher). Called outside RaceManager's lock. (2026-10-03)
        svc = getattr(self, "meet_service", None)
        if svc is not None:
            try:
                svc.on_races_changed(race_keys)
            except Exception:
                logger.exception("Meet update after a race change failed")
        try:
            from . import web
            web.emit_races_update()
        except Exception:
            logger.debug("races UI push skipped", exc_info=True)
        try:
            self._broadcast_gun()
        except Exception:
            logger.exception("GUN broadcast failed")

    def _on_lora_message(self, msg):
        """Handle received LoRa READ message (tag read from remote lap point).

        Routes through the same mode-aware pipeline as local reads, so remote
        reads get event/heat matching, race-read tracking for LIF generation,
        and buffering when FinishLynx is disconnected.
        """
        tag = TagRead(
            bib=msg.bib,
            timestamp=msg.timestamp,
            epc="",
            rssi=0,
            antenna=0,
            reader_id=msg.reader_id,
        )
        self._on_tag_read(tag)

    def _on_management_message(self, msg):
        """Handle management message on finish line (route to RemoteManager)."""
        if self.remote_manager:
            self.remote_manager.on_message(msg)

    def _on_remote_lap_receive(self, data: bytes):
        """Handle incoming LoRa data on remote_lap mode (management commands)."""
        if self.remote_agent:
            self.remote_agent.on_receive(data)

    def _on_remote_unit_change(self, unit_data: Dict):
        """Handle remote unit state change (notify web UI)."""
        if self._on_remote_unit_change_callback:
            self._on_remote_unit_change_callback(unit_data)

    def set_remote_unit_change_callback(self, callback):
        """Set callback for remote unit state changes (used by web interface)."""
        self._on_remote_unit_change_callback = callback

    def _finishlynx_reconnect_loop(self):
        """Background loop to auto-reconnect FinishLynx if connection drops."""
        while self._reconnect_running and self._running:
            # Sleep in short increments so stop() can join promptly
            for _ in range(50):
                time.sleep(0.1)
                if not (self._reconnect_running and self._running):
                    break

            if not (self._reconnect_running and self._running):
                break

            if self.finishlynx and not self.finishlynx.is_connected:
                logger.info("FinishLynx disconnected - attempting reconnect...")
                if self.finishlynx.connect():
                    logger.info("FinishLynx reconnected successfully")
                    self._flush_pending_sends()
                else:
                    logger.debug("FinishLynx reconnect failed, will retry...")

    def _flush_pending_sends(self):
        """Send any buffered reads to FinishLynx after reconnection."""
        with self._pending_lock:
            pending = self._pending_sends.copy()
            self._pending_sends.clear()

        if not pending:
            return

        logger.info(f"Flushing {len(pending)} buffered reads to FinishLynx...")
        sent = 0
        for i, item in enumerate(pending):
            if self.finishlynx and self.finishlynx.is_connected:
                # Use stored event/round/heat if available
                event_num = item.get('event')
                round_num = item.get('round')
                heat_num = item.get('heat')
                if self.finishlynx.send_split(item['bib'], item['timestamp'], event_num, round_num, heat_num):
                    sent += 1
                else:
                    # Connection dropped again, re-queue remaining ahead of
                    # any reads buffered since (preserve send order)
                    with self._pending_lock:
                        self._pending_sends[:0] = pending[i:]
                    break
            else:
                with self._pending_lock:
                    self._pending_sends[:0] = pending[i:]
                break

        logger.info(f"Flushed {sent}/{len(pending)} buffered reads to FinishLynx")

    def restart_reader(self) -> bool:
        """Restart the RFID reader with current config (applies power/antenna changes).

        Returns:
            True if reader restarted successfully.
        """
        if not self._running or not self.hardware_allowed:
            return False

        logger.info("Restarting RFID reader to apply new settings...")

        # Disconnect existing reader (even a half-open client leaks its
        # socket/thread if left unreferenced)
        if self.reader:
            self.reader.disconnect()

        # Create new reader with current config
        callback = self._on_tag_read if self.config.mode == "finish_line" else self._on_remote_tag_read
        self.reader = create_reader(
            self.config.reader,
            self.config.timing,
            on_tag_read=callback,
            reader_id="FINISH" if self.config.mode == "finish_line" else self.config.lora.reader_id,
        )

        if self.reader.connect():
            logger.info("Reader restarted with new settings")
            return True
        else:
            logger.error("Failed to restart reader")
            return False

    def restart_scoreboard_listener(self) -> bool:
        """Restart the Scoreboard listener (receives event/heat info from FinishLynx).

        Returns:
            True if listener restarted successfully.
        """
        if not self._running or not self.hardware_allowed:
            return False

        logger.info("Restarting Scoreboard listener...")

        if self.scoreboard_listener:
            self.scoreboard_listener.stop()

        self.scoreboard_listener = ScoreboardListener(
            self.config.scoreboard.host,
            self.config.scoreboard.port
        )
        self.scoreboard_listener.start()
        logger.info("Scoreboard listener restarted.")
        return True

    # Alias for backwards compatibility
    def restart_finishlynx_listener(self) -> bool:
        return self.restart_scoreboard_listener()

    def stop(self):
        """Stop reading."""
        self._running = False
        self._reconnect_running = False
        self._supervisor_stop.set()

        if self._reconnect_thread and self._reconnect_thread is not threading.current_thread():
            self._reconnect_thread.join(timeout=2.0)
            self._reconnect_thread = None

        if self.remote_manager:
            self.remote_manager.stop()

        if self.remote_agent:
            self.remote_agent.stop()

        if self.read_sender:
            self.read_sender.stop()

        if self.led_clock_service:
            self.led_clock_service.stop()

        if self.camera_start:
            self.camera_start.stop()

        if self.reader:
            self.reader.disconnect()

        if self.lora_receiver:
            self.lora_receiver.stop()

        if self.lora_transport and self.lora_transport.is_connected:
            self.lora_transport.disconnect()

        # Stop FinishLynx Listener
        if self.scoreboard_listener and self.scoreboard_listener._running: # Accessing _running directly for check
            self.scoreboard_listener.stop()

        logger.info("Application stopped")

    def shutdown(self):
        """Complete shutdown including FinishLynx connection."""
        self.stop()

        if self.finishlynx:
            self.finishlynx.disconnect()

    def get_status(self) -> Dict:
        """Get current application status."""
        with self._reads_lock:
            total_reads = len(self._reads)
            unique_bibs = len(self._unique_bibs)
            last_read = self._last_read_time
            antenna_reads = {str(k): v for k, v in sorted(self._antenna_reads.items())}

        # Determine connection status (connected OR reachable)
        now = time.time()
        reader_connected = (self.reader.is_connected if self.reader else False) \
            or (now - self._reader_reachable_at < REACHABLE_TTL_SEC)
        finishlynx_connected = (self.finishlynx.is_connected if self.finishlynx else False) \
            or (now - self._finishlynx_reachable_at < REACHABLE_TTL_SEC)

        # Get scoreboard status
        scoreboard_active = self.scoreboard_listener._running if self.scoreboard_listener else False
        scoreboard_connected = self.scoreboard_listener.is_connected if self.scoreboard_listener else False
        fl_gun = self._finishlynx_clock_gun()
        if self.config.mode == "finish_line":
            clock_gun, clock_source = self._current_gun_with_source()
        else:
            clock_gun, clock_source = self.remote_gun, ("lora" if self.remote_gun else "idle")
        sb = self.scoreboard_listener
        sb_last = getattr(sb, "_last_packet_time", None) if sb else None

        return {
            'version': __version__,
            'uptime_sec': round(time.time() - self._started_at),
            'config_path': self.config_path,
            'log_level': self.config.logging.level,
            'dedup_window_sec': self.config.timing.dedup_window_sec,
            'finishlynx_sync_count': self.finishlynx._sync_count if self.finishlynx else 0,
            'scoreboard_packets': getattr(sb, "_packet_count", 0) if sb else 0,
            'scoreboard_last_packet_age_sec': (
                round((datetime.now() - sb_last).total_seconds(), 1) if sb_last else None),
            'scoreboard_round': sb.round if sb else 0,
            'scoreboard_race_name': sb.race_name if sb else "",
            'led_clock_source': clock_source,
            # Mat-vs-film finish offset learned from FinishLynx's .lif files
            'film_offset': self.race_manager.film.stats() if self.race_manager else None,
            # Start gun sniffed from the camera
            'camera_start_enabled': self.config.camera_start.enabled,
            'camera_start': self.camera_start.status() if self.camera_start else None,
            'camera_gun': (self._camera_gun.isoformat()
                           if self._camera_gun and time.time() - self._camera_gun_at < CAMERA_GUN_TTL_SEC
                           else None),
            'led_clock_override': (
                {'mode': self._clock_override['mode'],
                 'gun': self._clock_override['gun'].isoformat()
                 if self._clock_override.get('gun') else None,
                 'race': self._clock_override.get('key'),
                 'race_name': (self.race_manager.races[self._clock_override['key']].name
                               if self.race_manager and self._clock_override.get('key') in self.race_manager.races
                               else None)}
                if self._clock_override else None),
            'running': self._running,
            # PREVIEW = this process may not touch any hardware (hw_guard.py)
            'preview': not self.hardware_allowed,
            'preview_reason': self.preview_reason,
            'mode': self.config.mode,
            'operation_mode': self.operation_mode,
            'manual_race_in_progress': self.manual_race_in_progress,
            'led_clock_connected': bool(self.led_clock_service and self.led_clock_service.is_connected),
            'led_clock_kind': self.led_clock_service.clock_kind if self.led_clock_service else None,
            'led_clock_enabled': self.config.led_clock.enabled,
            # One entry per clock (configured or discovered): port, kind, label,
            # status = driving | busy | missing. The console draws a chip each.
            'led_clocks': self.led_clock_service.clocks if self.led_clock_service else [],
            'reader_connected': reader_connected,
            'reader_outages': self.reader_outages(),
            'reader_down_since': (self._reader_outages[-1]["start"]
                                  if self._reader_outages and self._reader_outages[-1]["end"] is None
                                  else None),
            'finishlynx_connected': finishlynx_connected,
            # Scoreboard listener status (receives event/heat from FinishLynx)
            'scoreboard_listener_active': scoreboard_active,
            'scoreboard_connected': scoreboard_connected,
            'scoreboard_event': self.scoreboard_listener.event if self.scoreboard_listener else 0,
            # the event FinishLynx has open (its window title) - which event
            # finish reads land in; the scoreboard feed rarely names it
            'finishlynx_open_event': (lambda w: {'event': w[0], 'round': w[1], 'heat': w[2], 'name': w[3]}
                                      if w else None)(self.finishlynx_open_event()),
            'scoreboard_heat': self.scoreboard_listener.heat if self.scoreboard_listener else 0,
            # FinishLynx's own race clock as seen in running-time packets
            'finishlynx_clock_running': fl_gun is not None,
            'finishlynx_race_seconds': (
                round((datetime.now() - fl_gun).total_seconds(), 1) if fl_gun else None),
            # What the LED clocks are actually showing right now
            'led_clock_gun': clock_gun.isoformat() if clock_gun else None,
            # this PC's clock: the console shows the clocks' time of day from it
            'server_time': datetime.now().isoformat(timespec='milliseconds'),
            # Backwards compatibility aliases
            'finishlynx_listener_active': scoreboard_active,
            'tdp_connected': scoreboard_connected,
            'tdp_event': self.scoreboard_listener.event if self.scoreboard_listener else 0,
            'tdp_heat': self.scoreboard_listener.heat if self.scoreboard_listener else 0,
            'lora_enabled': self.config.lora.enabled,
            'lora_connected': (
                (self.lora_receiver.is_running if self.lora_receiver else False) or
                (self.lora_transport.is_connected if self.lora_transport else False)
            ),
            # the link has been up since this console started (split alarm)
            'lora_ever_connected': self.lora_link_seen_up(),
            # Which readers are the finish line right now (races.reader_points
            # can make a split station the finish, or the local mat a split)
            'finish_reader_ids': self.finish_reader_ids(),
            'local_reader_is_finish': self.reader_is_finish("FINISH"),
            # Stations panel switch: off = nothing to FinishLynx or the .lif
            'publishing': self.publishing,
            'publish_off_since': self._publish_off_at.isoformat() if self._publish_off_at else None,
            'publish_kept_back': len(self.race_manager.unsent_reads("off")) if self.race_manager else 0,
            'read_backup': self.backup_path(),
            'total_reads': total_reads,
            'unique_bibs': unique_bibs,
            'last_read_time': last_read,
            'antenna_reads': antenna_reads,
            'reader_antennas': list(self.config.reader.antennas),
            'recent_starts': self.recent_starts(),
        }

    def get_reads(self) -> List[Dict]:
        """Get all tag reads."""
        with self._reads_lock:
            return self._reads.copy()

    def clear_reads(self):
        """Clear all reads and reset deduplication state."""
        with self._reads_lock:
            self._reads.clear()
            self._unique_bibs.clear()
            self._bib_laps.clear()
            self._last_read_time = None
            self._antenna_reads.clear()

        # Also clear race reads for FinishLynx mode
        with self._race_reads_lock:
            self._race_reads.clear()

        # Reset deduplication so tags can be read again
        if self.reader:
            self.reader.reset_dedup()

        logger.info("Reads cleared and dedup state reset")

        # Notify frontend to clear the read log
        try:
            from . import web
            web.emit_reads_cleared()
        except Exception as e:
            logger.debug(f"Could not emit reads_cleared: {e}")

    def add_manual_read(self, bib: str, timestamp: Optional[datetime] = None) -> bool:
        """Add a manual bib entry.

        Args:
            bib: Bib number to add.
            timestamp: Read time; defaults to now.

        Returns:
            True if the read was processed, False if rejected (not in heat).
        """
        from . import web

        timestamp = timestamp or datetime.now()

        # Determine identifier based on operation mode
        if self.operation_mode == "finishlynx":
            event_info = web.get_current_heat_info()

            if self._is_xc_event(event_info):
                # XC event - check if entries exist
                event_bibs = set(event_info.get('bibs', [])) if event_info else set()
                if event_bibs and bib not in event_bibs:
                    # Entries exist but bib not in them - reject
                    logger.info(f"Manual entry rejected: Bib {bib} not in event entries (XC)")
                    return False
                identifier = bib  # XC always sends bib number
            else:
                # Track event - check if it's a relay
                if event_info and event_info.get('is_relay'):
                    # Relay event - use lane as identifier
                    lane = self._lookup_lane_for_bib(bib, event_info)
                    if not lane:
                        logger.info(f"Manual entry rejected: Bib {bib} not in current heat entries (relay)")
                        return False
                    identifier = str(lane)
                else:
                    # Individual track event - bib must be in heat, but send bib (not lane)
                    lane = self._lookup_lane_for_bib(bib, event_info)
                    if not lane:
                        # Bib not in current heat - reject
                        logger.info(f"Manual entry rejected: Bib {bib} not in current heat entries")
                        return False
                    identifier = bib  # Individual events send bib, not lane

            # Track the lap for display
            read_data = self._track_lap(bib, timestamp, 'USER', manual=True)

            # Track for LIF generation in FinishLynx mode
            with self._race_reads_lock:
                self._race_reads.append({
                    'bib': bib,
                    'identifier': identifier,
                    'timestamp': timestamp,
                    'time': read_data['time'],
                })
        else:
            # Current reads mode - use existing relay/validation logic
            read_data = self._track_lap(bib, timestamp, 'USER', manual=True)
            identifier, should_send, extra_data = self._get_finishlynx_identifier(bib, timestamp)
            if not should_send:
                identifier = bib  # Fallback

        # Send to FinishLynx (queue if disconnected) with event/round/heat -
        # unless publishing is off (Stations panel switch)
        self._backup_read("USER", bib, timestamp, point="FINISH", state="manual")
        if self.finishlynx and self._feeds_finishlynx("USER"):
            self._send_or_queue_finishlynx(identifier, timestamp)

        # Notify web interface
        if self._on_tag_read_callback:
            self._on_tag_read_callback(bib, timestamp, 'USER',
                                       read_data.get('lap_number'), read_data.get('position'))

        logger.info(f"Manual entry: Bib {bib} -> ID {identifier} at {read_data['time']} (Lap {read_data['lap_number']})")
        return True

    def reset_dedup(self):
        """Reset deduplication state."""
        if self.reader:
            self.reader.reset_dedup()
        logger.info("Deduplication reset")

    def send_start_time(self) -> bool:
        """Send start/zero time to FinishLynx.

        Returns:
            True if sent successfully.
        """
        if self.finishlynx and self.finishlynx.is_connected:
            return self.finishlynx.send_zero()
        return False

    def test_reader_connection(self) -> Dict:
        """Test RFID reader connection.

        Returns:
            Result dict with success status and message.
        """
        if self._preview_refusal():
            return self._preview_refusal()     # even a bare TCP connect can bump the live session
        try:
            # Try to create a socket connection to the reader
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(5)
                result = sock.connect_ex((self.config.reader.host, self.config.reader.port))
            finally:
                sock.close()

            if result == 0:
                self._reader_reachable_at = time.time()
                return {'success': True, 'message': f'Reader reachable at {self.config.reader.host}'}
            else:
                self._reader_reachable_at = 0.0
                return {'success': False, 'message': f'Cannot reach reader at {self.config.reader.host}'}
        except Exception as e:
            self._reader_reachable_at = 0.0
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def test_finishlynx_connection(self) -> Dict:
        """Test FinishLynx connection.

        Returns:
            Result dict with success status and message.
        """
        if self._preview_refusal():
            return self._preview_refusal()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(5)
                result = sock.connect_ex((self.config.finishlynx.host, self.config.finishlynx.port))
            finally:
                sock.close()

            if result == 0:
                self._finishlynx_reachable_at = time.time()
                return {'success': True, 'message': f'FinishLynx reachable at {self.config.finishlynx.host}'}
            else:
                self._finishlynx_reachable_at = 0.0
                return {'success': False, 'message': f'Cannot reach FinishLynx at {self.config.finishlynx.host}'}
        except Exception as e:
            self._finishlynx_reachable_at = 0.0
            return {'success': False, 'message': f'Connection error: {str(e)}'}

    def test_time_sync(self) -> Dict:
        """Test time synchronization between all systems.

        Sends a test time packet to FinishLynx and reports timing info.
        For LoRa, calculates round-trip latency with a ping-style test.

        Returns:
            Result dict with timing information for each system.
        """
        results = {
            'system_time': datetime.now().strftime('%H:%M:%S.%f')[:-3],
            'components': [],
        }

        # Test FinishLynx time sync
        if self.finishlynx:
            try:
                start = datetime.now()
                if self.finishlynx.is_connected:
                    success = self.finishlynx.send_time_sync()
                    elapsed_ms = (datetime.now() - start).total_seconds() * 1000
                    results['components'].append({
                        'name': 'FinishLynx',
                        'status': 'ok' if success else 'error',
                        'latency_ms': round(elapsed_ms, 1),
                        'message': f'Time sync sent ({elapsed_ms:.1f}ms)' if success else 'Failed to send',
                    })
                else:
                    results['components'].append({
                        'name': 'FinishLynx',
                        'status': 'disconnected',
                        'latency_ms': None,
                        'message': 'Not connected',
                    })
            except Exception as e:
                results['components'].append({
                    'name': 'FinishLynx',
                    'status': 'error',
                    'latency_ms': None,
                    'message': str(e),
                })

        # Test RFID Reader connectivity (LLRP doesn't have a ping, so we just check status)
        if self.reader:
            try:
                connected = self.reader.is_connected
                results['components'].append({
                    'name': 'RFID Reader',
                    'status': 'ok' if connected else 'disconnected',
                    'latency_ms': None,
                    'message': 'Connected and inventorying' if connected else 'Not connected',
                    'note': 'Reader uses its own clock for tag timestamps',
                })
            except Exception as e:
                results['components'].append({
                    'name': 'RFID Reader',
                    'status': 'error',
                    'latency_ms': None,
                    'message': str(e),
                })

        # Test LoRa (if enabled)
        if self.config.lora.enabled:
            lora = self.lora_receiver or self.lora_transport
            if lora:
                connected = lora.is_running if hasattr(lora, 'is_running') else lora.is_connected
                results['components'].append({
                    'name': 'LoRa Radio',
                    'status': 'ok' if connected else 'disconnected',
                    'latency_ms': None,
                    'message': 'Radio active' if connected else 'Not connected',
                    'note': 'LoRa adds ~50-200ms latency depending on packet size',
                })

        # Summary
        all_ok = all(c['status'] == 'ok' for c in results['components'])
        results['overall_status'] = 'ok' if all_ok else 'warning'
        results['message'] = 'All systems synchronized' if all_ok else 'Some systems not connected'

        logger.info(f"Time sync test: {results['message']}")
        return results

    def start_meet_sync(self) -> None:
        """(Re)start the meet sync runners from config.meet_sync: one per
        shared place (the NAS, a shared folder or both, 2026-10-08)."""
        from .meet_sync import ServiceStore, SyncGroup, SyncRunner, engines_from_config
        if self.meet_sync is not None:
            self.meet_sync.stop()
            self.meet_sync = None
        svc = self.meet_service
        svc.store.on_save = None
        svc.meet_sync = None
        try:
            base = os.path.dirname(os.path.abspath(self.config.races.state_file or "races-state.json"))
            engines = engines_from_config(self.config, svc.root, base, store=ServiceStore(svc))
        except Exception:
            logger.exception("Meet sync unavailable")
            return
        if not engines:
            return

        def racing() -> bool:
            mgr = self.race_manager
            return bool(mgr) and any(r.status == "racing" for r in list(mgr.races.values()))

        def report(rep: dict) -> None:
            try:
                from . import web
                web.socketio.emit("meet_sync", group.status())
                if rep.get("added") or rep.get("merged"):
                    web.emit_meet_update()                 # the Open list and badges
            except Exception:
                logger.debug("meet sync emit failed", exc_info=True)

        group = SyncGroup([(name, SyncRunner(sync, is_racing=racing, on_report=report))
                           for name, sync in engines])
        svc.store.on_save = group.notify_saved
        svc.meet_sync = group                  # a round before imports / bib assigns (2026-10-10)
        group.start()
        self.meet_sync = group
        logger.info(f"Meet sync on: {' + '.join(s.transport.name for _, s in engines)} as {engines[0][1].machine}")

    def save_config(self):
        """Save current configuration to file."""
        if not self.config_path:
            logger.warning("No config path set, cannot save")
            return

        data = {
            'mode': self.config.mode,
            'display_name': self.config.display_name,
            'header_color': self.config.header_color,
            'remote_readers': [
                {
                    'name': entry.name,
                    'host': entry.host,
                    'web_port': entry.web_port,
                }
                for entry in self.config.remote_readers
            ],
            'reader': {
                'host': self.config.reader.host,
                'port': self.config.reader.port,
                'antennas': self.config.reader.antennas,
                'power_dbm': self.config.reader.power_dbm,
                'type': self.config.reader.type,
                'beep': self.config.reader.beep,
            },
            'timing': {
                'dedup_window_sec': self.config.timing.dedup_window_sec,
                'lap_mode': self.config.timing.lap_mode,
                'total_laps': self.config.timing.total_laps,
            },
            'finishlynx': {
                'host': self.config.finishlynx.host,
                'port': self.config.finishlynx.port,
                'sync_mode': self.config.finishlynx.sync_mode,
                'reconnect_delay_sec': self.config.finishlynx.reconnect_delay_sec,
                'feed': self.config.finishlynx.feed,
            },
            'scoreboard': {
                'enabled': self.config.scoreboard.enabled,
                'host': self.config.scoreboard.host,
                'port': self.config.scoreboard.port,
            },
            'lora': {
                'enabled': self.config.lora.enabled,
                'port': self.config.lora.port,
                'baud': self.config.lora.baud,
                'send_interval_sec': self.config.lora.send_interval_sec,
                'reader_id': self.config.lora.reader_id,
                'pre_registered_units': self.config.lora.pre_registered_units,
                'announce_interval_sec': self.config.lora.announce_interval_sec,
                'command_timeout_sec': self.config.lora.command_timeout_sec,
                'offline_timeout_sec': self.config.lora.offline_timeout_sec,
                'air_latency_sec': self.config.lora.air_latency_sec,
            },
            'led_clock': {
                'enabled': self.config.led_clock.enabled,
                'port': self.config.led_clock.port,
                'type': self.config.led_clock.type,
                'hour_format': self.config.led_clock.hour_format,
                'clocks': [dict(c) for c in self.config.led_clock.clocks],
            },
            'camera_start': {
                'enabled': self.config.camera_start.enabled,
                'camera_host': self.config.camera_start.camera_host,
                'camera_port': self.config.camera_start.camera_port,
                'interface': self.config.camera_start.interface,
                'tshark_path': self.config.camera_start.tshark_path,
            },
            # Persist the races block - dropping it on save used to reset
            # races.enabled to its default and silently swap the finish-line
            # console for the legacy dashboard after any Settings change.
            'races': {
                'enabled': self.config.races.enabled,
                'lif_dir': self.config.races.lif_dir,
                'rfid_subdir': self.config.races.rfid_subdir,
                'chute_reader': self.config.races.chute_reader,
                'finish_readers': list(self.config.races.finish_readers),
                'point_labels': dict(self.config.races.point_labels),
                'point_distances': dict(self.config.races.point_distances),
                'reader_points': {k: list(v) for k, v in self.config.races.reader_points.items()},
                'pass_window_sec': self.config.races.pass_window_sec,
                'fastest_pace_sec_per_mile': self.config.races.fastest_pace_sec_per_mile,
                'state_file': self.config.races.state_file,
            },
            'logging': {
                'level': self.config.logging.level,
                'file': self.config.logging.file,
            },
            'support': {
                'update_repo': self.config.support.update_repo,
                'logs_repo': self.config.support.logs_repo,
                'logs_token': self.config.support.logs_token,
                'site_name': self.config.support.site_name,
            },
            'meet_sync': {                       # docs/MEET-SYNC-PLAN.md (2026-10-07)
                'enabled': self.config.meet_sync.enabled,
                'mode': self.config.meet_sync.mode,
                'remote': self.config.meet_sync.remote,
                'folder': self.config.meet_sync.folder,
                'machine': self.config.meet_sync.machine,
                'git': self.config.meet_sync.git,
            },
            'event_directory': self.config.event_directory,
            'evt_file': self.config.evt_file,
        }

        # temp file + replace: a power cut mid-save keeps the last good
        # config.yaml instead of a truncated one (2026-10-04 review)
        tmp = self.config_path + ".tmp"
        with self._config_save_lock:
            with open(tmp, 'w') as f:
                yaml.dump(data, f, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)

        logger.info(f"Configuration saved to {self.config_path}")

    def get_all_laps_summary(self) -> Dict[str, List[Dict]]:
        """Get all bibs' lap data for cross-reference.

        Returns dict mapping bib -> list of {lap, timestamp_ms, position}.
        timestamp_ms is milliseconds since midnight for easy JS math.
        """
        with self._reads_lock:
            result = {}
            for bib, laps in self._bib_laps.items():
                result[bib] = []
                for i, lap in enumerate(laps):
                    ts = lap['timestamp']
                    # Convert to ms since midnight
                    ms_since_midnight = (
                        ts.hour * 3600000 +
                        ts.minute * 60000 +
                        ts.second * 1000 +
                        ts.microsecond // 1000
                    )
                    position = self._compute_position(bib, i + 1)
                    result[bib].append({
                        'lap': i + 1,
                        'timestamp_ms': ms_since_midnight,
                        'timestamp': ts.strftime('%H:%M:%S.') + f'{ts.microsecond // 1000:03d}',
                        'position': position,
                    })
            return result

    def get_bib_detail(self, bib: str) -> Optional[Dict]:
        """Get full lap detail for a specific bib.

        Returns dict with lap_history including splits and positions.
        """
        with self._reads_lock:
            laps = self._bib_laps.get(bib)
            if not laps:
                return None

            history = []
            for i, lap in enumerate(laps):
                lap_num = i + 1
                ts = lap['timestamp']
                split = None
                if i > 0:
                    prev_ts = laps[i - 1]['timestamp']
                    delta = ts - prev_ts
                    total_sec = delta.total_seconds()
                    minutes = int(total_sec // 60)
                    seconds = total_sec % 60
                    split = f"{minutes}:{seconds:06.3f}"

                position = self._compute_position(bib, lap_num)
                history.append({
                    'lap': lap_num,
                    'timestamp': ts.strftime('%H:%M:%S.') + f'{ts.microsecond // 1000:03d}',
                    'split': split,
                    'position': position,
                    'reader_id': lap.get('reader_id', ''),
                    'synthetic': lap.get('synthetic', False),
                })

            return {
                'bib': bib,
                'lap_count': len(laps),
                'total_laps': self.config.timing.total_laps,
                'lap_history': history,
            }

    def adjust_bib_lap(self, bib: str, action: str, lap_number: Optional[int] = None,
                        after_lap: Optional[int] = None, time_str: Optional[str] = None) -> Optional[Dict]:
        """Adjust a bib's lap count manually.

        Args:
            bib: Bib number.
            action: 'add' to insert synthetic lap, 'remove' to delete a lap.
            lap_number: For 'remove', which lap to delete (1-indexed). If None, removes last.
            after_lap: For 'add', insert after this lap number (0 = before first). If None, appends.
            time_str: For 'add', the TOD time string (HH:MM:SS.S). If None, uses current time.

        Returns:
            Updated bib detail dict, or None on error.
        """
        with self._reads_lock:
            if action == "add":
                if bib not in self._bib_laps:
                    self._bib_laps[bib] = []

                # Parse the time if provided
                if time_str:
                    timestamp = self._parse_time_str_to_datetime(time_str)
                    if timestamp is None:
                        return None
                else:
                    timestamp = datetime.now()

                new_lap = {
                    'timestamp': timestamp,
                    'reader_id': 'USER',
                    'manual': True,
                    'synthetic': True,
                }

                # Insert at the correct position
                if after_lap is not None:
                    insert_idx = after_lap  # after_lap=0 means insert at index 0
                    self._bib_laps[bib].insert(insert_idx, new_lap)
                    logger.info(f"Lap inserted at position {insert_idx + 1} for bib {bib} (now {len(self._bib_laps[bib])} laps)")
                else:
                    self._bib_laps[bib].append(new_lap)
                    logger.info(f"Lap appended for bib {bib} (now {len(self._bib_laps[bib])} laps)")

            elif action == "remove":
                if bib not in self._bib_laps or len(self._bib_laps[bib]) == 0:
                    return None

                if lap_number is not None:
                    # Remove specific lap (1-indexed)
                    idx = lap_number - 1
                    if idx < 0 or idx >= len(self._bib_laps[bib]):
                        return None
                    removed = self._bib_laps[bib].pop(idx)
                    logger.info(f"Lap {lap_number} removed for bib {bib} (now {len(self._bib_laps[bib])} laps)")
                else:
                    # Remove last lap
                    removed = self._bib_laps[bib].pop()
                    logger.info(f"Last lap removed for bib {bib} (now {len(self._bib_laps[bib])} laps)")

            else:
                return None

        return self.get_bib_detail(bib)

    def edit_bib_time(self, bib: str, time_str: str, lap_number: Optional[int] = None) -> Optional[Dict]:
        """Edit the TOD time for a bib's lap.

        Args:
            bib: Bib number.
            time_str: New TOD time string (HH:MM:SS.S).
            lap_number: Which lap to edit (1-indexed). If None, edits the last lap.

        Returns:
            Updated bib detail dict, or None on error.
        """
        timestamp = self._parse_time_str_to_datetime(time_str)
        if timestamp is None:
            return None

        with self._reads_lock:
            if bib not in self._bib_laps or len(self._bib_laps[bib]) == 0:
                return None

            if lap_number is not None:
                idx = lap_number - 1
                if idx < 0 or idx >= len(self._bib_laps[bib]):
                    return None
            else:
                idx = len(self._bib_laps[bib]) - 1

            self._bib_laps[bib][idx]['timestamp'] = timestamp
            logger.info(f"Time edited for bib {bib} lap {idx + 1}: {time_str}")

        return self.get_bib_detail(bib)

    def _parse_time_str_to_datetime(self, time_str: str) -> Optional[datetime]:
        """Parse a time string (HH:MM:SS.S) to datetime (today's date with that time)."""
        import re
        time_str = time_str.strip()

        patterns = [
            (r'^(\d{1,2}):(\d{2}):(\d{2})\.(\d{1,3})$', 'hms_ms'),
            (r'^(\d{1,2}):(\d{2}):(\d{2})$', 'hms'),
            (r'^(\d{1,2}):(\d{2})\.(\d{1,3})$', 'ms_ms'),
            (r'^(\d{1,2}):(\d{2})$', 'ms'),
        ]

        for pattern, fmt in patterns:
            match = re.match(pattern, time_str)
            if match:
                groups = match.groups()
                hours, minutes, seconds, ms = 0, 0, 0, 0

                if fmt == 'hms_ms':
                    hours, minutes, seconds = int(groups[0]), int(groups[1]), int(groups[2])
                    ms = int(groups[3].ljust(3, '0')) * 1000  # to microseconds
                elif fmt == 'hms':
                    hours, minutes, seconds = int(groups[0]), int(groups[1]), int(groups[2])
                elif fmt == 'ms_ms':
                    minutes, seconds = int(groups[0]), int(groups[1])
                    ms = int(groups[2].ljust(3, '0')) * 1000
                elif fmt == 'ms':
                    minutes, seconds = int(groups[0]), int(groups[1])

                now = datetime.now()
                return now.replace(hour=hours, minute=minutes, second=seconds, microsecond=ms)

        return None

    def get_remote_units(self) -> List[Dict]:
        """Get list of remote units with status."""
        if self.remote_manager:
            return self.remote_manager.get_units()
        return []

    def send_remote_command(self, reader_id: str, command: str) -> Dict:
        """Send command to a remote unit."""
        if not self.remote_manager:
            return {"success": False, "message": "Remote manager not active"}
        future = self.remote_manager.send_command(reader_id, command)
        try:
            return future.result(timeout=10.0)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def start_clock_check(self, reader_id: str, count: int = 10) -> Dict:
        """Ping a station over the radio (RemoteManager.start_clock_check)."""
        if not self.remote_manager:
            return {"success": False, "message": "Remote manager not active"}
        mgr = self.race_manager
        if mgr and any(r.status == "racing" for r in mgr.races.values()):
            return {"success": False, "message": "A race is running: the pings would hold up its reads"}
        return self.remote_manager.start_clock_check(reader_id, count)

    def get_clock_check(self, reader_id: str) -> Dict:
        if not self.remote_manager:
            return {"running": False, "sent": 0, "replies": 0}
        return self.remote_manager.get_clock_check(reader_id)

    def get_remote_config(self, reader_id: str, section: str = "all") -> Dict:
        """Fetch config from a remote unit."""
        if not self.remote_manager:
            return {"success": False, "message": "Remote manager not active"}
        future = self.remote_manager.get_remote_config(reader_id, section)
        try:
            return future.result(timeout=10.0)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def set_remote_config(self, reader_id: str, section: str, values: Dict) -> Dict:
        """Set config on a remote unit."""
        if not self.remote_manager:
            return {"success": False, "message": "Remote manager not active"}
        future = self.remote_manager.set_remote_config(reader_id, section, values)
        try:
            return future.result(timeout=10.0)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def add_pre_registered_unit(self, reader_id: str):
        """Add a pre-registered remote unit."""
        if reader_id not in self.config.lora.pre_registered_units:
            self.config.lora.pre_registered_units.append(reader_id)
        if self.remote_manager:
            self.remote_manager.add_pre_registered(reader_id)
        self.save_config()

    def remove_remote_unit(self, reader_id: str):
        """Remove a remote unit."""
        if reader_id in self.config.lora.pre_registered_units:
            self.config.lora.pre_registered_units.remove(reader_id)
        if self.remote_manager:
            self.remote_manager.remove_unit(reader_id)
        self.save_config()

    @property
    def is_running(self) -> bool:
        """Check if application is running."""
        return self._running
