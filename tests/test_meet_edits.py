import unittest

from rfid_finishlynx.meet import edits, exports, lynx, printing, scoring
from rfid_finishlynx.meet.model import Athlete, Entry, Event, Meet, MeetInfo, Race, Team


def mixed_meet() -> Meet:
    """One mixed MS 2 Mile race: two teams, 6 girls and 6 boys each."""
    m = Meet(meet=MeetInfo(id="m", name="Mixed Test", date="2026-10-10"))
    m.meet.milesplit.tc_id, m.meet.milesplit.meet_id = 1, 2
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID", milesplit_id=1),
               Team("t2", "Valley High School", short="Valley", code="VAL", milesplit_id=2)]
    m.events = [Event(5, "Mixed 2 Mile Run CC Middle School", gender="X", distance_m=3218,
                      division="Middle School", code="2MILE")]
    race = Race(5, order=1)
    n = 0
    for t in ("t1", "t2"):
        for g in ("F", "M"):
            for _ in range(6):
                n += 1
                m.athletes.append(Athlete(f"a{n}", f"F{n}", f"L{n}", g, team_id=t,
                                          bib=str(100 + n), grad_year=2032, milesplit_id=n))
                race.entries.append(Entry(f"a{n}", 5))
    m.races = [race]
    return m


def rm_for(meet, race):
    """RaceManager.results-shaped finish order: alternate teams and genders."""
    bibs = [a.bib for a in sorted(meet.athletes, key=lambda a: (int(a.id[1:]) % 6, a.id))
            if a.bib]
    return {"finishers": [{"bib": b, "place": i, "finish": {"elapsed": 700.0 + i}, "splits": []}
                          for i, b in enumerate(bibs, 1)],
            "on_course": [], "no_show": [], "expected_points": []}


class TestScoreGendersSeparately(unittest.TestCase):

    def test_split_scores_and_prints_each_gender_apart(self):
        m = mixed_meet()
        race = m.races[0]
        rep = edits.split_genders(m, race)
        self.assertEqual(sorted(rep.events), ["F", "M"])
        self.assertEqual(len(rep.created), 2)
        self.assertEqual(rep.moved, 24)
        self.assertTrue(edits.is_split_by_gender(m, race))
        self.assertEqual(m.event(rep.events["F"]).name, "Girls 2 Mile Run CC Middle School")
        self.assertEqual(m.validate(), [])
        self.assertEqual(edits.split_genders(m, race).moved, 0)       # idempotent

        scoring.freeze(m, race, rm_for(m, race))
        res = race.results
        self.assertEqual(sorted(res["teams"]), sorted(str(n) for n in rep.events.values()))
        girls = [f for f in res["finishers"] if f["event"] == rep.events["F"]]
        self.assertEqual([f["event_place"] for f in girls], list(range(1, 13)))
        text = printing.results_text(m, race)
        self.assertIn("Girls 2 Mile Run CC Middle School", text)
        self.assertIn("Boys 2 Mile Run CC Middle School", text)
        genders = {r["event"]["gender"] for t in exports.mp_results(m, [race])["teams"]
                   for a in t["roster"] for r in a["results"]}
        self.assertEqual(genders, {"f", "m"})

    def test_combine_undoes_the_split(self):
        m = mixed_meet()
        race = m.races[0]
        split = edits.split_genders(m, race)
        rep = edits.combine_genders(m, race)
        self.assertEqual(rep.moved, 24)
        self.assertEqual(sorted(rep.removed), sorted(split.created))
        self.assertEqual([e.num for e in m.events], [5])
        self.assertFalse(edits.is_split_by_gender(m, race))
        self.assertEqual(m.validate(), [])

    def test_final_race_must_be_reopened_first(self):
        m = mixed_meet()
        race = m.races[0]
        scoring.freeze(m, race, rm_for(m, race))
        with self.assertRaises(ValueError):
            edits.split_genders(m, race)
        scoring.reopen(m, race)
        edits.split_genders(m, race)

    def test_existing_one_gender_events_are_reused(self):
        m = mixed_meet()
        m.events.append(Event(6, "Girls 2 Mile Run CC Middle School", gender="F",
                              distance_m=3218, division="Middle School", code="2MILE"))
        rep = edits.split_genders(m, m.races[0])
        self.assertEqual(rep.events["F"], 6)
        self.assertEqual(len(rep.created), 1)


