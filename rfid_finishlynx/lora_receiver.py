"""LoRa receiver for finish line - receives reads from remote lap points."""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .config import LoRaConfig
from .remote_protocol import ManagementMessage, format_ack
from .transport import LoRaBatch, LoRaMessage, LoRaTransport, parse_any_lora_message

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

        # BATCH: ACK once, then process each read individually so a
        # retransmitted batch only delivers the reads not yet seen.
        if isinstance(parsed, LoRaBatch):
            if self._transport:
                self._transport.send(format_ack(parsed.reader_id, parsed.seq, "OK"))
            for message in parsed.reads:
                self._accept_read(message)
            return

        # Single READ messages (LoRaMessage)
        message = parsed

        if message.seq is not None:
            # ACK delivery so the remote stops retransmitting.
            if self._transport:
                self._transport.send(format_ack(message.reader_id, message.seq, "OK"))
        self._accept_read(message, dedup=message.seq is not None)

    def _accept_read(self, message: LoRaMessage, dedup: bool = True):
        """Dedup, journal, and dispatch one read."""
        if dedup and self._is_duplicate(message):
            with self._stats_lock:
                self._stats["duplicates"] += 1
            logger.debug(
                f"Duplicate read from {message.reader_id}: bib {message.bib} - dropped"
            )
            self._journal("DUP", message)
            return

        with self._stats_lock:
            self._stats["received"] += 1

        logger.info(
            f"LoRa received: Bib {message.bib} at "
            f"{message.timestamp.strftime('%H:%M:%S.%f')[:-3]} "
            f"from {message.reader_id}"
        )

        self._journal("RECV", message)

        if self.on_message:
            self.on_message(message)

    def _journal(self, event: str, message: LoRaMessage):
        if not self._journal_path:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
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

    def _is_duplicate(self, message: LoRaMessage) -> bool:
        """Check and record a read by content, pruning expired entries."""
        now = time.monotonic()
        with self._stats_lock:
            for key, seen_at in list(self._seen_reads.items()):
                if now - seen_at > self.READ_DEDUP_TTL_SEC:
                    del self._seen_reads[key]
            key = (message.reader_id, message.bib, message.timestamp)
            if key in self._seen_reads:
                return True
            self._seen_reads[key] = now
            return False

    def start(self) -> bool:
        """Start the LoRa receiver.

        Returns:
            True if started successfully.
        """
        if self._running:
            return True

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
