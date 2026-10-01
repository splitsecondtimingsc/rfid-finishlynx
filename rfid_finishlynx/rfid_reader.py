"""RFID Reader interface using LLRP protocol for Impinj Speedway R420."""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sllurp.llrp import LLRPReaderConfig, LLRPReaderClient, LLRPReaderState

from .config import ReaderConfig, TimingConfig

logger = logging.getLogger(__name__)


@dataclass
class TagRead:
    """Represents a single RFID tag read."""
    bib: str
    timestamp: datetime
    epc: str
    rssi: int
    antenna: int
    reader_id: str = "LOCAL"


class RFIDReader:
    """Interface for Impinj RFID readers using LLRP protocol."""

    def __init__(
        self,
        reader_config: ReaderConfig,
        timing_config: TimingConfig,
        on_tag_read: Optional[Callable[[TagRead], None]] = None,
        reader_id: str = "LOCAL",
    ):
        """Initialize the RFID reader.

        Args:
            reader_config: Reader connection configuration.
            timing_config: Timing behavior configuration.
            on_tag_read: Callback function for tag reads.
            reader_id: Identifier for this reader (for multi-reader setups).
        """
        self.reader_config = reader_config
        self.timing_config = timing_config
        self.on_tag_read = on_tag_read
        self.reader_id = reader_id

        self._client: Optional[LLRPReaderClient] = None
        self._running = False
        self._last_reads: Dict[str, float] = {}  # bib -> timestamp for dedup
        self._warned_epcs: set = set()  # EPCs already warned about (avoid spam)
        self._lock = threading.Lock()

    def _extract_bib_from_epc(self, epc_hex: str) -> Optional[str]:
        """Extract bib number from tag EPC.

        Our tags carry the bib as a literal decimal number in the hex: bib 35
        is EPC ...00000035. That is the first and normal path. Two fallbacks
        remain for a hex value with A-F in it (read as an integer) and for a
        digit run anywhere in the EPC.

        There is deliberately NO ASCII decoding here any more. It used to run
        first and, after stripping NUL padding, mistook every 0x30-0x39 byte
        for the character "0"-"9": EPC 00000035 became "5", so bibs 30-39
        (and 3030-3939) had been reported as 0-9 since the first meet
        (found 2026-09-19). No chip we use is written as ASCII text.

        Args:
            epc_hex: Hexadecimal EPC string.

        Returns:
            Bib number as string, or None if unable to extract.
        """
        if not epc_hex:
            return None

        # Hex digits that are all 0-9 read as the literal decimal bib:
        # a tag written "00000016" means bib 16, not 0x16 = 22
        bib_digits = epc_hex[-8:]
        if bib_digits.isdigit():
            result = bib_digits.lstrip("0") or "0"
            if len(result) <= 5:
                logger.debug(f"Bib extraction method=DECIMAL_last8, EPC={epc_hex}, digits={bib_digits}, bib={result}")
                return result

        # Try direct hex-to-int for last 4 bytes
        try:
            bib_hex = epc_hex[-8:]
            bib_int = int(bib_hex, 16)
            if 0 < bib_int < 100000:  # Reasonable bib range
                logger.debug(f"Bib extraction method=HEX_INT_last8, EPC={epc_hex}, hex_slice={bib_hex}, int={bib_int}")
                return str(bib_int)
        except ValueError:
            pass

        # Try finding numeric portion in EPC
        match = re.search(r"(\d+)", epc_hex)
        if match:
            bib = match.group(1).lstrip("0") or "0"
            if len(bib) <= 5:  # Reasonable bib length
                logger.debug(f"Bib extraction method=REGEX, EPC={epc_hex}, match={match.group(1)}, bib={bib}")
                return bib

        if epc_hex not in self._warned_epcs:
            self._warned_epcs.add(epc_hex)
            logger.warning(f"Could not extract bib from EPC: {epc_hex} (last8={epc_hex[-8:]}, full_len={len(epc_hex)})")
        return None

    def _should_report(self, bib: str) -> bool:
        """Check if a tag read should be reported based on deduplication rules.

        Deduplication prevents the same tag from being reported multiple times
        within the dedup_window_sec (typically 10-15 seconds). This handles the
        case where an RFID tag is read many times per second as it passes through
        the reader's RF field.

        After the dedup window expires, subsequent reads ARE reported (for lap
        timing, multiple finishes, etc.).

        Args:
            bib: The bib number to check.

        Returns:
            True if the read should be reported.
        """
        current_time = time.time()

        with self._lock:
            last_read_time = self._last_reads.get(bib)

            if last_read_time is None:
                # First read of this bib
                self._last_reads[bib] = current_time
                return True

            elapsed = current_time - last_read_time

            # Report if enough time has passed since last read
            if elapsed >= self.timing_config.dedup_window_sec:
                self._last_reads[bib] = current_time
                return True

            # Within dedup window - suppress duplicate read
            return False

    def _on_llrp_tag_report(self, reader, tags):
        """Handle tag reports from LLRP reader.

        Args:
            reader: The LLRP reader client.
            tags: List of tag reports.
        """
        for tag in tags:
            try:
                epc_raw = tag.get("EPC-96", tag.get("EPC", b""))
                epc = epc_raw.hex()
                if not epc:
                    continue

                logger.debug(f"Raw tag report: EPC={epc} (len={len(epc)} hex chars, {len(epc_raw)} bytes), keys={list(tag.keys())}")

                if "FirstSeenTimestampUTC" in tag:
                    ts_us = tag["FirstSeenTimestampUTC"]
                    reader_time = datetime.fromtimestamp(ts_us / 1_000_000)
                    logger.debug(f"Reader timestamp: {reader_time.strftime('%H:%M:%S.%f')[:-3]} (using local time for FinishLynx)")

                self._handle_epc(epc, tag.get("PeakRSSI", 0), tag.get("AntennaID", 0))

            except Exception as e:
                logger.error(f"Error processing tag: {e}")

    def _handle_epc(self, epc: str, rssi: int, antenna: int):
        """Dedup an EPC sighting and emit a TagRead (shared by all reader types)."""
        bib = self._extract_bib_from_epc(epc)
        if bib is None:
            return

        if not self._should_report(bib):
            logger.debug(f"Deduplicated read for bib {bib}")
            return

        # Use local time for the split timestamp sent to FinishLynx.
        # This must match the T (time sync) packets which also use local time.
        # The reader's own clock may be in a different timezone or
        # unsynchronized, which would cause FinishLynx to discard the split.
        timestamp = datetime.now()

        tag_read = TagRead(
            bib=bib,
            timestamp=timestamp,
            epc=epc,
            rssi=rssi,
            antenna=antenna,
            reader_id=self.reader_id,
        )

        logger.info(f"Tag read: EPC={epc} -> Bib={bib}, time={timestamp.strftime('%H:%M:%S.%f')[:-3]}, RSSI={rssi}, ant={antenna}")

        if self.on_tag_read:
            self.on_tag_read(tag_read)

    def _detect_antenna_count(self) -> Optional[int]:
        """Detect the number of antenna ports on the reader.

        Queries the reader via SSH to determine the model and antenna count.
        Returns 2 for R220/R120, 4 for R420/R700, or None when the reader
        could not be identified (no sshpass on this box, SSH refused, unknown
        model) - in which case the configured antenna list is trusted as-is
        rather than silently trimmed to a guessed port count.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["sshpass", "-p", "impinj", "ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=3", f"root@{self.reader_config.host}",
                 "show system summary"],
                capture_output=True, text=True, timeout=5
            )

            output = result.stdout.lower()
            if "r220" in output or "r120" in output:
                logger.info("Detected Impinj R220/R120 (2 antenna ports)")
                return 2
            elif "r420" in output or "r700" in output:
                logger.info("Detected Impinj R420/R700 (4 antenna ports)")
                return 4
            else:
                # Check hostname for model hint
                if "r220" in self.reader_config.host.lower():
                    return 2
                logger.info("Could not detect reader model; using configured antennas")
                return None

        except FileNotFoundError:
            logger.debug("sshpass not available; using configured antennas")
            return None
        except Exception as e:
            logger.debug(f"Antenna detection failed: {e}; using configured antennas")
            return None

    def _check_reader_reachable(self, host: str, port: int = 5084, timeout: float = 2.0) -> bool:
        """Check if the reader is reachable at the given host."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def connect(self) -> bool:
        """Connect to the RFID reader.

        If the configured host is not reachable, attempts auto-configuration
        via mDNS discovery and SSH.

        Returns:
            True if connection successful.
        """
        try:
            # Check if configured reader is reachable
            if not self._check_reader_reachable(self.reader_config.host):
                logger.warning(f"Reader not reachable at {self.reader_config.host}, attempting auto-configure...")

                # Try to auto-configure a reader
                try:
                    from .app import auto_configure_reader
                    result = auto_configure_reader()
                    if result:
                        logger.info(f"Auto-configured reader at {result['host']}")
                        self.reader_config.host = result['host']
                    else:
                        logger.error("Auto-configure failed - no reader found via mDNS")
                        return False
                except ImportError:
                    logger.error("Could not import auto_configure_reader")
                    return False
                except Exception as e:
                    logger.error(f"Auto-configure error: {e}")
                    return False

            # Auto-detect antenna count based on reader model
            max_antennas = self._detect_antenna_count()
            antennas = usable_antennas(self.reader_config.antennas, max_antennas)
            logger.info(f"Using antennas: {antennas} ("
                        + (f"reader supports {max_antennas} ports" if max_antennas
                           else "port count unknown, as configured") + ")")

            config = LLRPReaderConfig()

            # Set antenna configuration (limited to what reader supports)
            config.antennas = antennas

            # Set transmit power per antenna in dBm (sllurp 2.x maps to reader's power table)
            # Both tx_power and tx_power_dbm must be set due to sllurp internal lookup
            config.tx_power = {ant: 0 for ant in antennas}
            config.tx_power_dbm = {ant: float(self.reader_config.power_dbm) for ant in antennas}

            # Report each tag immediately (default None = only on ROSpec end)
            config.report_every_n_tags = 1

            # Enable tag reports
            config.tag_content_selector = {
                "EnableROSpecID": False,
                "EnableSpecIndex": False,
                "EnableInventoryParameterSpecID": False,
                "EnableAntennaID": True,
                "EnableChannelIndex": False,
                "EnablePeakRSSI": True,
                "EnableFirstSeenTimestamp": True,
                "EnableLastSeenTimestamp": False,
                "EnableTagSeenCount": False,
                "EnableAccessSpecID": False,
            }

            # sllurp 2.x API: host and port are passed to constructor, not config
            self._client = LLRPReaderClient(
                host=self.reader_config.host,
                port=self.reader_config.port,
                config=config,
            )
            self._client.add_tag_report_callback(self._on_llrp_tag_report)

            logger.info(f"Attempting to connect to reader at {self.reader_config.host}:{self.reader_config.port}")
            try:
                self._client.connect()
                logger.info("connect() method returned. Checking state...")
            except Exception as e:
                logger.error("Exception during connect()", exc_info=True)
                return False

            # Wait for connection
            timeout = 10
            while timeout > 0 and self._client.llrp.state != LLRPReaderState.STATE_INVENTORYING:
                logger.debug(f"Waiting for inventorying state. Current state: {self._client.llrp.state}. Timeout: {timeout:.1f}s")
                time.sleep(0.1)
                timeout -= 0.1

            if self._client.llrp.state == LLRPReaderState.STATE_INVENTORYING:
                logger.info("Successfully connected and inventorying")
                self._running = True
                return True
            else:
                logger.error(f"Failed to start inventorying. Final state: {self._client.llrp.state}")
                self.disconnect()
                return False

        except Exception as e:
            logger.error(f"Failed to initialize LLRP client: {e}", exc_info=True)
            return False

    def disconnect(self):
        """Disconnect from the RFID reader."""
        self._running = False
        if self._client:
            try:
                self._client.disconnect()
                logger.info("Disconnected from reader")
            except Exception as e:
                logger.error(f"Error disconnecting: {e}")
            self._client = None

    def reset_dedup(self, bib: Optional[str] = None):
        """Reset deduplication state.

        Args:
            bib: Specific bib to reset, or None to reset all.
        """
        with self._lock:
            if bib is None:
                self._last_reads.clear()
                logger.info("Reset all deduplication state")
            else:
                self._last_reads.pop(bib, None)
                logger.info(f"Reset deduplication for bib {bib}")

    @property
    def is_connected(self) -> bool:
        """Check if reader is connected and inventorying."""
        client = self._client
        try:
            return (
                client is not None
                and client.llrp.state == LLRPReaderState.STATE_INVENTORYING
            )
        except AttributeError:
            # client.llrp can vanish mid-check during disconnect
            return False


