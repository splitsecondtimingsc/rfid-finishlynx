"""Full-roster meets end to end (2026-10-05, user's test orders): bibs only
for athletes entered on MileSplit even after the races are cancelled and
new ones made; a JV boy who runs Varsity, seen by the mat, by FinishLynx,
or both; and bib swaps between race runners and roster-only athletes.

Hardware- and network-free: python -m unittest tests.test_meet_switch_and_bibs"""

import json
import os
import time
from datetime import datetime, timedelta

from tests.test_film_offset import lif_text
from tests.test_meet_api import FIXTURE, MeetApiBase

VARSITY_BOYS, JV_BOYS = "Boys 5k Run CC Varsity", "Boys 5k Run CC JV"


def real_rosters(entries):
    """team-rosters in MileSplit's real shape for the first team: its entered
    athletes again (as a roster lists them) plus a roster-only boy and girl."""
    t = entries[0]

    def rec(ms_id, first, last, gender, grad):
        return {"athlete_id": str(ms_id), "team_id": t["teamMileSplitId"], "season_year": 2026,
                "athlete": {"id": str(ms_id), "first_name": first, "last_name": last, "gender": gender,
                            "hs_graduation_year": grad, "birth_date": None, "is_deleted": 0}}
    team = {a["athleteMileSplitUniversalId"] or str(a["athleteMileSplitId"]):
            rec(a["athleteMileSplitId"], a["athleteFirstName"], a["athleteLastName"], a["athleteGender"],
                a["athleteYear"]) for a in t["roster"]}
    team["ROS*BOY"] = rec(7700001, "Rory", "Roster", "m", 2028)
    team["ROS*GIRL"] = rec(7700002, "Rhea", "Roster", "f", 2029)
    return {str(t["teamMileSplitId"]): team}


class FullRosterMeet(MeetApiBase):

    def setUp(self):
        super().setUp()
        with open(FIXTURE, encoding="utf-8") as f:
            entries = json.load(f)
        self.post("/api/meet/import/milesplit", {"entries": entries, "rosters": real_rosters(entries),
                                                 "name": "Test XC", "date": "2026-10-10",
                                                 "tc_id": 42, "meet_id": 761226})
        self.meet = self.svc.meet
        self.assertTrue(self.meet.meet.full_rosters)
        self.entered = {e.athlete_id for r in self.meet.races for e in r.entries if e.status != "scratched"}
        self.roster_boy = next(a for a in self.meet.athletes if a.first == "Rory")
        self.roster_girl = next(a for a in self.meet.athletes if a.first == "Rhea")
        self.assertNotIn(self.roster_boy.id, self.entered)

    def assign(self):
        return self.post("/api/meet/bibs/assign", {"rows": [["100", ""]], "dry_run": False})["report"]

    def race(self, name):
        return next(r for r in self.meet.races if self.meet.event(r.event).name == name)

    def rk(self, race):
        return "-".join(map(str, race.key))

    def boy_in(self, name, n=0):
        return self.meet.athlete(self.race(name).entries[n].athlete_id)


class TestBibsOnlyForOriginallyEntered(FullRosterMeet):

    def test_assign_skips_roster_only(self):
        self.assign()
        self.assertEqual({a.id for a in self.meet.athletes if a.bib}, self.entered)

    def test_cancel_every_race_add_new_races_then_assign(self):
        """User's order: import with full rosters, delete the races, add new
        blank races, assign bibs: only the originally entered get numbers."""
        for race in list(self.meet.races):
            self.c.delete(f"/api/meet/races/{self.rk(race)}")
        self.assertEqual(self.meet.races, [])
        self.assertEqual(self.svc.summary()["counts"]["entered"], 0)
        self.post("/api/meet/races", {"new": {"name": "Boys 5k Open", "gender": "M", "distance_m": 5000}})
        self.post("/api/meet/races", {"new": {"name": "Girls 5k Open", "gender": "F", "distance_m": 5000}})
        self.assertEqual(len(self.meet.races), 2)
        self.assign()
        self.assertEqual({a.id for a in self.meet.athletes if a.bib}, self.entered)
        self.assertFalse(self.roster_boy.bib or self.roster_girl.bib)

    def test_roster_athlete_entered_later_gets_a_bib(self):
        self.assign()
        new = self.post("/api/meet/races", {"new": {"name": "Boys 5k Open", "gender": "M", "distance_m": 5000}})["race"]
        self.post(f"/api/meet/athletes/{self.roster_boy.id}/entry", {"race": new, "status": "entered"})
        self.assign()
        self.assertTrue(self.roster_boy.bib)
        self.assertFalse(self.roster_girl.bib)

    def test_refresh_keeps_the_rule(self):
        with open(FIXTURE, encoding="utf-8") as f:
            entries = json.load(f)
        for race in list(self.meet.races):
            self.c.delete(f"/api/meet/races/{self.rk(race)}")
        self.post("/api/meet/refresh", {"entries": entries, "rosters": real_rosters(entries)})
        self.assign()
        self.assertFalse(self.roster_boy.bib or self.roster_girl.bib)


