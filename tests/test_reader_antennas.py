"""Antenna handling on stations that cannot interrogate their reader, and
remote antenna changes taking effect without a RESTART.
unittest style: python -m unittest tests.test_reader_antennas
"""

import unittest
from unittest import mock

from rfid_finishlynx.remote_agent import RemoteAgent
from rfid_finishlynx.remote_protocol import format_cfgs, parse_management_message
from rfid_finishlynx.rfid_reader import RFIDReader, usable_antennas


class TestUsableAntennas(unittest.TestCase):

    def test_unknown_port_count_trusts_config(self):
        self.assertEqual(usable_antennas([1, 2, 3, 4], None), [1, 2, 3, 4])
        self.assertEqual(usable_antennas([1, 3, 5, 7], None), [1, 3, 5, 7])

    def test_known_port_count_trims(self):
        self.assertEqual(usable_antennas([1, 2, 3, 4], 2), [1, 2])
        self.assertEqual(usable_antennas([1, 2, 3, 4], 4), [1, 2, 3, 4])
        self.assertEqual(usable_antennas([3, 4], 2), [1, 2])       # nothing fits: all ports

    def test_cleans_the_list(self):
        self.assertEqual(usable_antennas([], None), [1, 2])       # every Impinj has two
        self.assertEqual(usable_antennas([], 4), [1, 2, 3, 4])    # every port when known
        self.assertEqual(usable_antennas([2, 2, 0, 1], None), [2, 1])
        self.assertEqual(usable_antennas(["1", "3"], None), [1, 3])


class TestDetection(unittest.TestCase):

    def _reader(self, host="192.168.0.50"):
        r = RFIDReader.__new__(RFIDReader)
        r.reader_config = mock.Mock(host=host)
        return r

    def test_no_sshpass_means_unknown(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(self._reader()._detect_antenna_count())

    def test_model_detected(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="Model: Speedway R420")):
            self.assertEqual(self._reader()._detect_antenna_count(), 4)
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="Model: Speedway R220")):
            self.assertEqual(self._reader()._detect_antenna_count(), 2)

    def test_unrecognised_output_is_unknown_unless_hostname_hints(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="permission denied")):
            self.assertIsNone(self._reader()._detect_antenna_count())
            self.assertEqual(self._reader("speedwayr220.local")._detect_antenna_count(), 2)


class TestRemoteAntennaChange(unittest.TestCase):

    def _agent(self):
        class App:
            class config:
                class lora:
                    reader_id = "S1"

                class reader:
                    host = "192.168.0.50"
                    port = 5084
                    power_dbm = 20.0
                    antennas = [1, 2, 3, 4]

                class timing:
                    dedup_window_sec = 12.0
                    lap_mode = False
                    total_laps = 1

            saved = 0
            restarts = 0

            def save_config(self):
                self.saved += 1

            def restart_reader(self):
                self.restarts += 1
                return True

        sent = []
        agent = RemoteAgent.__new__(RemoteAgent)
        agent._app = App()
        agent._transport = mock.Mock(send=sent.append)
        agent._seq_cache = {}
        import threading
        agent._lock = threading.Lock()
        return agent, sent

    def test_reader_change_reconnects_reader_after_ack(self):
        agent, sent = self._agent()
        agent.on_receive(format_cfgs("S1", 7, "reader", {"antennas": [1, 3, 5, 7]}))
        agent._restart_thread.join(timeout=2)
        self.assertEqual(agent._app.config.reader.antennas, [1, 3, 5, 7])
        self.assertEqual(agent._app.saved, 1)
        self.assertEqual(agent._app.restarts, 1)
        ack = parse_management_message(sent[0])
        self.assertEqual((ack.msg_type.value, ack.seq, ack.fields["status"]), ("ACK", 7, "OK"))

    def test_other_sections_do_not_touch_the_reader(self):
        agent, sent = self._agent()
        agent.on_receive(format_cfgs("S1", 8, "timing", {"total_laps": 3}))
        self.assertFalse(hasattr(agent, "_restart_thread"))
        self.assertEqual(agent._app.config.timing.total_laps, 3)
        self.assertEqual(agent._app.restarts, 0)


if __name__ == "__main__":
    unittest.main()
