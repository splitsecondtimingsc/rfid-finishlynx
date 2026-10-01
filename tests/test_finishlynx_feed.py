"""finishlynx.feed: which reads reach FinishLynx's LapTime plug-in.

LapTime packets carry no split number and FinishLynx fills each athlete's
lap slots in arrival order, so on 2026-09-17 split-station reads became
final times whenever an event's Total Laps was short. Default "finish"
sends only the finish-line reader's reads. Hardware-free:
python -m unittest tests.test_finishlynx_feed
"""
import json
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config
from rfid_finishlynx.rfid_reader import TagRead


class FakeLynx:
    def __init__(self):
        self.is_connected = True
        self.sent = []

    def send_split(self, identifier, ts, e=None, r=None, h=None):
        self.sent.append(identifier)
        return True


def make_app(feed="finish"):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.finishlynx.feed = feed
    app.finishlynx = FakeLynx()
    app.race_manager = None
    app.operation_mode = "current_reads"
    app._reads_lock = threading.Lock()
    app._antenna_reads = {}
    app._pending_lock = threading.Lock()
    app._pending_sends = []
    app._on_tag_read_callback = None
    app._get_finishlynx_identifier = lambda bib, ts: (bib, True, {})
    app._track_lap = lambda bib, ts, rid, manual=False: {"time": "10:00:00.000", "lap_number": 1, "position": 1}
    app._get_current_event_info = lambda: (None, None, None)
    return app


def read(reader_id, bib="101"):
    return TagRead(bib=bib, timestamp=datetime(2026, 9, 17, 10, 0, 0), epc="", rssi=0,
                   antenna=1, reader_id=reader_id)


class TestFeed(unittest.TestCase):

    def test_default_is_finish_only(self):
        self.assertEqual(Config().finishlynx.feed, "finish")

    def test_finish_only_drops_split_station_reads(self):
        app = make_app("finish")
        app._on_tag_read(read("S1"))
        app._on_tag_read(read("S2", "102"))
        app._on_tag_read(read("FINISH", "103"))
        self.assertEqual(app.finishlynx.sent, ["103"])

    def test_all_sends_everything(self):
        app = make_app("all")
        app._on_tag_read(read("S1"))
        app._on_tag_read(read("FINISH", "103"))
        self.assertEqual(app.finishlynx.sent, ["101", "103"])

    def test_split_reads_are_not_queued_while_disconnected_either(self):
        app = make_app("finish")
        app.finishlynx.is_connected = False
        app._on_tag_read(read("S1"))
        app._on_tag_read(read("FINISH", "103"))
        self.assertEqual([p["bib"] for p in app._pending_sends], ["103"])

    def test_finishlynx_mode_path_honours_it_too(self):
        app = make_app("finish")
        app._race_reads_lock = threading.Lock()
        app._race_reads = []
        app._send_and_track_finishlynx_read(read("S1"), "101", None)
        app._send_and_track_finishlynx_read(read("FINISH", "103"), "103", None)
        self.assertEqual(app.finishlynx.sent, ["103"])

    def test_configured_finish_readers_count(self):
        app = make_app("finish")
        app.config.races.finish_readers = ["LINE"]
        app._on_tag_read(read("LINE", "7"))
        self.assertEqual(app.finishlynx.sent, ["7"])


class FakePanes:
    """Race manager stand-in: ingest answers with a canned verdict per bib."""

    def __init__(self, verdicts):
        self.verdicts = verdicts          # bib -> (state, reason)
        self.reads = {}
        self.races = {}                   # nothing running: finishes are never judged by race
        self.athlete_lookup = lambda bib: None

    def ingest(self, bib, ts, reader_id, antenna=0):
        state, reason = self.verdicts.get(bib, ("assigned", ""))
        return {"state": state, "race": 1 if state == "assigned" else None,
                "reason": reason, "point": reader_id, "finish_offset": 0.0}

    def is_finish_read(self, read):
        return read.point == "FINISH"


def make_pane_app(feed, verdicts):
    app = make_app(feed)
    app.race_manager = FakePanes(verdicts)
    return app


