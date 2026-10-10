"""2026-09-26: what reaches FinishLynx and the merged .lif, and when.

- a finish the panes hold as TOO EARLY (loop-course mat crossed mid-race,
  a tester) is kept out of the LapTime feed - with Total Laps 1 it would
  become the athlete's final time - and goes the moment a pane takes it;
- the Stations panel's Publishing switch: off = reads still heard,
  classified and backed up, nothing sent, no .lif written; on again = the
  .lif catches up, kept-back reads go only when the operator says so;
- a plain-text backup of every read in the RFID folder;
- "Publish without splits" (waive) in the pane's Needs-attention list.

Hardware-free: python -m unittest tests.test_publish_guard
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config
from rfid_finishlynx.races import RaceManager
from rfid_finishlynx.rfid_reader import TagRead

GUN = datetime(2026, 9, 26, 9, 0, 0)
ATHLETES = {
    "101": {"last_name": "Aker", "first_name": "A", "school": "North", "gender": "F"},
    "102": {"last_name": "Bell", "first_name": "B", "school": "South", "gender": "F"},
    "103": {"last_name": "Cole", "first_name": "C", "school": "North", "gender": "F"},
}


class FakeLynx:
    def __init__(self):
        self.is_connected = True
        self.sent = []

    def send_split(self, identifier, ts, e=None, r=None, h=None):
        self.sent.append((identifier, ts))
        return True


def make_mgr(state_path=None):
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE",
                       point_distances={"S1": 1609.0, "S2": 3218.0}, state_path=state_path)


def make_app(feed="finish", mgr=None, hardware=False, event_dir=None):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.finishlynx.feed = feed
    if event_dir:
        app.config.event_directory = event_dir
    app.finishlynx = FakeLynx()
    app.race_manager = mgr
    app.operation_mode = "current_reads"
    app.hardware_allowed = hardware
    app.publishing = True
    app._publish_off_at = None
    app._reads_lock = threading.Lock()
    app._antenna_reads = {}
    app._pending_lock = threading.Lock()
    app._pending_sends = []
    app._on_tag_read_callback = None
    app._get_finishlynx_identifier = lambda bib, ts: (bib, True, {})
    app._track_lap = lambda bib, ts, rid, manual=False: {"time": "10:00:00.000", "lap_number": 1, "position": 1}
    app._get_current_event_info = lambda: (None, None, None)
    app._broadcast_gun = lambda: None
    if mgr is not None:
        mgr.on_change = app._on_races_changed
    return app


def read(reader_id, bib, ts, antenna=1):
    return TagRead(bib=bib, timestamp=ts, epc="", rssi=0, antenna=antenna, reader_id=reader_id)


def racing_5k(mgr):
    k = mgr.create_race("Girls Varsity", event=3, gender="F", distance="5000")
    mgr.set_gun(k, GUN)
    mgr.set_status(k, "racing")
    return k


class TestEarlyFinishGate(unittest.TestCase):

    def test_too_early_finish_is_kept_back_then_sent_when_placed(self):
        mgr = make_mgr()
        app = make_app(mgr=mgr)
        k = racing_5k(mgr)
        # 60 s after the gun nobody has run 5000 m: held as too early
        app._on_tag_read(read("FINISH", "101", GUN + timedelta(seconds=60)))
        self.assertEqual(app.finishlynx.sent, [])
        held = mgr.held_reads()
        self.assertEqual(len(held), 1)
        rid = held[0]["id"]
        self.assertEqual(mgr.reads[rid].unsent, "early")
        # the operator places it anyway (a real finish after all): it goes now
        mgr.assign_read(rid, k)
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])
        self.assertEqual(mgr.reads[rid].unsent, "")
        self.assertFalse(mgr.reads[rid].early)
        # the assign route reports it
        note = app.send_restored_read(rid)
        self.assertIn("sent to FinishLynx", note)
        self.assertEqual(len(app.finishlynx.sent), 1)          # not twice

    def test_plausible_finish_goes_at_once(self):
        mgr = make_mgr()
        app = make_app(mgr=mgr)
        racing_5k(mgr)
        app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20)))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])

    def test_held_for_other_reasons_still_goes(self):
        # no pane at all: FinishLynx stays the record for finishes
        mgr = make_mgr()
        app = make_app(mgr=mgr)
        app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20)))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])

    def test_pace_change_releases_it(self):
        mgr = make_mgr()
        app = make_app(mgr=mgr)
        k = racing_5k(mgr)
        app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=8)))   # < 12:26 floor
        self.assertEqual(app.finishlynx.sent, [])
        # a 2:00/mile floor makes 8 min plausible: reclassified, assigned, sent
        mgr.set_scoring(k, 5, 2, fastest_pace=120.0)
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])


class TestPublishingSwitch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = make_mgr()
        self.app = make_app(mgr=self.mgr, hardware=True, event_dir=self.tmp.name)
        self.k = racing_5k(self.mgr)

    def tearDown(self):
        self.tmp.cleanup()

    def lif(self):
        return os.path.join(self.tmp.name, "RFID", "003-1-01.lif")

    def test_off_keeps_everything_back_and_on_catches_up(self):
        out = self.app.set_publishing(False)
        self.assertFalse(out["publishing"])
        self.app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20)))
        self.assertEqual(self.app.finishlynx.sent, [])
        self.assertFalse(os.path.exists(self.lif()))            # no .lif while off
        res = self.mgr.results(self.k)
        self.assertEqual([r["bib"] for r in res["finishers"]], ["101"])   # but the pane has it
        self.assertEqual(len(self.mgr.unsent_reads("off")), 1)
        # on, declining the flush: nothing sent, .lif written, count cleared
        out = self.app.set_publishing(True, flush=False)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["kept_back"], 0)
        self.assertEqual(self.app.finishlynx.sent, [])
        self.assertTrue(os.path.exists(self.lif()))

    def test_on_with_flush_sends_kept_back_finishes(self):
        self.app.set_publishing(False)
        self.app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20)))
        self.app._on_tag_read(read("FINISH", "102", GUN + timedelta(minutes=21)))
        out = self.app.set_publishing(True, flush=True)
        self.assertEqual(out["sent"], 2)
        self.assertEqual([b for b, _ in self.app.finishlynx.sent], ["101", "102"])
        self.assertEqual(out["kept_back"], 0)

    def test_manual_reads_respect_it_too(self):
        self.app.set_publishing(False)
        self.app._race_reads_lock = threading.Lock()
        self.app._race_reads = []
        self.assertTrue(self.app.add_manual_read("103", GUN + timedelta(minutes=22)))
        self.assertEqual(self.app.finishlynx.sent, [])
        self.app.set_publishing(True)
        self.assertTrue(self.app.add_manual_read("103", GUN + timedelta(minutes=23)))
        self.assertEqual([b for b, _ in self.app.finishlynx.sent], ["103"])

    def test_status_reports_it(self):
        self.app.set_publishing(False)
        self.assertFalse(self.app.publishing)
        self.assertIsNotNone(self.app._publish_off_at)
        self.app.set_publishing(True)
        self.assertIsNone(self.app._publish_off_at)

    def test_api(self):
        calls = []
        web._application = SimpleNamespace(
            set_publishing=lambda on, flush=False: calls.append((on, flush)) or {"publishing": on, "sent": 0, "kept_back": 0})
        c = web.app.test_client()
        r = c.post("/api/publish", data=json.dumps({"on": False}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = c.post("/api/publish", data=json.dumps({"on": True, "flush": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [(False, False), (True, True)])
        r = c.post("/api/publish", data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 400)


class TestReadBackup(unittest.TestCase):

    def test_every_read_is_appended_with_the_panes_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = make_mgr()
            app = make_app(mgr=mgr, hardware=True, event_dir=tmp)
            racing_5k(mgr)
            app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20), antenna=7))
            app._on_tag_read(read("S1", "102", GUN + timedelta(seconds=30)))       # too early: held
            app.set_publishing(False)
            app._on_tag_read(read("FINISH", "103", GUN + timedelta(minutes=21)))   # kept back, still backed up
            path = app.backup_path()
            self.assertEqual(os.path.dirname(path), os.path.join(tmp, "RFID"))
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0].split("\t"), ["date", "clock", "reader", "bib", "antenna", "point", "pane", "reason"])
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[1].split("\t")[1:7], ["09:20:00.000", "FINISH", "101", "7", "FINISH", "assigned"])
            self.assertEqual(lines[2].split("\t")[2:7], ["S1", "102", "1", "S1", "held"])
            self.assertIn("too early", lines[2].split("\t")[7])
            self.assertEqual(lines[3].split("\t")[3], "103")

    def test_preview_and_no_meet_folder_write_nothing(self):
        mgr = make_mgr()
        app = make_app(mgr=mgr, hardware=False)
        self.assertEqual(app.backup_path(), "")
        app._on_tag_read(read("FINISH", "101", GUN + timedelta(minutes=20)))   # must not raise


class TestWaive(unittest.TestCase):

    def _race_with_gap(self, mgr):
        k = racing_5k(mgr)
        mgr.ingest("101", GUN + timedelta(minutes=6), "S1")
        mgr.ingest("101", GUN + timedelta(minutes=19), "FINISH")
        mgr.ingest("102", GUN + timedelta(minutes=20), "FINISH")       # no Mile 1 read
        return k

    def test_waive_clears_the_nag_and_is_undoable(self):
        mgr = make_mgr()
        k = self._race_with_gap(mgr)
        res = mgr.results(k)
        row = next(r for r in res["finishers"] if r["bib"] == "102")
        self.assertEqual(row["missing"], ["S1"])
        self.assertEqual(res["needs_review"], 1)
        self.assertEqual(mgr.waive_missing(k, "102"), ["S1"])
        res = mgr.results(k)
        row = next(r for r in res["finishers"] if r["bib"] == "102")
        self.assertEqual(row["missing"], [])
        self.assertEqual(row["waived"], ["S1"])
        self.assertEqual(res["needs_review"], 0)
        self.assertEqual(row["place"], 2)                       # still a finisher
        self.assertEqual(mgr.waive_missing(k, "102", undo=True), [])
        self.assertEqual(mgr.results(k)["needs_review"], 1)

    def test_estimate_all_skips_waived(self):
        mgr = make_mgr()
        k = self._race_with_gap(mgr)
        mgr.waive_missing(k, "102")
        out = mgr.estimate_missing(k)
        self.assertEqual(out["added"], [])

    def test_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            mgr = make_mgr(state_path=path)
            k = self._race_with_gap(mgr)
            mgr.waive_missing(k, "102", "S1")
            again = make_mgr(state_path=path)
            self.assertEqual(again.races[k].waived, {"102": ["S1"]})
            self.assertEqual(again.results(k)["needs_review"], 0)

    def test_api(self):
        mgr = make_mgr()
        k = self._race_with_gap(mgr)
        web._application = SimpleNamespace(race_manager=mgr, config=Config())
        c = web.app.test_client()
        r = c.post(f"/api/races/{k}/waive", data=json.dumps({"bib": "102"}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["waived"], ["S1"])
        r = c.post(f"/api/races/{k}/waive", data=json.dumps({"bib": "102", "undo": True}), content_type="application/json")
        self.assertEqual(r.get_json()["waived"], [])
        r = c.post(f"/api/races/{k}/waive", data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.status_code, 400)


class TestUnsentPersists(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            mgr = make_mgr(state_path=path)
            racing_5k(mgr)
            res = mgr.ingest("101", GUN + timedelta(seconds=60), "FINISH")
            mgr.mark_unsent(res["read_id"], "early")
            again = make_mgr(state_path=path)
            self.assertEqual(again.reads[res["read_id"]].unsent, "early")


if __name__ == "__main__":
    unittest.main()
