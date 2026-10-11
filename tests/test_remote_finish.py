"""A remote split station standing at the finish line (and the local mat
standing at a mile marker).

races.reader_points already let a reader count as another point by pass
number; this makes the FINISH role follow that map instead of the reader
id: {S1: [FINISH]} turns every S1 read into a finish - places, promotion
of held splits, the plausibility floor, the film offset, the FinishLynx
LapTime feed - and {FINISH: [S1]} turns the local mat into Mile 1.
python -m unittest tests.test_remote_finish
"""
import json
import os
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config
from rfid_finishlynx.races import RaceManager
from rfid_finishlynx.rfid_reader import TagRead

GUN = datetime(2026, 10, 3, 10, 0, 0)

ATHLETES = {
    "101": {"last_name": "Alvarez", "first_name": "K", "school": "North", "gender": "F"},
    "102": {"last_name": "Osei", "first_name": "M", "school": "North", "gender": "F"},
    "103": {"last_name": "Kirby", "first_name": "D", "school": "West", "gender": "F"},
}


def mgr(**kw):
    kw.setdefault("reader_points", {"S1": ["FINISH"]})
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE",
                       finish_readers={"FINISH"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                       point_distances={"S1": 1609.344, "S2": 3218.688}, **kw)


def m(minutes, seconds=0):
    return GUN + timedelta(minutes=minutes, seconds=seconds)


