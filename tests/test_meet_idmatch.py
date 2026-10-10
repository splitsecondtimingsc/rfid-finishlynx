import json
import os
import unittest

from rfid_finishlynx.meet import idmatch
from rfid_finishlynx.meet.idmatch import (Candidate, CandidatePool, IdConflict, approve,
                                          coverage_by_race, decline, grad_year_of, id_coverage,
                                          match_meet, norm, reopen, review_queue)
from rfid_finishlynx.meet.model import Athlete, Entry, Event, Meet, MeetInfo, Race, Team

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")


def ms_athlete(ms_id, first, last, year, gender="f", **extra):
    a = {"athleteFirstName": first, "athleteLastName": last, "athleteMileSplitId": ms_id,
         "athleteYear": year, "athleteGender": gender,
         "athleteMileSplitUniversalId": f"U{ms_id}", "entries": []}
    a.update(extra)
    return a


def entries_pull():
    """Synthetic entries pull: two attending teams (names invented)."""
    return [
        {"teamName": "Ridge High School", "teamMileSplitId": 900001, "roster": [
            ms_athlete(7001, "Avery", "Birch", 2030),
            ms_athlete(7002, "Zoë", "O'Neil-Park", 2028),
            ms_athlete(7003, "William", "Thorne", 2029, gender="m"),
            ms_athlete(7004, "Casey", "Lindqvist", 2027),
            ms_athlete(7005, "Casey", "Lindqvist", 2029),   # same name, same school
            ms_athlete(7006, "Jordan", "Pike", 2028),
        ]},
        {"teamName": "Valley High School", "teamMileSplitId": 900002, "roster": [
            ms_athlete(7101, "Morgan", "Hale", 2028),
            ms_athlete(7102, "William", "Thornbury", 2029, gender="m"),
        ]},
    ]


def new_meet() -> Meet:
    m = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10"))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID", milesplit_id=900001),
               Team("t2", "Valley High School", short="Valley", code="VAL", milesplit_id=900002),
               Team("t3", "Harbor High School", short="Harbor", code="HAR")]  # no MileSplit id
    m.events = [Event(1, "Girls 5k Run CC Varsity", gender="F", distance_m=5000)]
    m.races = [Race(1, order=1)]
    return m


def walk_up(m: Meet, first, last, team_id, gender="F", **kw) -> Athlete:
    a = Athlete(m.new_athlete_id(), first, last, gender, team_id=team_id, source="local", **kw)
    m.athletes.append(a)
    m.races[0].entries.append(Entry(a.id, 1))
    return a


def pool_for(m: Meet, entries=None, rosters=None) -> CandidatePool:
    return CandidatePool.from_milesplit(m, entries if entries is not None else entries_pull(),
                                        rosters)


class TestNormalisation(unittest.TestCase):
    def test_norm_folds_case_accents_punctuation(self):
        self.assertEqual(norm(" O'Néil-Park "), "oneilpark")
        self.assertEqual(norm("Zoë"), norm("zoe"))
        self.assertEqual(norm(None), "")

    def test_grade_text_converts_with_the_season(self):
        m = new_meet()                                  # fall 2026 -> season ends 2027
        for text, year in (("9", 2030), (" 12", 2027), ("09", 2030), ("Sr", 2027),
                           ("Freshman", 2030), ("10th", 2029)):
            a = Athlete("x", "A", "B", "F", grade_text=text)
            self.assertEqual(grad_year_of(m, a), year, text)
        self.assertIsNone(grad_year_of(m, Athlete("x", "A", "B", "F", grade_text="Senior?")))
        self.assertIsNone(grad_year_of(m, Athlete("x", "A", "B", "F")))
        self.assertEqual(grad_year_of(m, Athlete("x", "A", "B", "F", grad_year=2031,
                                                 grade_text="9")), 2031)  # stored year wins
        m.meet.date = ""
        self.assertIsNone(grad_year_of(m, Athlete("x", "A", "B", "F", grade_text="9")))


