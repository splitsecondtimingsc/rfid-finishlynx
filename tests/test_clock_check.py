"""2026-10-04: the clock check - PING/PONG over the radio.

- PING and PONG are the same length, parse back, and their t1 / t3 are
  stamped as the frame goes to the radio (in the transport's clock);
- a PING/PONG never shares a write with other queued messages;
- console + station over a simulated link with a known read-clock error
  and buffering on some frames: the round trip and the error come out
  right, and the suggested air latency cancels the error;
- the station takes air_latency_sec over the radio and re-learns;
- no clock check while a race is running; the bench report's numbers.

Hardware-free: python -m unittest tests.test_clock_check
"""
import importlib.util
import os
import threading
import time
import unittest
from types import SimpleNamespace

from rfid_finishlynx.app import Application
from rfid_finishlynx.config import LoRaConfig
from rfid_finishlynx.remote_agent import RemoteAgent
from rfid_finishlynx.remote_manager import RemoteManager
from rfid_finishlynx.remote_protocol import (
    MessageType, clock_check, format_cfgs, format_gun, format_ping, format_pong,
    parse_management_message, restamp_line,
)
from rfid_finishlynx.transport import LoRaTransport, parse_any_lora_message

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestFrames(unittest.TestCase):

    def test_same_length_and_parse(self):
        ping = format_ping("CHUTE", 7)
        pong = format_pong("CHUTE", 7, 1791136297.493, 1791136297.9, 1791136299.1, 0.3)
        self.assertEqual(len(ping), len(pong))
        m = parse_management_message(pong)
        self.assertEqual((m.msg_type, m.reader_id, m.seq), (MessageType.PONG, "CHUTE", 7))
        self.assertEqual((m.fields["t2"], m.fields["air"]), (1791136297.9, 0.3))
        self.assertEqual(parse_management_message(ping).msg_type, MessageType.PING)

    def test_restamp(self):
        self.assertEqual(parse_management_message(restamp_line(format_ping("S1", 1), 12.5)).fields["t1"], 12.5)
        pong = restamp_line(format_pong("S1", 1, 1.0, 2.0, 0.0, 0.3), 3.25)
        self.assertEqual(parse_management_message(pong).fields["t3"], 3.25)
        self.assertEqual(parse_management_message(pong).fields["t2"], 2.0)
        gun = restamp_line(format_gun(1000.0, "12", sent_at=1.0), 1004.25)
        self.assertEqual(parse_management_message(gun).fields["sent_at"], 1004.25)
        other = b"$READ,S1,123,10:00:00.000*00\n"
        self.assertEqual(restamp_line(other, 5.0), other)

    def test_summary_uses_fastest_round_trips(self):
        # station clock 0.05 s ahead, 0.1 s each way; two frames held 0.4 s one way
        samples = []
        for i, (fwd, back) in enumerate([(0.1, 0.1)] * 6 + [(0.5, 0.1), (0.1, 0.5)]):
            t1 = 100.0 + 10 * i
            t2 = t1 + fwd + 0.05
            t3 = t2 + 1.5
            samples.append((t1, t2, t3, t3 - 0.05 + back))
        r = clock_check(samples)
        self.assertEqual(r["replies"], 8)
        self.assertAlmostEqual(r["delay_min"], 0.2, places=3)
        self.assertAlmostEqual(r["delay_max"], 0.6, places=3)
        self.assertAlmostEqual(r["offset"], 0.05, places=3)
        self.assertEqual(clock_check([]), {"replies": 0})


class FakeStream:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class TestTransport(unittest.TestCase):

    def test_timing_frames_travel_alone(self):
        t = LoRaTransport(LoRaConfig())
        t._outbox.extend(b"$READ,a*1\n$ANNOUNCE,b*2\n$PONG,x*3\n$READ,c*4\n")
        self.assertEqual(t._take_from_outbox(), b"$READ,a*1\n$ANNOUNCE,b*2\n")
        self.assertEqual(t._take_from_outbox(), b"$PONG,x*3\n")
        self.assertEqual(t._take_from_outbox(), b"$READ,c*4\n")

    def test_written_ping_carries_the_transport_clock(self):
        t = LoRaTransport(LoRaConfig())
        t._serial, t._connected, t.clock = FakeStream(), True, lambda: 42.0
        self.assertTrue(t._write(format_ping("S1", 3)))
        self.assertEqual(parse_management_message(t._serial.written[0]).fields["t1"], 42.0)


