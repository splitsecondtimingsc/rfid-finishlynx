"""2026-10-09 (after Jim Crawford): one read per station per race.

Bib 1439 crossed the finish mat at 26:33 and again at 39:18 walking back to
the team tent; the last finish read won and she dropped to 38th. Now each
station counts one read per athlete in a race - the first read of the first
crossing - unless the race says otherwise per station:

- Reads before: crossings skipped before the one that counts (a loop
  course over the mat early);
- Reads after: later crossings expected and ignored quietly; any more are
  ignored and listed under Needs attention ("extra_crossings").

Skipped and extra crossings stay out of FinishLynx's LapTime feed and go
the moment they count (renumbered, or placed by hand).

Hardware-free: python -m unittest tests.test_crossings
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager
from tests.test_publish_guard import make_app, read

GUN = datetime(2026, 10, 8, 17, 4, 26)
ATHLETES = {b: {"last_name": f"L{b}", "first_name": f"F{b}", "school": "North", "gender": "F"}
            for b in ("101", "102", "103")}


def mgr(state_path=None, **kw):
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE", finish_readers={"FINISH"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                       point_distances={"S1": 1609.0, "S2": 3218.0}, state_path=state_path, **kw)


def at(minutes, seconds=0):
    return GUN + timedelta(minutes=minutes, seconds=seconds)


class Base(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.k = self.m.create_race("Girls 5k", event=1, gender="F", distance="5000")
        self.m.set_gun(self.k, GUN)

    def finish(self, bib):
        row = next((r for r in self.m.results(self.k)["finishers"] if r["bib"] == bib), None)
        return row and round(row["finish"]["elapsed"], 2)

    def flagged(self):
        return [(x["bib"], x["count"]) for x in self.m.results(self.k)["extra_crossings"]]


class TestOneReadPerStation(Base):

    def test_a_walk_back_is_not_a_second_finish(self):
        self.m.ingest("101", at(26, 33), "FINISH")
        back = self.m.ingest("101", at(39, 18), "FINISH")
        self.assertEqual((back["state"], back["crossing"]), ("ignored", "unexpected"))
        self.assertEqual(self.finish("101"), 26 * 60 + 33)
        self.assertEqual(self.flagged(), [("101", 1)])
        self.assertIn("after the one that counts", back["reason"])

    def test_the_first_read_of_a_crossing_counts(self):
        # standing on the mat in the chute: read again 15 s later
        self.m.ingest("101", at(20), "FINISH")
        again = self.m.ingest("101", at(20, 15), "FINISH")
        self.assertEqual(again["crossing"], "repeat")
        self.assertEqual(self.finish("101"), 20 * 60)
        self.assertEqual(self.flagged(), [])                   # same crossing: not flagged

    def test_reads_after_are_expected(self):
        self.m.set_crossings(self.k, "FINISH", 0, 1)
        self.m.ingest("101", at(26, 33), "FINISH")
        back = self.m.ingest("101", at(39, 18), "FINISH")
        self.assertEqual(back["crossing"], "after")
        self.assertEqual(self.flagged(), [])
        third = self.m.ingest("101", at(45), "FINISH")
        self.assertEqual(third["crossing"], "unexpected")
        self.assertEqual(self.flagged(), [("101", 1)])

    def test_reads_before_skip_a_loop_crossing(self):
        self.m.set_crossings(self.k, "FINISH", 1, 0)
        early = self.m.ingest("101", at(9), "FINISH")
        self.assertEqual((early["state"], early["crossing"]), ("ignored", "skip"))
        self.assertIsNone(self.finish("101"))
        self.m.ingest("101", at(19), "FINISH")
        self.assertEqual(self.finish("101"), 19 * 60)

    def test_changing_it_renumbers_reads_already_heard(self):
        self.m.ingest("101", at(14), "FINISH")      # a loop crossing, counted as the finish
        self.m.ingest("101", at(19), "FINISH")
        self.assertEqual(self.finish("101"), 14 * 60)
        self.assertEqual(self.flagged(), [("101", 1)])
        self.m.set_crossings(self.k, "FINISH", 1, 0)
        self.assertEqual(self.finish("101"), 19 * 60)
        self.assertEqual(self.flagged(), [])
        self.assertEqual(self.m.snapshot()["races"][str(self.k)]["crossings"],
                         {"FINISH": {"before": 1, "after": 0}})
        self.m.set_crossings(self.k, "FINISH", 0, 0)                  # back to the default
        self.assertEqual(self.finish("101"), 14 * 60)
        self.assertEqual(self.m.races[self.k].crossings, {})

    def test_split_stations_count_once_too(self):
        self.m.ingest("101", at(6), "S1")
        self.m.ingest("101", at(14), "S1")          # back past Mile 1 on the way home
        self.m.ingest("101", at(20), "FINISH")
        row = next(r for r in self.m.results(self.k)["finishers"] if r["bib"] == "101")
        self.assertEqual([s["elapsed"] for s in row["splits"]], [360.0])

    def test_other_athletes_are_not_affected(self):
        self.m.ingest("101", at(20), "FINISH")
        self.m.ingest("102", at(21), "FINISH")
        self.m.ingest("101", at(30), "FINISH")
        self.assertEqual((self.finish("101"), self.finish("102")), (1200.0, 1260.0))

    def test_bad_values_are_refused(self):
        for before, after in ((-1, 0), (0, 21), ("x", 0)):
            with self.assertRaises(ValueError):
                self.m.set_crossings(self.k, "FINISH", before, after)
        with self.assertRaises(ValueError):
            self.m.set_crossings(self.k, "", 0, 0)


class TestOperator(Base):

    def setUp(self):
        super().setUp()
        self.m.ingest("101", at(26, 33), "FINISH")
        self.m.ingest("101", at(39, 18), "FINISH")
        self.extra = next(r.id for r in self.m.reads.values() if r.extra)

    def test_checked_clears_the_flag_and_changes_nothing(self):
        self.m.ignore_read(self.extra)
        self.assertEqual(self.flagged(), [])
        self.assertEqual(self.finish("101"), 26 * 60 + 33)
        self.assertEqual(self.m.reads[self.extra].extra, "checked")
        # a later renumber keeps it checked
        self.m.set_crossings(self.k, "FINISH", 0, 0)
        self.m.ingest("102", at(30), "FINISH")
        self.assertEqual(self.flagged(), [])

    def test_assigning_an_extra_crossing_makes_it_count(self):
        self.m.assign_read(self.extra, self.k)
        self.assertEqual(self.finish("101"), 39 * 60 + 18)
        self.assertTrue(self.m.reads[self.extra].pinned)
        self.assertEqual(self.flagged(), [])

    def test_ignoring_the_counted_finish_promotes_the_next(self):
        first = next(r.id for r in self.m.reads.values() if r.state == "assigned")
        self.m.ignore_read(first)
        self.assertEqual(self.finish("101"), 39 * 60 + 18)

    def test_remove_from_race_takes_the_extra_crossings_too(self):
        self.assertEqual(self.m.remove_bib(self.k, "101"), 2)
        self.assertIsNone(self.finish("101"))
        self.m.ingest("101", at(50), "FINISH")       # renumbers: nothing comes back from before
        self.assertEqual(self.finish("101"), 50 * 60)


class TestPersistence(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races-state.json")
            a = mgr(path)
            k = a.create_race("Girls 5k", event=1, gender="F", distance="5000")
            a.set_gun(k, GUN)
            a.set_crossings(k, "FINISH", 0, 1)
            a.ingest("101", at(20), "FINISH")
            a.ingest("101", at(30), "FINISH")
            a.ingest("101", at(40), "FINISH")
            b = mgr(path)
            self.assertEqual(b.races[k].crossings, {"FINISH": {"before": 0, "after": 1}})
            self.assertEqual(sorted(r.extra for r in b.reads.values()), ["", "after", "unexpected"])
            self.assertEqual([x["bib"] for x in b.results(k)["extra_crossings"]], ["101"])
            # a fourth crossing after the restart counts on from the restored ones
            self.assertEqual(b.ingest("101", at(45), "FINISH")["crossing"], "unexpected")


class TestFinishLynxFeed(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.app = make_app(mgr=self.m)
        self.k = self.m.create_race("Girls 5k", event=1, gender="F", distance="5000")
        self.m.set_gun(self.k, GUN)
        self.m.set_status(self.k, "racing")

    def sent(self):
        return [(b, ts) for b, ts in self.app.finishlynx.sent]

    def test_only_the_crossing_that_counts_goes(self):
        self.app._on_tag_read(read("FINISH", "101", at(20)))
        self.app._on_tag_read(read("FINISH", "101", at(20, 15)))      # same crossing
        self.app._on_tag_read(read("FINISH", "101", at(33)))          # walk back
        self.assertEqual([b for b, _ in self.sent()], ["101"])

    def test_a_loop_crossing_is_kept_back_then_sent_when_placed(self):
        self.m.set_crossings(self.k, "FINISH", 1, 0)
        self.app._on_tag_read(read("FINISH", "101", at(19)))
        self.assertEqual(self.sent(), [])
        rid = next(r.id for r in self.m.reads.values() if r.bib == "101")
        self.assertEqual(self.m.reads[rid].unsent, "early")
        # the reader missed the loop crossing: the operator places this one
        self.m.assign_read(rid, self.k)
        self.assertEqual([b for b, _ in self.sent()], ["101"])


class TestApi(Base):

    def test_post_crossings(self):
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m)
        c = web.app.test_client()
        r = c.post(f"/api/races/{self.k}/crossings", json={"crossings": {"FINISH": {"before": 1, "after": 2}}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["crossings"], {"FINISH": {"before": 1, "after": 2}})
        r = c.post(f"/api/races/{self.k}/crossings", json={"crossings": {"FINISH": {"before": 99}}})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/races/999/crossings", json={"crossings": {"FINISH": {}}})
        self.assertEqual(r.status_code, 404)


class TestFixingARunner(Base):
    """Someone did something odd on the course: the operator picks the
    crossing that counts, retimes a read, deletes one (2026-10-09)."""

    def setUp(self):
        super().setUp()
        for t in (24, 30, 36):                      # three crossings of the finish mat
            self.m.ingest("101", at(t), "FINISH")
        self.ids = sorted(r.id for r in self.m.reads.values() if r.bib == "101")

    def kinds(self):
        return [r["kind"] for r in self.m.athlete_reads(self.k, "101")]

    def test_the_dialog_lists_every_read(self):
        self.assertEqual(self.kinds(), ["counts", "unexpected", "unexpected"])
        rows = self.m.athlete_reads(self.k, "101")
        self.assertEqual([r["elapsed"] for r in rows], [1440.0, 1800.0, 2160.0])

    def test_use_a_middle_crossing(self):
        # the first was a loop the operator forgot to set; the last a walk back
        self.m.use_read(self.ids[1], self.k)
        self.assertEqual(self.finish("101"), 1800.0)
        self.assertEqual(self.kinds(), ["replaced", "counts", "replaced"])
        self.assertEqual(self.flagged(), [])
        # more crossings later still don't move it
        self.m.ingest("101", at(40), "FINISH")
        self.assertEqual(self.finish("101"), 1800.0)

    def test_use_an_earlier_crossing_than_the_one_counting(self):
        self.m.set_crossings(self.k, "FINISH", 1, 1)       # now the 30:00 one counts
        self.assertEqual(self.finish("101"), 1800.0)
        self.m.use_read(self.ids[0], self.k)               # but it was the 24:00 one
        self.assertEqual(self.finish("101"), 1440.0)

    def test_deleting_the_used_read_goes_back_to_the_numbering(self):
        self.m.use_read(self.ids[2], self.k)
        self.m.ignore_read(self.ids[2])
        self.assertEqual(self.finish("101"), 1440.0)
        self.assertEqual(self.kinds(), ["counts", "unexpected", "deleted"])

    def test_a_deleted_read_can_be_used_again(self):
        self.m.ignore_read(self.ids[0])                    # a stray read
        self.assertEqual(self.finish("101"), 1800.0)       # the next crossing counts
        self.m.use_read(self.ids[0], self.k)               # no, it was right
        self.assertEqual(self.finish("101"), 1440.0)

    def test_a_typed_finish_wins_over_the_reader(self):
        self.m.add_manual_read(self.k, "101", at(25), "FINISH")
        self.assertEqual(self.finish("101"), 1500.0)
        self.assertEqual(self.kinds(), ["replaced", "counts", "replaced", "replaced"])

    def test_retiming_reorders_the_crossings(self):
        self.m.override_time(self.ids[2], at(23))          # the last read's clock was off
        self.assertEqual(self.finish("101"), 23 * 60)

    def test_api(self):
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m)
        c = web.app.test_client()
        r = c.get(f"/api/races/{self.k}/athlete/101/reads")
        self.assertEqual([x["kind"] for x in r.get_json()["reads"]], ["counts", "unexpected", "unexpected"])
        r = c.post(f"/api/reads/{self.ids[1]}/use", json={"race": self.k})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.finish("101"), 1800.0)
        # a race time, from the race's gun
        r = c.post(f"/api/reads/{self.ids[1]}/override", json={"clock": "29:58.5"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.finish("101"), 1798.5)
        # a clock time keeps the read's own day (the meet was yesterday)
        r = c.post(f"/api/reads/{self.ids[1]}/override", json={"clock": "17:34:20.000"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.finish("101"), 29 * 60 + 54)
        # empty: the raw read again
        c.post(f"/api/reads/{self.ids[1]}/override", json={"clock": ""})
        self.assertEqual(self.finish("101"), 1800.0)

    def test_film_offset_is_allowed_for(self):
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m)
        self.m.set_film_offset(manual=0.5)
        c = web.app.test_client()
        c.post(f"/api/reads/{self.ids[0]}/override", json={"clock": "24:10.00"})
        self.assertEqual(self.finish("101"), 1450.0)       # what was typed is what shows


if __name__ == "__main__":
    unittest.main()