class TestPool(unittest.TestCase):
    def test_maps_teams_by_id_then_exact_name(self):
        m = new_meet()
        rosters = {"data": [
            {"teamName": "Harbor High School", "teamId": 900003, "roster": [
                {"athleteFirstName": "Rowan", "athleteLastName": "Vale",
                 "athleteMileSplitId": "7201", "athleteYear": "2028"}]},
            {"teamName": "harbor high school", "roster": [      # name not exact: unmapped
                {"athleteFirstName": "Sky", "athleteLastName": "Moss",
                 "athleteMileSplitId": 7202}]},
            {"teamName": "Ridge HS", "teamMileSplitId": 900001, "roster": [   # id wins
                {"athleteFirstName": "Ash", "athleteLastName": "Fen", "athleteMileSplitId": 7203},
                {"athleteFirstName": "No", "athleteLastName": "Id"}]},     # skipped
        ]}
        pool = pool_for(m, entries=[], rosters=rosters)
        got = {c.milesplit_id: c for c in pool.candidates}
        self.assertEqual(sorted(got), [7201, 7202, 7203])
        self.assertEqual(got[7201].team_id, "t3")
        self.assertEqual(got[7201].grad_year, 2028)          # string year parsed
        self.assertEqual(got[7201].team_milesplit_id, 900003)
        self.assertIsNone(got[7202].team_id)
        self.assertEqual(got[7203].team_id, "t1")
        self.assertEqual(got[7203].gender, "")
        self.assertEqual(got[7203].source, "roster")

    def test_entries_and_roster_merge_into_one_candidate(self):
        m = new_meet()
        entries = [{"teamName": "Ridge High School", "teamMileSplitId": 900001, "roster": [
            {"athleteFirstName": "Avery", "athleteLastName": "Birch",
             "athleteMileSplitId": 7001, "athleteYear": None}]}]
        rosters = {"data": {"teams": [{"teamName": "Ridge High School", "teamId": 900001,
                                       "roster": [ms_athlete(7001, "Avery", "Birch", 2030)]}]}}
        pool = pool_for(m, entries, rosters)
        self.assertEqual(len(pool), 1)
        c = pool.candidates[0]
        self.assertEqual((c.source, c.grad_year, c.universal_id), ("entries", 2030, "U7001"))

    def test_same_athlete_on_two_teams_is_two_candidates(self):
        m = new_meet()
        pull = entries_pull()
        pull[1]["roster"].append(ms_athlete(7001, "Avery", "Birch", 2030))
        self.assertEqual(sum(c.milesplit_id == 7001 for c in pool_for(m, pull).candidates), 2)

    def test_tolerates_junk(self):
        m = new_meet()
        for raw in (None, [], {}, {"data": None}, [None, "x", {"roster": None}]):
            self.assertEqual(len(CandidatePool.from_milesplit(m, raw, raw)), 0, raw)


