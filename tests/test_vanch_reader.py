"""Tests for the Vanch Modbus TCP reader driver."""

import socket
import struct
import threading
import time

import pytest

from rfid_finishlynx.config import ReaderConfig, TimingConfig, load_config
from rfid_finishlynx.rfid_reader import RFIDReader, create_reader
from rfid_finishlynx.vanch_reader import (
    FIFO_WORDS,
    REG_FIFO,
    REG_POWER,
    REG_SAVE,
    REG_WORKING_MODE,
    VanchReader,
    parse_fifo,
)


def make_fifo(*tags):
    """Build an 88-word FIFO block from (epc_hex, antenna) tuples."""
    words = [0] * FIFO_WORDS
    for i, (epc_hex, antenna) in enumerate(tags):
        base = i * 22
        raw = bytes.fromhex(epc_hex)
        words[base] = (antenna << 8) | 0x01
        words[base + 1] = len(raw)
        padded = raw.ljust(12, b"\x00")
        for w in range(6):
            words[base + 2 + w] = struct.unpack(">H", padded[w * 2:w * 2 + 2])[0]
    return words


# Bib 21 as our tags are written: the decimal digits literally in the hex
# (96-bit EPC, zero-padded). ASCII-text EPCs are no longer decoded (2026-09-19).
EPC_BIB_21 = "21".rjust(24, "0")


class TestParseFifo:
    def test_empty_fifo(self):
        assert parse_fifo([0] * FIFO_WORDS) == []

    def test_single_tag(self):
        tags = parse_fifo(make_fifo((EPC_BIB_21, 2)))
        assert tags == [(EPC_BIB_21, 2)]

    def test_four_tags(self):
        epcs = [bytes(f"00000000002{i}", "ascii").hex() for i in range(4)]
        tags = parse_fifo(make_fifo(*[(e, 1) for e in epcs]))
        assert [t[0] for t in tags] == epcs

    def test_bogus_epc_length_skipped(self):
        words = make_fifo((EPC_BIB_21, 1))
        words[1] = 60  # > 12 bytes is invalid for the 96-bit area
        assert parse_fifo(words) == []


