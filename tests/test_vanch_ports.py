"""Any Vanch on any station: the port count is probed, "every port" is the
default, and ports the reader lacks are dropped instead of failing.
unittest style: python -m unittest tests.test_vanch_ports
"""

import socket
import threading
import unittest
from unittest import mock

import rfid_finishlynx.vanch_reader as vr
from rfid_finishlynx.config import ReaderConfig, TimingConfig, parse_antennas
from rfid_finishlynx.remote_agent import RemoteAgent
from rfid_finishlynx.remote_protocol import format_cfgq, format_cfgs, parse_management_message
from rfid_finishlynx.vanch_reader import (
    VUP_ERR_ANTENNA_UNSUPPORTED, VanchReader, VupRefused, probe_port_count, vup_checksum,
)


class FakeVupReader:
    """UDP server answering 15H/16H like a reader with `ports` antenna ports:
    15H with any antenna above that is refused with error 0x1B."""

    def __init__(self, ports, polling=(1,)):
        self.ports = ports
        self.antennas = list(polling)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(3.0)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def reply(self, cmd, params=b"", ok=True):
        body = bytes([0x01, cmd]) + params
        head = bytes([0xF0 if ok else 0xF4]) + (len(body) + 1).to_bytes(2, "big") + body
        return head + bytes([vup_checksum(head)])

    def serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except (socket.timeout, OSError):
                return
            cmd = data[4]
            if cmd == 0x16:
                self.sock.sendto(self.reply(0x16, bytes(self.antennas)), addr)
            elif cmd == 0x15:
                want = list(data[5:-1])
                if max(want) > self.ports:
                    self.sock.sendto(self.reply(0x15, bytes([VUP_ERR_ANTENNA_UNSUPPORTED]), ok=False), addr)
                else:
                    self.antennas = want
                    self.sock.sendto(self.reply(0x15), addr)

    def close(self):
        self.sock.close()


class VupServerCase(unittest.TestCase):
    ports = 4

    def setUp(self):
        self.srv = FakeVupReader(self.ports)
        self._old = vr.VUP_PORT
        vr.VUP_PORT = self.srv.port

    def tearDown(self):
        vr.VUP_PORT = self._old
        self.srv.close()

    def reader(self, antennas):
        r = VanchReader(ReaderConfig(host="127.0.0.1", port=502, antennas=antennas, type="vanch"),
                        TimingConfig())
        return r


class TestProbeFourPort(VupServerCase):
    ports = 4

    def test_probe_finds_four(self):
        self.assertEqual(probe_port_count("127.0.0.1"), 4)

    def test_every_port_polls_all_four(self):
        r = self.reader([])
        r._apply_polling_antennas("127.0.0.1")
        self.assertEqual(self.srv.antennas, [1, 2, 3, 4])
        self.assertEqual(r.port_count, 4)
        self.assertEqual(r.active_antennas, [1, 2, 3, 4])

    def test_ports_the_reader_lacks_are_dropped_not_fatal(self):
        r = self.reader([1, 3, 5, 7])
        r._apply_polling_antennas("127.0.0.1")
        self.assertEqual(self.srv.antennas, [1, 3])
        self.assertEqual(r.port_count, 4)
        self.assertEqual(r.active_antennas, [1, 3])

    def test_explicit_ports_are_set_exactly(self):
        r = self.reader([2, 4])
        r._apply_polling_antennas("127.0.0.1")
        self.assertEqual(self.srv.antennas, [2, 4])
        self.assertEqual(r.active_antennas, [2, 4])