class TestRule(unittest.TestCase):
    def run_one(self, *args, pull=None, **kw):
        m = new_meet()
        a = walk_up(m, *args, **kw)
        rep = match_meet(m, pool_for(m, pull))
        return m, a, rep

    def test_exact_match_auto_attaches(self):
        m, a, rep = self.run_one("Avery", "Birch", "t1", grad_year=2030)
        self.assertEqual((a.milesplit_id, a.universal_id, a.ms_match), (7001, "U7001", "auto"))
        self.assertEqual(rep.auto, [a.id])
        self.assertEqual(rep.queue, [])
        self.assertEqual(m.validate(), [])

    def test_exact_with_grade_text_and_padding(self):
        _, a, _ = self.run_one("  Avery ", "Birch", "t1", grade_text="9")
        self.assertEqual((a.milesplit_id, a.ms_match), (7001, "auto"))

    def test_case_only_difference_goes_to_review(self):
        _, a, rep = self.run_one("avery", "birch", "t1", grad_year=2030)
        self.assertEqual((a.milesplit_id, a.ms_match), (None, "pending"))
        item = rep.queue[0].to_dict()
        self.assertEqual(item["reasons"], ["name_normalized"])
        c = item["candidates"][0]
        self.assertEqual(c["fields"], {"first": "near", "last": "near", "grade": "match",
                                       "team": "match", "gender": "match"})
        self.assertEqual(c["mismatches"], ["first", "last"])

    def test_accent_and_punctuation_go_to_review(self):
        _, a, rep = self.run_one("Zoe", "ONeil Park", "t1", grad_year=2028)
        self.assertEqual(a.ms_match, "pending")
        self.assertEqual(rep.queue[0].candidates[0]["milesplit_id"], 7002)

    def test_grade_differs_goes_to_review(self):
        _, a, rep = self.run_one("Avery", "Birch", "t1", grad_year=2029)
        self.assertEqual(a.ms_match, "pending")
        item = rep.queue[0]
        self.assertEqual(item.reasons, ["grade_differs"])
        self.assertEqual(item.candidates[0]["grade"], "9")
        self.assertEqual(item.athlete["grade"], "10")

    def test_unknown_grade_never_auto(self):
        _, a, rep = self.run_one("Avery", "Birch", "t1", grade_text="Senior?")
        self.assertEqual(a.ms_match, "pending")
        self.assertEqual(rep.queue[0].reasons, ["grade_unknown"])

    def test_team_differs_goes_to_review(self):
        _, a, rep = self.run_one("Morgan", "Hale", "t1", grad_year=2028)
        self.assertEqual(a.ms_match, "pending")
        self.assertEqual(rep.queue[0].reasons, ["team_differs"])
        self.assertEqual(rep.queue[0].candidates[0]["team"], "Valley High School")

    def test_unattached_never_auto(self):
        _, a, rep = self.run_one("Avery", "Birch", None, grad_year=2030)
        self.assertEqual(a.ms_match, "pending")
        self.assertIn("team_unknown", rep.queue[0].reasons)

    def test_gender_contradiction_blocks_auto(self):
        _, a, rep = self.run_one("Avery", "Birch", "t1", gender="M", grad_year=2030)
        self.assertEqual(a.ms_match, "pending")
        self.assertEqual(rep.queue[0].reasons, ["gender_differs"])

    def test_two_candidates_with_same_name_and_school(self):
        _, a, rep = self.run_one("Casey", "Lindqvist", "t1", grad_year=2027)
        self.assertEqual((a.milesplit_id, a.ms_match), (None, "pending"))
        item = rep.queue[0]
        self.assertEqual(item.reasons, ["several_candidates"])
        # Ranked by fields matched: the 2027 one (all match) before the 2029 one.
        self.assertEqual([c["milesplit_id"] for c in item.candidates], [7004, 7005])
        self.assertEqual(item.candidates[1]["mismatches"], ["grade"])

    def test_nickname_on_same_team_is_reviewed(self):
        _, a, rep = self.run_one("Will", "Thorne", "t1", gender="M", grad_year=2029)
        self.assertEqual(a.ms_match, "pending")
        item = rep.queue[0]
        self.assertIn("name_nickname", item.reasons)
        self.assertEqual([c["milesplit_id"] for c in item.candidates], [7003])

    def test_nickname_on_other_team_is_not_plausible(self):
        _, a, rep = self.run_one("Will", "Thornbury", "t1", gender="M", grad_year=2029)
        self.assertEqual((a.ms_match, rep.queue, rep.unmatched), ("", [], [a.id]))

    def test_swapped_and_partial_last_names(self):
        _, a, rep = self.run_one("Pike", "Jordan", "t1", grad_year=2028)
        self.assertEqual(rep.queue[0].candidates[0]["relation"], "swapped")
        _, a, rep = self.run_one("Zoë", "Park", "t1", grad_year=2028)
        self.assertEqual(rep.queue[0].candidates[0]["relation"], "partial_last")

    def test_no_candidate_no_queue_item(self):
        _, a, rep = self.run_one("Quinn", "Nobody", "t1", grad_year=2028)
        self.assertEqual((a.ms_match, a.milesplit_id), ("", None))
        self.assertEqual((rep.queue, rep.unmatched), ([], [a.id]))

    def test_id_held_by_teammate_is_a_conflict(self):
        m = new_meet()
        m.athletes.append(Athlete("a1", "Avery", "Birch", "F", team_id="t1", grad_year=2030,
                                  milesplit_id=7001, universal_id="U7001"))
        a = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        rep = match_meet(m, pool_for(m))
        self.assertEqual((a.milesplit_id, a.ms_match), (None, "pending"))
        item = rep.queue[0].to_dict()
        self.assertEqual(item["reasons"], ["id_in_use"])
        self.assertEqual(item["candidates"][0]["conflict"], "a1")
        with self.assertRaises(IdConflict):
            approve(m, a.id, item["candidates"][0])
        self.assertEqual(m.athlete("a1").milesplit_id, 7001)    # untouched

    def test_id_on_another_team_is_not_a_conflict(self):
        m = new_meet()
        m.athletes.append(Athlete("a1", "Avery", "Birch", "F", team_id="t2", milesplit_id=7001))
        a = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        match_meet(m, pool_for(m))
        self.assertEqual((a.milesplit_id, a.ms_match), (7001, "auto"))

    def test_duplicate_walk_ups_dont_both_take_the_id(self):
        m = new_meet()
        a1 = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        a2 = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        rep = match_meet(m, pool_for(m))
        self.assertEqual((a1.ms_match, a2.ms_match), ("auto", "pending"))
        self.assertEqual(rep.queue[0].candidates[0]["conflict"], a1.id)

    def test_athletes_with_ids_are_left_alone(self):
        m = new_meet()
        m.athletes.append(Athlete("a1", "avery", "birch", "F", team_id="t1", milesplit_id=1,
                                  universal_id="X"))
        rep = match_meet(m, pool_for(m))
        self.assertEqual((rep.auto, rep.queue, rep.unmatched), ([], [], []))
        self.assertEqual((m.athletes[0].milesplit_id, m.athletes[0].ms_match), (1, ""))


