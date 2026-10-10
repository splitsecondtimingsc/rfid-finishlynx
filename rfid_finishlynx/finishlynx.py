"""FinishLynx LapTime protocol implementation and Scoreboard listener."""

import logging
import re
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .config import FinishLynxConfig

logger = logging.getLogger(__name__)


class ScoreboardListener:
    """UDP listener to receive scoreboard data from FinishLynx.

    FinishLynx sends scoreboard data via UDP broadcast (using "Network UDP" mode).
    Data format is pipe-delimited with semicolon line separators.

    Expected format from FinishLynx (varies based on LSS script):
    - Lines separated by semicolons (;)
    - Fields separated by pipes (|)
    - Event line: status|event_name|wind|event_number|round_number|heat_number|event_round_heat|start_type
    - Result lines: place|lane|id|name|team|time|...
    - *COMPLETE marker indicates end of transmission
    - Standard UDP packet size is 536 bytes
    - Packets of exactly 536 bytes without *COMPLETE are partial - accumulate them
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5891):
        """
        Initialize the Scoreboard listener (UDP).

        Args:
            host: The IP address to listen on (0.0.0.0 for all interfaces).
            port: The UDP port to listen on.
        """
        self.host = host if host else "0.0.0.0"
        self.port = port
        self._socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._event_change_lock = threading.Lock()

        # Parsed data
        self._event = 0
        self._heat = 0
        self._round = 0
        self._race_name = ""
        self._status = ""
        self._connected = False
        self._last_packet_time: Optional[datetime] = None
        self._packet_count = 0

        # Callbacks
        self._on_event_change_callback: Optional[Callable[[int, int, int, int], None]] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None

    @property
    def event(self) -> int:
        with self._lock:
            return self._event

    @property
    def heat(self) -> int:
        with self._lock:
            return self._heat

    @property
    def round(self) -> int:
        with self._lock:
            return self._round

    @property
    def race_name(self) -> str:
        with self._lock:
            return self._race_name

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def tdp_connected(self) -> bool:
        """Check if FinishLynx scoreboard is connected (compatibility alias)."""
        with self._lock:
            return self._connected

    @property
    def is_connected(self) -> bool:
        """Check if FinishLynx scoreboard is connected."""
        with self._lock:
            return self._connected

    def set_on_event_change_callback(self, callback: Callable[[int, int, int, int], None]):
        """Set callback for event/heat changes.

        Callback signature: callback(old_event, old_heat, new_event, new_heat)
        Called when FinishLynx changes to a different event or heat.
        """
        self._on_event_change_callback = callback

    def set_on_running_time_callback(self, callback: Callable[[float], None]):
        """Set callback for running-time packets.

        Callback signature: callback(seconds) - the race clock reading;
        gun time can be derived as now - seconds.
        """
        self._on_running_time_callback = callback

    def set_on_tdp_disconnect_callback(self, callback: Callable[[], None]):
        """Set callback for disconnection (compatibility alias)."""
        self._on_disconnect_callback = callback

    def set_on_disconnect_callback(self, callback: Callable[[], None]):
        """Set callback for disconnection."""
        self._on_disconnect_callback = callback

    def start(self):
        """Start the scoreboard UDP listener in a background thread."""
        if self._running:
            logger.info("Scoreboard listener is already running.")
            return

        logger.info(f"Starting Scoreboard UDP listener on {self.host}:{self.port}")
        self._running = True
        self._listener_thread = threading.Thread(target=self._udp_listener_loop, daemon=True)
        self._listener_thread.start()

    def stop(self):
        """Stop the scoreboard listener."""
        self._running = False

        # Close UDP socket
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None

        if self._listener_thread:
            self._listener_thread.join(timeout=2.0)

        with self._lock:
            self._connected = False

        logger.info("Scoreboard listener stopped.")

    def _udp_listener_loop(self):
        """Main UDP listener loop - receives datagrams from FinishLynx."""
        buffer = ""
        UDP_PACKET_SIZE = 536  # Standard FinishLynx UDP packet size

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.host, self.port))
            self._socket.settimeout(1.0)  # Allow checking _running periodically

            logger.info(f"Scoreboard: UDP listener bound to {self.host}:{self.port} - waiting for FinishLynx packets...")

            while self._running:
                try:
                    data, addr = self._socket.recvfrom(4096)

                    # Update connection status on first packet or if we haven't received in a while
                    with self._lock:
                        if not self._connected:
                            logger.info(f"Scoreboard: Receiving UDP packets from {addr}")
                            self._connected = True
                        self._last_packet_time = datetime.now()
                        self._packet_count += 1

                    # Decode as latin-1 (single-byte encoding per FinishLynx spec)
                    text = data.decode('latin-1')
                    buffer += text

                    # Log raw data received (first few packets in detail, then summary)
                    if self._packet_count <= 5:
                        logger.info(f"Scoreboard RX #{self._packet_count}: {len(data)} bytes from {addr}: {repr(text[:200])}")
                    elif self._packet_count % 100 == 0:
                        logger.debug(f"Scoreboard RX: packet #{self._packet_count}, {len(data)} bytes")

                    # Check for complete message
                    # *COMPLETE marker indicates end of transmission
                    if "*COMPLETE" in buffer:
                        message = buffer.replace(";*COMPLETE", "").replace("*COMPLETE", "")
                        self._parse_scoreboard_data(message)
                        buffer = ""
                    # If packet is less than UDP_PACKET_SIZE, it's the last packet of a message
                    elif len(data) < UDP_PACKET_SIZE and buffer:
                        self._parse_scoreboard_data(buffer)
                        buffer = ""
                    # If we have a lot of data without *COMPLETE, try to parse it
                    elif len(buffer) > 1000:
                        self._parse_scoreboard_data(buffer)
                        buffer = ""

                except socket.timeout:
                    # Socket timeout is normal - just allows checking _running periodically
                    continue

                except OSError as e:
                    if self._running:
                        logger.error(f"Scoreboard: UDP error: {e}")
                    break

        except Exception as e:
            logger.error(f"Scoreboard: Failed to start UDP listener: {e}", exc_info=True)
        finally:
            if self._socket:
                try:
                    self._socket.close()
                except:
                    pass
                self._socket = None


    def _parse_scoreboard_data(self, data: str):
        """Parse scoreboard data from FinishLynx.

        Supports multiple LSS script formats:
        1. Example/Example2 format: Newline-separated with "Key : Value" pairs
           *** Start List Header ***
           Event name         : Men's 800 Meters
           Event number       : 3
           Heat number        : 1
           *** Start List Trailer ***

        2. Pipe-delimited format: Fields separated by |, lines by ;
           status|event_name|wind|event_number|round_number|heat_number

        3. Running time packets - may be sent separately
           Running Time: 1:23.456
        """
        # FinishLynx sends its running time ~20 times a second: that alone
        # was 99% of the log (70k lines an hour, 2026-10-01). Log one of
        # those a minute; every other packet in full.
        if "running time" in data[:40].lower():
            now = time.time()
            if now - getattr(self, "_rt_logged", 0.0) >= 60.0:
                self._rt_logged = now
                logger.debug(f"Scoreboard: running time (1 of these logged a minute): {repr(data[:120])}")
        else:
            logger.debug(f"Scoreboard: Raw data: {repr(data[:500])}")

        with self._lock:
            old_event = self._event
            old_heat = self._heat
        changed = False

        # Running-time packets: derive-a-gun material for the race panes
        m = re.search(r"Running\s*Time\s*:?\s*([0-9]+(?::[0-9]{2})*(?:\.[0-9]+)?)",
                      data, re.IGNORECASE)
        if m and getattr(self, "_on_running_time_callback", None):
            parts = m.group(1).split(":")
            try:
                seconds = float(parts[-1])
                if len(parts) > 1:
                    seconds += int(parts[-2]) * 60
                if len(parts) > 2:
                    seconds += int(parts[-3]) * 3600
                self._on_running_time_callback(seconds)
            except (ValueError, TypeError):
                pass

        # Detect format and parse accordingly
        # Handle both "*** Start List Header ***" and "*** StartListHeader ***" formats
        if "StartListHeader" in data or "Start List Header" in data or ": " in data:
            # Example/Example2 format - newline separated with colon key-value pairs
            changed = self._parse_example_format(data)
        elif "|" in data and ";" in data:
            # Pipe-delimited format
            changed = self._parse_pipe_format(data)
        else:
            # Try to extract any useful info
            changed = self._parse_generic_format(data)

        # Trigger callback outside lock if event/heat changed
        if changed and self._on_event_change_callback:
            with self._lock:
                new_event = self._event
                new_heat = self._heat
            threading.Thread(
                target=self._run_event_change_callback,
                args=(old_event, old_heat, new_event, new_heat),
                daemon=True
            ).start()

    def _run_event_change_callback(self, old_event: int, old_heat: int,
                                    new_event: int, new_heat: int):
        """Run the event-change callback, serialized so rapid successive
        changes cannot drain/clear race reads concurrently."""
        with self._event_change_lock:
            try:
                self._on_event_change_callback(old_event, old_heat, new_event, new_heat)
            except Exception as e:
                logger.error(f"Scoreboard: event-change callback failed: {e}", exc_info=True)

    def _parse_example_format(self, data: str) -> bool:
        """Parse Example/Example2 LSS script format.

        Format:
        *** Start List Header ***
        OFFICIAL/UNOFFICIAL: UNOFFICIAL
        Event name         : Men's 800 Meters
        Wind               : nwi
        Event number       : 3
        Round number       : 1
        Heat number        : 1
        AUTO/MANUAL start  : MANUAL
        Number of results  : 2
        Plc Ln  Id  Name       Affil      ...
        --- --- --- ---------- ---------- ...
            1   108 Barnett    Byrnes     ...
        *** Start List Trailer ***
        """
        # Split by newlines (handle both \r\n and \n)
        lines = [line.strip() for line in data.replace('\r\n', '\n').split('\n') if line.strip()]

        event_num = 0
        heat_num = 0
        round_num = 0
        event_name = ""

        for line in lines:
            # Look for key-value pairs with colon separator
            if ':' in line and not line.startswith('***'):
                # Split on first colon only
                parts = line.split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip().lower()
                    value = parts[1].strip()

                    if 'event number' in key:
                        try:
                            event_num = int(value)
                        except ValueError:
                            pass
                    elif 'heat number' in key:
                        try:
                            heat_num = int(value)
                        except ValueError:
                            pass
                    elif 'round number' in key:
                        try:
                            round_num = int(value)
                        except ValueError:
                            pass
                    elif 'event name' in key:
                        event_name = value

        changed = False
        with self._lock:
            # Only process event change if we actually found event info in this packet
            # (Running time packets don't have event info - don't let them reset the event)
            if event_num > 0:
                if event_num != self._event or heat_num != self._heat:
                    logger.info(f"Scoreboard: Event/Heat change detected: "
                              f"E{self._event}H{self._heat} -> E{event_num}H{heat_num}, "
                              f"Name: '{event_name}'")
                    self._event = event_num
                    self._heat = heat_num
                    self._round = round_num
                    self._race_name = event_name
                    changed = True
                else:
                    # Same event/heat - log periodically for debugging
                    logger.debug(f"Scoreboard: Same event E{event_num}H{heat_num} ('{event_name}')")
                    # Update race name even if event/heat didn't change
                    if event_name and event_name != self._race_name:
                        self._race_name = event_name
                        logger.debug(f"Scoreboard: Updated race name to '{event_name}'")

        return changed

    def _parse_pipe_format(self, data: str) -> bool:
        """Parse pipe-delimited scoreboard format."""
        lines = [line.strip() for line in data.split(";") if line.strip()]
        if not lines:
            return False

        first_line = lines[0]
        fields = first_line.split("|")

        changed = False
        with self._lock:
            if len(fields) >= 6:
                try:
                    event_num = self._extract_number_from_field(fields[3]) or self._extract_number_from_field(fields[0])
                    heat_num = self._extract_number_from_field(fields[5]) or self._extract_number_from_field(fields[2])
                    round_num = self._extract_number_from_field(fields[4])
                    event_name = fields[1] if len(fields) > 1 and not fields[1].isdigit() else ""

                    # Only update if we found valid event info
                    if event_num > 0 and (event_num != self._event or heat_num != self._heat):
                        logger.info(f"Scoreboard: Event/Heat change: E{self._event}H{self._heat} -> E{event_num}H{heat_num}")
                        self._event = event_num
                        self._heat = heat_num
                        self._round = round_num
                        self._race_name = event_name
                        changed = True
                except (ValueError, IndexError) as e:
                    logger.debug(f"Scoreboard: Error parsing pipe format: {e}")

        return changed

    def _parse_generic_format(self, data: str) -> bool:
        """Try to extract event/heat info from any format using regex."""
        changed = False

        # Try to find event/heat numbers using regex patterns
        event_match = re.search(r'event\s*(?:number|#|num)?\s*[:\s]\s*(\d+)', data, re.IGNORECASE)
        heat_match = re.search(r'heat\s*(?:number|#|num)?\s*[:\s]\s*(\d+)', data, re.IGNORECASE)

        if event_match or heat_match:
            event_num = int(event_match.group(1)) if event_match else 0
            heat_num = int(heat_match.group(1)) if heat_match else 0

            with self._lock:
                # Only update if we found valid event info
                if event_num > 0 and (event_num != self._event or heat_num != self._heat):
                    logger.info(f"Scoreboard: Event/Heat change (generic): E{self._event}H{self._heat} -> E{event_num}H{heat_num}")
                    self._event = event_num
                    self._heat = heat_num
                    changed = True

        return changed

    def _extract_number_from_field(self, field: str) -> int:
        """Extract a number from a field string."""
        if not field:
            return 0
        field = field.strip()
        if field.isdigit():
            return int(field)
        match = re.search(r'\d+', field)
        return int(match.group()) if match else 0


# Alias for backwards compatibility
FinishLynxListener = ScoreboardListener


# LapTime Protocol Constants
SOT_SYNC_OK = 0x01  # Start of text - sync OK
SOT_NO_SYNC = 0x02  # Start of text - no sync
EOT = b"\r\n"  # End of text - CR+LF per protocol spec

# Opcodes
OPCODE_SPLIT = "S"  # Split/lap time
OPCODE_ZERO = "Z"  # Zero/start time
OPCODE_TIME = "T"  # Time sync

class FinishLynxOutput:
    """TCP client for FinishLynx LapTime protocol."""

    def __init__(self, config: FinishLynxConfig):
        """Initialize FinishLynx output.

        Args:
            config: FinishLynx configuration.
        """
        self.config = config
        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None
        self._running = False
        self._sync_count = 0

    @staticmethod
    def format_time(dt: datetime) -> str:
        """Format datetime as LapTime protocol time string.

        Args:
            dt: Datetime to format.

        Returns:
            Time string in format HH:MM:SS.mmm
        """
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    def _build_packet(self, opcode: str, time_str: str, identifier: Optional[str] = None,
                       event: Optional[int] = None, round_num: Optional[int] = None,
                       heat: Optional[int] = None) -> bytes:
        """Build a LapTime protocol packet.

        Packet format: <SOT><opcode>,<time>[,<identifier>[,<event>,<round>,<heat>]]<EOT>

        The extended format with event/round/heat ensures splits are matched to
        the correct race in FinishLynx.

        Args:
            opcode: Single character opcode (S, Z, T).
            time_str: Time in HH:MM:SS.mmm format.
            identifier: Optional identifier (lane number for track, bib for XC).
            event: Optional event number.
            round_num: Optional round number.
            heat: Optional heat number.

        Returns:
            Complete packet as bytes.
        """
        # SOT_SYNC_OK (0x01) = "use this packet for clock sync"
        # SOT_NO_SYNC (0x02) = "this is data, don't use for sync"
        # T packets are for sync; S and Z packets are data.
        if opcode == OPCODE_TIME:
            sot = SOT_SYNC_OK
        else:
            sot = SOT_NO_SYNC

        # Build payload
        if identifier:
            payload = f"{opcode},{time_str},{identifier}"
            # Add event/round/heat if provided (required for exact match in FinishLynx)
            if event is not None and round_num is not None and heat is not None:
                payload = f"{payload},{event},{round_num},{heat}"
        else:
            payload = f"{opcode},{time_str}"

        # Assemble packet
        packet = bytes([sot]) + payload.encode("ascii") + EOT
        return packet

    def connect(self) -> bool:
        """Connect to FinishLynx LapTime plugin.

        Returns:
            True if connection successful.
        """
        with self._lock:
            if self._connected:
                return True

            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(5.0)
                self._socket.connect((self.config.host, self.config.port))
                self._socket.settimeout(None)
                # Disable Nagle's algorithm so each packet is sent immediately
                # as its own TCP segment. Without this, the OS batches our small
                # (~20 byte) packets together, and FinishLynx receives concatenated
                # data it cannot parse as individual SOT...EOT frames.
                self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._connected = True
                logger.info(f"Connected to FinishLynx at {self.config.host}:{self.config.port}")

                # Start sync thread if using internal sync
                if self.config.sync_mode == "internal":
                    self._start_sync_thread()

                return True

            except socket.error as e:
                logger.error(f"Failed to connect to FinishLynx: {e}")
                self._socket = None
                self._connected = False
                return False

    def disconnect(self):
        """Disconnect from FinishLynx."""
        self._running = False

        if self._sync_thread:
            self._sync_thread.join(timeout=2.0)
            self._sync_thread = None

        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except socket.error:
                    pass
                self._socket = None
            self._connected = False
            logger.info("Disconnected from FinishLynx")

    def _send_packet(self, packet: bytes) -> bool:
        """Send a packet to FinishLynx.

        Args:
            packet: Packet bytes to send.

        Returns:
            True if send successful.
        """
        with self._lock:
            if not self._connected or not self._socket:
                logger.debug(f"Cannot send packet: connected={self._connected}, socket={'yes' if self._socket else 'no'}")
                return False

            try:
                self._socket.sendall(packet)
                return True
            except socket.error as e:
                logger.error(f"Failed to send packet: {e}, packet_hex={packet.hex()}")
                self._connected = False
                try:
                    self._socket.close()
                except (socket.error, OSError):
                    pass
                self._socket = None
                return False

    def send_split(self, identifier: str, timestamp: datetime,
                    event: Optional[int] = None, round_num: Optional[int] = None,
                    heat: Optional[int] = None) -> bool:
        """Send a split/lap time to FinishLynx.

        Args:
            identifier: Lane number (for track) or bib number (for XC).
            timestamp: Time of the split.
            event: Optional event number for exact matching.
            round_num: Optional round number for exact matching.
            heat: Optional heat number for exact matching.

        Returns:
            True if send successful.
        """
        time_str = self.format_time(timestamp)
        packet = self._build_packet(OPCODE_SPLIT, time_str, identifier, event, round_num, heat)

        if event is not None:
            logger.info(f"Sending split to FinishLynx: id={identifier}, time={time_str}, "
                       f"event={event}, round={round_num}, heat={heat}, packet={repr(packet)}")
        else:
            logger.info(f"Sending split to FinishLynx: id={identifier}, time={time_str}, packet={repr(packet)}")

        success = self._send_packet(packet)
        if success:
            logger.info(f"Split sent successfully: id={identifier}, time={time_str}")
        else:
            logger.error(f"Failed to send split: id={identifier}, time={time_str}, connected={self._connected}")
        return success

    def send_zero(self, timestamp: Optional[datetime] = None) -> bool:
        """Send a zero/start time to FinishLynx.

        Args:
            timestamp: Start time. If None, uses current time.

        Returns:
            True if send successful.
        """
        if timestamp is None:
            timestamp = datetime.now()

        time_str = self.format_time(timestamp)
        packet = self._build_packet(OPCODE_ZERO, time_str)

        success = self._send_packet(packet)
        if success:
            logger.info(f"Sent zero/start time: {time_str}")
        return success

    def send_time_sync(self, timestamp: Optional[datetime] = None) -> bool:
        """Send a time sync packet to FinishLynx.

        Args:
            timestamp: Current time. If None, uses current time.

        Returns:
            True if send successful.
        """
        if timestamp is None:
            timestamp = datetime.now()

        time_str = self.format_time(timestamp)
        packet = self._build_packet(OPCODE_TIME, time_str)

        success = self._send_packet(packet)
        if success:
            self._sync_count += 1
            # Log first sync and then every 1000th to confirm flow without spam
            if self._sync_count == 1:
                logger.info(f"First time sync sent: {time_str}, packet_hex={packet.hex()}, packet_repr={repr(packet)}")
            elif self._sync_count % 1000 == 0:
                logger.info(f"Time sync #{self._sync_count}: {time_str}")
        return success

    def _start_sync_thread(self):
        """Start background thread for periodic time sync."""
        self._running = True
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

    def _sync_loop(self):
        """Background loop for sending periodic time sync packets."""
        logger.info("Starting internal time sync (20 Hz, ms precision)")
        while self._running:
            if self._connected:
                self.send_time_sync()
            time.sleep(0.05)  # Sync 20 times per second for tight clock alignment

    def reconnect(self) -> bool:
        """Attempt to reconnect to FinishLynx.

        Returns:
            True if reconnection successful.
        """
        self.disconnect()
        time.sleep(self.config.reconnect_delay_sec)
        return self.connect()

    @property
    def is_connected(self) -> bool:
        """Check if connected to FinishLynx."""
        return self._connected
