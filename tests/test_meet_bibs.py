import json
import unittest

from rfid_finishlynx.meet import bibs
from rfid_finishlynx.meet.model import Athlete, Entry, Event, Meet, MeetInfo, Race, Team


def make_meet() -> Meet:
    """Two teams plus one athlete with no team; synthetic names only."""
    m = Meet(meet=MeetInfo(id="2026-10-10-bibs", name="Bib Test", date="2026-10-10"))
    m.teams = [Team("t1", "Valley High School", short="Valley", code="VAL"),
               Team("t2", "Ridge High School", short="Ridge", code="RID")]
    m.athletes = [
        Athlete("a1", "Quinn", "Zephyr", "F", team_id="t1", grad_year=2027),   # grade 12
        Athlete("a2", "Avery", "Marsh", "M", team_id="t1", grad_year=2030),    # 9
        Athlete("a3", "Blake", "Alder", "F", team_id="t2", grad_year=2028),    # 11
        Athlete("a4", "Casey", "Alder", "M", team_id="t2", grad_year=2029),    # 10
        Athlete("a5", "Drew", "Birch", "M", team_id="t2", grade_text="Senior"),
        Athlete("a6", "Ellis", "Cove", "F", team_id=None, grad_year=2028),
    ]
    m.events = [Event(1, "Boys 5k Run CC Varsity", gender="M", distance_m=5000),
                Event(2, "Girls 5k Run CC Varsity", gender="F", distance_m=5000)]
    m.races = [Race(1, order=2, entries=[Entry("a2", 1), Entry("a4", 1),
                                         Entry("a5", 1, status="scratched")]),
               Race(2, order=1, entries=[Entry("a1", 2), Entry("a3", 2, status="exhibition")])]
    m.bib_ranges = [[100, 199]]
    return m


def bib_of(m: Meet) -> dict:
    return {a.id: a.bib for a in m.athletes}


