"""Vanch UHF reader (VF-747P etc.) via Modbus TCP.

Protocol per "UHF Reader Modbus Protocol Development Manual v2.21"
(docs/vanch/). Reader defaults: 192.168.0.128:502, unit id 1.
The reader is switched to automatic mode + passive reporting, then the
4-tag FIFO at register 5000 (88 words) is polled continuously.
"""

import logging
import socket
import struct
import threading
import time
from typing import List, Optional, Tuple

from .rfid_reader import RFIDReader

logger = logging.getLogger(__name__)

VANCH_DEFAULT_HOST = "192.168.0.128"
VANCH_DEFAULT_PORT = 502
UNIT_ID = 1

REG_WORKING_MODE = 8008  # [valid, mode, report_valid, report_mode]
REG_POWER = 8014  # [valid, dbm]
REG_SAVE = 8024  # 1 = persist across power-down
REG_FIFO = 5000  # 4 tags x 22 words
FIFO_WORDS = 88
WORDS_PER_TAG = 22

POLL_INTERVAL_SEC = 0.03
STALE_SEC = 3.0

# VUP (Vanch native protocol) over UDP, port 1969 - used only for the one
# setting Modbus cannot reach: which antenna ports are polled in automatic
# mode ("VUP Protocol Development Handbook V2.1.9", 15H/16H). The reader's
# UDP server is on by default and coexists with our Modbus TCP session.
VUP_PORT = 1969
VUP_HEADER = 0x40
VUP_OK = 0xF0
VUP_CMD_SET_POLL_ANTENNAS = 0x15
VUP_CMD_GET_POLL_ANTENNAS = 0x16
VUP_MAX_ANTENNAS = 8
VUP_ERR_ANTENNA_UNSUPPORTED = 0x1B   # "This antenna is not supported" (handbook error table)
# Buzzer: the reader beeps on EVERY tag read out of the box (a mat full of
# runners = a constant squeal). Param 00 = off, 01 = on; kept by the reader
# across power cycles (checked 2026-09-30, set off by hand on 9/26).
VUP_CMD_SET_BUZZER = 0x0B
VUP_CMD_GET_BUZZER = 0x0C

# Where a station's reader may be sitting: the configured address first,
# then the README's station layout and the factory default. Lets a box take
# whichever reader is plugged into it without anyone editing config.
STATION_HOST_CANDIDATES = ("192.168.0.50", "192.168.0.51", VANCH_DEFAULT_HOST)


class VupRefused(IOError):
    """The reader answered a VUP command with an error code."""

    def __init__(self, cmd: int, code: Optional[int], detail: str = ""):
        self.cmd = cmd
        self.code = code
        codestr = f"0x{code:02x}" if code is not None else "?"
        super().__init__(f"reader refused {cmd:02X}H{detail}: error {codestr}")


def vup_checksum(data: bytes) -> int:
    """Sum of header..param, inverted, plus one (two's complement)."""
    return (~sum(data) + 1) & 0xFF


def vup_frame(cmd: int, params: bytes = b"", address: int = 1) -> bytes:
    """Header(0x40) Length(2, big-endian: address+cmd+params+check) Address Cmd Params Check."""
    body = bytes([address, cmd]) + params
    head = bytes([VUP_HEADER]) + (len(body) + 1).to_bytes(2, "big") + body
    return head + bytes([vup_checksum(head)])


def vup_parse_reply(data: bytes) -> Tuple[bool, bytes]:
    """(success, params) from a reply frame; F0 = ok, F4 = error (params = code)."""
    if len(data) < 6:
        raise IOError(f"short VUP reply: {data.hex()}")
    if vup_checksum(data[:-1]) != data[-1]:
        raise IOError(f"VUP checksum mismatch: {data.hex()}")
    length = int.from_bytes(data[1:3], "big")
    params = data[5:3 + length - 1]
    return data[0] == VUP_OK, params


def vup_request(host: str, cmd: int, params: bytes = b"", port: Optional[int] = None,
                timeout: float = 2.0) -> Tuple[bool, bytes]:
    if port is None:
        port = VUP_PORT          # looked up at call time so tests can redirect it
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(vup_frame(cmd, params), (host, port))
        data, _ = sock.recvfrom(1024)
    finally:
        sock.close()
    return vup_parse_reply(data)


