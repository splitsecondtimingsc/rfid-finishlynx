"""Management protocol for LoRa-based remote unit communication."""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .transport import calculate_checksum

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """LoRa management message types."""
    ANNOUNCE = "ANNOUNCE"
    CMD = "CMD"
    ACK = "ACK"
    CFGQ = "CFGQ"
    CFGR = "CFGR"
    CFGS = "CFGS"
    GUN = "GUN"
    PING = "PING"
    PONG = "PONG"


@dataclass
class ManagementMessage:
    """Parsed management message."""
    msg_type: MessageType
    reader_id: str
    seq: Optional[int] = None  # None for ANNOUNCE
    fields: Dict[str, Any] = field(default_factory=dict)


def encode_config_values(config_dict: Dict[str, Any]) -> str:
    """Encode a config dict as key=value;key=value string.

    Lists use '.' as delimiter (e.g., antennas=1.2.3.4).
    Booleans encoded as 0/1.
    """
    parts = []
    for key, value in config_dict.items():
        if isinstance(value, bool):
            parts.append(f"{key}={'1' if value else '0'}")
        elif isinstance(value, list):
            list_str = ".".join(str(v) for v in value)
            parts.append(f"{key}={list_str}")
        else:
            parts.append(f"{key}={value}")
    return ";".join(parts)


def decode_config_values(encoded: str) -> Dict[str, str]:
    """Decode key=value;key=value string to dict.

    Returns raw string values; caller is responsible for type conversion.
    """
    result = {}
    if not encoded:
        return result
    for pair in encoded.split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key] = value
    return result


def format_announce(reader_id: str, version: str, state: str) -> bytes:
    """Format an ANNOUNCE heartbeat message."""
    payload = f"ANNOUNCE,{reader_id},{version},{state}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_cmd(reader_id: str, seq: int, command: str, args: str = "") -> bytes:
    """Format a CMD message to a remote unit."""
    if args:
        payload = f"CMD,{reader_id},{seq:02X},{command},{args}"
    else:
        payload = f"CMD,{reader_id},{seq:02X},{command}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_ack(reader_id: str, seq: int, status: str, data: str = "") -> bytes:
    """Format an ACK response message."""
    if data:
        payload = f"ACK,{reader_id},{seq:02X},{status},{data}"
    else:
        payload = f"ACK,{reader_id},{seq:02X},{status}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_cfgq(reader_id: str, seq: int, section: str) -> bytes:
    """Format a CFGQ (config query) message."""
    payload = f"CFGQ,{reader_id},{seq:02X},{section}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_cfgr(reader_id: str, seq: int, section: str, values: Dict[str, Any]) -> bytes:
    """Format a CFGR (config response) message."""
    encoded = encode_config_values(values)
    payload = f"CFGR,{reader_id},{seq:02X},{section},{encoded}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_cfgs(reader_id: str, seq: int, section: str, values: Dict[str, Any]) -> bytes:
    """Format a CFGS (config set) message."""
    encoded = encode_config_values(values)
    payload = f"CFGS,{reader_id},{seq:02X},{section},{encoded}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def format_gun(gun_epoch: float, hour_format: Optional[str] = None,
               sent_at: Optional[float] = None) -> bytes:
    """Format a GUN broadcast (finish -> all stations, fire-and-forget).

    gun_epoch is the gun time as Unix epoch seconds; 0 = no race running
    (stations return their clocks to time-of-day). hour_format ("12" or
    "24") is the finish console's time-of-day display, mirrored on the
    stations' clocks; omitted = leave the station's own setting alone
    (older senders never include it). sent_at is the finish console's own
    clock (epoch seconds) at the moment of sending: a split station has no
    clock source of its own, so it uses this to line its clock up with the
    finish line. Older receivers ignore the extra field.
    """
    payload = f"GUN,ALL,{gun_epoch:.2f}"
    fmt = hour_format if hour_format in ("12", "24") else ""
    if fmt or sent_at is not None:
        payload += f",{fmt}"
    if sent_at is not None:
        payload += f",{sent_at:.2f}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