def default_port_for(reader_type: str) -> int:
    """Native port for a reader type: LLRP 5084 (impinj) / Modbus TCP 502 (vanch)."""
    return 502 if reader_type == "vanch" else 5084


def usable_antennas(configured: List[int], port_count: Optional[int]) -> List[int]:
    """The antenna ports to enable on an Impinj reader.

    When the reader's port count is known, configured ports beyond it are
    dropped (and if none survive, every port is used). When it is unknown
    the configured list is trusted as written: a 4-antenna mat on a station
    that cannot interrogate its reader must not quietly become a 2-antenna
    one. An empty list means every port when the count is known, else the
    two ports every Impinj model has (asking an R220 for ports it lacks
    would fail the whole connection).
    """
    ports = []
    for a in configured or []:
        a = int(a)
        if a >= 1 and a not in ports:
            ports.append(a)
    if port_count:
        ports = [a for a in ports if a <= port_count]
        if not ports:
            ports = list(range(1, port_count + 1))
    return ports or [1, 2]


def create_reader(reader_config: ReaderConfig, timing_config: TimingConfig,
                  on_tag_read: Optional[Callable[[TagRead], None]] = None,
                  reader_id: str = "LOCAL") -> RFIDReader:
    """Build the reader for the configured type (reader.type: impinj | vanch)."""
    if getattr(reader_config, "type", "impinj") == "vanch":
        from .vanch_reader import VanchReader
        return VanchReader(reader_config, timing_config, on_tag_read=on_tag_read, reader_id=reader_id)
    return RFIDReader(reader_config, timing_config, on_tag_read=on_tag_read, reader_id=reader_id)
