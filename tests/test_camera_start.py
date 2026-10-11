"""Camera start-sensor decoding, checked against bytes from the 2026-09-12
capture (etherlynx/captures/06-start-sensor.pcapng)."""

import os
import struct
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from rfid_finishlynx import camera_start
from rfid_finishlynx.camera_start import (
    START_CODE, CameraEvent, CameraStartListener, ReplyDecoder, normalize_camera_host,
)
from rfid_finishlynx.config import Config, load_config

BASE = 1_789_216_000.0   # Windows cannot timestamp() dates before 1970 local

# A quiet status reply and the one carrying the first start, verbatim.
QUIET = bytes.fromhex(
    "948d96b4c08700000100008015000000d457000016fc0000b3ddaf12"
    "0000000000000000000000000000000000000000ddfb00001b000080"
    "00000000000400040000000000040004000000000000000000040000"
    "00000000000400000000000000000000")
START = bytes.fromhex(
    "948d96b4c38700000100008015000000e357000016fc0000" + "0000b012"   # placeholder timebase
    + "00004011ac49b0120000000000000001" + "00000000"
    + "ddfb00001b00008000000000000400040000000000040004000000000000000000040000"
    + "00000000000400000000000000000000")


# The 48-byte hardware-info reply to command 0x1a that FinishLynx asks for on
# every connect: same magic, but no status words. Layout as captured (serial,
# RAM size, reversed MAC); the MAC is made up here. Its MAC bytes sit where a
# status reply has the event word, so the old decoder read a bogus event
# (code 0x0f17 on the real camera) and a garbage clock sample (2026-10-11).
HWINFO = bytes.fromhex(
    "948d96b40100000001000080080000001" "6fc000000040000f33f6401ccbbaa0f"
    "19000000400600001506142011000000")


def with_timebase(payload: bytes, tb: int) -> bytes:
    b = bytearray(payload)
    struct.pack_into("<I", b, 24, tb)
    return bytes(b)


def status(tb: int, word5: int = 0, ev_ts: int = 0) -> bytes:
    """A 100-byte status reply at camera time tb, optionally carrying an event
    (word 5 = event type at offset 28, word 6 = its timestamp at offset 32)."""
    b = bytearray(with_timebase(QUIET, tb))
    struct.pack_into("<II", b, 28, word5, ev_ts)
    return bytes(b)


class TestReplyDecoder(unittest.TestCase):

    def test_quiet_reply_yields_no_event_but_updates_offset(self):
        dec = ReplyDecoder()
        self.assertIsNone(dec.feed(1000.0, QUIET))
        self.assertEqual(dec.replies, 1)
        self.assertAlmostEqual(dec.offset, 1000.0 - 0x12afddb3 / 10000.0, places=6)

    def test_start_event_wall_time_uses_camera_timestamp(self):
        dec = ReplyDecoder()
        # Ten quiet polls, 160 ms apart, timebase advancing 1600 ticks each.
        tb0 = 0x12b00000
        for i in range(10):
            dec.feed(BASE + 0.16 * i, with_timebase(QUIET, tb0 + 1600 * i))
        # Start stamped 1.517 s before the reply that reports it.
        ev_ts = tb0 + 1600 * 10 - 15170
        payload = bytearray(with_timebase(START, tb0 + 1600 * 10))
        struct.pack_into("<I", payload, 32, ev_ts)
        ev = dec.feed(BASE + 1.6, bytes(payload))
        self.assertIsInstance(ev, CameraEvent)
        self.assertEqual(ev.code, START_CODE)
        self.assertEqual(ev.flag, 1)
        self.assertAlmostEqual(ev.wall.timestamp(), BASE + 1.6 - 1.517, delta=0.002)

    def test_non_reply_ignored(self):
        dec = ReplyDecoder()
        self.assertIsNone(dec.feed(1.0, b"Kirk" + b"\x00" * 60))
        self.assertIsNone(dec.feed(1.0, QUIET[:20]))
        self.assertEqual(dec.replies, 0)

    def test_other_input_code_is_reported_but_not_a_start(self):
        payload = bytearray(with_timebase(QUIET, 0x12b57c60))
        struct.pack_into("<H", payload, 30, 0x120b)
        struct.pack_into("<I", payload, 32, 0x12b57c4b)
        ev = ReplyDecoder().feed(3000.0, bytes(payload))
        self.assertEqual(ev.code, 0x120b)
        self.assertFalse(ev.as_dict()["is_start"])


