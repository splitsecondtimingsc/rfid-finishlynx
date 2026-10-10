import copy
import json
import random
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, Race,
                                        Scoring, Team)
from rfid_finishlynx.meet.scoring import (BLOCKING, Unresolved, build_results, freeze,
                                          live_scores, official_results, reopen,
                                          score_event)
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 10, 9, 0, 0)


class Builder:
    """A synthetic meet: one race (Lynx event 1) whose entries can score in
    events 1 and 2 (combined start)."""

    def __init__(self, scorers=5, displacers=2):
        self.meet = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test XC", date="2026-10-10",
                                       scoring=Scoring(scorers, displacers)))
        self.meet.events = [Event(1, "Boys 5k Run CC JV", gender="M", distance_m=5000),
                            Event(2, "Boys 5k Run CC JV MS", gender="M", distance_m=5000)]
        self.race = Race(1, order=1)
        self.meet.races = [self.race]

    def team(self, tid, name, score=True):
        self.meet.teams.append(Team(tid, name + " High", short=name, code=name[:4].upper(),
                                    score=score))
        return self

    def runner(self, bib, team, event=1, status="entered", unattached=False, entered=True):
        aid = f"a{bib}"
        self.meet.athletes.append(Athlete(aid, f"F{bib}", f"L{bib}", "M", team_id=team,
                                          bib=str(bib), unattached=unattached))
        if entered:
            self.race.entries.append(Entry(aid, event, status=status))
        return self

    def runners(self, team, bibs, event=1):
        for b in bibs:
            self.runner(b, team, event)
        return self


def rm(order, on_course=(), no_show=(), holds=None, missing=None, fl_only=()):
    """A RaceManager.results(key)-shaped dict: finishers in `order`, one
    second apart from 900 s."""
    holds, missing = holds or {}, missing or {}
    fins = []
    for i, bib in enumerate(order):
        bib = str(bib)
        why = holds.get(bib, "")
        fins.append({"bib": bib, "place": i + 1, "missing": list(missing.get(bib, [])),
                     "finish": {"elapsed": 900.0 + i, "fl_untimed": bool(why), "lif_why": why,
                                **({"fl_only": True} if bib in fl_only else {})}})
    return {"finishers": fins,
            "on_course": [{"bib": str(b), "splits": [{"point": "S1"}], "finish": None}
                          for b in on_course],
            "no_show": [str(b) for b in no_show], "needs_review": len(missing)}


def by_bib(results):
    return {f["bib"]: f for f in results["finishers"]}


def team_row(results, tid, event=1):
    return next(t for t in results["teams"][str(event)] if t["team_id"] == tid)