def two_jv_boys_meet() -> Meet:
    """Two JV boys 5k races (events 3 and 4), two teams of 7 in each."""
    m = Meet(meet=MeetInfo(id="m", name="JV Test", date="2026-10-10"))
    m.meet.milesplit.tc_id, m.meet.milesplit.meet_id = 1, 2
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID", milesplit_id=1),
               Team("t2", "Valley High School", short="Valley", code="VAL", milesplit_id=2)]
    m.events = [Event(3, "Boys 5k Run CC JV Large", gender="M", distance_m=5000,
                      division="JV Boys Large", code="5000"),
                Event(4, "Boys 5k Run CC JV Small", gender="M", distance_m=5000,
                      division="JV Boys Small", code="5000")]
    m.races = [Race(3, order=1, start_time="09:00"), Race(4, order=2, start_time="09:30")]
    n = 0
    for race in m.races:
        for t in ("t1", "t2"):
            for _ in range(7):
                n += 1
                m.athletes.append(Athlete(f"a{n}", f"F{n}", f"L{n}", "M", team_id=t,
                                          bib=str(100 + n), grad_year=2029, milesplit_id=n))
                race.entries.append(Entry(f"a{n}", race.event))
    return m


class TestRunTogether(unittest.TestCase):
    """Both JV boys races off one gun, scored separately (2026-10-05)."""

    def test_one_lynx_event_scored_per_race(self):
        m = two_jv_boys_meet()
        target = m.races[0]
        rep = edits.run_together(m, target, [m.races[1]])
        self.assertEqual((rep.moved, rep.removed, rep.events), (14, ["4-1-1"], [3, 4]))
        self.assertEqual([r.key for r in m.races], [(3, 1, 1)])
        self.assertEqual(len(target.entries), 28)
        self.assertEqual(target.name, "Boys 5k Run CC JV Large + Boys 5k Run CC JV Small")
        self.assertTrue(edits.is_run_together(m, target))
        self.assertFalse(edits.is_split_by_gender(m, target))
        self.assertEqual(m.validate(), [])

        # FinishLynx sees one event carrying all 28 runners
        out = lynx.render(m)
        self.assertEqual(out["sch"].split(), ["3,1,1"])
        self.assertEqual(out["counts"]["entries"], 28)

        # finishing order alternates the two races; each is placed and
        # team-scored within its own field
        scoring.freeze(m, target, rm_for(m, target))
        res = target.results
        self.assertEqual(sorted(res["teams"]), ["3", "4"])
        for ev in (3, 4):
            fs = [f for f in res["finishers"] if f["event"] == ev]
            self.assertEqual(len(fs), 14)
            self.assertEqual([f["event_place"] for f in fs], list(range(1, 15)))
        text = printing.results_text(m, target)
        self.assertIn("Boys 5k Run CC JV Large", text)
        self.assertIn("Boys 5k Run CC JV Small", text)
        divisions = {r["division_name"] for t in exports.mp_results(m, [target])["teams"]
                     for a in t["roster"] for r in a["results"]}
        self.assertEqual(divisions, {"JV Boys Large", "JV Boys Small"})

    def test_run_separately_puts_them_back(self):
        m = two_jv_boys_meet()
        edits.run_together(m, m.races[0], [m.races[1]])
        rep = edits.run_separately(m, m.races[0])
        self.assertEqual((rep.moved, rep.removed), (14, ["4-1-1"]))
        self.assertEqual(sorted(len(r.entries) for r in m.races), [14, 14])
        self.assertEqual({e.event for e in m.race(4).entries}, {4})
        self.assertEqual(m.races[0].name, "")
        self.assertFalse(edits.is_run_together(m, m.races[0]))
        self.assertEqual(m.validate(), [])
        with self.assertRaises(ValueError):
            edits.run_separately(m, m.races[0])

    def test_operator_name_kept(self):
        m = two_jv_boys_meet()
        m.races[0].name = "JV Boys 5k"
        edits.run_together(m, m.races[0], [m.races[1]])
        self.assertEqual(m.races[0].name, "JV Boys 5k")
        edits.run_separately(m, m.races[0])
        self.assertEqual(m.races[0].name, "JV Boys 5k")

    def test_refusals(self):
        m = two_jv_boys_meet()
        with self.assertRaises(ValueError):
            edits.run_together(m, m.races[0], [])
        m.races[1].status = "final"
        with self.assertRaises(ValueError):
            edits.run_together(m, m.races[0], [m.races[1]])
        m.races[1].status = "scheduled"
        edits.run_together(m, m.races[0], [m.races[1]])
        with self.assertRaises(ValueError):          # would fold both boys events into one
            edits.split_genders(m, m.races[0])
        with self.assertRaises(ValueError):
            edits.combine_genders(m, m.races[0])

    def test_girls_and_boys_together_is_not_a_gender_split(self):
        m = two_jv_boys_meet()
        m.events[1].gender = "F"
        for a in m.athletes[14:]:
            a.gender = "F"
        edits.run_together(m, m.races[0], [m.races[1]])
        self.assertFalse(edits.is_split_by_gender(m, m.races[0]))
        self.assertTrue(edits.is_run_together(m, m.races[0]))
        self.assertEqual(m.races[0].together, [4])

    def test_survives_save_and_load(self):
        m = two_jv_boys_meet()
        edits.run_together(m, m.races[0], [m.races[1]])
        back = Meet.from_dict(m.to_dict())
        self.assertEqual(back.races[0].together, [4])
        edits.run_separately(back, back.races[0])
        self.assertEqual(len(back.races), 2)

    def test_athlete_in_both_keeps_one_entry(self):
        m = two_jv_boys_meet()
        m.races[1].entries.append(Entry("a1", 4))
        rep = edits.run_together(m, m.races[0], [m.races[1]])
        self.assertEqual(rep.moved, 14)
        self.assertEqual(len(rep.warnings), 1)
        self.assertEqual(sum(1 for e in m.races[0].entries if e.athlete_id == "a1"), 1)