# Clock check (2026-10-04): the finish console sends PING, the station
# answers PONG, and four timestamps give the round trip and the station's
# clock error the way NTP does:
#   t1 console sends, t2 station receives, t3 station replies, t4 console receives
#   delay  = (t4 - t1) - (t3 - t2)
#   offset = ((t2 - t1) + (t3 - t4)) / 2      (station clock minus console clock)
# t1 and t3 are stamped as the frame leaves for the gateway (restamp_line), so
# time spent queued behind the radio's send gap is not counted. t2/t3 are in
# the station's corrected clock (the one its reads are stamped in), so offset
# is the error of its read times. PING carries blank t2/t3/air fields so both
# frames are the same length and take the same airtime each way; air is the
# station's lora.air_latency_sec, so the console can suggest a better one.
_STAMP = "{:014.3f}"
_BLANK = _STAMP.format(0)


def format_ping(reader_id: str, seq: int, t1: float = 0.0) -> bytes:
    """PING,<reader_id>,<seq>,<t1>,<blank>,<blank>,<blank air>"""
    payload = f"PING,{reader_id},{seq:02X},{_STAMP.format(t1)},{_BLANK},{_BLANK},0.000"
    return f"${payload}*{calculate_checksum(payload)}\n".encode("ascii")


def format_pong(reader_id: str, seq: int, t1: float, t2: float, t3: float = 0.0,
                air: float = 0.0) -> bytes:
    """PONG,<reader_id>,<seq>,<t1 echoed>,<t2>,<t3>,<air latency>"""
    payload = (f"PONG,{reader_id},{seq:02X},{_STAMP.format(t1)},{_STAMP.format(t2)},"
               f"{_STAMP.format(t3)},{min(max(air, 0.0), 9.999):.3f}")
    return f"${payload}*{calculate_checksum(payload)}\n".encode("ascii")


def restamp_line(line: bytes, now: float) -> bytes:
    """Stamp a GUN's sent_at, a PING's t1 or a PONG's t3 with `now`, at the
    moment the frame goes to the radio. Other lines come back unchanged."""
    if line.startswith(b"$GUN,ALL,"):
        return restamp_gun(line, now)
    if not line.startswith((b"$PING,", b"$PONG,")):
        return line
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError:
        return line
    if "*" not in text:
        return line
    parts = text[1:text.rfind("*")].split(",")
    if len(parts) != 7:
        return line
    parts[3 if parts[0] == "PING" else 5] = _STAMP.format(now)
    payload = ",".join(parts)
    return f"${payload}*{calculate_checksum(payload)}\n".encode("ascii")


