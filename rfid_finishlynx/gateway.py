"""Ebyte E90-DTU ethernet gateway: discovery, adoption and radio settings.

The gateway is the box that turns the LoRa radio's UART into a TCP socket.
It ships on a fixed factory address, which is the usual reason a freshly
cabled station cannot see it: the PC and the gateway end up on different
subnets and nothing routes between them.

This module finds the gateway regardless of which subnet it holds, moves it
to the station's standard address, and reads back the RF parameters that
must match at both ends of the link.
"""

import http.client
import json
import logging
import re
import socket
import subprocess
import sys
import time
import zlib
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Chengdu Ebyte OUI - the first half of every gateway's MAC address.
EBYTE_OUI = "78:ee:4c"

# Where a gateway is likely to answer: the address we standardise on, and
# the factory default it arrives with.
STANDARD_IP = "192.168.0.60"
FACTORY_IP = "192.168.4.101"

DATA_PORT = 8886  # Socket A, the radio's byte stream
HTTP_TIMEOUT = 4.0


class GatewayError(Exception):
    """Gateway did not respond as expected."""


def _decompress(payload: bytes) -> str:
    """Decode a response body; the gateway gzips some of them."""
    for window in (31, -15, 15):
        try:
            return zlib.decompress(payload, window).decode("utf-8", "replace")
        except zlib.error:
            continue
    return payload.decode("utf-8", "replace")


def _normalise_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def arp_neighbours() -> Dict[str, str]:
    """Return {ip: mac} from the OS neighbour table.

    ARP works below IP, so a directly cabled gateway shows up here even when
    its address is on a subnet the PC knows nothing about.
    """
    commands = [["ip", "neigh", "show"], ["arp", "-a"], ["arp", "-an"]]
    output = ""
    for command in commands:
        try:
            output = subprocess.run(
                command, capture_output=True, text=True, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if output.strip():
            break

    neighbours = {}
    ip_pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
    mac_pattern = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
    for line in output.splitlines():
        ip_match = ip_pattern.search(line)
        mac_match = mac_pattern.search(line)
        if ip_match and mac_match:
            neighbours[ip_match.group(1)] = _normalise_mac(mac_match.group(0))
    return neighbours


def candidate_addresses(extra: Optional[List[str]] = None) -> List[str]:
    """Addresses worth probing for a gateway, best guess first."""
    candidates = list(extra or [])
    candidates += [STANDARD_IP, FACTORY_IP]
    candidates += [ip for ip, mac in arp_neighbours().items()
                   if mac.startswith(EBYTE_OUI)]

    seen, ordered = set(), []
    for ip in candidates:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered


def bind_socket(sock: socket.socket, interface: Optional[str]):
    """Pin a socket to one interface, ignoring the routing table.

    Needed when something else claims the gateway's subnet - a VPN or a
    Tailscale subnet route will otherwise swallow traffic aimed at a
    directly cabled device.
    """
    if not interface:
        return
    if not hasattr(socket, "SO_BINDTODEVICE"):
        raise GatewayError("binding to an interface is not supported on this platform")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                    interface.encode() + b"\0")