class TestTeamScoring(unittest.TestCase):

    def test_two_complete_teams_points_flags_times(self):
        b = Builder().team("t1", "Ridge").team("t2", "Valley")
        b.runners("t1", range(101, 108)).runners("t2", range(201, 208))
        order = [101, 102, 103, 201, 104, 202, 203, 105, 204, 205, 106, 206, 107, 207]
        res, warns = build_results(b.meet, b.race, rm(order), now=GUN)
        self.assertEqual(warns, [])
        self.assertEqual(res["frozen_at"], "2026-10-10T09:00:00")
        ridge, valley = res["teams"]["1"]
        self.assertEqual((ridge["team_id"], ridge["rank"], ridge["score"]), ("t1", 1, 1 + 2 + 3 + 5 + 8))
        self.assertEqual((valley["team_id"], valley["rank"], valley["score"]), ("t2", 2, 4 + 6 + 7 + 9 + 10))
        self.assertEqual(ridge["count"], "7/5")
        self.assertTrue(ridge["complete"])
        self.assertEqual([r["points"] for r in ridge["runners"]], [1, 2, 3, 5, 8, 11, 13])
        self.assertEqual([r["scorer"] for r in ridge["runners"]], [True] * 5 + [False] * 2)
        self.assertEqual(ridge["runners"][0], {"athlete_id": "a101", "bib": "101",
                                               "points": 1, "scorer": True})
        self.assertEqual(ridge["tiebreak_points"], 11)
        # 101,102,103,104,105 ran 900,901,902,904,907
        self.assertAlmostEqual(ridge["total_time"], 900 + 901 + 902 + 904 + 907)
        self.assertAlmostEqual(ridge["avg_time"], (900 + 901 + 902 + 904 + 907) / 5)
        f = by_bib(res)
        self.assertEqual(f["101"], {"place": 1, "bib": "101", "athlete_id": "a101", "event": 1,
                                    "elapsed": 900.0, "status": "finished", "event_place": 1,
                                    "points": 1, "scorer": True, "displacer": False,
                                    "fl_only": False, "splits": []})
        self.assertEqual((f["106"]["points"], f["106"]["scorer"], f["106"]["displacer"]),
                         (11, False, True))

    def test_runners_beyond_seventh_neither_score_nor_displace(self):
        b = Builder().team("t1", "Ridge").team("t2", "Valley")
        b.runners("t1", range(101, 110)).runners("t2", range(201, 206))
        order = list(range(101, 110)) + list(range(201, 206))
        res, _ = build_results(b.meet, b.race, rm(order))
        f = by_bib(res)
        self.assertEqual([f[str(x)]["points"] for x in range(101, 110)],
                         [1, 2, 3, 4, 5, 6, 7, None, None])
        self.assertFalse(f["108"]["displacer"])
        # Valley's runners come straight after Ridge's 7th, not its 9th
        self.assertEqual([f[str(x)]["points"] for x in range(201, 206)], [8, 9, 10, 11, 12])
        self.assertEqual(team_row(res, "t1")["count"], "9/5")
        self.assertEqual(len(team_row(res, "t1")["runners"]), 7)

    def test_incomplete_team_does_not_displace_and_is_listed_unranked(self):
        b = Builder().team("t1", "Ridge").team("t2", "Valley").team("t3", "Hill")
        b.runners("t1", range(101, 106)).runners("t2", range(201, 206)).runners("t3", [301, 302, 303])
        order = [301, 101, 302, 201, 102, 202, 303, 103, 203, 104, 204, 105, 205]
        res, _ = build_results(b.meet, b.race, rm(order))
        f = by_bib(res)
        self.assertIsNone(f["301"]["points"])
        self.assertFalse(f["301"]["scorer"] or f["301"]["displacer"])
        self.assertEqual(f["101"]["points"], 1)
        self.assertEqual(f["301"]["event_place"], 1)      # still placed in the event
        rows = res["teams"]["1"]
        self.assertEqual([t["team_id"] for t in rows], ["t1", "t2", "t3"])
        hill = rows[-1]
        self.assertEqual((hill["rank"], hill["complete"], hill["count"], hill["score"]),
                         (None, False, "3/5", None))
        self.assertEqual([r["points"] for r in hill["runners"]], [None, None, None])
        self.assertIsNone(hill["total_time"])

    def test_partial_teams_ordered_by_count_then_first_runner(self):
        b = Builder().team("t1", "A").team("t2", "B").team("t3", "C")
        b.runners("t1", [101, 102]).runners("t2", [201, 202, 203]).runners("t3", [301, 302])
        res, _ = build_results(b.meet, b.race, rm([301, 101, 201, 102, 202, 302, 203]))
        self.assertEqual([(t["team_id"], t["count"]) for t in res["teams"]["1"]],
                         [("t2", "3/5"), ("t3", "2/5"), ("t1", "2/5")])

    def test_tied_score_sixth_runner_wins(self):
        b = Builder().team("t1", "A").team("t2", "B")
        b.runners("t1", range(101, 107)).runners("t2", range(201, 207))
        # a third team's runner (8th) makes a clean tie possible
        b.team("t3", "C").runners("t3", range(301, 306))
        # A: 1 4 5 9 10 = 29, 6th 12   B: 2 3 6 7 11 = 29, 6th 13
        order = [101, 201, 202, 102, 103, 203, 204, 301, 104, 105, 205, 106, 206,
                 302, 303, 304, 305]
        res, _ = build_results(b.meet, b.race, rm(order))
        a, bb = team_row(res, "t1"), team_row(res, "t2")
        self.assertEqual([r["points"] for r in a["runners"]], [1, 4, 5, 9, 10, 12])
        self.assertEqual([r["points"] for r in bb["runners"]], [2, 3, 6, 7, 11, 13])
        self.assertEqual((a["score"], bb["score"]), (29, 29))
        self.assertEqual((a["tiebreak_points"], bb["tiebreak_points"]), (12, 13))
        self.assertEqual((a["rank"], bb["rank"]), (1, 2))
        self.assertEqual(res["teams"]["1"][0]["team_id"], "t1")

        # flip the sixth runners: B's 6th now ahead, B wins the tie
        order = [101, 201, 202, 102, 103, 203, 204, 301, 104, 105, 205, 206, 106,
                 302, 303, 304, 305]
        res, _ = build_results(b.meet, b.race, rm(order))
        self.assertEqual([t["team_id"] for t in res["teams"]["1"][:2]], ["t2", "t1"])
        self.assertEqual(team_row(res, "t2")["rank"], 1)

    def test_tied_team_without_sixth_runner_loses(self):
        b = Builder().team("t1", "A").team("t2", "B").team("t3", "C")
        b.runners("t1", range(101, 107)).runners("t2", range(201, 206)).runners("t3", range(301, 306))
        # A: 1 4 5 9 10 = 29 (6th 14)   B: 2 3 6 7 11 = 29, no 6th
        order = [101, 201, 202, 102, 103, 203, 204, 301, 104, 105, 205, 302, 303,
                 106, 304, 305]
        res, _ = build_results(b.meet, b.race, rm(order))
        a, bb = team_row(res, "t1"), team_row(res, "t2")
        self.assertEqual((a["score"], bb["score"]), (29, 29))
        self.assertIsNone(bb["tiebreak_points"])
        self.assertEqual((a["rank"], bb["rank"]), (1, 2))

    def test_tie_with_neither_sixth_runner_stands(self):
        b = Builder().team("t1", "A").team("t2", "B").team("t3", "C")
        b.runners("t1", range(101, 106)).runners("t2", range(201, 206)).runners("t3", range(301, 307))
        # A: 1 4 5 9 10 = 29   B: 2 3 6 7 11 = 29   C: 8 12 13 14 15 = 62
        order = [101, 201, 202, 102, 103, 203, 204, 301, 104, 105, 205, 302, 303,
                 304, 305, 306]
        res, _ = build_results(b.meet, b.race, rm(order))
        ranks = {t["team_id"]: t["rank"] for t in res["teams"]["1"]}
        self.assertEqual(ranks, {"t1": 1, "t2": 1, "t3": 3})

    def test_non_scorers_neither_score_nor_displace(self):
        b = Builder().team("t1", "A").team("t2", "B").team("t9", "Club", score=False)
        b.runners("t1", range(101, 106)).runners("t2", range(201, 206))
        b.runner(150, "t1", status="exhibition")           # A's exhibition runner
        b.runner(151, "t1", unattached=True)               # listed with A, runs unattached
        b.runner(160, None)                                # no team at all
        b.runners("t9", range(901, 906))                   # a full non-scoring team
        order = [150, 151, 160, 901, 101, 902, 201, 903, 102, 904, 202, 905,
                 103, 203, 104, 204, 105, 205]
        res, warns = build_results(b.meet, b.race, rm(order))
        self.assertEqual(warns, [])
        f = by_bib(res)
        for bib in ("150", "151", "160", "901", "905"):
            self.assertIsNone(f[bib]["points"], bib)
            self.assertFalse(f[bib]["scorer"] or f[bib]["displacer"], bib)
        self.assertEqual([f[str(x)]["points"] for x in (101, 201, 102, 202)], [1, 2, 3, 4])
        self.assertEqual(f["101"]["place"], 5)
        self.assertEqual(f["101"]["event_place"], 5)
        self.assertEqual(team_row(res, "t1")["count"], "5/5")   # exhibition/unattached not counted
        self.assertNotIn("t9", [t["team_id"] for t in res["teams"]["1"]])

    def test_non_scorers_do_not_make_a_team_complete(self):
        b = Builder().team("t1", "A").team("t2", "B")
        b.runners("t1", range(101, 105)).runner(150, "t1", status="exhibition")
        b.runners("t2", range(201, 206))
        res, _ = build_results(b.meet, b.race, rm([101, 150, 102, 103, 104, 201, 202, 203, 204, 205]))
        self.assertEqual(team_row(res, "t1")["count"], "4/5")
        self.assertFalse(team_row(res, "t1")["complete"])
        self.assertEqual(by_bib(res)["201"]["points"], 1)

    def test_score_event_is_pure(self):
        b = Builder().team("t1", "A").team("t2", "B")
        b.runners("t1", range(101, 108)).runners("t2", range(201, 206))
        res, _ = build_results(b.meet, b.race, rm(list(range(101, 108)) + list(range(201, 206))))
        fins = res["finishers"]
        before = copy.deepcopy(fins)
        meet_before = copy.deepcopy(b.meet.to_dict())
        rows = score_event(b.meet, fins, 1)            # meet-wide entry lookup
        self.assertEqual(fins, before)
        self.assertEqual(b.meet.to_dict(), meet_before)
        self.assertEqual(rows, res["teams"]["1"])
        self.assertEqual(score_event(b.meet, fins, 2), [])