def get_polling_antennas(host: str) -> List[int]:
    ok, params = vup_request(host, VUP_CMD_GET_POLL_ANTENNAS)
    if not ok:
        raise VupRefused(VUP_CMD_GET_POLL_ANTENNAS, params[0] if params else None)
    return sorted(set(params))


def set_polling_antennas(host: str, antennas: List[int]) -> None:
    ants = sorted({int(a) for a in antennas if 1 <= int(a) <= VUP_MAX_ANTENNAS})
    if not ants:
        raise ValueError("no valid antenna numbers (1-8)")
    ok, params = vup_request(host, VUP_CMD_SET_POLL_ANTENNAS, bytes(ants))
    if not ok:
        raise VupRefused(VUP_CMD_SET_POLL_ANTENNAS, params[0] if params else None, f" {ants}")


def get_buzzer(host: str) -> bool:
    ok, params = vup_request(host, VUP_CMD_GET_BUZZER)
    if not ok or not params:
        raise VupRefused(VUP_CMD_GET_BUZZER, params[0] if params else None)
    return params[0] != 0


def set_buzzer(host: str, on: bool) -> None:
    ok, params = vup_request(host, VUP_CMD_SET_BUZZER, bytes([1 if on else 0]))
    if not ok:
        raise VupRefused(VUP_CMD_SET_BUZZER, params[0] if params else None)


def probe_port_count(host: str) -> Optional[int]:
    """How many antenna ports the reader has (8, 4, 2 or 1), or None.

    VUP has no "how many ports" query, but 15H refuses an antenna number
    the hardware lacks (error 0x1B), so ask for 1..8, then 1..4, 1..2 and
    1 until one is accepted; the reader's own answer to 16H afterwards is
    the final word (a reader that quietly accepts more than it has still
    reports what it really polls). Leaves the reader polling that set, so
    callers set the wanted set straight after.
    """
    for n in (8, 4, 2, 1):
        try:
            set_polling_antennas(host, list(range(1, n + 1)))
        except VupRefused as e:
            if e.code == VUP_ERR_ANTENNA_UNSUPPORTED:
                continue
            logger.warning(f"Vanch port probe: {e}")
            return None
        except (OSError, IOError) as e:
            logger.warning(f"Vanch port probe failed: {e}")
            return None
        try:
            now = get_polling_antennas(host)
        except (OSError, IOError):
            return n
        return max(now) if now else n
    return None


def parse_fifo(words: List[int]) -> List[Tuple[str, int]]:
    """Parse the register-5000 FIFO block into (epc_hex, antenna) tuples."""
    tags = []
    for i in range(0, min(len(words), FIFO_WORDS), WORDS_PER_TAG):
        tag = words[i:i + WORDS_PER_TAG]
        if len(tag) < 8:
            break
        antenna = (tag[0] >> 8) & 0xFF
        epc_len = tag[1]
        if not 0 < epc_len <= 12:
            continue
        raw = b"".join(struct.pack(">H", w) for w in tag[2:8])
        tags.append((raw[:epc_len].hex(), antenna))
    return tags