def _connect(host: str, port: int, interface: Optional[str],
             timeout: float = HTTP_TIMEOUT) -> http.client.HTTPConnection:
    """An HTTP connection, optionally pinned to an interface."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    if interface:
        sock = socket.socket()
        sock.settimeout(timeout)
        bind_socket(sock, interface)
        try:
            sock.connect((host, port))
        except OSError:
            sock.close()
            raise
        connection.sock = sock
    return connection


class EbyteGateway:
    """HTTP client for the gateway's configuration interface."""

    def __init__(self, host: str, username: str = "admin", password: str = "admin",
                 interface: Optional[str] = None):
        self.host = host
        self.username = username
        self.password = password
        self.interface = interface
        self._token: Optional[str] = None

    # -- transport ---------------------------------------------------------

    def _fetch(self, path: str, data: Optional[bytes] = None, auth: bool = True) -> str:
        if auth:
            if not self._token:
                self.login()
            separator = "&" if "?" in path else "?"
            path += f"{separator}token={self._token}&date={int(time.time() * 1000)}"

        connection = None
        try:
            connection = _connect(self.host, 80, self.interface)
            connection.request("POST" if data is not None else "GET", f"/{path}", body=data)
            return _decompress(connection.getresponse().read())
        except (OSError, http.client.HTTPException) as e:
            raise GatewayError(f"{self.host}: {e}") from e
        finally:
            if connection:
                connection.close()

    def login(self):
        """Authenticate; the token goes on every later request."""
        import hashlib
        import hmac

        script = self._fetch("login.js", auth=False)
        match = re.search(r"RAND_KEY\s*=\s*['\"]([^'\"]+)['\"]", script)
        if not match:
            raise GatewayError(f"{self.host}: no RAND_KEY in login.js")

        digest = hmac.new(
            self.password.encode(),
            (self.username + match.group(1)).encode(),
            hashlib.sha1,
        ).hexdigest()

        token = self._fetch("loginsubmit.json", data=digest.encode(), auth=False).strip()
        if not token:
            raise GatewayError(f"{self.host}: login rejected")
        self._token = token

    # -- configuration -----------------------------------------------------

    def get(self, name: str) -> dict:
        """Read a configuration section, e.g. 'basic' or 'wireless'."""
        for attempt in (1, 2):
            body = self._fetch(f"{name}.json")
            try:
                return json.loads(body)
            except json.JSONDecodeError as e:
                # A stale token gets the login page instead of JSON.
                if attempt == 2:
                    raise GatewayError(f"{self.host}: bad {name}.json response") from e
                self._token = None

    def put(self, name: str, values: dict):
        """Write a configuration section back."""
        self._fetch(f"{name}.json", data=json.dumps(values).encode())

    def commit(self, reboot: bool = False):
        """Persist staged configuration, optionally restarting the gateway."""
        command = "web set device reboot" if reboot else "parameter send completed"
        self._fetch("DeviceContrl", data=command.encode())
        if reboot:
            self._token = None  # tokens do not survive a restart

    def identify(self) -> dict:
        """Model, firmware, serial and MAC."""
        return self.get("static")

    def radio(self) -> dict:
        """RF parameters that must match at both ends of the link."""
        wireless = self.get("wireless")
        module = wireless.get("wlsmoudle", "")
        settings = {
            "module": module,
            "frequency_mhz": wireless.get("wlsfrequnce"),
            "power_class": wireless.get("wlspowertype"),
        }
        settings.update({
            key: value for key, value in wireless.get(module, {}).items()
            if key in RADIO_KEYS
        })
        return settings

    def set_radio(self, power: Optional[str] = None, air_rate: Optional[str] = None,
                  channel: Optional[int] = None) -> dict:
        """Change RF settings and reboot. Both stations must match.

        Returns the values actually written.
        """
        wireless = self.get("wireless")
        module = wireless.get("wlsmoudle", "")
        band = str(wireless.get("wlsfrequnce"))
        settings = wireless.get(module, {})

        if power is not None:
            if power not in POWER_LEVELS:
                raise GatewayError(f"power must be one of {sorted(POWER_LEVELS)}")
            settings["wlspower"] = POWER_LEVELS[power]
        if air_rate is not None:
            rates = AIR_RATES.get(band, {})
            if air_rate not in rates:
                raise GatewayError(
                    f"air rate for {band} MHz must be one of {sorted(rates)} kbps")
            settings["wlsairboud"] = rates[air_rate]
        if channel is not None:
            settings["wlschannel"] = int(channel)

        wireless[module] = settings
        self.put("wireless", wireless)
        self.commit(reboot=True)
        return settings

    def set_static_ip(self, ip: str, mask: str = "255.255.255.0",
                      router: Optional[str] = None):
        """Pin the gateway to a fixed address and reboot it."""
        basic = self.get("basic")
        basic["net_DHCP"] = 0
        basic["net_localIP"] = [int(part) for part in ip.split(".")]
        basic["net_mask"] = [int(part) for part in mask.split(".")]
        if router:
            basic["net_getway"] = [int(part) for part in router.split(".")]
        self.put("basic", basic)
        self.commit(reboot=True)


