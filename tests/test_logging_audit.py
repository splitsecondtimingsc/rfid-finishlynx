"""The log as a support tool (2026-10-01): operator actions, where each read
went, FinishLynx saves, quiet radio retries, a scoreboard feed that no longer
floods the file, daily rotation. python -m unittest tests.test_logging_audit"""

import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import fl_window, transport, web
from rfid_finishlynx.config import Config, LoRaConfig
from rfid_finishlynx.finishlynx import ScoreboardListener
from rfid_finishlynx.main import DailyLogFile
from rfid_finishlynx.races import RaceManager
from tests.test_lif_merge import FL_TEXT
from tests.test_split_lif_contract import GUN, race_with_splits


def setUpModule():
    # the timing PC has a real FinishLynx window open: never let it leak in
    p = mock.patch.object(fl_window, "open_event", lambda: None)
    p.start()
    unittest.addModuleCleanup(p.stop)


class TestOperatorActions(unittest.TestCase):

    def setUp(self):
        self.mgr = RaceManager(state_path=None)
        self.key = self.mgr.create_race("Girls 5K", event=1, gender="F", distance="5000")
        cfg = Config()
        web._application = SimpleNamespace(config=cfg, race_manager=self.mgr, save_config=lambda: None,
                                           on_operator_gun=lambda k: None)
        self.addCleanup(setattr, web, "_application", None)
        self.client = web.app.test_client()

    def test_action_is_logged_with_the_pane_name(self):
        with self.assertLogs("rfid_finishlynx.web", "INFO") as cm:
            self.client.post(f"/api/races/{self.key}/status", json={"status": "checkin"})
        self.assertTrue(any("UI: /api/races/1/status [Girls 5K]" in l and '"checkin"' in l
                            for l in cm.output), cm.output)

    def test_read_actions_name_the_read(self):
        self.mgr.set_gun(self.key, GUN)
        self.mgr.ingest("7", GUN + timedelta(minutes=18), "FINISH")
        rid = next(iter(self.mgr.reads))
        with self.assertLogs("rfid_finishlynx.web", "INFO") as cm:
            self.client.post(f"/api/reads/{rid}/ignore", json={})
        self.assertTrue(any(f"[read {rid}: bib 7 at FINISH" in l for l in cm.output), cm.output)

    def test_support_key_never_reaches_the_log(self):
        with self.assertLogs("rfid_finishlynx.web", "INFO") as cm:
            self.client.post("/api/support/settings", json={"token": "github_pat_SECRET", "site_name": "x"})
        self.assertFalse(any("github_pat_SECRET" in l for l in cm.output))
        self.assertTrue(any("(hidden)" in l for l in cm.output))

    def test_gets_are_not_logged(self):
        with self.assertNoLogs("rfid_finishlynx.web", "INFO"):
            self.client.get("/api/races")


class TestWhereEachReadWent(unittest.TestCase):

    def test_assigned_and_held_reads_say_so(self):
        from tests.test_finish_other_race import finish, meet
        app, mgr, men, women = meet()
        mgr.races[women].roster.add("2001")
        lines = []
        for bib in ("2001", "4242"):                     # on the roster / unknown bib
            r = finish(bib)
            r.antenna = 5
            with self.assertLogs("rfid_finishlynx.app", "INFO") as cm:
                app._on_tag_read(r)
            lines += cm.output
        self.assertTrue(any("Read 2001 at FINISH A5 -> 'Women's 5000 Meters'" in l for l in lines), lines)
        self.assertTrue(any("Read 4242 at FINISH A5 -> held: no chute; unknown bib" in l for l in lines), lines)


class TestFinishLynxSaves(unittest.TestCase):

    def test_save_is_logged_once_with_the_fl_only_bibs(self):
        lynx = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, lynx, True)
        mgr, key = race_with_splits()
        path = os.path.join(lynx, "002-1-01.lif")
        with open(path, "w", newline="") as f:
            f.write(FL_TEXT)
        t = (GUN + timedelta(minutes=19)).timestamp()
        os.utime(path, (t, t))
        with self.assertLogs("rfid_finishlynx.races", "INFO") as cm:
            mgr.write_merged_lif(key, lynx, os.path.join(lynx, "RFID"))
        saved = [l for l in cm.output if "FinishLynx saved 002-1-01.lif" in l]
        self.assertEqual(len(saved), 1, cm.output)
        self.assertIn("only in FinishLynx, no reader finish: 99", saved[0])
        with self.assertNoLogs("rfid_finishlynx.races", "INFO"):
            mgr.write_merged_lif(key, lynx, os.path.join(lynx, "RFID"))   # same file: quiet


class TestQuietRadioRetries(unittest.TestCase):

    def test_one_error_then_debug_then_a_reminder(self):
        t = transport.LoRaTransport(LoRaConfig(port="127.0.0.1:9", send_interval_sec=0))
        with mock.patch.object(transport, "_TcpStream", side_effect=OSError("refused")):
            with self.assertLogs("rfid_finishlynx.transport", "DEBUG") as cm:
                for _ in range(4):
                    t._open()
            levels = [l.split(":")[0] for l in cm.output]
            self.assertEqual(levels, ["ERROR", "DEBUG", "DEBUG", "DEBUG"])
            t._fail_logged -= 601
            with self.assertLogs("rfid_finishlynx.transport", "WARNING") as cm:
                t._open()
            self.assertIn("still unreachable", cm.output[0])
        with mock.patch.object(transport, "_TcpStream", return_value=mock.Mock()):
            with self.assertLogs("rfid_finishlynx.transport", "INFO") as cm:
                t._open()
        self.assertTrue(any("answering again" in l for l in cm.output))


class TestScoreboardFeed(unittest.TestCase):

    def test_running_time_logged_once_a_minute(self):
        sb = ScoreboardListener()
        logger = logging.getLogger("rfid_finishlynx.finishlynx")
        with mock.patch.object(logger, "debug") as dbg:
            for _ in range(50):
                sb._parse_scoreboard_data("\rRunning time:    12:34.5  ")
        self.assertEqual(sum("running time" in str(c) for c in dbg.call_args_list), 1)


class TestDailyLog(unittest.TestCase):

    def test_rotation_failure_keeps_logging(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        h = DailyLogFile(os.path.join(d, "x.log"))
        self.addCleanup(h.close)
        with mock.patch.object(h, "rotate", side_effect=PermissionError("in use")):
            h.doRollover()                               # Windows: someone has the file open
        self.assertIsNotNone(h.stream)
        self.assertGreater(h.rolloverAt, time.time())
        h.emit(logging.LogRecord("t", logging.INFO, "", 0, "still here", None, None))
        h.flush()
        with open(os.path.join(d, "x.log")) as f:
            self.assertIn("still here", f.read())


if __name__ == "__main__":
    unittest.main()
