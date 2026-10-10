import json
import os
import tempfile
import unittest

from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, MeetStore,
                                        Race, Team, parse_start_time, slug, start_time_text,
                                        time_text)


def small_meet() -> Meet:
    m = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10"))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID"),
               Team("t2", "Valley High School", short="Valley", code="VAL")]
    m.athletes = [Athlete("a1", "Avery", "Birch", "F", team_id="t1", bib="101", grad_year=2027),
                  Athlete("a2", "Blake", "Calder", "M", team_id="t2", bib="102", grad_year=2030),
                  Athlete("a3", "Casey", "Dunmore", "F", team_id="t1", grade_text="Senior")]
    m.events = [Event(1, "Girls 5k Run CC Varsity", gender="F", distance_m=5000,
                      division="Varsity Girls", code="5000"),
                Event(2, "Girls 5k Run CC JV", gender="F", distance_m=5000,
                      division="JV Girls", code="5000")]
    m.races = [Race(1, order=1, entries=[Entry("a1", 1), Entry("a3", 2, status="exhibition")])]
    m.bib_ranges = [[100, 199], [500, None]]
    return m


class TestModel(unittest.TestCase):
    def test_valid_meet_has_no_problems(self):
        self.assertEqual(small_meet().validate(), [])

    def test_grade_from_graduation_year(self):
        m = small_meet()
        self.assertEqual(m.season_end_year, 2027)            # fall 2026
        self.assertEqual(m.grade(m.athlete("a1")), "12")
        self.assertEqual(m.grade(m.athlete("a2")), "9")
        self.assertEqual(m.grade(m.athlete("a3")), "Senior")  # free text kept
        m.meet.date = "2027-04-03"                           # spring: same school year
        self.assertEqual(m.season_end_year, 2027)

    def test_athlete_lookup_matches_racemanager_shape(self):
        m = small_meet()
        self.assertEqual(m.athlete_lookup("101"), {"last_name": "Birch", "first_name": "Avery",
                                                   "school": "Ridge", "gender": "F"})
        self.assertIsNone(m.athlete_lookup("999"))
        self.assertIsNone(m.athlete_lookup(""))

    def test_validate_catches_conflicts(self):
        m = small_meet()
        m.athletes[1].bib = "101"
        m.teams[1].code = "rid"
        m.races[0].entries.append(Entry("a9", 1))
        m.races[0].entries.append(Entry("a1", 1))
        m.athletes[2].gender = "m"
        errs = "\n".join(m.validate())
        for want in ("bib 101 on 2", "team code", "unknown athlete a9", "a1 entered twice",
                     "gender 'm'"):
            self.assertIn(want, errs)

    def test_bibs_compare_as_the_reader_reports_them(self):
        m = small_meet()
        self.assertEqual(m.athlete_by_bib("0101").id, "a1")    # reader drops zeros
        m.athletes[2].bib = "0101"
        errs = "\n".join(m.validate())
        self.assertIn("leading zeros", errs)
        self.assertIn("bib 101 on 2", errs)
        m.athletes[2].bib = "100000"
        self.assertIn("1-99999", "\n".join(m.validate()))

    def test_new_ids_never_reuse(self):
        m = small_meet()
        self.assertEqual(m.new_athlete_id(), "a4")
        m.athletes.pop(0)
        self.assertEqual(m.new_athlete_id(), "a4")
        self.assertEqual(m.new_team_id(), "t3")

    def test_round_trip(self):
        m = small_meet()
        m.races[0].results = {"frozen_at": "x", "finishers": [], "teams": {}}
        again = Meet.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(again, m)

    def test_unknown_keys_from_newer_file_are_ignored(self):
        d = small_meet().to_dict()
        d["athletes"][0]["shoe_size"] = 9
        d["meet"]["weather"] = "rain"
        self.assertEqual(Meet.from_dict(d).athlete("a1").first, "Avery")


class TestStore(unittest.TestCase):
    def test_create_save_load_list(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetStore(root)
            m = store.create("Woodmont Invitational", "2026-10-10", host="Woodmont HS")
            self.assertEqual(m.meet.id, "2026-10-10-woodmont-invitational")
            m2 = store.create("Woodmont Invitational", "2026-10-10")
            self.assertEqual(m2.meet.id, "2026-10-10-woodmont-invitational-2")
            m.teams.append(Team("t1", "Ridge"))
            store.save(m)
            self.assertFalse(os.path.exists(store.path(m.meet.id) + ".tmp"))
            self.assertEqual(store.load(m.meet.id).teams[0].name, "Ridge")
            self.assertEqual(len(store.list()), 2)

    def test_raw_pulls_kept_beside_the_meet(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetStore(root)
            m = store.create("Test", "2026-10-10")
            self.assertIsNone(store.load_pull(m.meet.id, "entries"))
            store.save_pull(m.meet.id, "entries", [{"teamName": "Ridge"}])
            self.assertEqual(store.load_pull(m.meet.id, "entries"), [{"teamName": "Ridge"}])
            self.assertEqual(store.list(), [m.meet.id])
            with self.assertRaises(ValueError):
                store.pull_path(m.meet.id, "../x")

    def test_slug(self):
        self.assertEqual(slug("Region 1-5A  Championship!"), "region-1-5a-championship")
        self.assertEqual(slug("!!!"), "meet")


class TestStartTimes(unittest.TestCase):
    """Race.start_time (2026-10-03): stored HH:MM, typed leniently, read as
    '8:00 AM' (MileSplit Live shows that text verbatim)."""

    def test_parse_is_lenient(self):
        for typed, want in [("8:00 AM", "08:00"), ("8am", "08:00"), ("8:00a", "08:00"),
                            ("08:00", "08:00"), ("8:00", "08:00"), ("0800", "08:00"),
                            ("930", "09:30"), ("20:15", "20:15"), ("8:30 p.m.", "20:30"),
                            ("12:00 AM", "00:00"), ("12:30 pm", "12:30"), (" 7 ", "07:00"),
                            ("1:30", "13:30"), ("615", "18:15"), ("01:30", "01:30"),
                            ("12:45", "12:45"), ("13:30", "13:30"), ("1:30 am", "01:30"),
                            ("", ""), (None, "")]:
            self.assertEqual(parse_start_time(typed), want, typed)

    def test_garbage_is_refused(self):
        for typed in ("25:00", "13:00 PM", "0:30 am", "8:60", "8:5", "noon", "8:00 xm"):
            with self.assertRaises(ValueError, msg=typed):
                parse_start_time(typed)

    def test_text_has_no_leading_zero(self):
        self.assertEqual(time_text("08:00"), "8:00 AM")
        self.assertEqual(time_text("00:05"), "12:05 AM")
        self.assertEqual(time_text("12:00"), "12:00 PM")
        self.assertEqual(time_text("20:15"), "8:15 PM")
        self.assertEqual(time_text(""), "")
        self.assertEqual(start_time_text(Race(1, start_time="09:30")), "9:30 AM")

    def test_validate_and_round_trip(self):
        m = small_meet()
        m.races[0].start_time = "9:30"                       # not canonical
        self.assertIn("start time '9:30'", "\n".join(m.validate()))
        m.races[0].start_time = "09:30"
        self.assertEqual(m.validate(), [])
        self.assertEqual(Meet.from_dict(json.loads(json.dumps(m.to_dict()))).races[0].start_time,
                         "09:30")
        old = m.to_dict()                                    # a meet.json from before the field
        del old["races"][0]["start_time"]
        self.assertEqual(Meet.from_dict(old).races[0].start_time, "")


if __name__ == "__main__":
    unittest.main()
