"""Check-in chute (2026-09-30): any reader can be the chute (Stations
Location "Check-in", {id: [CHUTE]}); a runner reaching it up to 2 min after
the gun still joins the race; the pane lists everyone checked in, moving
each up as their reads arrive; "no-show" only once the race is final.
python -m unittest tests.test_checkin_roster"""

import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager

ATH = {b: {"gender": "F", "last_name": f"Runner{b}", "school": "Red"} for b in ("11", "12", "13", "14")}


def manager():
    mgr = RaceManager(athlete_lookup=ATH.get, state_path=None)
    key = mgr.create_race("Girls 5K", event=1, gender="F", distance="5000")
    return mgr, key


class TestChuteLocation(unittest.TestCase):

    def test_any_reader_set_to_check_in_is_the_chute(self):
        mgr, key = manager()
        mgr.set_reader_points({"FINISH": ["CHUTE"]})
        mgr.set_status(key, "checkin")
        res = mgr.ingest("11", datetime.now(), "FINISH")
        self.assertEqual((res["state"], res["race"]), ("roster", key))
        self.assertIn("11", mgr.races[key].roster)
        self.assertTrue(mgr.races[key].checkins["11"])

    def test_the_reader_named_chute_still_works(self):
        mgr, key = manager()
        mgr.set_status(key, "checkin")
        self.assertEqual(mgr.ingest("12", datetime.now(), "CHUTE")["state"], "roster")

    def test_switching_a_reader_to_check_in_leaves_its_old_reads_alone(self):
        mgr, key = manager()
        mgr.set_status(key, "racing")
        mgr.set_gun(key, datetime.now() - timedelta(minutes=20))
        mgr.ingest("11", datetime.now(), "S1")
        before = [(r.bib, r.point, r.state) for r in mgr.reads.values()]
        mgr.set_reader_points({"S1": ["CHUTE"]})
        self.assertEqual([(r.bib, r.point, r.state) for r in mgr.reads.values()], before)


class TestLateCheckIn(unittest.TestCase):

    def setUp(self):
        self.mgr, self.key = manager()
        self.mgr.set_status(self.key, "checkin")
        self.gun = datetime.now()
        self.mgr.set_status(self.key, "racing")
        self.mgr.set_gun(self.key, self.gun)

    def test_within_two_minutes_joins_the_race(self):
        res = self.mgr.ingest("13", self.gun + timedelta(seconds=90), "CHUTE")
        self.assertEqual((res["state"], res["race"]), ("roster", self.key))

    def test_later_is_not_a_check_in(self):
        res = self.mgr.ingest("14", self.gun + timedelta(seconds=150), "CHUTE")
        self.assertEqual(res["state"], "ignored")
        self.assertNotIn("14", self.mgr.races[self.key].roster)


class TestPaneRoster(unittest.TestCase):

    def test_checked_in_runners_listed_until_read(self):
        mgr, key = manager()
        mgr.set_status(key, "checkin")
        t0 = datetime.now()
        for i, b in enumerate(("11", "12", "13")):
            mgr.ingest(b, t0 + timedelta(seconds=i), "CHUTE")
        mgr.set_status(key, "racing")
        mgr.set_gun(key, t0 + timedelta(minutes=1))
        res = mgr.results(key)
        self.assertEqual([r["bib"] for r in res["not_seen"]], ["11", "12", "13"])   # check-in order
        self.assertEqual(res["not_seen"][0]["last_name"], "Runner11")
        self.assertTrue(res["not_seen"][0]["checked_in"])
        mgr.ingest("12", t0 + timedelta(minutes=8), "S1")                           # Mile 1
        res = mgr.results(key)
        self.assertEqual([r["bib"] for r in res["not_seen"]], ["11", "13"])
        self.assertEqual([r["bib"] for r in res["on_course"]], ["12"])
        self.assertEqual(res["no_show"], ["11", "13"])                              # CSV keeps its list


if __name__ == "__main__":
    unittest.main()
