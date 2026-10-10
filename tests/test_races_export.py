import csv
import io
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager
from rfid_finishlynx.web import create_missing_panes, evt_race_list

ATHLETES = {
    "1": {"last_name": "A", "first_name": "a", "school": "Red", "gender": "F"},
    "2": {"last_name": "B", "first_name": "b", "school": "Blue", "gender": "F"},
    "3": {"last_name": "C", "first_name": "c", "school": "Red", "gender": "F"},
}


def make_mgr():
    return RaceManager(athlete_lookup=lambda bib: ATHLETES.get(bib), on_change=lambda k: None,
                       chute_reader="CHUTE", finish_readers={"FINISH"},
                       point_labels={"S1": "Mile 1"}, state_path=None)


class TestCsvExport(unittest.TestCase):

    def test_single_race_csv_has_places_gaps_splits_and_teams(self):
        mgr = make_mgr()
        key = mgr.create_race("Girls 5K", event=2, gender="F", scorers=1, displacers=0)
        gun = datetime(2026, 9, 12, 8, 28, 47)
        mgr.set_gun(key, gun, "camera start")
        mgr.ingest("1", gun + timedelta(seconds=400), "S1")
        mgr.ingest("1", gun + timedelta(seconds=1200.5), "FINISH")
        mgr.ingest("2", gun + timedelta(seconds=1210.0), "FINISH")
        mgr.races[key].roster.add("3")                      # no-show
        rows = list(csv.reader(io.StringIO(mgr.results_csv([key]))))
        self.assertEqual(rows[0][:6], ["Race", "Event", "Round", "Heat", "Status", "Place"])
        first, second, noshow = rows[1], rows[2], rows[3]
        self.assertEqual(first[:7], ["Girls 5K", "2", "1", "1", "finished", "1", "1"])
        self.assertTrue(first[10].startswith("20:00.5"), first[10])     # time
        self.assertEqual(first[11], "")                                  # winner has no gap
        self.assertIn("Mile 1 6:40.0", first[13])                        # split
        self.assertTrue(second[11].startswith("0:09.5"), second[11])     # gap to winner
        self.assertEqual(noshow[4:7], ["no show", "", "3"])
        # blank line then team table
        self.assertEqual(rows[4], [])
        self.assertEqual(rows[5][4:6], ["Team rank", "Team"])
        self.assertEqual(rows[6][4:7], ["1", "Red", "1"])

    def test_meet_csv_covers_all_races_in_order(self):
        mgr = make_mgr()
        k1 = mgr.create_race("Boys 5K", event=1, gender="M")
        k2 = mgr.create_race("Girls 5K", event=2, gender="F")
        gun = datetime(2026, 9, 12, 8, 0, 0)
        mgr.set_gun(k1, gun, "manual"); mgr.set_gun(k2, gun + timedelta(minutes=30), "manual")
        mgr.ingest("1", gun + timedelta(minutes=30, seconds=1000), "FINISH")
        text = mgr.results_csv()
        races = [r[0] for r in csv.reader(io.StringIO(text)) if r and r[0] not in ("Race", "")]
        self.assertEqual(races, ["Girls 5K"])               # Boys had nobody
        self.assertTrue(text.endswith("\r\n"))


class TestPanesFromEvt(unittest.TestCase):

    EVT = "1,1,1,Boys 5K,,,,,,5000\r\n2,1,1,Girls 5K,,,,,,5000\r\n3,1,1,JV Boys 5K,,,,,,5000\r\n"

    def test_creates_missing_panes_once(self):
        mgr = make_mgr()
        mgr.create_race("Girls 5K", event=2)               # already exists
        made = create_missing_panes(mgr, evt_race_list(self.EVT))
        self.assertEqual(made, 2)
        self.assertEqual([mgr.races[k].name for k in mgr.order], ["Girls 5K", "Boys 5K", "JV Boys 5K"])
        self.assertTrue(all(mgr.races[k].status in ("pending", "racing") for k in mgr.order))
        self.assertEqual(create_missing_panes(mgr, evt_race_list(self.EVT)), 0)


class TestUnarmedCandidates(unittest.TestCase):

    def test_arm_false_lists_without_starting(self):
        mgr = make_mgr()
        key = mgr.create_race("Girls 5K", event=2, gender="F")
        mgr.add_gun_candidate(key, datetime(2026, 9, 12, 9, 8, 0), "camera start", arm=False)
        race = mgr.races[key]
        self.assertIsNone(race.gun)
        self.assertEqual(race.status, "pending")
        self.assertEqual(len(race.gun_candidates), 1)
        mgr.add_gun_candidate(key, datetime(2026, 9, 12, 9, 41, 34), "FinishLynx clock")
        self.assertEqual(race.gun, datetime(2026, 9, 12, 9, 41, 34))
        self.assertEqual(race.status, "racing")


if __name__ == "__main__":
    unittest.main()
