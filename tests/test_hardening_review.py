"""Fixes for the 2026-10-04 hardening review (~/rfid-middleware-hardening-review.md),
each checked the way the review reproduced it:

  #4  one LoRa message failing to process no longer ends reception; the ACK
      goes only after every read in it was processed, and a failed read is
      not remembered as seen, so the retransmission delivers it;
  #2  saves overlapping from many threads leave valid JSON with every read;
      a damaged state file falls back to the copy before it;
  #3  a FinishLynx send that finds the link broken is queued, not lost;
  #6  config.yaml survives a save that dies half-way;
  #1  the finish console listens on this PC only by default; another
      site's page cannot post to it; save routes take a plain .lif/.evt
      name, never a path;
  #5  a station sends again what it never saw ACKed before a restart, and
      the finish line, restarted too, still counts each read once;
  #7  an update cut off while installing is rolled back, never confirmed.

python -m unittest tests.test_hardening_review
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import main as main_mod
from rfid_finishlynx import updater, web
from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config, LoRaConfig
from rfid_finishlynx.lora_receiver import LoRaReceiver
from rfid_finishlynx.races import RaceManager
from rfid_finishlynx.transport import (
    JOURNAL_LINE_FMT, LoRaTransport, ReliableReadSender, _format_read_time, format_batch_message,
)
from tests.test_updater import NEW, InstallTestCase, read, release_zip

NOW = datetime.now().replace(microsecond=0)


class FakeLink:
    def __init__(self):
        self.sent = []
        self.config = LoRaConfig()

    def send(self, data):
        self.sent.append(data)
        return True


class TestLoRaAcceptance(unittest.TestCase):           # 4

    def receiver(self, fail_bibs):
        got = []

        def on_message(msg):
            if msg.bib in fail_bibs:
                fail_bibs.discard(msg.bib)        # fails once
                raise RuntimeError("boom")
            got.append(msg.bib)
        rx = LoRaReceiver(LoRaConfig(), on_message=on_message)
        rx._transport = FakeLink()
        return rx, got

    def test_failed_read_is_not_acked_and_comes_through_on_retry(self):
        rx, got = self.receiver({"101"})
        batch = format_batch_message("S1", 7, [("101", NOW), ("102", NOW)])
        with self.assertLogs("rfid_finishlynx.lora_receiver", "ERROR"):
            rx._on_receive(batch)
        self.assertEqual(got, ["102"])
        self.assertEqual(rx._transport.sent, [])             # no ACK yet
        rx._on_receive(batch)                                # the station retries
        self.assertEqual(got, ["102", "101"])                # 102 not counted twice
        self.assertEqual(len(rx._transport.sent), 1)
        self.assertIn(b"ACK,S1,07,OK", rx._transport.sent[0])

    def test_one_bad_message_does_not_end_reception(self):
        heard = []

        def on_receive(line):
            if b"bad" in line:
                raise RuntimeError("boom")
            heard.append(line)

        class Stream:
            chunks = [b"$bad*00\n$good*00\n"]

            def read(self, n):
                time.sleep(0.01)
                return self.chunks.pop(0) if self.chunks else b""

        t = LoRaTransport(LoRaConfig(), on_receive=on_receive)
        t._serial, t._connected, t._running = Stream(), True, True
        th = threading.Thread(target=t._receive_loop, daemon=True)
        with self.assertLogs("rfid_finishlynx.transport", "ERROR"):
            th.start()
            time.sleep(0.2)
        self.assertEqual(heard, [b"$good*00\n"])
        self.assertTrue(th.is_alive())
        t._running = False
        th.join(1)


class TestRaceStateSaves(unittest.TestCase):          # 2

    def test_overlapping_saves_stay_valid(self):
        # the review's reproduction: one save paused mid-write while another runs
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m = RaceManager(athlete_lookup=lambda b: {"gender": "F"}, state_path=path)
            k = m.create_race("Girls", event=1, gender="F")
            m.set_gun(k, NOW)
            real, calls, paused = json.dump, [], threading.Event()

            def slow_dump(obj, f, **kw):
                calls.append(1)
                if len(calls) == 1:
                    paused.set()
                    time.sleep(0.3)          # the other save runs now, unless saves are serialised
                return real(obj, f, **kw)
            with mock.patch("rfid_finishlynx.races.json.dump", slow_dump):
                first = threading.Thread(target=m.ingest, args=("101", NOW + timedelta(minutes=20), "FINISH"))
                first.start()
                paused.wait(2)
                m.ingest("102", NOW + timedelta(minutes=21), "FINISH")
                first.join()
            with open(path) as f:
                self.assertEqual(sorted(r["bib"] for r in json.load(f)["reads"]), ["101", "102"])
            self.assertEqual(len(RaceManager(state_path=path).reads), 2)

    def test_damaged_file_falls_back_to_the_previous_copy(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m = RaceManager(state_path=path)
            m.create_race("Girls", event=1)
            m.create_race("Boys", event=2)
            with open(path, "w") as f:
                f.write('{"order": [1, ')                  # cut off
            with self.assertLogs("rfid_finishlynx.races", "ERROR"):
                n = RaceManager(state_path=path)
            self.assertEqual([r.name for r in n.races.values()], ["Girls"])   # the save before


class TestFinishLynxQueue(unittest.TestCase):         # 3

    def test_send_that_finds_the_link_broken_is_queued(self):
        fl = SimpleNamespace(is_connected=True, send_split=lambda *a: False)
        app = SimpleNamespace(finishlynx=fl, _pending_sends=[], _pending_lock=threading.Lock(),
                              _get_current_event_info=lambda: (3, 1, 2))
        Application._send_or_queue_finishlynx(app, "212", NOW)
        self.assertEqual(app._pending_sends, [{"bib": "212", "timestamp": NOW,
                                               "event": 3, "round": 1, "heat": 2}])
        fl.send_split = lambda *a: True
        Application._send_or_queue_finishlynx(app, "213", NOW)
        self.assertEqual(len(app._pending_sends), 1)


class TestConfigSave(unittest.TestCase):              # 6

    def test_save_cut_off_keeps_the_last_good_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            with mock.patch.dict(os.environ, {"RFID_PREVIEW": "1"}):
                app = Application(Config(), path)
            app.save_config()
            with open(path) as f:
                good = f.read()

            def dies(data, f, **kw):
                f.write("mode: fin")
                raise OSError("power cut")
            app.config.reader.host = "10.9.9.9"
            with mock.patch("rfid_finishlynx.app.yaml.dump", dies), self.assertRaises(OSError):
                app.save_config()
            with open(path) as f:
                self.assertEqual(f.read(), good)


class TestNetworkBoundary(unittest.TestCase):         # 1

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        cfg = Config()
        cfg.event_directory = self.dir
        web._application = SimpleNamespace(config=cfg, race_manager=None, save_config=lambda: None)
        self.addCleanup(setattr, web, "_application", None)
        self.c = web.app.test_client()

    def save(self, filename, **kw):
        return self.c.post("/api/lif/save-adjusted", json={"content": "x", "filename": filename}, **kw)

    def test_save_routes_take_a_plain_lif_name(self):
        for bad in (os.path.join(self.dir, "evil.py"), "../evil.lif", "sub/evil.lif", "evil.py", "..\\evil.lif"):
            self.assertEqual(self.save(bad).status_code, 400, bad)
        r = self.save("race1.lif")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "RFID Events", "race1.lif")))
        r = self.c.post("/api/evt/save", json={"filename": "../x.py", "content": "x", "target_dir": self.dir})
        self.assertEqual(r.status_code, 400)

    def test_another_site_cannot_post(self):
        r = self.save("a.lif", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.save("a.lif", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.save("b.lif", headers={"Origin": "http://localhost"}).status_code, 200)
        self.assertNotEqual(self.c.get("/api/status", headers={"Origin": "http://evil.example"}).status_code, 403)

    def test_finish_line_listens_on_this_pc_only(self):
        for mode, argv, want in (("finish_line", [], "127.0.0.1"), ("remote_lap", [], "0.0.0.0"),
                                 ("finish_line", ["--web-host", "0.0.0.0"], "0.0.0.0")):
            cfg = Config()
            cfg.mode = mode
            with mock.patch.object(sys, "argv", ["run.py"] + argv), \
                    mock.patch.object(main_mod, "load_config", return_value=cfg), \
                    mock.patch.object(main_mod, "find_config_path", return_value="c.yaml"), \
                    mock.patch.object(main_mod, "setup_logging"), \
                    mock.patch.object(main_mod, "run_web_mode") as run:
                main_mod.main()
            self.assertEqual(run.call_args[0][2], want, (mode, argv))


def journal(path, lines):
    with open(path, "w") as f:
        for when, text in lines:
            f.write(f"{when.strftime(JOURNAL_LINE_FMT)} {text}\n")


class TestRestartRecovery(unittest.TestCase):         # 5

    def test_station_resends_what_was_never_acked(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sent.log")
            t = lambda s: _format_read_time(NOW + timedelta(seconds=s))
            journal(path, [
                (NOW - timedelta(hours=7), f"READ bib=900 t={t(-25200)}"),   # too old
                (NOW, f"READ bib=101 t={t(1)}"), (NOW, f"READ bib=102 t={t(2)}"),
                (NOW, f"READ bib=103 t={t(3)}"), (NOW, f"READ bib=101 t={t(9)}"),
                (NOW, f"SENT seq=00 bib=101 t={t(1)}"),
                (NOW, f"ACKED seq=00 bib=101 retries=0 t={t(1)}"),
                (NOW, "ACKED seq=01 bib=103 retries=0"),                     # older format: no time
                (NOW, f"DROP seq=02 bib=102 reason=stop t={t(2)}"),
            ])
            with self.assertLogs("rfid_finishlynx.transport", "WARNING"):
                s = ReliableReadSender(FakeLink(), "S1", journal_path=path)
            self.assertEqual([(b, _format_read_time(ts)) for b, ts in s._queue],
                             [("102", t(2)), ("101", t(9))])
            with open(path) as f:
                self.assertEqual(f.read().count("RESTORED"), 2)
            # restored reads re-journaled as RESTORED, not READ: a later ACK
            # resolves them and a second restart restores nothing more
            s.flush()
            s.handle_ack(0)
            self.assertEqual(ReliableReadSender(FakeLink(), "S1", journal_path=path)._queue, [])

    def test_finish_line_still_counts_each_read_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "received.log")
            got = []
            rx = LoRaReceiver(LoRaConfig(), on_message=lambda m: got.append(m.bib), journal_path=path)
            rx._transport = FakeLink()
            ts = NOW - timedelta(minutes=5)
            rx._on_receive(format_batch_message("S1", 3, [("101", ts)]))
            # the finish line restarts; the station resends what it never saw ACKed
            rx2 = LoRaReceiver(LoRaConfig(), on_message=lambda m: got.append(m.bib), journal_path=path)
            rx2._load_seen()
            rx2._transport = FakeLink()
            rx2._on_receive(format_batch_message("S1", 0, [("101", ts), ("102", ts)]))
            self.assertEqual(got, ["101", "102"])
            self.assertEqual(len(rx2._transport.sent), 1)                     # ACKed


class TestUpdateCutOff(InstallTestCase):              # 7

    def test_failure_while_installing_puts_the_old_version_back(self):
        real = os.replace
        calls = []

        def flaky(src, dst):
            if src.endswith(".update-tmp"):
                calls.append(dst)
                if len(calls) == 3:
                    raise OSError("disk gone")
            return real(src, dst)
        with mock.patch("rfid_finishlynx.updater.os.replace", flaky), \
                self.assertRaises(updater.UpdateError):
            self.apply()
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'old'\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "rfid_finishlynx", "brand_new.py")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "update-pending.json")))

    def test_cut_off_install_is_rolled_back_at_next_launch_and_never_confirmed(self):
        real = os.replace

        def killed(src, dst):
            if src.endswith(".update-tmp") and dst.endswith("main.py"):
                raise SystemExit("power cut")          # nothing after this runs
            return real(src, dst)
        with mock.patch("rfid_finishlynx.updater.os.replace", killed), self.assertRaises(SystemExit):
            self.apply()
        pending = os.path.join(self.root, "update-pending.json")
        with open(pending) as f:
            self.assertEqual(json.load(f)["state"], "applying")
        self.assertFalse(updater.confirm(self.root))
        self.assertTrue(os.path.exists(pending))
        self.guard().guard(self.root)
        self.assertEqual(read(self.root, "VERSION.txt").split()[4], "1111111")
        self.assertFalse(os.path.exists(pending))

    def test_complete_update_is_confirmed_as_before(self):
        self.apply()
        with open(os.path.join(self.root, "update-pending.json")) as f:
            self.assertEqual(json.load(f)["state"], "applied")
        self.assertTrue(updater.confirm(self.root))


if __name__ == "__main__":
    unittest.main()
