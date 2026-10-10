"""Tests for the LED race clock service (both clock models) and GUN broadcast."""

import time
from datetime import datetime, timedelta

from rfid_finishlynx.led_clock import (
    Ch340Driver,
    ElectroDriver,
    LEDClockService,
    electro_frame,
    electro_running_payload,
    find_clock,
    frame,
    hhmmss_to_seconds,
    parse_read_response,
    seconds_to_hhmmss,
)
from rfid_finishlynx.remote_protocol import (
    ManagementMessage,
    MessageType,
    format_gun,
    parse_management_message,
)


class TestCh340Protocol:
    def test_frame(self):
        assert frame("R") == bytes([0x1F, 0x30, 0x52, 0x09])
        assert frame("T", "1000000") == b"\x1f\x30T1000000\x09"

    def test_parse_read_response_live_frame(self):
        # Captured from hardware: 19:00:10, checksum 0xAF
        raw = bytes.fromhex("F11F1F3252313930303130 09 AF".replace(" ", ""))
        assert parse_read_response(raw) == "190010"

    def test_parse_rejects_bad_checksum(self):
        raw = bytes.fromhex("F11F1F325231393030313009AE")
        assert parse_read_response(raw) is None

    def test_parse_rejects_garbage(self):
        assert parse_read_response(b"") is None
        assert parse_read_response(b"\x00\x01\x02") is None

    def test_hhmmss_roundtrip(self):
        assert seconds_to_hhmmss(0) == "000000"
        assert seconds_to_hhmmss(3661) == "010101"
        assert hhmmss_to_seconds("190010") == 19 * 3600 + 10
        assert seconds_to_hhmmss(-5) == "000000"


class TestElectroProtocol:
    def test_frame_exact_width_and_framing(self):
        assert electro_frame("888888") == b"c888888\r\n"
        assert electro_frame("8") == b"c     8\r\n"  # right-justified, space-padded
        assert electro_frame("") == b"c      \r\n"

    def test_frame_overflow_raises(self):
        import pytest
        with pytest.raises(ValueError):
            electro_frame("1234567")

    def test_running_payload(self):
        assert electro_running_payload(0.0) == "00000 "
        assert electro_running_payload(83.45) == "01234 "  # 1:23.4
        assert electro_running_payload(-3.0) == "00000 "
        assert electro_running_payload(999999.0) == "99599 "  # capped 99:59.9


class TestDiscovery:
    def test_explicit_port_uses_type(self):
        assert find_clock("electro", "/dev/ttyUSB3") == ("electro", "/dev/ttyUSB3")
        assert find_clock("auto", "/dev/ttyUSB3") == ("ch340", "/dev/ttyUSB3")


class FakeSerialDriver:
    """Stands in for either driver class in service tests."""
    kind = "fake"

    def __init__(self, port):
        self.port = port
        self.driven = []
        self.closed = False

    def drive(self, gun, wait):
        self.driven.append(gun)
        return 0.01

    def close(self):
        self.closed = True


def make_service(get_gun, found=("fake", "/dev/fake0")):
    drivers = []

    def factory(kind, port):
        d = FakeSerialDriver(port)
        drivers.append(d)
        return d

    svc = LEDClockService(get_gun, driver_factory=factory,
                          find_clock_fn=lambda t, p: found)
    return svc, drivers


class TestLEDClockService:
    def test_connect_and_kind(self):
        svc, drivers = make_service(lambda: None)
        assert svc._connect()
        assert svc.is_connected
        assert svc.clock_kind == "fake"

    def test_no_clock_found(self):
        svc, drivers = make_service(lambda: None, found=None)
        assert not svc._connect()
        assert not svc.is_connected

    def test_loop_drives_and_stop_closes(self):
        gun = datetime.now()
        svc, drivers = make_service(lambda: gun)
        svc.start()
        deadline = time.time() + 2.0
        while (not drivers or len(drivers[0].driven) < 3) and time.time() < deadline:
            time.sleep(0.01)
        svc.stop()
        assert drivers and len(drivers[0].driven) >= 3
        assert drivers[0].driven[0] == gun
        assert drivers[0].closed


class FakePort:
    """Minimal serial.Serial stand-in recording writes."""

    def __init__(self):
        self.written = b""
        self.reply = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        r, self.reply = self.reply[:n], self.reply[n:]
        return r

    def close(self):
        pass


