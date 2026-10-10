"""Remote agent for responding to management commands from the finish line host."""

import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .remote_protocol import (
    ManagementMessage,
    MessageType,
    format_ack,
    format_announce,
    format_pong,
    parse_management_message,
)
from .transport import LoRaTransport, parse_any_lora_message

if TYPE_CHECKING:
    from .app import Application

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


class RemoteAgent:
    """Responds to management messages from the finish line host.

    Runs on remote_lap mode Pis. Handles:
    - Periodic ANNOUNCE heartbeats
    - CMD execution (START, STOP, RESET_DEDUP, RESTART, GET_STATUS)
    - CFGQ config queries
    - CFGS config set commands
    """

    # Processed-seq entries expire after this long. Must exceed the host's
    # command retry window (~9 s) but stay short enough that a restarted
    # host reusing low seq numbers isn't answered with stale cached ACKs.
    SEQ_CACHE_TTL_SEC = 30.0

    def __init__(self, app: "Application", transport: LoRaTransport):
        self._app = app
        self._transport = transport
        self._running = False
        self._announce_thread: Optional[threading.Thread] = None
        # seq -> (monotonic time processed, cached response messages)
        self._seq_cache: Dict[int, tuple] = {}
        self._lock = threading.Lock()

    def start(self):
        """Start the remote agent (announce loop + message handling)."""
        if self._running:
            return
        self._running = True
        # PING/PONG stamps in the clock this station's reads are sent in
        self._transport.clock = self._clock
        self._announce_thread = threading.Thread(
            target=self._announce_loop, daemon=True
        )
        self._announce_thread.start()
        logger.info("Remote agent started")

    def stop(self):
        """Stop the remote agent."""
        self._running = False
        if self._announce_thread:
            self._announce_thread.join(timeout=2.0)
            self._announce_thread = None
        logger.info("Remote agent stopped")

    def on_receive(self, data: bytes):
        """Handle incoming data from LoRa transport.

        Dispatches READ messages (ignored on remote side) and management
        messages to appropriate handlers.
        """
        arrived = self._clock()
        msg = parse_any_lora_message(data)
        if msg is None:
            return

        # Only handle management messages addressed to us
        if isinstance(msg, ManagementMessage):
            if msg.msg_type == MessageType.GUN:
                # Broadcast to every station: drives the local LED clocks,
                # mirrors the finish console's 12/24-hour display and carries
                # the finish line's clock so this box can line up with it.
                self._app.set_remote_gun(msg.fields.get("epoch", 0.0),
                                         hour_format=msg.fields.get("hour_format"),
                                         sent_at=msg.fields.get("sent_at"))
                return
            if msg.reader_id != self._app.config.lora.reader_id:
                return  # Not for us
            if msg.msg_type == MessageType.PING:
                # Clock check: t3 is stamped as the reply leaves (transport)
                self._transport.send(format_pong(
                    msg.reader_id, msg.seq, msg.fields["t1"], arrived,
                    air=float(getattr(self._app.config.lora, "air_latency_sec", 0.0) or 0.0)))
                return
            if msg.msg_type == MessageType.ACK:
                # Host acknowledging one of our READ transmissions
                sender = getattr(self._app, "read_sender", None)
                if sender and msg.seq is not None:
                    sender.handle_ack(msg.seq)
                return
            self._handle_management(msg)

    def _clock(self) -> float:
        """This station's corrected clock: the one its reads are sent in."""
        return time.time() - getattr(self._app, "remote_clock_offset", 0.0)

    def _announce_loop(self):
        """Periodically send ANNOUNCE heartbeats."""
        interval = self._app.config.lora.announce_interval_sec
        while self._running:
            state = self._get_state()
            message = format_announce(
                self._app.config.lora.reader_id, VERSION, state
            )
            self._transport.send(message)
            logger.debug(f"Sent ANNOUNCE: state={state}")

            # Sleep in small increments for responsive shutdown
            elapsed = 0.0
            while elapsed < interval and self._running:
                time.sleep(0.5)
                elapsed += 0.5

    def _handle_management(self, msg: ManagementMessage):
        """Route management messages to handlers."""
        if msg.msg_type == MessageType.CMD:
            self._handle_cmd(msg)
        elif msg.msg_type == MessageType.CFGQ:
            self._handle_cfgq(msg)
        elif msg.msg_type == MessageType.CFGS:
            self._handle_cfgs(msg)

    def _replay_if_duplicate(self, seq: int) -> bool:
        """Idempotency check with TTL expiry.

        If the seq was processed recently, re-send the cached response(s) and
        return True. Otherwise reserve the seq and return False.
        """
        now = time.monotonic()
        with self._lock:
            for s, (seen_at, _) in list(self._seq_cache.items()):
                if now - seen_at > self.SEQ_CACHE_TTL_SEC:
                    del self._seq_cache[s]
            entry = self._seq_cache.get(seq)
            if entry is None:
                self._seq_cache[seq] = (now, [])
                return False
            cached = list(entry[1])
        for message in cached:
            self._transport.send(message)
        return True

    def _cache_responses(self, seq: int, messages: List[bytes]):
        """Cache the response(s) for a seq for duplicate replay."""
        with self._lock:
            self._seq_cache[seq] = (time.monotonic(), list(messages))

    def _handle_cmd(self, msg: ManagementMessage):
        """Execute a command from the host."""
        seq = msg.seq
        command = msg.fields.get("command", "")

        if self._replay_if_duplicate(seq):
            return

        reader_id = self._app.config.lora.reader_id
        response = None

        if command == "START":
            success = self._app.start()
            if success:
                response = format_ack(reader_id, seq, "OK")
            else:
                response = format_ack(reader_id, seq, "ERR", "start_failed")

        elif command == "STOP":
            self._app.stop()
            response = format_ack(reader_id, seq, "OK")

        elif command == "RESET_DEDUP":
            if self._app.reader:
                self._app.reader.reset_dedup()
            response = format_ack(reader_id, seq, "OK")

        elif command == "GET_STATUS":
            status = self._app.get_status()
            state = self._get_state()
            reads = status.get("total_reads", 0)
            bibs = status.get("unique_bibs", 0)
            data = f"{state};reads={reads};bibs={bibs}"
            response = format_ack(reader_id, seq, "OK", data)

        elif command == "RESTART":
            response = format_ack(reader_id, seq, "OK")
            self._transport.send(response)
            self._cache_responses(seq, [response])
            # Restart after sending ACK
            logger.info("Restart requested by host, restarting...")
            self._app.stop()
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return  # Won't reach here

        else:
            response = format_ack(reader_id, seq, "ERR", "unknown_command")

        if response:
            self._cache_responses(seq, [response])
            self._transport.send(response)
            logger.info(f"Handled CMD {command}, seq={seq:02X}")

    def _handle_cfgq(self, msg: ManagementMessage):
        """Respond to a config query."""
        seq = msg.seq
        section = msg.fields.get("section", "")
        reader_id = self._app.config.lora.reader_id

        if self._replay_if_duplicate(seq):
            return

        from .remote_protocol import format_cfgr

        if section == "all":
            # Send each section separately; cache all for duplicate replay
            responses = []
            for sec in ("reader", "timing", "lora", "logging"):
                values = self._get_config_section(sec)
                resp = format_cfgr(reader_id, seq, sec, values)
                responses.append(resp)
                self._transport.send(resp)
                time.sleep(0.1)  # Small delay between multi-message responses
            self._cache_responses(seq, responses)
        else:
            values = self._get_config_section(section)
            if values is None:
                response = format_ack(reader_id, seq, "ERR", "unknown_section")
            else:
                response = format_cfgr(reader_id, seq, section, values)
            self._cache_responses(seq, [response])
            self._transport.send(response)

        logger.info(f"Handled CFGQ section={section}, seq={seq:02X}")

    def _handle_cfgs(self, msg: ManagementMessage):
        """Apply config changes from the host."""
        seq = msg.seq
        section = msg.fields.get("section", "")
        values = msg.fields.get("values", {})
        reader_id = self._app.config.lora.reader_id

        if self._replay_if_duplicate(seq):
            return

        success = self._set_config_section(section, values)
        if success:
            self._app.save_config()
            response = format_ack(reader_id, seq, "OK")
        else:
            response = format_ack(reader_id, seq, "ERR", "set_failed")

        self._cache_responses(seq, [response])
        self._transport.send(response)
        logger.info(f"Handled CFGS section={section}, seq={seq:02X}, success={success}")

        # Reader settings (host, power, antennas) only take effect on a
        # fresh connection. Do that now, after the ACK is on the air, in
        # the background - a reconnect can take several seconds and the
        # host would otherwise retry the command.
        if success and section == "reader":
            self._restart_thread = threading.Thread(
                target=self._restart_reader_after_config, daemon=True)
            self._restart_thread.start()

    def _restart_reader_after_config(self):
        try:
            ok = self._app.restart_reader()
            logger.info("Reader reconnected with new settings" if ok
                        else "Reader reconnect after config change failed; supervisor will keep retrying")
        except Exception:
            logger.exception("Reader reconnect after config change failed")

    def _get_state(self) -> str:
        """Get current device state string."""
        if self._app.reader and self._app.reader.is_connected:
            return "reading"
        elif self._app._running:
            return "idle"
        else:
            return "error"

    def _get_config_section(self, section: str) -> Optional[Dict]:
        """Get a config section as a flat dict."""
        config = self._app.config

        if section == "reader":
            reader = getattr(self._app, "reader", None)
            return {
                "type": config.reader.type,
                "host": config.reader.host,
                "port": config.reader.port,
                "power_dbm": config.reader.power_dbm,
                "antennas": config.reader.antennas,              # [] = every port
                "ports": getattr(reader, "port_count", None) or "",
                "active": getattr(reader, "active_antennas", None) or [],
            }
        elif section == "timing":
            return {
                "dedup_window_sec": config.timing.dedup_window_sec,
                "lap_mode": config.timing.lap_mode,
                "total_laps": config.timing.total_laps,
            }
        elif section == "lora":
            return {
                "port": config.lora.port,
                "baud": config.lora.baud,
                "reader_id": config.lora.reader_id,
                "air_latency_sec": config.lora.air_latency_sec,
            }
        elif section == "logging":
            return {
                "level": config.logging.level,
            }
        return None

    def _set_config_section(self, section: str, values: Dict[str, str]) -> bool:
        """Apply config values to a section. Returns True on success."""
        config = self._app.config

        try:
            if section == "reader":
                if "type" in values:
                    rtype = values["type"].strip().lower()
                    if rtype not in ("vanch", "impinj"):
                        return False
                    if rtype != config.reader.type:
                        from .rfid_reader import default_port_for
                        config.reader.type = rtype
                        if "port" not in values:
                            config.reader.port = default_port_for(rtype)
                if "host" in values:
                    config.reader.host = values["host"]
                if "port" in values:
                    config.reader.port = int(values["port"])
                if "power_dbm" in values:
                    config.reader.power_dbm = float(values["power_dbm"])
                if "antennas" in values:
                    from .config import parse_antennas
                    config.reader.antennas = parse_antennas(values["antennas"])   # "" / "all" = every port

            elif section == "timing":
                if "dedup_window_sec" in values:
                    config.timing.dedup_window_sec = max(12.0, float(values["dedup_window_sec"]))
                if "lap_mode" in values:
                    config.timing.lap_mode = values["lap_mode"] in ("1", "true", "True")
                if "total_laps" in values:
                    config.timing.total_laps = max(1, int(values["total_laps"]))

            elif section == "lora":
                if "port" in values:
                    config.lora.port = values["port"]
                if "baud" in values:
                    config.lora.baud = int(values["baud"])
                if "reader_id" in values:
                    config.lora.reader_id = values["reader_id"]
                if "air_latency_sec" in values:
                    air = float(values["air_latency_sec"])
                    if not 0.0 <= air <= 5.0:
                        return False
                    config.lora.air_latency_sec = air
                    # offsets measured with the old value: learn afresh
                    # from the next GUN (2026-10-04, clock check Apply)
                    self._app._remote_clock_samples = []

            elif section == "logging":
                if "level" in values:
                    config.logging.level = values["level"]
                    logging.getLogger().setLevel(values["level"])

            else:
                return False

            return True
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to set config section {section}: {e}")
            return False
