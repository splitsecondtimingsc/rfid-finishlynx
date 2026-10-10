import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime

from rfid_finishlynx.config import Config, LoRaConfig
from rfid_finishlynx.lora_receiver import LoRaReceiver
from rfid_finishlynx.remote_agent import RemoteAgent
from rfid_finishlynx.remote_manager import RemoteManager
from rfid_finishlynx.remote_protocol import (
    MessageType,
    format_ack,
    format_announce,
    format_cfgr,
    parse_management_message,
)
from rfid_finishlynx.gateway import (
    FACTORY_IP,
    STANDARD_IP,
    GatewayError,
    arp_neighbours,
    bind_socket,
    candidate_addresses,
    compare_radio,
    wired_interfaces,
)
from rfid_finishlynx.transport import (
    LoRaTransport,
    ReliableReadSender,
    format_lora_message,
    parse_any_lora_message,
    parse_lora_message,
    parse_tcp_endpoint,
)


class FakeTransport:
    """Records sends; always connected. Writes are 'transmitted' at once."""

    def __init__(self):
        self.sent = []
        self.is_connected = True
        self.on_sent = None

    def send(self, data):
        self.sent.append(data)
        if self.on_sent:
            self.on_sent(data)
        return True


class StubApp:
    """Minimal Application stand-in for RemoteAgent tests."""

    def __init__(self):
        self.config = Config()
        self.config.lora.reader_id = "LAP1"
        self._running = True
        self.reader = None
        self.read_sender = None
        self.status_calls = 0
        self.saved = False

    def start(self):
        return True

    def stop(self):
        pass

    def save_config(self):
        self.saved = True

    def get_status(self):
        self.status_calls += 1
        return {"total_reads": 5, "unique_bibs": 3}


class TestReadMessageFormat(unittest.TestCase):

    def test_round_trip_without_seq(self):
        ts = datetime.now().replace(hour=10, minute=30, second=15, microsecond=250000)
        raw = format_lora_message("123", ts, "LAP1")
        msg = parse_lora_message(raw)
        self.assertEqual(msg.bib, "123")
        self.assertEqual(msg.reader_id, "LAP1")
        self.assertIsNone(msg.seq)
        self.assertEqual(msg.timestamp.hour, 10)
        self.assertEqual(msg.timestamp.microsecond, 250000)

    def test_round_trip_with_seq(self):
        ts = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        raw = format_lora_message("45", ts, "LAP2", seq=0xAB)
        msg = parse_lora_message(raw)
        self.assertEqual(msg.bib, "45")
        self.assertEqual(msg.seq, 0xAB)

    def test_corrupt_checksum_rejected(self):
        ts = datetime.now()
        raw = format_lora_message("123", ts, "LAP1", seq=1)
        corrupted = raw.replace(b"123", b"124")
        self.assertIsNone(parse_lora_message(corrupted))

    def test_parse_any_dispatches_read_and_management(self):
        ts = datetime.now()
        read = parse_any_lora_message(format_lora_message("7", ts, "LAP1", seq=2))
        self.assertEqual(read.bib, "7")
        ann = parse_any_lora_message(format_announce("LAP1", "1.0.0", "idle"))
        self.assertEqual(ann.msg_type, MessageType.ANNOUNCE)


