"""Split stations line their clocks up with the finish line.

A Pi station has no real-time clock and no time source on its radio-only
network, so every GUN broadcast carries the finish console's clock. The
station shifts the gun into its own time and, when it is far enough out,
steps the OS clock so its read timestamps and time-of-day agree with the
finish line. unittest style: python -m unittest tests.test_remote_clock
"""

import unittest
from datetime import datetime
from unittest import mock

from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config
from rfid_finishlynx.remote_agent import RemoteAgent
from rfid_finishlynx.remote_protocol import calculate_checksum, format_gun, parse_management_message


def bare_station(mode="remote_lap"):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.mode = mode
    app.config.lora.air_latency_sec = 0.0     # radio delay is tests/test_clock_sync.py's subject
    app.led_clock_service = None
    app.remote_clock_offset = 0.0
    app._remote_clock_samples = []
    app._clock_step_failed = False
    app.save_config = lambda: None
    return app


class TestProtocol(unittest.TestCase):

    def test_sent_at_roundtrip(self):
        msg = parse_management_message(format_gun(1757280000.25, "12", sent_at=1757281234.5))
        self.assertAlmostEqual(msg.fields["epoch"], 1757280000.25, places=2)
        self.assertEqual(msg.fields["hour_format"], "12")
        self.assertAlmostEqual(msg.fields["sent_at"], 1757281234.5, places=2)

    def test_sent_at_without_format_keeps_field_positions(self):
        raw = format_gun(0.0, sent_at=1757281234.0)
        self.assertTrue(raw.startswith(b"$GUN,ALL,0.00,,1757281234.00*"))
        msg = parse_management_message(raw)
        self.assertIsNone(msg.fields["hour_format"])
        self.assertEqual(msg.fields["sent_at"], 1757281234.0)

    def test_older_sender_has_no_sent_at(self):
        msg = parse_management_message(format_gun(1757280000.0, "24"))
        self.assertIsNone(msg.fields["sent_at"])
        msg = parse_management_message(format_gun(1757280000.0))
        self.assertIsNone(msg.fields["sent_at"])
        payload = "GUN,ALL,5.00,24,soon"
        raw = f"${payload}*{calculate_checksum(payload)}\n".encode()
        self.assertIsNone(parse_management_message(raw).fields["sent_at"])

    def test_agent_hands_sent_at_to_app(self):
        class DummyApp:
            class config:
                class lora:
                    reader_id = "S1"
            calls = []

            def set_remote_gun(self, epoch, hour_format=None, sent_at=None):
                self.calls.append((epoch, hour_format, sent_at))

        app = DummyApp()
        agent = RemoteAgent.__new__(RemoteAgent)
        agent._app = app
        agent.on_receive(format_gun(100.0, "12", sent_at=200.0))
        agent.on_receive(format_gun(0.0))
        self.assertEqual(app.calls, [(100.0, "12", 200.0), (0.0, None, None)])