class TestOperator(unittest.TestCase):
    def test_approve_from_queue_dict_and_rerun_keeps_it(self):
        m = new_meet()
        a = walk_up(m, "Avery", "Birch", "t1", grad_year=2029)
        pool = pool_for(m)
        rep = match_meet(m, pool)
        cand = json.loads(json.dumps(rep.queue[0].to_dict()))["candidates"][0]
        approve(m, a.id, cand)
        self.assertEqual((a.milesplit_id, a.universal_id, a.ms_match), (7001, "U7001", "approved"))
        rep = match_meet(m, pool)
        self.assertEqual((rep.auto, rep.queue, rep.unmatched), ([], [], []))
        self.assertEqual((a.milesplit_id, a.ms_match), (7001, "approved"))
        self.assertEqual(review_queue(m, pool), [])

    def test_approve_accepts_a_candidate_object(self):
        m = new_meet()
        a = walk_up(m, "Avery", "Birch", "t1")
        approve(m, a.id, Candidate(7006, "U7006"))
        self.assertEqual((a.milesplit_id, a.universal_id, a.ms_match), (7006, "U7006", "approved"))
        with self.assertRaises(ValueError):
            approve(m, a.id, {"first": "no id"})
        with self.assertRaises(KeyError):
            approve(m, "a999", Candidate(1))

    def test_decline_sticks_until_reopened(self):
        m = new_meet()
        a = walk_up(m, "avery", "Birch", "t1", grad_year=2030)
        pool = pool_for(m)
        match_meet(m, pool)
        decline(m, a.id)
        self.assertEqual((a.milesplit_id, a.ms_match), (None, "declined"))
        match_meet(m, pool)
        self.assertEqual(a.ms_match, "declined")
        reopen(m, a.id)
        self.assertEqual(a.ms_match, "")
        match_meet(m, pool)
        self.assertEqual(a.ms_match, "pending")

    def test_decline_of_an_auto_clears_its_ids(self):
        m = new_meet()
        a = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        match_meet(m, pool_for(m))
        decline(m, a.id)
        self.assertEqual((a.milesplit_id, a.universal_id, a.ms_match), (None, None, "declined"))

    def test_decline_refuses_imported_ids(self):
        m = new_meet()
        m.athletes.append(Athlete("a1", "A", "B", "F", team_id="t1", milesplit_id=5,
                                  source="milesplit"))
        with self.assertRaises(ValueError):
            decline(m, "a1")

    def test_auto_kept_when_pool_later_grows_a_rival(self):
        m = new_meet()
        a = walk_up(m, "Avery", "Birch", "t1", grad_year=2030)
        match_meet(m, pool_for(m))
        pull = entries_pull()
        pull[0]["roster"].append(ms_athlete(7999, "Avery", "Birch", 2031))
        match_meet(m, pool_for(m, pull))
        self.assertEqual((a.milesplit_id, a.ms_match), (7001, "auto"))

    def test_pending_cleared_when_pool_drops_candidate(self):
        m = new_meet()
        a = walk_up(m, "avery", "birch", "t1", grad_year=2030)
        match_meet(m, pool_for(m))
        self.assertEqual(a.ms_match, "pending")
        rep = match_meet(m, pool_for(m, entries=[]))
        self.assertEqual((a.ms_match, rep.unmatched), ("", [a.id]))

    def test_review_queue_is_pure_and_deterministic(self):
        m = new_meet()
        walk_up(m, "Casey", "Lindqvist", "t1", grad_year=2027)
        walk_up(m, "avery", "birch", "t1", grad_year=2030)
        pool = pool_for(m)
        first = json.dumps(match_meet(m, pool).to_dict(), sort_keys=True)
        before = json.dumps(m.to_dict(), sort_keys=True)
        q = [i.to_dict() for i in review_queue(m, pool)]
        self.assertEqual(json.dumps(m.to_dict(), sort_keys=True), before)
        self.assertEqual(json.loads(first)["queue"], q)
        self.assertEqual(json.dumps(match_meet(m, pool).to_dict(), sort_keys=True), first)
        self.assertEqual(Meet.from_dict(m.to_dict()).validate(), [])


