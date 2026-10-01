"""Transport layer for TCP and LoRa communication."""

import logging
import random
import socket
import threading
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import serial

from .config import LoRaConfig

logger = logging.getLogger(__name__)


@dataclass
class LoRaMessage:
    """Represents a parsed LoRa message."""
    bib: str
    timestamp: datetime
    reader_id: str
    seq: Optional[int] = None  # Present when sender wants delivery ACK


@dataclass
class LoRaBatch:
    """A parsed BATCH message: many reads under one sequence number."""
    reader_id: str
    seq: int
    reads: list  # of LoRaMessage


def calculate_checksum(data: str) -> str:
    """Calculate CRC-32 checksum for LoRa protocol.

    A single-byte XOR was measured passing corrupted frames on the bench:
    RF collisions mangle characters in pairs that XOR back to the same
    value, silently delivering reads with WRONG TIMESTAMPS. CRC-32 makes
    that effectively impossible.

    Args:
        data: String data (between $ and *).

    Returns:
        Eight-character hex checksum.
    """
    return f"{zlib.crc32(data.encode('ascii')) & 0xFFFFFFFF:08X}"


def format_lora_message(bib: str, timestamp: datetime, reader_id: str,
                        seq: Optional[int] = None) -> bytes:
    """Format a tag read for LoRa transmission.

    Format: $READ,<bib>,<timestamp>,<reader_id>[,<seq>]*<checksum>\n

    Args:
        bib: Bib number.
        timestamp: Read timestamp.
        reader_id: Reader identifier.
        seq: Optional sequence number (hex) requesting a delivery ACK.

    Returns:
        Formatted message as bytes.
    """
    time_str = timestamp.strftime("%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}"
    payload = f"READ,{bib},{time_str},{reader_id}"
    if seq is not None:
        payload += f",{seq:02X}"
    checksum = calculate_checksum(payload)
    message = f"${payload}*{checksum}\n"
    return message.encode("ascii")


def parse_lora_message(data: bytes) -> Optional[LoRaMessage]:
    """Parse a LoRa message.

    Args:
        data: Raw message bytes.

    Returns:
        Parsed LoRaMessage, or None if invalid.
    """
    try:
        text = data.decode("ascii").strip()

        # Validate format
        if not text.startswith("$") or "*" not in text:
            logger.warning(f"Invalid LoRa message format: {text}")
            return None

        # Extract payload and checksum
        payload_end = text.rfind("*")
        payload = text[1:payload_end]
        received_checksum = text[payload_end + 1:]

        # Verify checksum
        expected_checksum = calculate_checksum(payload)
        if received_checksum.upper() != expected_checksum:
            logger.warning(f"Checksum mismatch: expected {expected_checksum}, got {received_checksum}")
            return None

        # Parse fields
        parts = payload.split(",")
        if len(parts) not in (4, 5) or parts[0] != "READ":
            logger.warning(f"Invalid LoRa payload: {payload}")
            return None

        _, bib, time_str, reader_id = parts[:4]
        seq = int(parts[4], 16) if len(parts) == 5 else None

        return LoRaMessage(bib=bib, timestamp=_parse_read_time(time_str),
                           reader_id=reader_id, seq=seq)

    except Exception as e:
        logger.error(f"Error parsing LoRa message: {e}")
        return None


def _format_read_time(timestamp: datetime) -> str:
    return timestamp.strftime("%H:%M:%S.") + f"{timestamp.microsecond // 1000:03d}"


def _parse_read_time(time_str: str) -> datetime:
    """Parse HH:MM:SS.mmm into a datetime.

    Uses today's date; if that lands more than 12h in the future the read
    was taken just before midnight - use yesterday.
    """
    time_parts = time_str.split(":")
    hour = int(time_parts[0])
    minute = int(time_parts[1])
    sec_parts = time_parts[2].split(".")
    second = int(sec_parts[0])
    millisecond = int(sec_parts[1]) if len(sec_parts) > 1 else 0

    now = datetime.now()
    timestamp = now.replace(
        hour=hour, minute=minute, second=second,
        microsecond=millisecond * 1000
    )
    if (timestamp - now).total_seconds() > 12 * 3600:
        timestamp -= timedelta(days=1)
    return timestamp


def format_batch_message(reader_id: str, seq: int, reads) -> bytes:
    """Format many reads as one BATCH message.

    Format: $BATCH,<reader_id>,<seq>,<bib>@<HH:MM:SS.mmm>,...*<checksum>\n

    Args:
        reader_id: Reader identifier (shared by all reads).
        seq: Sequence number; the finish line ACKs the whole batch.
        reads: Iterable of (bib, timestamp) pairs.
    """
    entries = ",".join(
        f"{bib}@{_format_read_time(ts)}" for bib, ts in reads
    )
    payload = f"BATCH,{reader_id},{seq:02X},{entries}"
    checksum = calculate_checksum(payload)
    return f"${payload}*{checksum}\n".encode("ascii")


def parse_batch_message(data: bytes) -> Optional[LoRaBatch]:
    """Parse a BATCH message into a LoRaBatch of individual reads."""
    try:
        text = data.decode("ascii").strip()
        if not text.startswith("$") or "*" not in text:
            return None
        payload_end = text.rfind("*")
        payload = text[1:payload_end]
        if calculate_checksum(payload) != text[payload_end + 1:].upper():
            logger.warning(f"Checksum mismatch in batch: {text}")
            return None
        parts = payload.split(",")
        if len(parts) < 4 or parts[0] != "BATCH":
            return None
        reader_id = parts[1]
        seq = int(parts[2], 16)
        reads = []
        for entry in parts[3:]:
            bib, _, time_str = entry.partition("@")
            reads.append(LoRaMessage(
                bib=bib, timestamp=_parse_read_time(time_str),
                reader_id=reader_id, seq=seq,
            ))
        return LoRaBatch(reader_id=reader_id, seq=seq, reads=reads)
    except Exception as e:
        logger.error(f"Error parsing batch message: {e}")
        return None


def parse_tcp_endpoint(port: str):
    """Return (host, port) if `port` is a host:port endpoint, else None."""
    host, sep, tail = str(port).rpartition(":")
    if not sep or not host or not tail.isdigit():
        return None
    return host, int(tail)


class _TcpStream:
    """Serial-like wrapper around a TCP socket.

    Ethernet LoRa gateways (Ebyte E90-DTU "E" models) expose the radio's
    UART as a TCP socket instead of a serial port; the byte stream either
    side of it is identical.
    """

    def __init__(self, host: str, port: int, timeout: float = 0.1):
        self._sock = socket.create_connection((host, port), timeout=5.0)
        # Without keepalives a gateway that reboots or loses power leaves the
        # session half-open and reads look "connected" forever.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                self._sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
        self._sock.settimeout(timeout)

    def read(self, size: int) -> bytes:
        try:
            data = self._sock.recv(size)
        except socket.timeout:
            return b""
        if not data:
            raise OSError("LoRa gateway closed the connection")
        return data

    def write(self, data: bytes):
        self._sock.sendall(data)

    def flush(self):
        pass

    def close(self):
        self._sock.close()


class Transport(ABC):
    """Abstract base class for transport layers."""

    @abstractmethod
    def connect(self) -> bool:
        """Connect the transport."""
        pass

    @abstractmethod
    def disconnect(self):
        """Disconnect the transport."""
        pass

    @abstractmethod
    def send(self, data: bytes) -> bool:
        """Send data over the transport."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        pass


class LoRaTransport(Transport):
    """Transport for LoRa radios.

    `config.port` selects the link: a device path (``/dev/ttyUSB0``, ``COM3``)
    for serial modules, or ``host:port`` for ethernet gateways.
    """

    RECONNECT_MIN_SEC = 1.0
    RECONNECT_MAX_SEC = 30.0

    # The radio fragments anything longer than this into separate packets.
    MAX_PACKET_BYTES = 240

    # Per-write ceiling. The ~5 s transmit gate is per network WRITE, not
    # per RF packet: an oversized write is fragmented by the module itself
    # and the fragments go out ~0.12 s apart, bypassing the gate. The radio
    # buffers 512 bytes; two full fragments leave headroom under that.
    # Bench-verify delivery at this size before relying on it at range.
    MAX_WRITE_BYTES = 480

    # Random extra delay per write, as a fraction of the send interval.
    # Both stations pace at exactly the same interval, so without jitter
    # their transmissions can phase-lock and collide every cycle forever
    # (measured on the bench: ACKs died 100% once locked).
    TX_JITTER_FRAC = 0.3

    # Two stations share one channel with no carrier sense (LBT starves at
    # close range), so the only collision-free moment we can KNOW about is
    # right after the peer finishes transmitting - its own ~5 s gate then
    # holds it off the air. A reception is heard fragment by fragment;
    # SETTLE covers the trailing fragment's airtime, CLEAR is how long
    # after that the window is trusted.
    POST_RX_SETTLE_SEC = 1.2
    POST_RX_CLEAR_SEC = 3.5

    # Ceiling on messages held for a link that is down. Roughly 100 reads;
    # older ones are dropped first, since stale splits help nobody.
    MAX_OUTBOX_BYTES = 4096

    def __init__(
        self,
        config: LoRaConfig,
        on_receive: Optional[Callable[[bytes], None]] = None,
    ):
        """Initialize LoRa transport.

        Args:
            config: LoRa configuration.
            on_receive: Callback for received data.
        """
        self.config = config
        self.on_receive = on_receive
        # Called with the written bytes after each successful radio write,
        # so senders can clock retries from actual transmission rather
        # than from when a message entered the paced outbox.
        self.on_sent: Optional[Callable[[bytes], None]] = None

        self._serial = None  # serial.Serial or _TcpStream
        self._connected = False
        # True once the link has opened at all: a gateway that never answered
        # is "not here", one that dropped is an outage (console alarm).
        self.ever_connected = False
        self._running = False
        self._receive_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Outbound coalescing: the radio drops transmissions issued closer
        # together than send_interval_sec, but carries a full packet's worth
        # of reads in one go, so queued messages travel together.
        self._outbox = bytearray()
        self._outbox_event = threading.Event()
        self._last_write = 0.0
        self._last_receive = 0.0
        self._rx_event = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        """Open the LoRa link and keep it open.

        A background thread reconnects with backoff whenever the link drops,
        so a station left alone recovers from radio power cycles and gateway
        reboots without intervention.

        Returns:
            True if the link is open now. False means the first attempt
            failed; the background thread keeps retrying regardless.
        """
        if self._connected:
            return True

        opened = self._open()
        self._start_receive_thread()
        if self.config.send_interval_sec > 0:
            self._start_sender_thread()
        return opened

    def _open(self) -> bool:
        """Open the underlying stream. Returns True on success."""
        endpoint = parse_tcp_endpoint(self.config.port)
        try:
            if endpoint:
                stream = _TcpStream(*endpoint)
                logger.info(f"Connected to LoRa gateway at {self.config.port}")
            else:
                stream = serial.Serial(
                    port=self.config.port,
                    baudrate=self.config.baud,
                    timeout=0.1,
                    write_timeout=1.0,
                )
                logger.info(f"Connected to LoRa on {self.config.port} at {self.config.baud} baud")

            with self._lock:
                self._serial = stream
                self._connected = True
                self.ever_connected = True
            return True

        except OSError as e:
            logger.error(f"Failed to connect to LoRa: {e}")
            with self._lock:
                self._serial = None
                self._connected = False
            return False

    def _drop(self):
        """Close a failed stream so the keeper thread can reopen it."""
        with self._lock:
            if self._serial:
                try:
                    self._serial.close()
                except OSError:
                    pass
                self._serial = None
            self._connected = False

    def disconnect(self):
        """Close the LoRa link and stop reconnecting."""
        self._running = False
        self._stop_event.set()
        self._outbox_event.set()

        if self._sender_thread:
            self._sender_thread.join(timeout=2.0)
            self._sender_thread = None

        if self._receive_thread:
            self._receive_thread.join(timeout=2.0)
            self._receive_thread = None

        with self._lock:
            if self._serial:
                try:
                    self._serial.close()
                except OSError:
                    pass
                self._serial = None
            self._connected = False
            logger.info("Disconnected from LoRa")

    def send(self, data: bytes) -> bool:
        """Send data over LoRa.

        Args:
            data: Data to send.

        Returns:
            True if send successful.
        """
        if self.config.send_interval_sec > 0:
            with self._lock:
                self._outbox.extend(data)
            self._outbox_event.set()
            return True
        return self._write(data)

    def _write(self, data: bytes) -> bool:
        """Write straight to the link, bypassing any pacing."""
        needs_drop = False
        ok = False
        with self._lock:
            if not self._connected or not self._serial:
                return False

            if b"$GUN,ALL," in data:
                # The stations read the GUN's sent_at as "the finish clock
                # right now": stamp it as the frame leaves, not when it was
                # queued behind the send gap / clear-air wait.
                from .remote_protocol import restamp_gun
                now = time.time()
                data = b"".join(restamp_gun(line, now) for line in data.splitlines(keepends=True))
            try:
                self._serial.write(data)
                self._serial.flush()
                self._last_write = time.monotonic()
                logger.debug(f"Sent LoRa: {data.decode('ascii', 'replace').strip()}")
                ok = True
            except OSError as e:
                logger.error(f"Failed to send LoRa data: {e}")
                needs_drop = True
        if needs_drop:
            self._drop()
        if ok and self.on_sent:
            self.on_sent(data)
        return ok

    def send_tag_read(self, bib: str, timestamp: datetime, reader_id: str,
                      seq: Optional[int] = None) -> bool:
        """Send a tag read message over LoRa.

        Args:
            bib: Bib number.
            timestamp: Read timestamp.
            reader_id: Reader identifier.
            seq: Optional sequence number requesting a delivery ACK.

        Returns:
            True if send successful.
        """
        message = format_lora_message(bib, timestamp, reader_id, seq)
        return self.send(message)

    def _start_sender_thread(self):
        """Start the outbound pacing thread (idempotent)."""
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()

    def _sender_loop(self):
        """Flush queued messages, never faster than the radio allows."""
        interval = self.config.send_interval_sec

        while self._running:
            if not self._outbox_event.wait(0.5):
                continue
            if not self._running:
                break

            gap = interval - (time.monotonic() - self._last_write)
            if gap > 0 and self._stop_event.wait(gap):
                break
            if not self._await_clear_air(interval):
                break

            with self._lock:
                chunk = self._take_from_outbox()
                if not self._outbox:
                    self._outbox_event.clear()
            if chunk and not self._write(chunk):
                # Link died mid-flush: keep the messages for the next attempt
                # rather than dropping them on the floor.
                with self._lock:
                    self._outbox[:0] = chunk
                    self._trim_outbox()
                self._outbox_event.set()
                self._stop_event.wait(1.0)

    def _await_clear_air(self, interval: float) -> bool:
        """Hold the write until the channel is likely clear.

        Preferred slot: just after the peer's transmission finishes (it is
        then gated off the air). Fresh reception in progress: let its
        trailing fragment land first. Nothing heard: take a jittered slot
        so two silent stations can't phase-lock.

        Returns False if the transport is stopping.
        """
        dithered = False
        while self._running:
            since_rx = time.monotonic() - self._last_receive
            if since_rx < self.POST_RX_SETTLE_SEC:
                if self._stop_event.wait(self.POST_RX_SETTLE_SEC - since_rx):
                    return False
            elif since_rx <= self.POST_RX_CLEAR_SEC:
                # With three or more radios on the channel, two stations
                # can wake on the same clear window; a random dither keeps
                # them from firing at the same instant (then re-evaluate -
                # a peer's frame may have landed meanwhile).
                if not dithered:
                    dithered = True
                    if self._stop_event.wait(random.uniform(0, 0.6)):
                        return False
                    continue
                return True
            else:
                self._rx_event.clear()
                jitter = random.uniform(0.2, max(0.3, interval * self.TX_JITTER_FRAC))
                if not self._rx_event.wait(jitter):
                    return True  # nothing heard - take the jittered slot
        return False

    def _trim_outbox(self):
        """Drop the oldest whole messages once the backlog is too large."""
        if len(self._outbox) <= self.MAX_OUTBOX_BYTES:
            return
        excess = len(self._outbox) - self.MAX_OUTBOX_BYTES
        cut = self._outbox.find(b"\n", excess)
        cut = cut + 1 if cut >= 0 else len(self._outbox)
        dropped = self._outbox[:cut].count(b"\n")
        del self._outbox[:cut]
        logger.warning(f"LoRa backlog full, dropped {dropped} queued message(s)")

    def _take_from_outbox(self) -> bytes:
        """Pull whole messages off the queue, up to one write's worth."""
        if len(self._outbox) <= self.MAX_WRITE_BYTES:
            chunk = bytes(self._outbox)
            self._outbox.clear()
            return chunk

        cut = self._outbox.rfind(b"\n", 0, self.MAX_WRITE_BYTES + 1)
        cut = cut + 1 if cut >= 0 else self.MAX_WRITE_BYTES
        chunk = bytes(self._outbox[:cut])
        del self._outbox[:cut]
        return chunk

    def _start_receive_thread(self):
        """Start the receive/reconnect thread (idempotent)."""
        if self._receive_thread and self._receive_thread.is_alive():
            return
        self._running = True
        self._stop_event.clear()
        self._receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._receive_thread.start()

    def _receive_loop(self):
        """Receive LoRa data, reopening the link whenever it drops."""
        buffer = b""
        delay = self.RECONNECT_MIN_SEC

        while self._running:
            if not self._connected:
                if self._stop_event.wait(delay):
                    break
                if self._open():
                    delay = self.RECONNECT_MIN_SEC
                    buffer = b""
                else:
                    delay = min(delay * 2, self.RECONNECT_MAX_SEC)
                    logger.warning(f"LoRa link down, retrying in {delay:.0f}s")
                continue

            try:
                # Grab the stream under the lock but read outside it: the
                # read blocks for its timeout and this loop is tight, so a
                # writer (GUN broadcast, CMD) queueing on the lock could be
                # starved for seconds - the console's config save stalled
                # behind it with a TCP gateway connected.
                with self._lock:
                    stream = self._serial
                if not stream:
                    continue
                data = stream.read(256)

                if data:
                    buffer += data
                    self._last_receive = time.monotonic()
                    self._rx_event.set()

                    # Process complete messages (newline-terminated)
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line and self.on_receive:
                            self.on_receive(line + b"\n")

            except OSError as e:
                logger.error(f"LoRa receive error: {e}")
                self._drop()

    @property
    def is_connected(self) -> bool:
        """Check if LoRa is connected."""
        return self._connected