class TestCombinedStart(unittest.TestCase):

    def test_two_scoring_events_in_one_race(self):
        b = Builder().team("t1", "A").team("t2", "B")
        b.runners("t1", range(101, 106), event=1).runners("t2", range(201, 206), event=1)
        b.runners("t1", range(111, 116), event=2).runners("t2", range(211, 216), event=2)
        order = [111, 101, 211, 201, 102, 112, 202, 212, 103, 113, 203, 213,
                 104, 114, 204, 214, 105, 115, 205, 215]
        res, warns = build_results(b.meet, b.race, rm(order))
        self.assertEqual(warns, [])
        self.assertEqual(sorted(res["teams"]), ["1", "2"])
        f = by_bib(res)
        self.assertEqual((f["111"]["place"], f["111"]["event"], f["111"]["event_place"],
                          f["111"]["points"]), (1, 2, 1, 1))
        self.assertEqual((f["101"]["place"], f["101"]["event"], f["101"]["event_place"],
                          f["101"]["points"]), (2, 1, 1, 1))
        self.assertEqual((f["201"]["place"], f["201"]["event_place"], f["201"]["points"]), (4, 2, 2))
        ev1 = {t["team_id"]: t["score"] for t in res["teams"]["1"]}
        ev2 = {t["team_id"]: t["score"] for t in res["teams"]["2"]}
        self.assertEqual(ev1, {"t1": 1 + 3 + 5 + 7 + 9, "t2": 2 + 4 + 6 + 8 + 10})
        self.assertEqual(ev2, {"t1": 1 + 3 + 5 + 7 + 9, "t2": 2 + 4 + 6 + 8 + 10})
        # the other event's runners never displace
        self.assertEqual([r["bib"] for r in team_row(res, "t1", 2)["runners"]],
                         ["111", "112", "113", "114", "115"])

    def test_event_with_entries_but_no_finishers_has_empty_list(self):
        b = Builder().team("t1", "A").runners("t1", range(101, 106)).runner(111, "t1", event=2)
        res = live_scores(b.meet, b.race, rm(range(101, 106)))
        self.assertEqual(res["teams"]["2"], [])
        self.assertEqual(team_row(res, "t1")["rank"], 1)