class TestStationIsTheFinish(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.race = self.m.create_race("Girls Varsity", event=3, gender="F", distance="5000")
        self.m.set_status(self.race, "checkin")
        self.m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        self.m.set_gun(self.race, GUN)

    def row(self, bib):
        res = self.m.results(self.race)
        return next((r for r in res["finishers"] + res["on_course"] if r["bib"] == bib), None)

    def test_station_read_is_a_finish_with_a_place(self):
        self.m.ingest("101", m(13), "S2")
        out = self.m.ingest("101", m(20), "S1")
        self.assertEqual((out["state"], out["point"]), ("assigned", "FINISH"))
        r = self.row("101")
        self.assertEqual(r["place"], 1)
        self.assertEqual(r["finish"]["elapsed"], 20 * 60)
        self.assertEqual([s["point"] for s in r["splits"]], ["S2"])
        # the raw station is remembered on the read
        fin = next(x for x in self.m.reads.values() if x.point == "FINISH")
        self.assertEqual(fin.reader, "S1")

    def test_finish_from_the_station_promotes_held_splits(self):
        # 102 is not on the roster: her Mile 2 waits, her finish validates her
        held = self.m.ingest("102", m(13), "S2")
        self.assertEqual(held["state"], "assigned")    # gender F, only open girls' race
        self.m.set_status(self.race, "racing")
        self.m.ingest("103", m(14), "S2")
        out = self.m.ingest("103", m(21), "S1")
        self.assertEqual(out["point"], "FINISH")
        self.assertEqual(self.row("103")["place"], 1)
        self.assertEqual([s["point"] for s in self.row("103")["splits"]], ["S2"])

    def test_plausibility_uses_the_finish_floor_not_mile_1(self):
        # 5000 m at 4:00/mile = 12:26; a station read at 5:00 is not a finish
        out = self.m.ingest("101", m(5), "S1")
        self.assertEqual(out["state"], "held")
        self.assertIn("too early for Finish", out["reason"])
        self.assertIsNone(self.row("101"))

    def test_film_offset_is_applied_to_the_station_finish(self):
        self.m.film.set_manual(0.65)
        out = self.m.ingest("101", m(20), "S1")
        self.assertAlmostEqual(out["finish_offset"], 0.65)
        # ...and not to a station that is a mile marker
        self.m.set_reader_points({"S1": ["S2"]})
        out = self.m.ingest("102", m(13), "S1")
        self.assertEqual(out["finish_offset"], 0.0)

    def test_loop_course_finish_station(self):
        self.m.set_reader_points({"S1": ["-", "FINISH"]})
        first = self.m.ingest("101", m(9), "S1")
        self.assertEqual(first["state"], "ignored")
        self.assertIn("not a split", first["reason"])
        real = self.m.ingest("101", m(19), "S1")
        self.assertEqual((real["state"], real["point"]), ("assigned", "FINISH"))
        self.assertEqual(self.row("101")["finish"]["elapsed"], 19 * 60)

    def test_one_mat_at_mile_2_and_the_finish(self):
        self.m.set_reader_points({"S1": ["S2", "FINISH"]})
        a = self.m.ingest("101", m(13), "S1")
        b = self.m.ingest("101", m(20), "S1")
        self.assertEqual((a["point"], b["point"]), ("S2", "FINISH"))
        r = self.row("101")
        self.assertEqual(r["place"], 1)
        self.assertEqual([s["point"] for s in r["splits"]], ["S2"])

    def test_switching_a_station_to_finish_relabels_reads_already_heard(self):
        self.m.set_reader_points({})
        self.m.ingest("101", m(20), "S1")             # heard as Mile 1
        self.assertIsNone(self.row("101")["finish"])
        self.m.set_reader_points({"S1": ["FINISH"]})
        self.assertEqual(self.row("101")["finish"]["elapsed"], 20 * 60)

    def test_lif_places_the_station_finish(self):
        import os
        import tempfile
        self.m.ingest("101", m(13), "S2")
        self.m.ingest("101", m(20), "S1")
        with tempfile.TemporaryDirectory() as d:
            path = self.m.write_lif(self.race, d)
            with open(path) as f:
                line = f.read().splitlines()[1]
        self.assertTrue(line.startswith("1,101,"), line)
        self.assertIn("20:00.00", line)


class TestWhichReadersAreTheFinish(unittest.TestCase):

    def test_unmapped_local_mat_is_the_finish(self):
        m_ = mgr(reader_points={})
        self.assertTrue(m_.reader_is_finish("FINISH"))
        self.assertFalse(m_.reader_is_finish("S1"))
        self.assertEqual(m_.finish_reader_ids(), ["FINISH"])

    def test_station_mapped_to_finish_joins_the_local_mat(self):
        m_ = mgr()
        self.assertTrue(m_.reader_is_finish("S1"))
        self.assertEqual(m_.finish_reader_ids(), ["S1", "FINISH"])

    def test_local_mat_as_a_mile_marker(self):
        m_ = mgr(reader_points={"S1": ["FINISH"], "FINISH": ["S2"]})
        self.assertFalse(m_.reader_is_finish("FINISH"))
        self.assertEqual(m_.finish_reader_ids(), ["S1"])
        race = m_.create_race("Boys", event=4, gender="M", distance="5000")
        m_.set_gun(race, GUN)
        m_.athlete_lookup = {"201": {"gender": "M", "last_name": "T", "first_name": "J", "school": "C"}}.get
        out = m_.ingest("201", m(13), "FINISH")
        self.assertEqual((out["state"], out["point"]), ("assigned", "S2"))
        self.assertEqual(out["finish_offset"], 0.0)
        res = m_.results(race)
        self.assertEqual(res["finishers"], [])
        self.assertEqual([s["point"] for s in res["on_course"][0]["splits"]], ["S2"])

    def test_loop_mat_counts_as_finish_reader(self):
        m_ = mgr(reader_points={"S1": ["-", "FINISH"]})
        self.assertTrue(m_.reader_is_finish("S1"))

    def test_snapshot_lists_the_finish_readers(self):
        self.assertEqual(mgr().snapshot()["finish_reader_ids"], ["S1", "FINISH"])


# ---------------------------------------------------------- FinishLynx feed

class FakeLynx:
    def __init__(self):
        self.is_connected = True
        self.sent = []

    def send_split(self, identifier, ts, e=None, r=None, h=None):
        self.sent.append((identifier, ts))
        return True


def make_app(reader_points):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.finishlynx.feed = "finish"
    app.config.races.reader_points = reader_points
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
                   antenna=0, reader_id=reader_id)