class TestLoRaReceiver(unittest.TestCase):

    def setUp(self):
        self.received = []
        self.mgmt = []
        self.receiver = LoRaReceiver(
            LoRaConfig(),
            on_message=self.received.append,
            on_management_message=self.mgmt.append,
        )
        self.transport = FakeTransport()
        self.receiver._transport = self.transport

    def test_seq_read_acked_and_dispatched_once(self):
        raw = format_lora_message("123", datetime.now(), "LAP1", seq=5)
        self.receiver._on_receive(raw)
        self.receiver._on_receive(raw)  # retransmission

        self.assertEqual(len(self.received), 1)
        # Both receptions ACKed so the remote stops retransmitting
        self.assertEqual(len(self.transport.sent), 2)
        ack = parse_management_message(self.transport.sent[0])
        self.assertEqual(ack.msg_type, MessageType.ACK)
        self.assertEqual(ack.reader_id, "LAP1")
        self.assertEqual(ack.seq, 5)
        self.assertEqual(ack.fields["status"], "OK")

        stats = self.receiver.get_stats()
        self.assertEqual(stats["received"], 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_same_seq_different_readers_not_deduped(self):
        self.receiver._on_receive(format_lora_message("1", datetime.now(), "LAP1", seq=0))
        self.receiver._on_receive(format_lora_message("2", datetime.now(), "LAP2", seq=0))
        self.assertEqual(len(self.received), 2)

    def test_same_content_different_seq_deduped(self):
        # Dedup is by content, so a very late retransmit can't double count
        # even after its seq has been reused for a newer read.
        ts = datetime.now()
        self.receiver._on_receive(format_lora_message("7", ts, "LAP1", seq=1))
        self.receiver._on_receive(format_lora_message("7", ts, "LAP1", seq=2))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.receiver.get_stats()["duplicates"], 1)

    def test_batch_acked_once_reads_dispatched_individually(self):
        from rfid_finishlynx.transport import format_batch_message
        ts = datetime.now()
        raw = format_batch_message("S1", 7, [(str(b), ts) for b in range(5)])
        self.receiver._on_receive(raw)

        self.assertEqual([m.bib for m in self.received], [str(b) for b in range(5)])
        self.assertEqual(len(self.transport.sent), 1)
        ack = parse_management_message(self.transport.sent[0])
        self.assertEqual(ack.msg_type, MessageType.ACK)
        self.assertEqual(ack.seq, 7)

        # Retransmitted batch: re-ACKed, but no read delivered twice
        self.receiver._on_receive(raw)
        self.assertEqual(len(self.received), 5)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(self.receiver.get_stats()["duplicates"], 5)

    def test_receiver_journal(self):
        path = tempfile.mktemp()
        try:
            receiver = LoRaReceiver(
                LoRaConfig(), on_message=self.received.append, journal_path=path
            )
            receiver._transport = self.transport
            raw = format_lora_message("42", datetime(2026, 8, 31, 12, 0, 0, 123000), "S1", seq=3)
            receiver._on_receive(raw)
            receiver._on_receive(raw)
            with open(path) as f:
                lines = f.read().splitlines()
        finally:
            if os.path.exists(path):
                os.unlink(path)
        self.assertIn("RECV bib=42 t=12:00:00.123 reader=S1 seq=03", lines[0])
        self.assertIn("DUP bib=42 t=12:00:00.123 reader=S1 seq=03", lines[1])

    def test_unseq_read_dispatched_without_ack(self):
        self.receiver._on_receive(format_lora_message("99", datetime.now(), "LAP1"))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.transport.sent, [])

    def test_dedup_entry_expires(self):
        self.receiver.READ_DEDUP_TTL_SEC = 0.05
        raw = format_lora_message("123", datetime.now(), "LAP1", seq=9)
        self.receiver._on_receive(raw)
        time.sleep(0.1)
        self.receiver._on_receive(raw)
        self.assertEqual(len(self.received), 2)

    def test_management_message_routed(self):
        self.receiver._on_receive(format_announce("LAP1", "1.0.0", "idle"))
        self.assertEqual(len(self.mgmt), 1)
        self.assertEqual(self.received, [])

    def test_garbage_counts_error(self):
        self.receiver._on_receive(b"$READ,garbage*FF\n")
        self.assertEqual(self.receiver.get_stats()["errors"], 1)