class Link:
    """One radio hop: stamps timing frames as they 'leave', delivers later."""

    def __init__(self, delay):
        self.clock = time.time
        self.delay = delay
        self.deliver = None
        self.sent = 0

    def send(self, data):
        if data.startswith((b"$PING,", b"$PONG,")):
            data = restamp_line(data, self.clock())
        self.sent += 1
        timer = threading.Timer(self.delay(self.sent), self.deliver, [data])
        timer.daemon = True
        timer.start()
        return True


def station_app(read_error):
    lora = SimpleNamespace(reader_id="S1", air_latency_sec=0.3, announce_interval_sec=3600)
    # corrected clock = time.time() - remote_clock_offset = time.time() + read_error
    return SimpleNamespace(config=SimpleNamespace(lora=lora), reader=None, _running=True,
                           remote_clock_offset=-read_error, _remote_clock_samples=[(1.0, 2.0)],
                           save_config=lambda: None)


class TestEndToEnd(unittest.TestCase):

    def run_check(self, read_error, delay, count=6, timeout=None):
        to_station = Link(delay)
        to_console = Link(delay)
        mgr = RemoteManager(to_station, LoRaConfig(pre_registered_units=[]))
        if timeout:
            mgr.PING_REPLY_TIMEOUT_SEC = timeout
        app = station_app(read_error)
        agent = RemoteAgent(app, to_console)
        to_station.deliver = agent.on_receive
        to_console.deliver = lambda d: mgr.on_message(parse_any_lora_message(d))
        mgr.start()
        agent.start()                      # ANNOUNCE makes the unit known
        deadline = time.time() + 5
        while not mgr.get_units() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(mgr.start_clock_check("S1", count)["success"])
        self.assertFalse(mgr.start_clock_check("S1", count)["success"])   # one at a time
        deadline = time.time() + 30
        while mgr.get_clock_check("S1")["running"] and time.time() < deadline:
            time.sleep(0.02)
        agent.stop()
        mgr.stop()
        return mgr.get_clock_check("S1"), app

    def test_round_trip_and_read_error(self):
        # 40 ms each way; every third frame held an extra 300 ms in a buffer
        r, _ = self.run_check(0.05, lambda n: 0.04 + (0.3 if n % 3 == 0 else 0.0))
        self.assertEqual((r["sent"], r["replies"]), (6, 6))
        self.assertAlmostEqual(r["delay_min"], 0.08, delta=0.02)
        self.assertAlmostEqual(r["offset"], 0.05, delta=0.015)
        self.assertAlmostEqual(r["air_suggested"], 0.25, delta=0.02)

    def test_no_replies(self):
        r, _ = self.run_check(0.0, lambda n: 0.01 if n == 1 else 60.0, count=2, timeout=0.3)
        self.assertEqual((r["sent"], r["replies"], r["running"]), (2, 0, False))
        self.assertNotIn("air_suggested", r)

    def test_station_takes_air_latency_over_the_radio(self):
        app = station_app(0.0)
        link = Link(lambda n: 0.0)
        acks = []
        link.deliver = acks.append
        agent = RemoteAgent(app, link)
        agent.on_receive(format_cfgs("S1", 9, "lora", {"air_latency_sec": 0.21}))
        self.assertEqual(app.config.lora.air_latency_sec, 0.21)
        self.assertEqual(app._remote_clock_samples, [])
        agent.on_receive(format_cfgs("S1", 10, "lora", {"air_latency_sec": 9}))
        self.assertEqual(app.config.lora.air_latency_sec, 0.21)
        time.sleep(0.05)
        self.assertIn(b",OK", acks[0])
        self.assertIn(b"set_failed", acks[1])


class TestGuards(unittest.TestCase):

    def test_not_while_racing(self):
        started = []
        fake = SimpleNamespace(
            remote_manager=SimpleNamespace(start_clock_check=lambda rid, n: started.append(rid) or {"success": True}),
            race_manager=SimpleNamespace(races={1: SimpleNamespace(status="racing")}))
        self.assertFalse(Application.start_clock_check(fake, "S1")["success"])
        fake.race_manager.races[1].status = "done"
        self.assertTrue(Application.start_clock_check(fake, "S1")["success"])
        self.assertEqual(started, ["S1"])

    def test_bench_report(self):
        spec = importlib.util.spec_from_file_location("test_lora_link", os.path.join(ROOT, "test_lora_link.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        lines = mod.bench_report({("GUN", "A->B"): [0.21, 0.19], ("PING", "A->B"): [0.31],
                                  ("PING", "B->A"): [0.29]}, {"GUN": 52, "PING": 72}, 2.4)
        text = "\n".join(lines)
        self.assertIn("52 bytes = 173 ms", text)
        self.assertIn("lora.air_latency_sec for the stations: 0.19", text)
        self.assertIn("+20 ms", text)
        self.assertIn("10 ms off", text)


if __name__ == "__main__":
    unittest.main()
