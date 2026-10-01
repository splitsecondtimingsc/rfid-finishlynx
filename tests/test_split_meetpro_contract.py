"""The streamed .lif as MeetPro2 2.3.137 reads it (tests/meetpro_model.py).

Two MeetPro paths read the file during a race:
  A  "Start Splits Auto-Pull"  - local results and split scores
  B  "Start MS Live Sync"      - the ONLY path that sends splits to
                                  MileSplit Live while the race runs
They are mutually exclusive in MeetPro's XC tab. Both map split entries to
the event's configured split points purely BY POSITION and skip empty
entries without keeping a slot.

MeetPro split points here: ["Mile 1", "Mile 2"] - the console's S1/S2.
The missed-split case was confirmed on real MeetPro 2.3.137 on 2026-09-23
(Splits Auto-Pull filed a Mile 2 under Mile 1 and the finish under Mile 2,
bench/results/meetpro-autopull-console-lif-20260923.*); lif_merge.py's
rules fix it and the tests below keep it fixed.
Hardware-free: python -m unittest tests.test_split_meetpro_contract
"""
import os
import unittest
from datetime import timedelta

from tests import meetpro_model as mp
from tests.test_split_lif_contract import (ATHLETES, FL_FIXTURE, GUN, load_tool, make_mgr,
                                           race_with_splits)

POINTS = ["Mile 1", "Mile 2"]
TEAMS = {"Red", "Blue"}


def streamed(mgr=None):
    mgr, key = race_with_splits(mgr)
    name = mgr.lif_name(mgr.races[key])
    return mgr, key, name, mgr.lif_content(key)


class TestModelMatchesTheBytecode(unittest.TestCase):
    """Guard the model itself with the facts read from MeetPro.swf."""

    def test_quoted_field_with_commas_stays_one_field(self):
        f = mp.fields('1,7,,A,B,Red,18:00.0,,,,"6:00.0 (6:00.0),12:00.0 (6:00.0)",x')
        self.assertEqual(len(f), 12)
        self.assertEqual(f[10], '"6:00.0 (6:00.0),12:00.0 (6:00.0)"')

    def test_empty_entry_takes_no_slot(self):
        r = mp.parse_row(mp.fields('1,7,,A,B,Red,18:00.0,,,,",12:00.0 (12:00.0),18:00.0 (6:00.0)"'))
        self.assertEqual([e["total"] for e in r["split_array"]], ["12:00.0", "18:00.0"])

    def test_unquoted_spilled_splits(self):
        r = mp.parse_row(mp.fields("1,7,,A,B,Red,18:00.0,,,,6:00.0 (6:00.0),12:00.0 (6:00.0),09:18:00"))
        self.assertEqual([e["total"] for e in r["split_array"]], ["6:00.0", "12:00.0"])

    def test_file_names(self):
        self.assertTrue(mp.ms_live_accepts_file("002-1-01.lif"))
        self.assertFalse(mp.ms_live_accepts_file("002-1-02.lif"))     # heat > 1 retired in 2.3.135
        self.assertFalse(mp.ms_live_accepts_file("002-1-01.LIF"))     # path B regex is case-sensitive


class TestFinishLynxOwnFile(unittest.TestCase):
    """FinishLynx's finish-only .lif (fixture from 2026-09-09) - what MeetPro
    sees once FinishLynx owns the file (TestStreamOwnership)."""

    def setUp(self):
        with open(FL_FIXTURE, newline="") as f:
            self.text = f.read()

    def test_ms_live_sends_no_splits(self):
        sent = mp.ms_live_sync("001-1-01.lif", self.text, POINTS, {"School A"})
        self.assertTrue(sent)
        self.assertTrue(all(v["splits"] == {} for v in sent.values()))

    def test_auto_pull_files_the_finish_as_mile_1(self):
        """HAZARD: with split points configured, Splits Auto-Pull stores
        FinishLynx's lone 'finish (finish)' entry as Mile 1."""
        got = mp.splits_auto_pull(self.text, POINTS)
        self.assertEqual(got[1836]["Mile 1"], "18:13.7")