class RaceDay(FullRosterMeet):
    """Bibs assigned, panes made, the Varsity boys' race running."""

    def setUp(self):
        super().setUp()
        self.assign()
        self.panes = self.make_panes()
        self.gun = datetime.now().replace(microsecond=0) - timedelta(minutes=30)
        self.varsity = self.race(VARSITY_BOYS)
        self.pane = self.panes[self.varsity.key]
        self.mgr.set_gun(self.pane, self.gun)
        self.jv = self.boy_in(JV_BOYS)
        self.other_jv = self.boy_in(JV_BOYS, 1)
        self.varsity_boy = self.boy_in(VARSITY_BOYS)
        self.mgr.ingest(self.varsity_boy.bib, self.gun + timedelta(minutes=17), "FINISH")

    def results(self):
        return self.get(f"/api/meet/races/{self.rk(self.varsity)}/results").get_json()

    def races_of(self, a):
        return [r.event for r, _ in self.meet.entries_for(a.id)]

    def finishlynx_times(self, *bibs_secs):
        """FinishLynx saves the Varsity event with these (bib, seconds)."""
        with open(os.path.join(self.lynx, self.mgr.lif_name(self.mgr.races[self.pane])), "w",
                  encoding="utf-8", newline="") as f:
            f.write(lif_text(bibs_secs, start=self.gun.strftime("%H:%M:%S.0000"), event=self.varsity.event))
        self.mgr.write_merged_lif(self.pane, self.lynx, self.rfid)
        deadline = time.time() + 3
        while time.time() < deadline and (self.svc._auto_running or self.svc._auto_pending):
            time.sleep(0.02)



