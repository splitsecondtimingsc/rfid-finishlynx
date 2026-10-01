"""12-hour vs 24-hour time-of-day on the LED clocks (unittest style so it
runs without pytest on the timing PC: python -m unittest tests.test_led_clock_hour_format)."""

import unittest
from datetime import datetime
from unittest import mock

from rfid_finishlynx.led_clock import (
    Ch340Driver,
    ElectroDriver,
    LEDClockService,
    electro_frame,
    frame,
    time_of_day_payload,
)


class FakePort:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        pass


class TestPayload(unittest.TestCase):

    def test_24_hour_default(self):
        self.assertEqual(time_of_day_payload(datetime(2026, 9, 15, 14, 5, 7)), "140507")
        self.assertEqual(time_of_day_payload(datetime(2026, 9, 15, 0, 5, 7)), "000507")

    def test_12_hour(self):
        self.assertEqual(time_of_day_payload(datetime(2026, 9, 15, 14, 5, 7), hour12=True), "020507")
        self.assertEqual(time_of_day_payload(datetime(2026, 9, 15, 0, 5, 7), hour12=True), "120507")
        self.assertEqual(time_of_day_payload(datetime(2026, 9, 15, 12, 0, 0), hour12=True), "120000")

    def test_electro_blanks_leading_zero_only_when_asked(self):
        t = datetime(2026, 9, 15, 9, 30, 0)
        self.assertEqual(time_of_day_payload(t, hour12=True, blank_leading_zero=True), "93000")
        self.assertEqual(time_of_day_payload(t, hour12=True), "093000")
        self.assertEqual(electro_frame(time_of_day_payload(t, hour12=True, blank_leading_zero=True)),
                         b"c 93000\r\n")


class TestDrivers(unittest.TestCase):

    def test_ch340_resends_time_when_format_flips(self):
        port = FakePort()
        with mock.patch("serial.Serial", lambda *a, **k: port):
            d = Ch340Driver("COMX")
        d.drive(None, lambda s: False)
        n = len(port.written)
        d.drive(None, lambda s: False)
        self.assertEqual(len(port.written), n)          # idle: no re-send
        d.hour12 = True
        with mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 15, 15, 45, 9)
            d.drive(None, lambda s: False)
        self.assertTrue(port.written.endswith(frame("M", "034509")))
        d.drive(None, lambda s: False)
        self.assertTrue(port.written.endswith(frame("M", "034509")))   # settled again

    def test_ch340_resync_resends_time_of_day_at_once(self):
        """After the station's OS clock is stepped over LoRa the clock must
        be re-set right away, not at the hourly refresh."""
        port = FakePort()
        with mock.patch("serial.Serial", lambda *a, **k: port):
            d = Ch340Driver("COMX")
        with mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 17, 21, 42, 0)     # wrong clock at boot
            d.drive(None, lambda s: False)
        self.assertTrue(port.written.endswith(frame("M", "214200")))
        n = len(port.written)
        d.drive(None, lambda s: False)
        self.assertEqual(len(port.written), n)                           # settled
        d.resync()
        with mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 17, 16, 57, 3)      # stepped
            d.drive(None, lambda s: False)
        self.assertTrue(port.written.endswith(frame("M", "165703")))

    def test_electro_idle_frame_in_12_hour(self):
        port = FakePort()
        with mock.patch("serial.Serial", lambda *a, **k: port):
            d = ElectroDriver("COMX")
        d.hour12 = True
        with mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 15, 15, 45, 9)
            d.drive(None, lambda s: False)
        self.assertEqual(port.written, b"c\r" + electro_frame("34509"))


def r_reply(hhmmss: str) -> bytes:
    """What the clock answers to 'R' when it shows hhmmss."""
    payload = b"2R" + hhmmss.encode()
    return b"\xf1\x1f\x1f" + payload + bytes([0x09, sum(payload) & 0xFF])


class ReplyingPort(FakePort):
    """FakePort whose 'R' reads come from a queue; time-of-day sets are noted."""

    def __init__(self):
        super().__init__()
        self.replies = []

    def read(self, n):
        if not self.replies:
            return b""
        r, rest = self.replies[0][:n], self.replies[0][n:]
        if rest:
            self.replies[0] = rest
        else:
            self.replies.pop(0)
        return r