class TestRanges(unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(bibs.parse_ranges("100-399, 500-"), [[100, 399], [500, None]])
        self.assertEqual(bibs.parse_ranges("1001-1500"), [[1001, 1500]])
        self.assertEqual(bibs.parse_ranges(" 100 - 199 ;250\n300 to 310, 900+ "),
                         [[100, 199], [250, 250], [300, 310], [900, None]])
        self.assertEqual(bibs.parse_ranges("100–199"), [[100, 199]])
        # An open range may start below an earlier one; it skips that one.
        self.assertEqual(bibs.parse_ranges("1000-1099, 100-"), [[1000, 1099], [100, None]])

    def test_parse_errors(self):
        for text, want in [("", "at least one"), ("abc", "not a range"),
                           ("100-199 500-", "not a range"), ("100-199, 150-250", "overlaps"),
                           ("200-100", "below start"), ("500-, 100-199", "only the last"),
                           ("0-10", "1 to 99999"), ("99990-100000", "1 to 99999"),
                           ("100-199, 150-", "starts inside"), ("5+6", "not a range"),
                           ("100-199, 199", "overlaps")]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as cm:
                    bibs.parse_ranges(text)
                self.assertIn(want, str(cm.exception))

    def test_format_round_trip_and_set_ranges(self):
        text = "100-399, 450, 500-"
        self.assertEqual(bibs.format_ranges(bibs.parse_ranges(text)), "100-399, 450, 500-")
        m = make_meet()
        bibs.set_ranges(m, "300-349, 400-")
        self.assertEqual(m.bib_ranges, [[300, 349], [400, None]])
        self.assertEqual(m.validate(), [])
        bibs.set_ranges(m, "  ")
        self.assertEqual(m.bib_ranges, [])

    def test_sequence(self):
        self.assertEqual(bibs.bib_sequence("5-6, 1-", 6), [5, 6, 1, 2, 3, 4])
        self.assertEqual(bibs.bib_sequence("1-5", 10, exclude={2, 4}), [1, 3, 5])
        self.assertEqual(bibs.bib_sequence([[10, 12]], 2), [10, 11])
        self.assertEqual(bibs.summarize_bibs([5, 1, 2, 3, "9"]), "1-3, 5, 9")


class TestAssign(unittest.TestCase):
    def test_default_sort_team_gender_last_first(self):
        m = make_meet()
        rep = bibs.assign(m)
        # Ridge before Valley, boys before girls, unattached last.
        self.assertEqual([aid for aid, _ in rep.assigned], ["a4", "a5", "a3", "a2", "a1", "a6"])
        self.assertEqual([b for _, b in rep.assigned], ["100", "101", "102", "103", "104", "105"])
        self.assertTrue(rep.applied)
        self.assertEqual(rep.bibs, "100-105")
        self.assertEqual(rep.unassigned, 0)
        self.assertEqual(m.validate(), [])
        self.assertTrue(bibs.conflicts(m).ok)

    def test_operator_sort_orders(self):
        m = make_meet()
        rep = bibs.assign(m, sort=("grade", "last"), dry_run=True)
        # 9, 10, 11, 12 (a1 and Senior a5: then last name), unattached a6 grade 11
        self.assertEqual([aid for aid, _ in rep.assigned], ["a2", "a4", "a3", "a6", "a5", "a1"])
        rep = bibs.assign(m, sort=("race", "gender", "last"), dry_run=True)
        # girls race runs first; a5 is scratched and a6 entered nowhere -> last
        self.assertEqual([aid for aid, _ in rep.assigned], ["a3", "a1", "a4", "a2", "a5", "a6"])
        for bad in [("team", "shoe"), ("last", "last")]:
            with self.assertRaises(ValueError):
                bibs.assign(m, sort=bad)

    def test_retain_existing_avoids_held_bibs(self):
        m = make_meet()
        m.athlete("a4").bib = "100"
        m.athlete("a1").bib = "0102"          # the reader reports 102
        rep = bibs.assign(m)
        got = bib_of(m)
        self.assertEqual(got["a4"], "100")
        self.assertEqual(got["a1"], "0102")
        self.assertEqual({got[a] for a in ("a5", "a3", "a2", "a6")}, {"101", "103", "104", "105"})
        self.assertEqual({s["athlete_id"]: s["reason"] for s in rep.skipped},
                         {"a4": "has bib", "a1": "has bib"})

    def test_renumber_selection_keeps_others(self):
        m = make_meet()
        bibs.assign(m)
        m.athlete("a6").bib = "150"
        rep = bibs.assign(m, retain_existing=False, athlete_ids=["a1", "a2"],
                          ranges="100-199", sort=("first",))
        # Valley's two get the lowest free bibs: their own 103/104 are released, held ones avoided
        self.assertEqual(rep.assigned, [("a2", "103"), ("a1", "104")])
        self.assertEqual(bib_of(m)["a6"], "150")
        self.assertEqual(m.validate(), [])

    def test_renumber_that_does_not_fit_changes_nothing(self):
        m = make_meet()
        bibs.assign(m)
        before = bib_of(m)
        rep = bibs.assign(m, retain_existing=False, ranges="100-102")
        self.assertFalse(rep.applied)
        self.assertEqual(rep.unassigned, 3)
        self.assertEqual(bib_of(m), before)
        self.assertIn("nothing was changed", rep.warnings[-1])

    def test_athlete_ids_limit_and_unknown(self):
        m = make_meet()
        rep = bibs.assign(m, athlete_ids=["a6", "a99"])
        self.assertEqual(rep.assigned, [("a6", "100")])
        self.assertEqual(rep.warnings, ["unknown athlete id a99"])
        self.assertEqual(sum(1 for b in bib_of(m).values() if b), 1)

    def test_entered_only(self):
        m = make_meet()
        rep = bibs.assign(m, entered_only=True)
        self.assertEqual(sorted(aid for aid, _ in rep.assigned), ["a1", "a2", "a3", "a4"])
        self.assertEqual(sorted(s["athlete_id"] for s in rep.skipped if s["reason"] == "not entered"),
                         ["a5", "a6"])          # scratched only / entered nowhere

    def test_skip(self):
        m = make_meet()
        rep = bibs.assign(m, skip="101-102, 104")
        self.assertEqual([b for _, b in rep.assigned], ["100", "103", "105", "106", "107", "108"])
        rep = bibs.assign(make_meet(), skip=[100, "101"], dry_run=True)
        self.assertEqual(rep.assigned[0][1], "102")
        with self.assertRaises(ValueError):
            bibs.assign(make_meet(), skip="100-")

    def test_exhausted_ranges_report_not_overflow(self):
        m = make_meet()
        m.bib_ranges = [[100, 103]]
        rep = bibs.assign(m)
        self.assertEqual(len(rep.assigned), 4)
        self.assertEqual(rep.unassigned, 2)
        self.assertEqual(rep.unassigned_ids, ["a1", "a6"])
        self.assertTrue(rep.applied)
        self.assertIn("2 athletes could not get a bib", rep.warnings[0])
        self.assertTrue(all(100 <= int(b) <= 103 for b in bib_of(m).values() if b))
        # add a range and assign again: the rest fill in, nobody renumbered
        before = {k: v for k, v in bib_of(m).items() if v}
        bibs.set_ranges(m, "100-103, 200-")
        rep = bibs.assign(m)
        self.assertEqual(rep.assigned, [("a1", "200"), ("a6", "201")])
        self.assertEqual({k: v for k, v in bib_of(m).items() if k in before}, before)
        self.assertEqual(m.validate(), [])

    def test_dry_run_changes_nothing(self):
        m = make_meet()
        rep = bibs.assign(m, dry_run=True)
        self.assertEqual(len(rep.assigned), 6)
        self.assertFalse(rep.applied)
        self.assertEqual(set(bib_of(m).values()), {""})

    def test_explicit_ranges_are_not_stored_but_warned(self):
        m = make_meet()
        rep = bibs.assign(m, ranges="900-", athlete_ids=["a6"])
        self.assertEqual(rep.assigned, [("a6", "900")])
        self.assertEqual(m.bib_ranges, [[100, 199]])
        self.assertIn("outside the meet's ranges", rep.warnings[0])

    def test_no_ranges_is_an_error(self):
        m = make_meet()
        m.bib_ranges = []
        with self.assertRaises(ValueError):
            bibs.assign(m)

    def test_report_is_json(self):
        m = make_meet()
        rep = bibs.assign(m, per_team_blocks="round10", entered_only=True)
        again = json.loads(json.dumps(rep.to_dict()))
        self.assertEqual(again["assigned"][0], ["a4", "100"])
        for key in ("assigned", "skipped", "unassigned", "unassigned_ids", "warnings",
                    "blocks", "bibs", "applied", "dry_run"):
            self.assertIn(key, again)
        json.dumps(bibs.conflicts(m).to_dict())
        json.dumps(bibs.clear(m, ["a4"]).to_dict())
        json.dumps(bibs.set_bib(m, "a4", "100").to_dict())
        json.dumps(bibs.team_bib_report(m))


class TestPerTeamBlocks(unittest.TestCase):
    def test_fixed_blocks(self):
        m = make_meet()
        rep = bibs.assign(m, per_team_blocks=10)
        self.assertEqual([(b["team"], b["start"], b["end"], b["used"]) for b in rep.blocks],
                         [("Ridge High School", 100, 109, 3), ("Valley High School", 110, 119, 2),
                          ("Unattached", 120, 129, 1)])
        self.assertEqual(bib_of(m), {"a4": "100", "a5": "101", "a3": "102",
                                     "a2": "110", "a1": "111", "a6": "120"})
        self.assertEqual(m.validate(), [])

    def test_team_bigger_than_block(self):
        m = make_meet()
        rep = bibs.assign(m, per_team_blocks=2)
        self.assertEqual(rep.unassigned_ids, ["a3"])
        self.assertTrue(any("3 athletes but blocks are 2" in w for w in rep.warnings))

    def test_rounded_blocks_avoid_held_and_stay_in_one_range(self):
        m = make_meet()
        for i in range(7, 20):                 # Ridge grows to 16 athletes
            m.athletes.append(Athlete(f"a{i}", f"Runner{i}", f"Synth{i:02d}", "M", team_id="t2"))
        m.athletes.append(Athlete("a20", "Held", "Bib", "F", team_id="t1", bib="125"))
        m.bib_ranges = [[100, 129], [300, None]]
        rep = bibs.assign(m, per_team_blocks="round10")
        blocks = {b["team"]: (b["start"], b["end"]) for b in rep.blocks}
        self.assertEqual(blocks["Ridge High School"], (100, 119))   # 16 -> 20
        # 120-129 holds 125, so Valley's 10-block moves to the next range
        self.assertEqual(blocks["Valley High School"], (300, 309))
        self.assertEqual(blocks["Unattached"], (310, 319))
        self.assertEqual(m.validate(), [])
        rep = bibs.assign(make_meet(), per_team_blocks="round25", dry_run=True)
        self.assertEqual([b["start"] for b in rep.blocks], [100, 125, 150])

    def test_no_room_for_a_block(self):
        m = make_meet()
        m.bib_ranges = [[100, 114]]
        rep = bibs.assign(m, per_team_blocks=10)
        self.assertEqual(rep.unassigned, 3)          # Valley (2) + Unattached (1)
        self.assertEqual(len(rep.blocks), 1)
        with self.assertRaises(ValueError):
            bibs.assign(make_meet(), per_team_blocks="round7")
        with self.assertRaises(ValueError):
            bibs.assign(make_meet(), per_team_blocks=True)


class TestClearAndEdit(unittest.TestCase):
    def test_clear(self):
        m = make_meet()
        bibs.assign(m)
        rep = bibs.clear(m, ["a1", "a2", "a77"])
        self.assertEqual(sorted(rep.cleared), [("a1", "104"), ("a2", "103")])
        self.assertEqual(rep.warnings, ["unknown athlete id a77"])
        self.assertEqual(bib_of(m)["a3"], "102")
        rep = bibs.clear(m)
        self.assertEqual(len(rep.cleared), 4)
        self.assertEqual(set(bib_of(m).values()), {""})

    def test_set_bib(self):
        m = make_meet()
        bibs.assign(m)
        r = bibs.set_bib(m, "a6", "150")
        self.assertTrue(r.ok)
        self.assertEqual((r.previous, r.bib, r.warnings), ("105", "150", []))
        # duplicate: refused, unchanged
        r = bibs.set_bib(m, "a6", "100")
        self.assertFalse(r.ok)
        self.assertIn("already on Casey Alder (a4)", r.error)
        self.assertEqual(m.athlete("a6").bib, "150")
        r = bibs.set_bib(m, "a6", "0100")          # same tag number
        self.assertFalse(r.ok)
        # out of range: warned, applied
        r = bibs.set_bib(m, "a6", 950)
        self.assertTrue(r.ok)
        self.assertIn("outside the meet's ranges", r.warnings[0])
        # leading zeros normalised
        r = bibs.set_bib(m, "a6", "0151")
        self.assertEqual((r.ok, r.bib), (True, "151"))
        self.assertIn("stored as 151", r.warnings[0])
        for bad in ("A12", "0", "100000", "12 3"):
            r = bibs.set_bib(m, "a6", bad)
            self.assertFalse(r.ok, bad)
            self.assertEqual(m.athlete("a6").bib, "151")
        r = bibs.set_bib(m, "a6", "")
        self.assertEqual((r.ok, m.athlete("a6").bib), (True, ""))
        self.assertFalse(bibs.set_bib(m, "a99", "5").ok)
        self.assertEqual(m.validate(), [])

    def test_final_race_warns(self):
        m = make_meet()
        bibs.assign(m)
        m.race(2).status = "final"
        r = bibs.set_bib(m, "a1", "160")
        self.assertTrue(r.ok)
        self.assertTrue(any("went final" in w for w in r.warnings))
        self.assertTrue(any("went final" in w for w in bibs.clear(m, ["a3"]).warnings))


class TestConflictsAndReport(unittest.TestCase):
    def test_conflicts(self):
        m = make_meet()
        m.athlete("a1").bib = "101"
        m.athlete("a2").bib = "0101"
        m.athlete("a3").bib = "x7"
        m.athlete("a6").bib = "500"
        c = bibs.conflicts(m)
        self.assertFalse(c.ok)
        self.assertEqual(c.duplicates, [{"bib": "101", "athlete_ids": ["a1", "a2"]}])
        self.assertEqual([(i["athlete_id"], i["bib"]) for i in c.invalid], [("a2", "0101"), ("a3", "x7")])
        self.assertEqual(c.out_of_range, [{"athlete_id": "a6", "bib": "500"}])
        # a4 runs without a bib; a5 is scratched (not missing); a3 has a (bad) bib
        self.assertEqual(c.missing, [{"athlete_id": "a4", "races": [[1, 1, 1]]}])
        m.bib_ranges = []                          # no ranges: nothing is out of range
        self.assertEqual(bibs.conflicts(m).out_of_range, [])

    def test_team_bib_report(self):
        m = make_meet()
        bibs.assign(m, athlete_ids=["a3", "a4", "a1", "a6"])
        m.athlete("a3").bib = "190"
        rep = bibs.team_bib_report(m)
        self.assertEqual([t["team"] for t in rep],
                         ["Ridge High School", "Valley High School", "Unattached"])
        ridge = rep[0]
        self.assertEqual((ridge["short"], ridge["count"], ridge["without_bib"]), ("Ridge", 3, 1))
        self.assertEqual(ridge["rows"], [("100", "Alder", "Casey", "M", "10"),
                                         ("190", "Alder", "Blake", "F", "11"),
                                         ("", "Birch", "Drew", "M", "Senior")])
        self.assertEqual(rep[2]["rows"], [("103", "Cove", "Ellis", "F", "11")])
        self.assertIsNone(rep[2]["team_id"])


if __name__ == "__main__":
    unittest.main()


class TestDialogRowsSwapAndFullRosters(unittest.TestCase):
    """The bib dialog's needs (2026-10-03): the roster tool's Start/End rows,
    Swap bibs from the grid, and entered-only when full rosters came in."""

    def meet(self, full_rosters=False):
        m = Meet(meet=MeetInfo(id="m", name="T", date="2026-10-10", full_rosters=full_rosters))
        m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID")]
        m.events = [Event(1, "Girls 5k Run CC Varsity", gender="F", distance_m=5000)]
        m.athletes = [Athlete(f"a{i}", f"F{i}", f"L{i}", "F", team_id="t1") for i in range(1, 6)]
        m.races = [Race(1, order=1, entries=[Entry("a1", 1), Entry("a2", 1),
                                             Entry("a3", 1, status="scratched")])]
        for a in m.athletes[3:]:
            a.roster_only = full_rosters                   # as the roster import marks them
        return m

    def test_rows_like_the_roster_tool(self):
        self.assertEqual(bibs.parse_range_rows([("101", "199"), ("", ""), ("450", "460"),
                                                (" 500 ", "")]),
                         [[101, 199], [450, 460], [500, None]])
        self.assertEqual(bibs.parse_range_rows([("250", "250")]), [[250, 250]])
        for rows, msg in [([], "at least one"),
                          ([("", "10")], "Start box is empty"),
                          ([("1a", "")], "not a whole number"),
                          ([("100", ""), ("300", "399")], "only the last range"),
                          ([("100", "199"), ("150", "160")], "overlaps")]:
            with self.assertRaises(ValueError) as cm:
                bibs.parse_range_rows(rows)
            self.assertIn(msg, str(cm.exception))

    def test_swap(self):
        m = self.meet()
        m.athletes[0].bib, m.athletes[1].bib = "406", "431"
        res = bibs.swap_bibs(m, "a1", "a2")
        self.assertTrue(res.ok)
        self.assertEqual((m.athletes[0].bib, m.athletes[1].bib), ("431", "406"))
        self.assertEqual(m.validate(), [])
        res = bibs.swap_bibs(m, "a1", "a5")                 # other side has none: a move
        self.assertEqual((m.athletes[0].bib, m.athletes[4].bib), ("", "431"))
        self.assertFalse(bibs.swap_bibs(m, "a1", "a1").ok)
        self.assertFalse(bibs.swap_bibs(m, "a1", "nope").ok)

    def test_full_rosters_numbers_only_entered_athletes(self):
        m = self.meet(full_rosters=True)
        bibs.set_ranges(m, "100-")
        rep = bibs.assign(m, entered_only=False)            # asked otherwise: still enforced
        self.assertEqual({a.id for a in m.athletes if a.bib}, {"a1", "a2"})
        self.assertTrue(any("full rosters" in w for w in rep.warnings))
        res = bibs.set_bib(m, "a4", "300")                  # by hand: allowed, warned
        self.assertTrue(res.ok)
        self.assertTrue(any("not entered" in w for w in res.warnings))

    def test_entered_only_off_by_default_without_full_rosters(self):
        m = self.meet()
        bibs.set_ranges(m, "100-")
        bibs.assign(m)
        self.assertEqual(sum(1 for a in m.athletes if a.bib), 5)


class TestNextFree(unittest.TestCase):
    """bibs.next_free (2026-10-03): what the console offers as "next free"."""

    def test_first_number_nobody_holds(self):
        m = make_meet()
        self.assertEqual(bibs.next_free(m), 100)
        m.athlete("a1").bib, m.athlete("a2").bib = "100", "101"
        self.assertEqual(bibs.next_free(m), 102)
        self.assertEqual(bibs.next_free(m, skip=["102", 103]), 104)

    def test_ranges_in_order_and_used_up(self):
        m = make_meet()
        m.bib_ranges = [[100, 101], [500, None]]
        m.athlete("a1").bib, m.athlete("a2").bib = "100", "101"
        self.assertEqual(bibs.next_free(m), 500)
        m.bib_ranges = [[100, 101]]
        self.assertIsNone(bibs.next_free(m))
        m.bib_ranges = []
        self.assertIsNone(bibs.next_free(m))                 # no ranges: nothing to offer

    def test_agrees_with_assign(self):
        m = make_meet()
        m.athlete("a1").bib = "100"
        want = bibs.next_free(m)
        bibs.assign(m, athlete_ids=["a2"])
        self.assertEqual(m.athlete("a2").bib, str(want))