class TestReliableReadSender(unittest.TestCase):

    def setUp(self):
        self.transport = FakeTransport()
        self.sender = ReliableReadSender(self.transport, "LAP1")

    def tearDown(self):
        self.sender.stop()

    def test_ack_clears_pending(self):
        self.sender.send_read("123", datetime.now())
        self.assertEqual(self.sender.pending_count, 1)
        batch = parse_any_lora_message(self.transport.sent[0])
        self.sender.handle_ack(batch.seq)
        self.assertEqual(self.sender.pending_count, 0)

    def test_seq_increments(self):
        self.sender.send_read("1", datetime.now())
        self.sender.send_read("2", datetime.now())
        seqs = [parse_any_lora_message(m).seq for m in self.transport.sent]
        self.assertEqual(seqs, [0, 1])

    def test_queued_reads_share_one_batch(self):
        # Reads that accumulate before a flush travel under one envelope
        # and one sequence number.
        ts = datetime.now()
        with self.sender._lock:
            self.sender._queue.extend([(str(b), ts) for b in range(10)])
        self.sender.flush()
        self.assertEqual(len(self.transport.sent), 1)
        batch = parse_any_lora_message(self.transport.sent[0])
        self.assertEqual([m.bib for m in batch.reads], [str(b) for b in range(10)])
        self.assertEqual(self.sender.pending_count, 10)

    def test_oversized_queue_splits_into_batches(self):
        ts = datetime.now()
        with self.sender._lock:
            self.sender._queue.extend([(f"{b:04d}", ts) for b in range(60)])
        self.sender.flush()
        batches = [parse_any_lora_message(m) for m in self.transport.sent]
        self.assertGreater(len(batches), 1)
        for m in self.transport.sent:
            self.assertLessEqual(len(m), LoRaTransport.MAX_WRITE_BYTES)
        self.assertEqual(sum(len(b.reads) for b in batches), 60)
        self.assertEqual(len({b.seq for b in batches}), len(batches))

    def test_flush_waits_for_full_batch_or_linger(self):
        ts = datetime.now()
        with self.sender._lock:
            self.sender._queue_since = time.monotonic()
            self.sender._queue.append(("1", ts))
        self.assertFalse(self.sender._flush_due())  # 1 read, not lingered

        with self.sender._lock:
            self.sender._queue.extend([(f"{b:04d}", ts) for b in range(30)])
        self.assertTrue(self.sender._flush_due())  # full batch accumulated

        with self.sender._lock:
            self.sender._queue = [("1", ts)]
            self.sender._queue_since = time.monotonic() - 2.0
        self.assertTrue(self.sender._flush_due())  # lingered past LINGER_SEC

    def test_retransmit_never_gives_up(self):
        self.sender.RETRY_INTERVAL_SEC = 0.05
        self.sender.RETRY_MAX_SEC = 0.05
        self.sender.TICK_SEC = 0.02
        self.sender.start()
        self.sender.send_read("123", datetime.now())

        deadline = time.monotonic() + 5.0
        while len(self.transport.sent) < 6 and time.monotonic() < deadline:
            time.sleep(0.05)

        # Initial send + at least 5 retries, all identical, still pending
        self.assertGreaterEqual(len(self.transport.sent), 6)
        self.assertEqual(len(set(self.transport.sent)), 1)
        self.assertEqual(self.sender.pending_count, 1)

    def test_seq_reuse_drops_stale_read(self):
        self.sender.send_read("1", datetime.now())
        self.sender._seq = 0  # simulate the sequence space wrapping
        self.sender.send_read("2", datetime.now())
        self.assertEqual(self.sender.pending_count, 1)
        seqs = [parse_any_lora_message(m).seq for m in self.transport.sent]
        self.assertEqual(seqs, [0, 0])

    def test_journal_records_lifecycle(self):
        path = tempfile.mktemp()
        try:
            sender = ReliableReadSender(self.transport, "S1", journal_path=path)
            sender.send_read("42", datetime(2026, 8, 31, 12, 0, 0, 123000))
            batch = parse_any_lora_message(self.transport.sent[-1])
            sender.handle_ack(batch.seq)
            sender.send_read("43", datetime.now())
            sender.stop()
            with open(path) as f:
                lines = f.read().splitlines()
        finally:
            if os.path.exists(path):
                os.unlink(path)
        self.assertIn("READ bib=42 t=12:00:00.123", lines[0])
        self.assertIn("SENT seq=00 bib=42", lines[1])
        self.assertIn("ACKED seq=00 bib=42", lines[2])
        self.assertIn("READ bib=43", lines[3])
        self.assertIn("SENT seq=01 bib=43", lines[4])
        self.assertIn("DROP seq=01 bib=43 reason=stop", lines[5])

    def test_ack_stops_retransmits(self):
        self.sender.RETRY_INTERVAL_SEC = 0.05
        self.sender.TICK_SEC = 0.02
        self.sender.send_read("123", datetime.now())
        batch = parse_any_lora_message(self.transport.sent[0])
        self.sender.handle_ack(batch.seq)
        self.sender.start()
        time.sleep(0.3)
        self.assertEqual(len(self.transport.sent), 1)