class TestAllFollowsThePanes(unittest.TestCase):
    """2026-09-26: boys warming up over the Mile 1 mat during a girls' race
    were held by the panes ("no open M race") yet every one reached
    FinishLynx, because feed "all" never asked the panes."""

    def test_held_and_ignored_splits_stay_out(self):
        app = make_pane_app("all", {"612": ("held", "no chute; no open M race"),
                                    "3022": ("ignored", "reader not in use")})
        app._on_tag_read(read("S1", "612"))
        app._on_tag_read(read("S1", "3022"))
        app._on_tag_read(read("S1", "494"))          # a girl: assigned
        self.assertEqual(app.finishlynx.sent, ["494"])

    def test_finish_reads_go_even_when_held(self):
        # FinishLynx is the record for finishes: no pane must never mean no finish
        app = make_pane_app("all", {"860": ("held", "no open M race")})
        app._on_tag_read(read("FINISH", "860"))
        self.assertEqual(app.finishlynx.sent, ["860"])

    def test_finish_feed_is_unchanged(self):
        app = make_pane_app("finish", {})
        app._on_tag_read(read("S1", "494"))          # assigned, but splits never go
        app._on_tag_read(read("FINISH", "860"))
        self.assertEqual(app.finishlynx.sent, ["860"])

    def test_held_split_is_not_queued_while_disconnected(self):
        app = make_pane_app("all", {"612": ("held", "no open M race")})
        app.finishlynx.is_connected = False
        app._on_tag_read(read("S1", "612"))
        app._on_tag_read(read("S1", "494"))
        self.assertEqual([p["bib"] for p in app._pending_sends], ["494"])


class TestRestoredRead(unittest.TestCase):
    """Placing a held read into a race by hand sends it (feed "all") - unless
    the athlete's finish is already there: LapTime fills laps in arrival
    order, so a late split would become the final time."""

    def _app(self, feed="all"):
        from rfid_finishlynx.races import Read
        app = make_pane_app(feed, {})
        mgr = app.race_manager
        mgr.reads[5] = Read(id=5, bib="612", ts=datetime(2026, 9, 26, 8, 50, 43),
                            point="S1", race=1, state="assigned")
        return app, mgr, Read

    def test_restored_split_is_sent(self):
        app, mgr, _ = self._app()
        self.assertEqual(app.send_restored_read(5), "sent to FinishLynx as bib 612")
        self.assertEqual(app.finishlynx.sent, ["612"])

    def test_not_after_the_finish(self):
        app, mgr, Read = self._app()
        mgr.reads[9] = Read(id=9, bib="612", ts=datetime(2026, 9, 26, 9, 10, 0),
                            point="FINISH", race=1, state="assigned")
        note = app.send_restored_read(5)
        self.assertIn("already has a finish", note)
        self.assertEqual(app.finishlynx.sent, [])

    def test_nothing_under_finish_feed_or_for_finishes_or_manual(self):
        app, mgr, Read = self._app("finish")
        self.assertIsNone(app.send_restored_read(5))
        app, mgr, Read = self._app()
        mgr.reads[5].point = "FINISH"
        self.assertIsNone(app.send_restored_read(5))
        mgr.reads[5].point = "S1"
        mgr.reads[5].manual = True
        self.assertIsNone(app.send_restored_read(5))
        mgr.reads[5].manual = False
        mgr.reads[5].state = "held"
        self.assertIsNone(app.send_restored_read(5))
        self.assertIsNone(app.send_restored_read(404))
        self.assertEqual(app.finishlynx.sent, [])

    def test_assign_route_reports_the_note(self):
        calls = []

        class Mgr:
            def assign_read(self, read_id, race_key):
                calls.append((read_id, race_key))

        web._application = SimpleNamespace(config=Config(), race_manager=Mgr(),
                                           send_restored_read=lambda rid: f"sent {rid}")
        c = web.app.test_client()
        r = c.post("/api/reads/5/assign", data=json.dumps({"race": 1}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(calls, [(5, 1)])
        self.assertEqual(r.get_json()["finishlynx"], "sent 5")


class TestConfigApi(unittest.TestCase):

    def test_round_trip(self):
        cfg = Config()
        web._application = SimpleNamespace(config=cfg, race_manager=None, save_config=lambda: None,
                                           is_running=False, finishlynx=None)
        c = web.app.test_client()
        r = c.post("/api/config", data=json.dumps({"finishlynx": {"feed": "all"}}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(cfg.finishlynx.feed, "all")
        self.assertEqual(c.get("/api/config").get_json()["finishlynx"]["feed"], "all")
        r = c.post("/api/config", data=json.dumps({"finishlynx": {"feed": "some"}}), content_type="application/json")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