class TestFeed(unittest.TestCase):

    def test_finish_station_reads_go_to_laptime(self):
        app = make_app({"S1": ["FINISH"]})
        app._on_tag_read(read("S1"))
        app._on_tag_read(read("S2", "102"))
        app._on_tag_read(read("FINISH", "103"))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101", "103"])

    def test_local_mat_as_a_split_stays_out_of_laptime(self):
        app = make_app({"S1": ["FINISH"], "FINISH": ["S2"]})
        app._on_tag_read(read("FINISH", "103"))
        app._on_tag_read(read("S1"))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])
        self.assertEqual(app.finish_reader_ids(), ["S1"])

    def test_loop_course_finish_station_feeds_every_crossing(self):
        # as the local finish mat always did: the feed gets every read of
        # a reader that is the finish, FinishLynx's lap slots sort it out
        app = make_app({"S1": ["-", "FINISH"]})
        app._on_tag_read(read("S1"))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])

    def test_manual_reads_always_feed(self):
        app = make_app({"FINISH": ["S1"]})
        app._on_tag_read(read("USER"))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])

    def test_film_offset_reaches_the_station_finish_sent_to_laptime(self):
        app = make_app({"S1": ["FINISH"]})
        app.race_manager = mgr()
        app.race_manager.film.set_manual(0.5)
        race = app.race_manager.create_race("Girls", event=3, gender="F", distance="5000")
        app.race_manager.set_gun(race, datetime(2026, 9, 17, 9, 40, 0))
        app._on_tag_read(read("S1"))
        self.assertEqual(app.finishlynx.sent, [("101", datetime(2026, 9, 17, 9, 59, 59, 500000))])