class TestRemoteAgent(unittest.TestCase):

    def setUp(self):
        self.app = StubApp()
        self.transport = FakeTransport()
        self.agent = RemoteAgent(self.app, self.transport)

    def _cmd(self, seq, command):
        from rfid_finishlynx.remote_protocol import format_cmd
        return format_cmd("LAP1", seq, command)

    def test_get_status_acked(self):
        self.agent.on_receive(self._cmd(1, "GET_STATUS"))
        ack = parse_management_message(self.transport.sent[0])
        self.assertEqual(ack.fields["status"], "OK")
        self.assertIn("reads=5", ack.fields["data"])

    def test_duplicate_cmd_replays_cached_ack(self):
        self.agent.on_receive(self._cmd(1, "GET_STATUS"))
        self.agent.on_receive(self._cmd(1, "GET_STATUS"))
        self.assertEqual(self.app.status_calls, 1)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(self.transport.sent[0], self.transport.sent[1])

    def test_seq_cache_expires(self):
        self.agent.SEQ_CACHE_TTL_SEC = 0.05
        self.agent.on_receive(self._cmd(1, "GET_STATUS"))
        time.sleep(0.1)
        self.agent.on_receive(self._cmd(1, "GET_STATUS"))
        self.assertEqual(self.app.status_calls, 2)

    def test_cfgq_all_sends_and_replays_all_sections(self):
        from rfid_finishlynx.remote_protocol import format_cfgq
        self.agent.on_receive(format_cfgq("LAP1", 2, "all"))
        self.assertEqual(len(self.transport.sent), 4)
        sections = {parse_management_message(m).fields["section"] for m in self.transport.sent}
        self.assertEqual(sections, {"reader", "timing", "lora", "logging"})

        # Duplicate replays all four cached responses
        self.agent.on_receive(format_cfgq("LAP1", 2, "all"))
        self.assertEqual(len(self.transport.sent), 8)
        self.assertEqual(self.transport.sent[:4], self.transport.sent[4:])

    def test_cfgs_applies_and_saves(self):
        from rfid_finishlynx.remote_protocol import format_cfgs
        self.agent.on_receive(format_cfgs("LAP1", 3, "timing", {"total_laps": 4}))
        self.assertEqual(self.app.config.timing.total_laps, 4)
        self.assertTrue(self.app.saved)
        ack = parse_management_message(self.transport.sent[0])
        self.assertEqual(ack.fields["status"], "OK")

    def test_read_ack_routed_to_read_sender(self):
        class StubSender:
            def __init__(self):
                self.acked = []

            def handle_ack(self, seq):
                self.acked.append(seq)

        self.app.read_sender = StubSender()
        self.agent.on_receive(format_ack("LAP1", 7, "OK"))
        self.assertEqual(self.app.read_sender.acked, [7])
        self.assertEqual(self.transport.sent, [])

    def test_other_reader_ignored(self):
        from rfid_finishlynx.remote_protocol import format_cmd
        self.agent.on_receive(format_cmd("LAP2", 1, "GET_STATUS"))
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.app.status_calls, 0)


class TestRemoteManager(unittest.TestCase):

    def setUp(self):
        self.transport = FakeTransport()
        self.config = LoRaConfig()
        self.changes = []
        self.manager = RemoteManager(
            self.transport, self.config, on_unit_change=self.changes.append
        )

    def tearDown(self):
        self.manager.stop()

    def _announce(self, reader_id="LAP1"):
        self.manager.on_message(
            parse_management_message(format_announce(reader_id, "1.0.0", "idle"))
        )

    def test_announce_discovers_unit(self):
        self._announce()
        units = self.manager.get_units()
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["reader_id"], "LAP1")
        self.assertEqual(units[0]["state"], "idle")
        self.assertTrue(self.changes)

    def test_command_resolved_by_ack(self):
        self._announce()
        future = self.manager.send_command("LAP1", "STOP")
        sent = parse_management_message(self.transport.sent[0])
        self.assertEqual(sent.fields["command"], "STOP")

        self.manager.on_message(
            parse_management_message(format_ack("LAP1", sent.seq, "OK"))
        )
        result = future.result(timeout=1.0)
        self.assertTrue(result["success"])

    def test_command_to_unknown_unit_fails_fast(self):
        future = self.manager.send_command("NOPE", "STOP")
        self.assertFalse(future.result(timeout=1.0)["success"])

    def test_cfgq_all_accumulates_every_section(self):
        self._announce()
        future = self.manager.get_remote_config("LAP1", "all")
        sent = parse_management_message(self.transport.sent[0])
        seq = sent.seq

        sections = {
            "reader": {"host": "10.0.0.1", "port": "5084"},
            "timing": {"total_laps": "2"},
            "lora": {"reader_id": "LAP1"},
            "logging": {"level": "INFO"},
        }
        for name, values in sections.items():
            self.manager.on_message(
                parse_management_message(format_cfgr("LAP1", seq, name, values))
            )

        result = future.result(timeout=1.0)
        self.assertTrue(result["success"])
        self.assertEqual(set(result["sections"]), set(sections))
        self.assertEqual(result["sections"]["reader"]["host"], "10.0.0.1")
        # All sections also cached on the unit
        unit = self.manager.get_units()[0]
        self.assertEqual(set(unit["config_cache"]), set(sections))

    def test_cfgq_single_section_resolves_immediately(self):
        self._announce()
        future = self.manager.get_remote_config("LAP1", "timing")
        seq = parse_management_message(self.transport.sent[0]).seq
        self.manager.on_message(
            parse_management_message(format_cfgr("LAP1", seq, "timing", {"total_laps": "3"}))
        )
        result = future.result(timeout=1.0)
        self.assertTrue(result["success"])
        self.assertEqual(result["values"]["total_laps"], "3")

    def test_offline_detection(self):
        self.config.offline_timeout_sec = 0.1
        self._announce()
        self.manager.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.manager.get_units()[0]["state"] == "offline":
                break
            time.sleep(0.1)
        self.assertEqual(self.manager.get_units()[0]["state"], "offline")