class ModbusTCPClient:
    """Minimal Modbus TCP client (read holding / write registers)."""

    def __init__(self, host: str, port: int = VANCH_DEFAULT_PORT, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._txid = 0
        self._lock = threading.Lock()

    def connect(self):
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Modbus socket closed")
            data += chunk
        return data

    def _request(self, pdu: bytes) -> bytes:
        with self._lock:
            self._txid = (self._txid + 1) & 0xFFFF
            mbap = struct.pack(">HHHB", self._txid, 0, len(pdu) + 1, UNIT_ID)
            self._sock.sendall(mbap + pdu)
            header = self._recv_exact(7)
            _, _, length, _ = struct.unpack(">HHHB", header)
            resp = self._recv_exact(length - 1)
        if resp[0] & 0x80:
            raise IOError(f"Modbus exception 0x{resp[1]:02x} for function 0x{pdu[0]:02x}")
        return resp

    def read_holding(self, addr: int, count: int) -> List[int]:
        resp = self._request(struct.pack(">BHH", 0x03, addr, count))
        nbytes = resp[1]
        return list(struct.unpack(f">{nbytes // 2}H", resp[2:2 + nbytes]))

    def write_registers(self, addr: int, values: List[int]):
        payload = struct.pack(">BHHB", 0x10, addr, len(values), len(values) * 2)
        payload += struct.pack(f">{len(values)}H", *values)
        self._request(payload)


class VanchReader(RFIDReader):
    """Vanch reader with the same interface as the Impinj RFIDReader."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._modbus: Optional[ModbusTCPClient] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._last_ok = 0.0
        self.port_count: Optional[int] = None        # learned from the reader (probe)
        self.active_antennas: Optional[List[int]] = None  # what it is polling now
        self.buzzer_on: Optional[bool] = None        # as the reader reports it; None = unknown

    def connect(self) -> bool:
        self.disconnect()
        host = self.reader_config.host
        port = self.reader_config.port or VANCH_DEFAULT_PORT
        if not self._check_reader_reachable(host, port):
            for cand in STATION_HOST_CANDIDATES:
                if cand != host and self._check_reader_reachable(cand, VANCH_DEFAULT_PORT):
                    logger.warning(f"Vanch reader not at {host}, adopting the one at {cand}")
                    self.reader_config.host = host = cand
                    self.reader_config.port = port = VANCH_DEFAULT_PORT
                    break
            else:
                logger.error(f"Vanch reader not reachable at {host}:{port} "
                             f"(also tried {', '.join(c for c in STATION_HOST_CANDIDATES if c != host)})")
                return False
        try:
            self._modbus = ModbusTCPClient(host, port)
            self._modbus.connect()
            # automatic mode + passive report, then TX power, persisted
            self._modbus.write_registers(REG_WORKING_MODE, [1, 1, 1, 3])
            self._modbus.write_registers(REG_POWER, [1, int(self.reader_config.power_dbm)])
            self._modbus.write_registers(REG_SAVE, [1])
            self._modbus.read_holding(REG_FIFO, FIFO_WORDS)  # drain stale FIFO
        except (OSError, IOError) as e:
            logger.error(f"Vanch reader configure failed at {host}:{port}: {e}")
            self.disconnect()
            return False

        logger.info(f"Connected to Vanch reader at {host}:{port} (automatic mode, passive report)")
        self._apply_polling_antennas(host)
        self._apply_buzzer(host)
        self._running = True
        self._last_ok = time.time()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="vanch-poll", daemon=True)
        self._poll_thread.start()
        return True

    def _apply_polling_antennas(self, host: str):
        """Make the reader poll the configured antenna ports.

        The 2026-09-12 meet ran on port 1 alone because the reader's polling
        set is a VUP-only setting the Modbus path never touched. Non-fatal:
        a reader without VUP/UDP just keeps whatever it has, and we log it.

        An empty list means "every port the reader has": the port count is
        probed, so a 4-port or an 8-port reader with antennas on any mix of
        ports all get polled without anyone editing config. Ports named in
        config that the reader does not have are dropped with an error in
        the log rather than failing the whole set.
        """
        configured = sorted({int(a) for a in (self.reader_config.antennas or [])
                             if 1 <= int(a) <= VUP_MAX_ANTENNAS})
        try:
            have = get_polling_antennas(host)
        except (OSError, IOError) as e:
            logger.warning(f"Vanch: could not read polling antennas over VUP ({e}); leaving reader as is")
            return

        if configured:
            want = configured
        else:
            count = probe_port_count(host)
            if count is None:
                logger.warning("Vanch: could not learn the port count; leaving reader polling "
                               f"{have} (set reader.antennas explicitly to change it)")
                self.active_antennas = have
                return
            self.port_count = count
            want = list(range(1, count + 1))
            logger.info(f"Vanch: {count}-port reader, polling every port")

        if have == want:
            logger.info(f"Vanch polling antennas already {have}")
            self.active_antennas = have
            return
        try:
            set_polling_antennas(host, want)
        except VupRefused as e:
            if e.code != VUP_ERR_ANTENNA_UNSUPPORTED:
                logger.error(f"Vanch: failed to set polling antennas {want} (was {have}): {e}")
                return
            count = probe_port_count(host)
            if not count:
                logger.error(f"Vanch: reader refused antennas {want} and its port count "
                             f"could not be learned; polling stays {have}")
                return
            self.port_count = count
            missing = [a for a in want if a > count]
            want = [a for a in want if a <= count] or list(range(1, count + 1))
            logger.error(f"Vanch: ports {missing} do not exist on this {count}-port reader; "
                         f"polling {want} instead - fix reader.antennas")
            try:
                set_polling_antennas(host, want)
            except (OSError, IOError, ValueError) as e2:
                logger.error(f"Vanch: failed to set polling antennas {want}: {e2}")
                return
        except (OSError, IOError, ValueError) as e:
            logger.error(f"Vanch: failed to set polling antennas {want} (was {have}): {e}")
            return
        try:
            now = get_polling_antennas(host)
        except (OSError, IOError):
            now = want
        self.active_antennas = now
        if self.port_count is None and now:
            self.port_count = max(max(now), max(want))
        logger.warning(f"Vanch polling antennas changed {have} -> {now}")

    def _apply_buzzer(self, host: str):
        """Beep per reader.beep (default off) - a fresh reader beeps on every
        read. Non-fatal like the antenna set: a reader without VUP keeps
        whatever it has."""
        want = bool(getattr(self.reader_config, "beep", False))
        try:
            have = get_buzzer(host)
            if have != want:
                set_buzzer(host, want)
                have = get_buzzer(host)
                logger.warning(f"Vanch buzzer turned {'on' if have else 'off'}")
            self.buzzer_on = have
        except (OSError, IOError) as e:
            logger.warning(f"Vanch: could not check or set the buzzer over VUP ({e})")

    def polling_antennas(self) -> Optional[List[int]]:
        """Antenna ports the reader is currently polling (VUP 16H), or None."""
        try:
            return get_polling_antennas(self.reader_config.host)
        except (OSError, IOError):
            return None

    def _poll_loop(self):
        while self._running:
            start = time.time()
            try:
                words = self._modbus.read_holding(REG_FIFO, FIFO_WORDS)
                self._last_ok = time.time()
            except (OSError, IOError) as e:
                if not self._running:
                    break
                # One immediate reconnect before giving up: the supervisor's
                # 15 s retry costs a whole pack of finishers.
                logger.warning(f"Vanch poll failed ({e}); reconnecting immediately")
                try:
                    self._modbus.connect()
                    self._last_ok = time.time()
                    continue
                except (OSError, IOError) as e2:
                    logger.error(f"Vanch poll failed, dropping connection: {e2}")
                    self._running = False
                    break
            tags = parse_fifo(words)
            if tags and logger.isEnabledFor(logging.DEBUG):
                # Raw record heads: settles where the antenna number really lives.
                for i in range(0, min(len(words), FIFO_WORDS), WORDS_PER_TAG):
                    rec = words[i:i + WORDS_PER_TAG]
                    if any(rec):
                        logger.debug("Vanch FIFO record words[0..21]: "
                                     + " ".join(f"{w:04x}" for w in rec))
            for epc, antenna in tags:
                self._handle_epc(epc, rssi=0, antenna=antenna)
            time.sleep(max(0.0, POLL_INTERVAL_SEC - (time.time() - start)))

    def read_registers(self, addr: int, count: int) -> List[int]:
        """Read holding registers over the live connection (diagnostics).
        Never use this on the FIFO block: reading it consumes tags."""
        if not self._modbus:
            raise IOError("reader not connected")
        if addr <= REG_FIFO < addr + count:
            raise ValueError("refusing to read the tag FIFO (it would consume reads)")
        return self._modbus.read_holding(addr, min(count, 125))

    def disconnect(self):
        self._running = False
        thread = self._poll_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._poll_thread = None
        if self._modbus:
            self._modbus.close()
            self._modbus = None

    @property
    def is_connected(self) -> bool:
        return self._running and (time.time() - self._last_ok) < STALE_SEC
