"""A station's race clock and read times within a second of the finish line.

The user's rule (2026-09-22): "no more than a second between this PC's
race time and the Pi's". Three things made that fail before:
  * the GUN's sent_at was stamped when the message was QUEUED, and the
    paced outbox held it 0.2 s to several seconds before it flew - the
    station read the wait as clock offset and ran that much behind;
  * the radio's own one-way delay was never subtracted;
  * the CH340 driver tolerated 1.5 s of drift and ignored gun shifts under
    1 s, and a read taken just before a tick counted as an error.
python -m unittest tests.test_clock_sync
"""
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config, LoRaConfig
from rfid_finishlynx.led_clock import Ch340Driver, GUN_MOVED_SEC, RACE_TOLERANCE_SEC
from rfid_finishlynx.remote_protocol import format_gun, parse_management_message, restamp_gun
from rfid_finishlynx.rfid_reader import TagRead
from rfid_finishlynx.transport import LoRaTransport


class TestRestamp(unittest.TestCase):

    def test_sent_at_is_rewritten_and_checksum_fixed(self):
        line = format_gun(1000.0, "12", sent_at=1000.0)
        out = restamp_gun(line, 1004.25)
        msg = parse_management_message(out)          # checksum must still verify
        self.assertIsNotNone(msg)
        self.assertEqual(msg.fields["epoch"], 1000.0)
        self.assertEqual(msg.fields["hour_format"], "12")
        self.assertEqual(msg.fields["sent_at"], 1004.25)

    def test_other_lines_untouched(self):
        for line in (b"$READ,S1,101,10:00:00.000*00\n", format_gun(5.0, "24"), b"garbage\n", b"\xff\xfe"):
            self.assertEqual(restamp_gun(line, 9.0), line)


class FakeSerial:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))

    def flush(self):
        pass


class TestTransportStampsAtTransmit(unittest.TestCase):

    def _transport(self):
        t = LoRaTransport(LoRaConfig(enabled=True, port="127.0.0.1:1"))
        t._serial = FakeSerial()
        t._connected = True
        return t

    def test_gun_leaves_with_the_current_clock(self):
        t = self._transport()
        stale = format_gun(700.0, "12", sent_at=1000.0)
        with mock.patch("rfid_finishlynx.transport.time.time", return_value=1003.5):
            self.assertTrue(t._write(stale))
        sent = parse_management_message(t._serial.written[0])
        self.assertEqual(sent.fields["sent_at"], 1003.5)
        self.assertEqual(sent.fields["epoch"], 700.0)

    def test_gun_in_a_coalesced_chunk_is_restamped_and_the_rest_kept(self):
        t = self._transport()
        read = b"$READ,S1,101,10:00:00.000*00\n"
        chunk = read + format_gun(0.0, "24", sent_at=1.0) + read
        with mock.patch("rfid_finishlynx.transport.time.time", return_value=2.0):
            t._write(chunk)
        lines = t._serial.written[0].splitlines(keepends=True)
        self.assertEqual((lines[0], lines[2]), (read, read))
        self.assertEqual(parse_management_message(lines[1]).fields["sent_at"], 2.0)

    def test_gun_without_sent_at_is_left_alone(self):
        t = self._transport()
        old = format_gun(0.0, "24")
        t._write(old)
        self.assertEqual(t._serial.written, [old])


def station():
    app = Application.__new__(Application)
    app.config = Config()
    app.config.mode = "remote_lap"
    app.config.lora.air_latency_sec = 0.3
    app.led_clock_service = None
    app.remote_clock_offset = 0.0
    app._remote_clock_samples = []
    app._clock_step_failed = False
    app.save_config = lambda: None
    app._step_system_clock = mock.Mock(return_value=False)
    return app


class TestStationOffset(unittest.TestCase):

    def test_air_delay_is_not_mistaken_for_clock_offset(self):
        app = station()
        # perfect clocks: the GUN lands 0.3 s after it left
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1000.3):
            app.set_remote_gun(500.0, sent_at=1000.0)
        self.assertAlmostEqual(app.remote_clock_offset, 0.0)
        self.assertEqual(app.remote_gun, datetime.fromtimestamp(500.0))

    def test_step_threshold_is_one_second_and_targets_the_true_time(self):
        app = station()
        app._step_system_clock = mock.Mock(return_value=True)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1001.2):
            app.set_remote_gun(0.0, sent_at=1000.0)          # 0.9 s ahead: no step
        app._step_system_clock.assert_not_called()
        self.assertAlmostEqual(app.remote_clock_offset, 0.9)
        # (the smallest sample in the 5-min window wins, so the earlier
        # 0.9 must have aged out before a 1.2 can count as the offset)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1401.5):
            app.set_remote_gun(0.0, sent_at=1400.0)          # 1.2 s ahead: step
        app._step_system_clock.assert_called_once()
        target, offset = app._step_system_clock.call_args[0]
        self.assertAlmostEqual(target, 1400.3)               # finish time as the frame lands
        self.assertAlmostEqual(offset, 1.2)
        self.assertEqual(app.remote_clock_offset, 0.0)


class TestReadsInFinishTime(unittest.TestCase):

    def test_read_timestamp_is_shifted_by_the_learned_offset(self):
        app = station()
        app.remote_clock_offset = 0.8                        # this Pi runs 0.8 s ahead
        app._remote_last_sent = {}
        app.config.timing.remote_repeat_suppress_sec = 60.0
        app._track_lap = lambda bib, ts, rid: {"time": "10:00:00.000", "lap_number": 1, "position": 1}
        app._on_tag_read_callback = None
        app.read_sender = mock.Mock()
        pi_time = datetime(2026, 9, 22, 10, 0, 0, 800000)
        app._on_remote_tag_read(TagRead(bib="101", timestamp=pi_time, epc="", rssi=0, antenna=1, reader_id="S1"))
        app.read_sender.send_read.assert_called_once_with("101", datetime(2026, 9, 22, 10, 0, 0))

    def test_no_offset_no_change(self):
        app = station()
        app._remote_last_sent = {}
        app.config.timing.remote_repeat_suppress_sec = 60.0
        app._track_lap = lambda bib, ts, rid: {"time": "10:00:00.000", "lap_number": 1, "position": 1}
        app._on_tag_read_callback = None
        app.read_sender = mock.Mock()
        t = datetime(2026, 9, 22, 10, 0, 0)
        app._on_remote_tag_read(TagRead(bib="101", timestamp=t, epc="", rssi=0, antenna=1, reader_id="S1"))
        app.read_sender.send_read.assert_called_once_with("101", t)


class TestDriverTolerance(unittest.TestCase):

    def test_race_error_ignores_the_display_second(self):
        # showing 12 while race time is 12.0 .. 12.999 is exact
        self.assertEqual(Ch340Driver._race_error(12, 12.0), 0.0)
        self.assertEqual(Ch340Driver._race_error(12, 12.99), 0.0)
        # a full second behind or ahead is a real error
        self.assertAlmostEqual(Ch340Driver._race_error(12, 13.6), 0.6)
        self.assertAlmostEqual(Ch340Driver._race_error(14, 12.5), 1.5)

    def test_thresholds_fit_the_one_second_rule(self):
        self.assertLessEqual(RACE_TOLERANCE_SEC, 1.0)
        self.assertLessEqual(GUN_MOVED_SEC, 0.5)


if __name__ == "__main__":
    unittest.main()