class TestEndToEndDelivery(unittest.TestCase):
    """Remote sender -> finish line receiver -> ACK -> remote agent loopback."""

    def test_read_delivery_and_ack_loop(self):
        remote_transport = FakeTransport()
        host_transport = FakeTransport()

        # Remote side
        app = StubApp()
        sender = ReliableReadSender(remote_transport, "LAP1")
        app.read_sender = sender
        agent = RemoteAgent(app, remote_transport)

        # Finish line side
        received = []
        receiver = LoRaReceiver(LoRaConfig(), on_message=received.append)
        receiver._transport = host_transport

        # Remote transmits a read; radio carries it to the finish line
        sender.send_read("123", datetime.now())
        receiver._on_receive(remote_transport.sent[0])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].bib, "123")

        # Finish line ACKed; radio carries it back; pending cleared
        self.assertEqual(len(host_transport.sent), 1)
        agent.on_receive(host_transport.sent[0])
        self.assertEqual(sender.pending_count, 0)

    def test_lost_first_transmission_recovered_by_retry(self):
        remote_transport = FakeTransport()
        host_transport = FakeTransport()
        sender = ReliableReadSender(remote_transport, "LAP1")
        received = []
        receiver = LoRaReceiver(LoRaConfig(), on_message=received.append)
        receiver._transport = host_transport

        sender.RETRY_INTERVAL_SEC = 0.05
        sender.TICK_SEC = 0.02
        sender.start()
        try:
            sender.send_read("55", datetime.now())
            # First transmission "lost"; wait for a retransmission
            deadline = time.monotonic() + 5.0
            while len(remote_transport.sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(len(remote_transport.sent), 2)

            # Retry arrives at the finish line and is processed exactly once
            receiver._on_receive(remote_transport.sent[1])
            self.assertEqual(len(received), 1)
        finally:
            sender.stop()


class TestGatewayTransport(unittest.TestCase):
    """LoRaTransport over a TCP gateway (Ebyte E90-DTU ethernet models)."""

    def test_parse_tcp_endpoint(self):
        self.assertEqual(parse_tcp_endpoint("192.168.0.60:8886"), ("192.168.0.60", 8886))
        self.assertIsNone(parse_tcp_endpoint("/dev/ttyUSB0"))
        self.assertIsNone(parse_tcp_endpoint("COM3"))

    def test_roundtrip_over_socket(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        received = []
        transport = LoRaTransport(
            LoRaConfig(enabled=True, port=f"{host}:{port}"),
            on_receive=received.append,
        )
        threading.Thread(target=transport.connect, daemon=True).start()
        conn, _ = server.accept()
        try:
            deadline = time.monotonic() + 5.0
            while not transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.05)

            # Gateway -> application
            conn.sendall(format_lora_message("77", datetime.now(), "SPLIT1", seq=5))
            while not received and time.monotonic() < deadline:
                time.sleep(0.05)
            message = parse_lora_message(received[0])
            self.assertEqual(message.bib, "77")
            self.assertEqual(message.reader_id, "SPLIT1")
            self.assertEqual(message.seq, 5)

            # Application -> gateway
            self.assertTrue(transport.send(format_lora_message("88", datetime.now(), "LAP1")))
            conn.settimeout(5.0)
            self.assertEqual(parse_lora_message(conn.recv(256)).bib, "88")
        finally:
            transport.disconnect()
            conn.close()
            server.close()

    def test_connect_failure_on_closed_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
        probe.close()

        transport = LoRaTransport(LoRaConfig(enabled=True, port=f"{host}:{port}"))
        self.assertFalse(transport.connect())
        self.assertFalse(transport.is_connected)

    def test_writes_are_not_starved_by_an_idle_receive_loop(self):
        """The receive loop used to hold the transport lock while blocked in
        its 0.1 s socket read and re-take it at once, so a GUN/CMD write
        from another thread waited seconds (the console's config save
        stalled with a TCP gateway connected). Writes must go out promptly
        while the gateway is silent."""
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        transport = LoRaTransport(LoRaConfig(enabled=True, port=f"{host}:{port}"))
        threading.Thread(target=transport.connect, daemon=True).start()
        conn, _ = server.accept()
        try:
            deadline = time.monotonic() + 5.0
            while not transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)                      # let the receive loop settle into its reads

            t0 = time.monotonic()
            for i in range(20):
                self.assertTrue(transport.send(format_lora_message(str(i), datetime.now(), "LAP1")))
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5, f"20 writes took {elapsed:.2f}s - starved behind the receive loop")

            conn.settimeout(5.0)
            got = b""
            while got.count(b"\n") < 20 and time.monotonic() < deadline:
                got += conn.recv(4096)
            self.assertEqual(got.count(b"\n"), 20)
        finally:
            transport.disconnect()
            conn.close()
            server.close()