class TestJvBoyRunsVarsity(RaceDay):
    """A JV boy finishes the Varsity boys' race."""

    def test_mat_read_only_is_offered_not_switched(self):
        self.mgr.ingest(self.jv.bib, self.gun + timedelta(minutes=18), "FINISH")
        d = self.results()
        row = next(x for x in d["not_entered"] if x["athlete_id"] == self.jv.id)
        self.assertEqual(row["from"], ["Boys 5k Run CC JV"])
        self.assertEqual(row["blocked"], "")
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])   # not by itself
        r = self.post(f"/api/meet/races/{self.rk(self.varsity)}/switch-in", {"athletes": [self.jv.id]})
        self.assertEqual([x["athlete_id"] for x in r["switched"]], [self.jv.id])
        self.assertEqual(self.races_of(self.jv), [self.varsity.event])
        d = self.results()
        self.assertEqual(d["not_entered"], [])
        self.assertIn(self.jv.bib, {f["bib"] for f in d["results"]["finishers"] if f["status"] == "finished"})

    def test_finishlynx_only_is_flagged_not_switched(self):
        """No mat read at all: FinishLynx timed him in the Varsity race. Up
        to 2026-10-06 that switched him by itself; now he is flagged like
        any runner who isn't entered (it hid swapped bibs), and Override
        switches him."""
        self.finishlynx_times((self.varsity_boy.bib, 1020.0), (self.jv.bib, 1085.5))
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        row = next(f for f in self.mgr.results(self.pane)["finishers"] if f["bib"] == self.jv.bib)
        self.assertTrue(row["finish"].get("fl_only"))
        d = self.results()
        self.assertEqual([(i["bib"], i["code"]) for i in d["issues"]], [(self.jv.bib, "not_entered")])
        self.post(f"/api/meet/races/{self.rk(self.varsity)}/issues/{self.jv.bib}", {"action": "override"})
        self.assertEqual(self.races_of(self.jv), [self.varsity.event])
        self.assertEqual(self.results()["issues"], [])

    def test_mat_and_finishlynx_is_flagged_not_switched(self):
        self.mgr.ingest(self.jv.bib, self.gun + timedelta(minutes=18), "FINISH")
        self.finishlynx_times((self.varsity_boy.bib, 1020.0), (self.jv.bib, 1080.0))
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        self.assertEqual([x["athlete_id"] for x in self.results()["not_entered"]], [self.jv.id])

    def test_reads_in_his_own_race_too_means_ask(self):
        own = self.panes[self.race(JV_BOYS).key]
        self.mgr.set_gun(own, self.gun)
        self.mgr.ingest(self.jv.bib, self.gun + timedelta(minutes=8), "S1")
        mile = max((r for r in self.mgr.reads.values() if r.bib == self.jv.bib), key=lambda r: r.id)
        self.mgr.assign_read(mile.id, own)
        self.finishlynx_times((self.jv.bib, 1080.0))
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        self.assertIn(self.jv.id, {x["athlete_id"] for x in self.results()["not_entered"]})

    # ------------------------------------------------------------ bib swaps

    def test_swap_with_a_roster_boy_moves_the_reads_with_the_bib(self):
        bib = self.varsity_boy.bib
        self.post("/api/meet/bibs/swap", {"a": self.varsity_boy.id, "b": self.roster_boy.id})
        self.assertEqual((self.varsity_boy.bib, self.roster_boy.bib), ("", bib))
        d = self.results()
        row = next(x for x in d["not_entered"] if x["bib"] == bib)
        self.assertEqual((row["athlete_id"], row["from"], row["blocked"]), (self.roster_boy.id, [], ""))
        self.assertIn("not_entered", {i["code"] for i in d["issues"]})   # flagged, left out
        missing = self.get("/api/meet/bibs/conflicts").get_json()["missing"]
        self.assertIn(self.varsity_boy.id, {m["athlete_id"] for m in missing})
        self.assertIn(f"\n{bib},Roster,Rory,", "\n" + self.rfid_file("lynx.ppl"))   # FinishLynx shows Rory
        self.assertNotIn(f",{bib},", self.rfid_file("lynx.evt"))   # not in any race's start list
        self.assign()                                     # the runner gets a new bib, Rhea still none
        self.assertTrue(self.varsity_boy.bib)
        self.assertNotEqual(self.varsity_boy.bib, bib)
        self.assertFalse(self.roster_girl.bib)
        # FinishLynx timing Rory never enters him by itself (no race to come from)
        self.finishlynx_times((bib, 1020.0))
        self.assertEqual(self.races_of(self.roster_boy), [])

    def test_swap_across_genders_never_switches_a_girl_into_the_boys_race(self):
        bib = self.varsity_boy.bib
        self.post("/api/meet/bibs/swap", {"a": self.varsity_boy.id, "b": self.roster_girl.id})
        row = next(x for x in self.results()["not_entered"] if x["bib"] == bib)
        self.assertEqual(row["athlete_id"], self.roster_girl.id)
        self.assertIn("a girl in a boys' race", row["blocked"])
        r = self.post(f"/api/meet/races/{self.rk(self.varsity)}/switch-in", {"athletes": [self.roster_girl.id]})
        self.assertEqual(r["switched"], [])
        self.assertIn("girl", r["skipped"][0]["reason"])
        self.assertEqual(self.races_of(self.roster_girl), [])
        girl_jv = self.meet.athlete(self.race("Girls 5k Run CC JV").entries[0].athlete_id)
        self.post("/api/meet/bibs/swap", {"a": self.jv.id, "b": girl_jv.id})
        self.finishlynx_times((self.jv.bib, 1080.0))      # the JV girl now wears his bib
        self.assertEqual(self.races_of(girl_jv), [self.race("Girls 5k Run CC JV").event])

    def test_swap_between_jv_and_varsity_boys_before_the_race(self):
        """Handed out wrong, fixed before the gun: each keeps his race, the
        start lists follow, and their reads land without questions."""
        v = self.boy_in(VARSITY_BOYS, 1)
        jv_bib, v_bib = self.jv.bib, v.bib
        self.post("/api/meet/bibs/swap", {"a": self.jv.id, "b": v.id})
        self.assertEqual((self.jv.bib, v.bib), (v_bib, jv_bib))
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        self.assertEqual(self.races_of(v), [self.varsity.event])
        self.mgr.ingest(v.bib, self.gun + timedelta(minutes=18), "FINISH")
        d = self.results()
        self.assertEqual(d["not_entered"], [])
        self.assertIn(v.bib, {f["bib"] for f in d["results"]["finishers"]})

    def test_swap_after_final_keeps_the_frozen_results(self):
        for e in self.varsity.entries[1:]:
            self.post(f"/api/meet/races/{self.rk(self.varsity)}/marks",
                      {"bib": self.meet.athlete(e.athlete_id).bib, "mark": "DNS"})
        self.post(f"/api/meet/races/{self.rk(self.varsity)}/final")
        bib = self.varsity_boy.bib
        r = self.post("/api/meet/bibs/swap", {"a": self.varsity_boy.id, "b": self.roster_boy.id})
        self.assertTrue(any("final" in w for w in r["result"]["warnings"]))
        d = self.results()
        self.assertTrue(d["official"])
        first = next(f for f in d["results"]["finishers"] if f["bib"] == bib)
        self.assertEqual(first["athlete_id"], self.varsity_boy.id)