class TestStatusRepliesOnly(unittest.TestCase):
    """Bug 2026-10-11: anything with the magic and >= 44 bytes was decoded."""

    def test_fixtures_are_the_real_sizes(self):
        self.assertEqual((len(QUIET), len(START), len(HWINFO)), (100, 100, 48))

    def test_hardware_info_reply_is_no_event_and_no_clock_sample(self):
        dec = ReplyDecoder()
        self.assertIsNone(dec.feed(1000.0, HWINFO))
        self.assertEqual(dec.replies, 0)
        self.assertIsNone(dec.offset)

    def test_longer_reply_with_the_magic_is_ignored(self):
        dec = ReplyDecoder()
        self.assertIsNone(dec.feed(1000.0, START + bytes(1410)))     # image-block sized
        self.assertEqual(dec.replies, 0)

    def command(self, host):
        lst = CameraStartListener(camera_host=host)
        with mock.patch.object(camera_start, "find_tshark", return_value="tshark"), \
                mock.patch.object(camera_start, "pick_interfaces", return_value=["eth0"]):
            cmd = lst._command()
        return cmd[cmd.index("-f") + 1]

    def test_capture_filter_keeps_only_100_byte_replies(self):
        # udp[4:2] is the UDP length field: 100-byte payload + 8-byte header
        self.assertEqual(self.command("auto"), "udp src port 41925 and udp[4:2] == 108")
        self.assertEqual(self.command("10.0.0.9"),
                         "udp src port 41925 and src host 10.0.0.9 and udp[4:2] == 108")


class TestEventTypes(unittest.TestCase):
    """Word 5 is the event type (firmware, 2026-10-11): 1 wired start,
    2..4 photo-eye inputs, 0x1000_0000 | code from RadioLynx (high byte 0x11
    start impulse, 0x12 transmitter status)."""

    def event(self, word5):
        dec = ReplyDecoder()
        tb = 0x12b00000
        for i in range(5):
            dec.feed(BASE + 0.16 * i, status(tb + 1600 * i))
        return dec.feed(BASE + 0.8, status(tb + 8000, word5, tb + 6000))

    def test_wired_start_is_a_gun(self):
        ev = self.event(1)
        self.assertTrue(ev.is_start)
        self.assertEqual(ev.kind, "wired start")
        self.assertAlmostEqual(ev.wall.timestamp(), BASE + 0.6, delta=0.002)
        self.assertTrue(ev.as_dict()["is_start"])

    def test_radiolynx_start_impulse_is_a_gun(self):
        ev = self.event(0x11400000)
        self.assertEqual((ev.code, ev.is_start, ev.kind), (START_CODE, True, "RadioLynx start"))
        self.assertEqual(ev.as_dict()["code"], "0x1140")
        self.assertTrue(self.event(0x11410000).is_start)

    def test_radiolynx_status_and_photo_eyes_are_not_guns(self):
        for word5, kind in ((0x120b0000, "RadioLynx status"), (2, "photo-eye"),
                            (3, "photo-eye"), (4, "photo-eye")):
            ev = self.event(word5)
            self.assertEqual((ev.kind, ev.is_start), (kind, False), hex(word5))

    def test_code_alone_still_classifies(self):
        now = datetime.now()
        self.assertTrue(CameraEvent(0x1140, 0, now, 0.0, 1).is_start)
        self.assertTrue(CameraEvent(1, 0, now, 0.0, 0).is_start)
        self.assertFalse(CameraEvent(0x120b, 0, now, 0.0, 0).is_start)


class TestDuplicatesAndAge(unittest.TestCase):

    def test_cached_snapshot_does_not_give_a_second_gun(self):
        dec = ReplyDecoder()
        tb = 0x12b00000
        dec.feed(BASE, status(tb))
        first = dec.feed(BASE + 0.16, status(tb + 1600, 0x11400000, tb + 1000))
        again = dec.feed(BASE + 0.32, status(tb + 1600, 0x11400000, tb + 1000))   # same token
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        # a later start with its own timestamp still counts
        self.assertIsNotNone(dec.feed(BASE + 5.0, status(tb + 50000, 0x11400000, tb + 49000)))

    def test_old_event_uses_the_camera_clock_and_warns(self):
        dec = ReplyDecoder()
        tb = 0x12b00000
        for i in range(5):
            dec.feed(BASE + 0.16 * i, status(tb + 1600 * i))
        with self.assertLogs("rfid_finishlynx.camera_start", level="WARNING") as cm:
            ev = dec.feed(BASE + 0.8, status(tb + 8000, 1, tb + 8000 - 900_000))   # 90 s old
        self.assertAlmostEqual(ev.wall.timestamp(), BASE + 0.8 - 90.0, delta=0.002)
        self.assertIn("90", cm.output[0])