class TestReconnect(unittest.TestCase):
    """The link must heal itself; stations are a mile from the operator."""

    def test_reconnects_after_gateway_drops(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        received = []
        transport = LoRaTransport(
            LoRaConfig(enabled=True, port=f"{host}:{port}"),
            on_receive=received.append,
        )
        transport.RECONNECT_MIN_SEC = 0.05
        transport.RECONNECT_MAX_SEC = 0.2

        threading.Thread(target=transport.connect, daemon=True).start()
        first, _ = server.accept()
        try:
            deadline = time.monotonic() + 5.0
            while not transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(transport.is_connected)

            # Gateway reboots: the session dies underneath us.
            first.close()
            while transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(transport.is_connected)

            # It comes back and the transport reopens the link unaided.
            second, _ = server.accept()
            while not transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(transport.is_connected)

            second.sendall(format_lora_message("42", datetime.now(), "SPLIT1"))
            while not received and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(parse_lora_message(received[0]).bib, "42")
            second.close()
        finally:
            transport.disconnect()
            server.close()

    def test_disconnect_stops_reconnecting(self):
        transport = LoRaTransport(LoRaConfig(enabled=True, port="127.0.0.1:1"))
        transport.RECONNECT_MIN_SEC = 0.05
        transport.RECONNECT_MAX_SEC = 0.1

        self.assertFalse(transport.connect())  # nothing listening
        time.sleep(0.2)
        transport.disconnect()
        time.sleep(0.2)
        self.assertFalse(transport.is_connected)
        self.assertIsNone(transport._receive_thread)


class TestOutboundCoalescing(unittest.TestCase):
    """Reads arriving together must travel in one transmission."""

    def _paced_transport(self, server_port, interval=1.0):
        config = LoRaConfig(enabled=True, port=f"127.0.0.1:{server_port}")
        config.send_interval_sec = interval
        return LoRaTransport(config)

    def test_burst_is_coalesced_into_one_write(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _, port = server.getsockname()

        transport = self._paced_transport(port, interval=1.0)
        threading.Thread(target=transport.connect, daemon=True).start()
        conn, _ = server.accept()
        try:
            deadline = time.monotonic() + 5.0
            while not transport.is_connected and time.monotonic() < deadline:
                time.sleep(0.02)

            # A pack of runners crossing at once.
            for i in range(5):
                transport.send(format_lora_message(str(500 + i), datetime.now(), "SPLIT1"))

            conn.settimeout(5.0)
            first = conn.recv(4096)
            # Every read in the burst rides in a single transmission.
            self.assertEqual(first.count(b"\n"), 5)
            for i in range(5):
                self.assertIn(str(500 + i).encode(), first)
        finally:
            transport.disconnect()
            conn.close()
            server.close()

    def test_oversized_queue_splits_on_message_boundaries(self):
        config = LoRaConfig(enabled=True, port="127.0.0.1:1")
        config.send_interval_sec = 1.0
        transport = LoRaTransport(config)

        messages = [format_lora_message(str(600 + i), datetime.now(), "SPLIT1")
                    for i in range(10)]
        transport._outbox.extend(b"".join(messages))

        chunk = transport._take_from_outbox()
        self.assertLessEqual(len(chunk), LoRaTransport.MAX_WRITE_BYTES)
        self.assertTrue(chunk.endswith(b"\n"))  # never cuts a message in half
        remainder = transport._take_from_outbox()
        self.assertEqual(chunk + remainder, b"".join(messages))

    def test_queued_reads_survive_a_dropped_link(self):
        config = LoRaConfig(enabled=True, port="127.0.0.1:1")
        config.send_interval_sec = 0.05
        transport = LoRaTransport(config)
        transport._running = True  # sender loop runs without a live link

        message = format_lora_message("900", datetime.now(), "SPLIT1")
        transport.send(message)
        transport._start_sender_thread()
        time.sleep(0.4)
        transport._running = False
        transport._outbox_event.set()

        # Nothing was written (no link), so the read must still be queued.
        self.assertIn(b"900", bytes(transport._outbox))

    def test_backlog_is_capped_oldest_first(self):
        config = LoRaConfig(enabled=True, port="127.0.0.1:1")
        config.send_interval_sec = 1.0
        transport = LoRaTransport(config)

        for i in range(200):
            transport._outbox.extend(
                format_lora_message(str(1000 + i), datetime.now(), "SPLIT1"))
        transport._trim_outbox()

        self.assertLessEqual(len(transport._outbox), LoRaTransport.MAX_OUTBOX_BYTES)
        self.assertTrue(bytes(transport._outbox).startswith(b"$READ"))
        self.assertIn(b"1199", bytes(transport._outbox))   # newest kept
        self.assertNotIn(b"1000,", bytes(transport._outbox))  # oldest dropped

    def test_pacing_off_by_default_writes_immediately(self):
        self.assertEqual(LoRaConfig().send_interval_sec, 0.0)


class TestGatewayHelpers(unittest.TestCase):
    """Gateway discovery helpers (no hardware required)."""

    def test_radio_comparison_reports_mismatches(self):
        baseline = {"wlschannel": 40, "wlsnetid": 0, "wlsairboud": 3}
        self.assertEqual(compare_radio(dict(baseline), baseline), [])

        drifted = dict(baseline, wlschannel=23)
        self.assertEqual(compare_radio(drifted, baseline), ["wlschannel: 23 != 40"])

    def test_candidates_include_standard_and_factory_addresses(self):
        candidates = candidate_addresses()
        self.assertIn(STANDARD_IP, candidates)
        self.assertIn(FACTORY_IP, candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_candidates_put_explicit_address_first(self):
        self.assertEqual(candidate_addresses(["10.0.0.5"])[0], "10.0.0.5")

    def test_wired_interfaces_excludes_virtual_ones(self):
        for name in wired_interfaces():
            self.assertNotIn(name, ("lo",))
            self.assertFalse(name.startswith(("wl", "docker", "tail", "veth")))

    def test_bind_socket_is_a_no_op_without_an_interface(self):
        sock = socket.socket()
        try:
            bind_socket(sock, None)  # must not raise
        finally:
            sock.close()

    def test_bind_socket_rejects_unsupported_platform(self):
        if hasattr(socket, "SO_BINDTODEVICE"):
            self.skipTest("platform supports SO_BINDTODEVICE")
        sock = socket.socket()
        try:
            with self.assertRaises(GatewayError):
                bind_socket(sock, "eth0")
        finally:
            sock.close()

    def test_arp_table_parses_without_error(self):
        for ip, mac in arp_neighbours().items():
            self.assertRegex(ip, r"^\d+\.\d+\.\d+\.\d+$")
            self.assertRegex(mac, r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


if __name__ == "__main__":
    unittest.main()
