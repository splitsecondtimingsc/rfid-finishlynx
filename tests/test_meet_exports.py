"""mpResults payload (and Hy-Tek semicolon results) from the meet model's frozen
results. All names and ids are synthetic (2026-10-03)."""
import json
import unittest

from rfid_finishlynx.meet import exports as X
from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, MilesplitRef,
                                        Race, Team)


def _f(place, bib, aid, event, elapsed, status="finished", event_place=None, points=None,
       scorer=False, displacer=False):
    return {"place": place if status == "finished" else None, "bib": bib, "athlete_id": aid,
            "event": event, "elapsed": elapsed if status == "finished" else None,
            "status": status, "event_place": event_place if status == "finished" else None,
            "points": points, "scorer": scorer, "displacer": displacer, "fl_only": False}


def build_meet() -> Meet:
    """Four scoring events, three races:
    race 1 (event 1, varsity boys): final, full scoring, every edge case;
    race 2 (Lynx event 2): one gun for JV boys (event 2) + JV girls (event 4);
    race 3 (event 3, varsity girls): scheduled, no results."""
    m = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10",
                           host="Ridge HS", milesplit=MilesplitRef(tc_id=700, meet_id=800100)))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID", milesplit_id=9001),
               Team("t2", "Valley High School", short="Valley", code="VAL", milesplit_id=9002),
               Team("t3", "Summit Academy", short="Summit", code="SUM", milesplit_id=9003),
               Team("t4", "Lakeside Prep", short="Lakeside", code="LAK", score=False)]
    m.events = [Event(1, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys", "5000"),
                Event(2, "Boys 5k Run CC JV", "M", 5000, "JV Boys", "5000"),
                Event(3, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls", "5000"),
                Event(4, "Girls 5k Run CC JV", "F", 5000, "JV Girls", "5000")]
    people = [  # id, first, last, gender, team, bib, grad year
        ("a1", "Avery", "Alder", "M", "t1", "101", 2027), ("a2", "Blake", "Birch", "M", "t1", "102", 2028),
        ("a3", "Casey", "Cedar", "M", "t1", "103", 2029), ("a4", "Drew", "Dogwood", "M", "t1", "104", 2030),
        ("a5", "Emery", "Elm", "M", "t1", "105", 2027), ("a6", "Finley", "Fir", "M", "t1", "106", 2028),
        ("a7", "Gray", "Gum", "M", "t2", "201", 2027), ("a8", "Harper", "Hazel", "M", "t2", "202", 2028),
        ("a9", "Indy", "Ivy", "M", "t2", "203", 2029), ("a10", "Jules", "Juniper", "M", "t2", "204", 2030),
        ("a11", "Kai", "Kapok", "M", "t2", "205", 2027), ("a12", "Lane", "Larch", "M", "t3", "301", 2028),
        ("a13", "Morgan", "Maple", "M", "t3", "302", 2029), ("a14", "Noel", "Nutmeg", "M", "t4", "401", 2027),
        ("a15", "Oakley", "Olive", "M", "t1", "115", 2030), ("a16", "Parker", "Pine", "M", None, "16", 2028),
        ("a17", "Quinn", "Quince", "M", "t2", "206", 2029), ("a18", "Reese", "Rowan", "M", "t3", "303", 2027),
        ("a19", "Sage", "Spruce", "M", "t3", "304", 2028),
        ("a20", "Tatum", "Teak", "M", "t1", "120", 2030), ("a21", "Uma", "Umber", "F", "t3", "320", 2030),
        ("a22", "Vale", "Vine", "M", "t1", "121", 2029), ("a23", "Wren", "Willow", "F", "t2", "220", 2029),
        ("a24", "Xen", "Yew", "F", "t1", "130", 2027)]
    for i, (aid, first, last, g, tid, bib, gy) in enumerate(people):
        n = int(aid[1:])
        m.athletes.append(Athlete(aid, first, last, g, team_id=tid, bib=bib, grad_year=gy,
                                  milesplit_id=5000000 + n, universal_id=f"UID{n:04d}"))
    for aid in ("a16", "a17"):                       # walk-ups: no MileSplit ids
        a = m.athlete(aid)
        a.milesplit_id, a.universal_id, a.source = None, None, "local"
    m.athlete("a18").ms_match = "pending"            # in the review queue: ids held back
    ex = {"a15"}
    m.races = [
        Race(1, order=1, status="final",
             entries=[Entry(f"a{n}", 1, status="exhibition" if f"a{n}" in ex else "entered")
                      for n in range(1, 20)]),
        Race(2, order=2, status="final",
             entries=[Entry("a20", 2), Entry("a21", 4), Entry("a22", 2), Entry("a23", 4)]),
        Race(3, order=3, entries=[Entry("a24", 3)]),
    ]
    t = 983.52
    fin = []
    rows = [  # bib, athlete, points, scorer, displacer
        ("101", "a1", 1, True, False), ("201", "a7", 2, True, False), ("102", "a2", 3, True, False),
        ("401", "a14", None, False, False), ("202", "a8", 4, True, False), ("103", "a3", 5, True, False),
        ("203", "a9", 6, True, False), ("104", "a4", 7, True, False), ("105", "a5", 8, True, False),
        ("115", "a15", None, False, False), ("204", "a10", 9, True, False),
        ("205", "a11", 10, True, False), ("106", "a6", 11, False, True),
        ("301", "a12", None, False, False), ("16", "a16", None, False, False)]
    for i, (bib, aid, pts, sc, dp) in enumerate(rows):
        fin.append(_f(i + 1, bib, aid, 1, round(t + i * 5.13, 3), event_place=i + 1, points=pts,
                      scorer=sc, displacer=dp))
    fin[-1]["elapsed"] = 1060.4415                    # thousandths: rounds UP to 1060.45
    fin += [_f(None, "206", "a17", 1, None, "DNF"), _f(None, "303", "a18", 1, None, "DQ"),
            _f(None, "302", "a13", 1, None, "DNF"), _f(None, "304", "a19", 1, None, "DNS")]
    m.races[0].results = {
        "frozen_at": "2026-10-10T11:42:05", "finishers": fin,
        "teams": {"1": [
            {"rank": 1, "team_id": "t1", "score": 24, "complete": True, "count": "6/5",
             "runners": [], "tiebreak_points": 11, "total_time": 5000.0, "avg_time": 1000.0},
            {"rank": 2, "team_id": "t2", "score": 31, "complete": True, "count": "5/5",
             "runners": [], "tiebreak_points": None, "total_time": 5100.0, "avg_time": 1020.0},
            {"rank": None, "team_id": "t3", "score": None, "complete": False, "count": "1/5",
             "runners": []}]}}
    m.races[1].results = {
        "frozen_at": "2026-10-10T12:30:00",
        "finishers": [_f(1, "120", "a20", 2, 1100.0, event_place=1),
                      _f(2, "320", "a21", 4, 1101.25, event_place=1),
                      _f(3, "121", "a22", 2, 1102.5, event_place=2),
                      _f(4, "220", "a23", 4, 1103.75, event_place=2)],
        "teams": {}}
    return m


def results_of(payload, bib):
    for t in payload["teams"]:
        for a in t["roster"]:
            if a.get("bib") == bib:
                return t, a
    return None, None


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.meet = build_meet()
        self.p = X.mp_results(self.meet)

    def test_assert_meet_fixture_is_valid(self):
        self.assertEqual(self.meet.validate(), [])

    def test_top_level_and_team_order(self):
        self.assertEqual(self.p["meet_id"], 800100)
        self.assertEqual([t["code"] for t in self.p["teams"]], ["LAK", "RID", "SUM", "UNAT", "VAL"])
        lak = self.p["teams"][0]
        self.assertIn("milesplit_team_id", lak)          # null kept on teams only
        self.assertIsNone(lak["milesplit_team_id"])
        self.assertEqual(lak["relays"], [])

    def test_scorer_result_matches_meetpro_shape(self):
        team, a = results_of(self.p, 101)
        self.assertEqual(team["milesplit_team_id"], 9001)
        self.assertEqual(a["year"], "12")
        self.assertEqual(a["gender"], "m")
        self.assertEqual(a["athlete_id"], 1)
        self.assertEqual(a["milesplit_athlete_id"], "5000001")
        self.assertEqual(a["milesplit_universal_id"], "UID0001")
        self.assertEqual(a["results"], [{
            "place": 1, "score": 1, "mark": 983.52, "fat": 1,
            "event": {"code": "5000", "milesplit_code": "5000m", "gender": "m", "event_id": 1,
                      "name": "Boys 5k Run CC Varsity", "event_num": 1},
            "division_id": X.division_id_for("Varsity Boys", {}),
            "division_name": "Varsity Boys"}])

    def test_score_only_for_scorers_and_displacers(self):
        self.assertEqual(results_of(self.p, 106)[1]["results"][0]["score"], 11)   # displacer
        for bib in (401, 115, 301, 16):          # non-scoring team, exhibition, short team, unattached
            self.assertNotIn("score", results_of(self.p, bib)[1]["results"][0], bib)
            self.assertIn("mark", results_of(self.p, bib)[1]["results"][0])

    def test_non_finishers(self):
        r = results_of(self.p, 206)[1]["results"][0]
        self.assertEqual(r["status"], "DNF")
        self.assertEqual(r["fat"], 0)
        self.assertNotIn("place", r)
        self.assertNotIn("mark", r)
        self.assertEqual(results_of(self.p, 303)[1]["results"][0]["status"], "DQ")
        self.assertEqual(results_of(self.p, 304), (None, None))        # DNS left out by default
        with_dns = X.mp_results(self.meet, include_dns=True)
        self.assertEqual(results_of(with_dns, 304)[1]["results"][0]["status"], "DNS")

    def test_write_ins_and_review_queue_go_without_ids(self):
        for bib in (206, 16, 303):
            a = results_of(self.p, bib)[1]
            self.assertNotIn("milesplit_athlete_id", a, bib)
            self.assertNotIn("milesplit_universal_id", a, bib)
        team, a = results_of(self.p, 16)
        self.assertEqual((team["name"], team["code"]), ("Unattached", "UNAT"))

    def test_marks_round_up_to_hundredths(self):
        self.assertEqual(results_of(self.p, 16)[1]["results"][0]["mark"], 1060.45)
        self.assertEqual(X.hundredths(2665.642), 2665.65)
        self.assertEqual(X.hundredths(983.52), 983.52)
        self.assertEqual(X.hundredths(983.5200000001), 983.52)
        self.assertEqual(X.hundredths(1000), 1000.0)

    def test_integer_floats_written_as_ints(self):
        a = results_of(self.p, 120)[1]
        self.assertIsInstance(a["results"][0]["mark"], int)            # 1100.0 -> 1100
        self.assertIn('"mark": 1100,', json.dumps(a))


class TestDivisions(unittest.TestCase):
    def test_golden_division_id_from_a_real_upload(self):
        # Woodmont 2026-10-03: MeetPro sent "JV Boys" as division 289275230.
        self.assertEqual(X.division_id_for("JV Boys", {}), 289275230)

    def test_shared_gender_distance_events_get_their_own_division(self):
        labels = X.division_labels(build_meet())
        self.assertEqual({n: name for n, (_id, name) in labels.items()},
                         {1: "Varsity Boys", 2: "JV Boys", 3: "Varsity Girls", 4: "JV Girls"})

    def test_lone_event_gets_no_division(self):
        m = build_meet()
        m.races[2].entries = []                   # varsity girls not run: JV girls alone in F|5000m
        labels = X.division_labels(m)
        self.assertNotIn(4, labels)
        self.assertIn(2, labels)
        r = results_of(X.mp_results(m), 320)[1]["results"][0]
        self.assertNotIn("division_name", r)

    def test_combined_start_places_per_scoring_event(self):
        p = X.mp_results(build_meet())
        self.assertEqual(results_of(p, 121)[1]["results"][0]["place"], 2)
        r = results_of(p, 320)[1]["results"][0]
        self.assertEqual((r["place"], r["event"]["event_num"], r["division_name"]), (1, 4, "JV Girls"))

    def test_event_place_missing_falls_back_and_renumbers(self):
        m = build_meet()
        for f in m.races[1].results["finishers"]:
            f["event_place"] = None
        p = X.mp_results(m)
        self.assertEqual([results_of(p, b)[1]["results"][0]["place"] for b in (120, 121, 320, 220)],
                         [1, 2, 1, 2])

    def test_partial_upload_keeps_the_full_meet_labels(self):
        m = build_meet()
        full = X.mp_results(m)
        jv = X.mp_results(m, races=[m.races[1]])
        self.assertEqual(sorted(t["code"] for t in jv["teams"]), ["RID", "SUM", "VAL"])
        for bib in (120, 121, 320, 220):
            self.assertEqual(results_of(jv, bib)[1]["results"], results_of(full, bib)[1]["results"])
        self.assertEqual(X.mp_results(m, races=[(2, 1, 1)]), jv)
        self.assertEqual(X.mp_results(m, races=[2]), jv)

    def test_unique_division_name(self):
        used = {}
        self.assertEqual(X.unique_division_name("JV Boys", used), "JV Boys")
        self.assertEqual(X.unique_division_name("jv boys", used), "jv boys 2")
        long = "A" * 40
        self.assertEqual(X.unique_division_name(long, used), "A" * 32)
        self.assertEqual(X.unique_division_name(long, used), "A" * 30 + " 2")

    def test_apply_division_places_keeps_ties(self):
        rs = [{"place": 4}, {"place": 9}, {"place": 9}, {"place": 12}]
        X.apply_division_places(rs)
        self.assertEqual([r["place"] for r in rs], [1, 2, 2, 4])


class TestEventCodes(unittest.TestCase):
    def ev(self, code="", d=0):
        return Event(1, "x", "M", d, code=code)

    def test_codes(self):
        self.assertEqual(X.event_codes(self.ev("5000", 5000)), ("5000", "5000m"))
        self.assertEqual(X.event_codes(self.ev("", 8000)), ("8000", "8000m"))
        self.assertEqual(X.event_codes(self.ev("2MILE")), ("2mile", "2Mile"))
        self.assertEqual(X.event_codes(self.ev("", 3219)), ("2mile", "2Mile"))
        self.assertEqual(X.event_codes(self.ev("1.5 mile")), ("1.5mile", "1.5Mile"))
        self.assertEqual(X.event_codes(self.ev("3.1MILE")), ("3.1mile", "5000m"))
        self.assertEqual(X.event_codes(self.ev("", 2414)), ("1.5mile", "1.5Mile"))
        self.assertEqual(X.event_codes(self.ev("", 4000)), ("4000", "4000m"))

    def test_relay_refused(self):
        with self.assertRaises(ValueError):
            X.event_codes(Event(1, "x", "M", 5000, kind="relay"))


class TestOfficialOnly(unittest.TestCase):
    def test_reopened_race_is_left_out(self):
        m = build_meet()
        m.races[0].status = "scheduled"           # reopened: results kept, not official
        p = X.mp_results(m)
        self.assertEqual(results_of(p, 101), (None, None))
        self.assertIsNotNone(results_of(p, 120)[1])
        probs = "\n".join(X.mp_results_problems(m))
        self.assertIn("reopened", probs)
        self.assertIn("not final", "\n".join(X.mp_results_problems(m, races=[m.races[0]])))

    def test_scheduled_race_chosen_explicitly_is_a_problem(self):
        m = build_meet()
        probs = X.mp_results_problems(m, races=[m.races[2]])
        self.assertTrue(any("not final yet" in p for p in probs), probs)
        self.assertIn("no results to send", probs)


class TestChecks(unittest.TestCase):
    def test_ready_meet(self):
        m = build_meet()
        probs = X.mp_results_problems(m)
        # Only the review-queue note: everything else is in order.
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("review queue", probs[0])

    def test_id_coverage(self):
        cov = X.id_coverage(X.mp_results(build_meet()))
        self.assertEqual((cov["athletes"], cov["athletes_with_id"]), (22, 19))
        self.assertEqual((cov["teams"], cov["teams_with_id"]), (5, 3))
        self.assertEqual(cov["write_ins"], 3)
        self.assertTrue(cov["ok"])

    def test_under_half_ids_blocks(self):
        m = build_meet()
        for a in m.athletes[:15]:
            a.milesplit_id = None
        probs = "\n".join(X.mp_results_problems(m))
        self.assertIn("at least 50%", probs)
        self.assertFalse(X.id_coverage(X.mp_results(m))["ok"])
        m = build_meet()
        for t in m.teams:
            t.milesplit_id = None
        self.assertIn("teams 0/5", "\n".join(X.mp_results_problems(m)))

    def test_unlinked_meet_and_unknown_bib(self):
        m = build_meet()
        m.meet.milesplit = MilesplitRef()
        m.races[0].results["finishers"].insert(
            15, _f(16, "999", None, 1, 1070.0, event_place=16))
        probs = "\n".join(X.mp_results_problems(m))
        self.assertIn("not linked", probs)
        self.assertIn("timing company", probs)
        self.assertIn("bib(s) not in the meet, left out: 999", probs)
        self.assertEqual(X.mp_results(m)["meet_id"], 0)

    def test_places_must_start_at_one(self):
        m = build_meet()
        for f in m.races[1].results["finishers"]:
            if f["event_place"]:
                f["event_place"] += 1
        self.assertTrue(any("places start at 2" in p for p in X.mp_results_problems(m)))

    def test_merge_warning(self):
        m = build_meet()
        p = X.mp_results(m)
        for t in p["teams"]:
            for a in t["roster"]:
                for r in a["results"]:
                    r.pop("division_name", None)
        probs = X.mp_results_problems(m, payload=p)
        self.assertTrue(any("would merge" in x for x in probs), probs)

    def test_fingerprint(self):
        m = build_meet()
        a, b = X.mp_results(m), X.mp_results(m)
        self.assertEqual(X.fingerprint(a), X.fingerprint(b))
        b["_match"] = {"x": 1}
        self.assertEqual(X.fingerprint(a), X.fingerprint(b))
        reordered = json.loads(json.dumps(a, sort_keys=True))
        self.assertEqual(X.fingerprint(a), X.fingerprint(reordered))
        m.races[1].results["finishers"][0]["elapsed"] = 1100.01
        self.assertNotEqual(X.fingerprint(a), X.fingerprint(X.mp_results(m)))

    def test_summary_line(self):
        line = X.summary_line(X.mp_results(build_meet()))
        self.assertIn("Varsity Boys: 15 timed +3 DNF/DNS/DQ", line)
        self.assertIn("ids: athletes 19/22, teams 3/5", line)
        self.assertEqual(X.fmt_time(3723.45), "1:02:03.45")


class TestHytek(unittest.TestCase):
    def test_e_records(self):
        text = X.hytek_results(build_meet())
        lines = text.split("\r\n")
        self.assertTrue(lines[0].startswith("H;Test Invitational;10/10/2026;10/10/2026"))
        e = [ln.split(";") for ln in lines[1:] if ln]
        self.assertTrue(all(len(x) == 33 for x in e))
        first = e[0]
        self.assertEqual(first[:11], ["E", "T", "1", "", "5000", "M", "",
                                      str(X.division_id_for("Varsity Boys", {})), "Varsity Boys",
                                      "F", "16:23.52"])
        self.assertEqual((first[13], first[17], first[22], first[23], first[27], first[30], first[31]),
                         ("1", "1", "Alder", "Avery", "RID", "12", "101"))
        dnf = next(x for x in e if x[31] == "206")
        self.assertEqual((dnf[10], dnf[13]), ("DNF", ""))
        exh = next(x for x in e if x[31] == "115")
        self.assertEqual((exh[16], exh[17]), ("Y", ""))


if __name__ == "__main__":
    unittest.main()