class TestOldMeetFile(FullRosterMeet):

    def test_meet_saved_before_the_flag_keeps_roster_athletes_unnumbered(self):
        from rfid_finishlynx.meet.model import Meet
        d = self.meet.to_dict()
        for a in d["athletes"]:
            del a["roster_only"]
        old = Meet.from_dict(d)
        self.assertTrue(old.athlete(self.roster_boy.id).roster_only)
        self.assertFalse(old.athlete(next(iter(self.entered))).roster_only)


class TestBoysBibOnAGirl(RaceDay):
    """A bib first given to a Varsity boy ends up on a girl (2026-10-05,
    user). Her result must find a race by the same rules as a JV boy in
    Varsity: offered at close, switched by itself when FinishLynx timed her
    there and she is entered in another girls' race - never the boys' race,
    unless she is put in a race right when the bib moves."""

    def setUp(self):
        super().setUp()
        self.girls = self.race("Girls 5k Run CC Varsity")
        self.girls_pane = self.panes[self.girls.key]
        self.mgr.set_gun(self.girls_pane, self.gun)
        self.mgr.set_status(self.pane, "closed")           # the boys ran earlier
        self.bib = self.boy_bib = self.boy_in(VARSITY_BOYS, 1).bib
        self.boy = self.boy_in(VARSITY_BOYS, 1)
        self.jv_girl = self.meet.athlete(self.race("Girls 5k Run CC JV").entries[0].athlete_id)

    def resync(self):
        """What the .evt monitor does after the start lists are rewritten."""
        from rfid_finishlynx import web
        for spec in web.evt_race_list(self.rfid_file("lynx.evt")):
            pane = self.panes.get((spec["event"], spec["round"], spec["heat"]))
            if pane is not None:
                self.mgr.set_entries(pane, spec["bibs"])

    def girls_results(self):
        return self.get(f"/api/meet/races/{self.rk(self.girls)}/results").get_json()

    def finishlynx_girls(self, *bibs_secs):
        with open(os.path.join(self.lynx, self.mgr.lif_name(self.mgr.races[self.girls_pane])), "w",
                  encoding="utf-8", newline="") as f:
            f.write(lif_text(bibs_secs, start=self.gun.strftime("%H:%M:%S.0000"), event=self.girls.event))
        self.mgr.write_merged_lif(self.girls_pane, self.lynx, self.rfid)
        deadline = time.time() + 3
        while time.time() < deadline and (self.svc._auto_running or self.svc._auto_pending):
            time.sleep(0.02)

    def read(self, bib, minutes=19):
        self.mgr.ingest(bib, self.gun + timedelta(minutes=minutes), "FINISH")
        return max((r for r in self.mgr.reads.values() if r.bib == bib), key=lambda r: r.id)

    def test_swap_with_a_jv_girl_mat_read_is_offered(self):
        self.post("/api/meet/bibs/swap", {"a": self.boy.id, "b": self.jv_girl.id})
        self.resync()
        self.assertEqual(self.jv_girl.bib, self.bib)
        self.assertEqual(self.races_of(self.jv_girl), [self.race("Girls 5k Run CC JV").event])
        self.assertEqual(self.races_of(self.boy), [self.varsity.event])
        r = self.read(self.bib)
        self.assertEqual(r.race, self.girls_pane)                 # her race by gender, not his
        row = next(x for x in self.girls_results()["not_entered"] if x["bib"] == self.bib)
        self.assertEqual((row["athlete_id"], row["from"], row["blocked"]),
                         (self.jv_girl.id, ["Girls 5k Run CC JV"], ""))

    def test_swap_with_a_jv_girl_finishlynx_flags_her(self):
        self.post("/api/meet/bibs/swap", {"a": self.boy.id, "b": self.jv_girl.id})
        self.resync()
        self.read(self.bib)
        self.finishlynx_girls((self.bib, 1140.0))
        self.assertEqual(self.races_of(self.jv_girl), [self.race("Girls 5k Run CC JV").event])
        self.post(f"/api/meet/races/{self.rk(self.girls)}/issues/{self.bib}", {"action": "override"})
        self.assertEqual(self.races_of(self.jv_girl), [self.girls.event])
        self.assertEqual(self.girls_results()["not_entered"], [])

    def test_wearer_use_roster_girl_is_not_entered_in_the_boys_race(self):
        r = self.post(f"/api/meet/bibs/{self.bib}/wearer", {"athlete_id": self.roster_girl.id})
        self.assertIsNone(r["report"]["entered_in"])
        self.assertTrue(any("boys' race" in w for w in r["report"]["warnings"]))
        self.resync()
        self.assertEqual(self.roster_girl.bib, self.bib)
        self.assertEqual(self.races_of(self.roster_girl), [])
        self.assertTrue(self.boy.bib and self.boy.bib != self.bib)        # he got the next free one
        self.read(self.bib)
        self.finishlynx_girls((self.bib, 1140.0))      # no race of her own: offered, not automatic
        row = next(x for x in self.girls_results()["not_entered"] if x["bib"] == self.bib)
        self.assertEqual((row["athlete_id"], row["from"], row["blocked"]), (self.roster_girl.id, [], ""))
        s = self.post(f"/api/meet/races/{self.rk(self.girls)}/switch-in", {"athletes": [self.roster_girl.id]})
        self.assertEqual([x["athlete_id"] for x in s["switched"]], [self.roster_girl.id])
        self.assertEqual(self.races_of(self.roster_girl), [self.girls.event])

    def test_wearer_use_with_a_race_enters_her_right_away(self):
        r = self.post(f"/api/meet/bibs/{self.bib}/wearer",
                      {"athlete_id": self.roster_girl.id, "race": self.rk(self.girls)})
        self.assertEqual(r["report"]["entered_in"], list(self.girls.key))
        self.resync()
        self.read(self.bib)
        self.assertEqual(self.girls_results()["not_entered"], [])
        self.post(f"/api/meet/bibs/{self.bib}/wearer",
                  {"athlete_id": self.roster_boy.id, "race": self.rk(self.girls)}, status=400)

    def test_wearer_new_girl_takes_no_boys_race(self):
        r = self.post(f"/api/meet/bibs/{self.bib}/wearer",
                      {"new": {"first": "Nia", "last": "New", "gender": "F"}})
        self.assertIsNone(r["report"]["entered_in"])
        self.post(f"/api/meet/bibs/{self.bib}/wearer",
                  {"new": {"first": "Ona", "last": "Other", "gender": "F", "race": self.rk(self.varsity)}},
                  status=400)