class TestCoverage(unittest.TestCase):
    def test_share_counts_entered_athletes_only(self):
        m = new_meet()
        m.events.append(Event(2, "Girls 5k Run CC JV", gender="F"))
        m.races.append(Race(2, order=2))
        ids = [Athlete(f"a{i}", "F", f"L{i}", "F", team_id="t1",
                       milesplit_id=(i if i <= 2 else None)) for i in range(1, 6)]
        m.athletes = ids
        m.races[0].entries = [Entry("a1", 1), Entry("a3", 1), Entry("a4", 1),
                              Entry("a5", 1, status="scratched")]
        m.races[1].entries = [Entry("a2", 2), Entry("a3", 2)]
        cov = id_coverage(m)
        self.assertEqual((cov.athletes, cov.with_id, cov.ok), (4, 2, True))   # a1,a2 of a1-a4
        per = coverage_by_race(m)
        self.assertEqual(per[(1, 1, 1)].to_dict(),
                         {"athletes": 3, "with_id": 1, "share": 0.3333, "ok": False})
        self.assertTrue(per[(2, 1, 1)].ok)
        m.races[1].entries = []
        self.assertNotIn((2, 1, 1), coverage_by_race(m))
        self.assertEqual(idmatch.Coverage(0, 0).share, 1.0)


class TestFixture(unittest.TestCase):
    """The scrubbed real entries pull: a walk-up re-typed from it attaches."""

    def test_walk_up_from_fixture_entries(self):
        with open(FIXTURE, encoding="utf-8") as f:
            pull = json.load(f)
        m = new_meet()
        m.teams = [Team(f"t{i + 1}", t["teamName"], milesplit_id=t["teamMileSplitId"])
                   for i, t in enumerate(pull)]
        pool = CandidatePool.from_milesplit(m, entries=pull)
        self.assertEqual(len(pool), sum(len(t["roster"]) for t in pull))
        self.assertTrue(all(c.team_id for c in pool.candidates))
        src = pull[2]["roster"][3]
        a = walk_up(m, src["athleteFirstName"], src["athleteLastName"], "t3",
                    gender=src["athleteGender"].upper(), grad_year=int(src["athleteYear"]))
        b = walk_up(m, src["athleteFirstName"].upper(), src["athleteLastName"], "t3",
                    gender=src["athleteGender"].upper(), grad_year=int(src["athleteYear"]))
        rep = match_meet(m, pool)
        self.assertEqual((a.milesplit_id, a.ms_match), (src["athleteMileSplitId"], "auto"))
        self.assertEqual(b.ms_match, "pending")
        json.dumps(rep.to_dict())


if __name__ == "__main__":
    unittest.main()