class TestStatusesAndWarnings(unittest.TestCase):

    def setUp(self):
        self.b = Builder().team("t1", "A").team("t2", "B")
        self.b.runners("t1", range(101, 107)).runners("t2", range(201, 207))

    def test_marks_on_the_race_are_used_by_default(self):
        b = self.b
        b.race.marks = {"206": "DNF", "201": "DQ"}
        order = [101, 201, 102, 202, 103, 203, 104, 204, 105, 205, 106]
        res, _ = build_results(b.meet, b.race, rm(order, on_course=[206], no_show=[]))
        f = by_bib(res)
        self.assertEqual((f["206"]["status"], f["201"]["status"]), ("DNF", "DQ"))
        res, _ = build_results(b.meet, b.race, rm(order, on_course=[206], no_show=[]),
                               statuses={"206": "DNF"})          # explicit wins
        self.assertEqual(by_bib(res)["201"]["status"], "finished")

    def test_dnf_dns_dq(self):
        b = self.b
        b.runner(108, "t1", status="scratched")
        order = [101, 201, 102, 202, 103, 203, 104, 204, 105, 205, 106]
        res, warns = build_results(b.meet, b.race,
                                   rm(order, on_course=[206], no_show=[]),
                                   statuses={"206": "dnf", "201": "DQ"})
        self.assertEqual(warns, [])
        f = by_bib(res)
        self.assertEqual(f["206"]["status"], "DNF")
        self.assertEqual(f["201"]["status"], "DQ")
        self.assertIsNone(f["201"]["place"])
        self.assertEqual(f["202"]["place"], 3)            # moved up past the DQ
        self.assertIsNone(f["202"]["points"])             # B no longer complete
        self.assertEqual(f["102"]["points"], 2)
        # B is left with 4 finishers (201 DQ, 206 DNF): incomplete
        self.assertEqual(team_row(res, "t2")["count"], "4/5")
        self.assertEqual(team_row(res, "t1")["score"], 1 + 2 + 3 + 4 + 5)
        self.assertNotIn("108", f)                        # scratched: not listed
        tail = [(x["bib"], x["status"]) for x in res["finishers"] if x["status"] != "finished"]
        self.assertEqual(tail, [("206", "DNF"), ("201", "DQ")])
        self.assertEqual(res["finishers"][-1]["event"], 1)

    def test_entered_but_never_seen_is_dns_on_final_only(self):
        b = self.b
        order = [101, 201, 102, 202, 103, 203, 104, 204, 105, 205]
        res, _ = build_results(b.meet, b.race, rm(order, no_show=["106"]))
        dns = [x["bib"] for x in res["finishers"] if x["status"] == "DNS"]
        self.assertEqual(dns, ["106", "206"])
        self.assertEqual(by_bib(res)["206"]["athlete_id"], "a206")
        live = live_scores(b.meet, b.race, rm(order, on_course=["106"]))
        self.assertIsNone(live["frozen_at"])
        self.assertEqual([x["status"] for x in live["finishers"]], ["finished"] * 10)

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            build_results(self.b.meet, self.b.race, rm([101]), statuses={"101": "SCR"})

    def test_unknown_bib_left_out_and_flagged(self):
        # 2026-10-06: a flagged runner takes no place and no point, and no
        # longer stops the race going final; it waits in results["issues"]
        b = self.b
        order = [101, 999, 201, 102, 202, 103, 203, 104, 204, 105, 205]
        res, warns = build_results(b.meet, b.race, rm(order), final=False)
        f = by_bib(res)
        self.assertNotIn("999", f)
        self.assertEqual((f["201"]["place"], f["201"]["points"]), (2, 2))   # nobody moved back
        self.assertEqual([(w["code"], w["bib"]) for w in warns], [("unknown_bib", "999")])
        (i,) = res["issues"]
        self.assertEqual((i["code"], i["bib"], i["athlete_id"], i["crossed"], i["status"], i["elapsed"]),
                         ("unknown_bib", "999", None, 2, "finished", 901.0))
        freeze(b.meet, b.race, rm(order))
        self.assertEqual(b.race.status, "final")
        self.assertEqual([x["bib"] for x in b.race.results["issues"]], ["999"])

    def test_ignored_bib_is_gone_until_its_bib_changes_hands(self):
        b = self.b
        order = [101, 999, 201, 102, 202, 103, 203, 104, 204, 105, 205]
        b.race.ignored = {"999": ""}
        res, warns = build_results(b.meet, b.race, rm(order))
        self.assertEqual((res["issues"], warns), ([], []))
        self.assertEqual(res["ignored"], [{"bib": "999", "athlete_id": None}])
        # the bib is given to someone afterwards: the ignore no longer hides it
        b.runner(999, "t1", entered=False)
        res, _ = build_results(b.meet, b.race, rm(order))
        self.assertEqual([(i["code"], i["bib"]) for i in res["issues"]], [("not_entered", "999")])
        self.assertEqual(res["ignored"], [])

    def test_marked_runner_is_not_flagged(self):
        b = self.b
        res, warns = build_results(b.meet, b.race, rm([101, 999]), statuses={"999": "DNF"})
        self.assertEqual(res["issues"], [])
        self.assertEqual(by_bib(res)["999"]["status"], "DNF")

    def test_not_entered_and_scratched_finishers_flagged(self):
        b = self.b
        b.runner(150, "t1", entered=False)
        b.runner(151, "t1", status="scratched")
        res, warns = build_results(b.meet, b.race, rm([150, 151, 101, 102, 103, 104, 105],
                                                      on_course=[152]))
        self.assertEqual({w["code"] for w in warns}, {"not_entered", "scratched", "unknown_bib"})
        f = by_bib(res)
        self.assertNotIn("150", f)
        self.assertEqual((f["101"]["place"], f["101"]["points"]), (1, 1))
        self.assertEqual([(i["bib"], i["code"], i["status"]) for i in res["issues"]],
                         [("150", "not_entered", "finished"), ("151", "scratched", "finished"),
                          ("152", "unknown_bib", "on_course")])
        self.assertFalse({"not_entered", "scratched", "unknown_bib"} & BLOCKING)

    def test_missing_splits_warn_but_do_not_block(self):
        b = self.b
        order = [101, 201, 102, 202, 103, 203, 104, 204, 105, 205, 106, 206]
        warns = freeze(b.meet, b.race, rm(order, missing={"104": ["S1"]}))
        self.assertEqual([(w["code"], w["bib"]) for w in warns], [("missing_splits", "104")])
        self.assertEqual(b.race.status, "final")

    def test_fl_only_flag_carried(self):
        res, _ = build_results(self.b.meet, self.b.race, rm([101, 102], fl_only=["102"]), final=False)
        self.assertEqual([x["fl_only"] for x in res["finishers"]], [False, True])