class TestCoachHandedTheBibOn(RaceDay):
    """A coach gave an absent runner's bib to a teammate without telling the
    timer (2026-10-05, user): "Someone else is wearing N" -> Use, holder gets
    "no bib", which also takes them out of their races."""

    def setUp(self):
        super().setUp()
        self.absent = self.boy_in(VARSITY_BOYS, 2)
        self.bib = self.absent.bib

    def test_no_bib_takes_the_absent_holder_out_of_his_race(self):
        r = self.post(f"/api/meet/bibs/{self.bib}/wearer",
                      {"athlete_id": self.roster_boy.id, "holder_action": "clear"})["report"]
        self.assertEqual((self.absent.bib, self.roster_boy.bib), ("", self.bib))
        self.assertEqual(r["holder_out_of"], [list(self.varsity.key)])
        self.assertEqual(self.races_of(self.absent), [])
        self.assertEqual(self.races_of(self.roster_boy), [self.varsity.event])   # took his place
        evt = self.rfid_file("lynx.evt")
        self.assertIn(f",{self.bib},,Roster,Rory,", evt)
        self.assertNotIn(f",{self.absent.last},{self.absent.first},", evt)
        self.mgr.ingest(self.bib, self.gun + timedelta(minutes=18), "FINISH")
        self.assertEqual(self.results()["not_entered"], [])
        # the absent runner is not a DNS in the final results: he is out of the race
        for e in self.varsity.entries:
            a = self.meet.athlete(e.athlete_id)
            if a.bib not in (self.bib, self.varsity_boy.bib):
                self.post(f"/api/meet/races/{self.rk(self.varsity)}/marks", {"bib": a.bib, "mark": "DNS"})
        self.post(f"/api/meet/races/{self.rk(self.varsity)}/final")
        rows = {f.get("athlete_id") for f in self.results()["results"]["finishers"]}
        self.assertNotIn(self.absent.id, rows)
        self.assertIn(self.roster_boy.id, rows)

    def test_teammate_in_another_race_keeps_his_race(self):
        self.post(f"/api/meet/bibs/{self.bib}/wearer", {"athlete_id": self.jv.id, "holder_action": "clear"})
        self.assertEqual(self.jv.bib, self.bib)
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        self.assertEqual(self.races_of(self.absent), [])

    def test_next_free_keeps_the_holder_in_his_race(self):
        r = self.post(f"/api/meet/bibs/{self.bib}/wearer", {"athlete_id": self.roster_boy.id})["report"]
        self.assertTrue(self.absent.bib and self.absent.bib != self.bib)
        self.assertEqual(r["holder_out_of"], [])
        self.assertEqual(self.races_of(self.absent), [self.varsity.event])

    def test_undo_puts_him_back(self):
        self.post(f"/api/meet/bibs/{self.bib}/wearer", {"athlete_id": self.roster_boy.id, "holder_action": "clear"})
        self.post("/api/meet/undo")
        absent = self.svc.meet.athlete(self.absent.id)
        self.assertEqual(absent.bib, self.bib)
        self.assertEqual([r.event for r, _ in self.svc.meet.entries_for(absent.id)], [self.varsity.event])