# Transmit power, weakest last. Two radios on one bench at "maximum" can
# swamp each other's receivers; a mile apart they need every dB.
POWER_LEVELS = {"maximum": 0, "middle": 1, "low": 2, "minimum": 3}

# Air rate in kbps -> setting index, per frequency band. Slower is longer
# range but more time on air per read.
AIR_RATES = {
    "230": {"2.4": 3, "4.8": 4, "9.6": 5, "15.6": 6},
    "400": {"2.4": 2, "4.8": 3, "9.6": 4, "19.2": 5, "38.4": 6, "62.5": 7},
    "900": {"2.4": 2, "4.8": 3, "9.6": 4, "19.2": 5, "38.4": 6, "62.5": 7},
}


# RF settings a mismatch in which silently produces no data at all.
RADIO_KEYS = (
    "wlsaddress",
    "wlschannel",
    "wlsnetid",
    "wlsairboud",
    "wlspower",
    "wlstransmode",
    "wlspacklength",
)


def probe(host: str, username: str = "admin", password: str = "admin",
          interface: Optional[str] = None) -> Optional[dict]:
    """Return a gateway's identity if one answers at `host`, else None."""
    gateway = EbyteGateway(host, username, password, interface)
    try:
        info = gateway.identify()
    except GatewayError:
        return None
    return {
        "ip": host,
        "interface": interface,
        "model": info.get("static_moudle"),
        "firmware": info.get("static_FW"),
        "serial": (info.get("static_SN") or "").strip(),
        "mac": _normalise_mac(info.get("static_MAC", "")),
    }


def discover(extra: Optional[List[str]] = None,
             username: str = "admin", password: str = "admin",
             interface: Optional[str] = None) -> List[dict]:
    """Find every gateway that answers on the local link.

    Each address is tried through the routing table first, then pinned to
    `interface` if that fails, so a gateway stays findable when a VPN route
    covers its subnet.
    """
    found, seen = [], set()
    for ip in candidate_addresses(extra):
        for iface in (None, interface):
            result = probe(ip, username, password, iface)
            if result and result["mac"] not in seen:
                seen.add(result["mac"])
                found.append(result)
                break
    return found


def wired_interfaces() -> List[str]:
    """Names of wired interfaces that could have a gateway on them."""
    names = []
    try:
        for _, name in socket.if_nameindex():
            if name != "lo" and not name.startswith(("wl", "tun", "tail", "docker", "br-", "veth", "virbr")):
                names.append(name)
    except OSError:
        pass
    return names


def data_port_open(host: str, port: int = DATA_PORT, timeout: float = 3.0,
                   interface: Optional[str] = None) -> bool:
    """True if the radio's byte stream is accepting connections."""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        bind_socket(sock, interface)
        sock.connect((host, port))
        return True
    except (OSError, GatewayError):
        return False
    finally:
        sock.close()


def compare_radio(local: dict, reference: dict) -> List[str]:
    """Return human-readable differences between two radio configurations."""
    differences = []
    for key in sorted(set(local) | set(reference)):
        mine, theirs = local.get(key), reference.get(key)
        if mine != theirs:
            differences.append(f"{key}: {mine!r} != {theirs!r}")
    return differences


def unreachable_hint(ip: str) -> str:
    """Tell the operator how to reach a gateway on a foreign subnet."""
    prefix = ip.rsplit(".", 1)[0]
    if sys.platform == "win32":
        return (
            f"No route to {ip}. It is probably on a different subnet than this PC.\n"
            f"Run as Administrator, substituting your adapter name:\n"
            f'  netsh interface ip add address "Ethernet" {prefix}.98 255.255.255.0'
        )
    return (
        f"No route to {ip}. It is probably on a different subnet than this PC.\n"
        f"Add a temporary address on the wired interface:\n"
        f"  sudo ip addr add {prefix}.98/24 dev <interface>"
    )