class TestFreeze(unittest.TestCase):

    def setUp(self):
        self.b = Builder().team("t1", "A").team("t2", "B")
        self.b.runners("t1", range(101, 106)).runners("t2", range(201, 206))
        self.order = [101, 201, 102, 202, 103, 203, 104, 204, 105, 205]

    def test_lif_hold_and_on_course_block(self):
        b = self.b
        with self.assertRaises(Unresolved) as cm:
            freeze(b.meet, b.race, rm(self.order[:-1], on_course=[205], holds={"103": "fl_untimed",
                                                                            "104": "off_list"}))
        self.assertEqual([(i["code"], i["bib"]) for i in cm.exception.issues],
                         [("lif_hold", "103"), ("lif_hold", "104"), ("on_course", "205")])
        self.assertIn("FinishLynx lists them without a time", str(cm.exception))
        self.assertIsNone(b.race.results)
        # the operator marks 205 DNF and saves the chip times to the .lif
        freeze(b.meet, b.race, rm(self.order[:-1], on_course=[205]), statuses={"205": "DNF"})
        self.assertEqual(b.race.status, "final")

    def test_freeze_sets_results_and_survives_json_round_trip(self):
        b = self.b
        warns = freeze(b.meet, b.race, rm(self.order), now=datetime(2026, 10, 10, 11, 42, 5))
        self.assertEqual(warns, [])
        self.assertEqual(b.race.status, "final")
        self.assertEqual(b.race.results["frozen_at"], "2026-10-10T11:42:05")
        self.assertIs(official_results(b.race), b.race.results)
        again = Meet.from_dict(json.loads(json.dumps(b.meet.to_dict())))
        self.assertEqual(again.races[0].results, b.race.results)
        self.assertEqual(list(again.races[0].results["teams"]), ["1"])
        self.assertEqual(again.validate(), [])

    def test_reopen_keeps_last_frozen_copy_until_refrozen(self):
        b = self.b
        freeze(b.meet, b.race, rm(self.order), now=GUN)
        frozen = b.race.results
        reopen(b.meet, b.race)
        self.assertEqual(b.race.status, "scheduled")
        self.assertIs(b.race.results, frozen)
        self.assertIsNone(official_results(b.race))
        # a bib swap fixed, frozen again: replaced
        swapped = [201, 101] + self.order[2:]
        freeze(b.meet, b.race, rm(swapped), now=GUN + timedelta(minutes=5))
        self.assertEqual(by_bib(b.race.results)["201"]["place"], 1)
        self.assertEqual(b.race.results["frozen_at"], "2026-10-10T09:05:00")

    def test_live_scores_shape_matches_frozen(self):
        b = self.b
        live = live_scores(b.meet, b.race, rm(self.order))
        freeze(b.meet, b.race, rm(self.order), now=GUN)
        self.assertEqual(live["finishers"], b.race.results["finishers"])
        self.assertEqual(live["teams"], b.race.results["teams"])
        self.assertEqual(live["issues"], b.race.results["issues"])
        self.assertEqual(set(live), {"frozen_at", "finishers", "teams", "split_labels", "issues",
                                     "ignored"})