class TestCh340Verify(unittest.TestCase):
    """2026-09-18: the time-of-day sent right after opening COM10 never
    reached the clock, which then showed a stale time (11 min slow) until the
    hourly refresh. Every set is now read back and re-sent if it did not take."""

    def _driver(self):
        port = ReplyingPort()
        with mock.patch("serial.Serial", lambda *a, **k: port):
            d = Ch340Driver("COMX")
        return d, port

    def test_lost_time_of_day_set_is_resent_after_read_back(self):
        d, port = self._driver()
        d.hour12 = True
        now = [1000.0]
        with mock.patch("rfid_finishlynx.led_clock.time.time", lambda: now[0]), \
                mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 18, 16, 36, 48)
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("M", "043648")), 1)
            now[0] += 1.0                                   # too soon to verify
            d.drive(None, lambda s: False)
            self.assertNotIn(frame("R"), port.written)
            now[0] += 1.5                                   # 2.5 s after the set
            port.replies.append(r_reply("042514"))          # clock never took it: 11.5 min slow
            dt.now.return_value = datetime(2026, 9, 18, 16, 36, 50)
            d.drive(None, lambda s: False)
            self.assertIn(frame("R"), port.written)
            self.assertTrue(port.written.endswith(frame("M", "043650")))
            # the re-send is verified in turn, and a good read-back settles it
            now[0] += 2.5
            port.replies.append(r_reply("043653"))
            dt.now.return_value = datetime(2026, 9, 18, 16, 36, 53)
            d.drive(None, lambda s: False)
            n = len(port.written)
            now[0] += 5.0
            d.drive(None, lambda s: False)
            self.assertEqual(len(port.written), n)         # nothing until the next spot-check

    def test_time_of_day_spot_check_catches_drift(self):
        d, port = self._driver()
        now = [1000.0]
        with mock.patch("rfid_finishlynx.led_clock.time.time", lambda: now[0]), \
                mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 18, 9, 0, 0)
            d.drive(None, lambda s: False)
            now[0] += 2.5
            port.replies.append(r_reply("090002"))         # took: within tolerance
            dt.now.return_value = datetime(2026, 9, 18, 9, 0, 2, 400000)
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("M", "090000")), 1)
            now[0] += 31.0                                  # next spot-check
            port.replies.append(r_reply("085900"))         # a minute slow
            dt.now.return_value = datetime(2026, 9, 18, 9, 0, 33)
            d.drive(None, lambda s: False)
            self.assertTrue(port.written.endswith(frame("M", "090033")))

    def test_12_hour_read_back_tolerates_clock_counting_past_noon(self):
        d, port = self._driver()
        d.hour12 = True
        now = [1000.0]
        with mock.patch("rfid_finishlynx.led_clock.time.time", lambda: now[0]), \
                mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 18, 12, 59, 50)
            d.drive(None, lambda s: False)                  # "125950"
            now[0] += 31.0
            port.replies.append(r_reply("130021"))         # clock rolled to 13:xx, we expect 01:xx
            dt.now.return_value = datetime(2026, 9, 18, 13, 0, 21)
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0M"), 1)   # no spurious re-set

    def test_silent_read_backs_resend_the_set(self):
        """2026-09-19: at plug-in the time-of-day set AND the first read-back
        were both lost, so the clock kept counting yesterday's race until the
        30-s spot-check. Silence is now retried in 2 s and re-sent after two."""
        d, port = self._driver()
        d.hour12 = True
        now = [1000.0]

        def ticking():          # a frozen clock would never time out the unanswered read
            now[0] += 0.01
            return now[0]

        with mock.patch("rfid_finishlynx.led_clock.time.time", ticking), \
                mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 19, 7, 44, 13)
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("M", "074413")), 1)
            now[0] += 2.5                                   # first read-back: no answer
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("R")), 1)
            self.assertEqual(port.written.count(b"\x1f0M"), 1)   # not re-sent yet
            now[0] += 1.0                                   # still waiting
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("R")), 1)
            now[0] += 1.5                                   # second silent read-back -> re-send
            dt.now.return_value = datetime(2026, 9, 19, 7, 44, 18)
            d.drive(None, lambda s: False)
            self.assertEqual(port.written.count(frame("R")), 2)
            self.assertTrue(port.written.endswith(frame("M", "074418")))
            now[0] += 2.5                                   # and that one is confirmed
            port.replies.append(r_reply("074420"))
            dt.now.return_value = datetime(2026, 9, 19, 7, 44, 20, 500000)
            d.drive(None, lambda s: False)
            n = len(port.written)
            now[0] += 10.0
            d.drive(None, lambda s: False)
            self.assertEqual(len(port.written), n)         # settled until the spot-check

    def test_resync_while_racing_rearms(self):
        """An operator click (or an OS clock step) re-sends the count-up too."""
        d, port = self._driver()
        gun = datetime(2026, 9, 19, 8, 0, 0)
        with mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 19, 8, 1, 2, 300000)
            d.drive(gun, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0T1"), 1)
            d.drive(gun, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0T1"), 1)   # same gun: nothing
            d.resync()
            d.drive(gun, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0T1"), 2)

    def test_lost_arm_is_resent_after_read_back(self):
        d, port = self._driver()
        now = [1000.0]
        gun = datetime(2026, 9, 18, 16, 42, 59, 477000)
        with mock.patch("rfid_finishlynx.led_clock.time.time", lambda: now[0]), \
                mock.patch("rfid_finishlynx.led_clock.datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 18, 16, 43, 2, 100000)
            d.drive(gun, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0T1"), 1)
            now[0] += 2.5
            port.replies.append(r_reply("043100"))         # still showing time of day
            dt.now.return_value = datetime(2026, 9, 18, 16, 43, 4, 600000)
            d.drive(gun, lambda s: False)
            self.assertEqual(port.written.count(b"\x1f0T1"), 2)


class TestService(unittest.TestCase):

    def test_service_resync_reaches_every_live_driver(self):
        class Drv:
            kind = "fake"

            def __init__(self, port):
                self.hour12 = None
                self.resyncs = 0

            def drive(self, gun, wait):
                return 0.01

            def resync(self):
                self.resyncs += 1

            def close(self):
                pass

        class Streaming:                      # electro-style: no resync() needed
            kind = "fake2"

            def __init__(self, port):
                self.hour12 = None

            def drive(self, gun, wait):
                return 0.01

            def close(self):
                pass

        drivers = []

        def factory(kind, port):
            drivers.append(Drv(port) if kind == "fake" else Streaming(port))
            return drivers[-1]

        svc = LEDClockService(lambda: None, driver_factory=factory,
                              find_clock_fn=lambda t, p: ("fake", "COMX"))
        self.assertTrue(svc._connect())
        svc.resync_time_of_day()
        self.assertEqual([d.resyncs for d in drivers if hasattr(d, "resyncs")], [1])

    def test_service_pushes_format_to_drivers(self):
        class Drv:
            kind = "fake"

            def __init__(self, port):
                self.hour12 = None
                self.seen = []

            def drive(self, gun, wait):
                self.seen.append(self.hour12)
                return 0.01

            def close(self):
                pass

        drivers = []

        def factory(kind, port):
            drivers.append(Drv(port))
            return drivers[-1]

        svc = LEDClockService(lambda: None, driver_factory=factory,
                              find_clock_fn=lambda t, p: ("fake", "COMX"), hour12=True)
        self.assertTrue(svc.hour12)
        self.assertTrue(svc._connect())
        svc._drive_all()
        svc.set_hour12(False)
        svc._drive_all()
        self.assertEqual(drivers[0].seen, [True, False])


class TestMirrorToStations(unittest.TestCase):
    """The GUN broadcast carries the finish console's 12/24 setting; a split
    station applies it so its clocks mirror the finish line."""

    def test_gun_message_roundtrip_with_format(self):
        from rfid_finishlynx.remote_protocol import format_gun, parse_management_message
        msg = parse_management_message(format_gun(1757280000.25, "12"))
        self.assertAlmostEqual(msg.fields["epoch"], 1757280000.25, places=2)
        self.assertEqual(msg.fields["hour_format"], "12")
        msg = parse_management_message(format_gun(0.0, "24"))
        self.assertEqual(msg.fields["epoch"], 0.0)
        self.assertEqual(msg.fields["hour_format"], "24")

    def test_old_sender_without_format_still_parses(self):
        from rfid_finishlynx.remote_protocol import format_gun, parse_management_message
        raw = format_gun(1757280000.0)                 # no format field
        self.assertTrue(raw.startswith(b"$GUN,ALL,1757280000.00*"))
        msg = parse_management_message(raw)
        self.assertEqual(msg.fields["epoch"], 1757280000.0)
        self.assertIsNone(msg.fields["hour_format"])
        # garbage in the optional field is ignored, not fatal
        payload = "GUN,ALL,5.00,banana"
        from rfid_finishlynx.remote_protocol import calculate_checksum
        raw = f"${payload}*{calculate_checksum(payload)}\n".encode()
        self.assertIsNone(parse_management_message(raw).fields["hour_format"])

    def test_agent_hands_format_to_app(self):
        from rfid_finishlynx.remote_agent import RemoteAgent
        from rfid_finishlynx.remote_protocol import format_gun

        class DummyApp:
            class config:
                class lora:
                    reader_id = "LAP1"
            calls = []

            def set_remote_gun(self, epoch, hour_format=None, sent_at=None):
                self.calls.append((epoch, hour_format))

        app = DummyApp()
        agent = RemoteAgent.__new__(RemoteAgent)
        agent._app = app
        agent.on_receive(format_gun(1757280000.0, "12"))
        agent.on_receive(format_gun(0.0))
        self.assertEqual(app.calls, [(1757280000.0, "12"), (0.0, None)])

    def test_station_applies_and_persists_mirrored_format(self):
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config

        class Svc:
            def __init__(self):
                self.hour12 = None

            def set_hour12(self, v):
                self.hour12 = v

        app = Application.__new__(Application)
        app.config = Config()
        app.led_clock_service = Svc()
        app.remote_clock_offset = 0.0
        app._remote_clock_samples = []
        saves = []
        app.save_config = lambda: saves.append(1)
        app.set_remote_gun(1757280000.0, hour_format="12")
        self.assertIsNotNone(app.remote_gun)
        self.assertEqual(app.config.led_clock.hour_format, "12")
        self.assertTrue(app.led_clock_service.hour12)
        self.assertEqual(saves, [1])
        # same format again: no churn; absent format: untouched
        app.set_remote_gun(0.0, hour_format="12")
        app.set_remote_gun(0.0)
        self.assertIsNone(app.remote_gun)
        self.assertEqual(app.config.led_clock.hour_format, "12")
        self.assertEqual(saves, [1])
        app.set_remote_gun(0.0, hour_format="24")
        self.assertFalse(app.led_clock_service.hour12)
        self.assertEqual(saves, [1, 1])

    def test_finish_rebroadcasts_on_format_change(self):
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config
        from types import SimpleNamespace

        sent = []
        app = Application.__new__(Application)
        app.config = Config()
        app.lora_receiver = SimpleNamespace(transport=SimpleNamespace(send=sent.append))
        app._gun_broadcast_epoch = None
        app._gun_broadcast_hour_format = None
        app._gun_broadcast_at = 0.0
        app._current_gun = lambda: None
        app._broadcast_gun()
        app._broadcast_gun()                       # unchanged, within 30 s: nothing
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith(b"$GUN,ALL,0.00,24,"))   # ...,<sent_at>*<sum>
        app.config.led_clock.hour_format = "12"
        app._broadcast_gun()
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[1].startswith(b"$GUN,ALL,0.00,12,"))
        app._gun_broadcast_at -= 31                # idle for 30 s: re-sent even with no race
        app._broadcast_gun()
        self.assertEqual(len(sent), 3)


if __name__ == "__main__":
    unittest.main()