class TestClockResync(unittest.TestCase):

    def test_camera_clock_jumping_back_resets_the_offset(self):
        dec = ReplyDecoder()
        tb = 0x12b00000
        for i in range(40):
            dec.feed(BASE + 0.16 * i, status(tb + 1600 * i))
        # Camera power-cycled, FinishLynx re-booted it: its clock restarts near 0.
        t0 = BASE + 120.0
        for i in range(3):
            dec.feed(t0 + 0.16 * i, status(50_000 + 1600 * i))
        self.assertAlmostEqual(dec.offset, t0 - 5.0, places=3)
        ev = dec.feed(t0 + 0.48, status(50_000 + 4800, 1, 50_000 + 3000))
        self.assertAlmostEqual(ev.wall.timestamp(), t0 + 0.3, delta=0.002)


class TestCameraHost(unittest.TestCase):

    def test_auto_values(self):
        for v in ("", "auto", " AUTO ", None, "192.168.0.201"):
            self.assertEqual(normalize_camera_host(v), "auto", repr(v))
        self.assertEqual(normalize_camera_host(" 192.168.0.67 "), "192.168.0.67")

    def test_config_default_is_auto_and_old_default_counts_as_auto(self):
        self.assertEqual(Config().camera_start.camera_host, "auto")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            for value, want in (("192.168.0.201", "auto"), ("192.168.0.67", "192.168.0.67"), ("", "auto")):
                with open(path, "w") as f:
                    f.write(f"camera_start:\n  camera_host: '{value}'\n")
                self.assertEqual(load_config(path).camera_start.camera_host, want, value)

    def test_auto_learns_the_camera_from_the_first_status_reply(self):
        lst = CameraStartListener(camera_host="auto")
        tb = 0x12b00000
        lst._handle_line(f"{BASE}\t{HWINFO.hex()}\t192.168.0.99\n")       # not a status reply
        self.assertIsNone(lst.camera_ip)
        lst._handle_line(f"{BASE + 0.1}\t{status(tb).hex()}\t192.168.0.67\n")
        self.assertEqual(lst.camera_ip, "192.168.0.67")
        st = lst.status()
        self.assertEqual(st["camera_ip"], "192.168.0.67")
        self.assertIn("192.168.0.67", st["camera"])
        # a second camera on the net is not mixed into this one's clock
        lst._handle_line(f"{BASE + 0.2}\t{status(5).hex()}\t192.168.0.69\n")
        self.assertEqual((lst.camera_ip, lst.decoder.replies), ("192.168.0.67", 1))

    def test_explicit_host_is_kept(self):
        lst = CameraStartListener(camera_host="192.168.0.67")
        self.assertEqual(lst.camera_ip, "192.168.0.67")
        self.assertIn("192.168.0.67", lst.status()["camera"])


class FakeProc:
    """Stands in for the tshark subprocess: yields canned field lines."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stderr = type("E", (), {"read": staticmethod(lambda: "")})()
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self._alive = False


class TestListenerParsing(unittest.TestCase):

    def test_lines_from_tshark_become_events(self):
        got = []
        tb = 0x12b00000
        lines = [f"{BASE + 0.16 * i}\t{with_timebase(QUIET, tb + 1600 * i).hex()}\n" for i in range(5)]
        payload = bytearray(with_timebase(START, tb + 8000))
        struct.pack_into("<I", payload, 32, tb + 8000 - 2000)
        lines.append(f"{BASE + 0.8}\t{bytes(payload).hex()}\n")
        lst = CameraStartListener(on_event=got.append, popen=lambda *a, **k: FakeProc(lines))
        for line in lines:
            lst._handle_line(line)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].code, START_CODE)
        st = lst.status()
        self.assertEqual((st["replies"], st["starts"]), (6, 1))
        self.assertTrue(st["last_start"].startswith(datetime.fromtimestamp(BASE + 0.6).isoformat()[:16]))


if __name__ == "__main__":
    unittest.main()