class TestStationClock(unittest.TestCase):

    def test_gun_shifted_when_clock_cannot_be_stepped(self):
        app = bare_station()
        app._step_system_clock = mock.Mock(return_value=False)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=6000.0):
            # finish line says it is 1000.0; this box thinks it is 6000.0
            app.set_remote_gun(700.0, sent_at=1000.0)
        app._step_system_clock.assert_called_once()
        self.assertAlmostEqual(app.remote_clock_offset, 5000.0)
        self.assertEqual(app.remote_gun, datetime.fromtimestamp(5700.0))

    def test_gun_unshifted_after_clock_stepped(self):
        app = bare_station()
        app._step_system_clock = mock.Mock(return_value=True)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=6000.0):
            app.set_remote_gun(700.0, sent_at=1000.0)
        app._step_system_clock.assert_called_once_with(1000.0, 5000.0)
        self.assertEqual(app.remote_clock_offset, 0.0)
        self.assertEqual(app._remote_clock_samples, [])
        self.assertEqual(app.remote_gun, datetime.fromtimestamp(700.0))

    def test_clock_step_resyncs_the_led_clocks(self):
        """The 2026-09-17 case: the station booted 6 h off, set its LED clock
        from that, then had its OS clock stepped by the first GUN - and the
        display stayed wrong because the driver only refreshes hourly."""
        app = bare_station()
        app._step_system_clock = mock.Mock(return_value=True)
        app.led_clock_service = mock.Mock()
        with mock.patch("rfid_finishlynx.app.time.time", return_value=6000.0):
            app.set_remote_gun(0.0, sent_at=1000.0)
        app.led_clock_service.resync_time_of_day.assert_called_once()
        # a failed step or a small offset leaves the display alone
        app.led_clock_service.reset_mock()
        app._step_system_clock = mock.Mock(return_value=False)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=6000.0):
            app.set_remote_gun(0.0, sent_at=1000.0)
        app.led_clock_service.resync_time_of_day.assert_not_called()
        app._step_system_clock = mock.Mock(return_value=True)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1000.5):
            app.set_remote_gun(0.0, sent_at=1000.0)
        app.led_clock_service.resync_time_of_day.assert_not_called()

    def test_small_offset_applied_without_stepping(self):
        app = bare_station()
        app._step_system_clock = mock.Mock(return_value=True)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1000.8):
            app.set_remote_gun(500.0, sent_at=1000.0)
        app._step_system_clock.assert_not_called()
        self.assertAlmostEqual(app.remote_clock_offset, 0.8)
        self.assertEqual(app.remote_gun, datetime.fromtimestamp(500.8))

    def test_smallest_sample_wins_over_radio_latency(self):
        app = bare_station()
        app._step_system_clock = mock.Mock(return_value=False)
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1001.5):
            app.set_remote_gun(0.0, sent_at=1000.0)        # slow delivery: +1.5
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1030.3):
            app.set_remote_gun(0.0, sent_at=1030.0)        # quick delivery: +0.3
        with mock.patch("rfid_finishlynx.app.time.time", return_value=1061.2):
            app.set_remote_gun(0.0, sent_at=1060.0)        # slow again: +1.2
        self.assertAlmostEqual(app.remote_clock_offset, 0.3)
        self.assertIsNone(app.remote_gun)

    def test_no_sent_at_leaves_offset_alone(self):
        app = bare_station()
        app.remote_clock_offset = 2.5
        app.set_remote_gun(100.0)
        self.assertEqual(app.remote_clock_offset, 2.5)
        self.assertEqual(app.remote_gun, datetime.fromtimestamp(102.5))

    def test_step_refused_is_reported_once(self):
        app = bare_station()
        with mock.patch("rfid_finishlynx.app.os.name", "posix"), \
             mock.patch("rfid_finishlynx.app.subprocess.run") as run, \
             mock.patch("rfid_finishlynx.app.logger") as log:
            run.return_value = mock.Mock(returncode=1, stderr="sudo: a password is required")
            self.assertFalse(app._step_system_clock(1000.0, 5000.0))
            self.assertFalse(app._step_system_clock(1000.0, 5000.0))
            self.assertEqual(log.warning.call_count, 1)
            run.return_value = mock.Mock(returncode=0, stderr="")
            self.assertTrue(app._step_system_clock(1000.0, 5000.0))
            self.assertEqual(run.call_args[0][0], ["sudo", "-n", "date", "-u", "-s", "@1000.00"])

    def test_never_steps_a_windows_clock(self):
        app = bare_station()
        with mock.patch("rfid_finishlynx.app.os.name", "nt"), \
             mock.patch("rfid_finishlynx.app.subprocess.run") as run:
            self.assertFalse(app._step_system_clock(1000.0, 5000.0))
            run.assert_not_called()


class TestFinishLynxOnlyAtFinish(unittest.TestCase):

    def test_split_station_does_not_use_finishlynx(self):
        self.assertFalse(bare_station("remote_lap")._uses_finishlynx())
        self.assertTrue(bare_station("finish_line")._uses_finishlynx())


if __name__ == "__main__":
    unittest.main()
