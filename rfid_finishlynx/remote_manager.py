"""Remote unit manager for the finish line host."""

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .config import LoRaConfig
from .remote_protocol import (
    ManagementMessage,
    MessageType,
    clock_check,
    format_cfgq,
    format_cfgs,
    format_cmd,
    format_ping,
)
from .transport import LoRaTransport

logger = logging.getLogger(__name__)


@dataclass
class RemoteUnit:
    """Represents a discovered or pre-registered remote unit."""
    reader_id: str
    state: str = "offline"  # idle, reading, error, offline
    version: str = ""
    last_seen: Optional[datetime] = None
    config_cache: Dict[str, Dict] = field(default_factory=dict)
    pre_registered: bool = False


@dataclass
class PendingCommand:
    """A command awaiting acknowledgment."""
    seq: int
    reader_id: str
    message_bytes: bytes
    sent_at: float
    retries: int = 0
    future: Future = field(default_factory=Future)
    # For multi-part CFGR responses (CFGQ "all"): the sections still expected,
    # and those collected so far. None for single-response commands.
    expect_sections: Optional[frozenset] = None
    collected: Dict[str, Dict] = field(default_factory=dict)


class RemoteManager:
    """Manages remote units from the finish line host.

    Tracks discovered and pre-registered units, sends commands,
    handles ACK/timeout/retry logic.
    """

    MAX_RETRIES = 2
    # Clock check: one PING at a time. A reply waits out both radios' ~5 s
    # send gaps and the clear-air logic, so allow well over that.
    PING_REPLY_TIMEOUT_SEC = 25.0

    def __init__(
        self,
        transport: LoRaTransport,
        config: LoRaConfig,
        on_unit_change: Optional[Callable[[Dict], None]] = None,
    ):
        self._transport = transport
        self._config = config
        self._on_unit_change = on_unit_change

        self._units: Dict[str, RemoteUnit] = {}
        self._pending: Dict[int, PendingCommand] = {}  # seq -> PendingCommand
        self._command_queues: Dict[str, List[PendingCommand]] = {}  # reader_id -> queue
        self._seq_counter = 0
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._clock_checks: Dict[str, Dict] = {}     # reader_id -> latest clock check
        self._pings: Dict[int, tuple] = {}           # seq -> (reader_id, Event, check)

        # Pre-register units from config
        for reader_id in config.pre_registered_units:
            self._units[reader_id] = RemoteUnit(
                reader_id=reader_id, pre_registered=True
            )

    def start(self):
        """Start the monitor thread for offline detection and retries."""
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._monitor_thread.start()
        logger.info("Remote manager started")

    def stop(self):
        """Stop the monitor thread."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

        # Cancel pending futures
        with self._lock:
            for _, replied, _ in self._pings.values():
                replied.set()
            for pc in self._pending.values():
                if not pc.future.done():
                    pc.future.set_result({"success": False, "message": "Manager stopped"})
            self._pending.clear()

        logger.info("Remote manager stopped")

    def get_units(self) -> List[Dict]:
        """Get all remote units with status for the web UI."""
        with self._lock:
            result = []
            for unit in self._units.values():
                result.append({
                    "reader_id": unit.reader_id,
                    "state": unit.state,
                    "version": unit.version,
                    "last_seen": unit.last_seen.isoformat() if unit.last_seen else None,
                    "pre_registered": unit.pre_registered,
                    "config_cache": unit.config_cache,
                })
            return result

    def send_command(self, reader_id: str, command: str) -> Future:
        """Send a command to a remote unit.

        Returns a Future that resolves to {"success": bool, "message": str, "data": ...}
        """
        future = Future()

        with self._lock:
            unit = self._units.get(reader_id)
            if not unit:
                future.set_result({"success": False, "message": "Unknown unit"})
                return future
            if unit.state == "offline":
                future.set_result({"success": False, "message": "Unit offline"})
                return future

        seq = self._next_seq()
        message = format_cmd(reader_id, seq, command)
        pc = PendingCommand(
            seq=seq,
            reader_id=reader_id,
            message_bytes=message,
            sent_at=time.time(),
            future=future,
        )

        with self._lock:
            self._pending[seq] = pc

        self._transport.send(message)
        logger.info(f"Sent CMD {command} to {reader_id}, seq={seq:02X}")
        return future

    def get_remote_config(self, reader_id: str, section: str = "all") -> Future:
        """Fetch config from a remote unit."""
        future = Future()

        with self._lock:
            unit = self._units.get(reader_id)
            if not unit:
                future.set_result({"success": False, "message": "Unknown unit"})
                return future
            if unit.state == "offline":
                future.set_result({"success": False, "message": "Unit offline"})
                return future

        seq = self._next_seq()
        message = format_cfgq(reader_id, seq, section)
        pc = PendingCommand(
            seq=seq,
            reader_id=reader_id,
            message_bytes=message,
            sent_at=time.time(),
            future=future,
            expect_sections=(
                frozenset({"reader", "timing", "lora", "logging"})
                if section == "all" else None
            ),
        )

        with self._lock:
            self._pending[seq] = pc

        self._transport.send(message)
        logger.info(f"Sent CFGQ section={section} to {reader_id}, seq={seq:02X}")
        return future

    def set_remote_config(self, reader_id: str, section: str, values: Dict) -> Future:
        """Set config on a remote unit."""
        future = Future()

        with self._lock:
            unit = self._units.get(reader_id)
            if not unit:
                future.set_result({"success": False, "message": "Unknown unit"})
                return future
            if unit.state == "offline":
                future.set_result({"success": False, "message": "Unit offline"})
                return future

        seq = self._next_seq()
        message = format_cfgs(reader_id, seq, section, values)
        pc = PendingCommand(
            seq=seq,
            reader_id=reader_id,
            message_bytes=message,
            sent_at=time.time(),
            future=future,
        )

        with self._lock:
            self._pending[seq] = pc

        self._transport.send(message)
        logger.info(f"Sent CFGS section={section} to {reader_id}, seq={seq:02X}")
        return future

    def start_clock_check(self, reader_id: str, count: int = 10) -> Dict:
        """Ping a station `count` times in the background (get_clock_check)."""
        with self._lock:
            unit = self._units.get(reader_id)
            if not unit:
                return {"success": False, "message": "Unknown unit"}
            if unit.state == "offline":
                return {"success": False, "message": "Unit offline"}
            current = self._clock_checks.get(reader_id)
            if current and current["running"]:
                return {"success": False, "message": "A clock check is already running"}
            check = {"running": True, "count": count, "sent": 0, "samples": [], "air": None,
                     "started": datetime.now().isoformat(timespec="seconds")}
            self._clock_checks[reader_id] = check
        threading.Thread(target=self._run_clock_check, args=(reader_id, check), daemon=True).start()
        return {"success": True}

    def get_clock_check(self, reader_id: str) -> Dict:
        """Progress and, once some replies are in, the summary:
        delay = round trip minus the station's hold; offset = the station's
        read clock minus this PC's (+ = its read times are late); air_suggested
        = the station's lora.air_latency_sec that would bring offset to 0."""
        with self._lock:
            check = self._clock_checks.get(reader_id)
            if not check:
                return {"running": False, "sent": 0, "replies": 0}
            samples = list(check["samples"])
            out = {k: check[k] for k in ("running", "count", "sent", "started", "air")}
        out.update(clock_check(samples))
        if samples and out["air"] is not None:
            out["air_suggested"] = round(max(0.0, out["air"] - out["offset"]), 2)
        return out

    def _run_clock_check(self, reader_id: str, check: Dict):
        for _ in range(check["count"]):
            if not self._running:
                break
            seq = self._next_seq()
            replied = threading.Event()
            with self._lock:
                self._pings[seq] = (reader_id, replied, check)
                check["sent"] += 1
            self._transport.send(format_ping(reader_id, seq))
            replied.wait(self.PING_REPLY_TIMEOUT_SEC)
            with self._lock:
                self._pings.pop(seq, None)
        with self._lock:
            check["running"] = False
        logger.info(f"Clock check {reader_id}: {self.get_clock_check(reader_id)}")

    def _handle_pong(self, msg: ManagementMessage):
        arrived = self._transport.clock()
        with self._lock:
            entry = self._pings.get(msg.seq)
            if not entry or entry[0] != msg.reader_id:
                return
            f = msg.fields
            entry[2]["samples"].append((f["t1"], f["t2"], f["t3"], arrived))
            entry[2]["air"] = f["air"]
        entry[1].set()

    def add_pre_registered(self, reader_id: str):
        """Add a pre-registered remote unit."""
        with self._lock:
            if reader_id not in self._units:
                self._units[reader_id] = RemoteUnit(
                    reader_id=reader_id, pre_registered=True
                )
            else:
                self._units[reader_id].pre_registered = True
        self._notify_change(reader_id)

    def remove_unit(self, reader_id: str):
        """Remove a remote unit from tracking."""
        with self._lock:
            self._units.pop(reader_id, None)

    def on_message(self, msg: ManagementMessage):
        """Handle a parsed management message from LoRa receiver."""
        if msg.msg_type == MessageType.ANNOUNCE:
            self._handle_announce(msg)
        elif msg.msg_type == MessageType.ACK:
            self._handle_ack(msg)
        elif msg.msg_type == MessageType.CFGR:
            self._handle_cfgr(msg)
        elif msg.msg_type == MessageType.PONG:
            self._handle_pong(msg)

    def _handle_announce(self, msg: ManagementMessage):
        """Process an ANNOUNCE heartbeat from a remote unit."""
        reader_id = msg.reader_id
        version = msg.fields.get("version", "")
        state = msg.fields.get("state", "idle")

        with self._lock:
            if reader_id not in self._units:
                # Auto-discover new unit
                self._units[reader_id] = RemoteUnit(reader_id=reader_id)
                logger.info(f"Discovered new remote unit: {reader_id}")

            unit = self._units[reader_id]
            was_offline = unit.state == "offline"
            unit.state = state
            unit.version = version
            unit.last_seen = datetime.now()

        if was_offline:
            logger.info(f"Remote unit {reader_id} came online (state={state})")
        self._notify_change(reader_id)

    def _handle_ack(self, msg: ManagementMessage):
        """Process an ACK response to a pending command."""
        seq = msg.seq
        status = msg.fields.get("status", "")
        data = msg.fields.get("data", "")

        with self._lock:
            pc = self._pending.pop(seq, None)

        if pc and not pc.future.done():
            result = {
                "success": status == "OK",
                "message": data if status != "OK" else "OK",
                "data": data,
            }
            pc.future.set_result(result)
            logger.info(f"Received ACK for seq={seq:02X}: {status}")

    def _handle_cfgr(self, msg: ManagementMessage):
        """Process a CFGR config response.

        A CFGQ for "all" produces one CFGR per section under the same seq;
        the pending command stays open until every expected section arrives.
        """
        seq = msg.seq
        section = msg.fields.get("section", "")
        values = msg.fields.get("values", {})
        reader_id = msg.reader_id

        done_pc = None
        with self._lock:
            # Cache the config
            unit = self._units.get(reader_id)
            if unit:
                unit.config_cache[section] = values

            pc = self._pending.get(seq)
            if pc:
                if pc.expect_sections:
                    pc.collected[section] = values
                    # Keep the command alive while the multi-part response streams in
                    pc.sent_at = time.time()
                    if set(pc.collected) >= pc.expect_sections:
                        done_pc = self._pending.pop(seq)
                else:
                    done_pc = self._pending.pop(seq)

        if done_pc and not done_pc.future.done():
            if done_pc.expect_sections:
                done_pc.future.set_result({
                    "success": True,
                    "sections": done_pc.collected,
                })
            else:
                done_pc.future.set_result({
                    "success": True,
                    "section": section,
                    "values": values,
                })
            logger.info(f"Received CFGR from {reader_id} section={section}")

    def _monitor_loop(self):
        """Background loop for offline detection and command retries."""
        while self._running:
            now = time.time()

            with self._lock:
                # Check for offline units
                for unit in self._units.values():
                    if unit.state != "offline" and unit.last_seen:
                        elapsed = (datetime.now() - unit.last_seen).total_seconds()
                        if elapsed > self._config.offline_timeout_sec:
                            unit.state = "offline"
                            logger.warning(f"Remote unit {unit.reader_id} went offline")
                            self._notify_change_unlocked(unit.reader_id)

                # Check for timed-out commands
                timed_out = []
                for seq, pc in list(self._pending.items()):
                    elapsed = now - pc.sent_at
                    if elapsed > self._config.command_timeout_sec:
                        if pc.retries < self.MAX_RETRIES:
                            # Retry
                            pc.retries += 1
                            pc.sent_at = now
                            self._transport.send(pc.message_bytes)
                            logger.debug(f"Retrying seq={seq:02X}, attempt {pc.retries + 1}")
                        else:
                            timed_out.append(seq)

                for seq in timed_out:
                    pc = self._pending.pop(seq)
                    if not pc.future.done():
                        pc.future.set_result({
                            "success": False,
                            "message": "No response from unit",
                        })
                    logger.warning(f"Command seq={seq:02X} to {pc.reader_id} timed out")

            time.sleep(1.0)

    def _next_seq(self) -> int:
        """Get next sequence number (0-255, wrapping)."""
        with self._lock:
            seq = self._seq_counter
            self._seq_counter = (self._seq_counter + 1) % 256
            return seq

    def _notify_change(self, reader_id: str):
        """Notify listeners of a unit state change."""
        if not self._on_unit_change:
            return
        with self._lock:
            unit = self._units.get(reader_id)
            if not unit:
                return
            data = {
                "reader_id": unit.reader_id,
                "state": unit.state,
                "version": unit.version,
                "last_seen": unit.last_seen.isoformat() if unit.last_seen else None,
                "pre_registered": unit.pre_registered,
            }
        self._on_unit_change(data)

    def _notify_change_unlocked(self, reader_id: str):
        """Notify without acquiring lock (caller must hold lock)."""
        if self._on_unit_change:
            unit = self._units.get(reader_id)
            if unit:
                data = {
                    "reader_id": unit.reader_id,
                    "state": unit.state,
                    "version": unit.version,
                    "last_seen": unit.last_seen.isoformat() if unit.last_seen else None,
                    "pre_registered": unit.pre_registered,
                }
                # Schedule callback outside lock
                threading.Thread(
                    target=self._on_unit_change, args=(data,), daemon=True
                ).start()