class TestAgainstRaceManager(unittest.TestCase):
    """Same ranks, scores and places as RaceManager._team_scores when there
    are no exhibition / unattached / combined-start complications."""

    def _compare(self, seed):
        rnd = random.Random(seed)
        b = Builder()
        bibs = []
        for t in range(1, rnd.randint(3, 7) + 1):
            b.team(f"t{t}", f"Team{t}")
            n = rnd.randint(3, 9)
            team_bibs = [t * 100 + i for i in range(1, n + 1)]
            b.runners(f"t{t}", team_bibs)
            bibs += team_bibs
        rnd.shuffle(bibs)
        finished = bibs[:max(1, len(bibs) - rnd.randint(0, 4))]

        mgr = RaceManager(athlete_lookup=b.meet.athlete_lookup, finish_readers={"FIN"})
        key = mgr.create_race("JV Boys", event=1, gender="M")
        mgr.set_gun(key, GUN)
        for i, bib in enumerate(finished):
            mgr.ingest(str(bib), GUN + timedelta(minutes=17, seconds=i * 3), "FIN")
        rmr = mgr.results(key)
        res, warns = build_results(b.meet, b.race, rmr, final=False)
        self.assertEqual(warns, [])

        short = {t.id: t.short for t in b.meet.teams}
        ours = [t for t in res["teams"]["1"] if t["complete"]]
        theirs = rmr["teams"]
        self.assertEqual({short[t["team_id"]]: t["score"] for t in ours},
                         {t["team"]: t["score"] for t in theirs})
        self.assertEqual({short[t["team_id"]]: [r["points"] for r in t["runners"] if r["scorer"]]
                          for t in ours},
                         {t["team"]: t["places"] for t in theirs})
        # ranks agree except where a tie stands (neither team has a 6th)
        stands = {t["rank"] for t in ours
                  if sum(1 for o in ours if o["rank"] == t["rank"]) > 1}
        for t in ours:
            if t["rank"] not in stands:
                rm_rank = next(x["rank"] for x in theirs if x["team"] == short[t["team_id"]])
                self.assertEqual(t["rank"], rm_rank)
        self.assertEqual([f["place"] for f in res["finishers"]], list(range(1, len(finished) + 1)))
        return len(ours)

    def test_random_fields_agree(self):
        scored = sum(self._compare(seed) for seed in range(40))
        self.assertGreater(scored, 40)


if __name__ == "__main__":
    unittest.main()
