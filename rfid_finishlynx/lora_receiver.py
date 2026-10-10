"""LoRa receiver for finish line - receives reads from remote lap points."""

import logging
import re
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .config import LoRaConfig
from .remote_protocol import ManagementMessage, format_ack
from .transport import (
    JOURNAL_LINE_FMT, LoRaBatch, LoRaMessage, LoRaTransport, journal_read_time,
    parse_any_lora_message,
)

logger = logging.getLogger(__name__)


class LoRaReceiver:
    """Receives RFID reads from remote lap points via LoRa.

    READ messages are acknowledged (when they carry a seq), deduplicated
    against retransmissions, and dispatched via on_message. Processing and
    forwarding to FinishLynx is the caller's responsibility so remote reads
    go through the same pipeline as local ones.
    """

    # How long a read is remembered for retransmit dedup. Keyed by content
    # (reader_id, bib, timestamp), not seq, so a retransmit arriving hours
    # late - the sender now retries until acknowledged - still can't double
    # count a runner.
    READ_DEDUP_TTL_SEC = 6 * 3600.0

    def __init__(
        self,
        config: LoRaConfig,
        on_message: Optional[Callable[[LoRaMessage], None]] = None,
        on_management_message: Optional[Callable[[ManagementMessage], None]] = None,
        journal_path: Optional[str] = None,
    ):
        """Initialize LoRa receiver.

        Args:
            config: LoRa configuration.
            on_message: Callback for received READ messages.
            on_management_message: Callback for management protocol messages.
            journal_path: Append-only log of every read that arrives.
        """
        self.config = config
        self.on_message = on_message
        self.on_management_message = on_management_message
        self._journal_path = journal_path

        self._transport: Optional[LoRaTransport] = None
        self._running = False
        self._seen_reads: dict = {}  # (reader_id, bib, timestamp) -> monotonic time
        self._stats = {
            "received": 0,
            "duplicates": 0,
            "errors": 0,
        }
        self._stats_lock = threading.Lock()

    def _on_receive(self, data: bytes):
        """Handle received LoRa data.

        Dispatches READ messages to on_message and management messages to
        the management callback.

        Args:
            data: Raw received data.
        """
        parsed = parse_any_lora_message(data)

        if parsed is None:
            with self._stats_lock:
                self._stats["errors"] += 1
            return

        # Management messages go to their own handler
        if isinstance(parsed, ManagementMessage):
            if self.on_management_message:
                self.on_management_message(parsed)
            return

        # The ACK means "accepted": it goes only after every read in the
        # message was processed, so a failure leaves the sender retrying.
        # A read counts as seen only once processed, so the retry of a
        # batch delivers just the reads that failed (2026-10-04 review).
        if isinstance(parsed, LoRaBatch):
            ok = all([self._accept_read(message) for message in parsed.reads])
            if ok and self._transport:
                self._transport.send(format_ack(parsed.reader_id, parsed.seq, "OK"))
            return

        # Single READ messages (LoRaMessage)
        message = parsed
        ok = self._accept_read(message, dedup=message.seq is not None)
        if ok and message.seq is not None and self._transport:
            # ACK delivery so the remote stops retransmitting.
            self._transport.send(format_ack(message.reader_id, message.seq, "OK"))

    def _accept_read(self, message: LoRaMessage, dedup: bool = True) -> bool:
        """Dedup, journal, and dispatch one read. False = it failed to
        process (not marked seen, so a retransmission tries again)."""
        if dedup and self._seen(message):
            with self._stats_lock:
                self._stats["duplicates"] += 1
            logger.debug(
                f"Duplicate read from {message.reader_id}: bib {message.bib} - dropped"
            )
            self._journal("DUP", message)
            return True

        logger.info(
            f"LoRa received: Bib {message.bib} at "
            f"{message.timestamp.strftime('%H:%M:%S.%f')[:-3]} "
            f"from {message.reader_id}"
        )

        if self.on_message:
            try:
                self.on_message(message)
            except Exception:
                logger.exception(f"Read from {message.reader_id} (bib {message.bib}) failed to "
                                 f"process; not acknowledged, the station will send it again")
                self._journal("FAIL", message)
                return False
        # RECV = accepted: a restart rebuilds the dedup memory from these
        self._journal("RECV", message)
        with self._stats_lock:
            self._stats["received"] += 1
        if dedup:
            self._remember(message)
        return True

    def _journal(self, event: str, message: LoRaMessage):
        if not self._journal_path:
            return
        stamp = datetime.now().strftime(JOURNAL_LINE_FMT)
        seq = f" seq={message.seq:02X}" if message.seq is not None else ""
        line = (
            f"{stamp} {event} bib={message.bib} "
            f"t={message.timestamp.strftime('%H:%M:%S.%f')[:-3]} "
            f"reader={message.reader_id}{seq}"
        )
        try:
            with open(self._journal_path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.error(f"Read journal write failed: {e}")

    def _seen(self, message: LoRaMessage) -> bool:
        """Was this read (by content) processed already? Prunes expired entries."""
        now = time.monotonic()
        with self._stats_lock:
            for key, seen_at in list(self._seen_reads.items()):
                if now - seen_at > self.READ_DEDUP_TTL_SEC:
                    del self._seen_reads[key]
            return (message.reader_id, message.bib, message.timestamp) in self._seen_reads

    def _remember(self, message: LoRaMessage):
        with self._stats_lock:
            self._seen_reads[(message.reader_id, message.bib, message.timestamp)] = time.monotonic()

    _RECV_RE = re.compile(r"^(\S+ \S+) RECV bib=(\S+) t=(\S+) reader=(\S+)")

    def _load_seen(self):
        """Reads accepted in the last READ_DEDUP_TTL_SEC, from the journal:
        a station resends what it never saw ACKed after either side
        restarts, and those must not count twice (2026-10-04 review)."""
        if not self._journal_path:
            return
        now, mono = datetime.now(), time.monotonic()
        try:
            with open(self._journal_path) as f:
                lines = f.readlines()
        except OSError:
            return
        loaded = 0
        for line in lines:
            m = self._RECV_RE.match(line)
            if not m:
                continue
            try:
                stamp = datetime.strptime(m.group(1), JOURNAL_LINE_FMT)
                ts = journal_read_time(stamp, m.group(3))
            except ValueError:
                continue
            age = (now - stamp).total_seconds()
            if 0 <= age < self.READ_DEDUP_TTL_SEC:
                self._seen_reads[(m.group(4), m.group(2), ts)] = mono - age
                loaded += 1
        if loaded:
            logger.info(f"Remembering {loaded} read(s) accepted before the restart (dedup)")

    def start(self) -> bool:
        """Start the LoRa receiver.

        Returns:
            True if started successfully.
        """
        if self._running:
            return True

        self._load_seen()
        self._transport = LoRaTransport(self.config, on_receive=self._on_receive)

        # The transport keeps a reconnect thread going even when the first
        # attempt fails (the gateway answers about a minute after power-up,
        # and the console usually boots first). The receiver is running from
        # here on; is_running turns true the moment the link opens. It used
        # to stay False forever after a failed first attempt, so the console
        # showed "radio link down" all meet while split reads were arriving.
        self._running = True

        if not self._transport.connect():
            logger.warning(f"LoRa gateway {self.config.port} not answering yet; "
                           "the link keeps retrying in the background")
            return False

        logger.info(f"LoRa receiver started on {self.config.port}")
        return True

    def stop(self):
        """Stop the LoRa receiver."""
        self._running = False

        if self._transport:
            self._transport.disconnect()
            self._transport = None

        logger.info("LoRa receiver stopped")

    def get_stats(self) -> dict:
        """Get receiver statistics.

        Returns:
            Dictionary with received, duplicate, and error counts.
        """
        with self._stats_lock:
            return self._stats.copy()

    def reset_stats(self):
        """Reset receiver statistics."""
        with self._stats_lock:
            self._stats = {
                "received": 0,
                "duplicates": 0,
                "errors": 0,
            }

    @property
    def transport(self) -> Optional[LoRaTransport]:
        """Get the underlying transport (for management commands)."""
        return self._transport

    @property
    def is_running(self) -> bool:
        """Check if receiver is running."""
        return self._running and self._transport is not None and self._transport.is_connected