class TestBibWearer(unittest.TestCase):
    """'Someone else is wearing this bib' from the Swap bibs popover
    (2026-10-03): pick from the team roster, or add a new athlete with the
    holder's team/gender/race prefilled, each field changeable or blank."""

    def meet(self):
        m = mixed_meet()
        edits.split_genders(m, m.races[0])          # boys score in their own event
        m.bib_ranges = [[100, 199]]
        # a roster-only teammate (full rosters imported), no bib, no entry
        m.athletes.append(Athlete("r1", "William", "Hollister", "M", team_id="t1",
                                  grad_year=2032, milesplit_id=999))
        return m

    def holder(self, m):
        return next(a for a in m.athletes if a.gender == "M" and a.team_id == "t1" and a.bib)

    def test_context_prefills_from_the_holder(self):
        m = self.meet()
        h = self.holder(m)
        ctx = edits.bib_context(m, h.bib)
        self.assertEqual(ctx["holder"]["athlete_id"], h.id)
        pre = ctx["prefill"]
        self.assertEqual((pre["team_id"], pre["gender"], pre["race"]), ("t1", "M", [5, 1, 1]))
        boys_event = next(e.event for e in m.races[0].entries if e.athlete_id == h.id)
        self.assertEqual(pre["event"], boys_event)
        self.assertIn("r1", [v["athlete_id"] for v in ctx["team_roster"]])
        self.assertFalse(ctx["team_roster"][-1]["entered"])        # roster-only last

    def test_search_team_finds_nickname_and_roster_only(self):
        m = self.meet()
        found = edits.search_team(m, "t1", "Will", "Hollister")
        self.assertEqual(found[0]["athlete_id"], "r1")
        self.assertEqual(edits.search_team(m, "t2", "Will", "Hollister"), [])

    def test_roster_athlete_takes_the_bib_holder_gets_next_free(self):
        m = self.meet()
        h = self.holder(m)
        bib = h.bib
        rep = edits.give_bib_to(m, bib, "r1")
        r1 = m.athlete("r1")
        self.assertEqual(r1.bib, bib)
        self.assertEqual(rep.entered_in, [5, 1, 1])
        self.assertTrue(m.entered("r1"))
        self.assertTrue(h.bib and h.bib != bib)                   # next free from 100-199
        self.assertEqual(m.validate(), [])

    def test_new_athlete_gets_everything_prefilled(self):
        m = self.meet()
        h = self.holder(m)
        bib = h.bib
        rep = edits.add_wearer(m, bib, "Theo", "Garrow")
        a = m.athlete(rep.athlete_id)
        self.assertTrue(rep.created)
        self.assertEqual((a.team_id, a.gender, a.bib, a.source), ("t1", "M", bib, "local"))
        self.assertEqual(rep.entered_in, [5, 1, 1])
        self.assertEqual(m.validate(), [])

    def test_prefills_can_be_changed_or_left_blank(self):
        m = self.meet()
        h = self.holder(m)
        rep = edits.add_wearer(m, h.bib, "Ana", "Reyes", gender="F", team_id=None,
                               race=None, grade_text="", holder_action="clear")
        a = m.athlete(rep.athlete_id)
        self.assertEqual((a.gender, a.team_id, a.grade_text), ("F", None, ""))
        self.assertIsNone(rep.entered_in)                         # not entered yet
        self.assertFalse(m.entered(a.id))
        self.assertEqual(h.bib, "")                               # holder left without
        with self.assertRaises(ValueError):
            edits.add_wearer(m, "150", "No", "Gender", gender="")
        self.assertEqual(m.validate(), [])

    def test_final_race_must_be_reopened(self):
        m = self.meet()
        race = m.races[0]
        scoring.freeze(m, race, rm_for(m, race))
        h = self.holder(m)
        with self.assertRaises(ValueError):
            edits.add_wearer(m, h.bib, "Theo", "Garrow")
        # entering nowhere doesn't touch the final race, so it is allowed
        edits.add_wearer(m, h.bib, "Theo", "Garrow", race=None, holder_action="swap")


if __name__ == "__main__":
    unittest.main()
