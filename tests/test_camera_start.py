"""Camera start-sensor decoding, checked against bytes from the 2026-09-12
capture (etherlynx/captures/06-start-sensor.pcapng)."""

import struct
import unittest
from datetime import datetime

from rfid_finishlynx.camera_start import (
    START_CODE, CameraEvent, CameraStartListener, ReplyDecoder,
)

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


def with_timebase(payload: bytes, tb: int) -> bytes:
    b = bytearray(payload)
    struct.pack_into("<I", b, 24, tb)
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