class FakeVanchServer:
    """Minimal Modbus TCP server emulating the VF-747P register map."""

    def __init__(self):
        self.writes = {}
        self.fifo_queue = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                while self._running:
                    header = self._recv_exact(conn, 7)
                    if header is None:
                        break
                    txid, proto, length, unit = struct.unpack(">HHHB", header)
                    pdu = self._recv_exact(conn, length - 1)
                    if pdu is None:
                        break
                    resp = self._handle(pdu)
                    mbap = struct.pack(">HHHB", txid, 0, len(resp) + 1, unit)
                    conn.sendall(mbap + resp)
            finally:
                conn.close()

    @staticmethod
    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            try:
                chunk = conn.recv(n - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _handle(self, pdu):
        fn = pdu[0]
        if fn == 0x10:
            addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
            values = list(struct.unpack(f">{count}H", pdu[6:6 + nbytes]))
            self.writes[addr] = values
            return struct.pack(">BHH", 0x10, addr, count)
        if fn == 0x03:
            addr, count = struct.unpack(">HH", pdu[1:5])
            if addr == REG_FIFO:
                words = self.fifo_queue.pop(0) if self.fifo_queue else [0] * FIFO_WORDS
            else:
                words = [0] * count
            return struct.pack(">BB", 0x03, count * 2) + struct.pack(f">{count}H", *words[:count])
        return struct.pack(">BB", fn | 0x80, 0x01)


@pytest.fixture
def server():
    s = FakeVanchServer()
    yield s
    s.stop()


def make_reader(server, on_tag_read=None):
    return VanchReader(
        ReaderConfig(host="127.0.0.1", port=server.port, type="vanch"),
        TimingConfig(),
        on_tag_read=on_tag_read,
    )


class TestVanchReader:
    def test_connect_configures_automatic_passive_mode(self, server):
        reader = make_reader(server)
        assert reader.connect()
        assert reader.is_connected
        assert server.writes[REG_WORKING_MODE] == [1, 1, 1, 3]
        assert server.writes[REG_POWER][0] == 1
        assert server.writes[REG_SAVE] == [1]
        reader.disconnect()
        assert not reader.is_connected

    def test_tag_read_emitted(self, server):
        reads = []
        reader = make_reader(server, on_tag_read=reads.append)
        assert reader.connect()
        server.fifo_queue.append(make_fifo((EPC_BIB_21, 3)))
        deadline = time.time() + 2.0
        while not reads and time.time() < deadline:
            time.sleep(0.01)
        reader.disconnect()
        assert len(reads) == 1
        assert reads[0].bib == "21"
        assert reads[0].epc == EPC_BIB_21
        assert reads[0].antenna == 3

    def test_dedup_suppresses_repeat(self, server):
        reads = []
        reader = make_reader(server, on_tag_read=reads.append)
        assert reader.connect()
        server.fifo_queue.append(make_fifo((EPC_BIB_21, 1)))
        server.fifo_queue.append(make_fifo((EPC_BIB_21, 1)))
        deadline = time.time() + 2.0
        while len(server.fifo_queue) > 0 and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        reader.disconnect()
        assert len(reads) == 1

    def test_connect_fails_when_unreachable(self, monkeypatch):
        # Keep the factory-default fallback from finding a real bench reader
        import rfid_finishlynx.vanch_reader as vr
        monkeypatch.setattr(vr, "VANCH_DEFAULT_HOST", "127.0.0.1")
        monkeypatch.setattr(vr, "VANCH_DEFAULT_PORT", 1)
        reader = VanchReader(
            ReaderConfig(host="127.0.0.1", port=1, type="vanch"),
            TimingConfig(),
        )
        assert not reader.connect()
        assert not reader.is_connected


class TestAutoAdoption:
    """Supervisor swaps reader vendor when the other type answers."""

    def _make_app(self, reader_type):
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config
        app = Application.__new__(Application)
        app.config = Config()
        app.config.mode = "finish_line"
        app.config.reader = ReaderConfig(host="127.0.0.1", type=reader_type)
        app.reader = None
        app.save_config = lambda: None
        return app

    def test_adopts_vanch_when_impinj_configured(self, server, monkeypatch):
        import rfid_finishlynx.app as app_module
        monkeypatch.setattr(app_module, "default_port_for", lambda t: server.port)
        app = self._make_app("impinj")
        assert app._try_adopt_other_reader_type()
        assert isinstance(app.reader, VanchReader)
        assert app.config.reader.type == "vanch"
        assert app.config.reader.port == server.port
        app.reader.disconnect()

    def test_no_adoption_when_nothing_answers(self, monkeypatch):
        import rfid_finishlynx.app as app_module
        import rfid_finishlynx.vanch_reader as vr
        monkeypatch.setattr(app_module, "default_port_for", lambda t: 1)
        monkeypatch.setattr(vr, "VANCH_DEFAULT_HOST", "127.0.0.1")
        monkeypatch.setattr(vr, "VANCH_DEFAULT_PORT", 1)
        app = self._make_app("impinj")
        assert not app._try_adopt_other_reader_type()
        assert app.reader is None
        assert app.config.reader.type == "impinj"


class TestFactoryAndConfig:
    def test_factory_returns_vanch(self):
        reader = create_reader(ReaderConfig(type="vanch"), TimingConfig())
        assert isinstance(reader, VanchReader)

    def test_factory_defaults_to_impinj(self):
        reader = create_reader(ReaderConfig(), TimingConfig())
        assert isinstance(reader, RFIDReader)
        assert not isinstance(reader, VanchReader)

    def test_config_parses_type_and_defaults_vanch_port(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("reader:\n  host: 1.2.3.4\n  type: vanch\n")
        config = load_config(str(p))
        assert config.reader.type == "vanch"
        assert config.reader.port == 502


class TestBibExtraction:
    """EPC -> bib decoding across the supported encodings."""

    def _extract(self, epc):
        reader = RFIDReader(ReaderConfig(), TimingConfig())
        return reader._extract_bib_from_epc(epc)

    def test_bibs_30_to_39_are_not_ascii_digits(self):
        # 2026-09-19: 0x30-0x39 bytes were being read as ASCII "0"-"9"
        for bib in range(30, 40):
            assert self._extract(f"{bib:08d}") == str(bib)
        assert self._extract("00003035") == "3035"

    def test_ascii_text_epcs_are_not_decoded(self):
        # no chip of ours is written as text; such an EPC is simply unknown
        assert self._extract(bytes("000000000021", "ascii").hex()) is None

    def test_all_digit_hex_is_literal_decimal(self):
        # The Vanch bench tag: EPC 00000016 is bib 16, not 0x16 = 22
        assert self._extract("00000016") == "16"

    def test_hex_int_when_digits_include_af(self):
        assert self._extract("00000a2c") == str(0x0A2C)
