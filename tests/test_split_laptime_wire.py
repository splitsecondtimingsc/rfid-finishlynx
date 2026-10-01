"""What a split looks like on the LapTime wire to FinishLynx.

A real loopback socket stands in for FinishLynx's LapTime listener and
captures the bytes FinishLynxOutput sends, frame by frame. The point these
tests nail down: an S packet carries time, id and event/round/heat - and
nothing that says WHICH timing point heard the read. A Mile 1 read and a
finish read of the same bib are the same packet with a different time, so
over one LapTime connection FinishLynx can only place splits by arrival
order (its lap slots). Anything that must reach a specific split column
has to be told apart some other way (one LapTime device per point - see
bench/split_lab.py laptime --ports - or the .lif).
Hardware-free: python -m unittest tests.test_split_laptime_wire
"""
import socket
import threading
import time
import unittest
from datetime import datetime

from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config, FinishLynxConfig
from rfid_finishlynx.finishlynx import FinishLynxOutput
from rfid_finishlynx.rfid_reader import TagRead

SOT_SYNC, SOT_DATA, EOT = 0x01, 0x02, b"\r\n"


class LapTimeListener:
    """Accepts one connection and records everything received."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.data = b""
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        conn, _ = self.srv.accept()
        conn.settimeout(2.0)
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                self.data += chunk

    def frames(self, timeout=3.0):
        self._t.join(timeout)
        self.srv.close()
        return [f + EOT for f in self.data.split(EOT) if f]


def data_fields(frame):
    """b'\\x02S,09:06:00.000,101,2,1,1\\r\\n' -> ['S', '09:06:00.000', '101', '2', '1', '1']"""
    return frame[1:-2].decode("ascii").split(",")


def send(reads, sync_mode="none"):
    """reads: [(identifier, datetime, (event, round, heat) | None)] -> frames"""
    lynx = LapTimeListener()
    out = FinishLynxOutput(FinishLynxConfig(host="127.0.0.1", port=lynx.port, sync_mode=sync_mode))
    assert out.connect()
    for ident, ts, erh in reads:
        e, r, h = erh or (None, None, None)
        assert out.send_split(ident, ts, e, r, h)
    time.sleep(0.2)
    out.disconnect()
    return lynx.frames()


class TestSplitPacket(unittest.TestCase):

    def test_exact_bytes(self):
        frames = send([("101", datetime(2026, 9, 20, 9, 6, 0, 123000), (2, 1, 1))])
        self.assertEqual(frames, [b"\x02S,09:06:00.123,101,2,1,1\r\n"])

    def test_without_event_info(self):
        frames = send([("101", datetime(2026, 9, 20, 9, 6, 0), None)])
        self.assertEqual(frames, [b"\x02S,09:06:00.000,101\r\n"])

    def test_milliseconds_are_truncated_not_rounded(self):
        frames = send([("7", datetime(2026, 9, 20, 9, 6, 0, 999900), None)])
        self.assertEqual(data_fields(frames[0])[1], "09:06:00.999")

    def test_each_read_is_its_own_frame(self):
        frames = send([(str(b), datetime(2026, 9, 20, 9, 6, b), (2, 1, 1)) for b in range(1, 21)])
        self.assertEqual(len(frames), 20)
        self.assertTrue(all(f[0] == SOT_DATA and f.endswith(EOT) for f in frames))

    def test_a_split_and_a_finish_are_indistinguishable(self):
        """Mile 1 at 09:06 and the finish at 09:18 for bib 101: identical
        packets apart from the time. FinishLynx gets no point identity."""
        mile1, finish = send([("101", datetime(2026, 9, 20, 9, 6, 0), (2, 1, 1)),
                              ("101", datetime(2026, 9, 20, 9, 18, 0), (2, 1, 1))])
        a, b = data_fields(mile1), data_fields(finish)
        self.assertEqual(len(a), len(b))
        self.assertEqual([x for i, x in enumerate(a) if i != 1], [x for i, x in enumerate(b) if i != 1])


class TestSyncPacket(unittest.TestCase):

    def test_time_packets_are_marked_for_clock_sync(self):
        out = FinishLynxOutput(FinishLynxConfig())
        t = out._build_packet("T", "09:00:00.000")
        s = out._build_packet("S", "09:00:00.000", "101", 2, 1, 1)
        self.assertEqual(t, b"\x01T,09:00:00.000\r\n")
        self.assertEqual(s[0], SOT_DATA)

    def test_internal_sync_streams_time_packets(self):
        lynx = LapTimeListener()
        out = FinishLynxOutput(FinishLynxConfig(host="127.0.0.1", port=lynx.port, sync_mode="internal"))
        self.assertTrue(out.connect())
        time.sleep(0.35)
        out.disconnect()
        sync = [f for f in lynx.frames() if f[0] == SOT_SYNC]
        self.assertGreaterEqual(len(sync), 4)                     # 20 Hz
        self.assertTrue(all(data_fields(f)[0] == "T" for f in sync))


class RecordingLynx:
    def __init__(self):
        self.is_connected = True
        self.sent = []

    def send_split(self, identifier, ts, e=None, r=None, h=None):
        self.sent.append((identifier, ts, e, r, h))
        return True


def feed_app(feed):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.finishlynx.feed = feed
    app.finishlynx = RecordingLynx()
    app.race_manager = None
    app.operation_mode = "current_reads"
    app._reads_lock = threading.Lock()
    app._antenna_reads = {}
    app._pending_lock = threading.Lock()
    app._pending_sends = []
    app._on_tag_read_callback = None
    app._get_finishlynx_identifier = lambda bib, ts: (bib, True, {})
    app._track_lap = lambda bib, ts, rid, manual=False: {"time": "", "lap_number": 1, "position": 1}
    app._get_current_event_info = lambda: (2, 1, 1)
    return app


def tag(reader_id, minute, bib="101"):
    return TagRead(bib=bib, timestamp=datetime(2026, 9, 20, 9, minute, 0), epc="", rssi=0,
                   antenna=1, reader_id=reader_id)


class TestRoutedSplits(unittest.TestCase):
    """feed=all through the application: what FinishLynx actually receives."""

    def test_split_stations_reach_laptime_only_with_feed_all(self):
        app = feed_app("finish")
        for rid, m in (("S1", 6), ("S2", 12), ("FINISH", 18)):
            app._on_tag_read(tag(rid, m))
        self.assertEqual([s[1].minute for s in app.finishlynx.sent], [18])

    def test_feed_all_sends_every_point_with_the_same_identity(self):
        app = feed_app("all")
        for rid, m in (("S1", 6), ("S2", 12), ("FINISH", 18)):
            app._on_tag_read(tag(rid, m))
        sent = app.finishlynx.sent
        self.assertEqual([s[1].minute for s in sent], [6, 12, 18])
        self.assertEqual({(s[0], s[2], s[3], s[4]) for s in sent}, {("101", 2, 1, 1)})

    def test_feed_all_order_is_arrival_order(self):
        """A LoRa batch that lands after the finish puts Mile 1 LAST on the
        wire - into the final-time slot if FinishLynx fills by arrival."""
        app = feed_app("all")
        app._on_tag_read(tag("FINISH", 18))
        app._on_tag_read(tag("S1", 6))
        self.assertEqual([s[1].minute for s in app.finishlynx.sent], [18, 6])


if __name__ == "__main__":
    unittest.main()