class TestConfigApi(unittest.TestCase):

    def setUp(self):
        self.cfg = Config()
        self.mgr = mgr(reader_points={})
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(config=self.cfg, race_manager=self.mgr, save_config=lambda: None,
                                           is_running=False, finishlynx=None)
        self.c = web.app.test_client()

    def post(self, rp):
        return self.c.post("/api/config", data=json.dumps({"races": {"reader_points": rp}}),
                           content_type="application/json")

    def test_station_can_be_mapped_to_the_finish(self):
        r = self.post({"S1": ["FINISH"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.cfg.races.reader_points, {"S1": ["FINISH"]})
        self.assertEqual(self.mgr.reader_points, {"S1": ["FINISH"]})
        self.assertEqual(self.mgr.finish_reader_ids(), ["S1", "FINISH"])
        self.assertEqual(self.c.get("/api/config").get_json()["races"]["reader_points"], {"S1": ["FINISH"]})

    def test_local_mat_can_be_mapped_to_a_split(self):
        r = self.post({"FINISH": ["S1"], "S1": ["FINISH"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.mgr.finish_reader_ids(), ["S1"])

    def test_unknown_point_still_refused(self):
        self.assertEqual(self.post({"S1": ["LINE"]}).status_code, 400)


class TestReaderNotInUse(unittest.TestCase):
    """{FINISH: [off]}: the local mat is unplugged because a station is
    the finish line - its reads are ignored, it is never the finish, and
    the console must not alarm when it is down."""

    def setUp(self):
        self.m = mgr(reader_points={"S1": ["FINISH"], "FINISH": ["off"]})
        self.race = self.m.create_race("Girls Varsity", event=3, gender="F", distance="5000")
        self.m.set_gun(self.race, GUN)

    def test_aliases(self):
        self.assertEqual(RaceManager.normalize_points(["Not in use"]), ["off"])
        self.assertEqual(RaceManager.normalize_points(["unused"]), ["off"])

    def test_not_the_finish_and_not_a_split(self):
        self.assertFalse(self.m.reader_is_finish("FINISH"))
        self.assertTrue(self.m.reader_off("FINISH"))
        self.assertEqual(self.m.finish_reader_ids(), ["S1"])

    def test_reads_are_ignored_restorably(self):
        out = self.m.ingest("101", m(20), "FINISH")
        self.assertEqual((out["state"], out["race"], out["finish_offset"]), ("ignored", None, 0.0))
        self.assertIn("not in use", out["reason"])
        res = self.m.results(self.race)
        self.assertEqual((res["finishers"], res["on_course"]), ([], []))
        self.assertEqual([(r["bib"], r["point"]) for r in self.m.ignored_reads()], [("101", "FINISH")])
        # the station's finish still places her
        self.assertEqual(self.m.ingest("101", m(21), "S1")["point"], "FINISH")
        self.assertEqual(self.m.results(self.race)["finishers"][0]["place"], 1)

    def test_switching_off_moves_its_reads_out_and_back(self):
        self.m.set_reader_points({"S1": ["FINISH"]})          # local mat in use again
        self.m.ingest("101", m(20), "FINISH")
        self.assertEqual(self.m.results(self.race)["finishers"][0]["bib"], "101")
        self.m.set_reader_points({"S1": ["FINISH"], "FINISH": ["off"]})
        self.assertEqual(self.m.results(self.race)["finishers"], [])
        self.assertEqual(len(self.m.ignored_reads()), 1)
        # back in use: the finish returns to the race it was in, not to Held
        self.m.set_reader_points({"S1": ["FINISH"]})
        self.assertEqual([r["bib"] for r in self.m.results(self.race)["finishers"]], ["101"])
        self.assertEqual(self.m.held_reads(), [])
        self.assertEqual(self.m.ignored_reads(), [])

    def test_heard_while_off_goes_to_held_when_back_on(self):
        # never in a race (maybe a warm-up or tag check): the operator decides
        self.m.ingest("101", m(20), "FINISH")                   # local mat is off
        self.m.set_reader_points({"S1": ["FINISH"]})
        self.assertEqual([(r["bib"], r["point"]) for r in self.m.held_reads()], [("101", "FINISH")])
        self.assertEqual(self.m.results(self.race)["finishers"], [])

    def test_finish_moved_to_a_station_and_back_mid_race(self):
        # 2026-09-26 console test: S1 made the finish (This PC -> Not in
        # use), then This PC made the finish again (S1 -> Not in use), then
        # S1 back to Mile 1. Every read must end up in the race again.
        m_ = mgr(reader_points={})
        race = m_.create_race("Girls Varsity", event=2, gender="F", distance="5000")
        m_.set_gun(race, GUN)
        for i, bib in enumerate(("101", "102", "103")):
            m_.ingest(bib, m(5.5), "S1")
            m_.ingest(bib, m(16 + i / 10), "FINISH")
        m_.set_reader_points({"S1": ["FINISH"], "FINISH": ["off"]})
        m_.set_reader_points({"FINISH": ["FINISH"], "S1": ["off"]})
        m_.set_reader_points({})
        res = m_.results(race)
        self.assertEqual([r["bib"] for r in res["finishers"]], ["101", "102", "103"])
        self.assertTrue(all(any(s["point"] == "S1" for s in r["splits"]) for r in res["finishers"]))
        self.assertEqual((m_.held_reads(), m_.ignored_reads()), ([], []))

    def test_off_race_survives_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            m1 = mgr(reader_points={}, state_path=path)
            race = m1.create_race("Girls Varsity", event=3, gender="F", distance="5000")
            m1.set_gun(race, GUN)
            m1.ingest("101", m(20), "FINISH")
            m1.set_reader_points({"S1": ["FINISH"], "FINISH": ["off"]})
            m2 = mgr(reader_points={"S1": ["FINISH"], "FINISH": ["off"]}, state_path=path)
            m2.set_reader_points({"S1": ["FINISH"]})
            self.assertEqual([r["bib"] for r in m2.results(race)["finishers"]], ["101"])

    def test_feed_and_role(self):
        app = make_app({"S1": ["FINISH"], "FINISH": ["off"]})
        app._on_tag_read(read("FINISH", "103"))
        app._on_tag_read(read("S1"))
        self.assertEqual([b for b, _ in app.finishlynx.sent], ["101"])
        self.assertEqual(app.reader_role("FINISH"), "not in use")
        self.assertEqual(app.reader_role("S1"), "the finish")
        app.config.races.reader_points = {"FINISH": ["S1"]}
        self.assertEqual(app.reader_role("FINISH"), "Mile 1")

    def test_config_api(self):
        cfg = Config()
        mgr_ = mgr(reader_points={})
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(config=cfg, race_manager=mgr_, save_config=lambda: None,
                                           is_running=False, finishlynx=None)
        c = web.app.test_client()
        post = lambda rp: c.post("/api/config", data=json.dumps({"races": {"reader_points": rp}}),
                                 content_type="application/json")
        self.assertEqual(post({"S1": ["FINISH"], "FINISH": ["off"]}).status_code, 200)
        self.assertEqual(mgr_.finish_reader_ids(), ["S1"])
        self.assertEqual(post({"FINISH": ["off", "S1"]}).status_code, 400)
        self.assertEqual(c.get("/api/races").get_json()["finish_reader_ids"], ["S1"])


if __name__ == "__main__":
    unittest.main()