def clock_check(samples) -> dict:
    """Summarise (t1, t2, t3, t4) samples. The fastest round trips spent the
    least time held in a buffer, so the offset is taken from those."""
    rows = sorted(((t4 - t1) - (t3 - t2), ((t2 - t1) + (t3 - t4)) / 2) for t1, t2, t3, t4 in samples)
    if not rows:
        return {"replies": 0}
    delays = [d for d, _ in rows]
    best = rows[:max(1, len(rows) // 3)]
    return {
        "replies": len(rows),
        "delay_min": round(delays[0], 4),
        "delay_median": round(delays[len(delays) // 2], 4),
        "delay_max": round(delays[-1], 4),
        "offset": round(sorted(o for _, o in best)[len(best) // 2], 4),
        "offset_spread": round(max(o for _, o in best) - min(o for _, o in best), 4),
    }


def restamp_gun(line: bytes, now: float) -> bytes:
    """Rewrite the sent_at field of one GUN line to `now`, at the moment it
    actually goes on the air.

    A GUN sits in the paced outbox for anything from 0.2 s to several
    seconds (send gap, clear-air wait, jitter) after it was formatted, and
    the stations take sent_at as "the finish console's clock right now" -
    a stale stamp reads as clock offset and puts their race clocks that
    much behind. Lines that are not a GUN with a sent_at are returned
    unchanged.
    """
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError:
        return line
    if not text.startswith("$GUN,ALL,") or "*" not in text:
        return line
    payload = text[1:text.rfind("*")]
    parts = payload.split(",")
    if len(parts) < 5:
        return line
    parts[4] = f"{now:.2f}"
    payload = ",".join(parts)
    return f"${payload}*{calculate_checksum(payload)}\n".encode("ascii")


def parse_management_message(data: bytes) -> Optional[ManagementMessage]:
    """Parse a management message from raw bytes.

    Expected format: $TYPE,<fields>*<checksum>\n
    """
    try:
        text = data.decode("ascii").strip()
    except (UnicodeDecodeError, AttributeError):
        return None

    if not text.startswith("$") or "*" not in text:
        return None

    # Extract payload and checksum
    payload_end = text.rfind("*")
    payload = text[1:payload_end]
    received_checksum = text[payload_end + 1:]

    # Verify checksum
    expected_checksum = calculate_checksum(payload)
    if received_checksum.upper() != expected_checksum.upper():
        logger.debug(f"Checksum mismatch: got {received_checksum}, expected {expected_checksum}")
        return None

    # Split fields
    parts = payload.split(",")
    if len(parts) < 2:
        return None

    msg_type_str = parts[0]
    try:
        msg_type = MessageType(msg_type_str)
    except ValueError:
        logger.debug(f"Unknown message type: {msg_type_str}")
        return None

    if msg_type == MessageType.GUN:
        # GUN,ALL,<epoch seconds>[,<12|24>[,<sender's clock, epoch>]]
        # (epoch 0 = no race; the optional fields mirror the finish
        # console's time-of-day format and carry its clock for stations
        # without one). Anything unparseable in an optional field is
        # treated as absent, never as a bad message.
        if len(parts) < 3:
            return None
        try:
            epoch = float(parts[2])
        except ValueError:
            return None
        hour_format = parts[3].strip() if len(parts) > 3 and parts[3].strip() in ("12", "24") else None
        sent_at = None
        if len(parts) > 4:
            try:
                sent_at = float(parts[4])
            except ValueError:
                sent_at = None
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=None,
            fields={"epoch": epoch, "hour_format": hour_format, "sent_at": sent_at},
        )

    if msg_type in (MessageType.PING, MessageType.PONG):
        # PING|PONG,<reader_id>,<seq>,<t1>,<t2>,<t3>,<air>
        if len(parts) != 7:
            return None
        try:
            t1, t2, t3, air = (float(x) for x in parts[3:7])
            seq = int(parts[2], 16)
        except ValueError:
            return None
        return ManagementMessage(msg_type=msg_type, reader_id=parts[1], seq=seq,
                                 fields={"t1": t1, "t2": t2, "t3": t3, "air": air})

    if msg_type == MessageType.ANNOUNCE:
        # ANNOUNCE,<reader_id>,<version>,<state>
        if len(parts) < 4:
            return None
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=None,
            fields={
                "version": parts[2],
                "state": parts[3],
            },
        )

    elif msg_type == MessageType.CMD:
        # CMD,<reader_id>,<seq>,<command>[,<args>]
        if len(parts) < 4:
            return None
        seq = int(parts[2], 16)
        fields = {"command": parts[3]}
        if len(parts) > 4:
            fields["args"] = ",".join(parts[4:])
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=seq,
            fields=fields,
        )

    elif msg_type == MessageType.ACK:
        # ACK,<reader_id>,<seq>,<status>[,<data>]
        if len(parts) < 4:
            return None
        seq = int(parts[2], 16)
        fields = {"status": parts[3]}
        if len(parts) > 4:
            fields["data"] = ",".join(parts[4:])
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=seq,
            fields=fields,
        )

    elif msg_type == MessageType.CFGQ:
        # CFGQ,<reader_id>,<seq>,<section>
        if len(parts) < 4:
            return None
        seq = int(parts[2], 16)
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=seq,
            fields={"section": parts[3]},
        )

    elif msg_type == MessageType.CFGR:
        # CFGR,<reader_id>,<seq>,<section>,<k=v;k=v>
        if len(parts) < 5:
            return None
        seq = int(parts[2], 16)
        encoded_values = ",".join(parts[4:])  # Rejoin in case values contain commas (shouldn't, but safe)
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=seq,
            fields={
                "section": parts[3],
                "values": decode_config_values(encoded_values),
            },
        )

    elif msg_type == MessageType.CFGS:
        # CFGS,<reader_id>,<seq>,<section>,<k=v;k=v>
        if len(parts) < 5:
            return None
        seq = int(parts[2], 16)
        encoded_values = ",".join(parts[4:])
        return ManagementMessage(
            msg_type=msg_type,
            reader_id=parts[1],
            seq=seq,
            fields={
                "section": parts[3],
                "values": decode_config_values(encoded_values),
            },
        )

    return None