def parse_any_lora_message(data: bytes):
    """Parse any LoRa message - READ or management type.

    Returns:
        LoRaMessage for READ messages, ManagementMessage for management
        messages, or None if parsing fails.
    """
    try:
        text = data.decode("ascii").strip()
    except (UnicodeDecodeError, AttributeError):
        return None

    if not text.startswith("$") or "*" not in text:
        return None

    # Extract message type from payload
    payload_end = text.rfind("*")
    payload = text[1:payload_end]
    parts = payload.split(",")
    if not parts:
        return None

    msg_type = parts[0]

    if msg_type == "READ":
        return parse_lora_message(data)
    elif msg_type == "BATCH":
        return parse_batch_message(data)
    else:
        # Late import to avoid circular dependency
        from .remote_protocol import parse_management_message
        return parse_management_message(data)


class ReliableReadSender:
    """Batches reads and retransmits them until the finish line ACKs.

    Runs on remote_lap units. Reads queue briefly (up to one tick) and go
    out as BATCH messages - many reads under one sequence number - so a
    packed field shares one envelope instead of paying it per read.
    Unacknowledged batches are retransmitted forever with exponential
    backoff; a batch is only abandoned when the sender stops or its
    sequence number comes around again (256 outstanding batches). Every
    read, ACK, and drop is journaled to disk so nothing is lost silently
    even if it never crosses the air.
    """

    # The ACK rides the finish line's paced outbox too, so the worst-case
    # round trip is about two send intervals; retrying sooner would only
    # transmit duplicates.
    RETRY_INTERVAL_SEC = 2.0
    RETRY_MAX_SEC = 60.0
    TICK_SEC = 0.5
    BATCH_MAX_BYTES = LoRaTransport.MAX_WRITE_BYTES
    # Under continuous arrivals, wait until a full batch accumulates (or
    # the oldest read has lingered this long) before flushing - full
    # batches amortise the envelope; per-read batches melt down under load.
    LINGER_SEC = 1.0
    # A batch that never reached the air (trimmed from a full outbox)
    # is re-sent after this long.
    STUCK_RESEND_SEC = 60.0
    # Cap on unacknowledged batches in flight. Beyond this, reads wait in
    # the queue (journaled, nothing lost) instead of piling more retry
    # traffic onto an already-congested channel.
    MAX_PENDING_BATCHES = 8

    def __init__(self, transport: LoRaTransport, reader_id: str,
                 journal_path: Optional[str] = None):
        self._transport = transport
        self._reader_id = reader_id
        self._journal_path = journal_path
        pacing = getattr(transport, "config", None)
        if pacing is not None and getattr(pacing, "send_interval_sec", 0) > 0:
            self.RETRY_INTERVAL_SEC = pacing.send_interval_sec * 2 + 2.0
        self._queue: list = []  # (bib, timestamp) awaiting a batch
        self._queue_since = 0.0  # when the oldest queued read arrived
        # seq -> {"message", "sent_at", "retries", "reads", "transmitted",
        #         "created_at"}. sent_at is the last actual radio write, so
        # a batch stuck behind a backlog isn't retried before it ever flies.
        self._pending: dict = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._running = False
        self._retry_thread: Optional[threading.Thread] = None
        transport.on_sent = self._on_transmitted

    def _on_transmitted(self, chunk: bytes):
        """Stamp pending batches that just left in this radio write."""
        now = time.monotonic()
        with self._lock:
            for entry in self._pending.values():
                if entry["message"] in chunk:
                    entry["transmitted"] = True
                    entry["sent_at"] = now

    def _journal(self, event: str, bib: str, seq: Optional[int] = None,
                 extra: str = ""):
        if not self._journal_path:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        line = f"{stamp} {event}"
        if seq is not None:
            line += f" seq={seq:02X}"
        line += f" bib={bib}"
        if extra:
            line += f" {extra}"
        try:
            with open(self._journal_path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.error(f"Read journal write failed: {e}")

    def start(self):
        """Start the batch/retry thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._retry_thread = threading.Thread(target=self._retry_loop, daemon=True)
        self._retry_thread.start()

    def stop(self):
        """Stop the thread; queued and pending reads are dropped (journaled)."""
        self._running = False
        if self._retry_thread:
            self._retry_thread.join(timeout=2.0)
            self._retry_thread = None
        with self._lock:
            dropped = len(self._queue) + sum(
                len(e["reads"]) for e in self._pending.values()
            )
            if dropped:
                logger.warning(f"Dropping {dropped} unacknowledged reads on stop")
            for bib, _ts in self._queue:
                self._journal("DROP", bib, extra="reason=stop")
            for seq, entry in self._pending.items():
                for bib, _ts in entry["reads"]:
                    self._journal("DROP", bib, seq, "reason=stop")
            self._queue.clear()
            self._pending.clear()

    def send_read(self, bib: str, timestamp: datetime) -> bool:
        """Record a tag read for delivery.

        The read is journaled and queued immediately; it leaves in the next
        batch (at most one tick away, or right now if the sender's thread
        is not running).
        """
        self._journal("READ", bib, extra=f"t={_format_read_time(timestamp)}")
        with self._lock:
            if not self._queue:
                self._queue_since = time.monotonic()
            self._queue.append((bib, timestamp))
        if not self._running:
            self.flush()
        return True

    def _flush_due(self) -> bool:
        """A full batch has accumulated, or the oldest read has lingered."""
        with self._lock:
            if not self._queue:
                return False
            if len(self._pending) >= self.MAX_PENDING_BATCHES:
                return False
            size = 21 + len(self._reader_id) + sum(
                14 + len(bib) for bib, _ts in self._queue
            )
            if size >= self.BATCH_MAX_BYTES:
                return True
            return time.monotonic() - self._queue_since >= self.LINGER_SEC

    def flush(self):
        """Package the currently queued reads into batches and transmit.

        One pass over a snapshot: reads that arrive while a flush is in
        progress wait for the next tick, so continuous arrivals can't spin
        out endless tiny batches.
        """
        with self._lock:
            queue, self._queue = self._queue, []
        while queue:
            # Envelope: $BATCH,<reader>,<seq>, ... *CRC32\n plus one comma
            # per entry (over-counts the first entry's comma by design).
            size = 21 + len(self._reader_id)
            reads = []
            while queue:
                bib, ts = queue[0]
                entry_len = 1 + len(bib) + 1 + 12  # ,bib@HH:MM:SS.mmm
                if reads and size + entry_len > self.BATCH_MAX_BYTES:
                    break
                size += entry_len
                reads.append(queue.pop(0))
            with self._lock:
                seq = self._seq
                self._seq = (self._seq + 1) % 256
                stale = self._pending.pop(seq, None)
                message = format_batch_message(self._reader_id, seq, reads)
                now = time.monotonic()
                self._pending[seq] = {
                    "message": message,
                    "sent_at": now,
                    "created_at": now,
                    "retries": 0,
                    "reads": reads,
                    "transmitted": False,
                }
            if stale is not None:
                logger.warning(
                    f"Sequence {seq:02X} reused with {len(stale['reads'])} "
                    f"read(s) still unacknowledged - dropped"
                )
                for bib, _ts in stale["reads"]:
                    self._journal("DROP", bib, seq, "reason=seq-reuse")
            for bib, _ts in reads:
                self._journal("SENT", bib, seq)
            self._transport.send(message)

    def handle_ack(self, seq: int):
        """Mark a batch as delivered."""
        with self._lock:
            entry = self._pending.pop(seq, None)
        if entry is not None:
            logger.debug(f"Batch seq={seq:02X} acknowledged by finish line")
            for bib, _ts in entry["reads"]:
                self._journal("ACKED", bib, seq, f"retries={entry['retries']}")

    @property
    def pending_count(self) -> int:
        """Reads not yet acknowledged (queued plus in-flight)."""
        with self._lock:
            return len(self._queue) + sum(
                len(e["reads"]) for e in self._pending.values()
            )

    def _retry_loop(self):
        while self._running:
            if self._flush_due():
                self.flush()
            now = time.monotonic()
            to_send = []
            with self._lock:
                for seq, entry in self._pending.items():
                    if not entry["transmitted"]:
                        # Still waiting in the outbox; only re-send if it
                        # has been stuck long enough to have been trimmed.
                        if now - entry["created_at"] < self.STUCK_RESEND_SEC:
                            continue
                        entry["created_at"] = now
                    else:
                        backoff = min(
                            self.RETRY_INTERVAL_SEC * (2 ** entry["retries"]),
                            self.RETRY_MAX_SEC,
                        )
                        if now - entry["sent_at"] < backoff:
                            continue
                        entry["retries"] += 1
                        entry["transmitted"] = False
                        entry["created_at"] = now
                    to_send.append((seq, entry["message"], entry["retries"]))
            for seq, message, attempt in to_send:
                self._transport.send(message)
                logger.debug(f"Retransmitting batch seq={seq:02X}, attempt {attempt + 1}")
            time.sleep(self.TICK_SEC)