class TestOpenRace(RaceDay):
    """A race with no entries of its own (2026-10-06, user: "Always flag
    UNLESS the current race is totally empty of entries"): whoever finishes
    it is entered in it, moved out of their own race; an unknown bib is
    still flagged; once the operator enters someone it is a normal race."""

    def setUp(self):
        super().setUp()
        self.open_rk = self.post("/api/meet/races", {"new": {"name": "Boys 5k Open", "gender": "M",
                                                             "distance_m": 5000}})["race"]
        self.open_race = self.meet.race(*map(int, self.open_rk.split("-")))
        self.open_pane = self.make_panes()[self.open_race.key]
        self.mgr.set_status(self.pane, "closed")       # varsity done; only the open race runs
        self.mgr.set_gun(self.open_pane, self.gun)

    def finish(self, bib, minutes):
        self.mgr.ingest(bib, self.gun + timedelta(minutes=minutes), "FINISH")
        read = max((r for r in self.mgr.reads.values() if r.bib == bib), key=lambda r: r.id)
        if read.race != self.open_pane:
            self.mgr.assign_read(read.id, self.open_pane)
        deadline = time.time() + 3
        while time.time() < deadline and (self.svc._auto_running or self.svc._auto_pending):
            time.sleep(0.02)

    def open_results(self):
        return self.get(f"/api/meet/races/{self.open_rk}/results").get_json()

    def test_finishers_are_entered_unknown_bib_flagged(self):
        self.finish(self.jv.bib, 18)
        self.finish(self.other_jv.bib, 19)
        self.finish("98765", 20)
        self.assertEqual(self.races_of(self.jv), [self.open_race.event])
        self.assertEqual(self.races_of(self.other_jv), [self.open_race.event])
        self.assertTrue(all(e.auto for e in self.open_race.entries))
        d = self.open_results()
        self.assertEqual([(i["bib"], i["code"]) for i in d["issues"]], [("98765", "unknown_bib")])
        placed = [f["bib"] for f in d["results"]["finishers"] if f["status"] == "finished"]
        self.assertEqual(placed, [self.jv.bib, self.other_jv.bib])

    def test_an_operator_entry_makes_it_a_normal_race(self):
        self.post(f"/api/meet/athletes/{self.roster_boy.id}/entry", {"race": self.open_rk})
        self.finish(self.jv.bib, 18)
        self.assertEqual(self.races_of(self.jv), [self.race(JV_BOYS).event])
        self.assertEqual([i["code"] for i in self.open_results()["issues"]], ["not_entered"])

    def test_ignored_runner_stays_out(self):
        self.post(f"/api/meet/races/{self.open_rk}/issues/{self.varsity_boy.bib}", {"action": "ignore"})
        self.finish(self.varsity_boy.bib, 18)
        self.assertEqual(self.races_of(self.varsity_boy), [self.varsity.event])
        self.assertEqual(self.open_results()["issues"], [])


