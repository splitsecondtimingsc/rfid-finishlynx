"""True ties (owner, 2026-10-13): the same time to the thousandth (truncated)
gives the same place and the same points; the next place is skipped.
python -m pytest tests/test_true_ties.py"""

import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.meet import scoring
from rfid_finishlynx.meet.model import Athlete, Entry, Meet, MeetInfo, Race, Team
from rfid_finishlynx.races import RaceManager, tie_key

GUN = datetime(2026, 10, 13, 16, 0, 0)
ATH = {str(b): {"gender": "M", "last_name": f"R{b}", "school": "A" if b < 106 else "B"} for b in range(100, 112)}


class Keys(unittest.TestCase):

    def test_truncated_thousandths(self):
        self.assertEqual(tie_key(1000.1234), tie_key(1000.1239))
        self.assertNotEqual(tie_key(1000.1234), tie_key(1000.1241))
        self.assertIsNone(tie_key(None))


class RaceCard(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: ATH.get(b, {}))
        self.k = self.m.create_race("Boys", event=2, gender="M")
        self.m.set_gun(self.k, GUN)
        times = {"100": 1000.1234, "101": 1000.1236, "102": 1001.0, "106": 999.0,
                 "103": 1002.0, "104": 1003.0, "105": 1004.0, "107": 1005.0, "108": 1006.0,
                 "109": 1007.0, "110": 1008.0}
        for b, t in times.items():
            self.m.races[self.k].roster.add(b)
            self.m.ingest(b, GUN + timedelta(seconds=t), "FINISH")

    def test_places_and_points(self):
        res = self.m.results(self.k)
        pl = {r["bib"]: r["place"] for r in res["finishers"]}
        self.assertEqual((pl["106"], pl["100"], pl["101"], pl["102"]), (1, 2, 2, 4))
        teams = {t["team"]: t["places"] for t in res["teams"]}
        self.assertEqual(teams["A"][:3], [2, 2, 4])          # tied runners share the points

    def test_results_csv_rows(self):
        rows = self.m.results_rows(self.k)                  # [name, event, round, heat, status, place, bib, ...]
        self.assertEqual([(r[5], r[6]) for r in rows[:4]], [("1", "106"), ("2", "100"), ("2", "101"), ("4", "102")])


class MeetResults(unittest.TestCase):

    def test_places_event_places_and_points(self):
        meet = Meet(MeetInfo(id="m", name="Meet", date="2026-10-13"))
        meet.teams += [Team(id="tA", name="A"), Team(id="tB", name="B")]
        race = Race(event=1)
        for i in range(10):
            aid = f"a{i}"
            meet.athletes.append(Athlete(id=aid, first="F", last=f"L{i}", gender="M",
                                         team_id="tA" if i < 5 else "tB", bib=str(200 + i)))
            race.entries.append(Entry(athlete_id=aid, event=1))
        meet.races.append(race)
        times = [900.0, 900.0004, 901.0, 902.0, 903.0, 904.0, 905.0, 906.0, 907.0, 908.0]
        rm = {"finishers": [{"bib": str(200 + i), "finish": {"elapsed": t}, "splits": []} for i, t in enumerate(times)],
              "on_course": []}
        res, _ = scoring.build_results(meet, race, rm, final=False)
        f = res["finishers"]
        self.assertEqual([x["place"] for x in f[:3]], [1, 1, 3])
        self.assertEqual([x["event_place"] for x in f[:3]], [1, 1, 3])
        team_a = next(t for t in res["teams"]["1"] if t["team_id"] == "tA")
        self.assertEqual([r["points"] for r in team_a["runners"]][:3], [1, 1, 3])


if __name__ == "__main__":
    unittest.main()