class TestStreamedFileThroughMeetPro(unittest.TestCase):

    def setUp(self):
        self.mgr, self.key, self.name, self.text = streamed()

    def test_name_is_picked_up_by_both_paths(self):
        self.assertEqual(self.name, "002-1-01.lif")
        self.assertTrue(mp.ms_live_accepts_file(self.name))

    def test_complete_athlete_on_both_paths(self):
        a = mp.splits_auto_pull(self.text, POINTS)[1]
        b = mp.ms_live_sync(self.name, self.text, POINTS, TEAMS)[1]
        self.assertEqual((a["Mile 1"], a["Mile 2"]), ("6:00.000", "12:10.250"))
        self.assertEqual(b["splits"], {"Mile 1": "6:00.000", "Mile 2": "12:10.250"})

    def test_late_delivered_split_lands_right(self):
        b = mp.ms_live_sync(self.name, self.text, POINTS, TEAMS)[3]
        self.assertEqual(b["splits"], {"Mile 1": "6:05.000", "Mile 2": "12:25.000"})

    def test_finish_entry_is_not_sent_as_a_split(self):
        for bib, v in mp.ms_live_sync(self.name, self.text, POINTS, TEAMS).items():
            self.assertNotIn(v["result"], v["splits"].values(), bib)

    def test_teams_match_so_athletes_score(self):
        sent = mp.ms_live_sync(self.name, self.text, POINTS, TEAMS)
        self.assertTrue(all(v["team_scored"] for v in sent.values()))

    def test_missed_mile_1_is_never_filed_under_another_point(self):
        """Bib 2 missed Mile 1: nothing of theirs is filed as Mile 1 or
        Mile 2 until a Mile 1 exists; then both land right."""
        a = mp.splits_auto_pull(self.text, POINTS)[2]
        b = mp.ms_live_sync(self.name, self.text, POINTS, TEAMS)[2]
        self.assertEqual({k: v for k, v in a.items() if k != "_status"}, {})
        self.assertEqual(b["splits"], {})
        self.mgr.add_manual_read(self.key, "2", GUN + timedelta(seconds=362), "S1", estimated=True)
        text = self.mgr.lif_content(self.key)
        self.assertEqual(mp.ms_live_sync(self.name, text, POINTS, TEAMS)[2]["splits"],
                         {"Mile 1": "6:02.000", "Mile 2": "12:20.000"})

    def test_on_course_mile_1_reaches_ms_live_before_the_finish(self):
        """An athlete still racing is listed with their splits so far, so
        Mile 1 reaches MileSplit Live before they finish (MeetPro: ENR)."""
        mgr, key = race_with_splits()
        ATHLETES["4"] = {"last_name": "Dunn", "first_name": "Di", "school": "Blue", "gender": "F"}
        try:
            mgr.ingest("4", GUN + timedelta(seconds=400), "S1")
            sent = mp.ms_live_sync("002-1-01.lif", mgr.lif_content(key), POINTS, TEAMS)
        finally:
            del ATHLETES["4"]
        self.assertEqual(sent.get(4, {}).get("splits"), {"Mile 1": "6:40.000"})

    def test_finish_never_lands_in_a_split_slot(self):
        """MeetPro set up with more split points than the console has
        (a Mile 3): Auto-Pull must not store the finish as Mile 3."""
        a = mp.splits_auto_pull(self.text, POINTS + ["Mile 3"])[1]
        self.assertNotIn("Mile 3", a)

    def test_comma_in_school_keeps_the_team(self):
        """'Hammond, SC' is quoted, so MeetPro reads the whole team name
        and the time from the right column."""
        book = dict(ATHLETES)
        book["1"] = dict(book["1"], school="Hammond, SC")
        mgr, key = race_with_splits(make_mgr(book))
        b = mp.ms_live_sync("002-1-01.lif", mgr.lif_content(key), POINTS, TEAMS | {"Hammond, SC"})[1]
        self.assertTrue(b["team_scored"])
        self.assertEqual(b["splits"], {"Mile 1": "6:00.000", "Mile 2": "12:10.250"})


class TestHazardsDocumented(unittest.TestCase):
    """Behaviour of MeetPro that the console has to live with (passing)."""

    def test_athlete_without_a_school_never_reaches_ms_live(self):
        book = dict(ATHLETES)
        book["1"] = dict(book["1"], school="")
        mgr, key = race_with_splits(make_mgr(book))
        self.assertNotIn(1, mp.ms_live_sync("002-1-01.lif", mgr.lif_content(key), POINTS, TEAMS))

    def test_heat_2_pane_is_ignored_by_ms_live(self):
        mgr = make_mgr()
        key = mgr.create_race("Girls 5K B", event=2, heat=2, gender="F")
        self.assertFalse(mp.ms_live_accepts_file(mgr.lif_name(mgr.races[key])))

    def test_auto_pull_never_corrects_a_stored_split(self):
        """A Mile 1 fixed in the console after Auto-Pull stored it stays
        wrong in MeetPro's local results (write-once per athlete+point)."""
        mgr, key, name, text = streamed()
        stored = {(1, p): t for p, t in mp.splits_auto_pull(text, POINTS)[1].items() if p != "_status"}
        read_id = next(r.id for r in mgr.reads.values() if r.bib == "1" and r.point == "S1")
        mgr.override_time(read_id, GUN + timedelta(seconds=355))
        again = mp.splits_auto_pull(mgr.lif_content(key), POINTS, stored)[1]
        self.assertEqual(again["Mile 1"], "6:00.000")

    def test_both_console_writers_give_meetpro_the_same_splits(self):
        mgr, key, name, text = streamed()
        race = {"event": 2, "round": 1, "heat": 1, "name": "Girls 5K", "distance": "5000",
                "gun": GUN.isoformat()}
        tool = load_tool("write_lif").build(race, mgr.results(key), splits=True)
        self.assertEqual(mp.ms_live_sync(name, tool, POINTS, TEAMS),
                         mp.ms_live_sync(name, text, POINTS, TEAMS))


if __name__ == "__main__":
    unittest.main()