class TestProbeEightPort(VupServerCase):
    ports = 8

    def test_probe_finds_eight(self):
        self.assertEqual(probe_port_count("127.0.0.1"), 8)

    def test_every_port_polls_all_eight(self):
        r = self.reader([])
        r._apply_polling_antennas("127.0.0.1")
        self.assertEqual(self.srv.antennas, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(r.port_count, 8)

    def test_odd_ports_on_eight_port_reader(self):
        r = self.reader([1, 3, 5, 7])
        r._apply_polling_antennas("127.0.0.1")
        self.assertEqual(self.srv.antennas, [1, 3, 5, 7])


class TestProbeUnreachable(unittest.TestCase):

    def test_no_vup_means_none_and_reader_left_alone(self):
        with mock.patch.object(vr, "vup_request", side_effect=OSError("timed out")):
            self.assertIsNone(probe_port_count("127.0.0.1"))
            r = VanchReader(ReaderConfig(host="127.0.0.1", port=502, antennas=[], type="vanch"), TimingConfig())
            r._apply_polling_antennas("127.0.0.1")       # must not raise
            self.assertIsNone(r.port_count)

    def test_refused_error_carries_code(self):
        e = VupRefused(0x15, 0x1B, " [1, 9]")
        self.assertEqual(e.code, 0x1B)
        self.assertIn("15H [1, 9]: error 0x1b", str(e))


class TestParseAntennas(unittest.TestCase):

    def test_all_forms_of_every_port(self):
        for v in (None, "", "all", "ALL", "auto", []):
            self.assertEqual(parse_antennas(v), [])

    def test_lists_and_strings(self):
        self.assertEqual(parse_antennas([1, 3, 5, 7]), [1, 3, 5, 7])
        self.assertEqual(parse_antennas("1.3.5.7"), [1, 3, 5, 7])     # radio encoding
        self.assertEqual(parse_antennas("2, 4"), [2, 4])
        self.assertEqual(parse_antennas(["1", "1", "0", "2"]), [1, 2])
        self.assertEqual(parse_antennas(3), [3])

    def test_yaml_all_loads_as_every_port(self):
        import os
        import tempfile
        from rfid_finishlynx.config import load_config
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.yaml")
            with open(p, "w") as f:
                f.write("mode: remote_lap\nreader:\n  type: vanch\n  host: 192.168.0.50\n  antennas: all\n")
            cfg = load_config(p)
        self.assertEqual(cfg.reader.antennas, [])
        self.assertEqual(cfg.reader.type, "vanch")
        self.assertEqual(cfg.reader.port, 502)


class TestRemoteReaderSection(unittest.TestCase):

    def _agent(self):
        from types import SimpleNamespace

        class App:
            class config:
                class lora:
                    reader_id = "S1"
                reader = ReaderConfig(host="192.168.0.50", port=502, antennas=[], power_dbm=20.0, type="vanch")
                class timing:
                    dedup_window_sec = 12.0
                    lap_mode = False
                    total_laps = 1
            reader = SimpleNamespace(port_count=8, active_antennas=[1, 2, 3, 4, 5, 6, 7, 8])
            restarts = 0

            def save_config(self):
                pass

            def restart_reader(self):
                self.restarts += 1
                return True

        sent = []
        agent = RemoteAgent.__new__(RemoteAgent)
        agent._app = App()
        agent._transport = mock.Mock(send=sent.append)
        agent._seq_cache = {}
        agent._lock = threading.Lock()
        return agent, sent

    def test_query_reports_type_ports_and_every_port(self):
        agent, sent = self._agent()
        agent.on_receive(format_cfgq("S1", 3, "reader"))
        msg = parse_management_message(sent[0])
        v = msg.fields["values"]
        self.assertEqual((v["type"], v["ports"], v["antennas"], v["active"]),
                         ("vanch", "8", "", "1.2.3.4.5.6.7.8"))

    def test_set_specific_ports_then_back_to_every_port(self):
        agent, sent = self._agent()
        agent.on_receive(format_cfgs("S1", 4, "reader", {"antennas": [1, 3, 5, 7], "power_dbm": 25}))
        agent._restart_thread.join(2)
        self.assertEqual(agent._app.config.reader.antennas, [1, 3, 5, 7])
        self.assertEqual(agent._app.config.reader.power_dbm, 25.0)
        agent.on_receive(format_cfgs("S1", 5, "reader", {"antennas": []}))
        agent._restart_thread.join(2)
        self.assertEqual(agent._app.config.reader.antennas, [])
        self.assertEqual(agent._app.restarts, 2)

    def test_switching_type_picks_native_port(self):
        agent, sent = self._agent()
        agent.on_receive(format_cfgs("S1", 6, "reader", {"type": "impinj"}))
        agent._restart_thread.join(2)
        self.assertEqual((agent._app.config.reader.type, agent._app.config.reader.port), ("impinj", 5084))
        agent.on_receive(format_cfgs("S1", 7, "reader", {"type": "bogus"}))
        self.assertEqual(parse_management_message(sent[-1]).fields["status"], "ERR")


if __name__ == "__main__":
    unittest.main()