class TestRostersAddedOnRefresh(MeetApiBase):
    """A meet imported entries-only, then Refresh from MileSplit with "full
    team rosters" ticked (2026-10-08: the box was ignored on Refresh, so no
    roster-only athletes ever came in)."""

    def setUp(self):
        super().setUp()
        with open(FIXTURE, encoding="utf-8") as f:
            self.entries = json.load(f)
        self.post("/api/meet/import/milesplit", {"entries": self.entries, "name": "Test XC", "date": "2026-10-10",
                                                 "tc_id": 42, "meet_id": 761226})
        self.asked = []

        def fetch(tc, mid, full):
            self.asked.append(full)
            return self.entries, (real_rosters(self.entries) if full else None)
        self.svc.fetch_milesplit = fetch

    def counts(self):
        return self.get("/api/meet").get_json()["counts"]

    def test_ticked_box_brings_the_rosters_in_and_they_stay(self):
        self.assertEqual(self.counts()["roster_only"], 0)
        self.post("/api/meet/refresh", {"fetch": True, "full_rosters": False})
        self.assertEqual((self.asked, self.counts()["roster_only"]), ([False], 0))
        self.post("/api/meet/refresh", {"fetch": True, "full_rosters": True})
        meet = self.svc.meet
        self.assertTrue(meet.meet.full_rosters)
        self.assertEqual(sorted(a.first for a in meet.athletes if a.roster_only), ["Rhea", "Rory"])
        self.assertEqual(self.counts()["roster_only"], 2)
        self.assertIsNotNone(self.svc.store.load_pull(meet.meet.id, "rosters"))
        team = self.get(f"/api/meet/athletes?team={next(a.team_id for a in meet.athletes if a.first == 'Rory')}").get_json()
        self.assertEqual(sorted(v["first"] for v in team["athletes"] if v["status"] == "roster"), ["Rhea", "Rory"])
        self.post("/api/meet/refresh", {"fetch": True})               # box not sent: the meet keeps its rosters
        self.assertEqual((self.asked[-1], self.counts()["roster_only"]), (True, 2))
        self.post("/api/meet/bibs/assign", {"rows": [["100", ""]], "dry_run": False})
        self.assertEqual([a.bib for a in self.svc.meet.athletes if a.roster_only], ["", ""])   # never numbered