def make_ch340(monkeypatch):
    port = FakePort()
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    return Ch340Driver("/dev/fake0"), port


def make_electro(monkeypatch):
    port = FakePort()
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    return ElectroDriver("/dev/fake0"), port


class TestCh340Driver:
    def test_gun_arms_countup(self, monkeypatch):
        d, port = make_ch340(monkeypatch)
        gun = datetime.now() - timedelta(seconds=125.9)
        d.drive(gun, lambda s: False)
        assert frame("T", "1" + "000206") in port.written  # 126 s = 00:02:06

    def test_idle_sets_time_of_day_once(self, monkeypatch):
        d, port = make_ch340(monkeypatch)
        d.drive(None, lambda s: False)
        assert port.written.startswith(bytes([0x1F, 0x30, ord("M")]))
        n = len(port.written)
        d.drive(None, lambda s: False)
        assert len(port.written) == n  # no re-send while idle

    def test_no_rearm_on_same_gun(self, monkeypatch):
        d, port = make_ch340(monkeypatch)
        gun = datetime.now() - timedelta(seconds=5)
        d.drive(gun, lambda s: False)
        n = len(port.written)
        d.drive(gun, lambda s: False)
        assert len(port.written) == n

    def test_drift_rearms(self, monkeypatch):
        d, port = make_ch340(monkeypatch)
        gun = datetime.now() - timedelta(seconds=50)
        d.drive(gun, lambda s: False)
        # fake an 'R' reply showing 10 s while the race is at ~50 s
        shown = "000010"
        payload = b"2R" + shown.encode()
        port.reply = b"\xf1\x1f\x1f" + payload + bytes([0x09, sum(payload) & 0xFF])
        d._drift_checked_at = time.time() - 120
        before = port.written.count(b"\x1f\x30T1")
        d.drive(gun, lambda s: False)
        assert port.written.count(b"\x1f\x30T1") == before + 1


class TestElectroDriver:
    def test_init_frame_sent_on_open(self, monkeypatch):
        d, port = make_electro(monkeypatch)
        assert port.written == b"c\r"

    def test_racing_streams_running_frames(self, monkeypatch):
        d, port = make_electro(monkeypatch)
        gun = datetime.now() - timedelta(seconds=83.45)
        delay = d.drive(gun, lambda s: False)
        assert delay == 0.1
        assert b"c0123" in port.written  # 1:23.x running frame

    def test_idle_shows_time_of_day(self, monkeypatch):
        d, port = make_electro(monkeypatch)
        before = datetime.now().strftime("%H%M%S")
        delay = d.drive(None, lambda s: False)
        after = datetime.now().strftime("%H%M%S")
        assert delay == 0.2
        assert port.written in (b"c\r" + electro_frame(before),
                                b"c\r" + electro_frame(after))

    def test_show_dedups_identical_payloads(self, monkeypatch):
        d, port = make_electro(monkeypatch)
        d._show("123456")
        d._show("123456")
        assert port.written == b"c\r" + electro_frame("123456")


class TestGunBroadcast:
    def test_format_parse_roundtrip(self):
        epoch = 1757280000.25
        msg = parse_management_message(format_gun(epoch))
        assert isinstance(msg, ManagementMessage)
        assert msg.msg_type == MessageType.GUN
        assert msg.reader_id == "ALL"
        assert abs(msg.fields["epoch"] - epoch) < 0.01

    def test_zero_epoch_means_no_race(self):
        msg = parse_management_message(format_gun(0.0))
        assert msg.fields["epoch"] == 0.0

    def test_agent_handles_gun_before_address_filter(self):
        from rfid_finishlynx.remote_agent import RemoteAgent

        class DummyApp:
            class config:
                class lora:
                    reader_id = "LAP1"
            remote_gun = "unset"

            def set_remote_gun(self, epoch, hour_format=None, sent_at=None):
                self.remote_gun = epoch

        app = DummyApp()
        agent = RemoteAgent.__new__(RemoteAgent)
        agent._app = app
        agent.on_receive(format_gun(1757280000.0))
        assert app.remote_gun == 1757280000.0
        agent.on_receive(format_gun(0.0))
        assert app.remote_gun == 0.0
