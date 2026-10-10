"""Divisions (2026-10-06): extra standings inside a race, picked by gender,
teams and grades, re-scored among their own runners, per scoring event.

Driven through the console's API against a real RaceManager, with the
mess-ups a meet really has (user: "kids running in the wrong race by
accident, reassigning bibs afterwards, mis-entering a result, adding
something in later that was missed"). Every division standing is checked
against scoring done independently in this file.

python -m unittest tests.test_meet_divisions"""

import io
import random
import re
import unittest
from datetime import timedelta

from rfid_finishlynx import web
from rfid_finishlynx.meet import divisions as divs_mod
from rfid_finishlynx.meet import printing
from rfid_finishlynx.meet.model import Division, Meet, MeetInfo, Race
from tests.test_meet_api import GUN, MeetApiBase
from tests.test_meet_pdf import all_text
from tests.test_run_together_meet import roster_csv

LARGE = ("Ridge High School", "Valley High School", "Mesa High School")
SMALL = ("Forest High School", "Grove High School", "Bluff High School")   # Bluff: 4 in 7-9


# --------------------------------------------------------------- independent scoring

def expected(meet, race, finishers, event, member, team_scores=True, scorers=5, displacers=2):
    """Division standings worked out from scratch: the division's finishers
    in race order get places 1..n; complete teams (>= scorers eligible) take
    points in order for their first scorers + displacers; others take none.
    Returns ([(bib, place, points)], [(team_id, score, rank)] ranked teams)."""
    entry = {e.athlete_id: e for e in race.entries}
    rows = [f for f in finishers if f["status"] == "finished" and f["event"] == event
            and member(meet.athlete(f["athlete_id"]))]
    rows.sort(key=lambda f: f["place"])

    def team_of(f):
        a = meet.athlete(f["athlete_id"])
        t = meet.team(a.team_id) if a.team_id else None
        e = entry.get(a.id)                 # a runner not entered here never scores
        if a.unattached or t is None or not t.score or e is None or e.status != "entered":
            return None
        return t.id

    count = {}
    for f in rows:
        if team_of(f):
            count[team_of(f)] = count.get(team_of(f), 0) + 1
    complete = {t for t, n in count.items() if n >= scorers}
    taken, pts, out, per_team = {}, 0, [], {}
    for i, f in enumerate(rows, 1):
        t = team_of(f)
        p = None
        if team_scores and t in complete and taken.get(t, 0) < scorers + displacers:
            taken[t] = taken.get(t, 0) + 1
            pts += 1
            p = pts
            per_team.setdefault(t, []).append(p)
        out.append((f["bib"], i, p))
    teams = []
    for t, ps in per_team.items():
        tb = ps[scorers] if len(ps) > scorers else None
        teams.append((sum(ps[:scorers]), tb is None, tb or 0, t))
    teams.sort()
    ranked = [(t, s, i) for i, (s, _, _, t) in enumerate(teams, 1)]
    return out, ranked


def got(div_results, event):
    """The API's division standings in the same shape as expected()."""
    rows = [(f["bib"], f["event_place"], f["points"]) for f in div_results["finishers"]
            if f["status"] == "finished" and f["event"] == event]
    teams = [(t["team_id"], t["score"], t["rank"])
             for t in div_results["teams"].get(str(event), []) if t["rank"] is not None]
    return rows, teams


class DivisionMeet(MeetApiBase):
    """JV Boys 7-9 and JV Boys 10-12 (bibs 3xx / 5xx), six teams, grades
    7-12, each race with its own pane, gun and reader."""

    def setUp(self):
        super().setUp()
        r = self.c.post("/api/meet/import/file", data={
            "file": (io.BytesIO(roster_csv().encode()), "jv.csv"), "name": "JV", "date": "2026-10-10"},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.young = next(k for k, v in races.items() if "7-9" in v["name"])
        self.old = next(k for k, v in races.items() if "10-12" in v["name"])
        self.tid = {t.name: t.id for t in self.svc.meet.teams}
        self.setup_panes()

    def setup_panes(self):
        self.panes = self.make_panes()
        for spec in web.evt_race_list(self.rfid_file("lynx.evt")):
            key = (spec["event"], spec["round"], spec["heat"])
            if key in self.panes:
                self.mgr.set_entries(self.panes[key], spec["bibs"])
                self.mgr.set_gun(self.panes[key], GUN)

    # ---------------------------------------------------------- helpers

    def key(self, rk):
        return tuple(map(int, rk.split("-")))

    def race(self, rk):
        return self.svc.meet.race(*self.key(rk))

    def pane(self, rk):
        return self.panes[self.key(rk)]

    def bibs(self, rk):
        return sorted(self.svc.meet.athlete(e.athlete_id).bib for e in self.race(rk).entries)

    def athlete(self, bib):
        return self.svc.meet.athlete_by_bib(bib)

    def cross(self, rk, bibs, start=0):
        """Finish reads in this order, 4 s apart, placed in rk's pane."""
        for i, b in enumerate(bibs):
            self.mgr.ingest(str(b), GUN + timedelta(minutes=17, seconds=start + 4 * i), "FINISH")
            read = max((r for r in self.mgr.reads.values() if r.bib == str(b)), key=lambda r: r.id)
            if read.race != self.pane(rk):
                self.mgr.assign_read(read.id, self.pane(rk))

    def results(self, rk):
        return self.get(f"/api/meet/races/{rk}/results").get_json()

    def div(self, rk, name):
        d = next((x for x in self.results(rk)["divisions"] if x["name"] == name), None)
        return d and d["results"]

    def set_divisions(self, rows, status=200):
        return self.post("/api/meet/divisions", {"divisions": rows}, status)

    def standard_divisions(self, races=None):
        s = self.set_divisions([
            {"name": "Large", "team_ids": [self.tid[t] for t in LARGE]},
            {"name": "Small", "team_ids": [self.tid[t] for t in SMALL]},
            {"name": "Ninth", "grades": ["9"], "team_scores": False}])["meet"]
        ids = {d["name"]: d["id"] for d in s["divisions"]}
        for rk in races or (self.young, self.old):
            self.post(f"/api/meet/races/{rk}", {"divisions": list(ids.values())})
        return ids

    def member(self, name):
        meet = self.svc.meet
        d = next(x for x in meet.meet.divisions if x.name == name)
        return lambda a: divs_mod.member(d, a, meet)

    def check(self, rk, name, team_scores=True):
        """The API's standings for division `name` in every event of rk
        agree with expected()."""
        res = self.results(rk)
        race, meet = self.race(rk), self.svc.meet
        dres = next(x for x in res["divisions"] if x["name"] == name)["results"]
        events = {e.event for e in race.entries} | {f["event"] for f in res["results"]["finishers"]}
        for ev in sorted(events):
            want = expected(meet, race, res["results"]["finishers"], ev, self.member(name),
                            team_scores=team_scores)
            self.assertEqual(got(dres, ev), want, (rk, name, ev))
        return dres

    def finish_all(self, rk, order=None, skip=()):
        bibs = order or self.bibs(rk)
        self.cross(rk, [b for b in bibs if b not in skip])
        for b in skip:
            self.post(f"/api/meet/races/{rk}/marks", {"bib": b, "mark": "DNS"})


# --------------------------------------------------------------- tests

class TestNoDivisions(DivisionMeet):
    """A meet without divisions works and prints exactly as before."""

    def test_meet_without_divisions_is_unchanged(self):
        self.finish_all(self.young)
        res = self.results(self.young)
        self.assertEqual(res["divisions"], [])
        self.assertEqual(self.get("/api/meet").get_json()["divisions"], [])
        before = self.get(f"/api/meet/print/results.txt?race={self.young}").data
        self.post(f"/api/meet/races/{self.young}/final")
        final_before = self.get(f"/api/meet/print/results.txt?race={self.young}").data
        frozen = self.race(self.young).results

        # divisions in the meet but not on this race: nothing changes
        ids = self.standard_divisions(races=[self.old])
        self.assertEqual(self.results(self.young)["divisions"], [])
        self.assertEqual(self.get(f"/api/meet/print/results.txt?race={self.young}").data,
                         final_before)
        self.assertEqual(self.race(self.young).results, frozen)
        self.assertNotIn(b"Division", before)
        # on it: the race's own results are untouched; "none" prints as before
        self.post(f"/api/meet/races/{self.young}", {"divisions": list(ids.values())})
        self.assertEqual(self.results(self.young)["results"], frozen)
        self.assertEqual(self.get(f"/api/meet/print/results.txt?race={self.young}&divisions=none").data,
                         final_before)
        # and every division off again: back to the plain sheet
        self.set_divisions([])
        self.assertEqual(self.race(self.young).divisions, [])
        self.assertEqual(self.get(f"/api/meet/print/results.txt?race={self.young}").data,
                         final_before)

    def test_milesplit_payload_ignores_divisions(self):
        from rfid_finishlynx.meet import exports
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")
        plain = exports.mp_results(self.svc.snapshot(), [self.race(self.young)])
        self.standard_divisions()
        self.assertEqual(exports.mp_results(self.svc.snapshot(), [self.race(self.young)]), plain)

    def test_start_lists_not_rewritten_for_divisions(self):
        evt = self.rfid_file("lynx.evt")
        self.standard_divisions()
        self.assertEqual(self.rfid_file("lynx.evt"), evt)


class TestStandings(DivisionMeet):

    def test_rescored_inside_each_division(self):
        self.standard_divisions()
        order = self.bibs(self.young)
        random.Random(11).shuffle(order)
        self.finish_all(self.young, order)
        large = self.check(self.young, "Large")
        small = self.check(self.young, "Small")
        ninth = self.check(self.young, "Ninth", team_scores=False)
        # Small: Forest and Grove score; Bluff (4 runners) is listed, unscored
        ev = self.race(self.young).event
        ranked = {t["team_id"] for t in small["teams"][str(ev)] if t["rank"]}
        self.assertEqual(ranked, {self.tid["Forest High School"], self.tid["Grove High School"]})
        bluff = next(t for t in small["teams"][str(ev)] if t["team_id"] == self.tid["Bluff High School"])
        self.assertEqual((bluff["rank"], bluff["count"]), (None, "4/5"))
        self.assertEqual(ninth["teams"], {})
        self.assertTrue(all(f["points"] is None for f in ninth["finishers"]))
        self.assertTrue(all(self.svc.meet.grade(self.athlete(f["bib"])) == "9"
                            for f in ninth["finishers"]))
        # every runner keeps their place in the whole race beside it
        whole = {f["bib"]: f["event_place"] for f in self.results(self.young)["results"]["finishers"]}
        for f in large["finishers"]:
            self.assertEqual(f["overall_place"], whole[f["bib"]])
        # the race's own results never move
        self.assertEqual(len(large["finishers"]) + len(small["finishers"]),
                         len(self.results(self.young)["results"]["finishers"]))

    def test_live_while_running_then_final(self):
        self.standard_divisions()
        bibs = self.bibs(self.young)
        self.cross(self.young, bibs[:12])
        self.check(self.young, "Small")
        self.cross(self.young, bibs[12:], start=100)
        self.check(self.young, "Small")
        self.post(f"/api/meet/races/{self.young}/final")
        self.assertTrue(self.results(self.young)["official"])
        self.check(self.young, "Small")
        self.check(self.young, "Large")

    def test_girls_only_division_in_a_boys_race_is_empty(self):
        s = self.set_divisions([{"name": "Girls", "gender": "F"}])["meet"]
        self.post(f"/api/meet/races/{self.young}", {"divisions": [s["divisions"][0]["id"]]})
        self.finish_all(self.young)
        d = self.div(self.young, "Girls")
        self.assertEqual((d["finishers"], d["teams"]), ([], {}))
        text = self.get(f"/api/meet/print/results.txt?race={self.young}").data.decode()
        self.assertNotIn("Division: Girls", text)          # nobody in it, no section


class TestMessUps(DivisionMeet):
    """What goes wrong on meet day, with divisions on."""

    def setUp(self):
        super().setUp()
        self.ids = self.standard_divisions()

    def small_bib(self, rk):
        small = {self.tid[t] for t in SMALL[:2]}
        for b in self.bibs(rk):
            if self.athlete(b).team_id in small:
                return b

    def test_kid_runs_in_the_wrong_race(self):
        """A 10-12 Forest runner lines up with 7-9. The timer doesn't deal
        with the flag and marks the race final: the runner is left out of
        the race and its divisions, kept as a flag; Override afterwards
        re-finalizes by itself and they score in Small (2026-10-06)."""
        stray = self.small_bib(self.old)
        young = self.bibs(self.young)
        self.cross(self.young, young[:10] + [stray] + young[10:])
        self.assertEqual([i["bib"] for i in self.results(self.young)["issues"]], [stray])
        self.post(f"/api/meet/races/{self.young}/final")
        self.assertNotIn(stray, {f["bib"] for f in self.div(self.young, "Small")["finishers"]})
        self.check(self.young, "Small")
        r = self.post(f"/api/meet/races/{self.young}/issues/{stray}", {"action": "override"})
        self.assertEqual(r["report"]["refinalized"], [self.young])
        self.assertEqual(self.race(self.young).status, "final")
        row = next(f for f in self.div(self.young, "Small")["finishers"] if f["bib"] == stray)
        self.assertEqual(row["status"], "finished")
        self.check(self.young, "Small")
        self.check(self.young, "Large")
        # and they are out of the race they were entered in
        aid = self.athlete(stray).id
        self.assertNotIn(aid, {e.athlete_id for e in self.race(self.old).entries})

    def test_bibs_swapped_after_final_then_refinalized(self):
        """Two kids wore each other's bibs: a Ridge finisher and a Forest
        runner marked DNS. Final results (and their divisions) keep the old
        bibs until Re-finalize; then both standings follow."""
        bibs = self.bibs(self.young)
        ridge = next(b for b in bibs if self.athlete(b).team_id == self.tid["Ridge High School"])
        forest = next(b for b in bibs if self.athlete(b).team_id == self.tid["Forest High School"])
        self.finish_all(self.young, skip=[forest])
        self.post(f"/api/meet/races/{self.young}/final")
        a, b = self.athlete(ridge), self.athlete(forest)
        self.assertIn(ridge, {f["bib"] for f in self.div(self.young, "Large")["finishers"]
                              if f["status"] == "finished"})
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        # frozen: the finish still belongs to the Ridge runner everywhere
        large = self.div(self.young, "Large")
        self.assertIn(a.id, {f["athlete_id"] for f in large["finishers"] if f["status"] == "finished"})
        self.check(self.young, "Large")
        # the mark was on the bib Forest wore; it now belongs to Ridge's athlete,
        # and the finish read to Forest's: move the DNS to who didn't run
        self.post(f"/api/meet/races/{self.young}/reopen")
        self.post(f"/api/meet/races/{self.young}/marks", {"bib": forest, "mark": None})
        self.post(f"/api/meet/races/{self.young}/final")
        small = self.div(self.young, "Small")
        self.assertIn(b.id, {f["athlete_id"] for f in small["finishers"] if f["status"] == "finished"})
        large = self.div(self.young, "Large")
        self.assertNotIn(a.id, {f["athlete_id"] for f in large["finishers"] if f["status"] == "finished"})
        self.check(self.young, "Large")
        self.check(self.young, "Small")

    def test_refinalize_after_swap_moves_division_standings(self):
        bibs = self.bibs(self.young)
        ridge = [b for b in bibs if self.athlete(b).team_id == self.tid["Ridge High School"]][:1]
        grove = [b for b in bibs if self.athlete(b).team_id == self.tid["Grove High School"]][:1]
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")
        a, b = self.athlete(ridge[0]), self.athlete(grove[0])
        before = {f["athlete_id"]: f["event_place"] for f in self.div(self.young, "Small")["finishers"]}
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        self.assertEqual({f["athlete_id"]: f["event_place"]
                          for f in self.div(self.young, "Small")["finishers"]}, before)   # frozen
        self.post(f"/api/meet/races/{self.young}/refinalize")
        self.check(self.young, "Small")
        self.check(self.young, "Large")

    def test_wrong_mark_then_corrected(self):
        """A Grove scorer DQ'd by mistake (should have been the bib next to
        it), then cleared: the division closes up and comes back."""
        self.finish_all(self.young)
        first = self.check(self.young, "Small")
        grove = next(f["bib"] for f in first["finishers"]
                     if self.athlete(f["bib"]).team_id == self.tid["Grove High School"])
        self.post(f"/api/meet/races/{self.young}/marks", {"bib": grove, "mark": "DQ"})
        dq = self.check(self.young, "Small")
        row = next(f for f in dq["finishers"] if f["bib"] == grove)
        self.assertEqual((row["status"], row["event_place"], row["points"]), ("DQ", None, None))
        self.post(f"/api/meet/races/{self.young}/marks", {"bib": grove, "mark": None})
        self.assertEqual(self.check(self.young, "Small")["finishers"], first["finishers"])
        self.post(f"/api/meet/races/{self.young}/marks", {"bib": grove, "mark": "DNF"})
        self.post(f"/api/meet/races/{self.young}/final")
        self.check(self.young, "Small")

    def test_walkup_whose_read_came_in_first(self):
        """Bib 399 crossed but nobody was given it. Before final: the walk-up
        is added with the bib and the race, and lands in Small and Ninth."""
        young = self.bibs(self.young)
        self.cross(self.young, young[:3] + ["399"] + young[3:])
        self.assertEqual([(i["bib"], i["code"]) for i in self.results(self.young)["issues"]],
                         [("399", "unknown_bib")])
        self.post("/api/meet/athletes", {"first": "Wren", "last": "Late", "gender": "M", "grade": "9",
                                         "team_id": self.tid["Forest High School"],
                                         "race": self.young, "bib": "399"})
        self.assertIn("399", {f["bib"] for f in self.div(self.young, "Small")["finishers"]})
        ninth = self.div(self.young, "Ninth")
        self.assertEqual(next(f for f in ninth["finishers"] if f["bib"] == "399")["status"], "finished")
        self.post(f"/api/meet/races/{self.young}/final")
        self.check(self.young, "Small")
        self.check(self.young, "Ninth", team_scores=False)

    def test_walkup_added_after_final(self):
        """The race went final with bib 399 flagged. The console's "Add new
        athlete with bib 399" adds them (a final race can't take an entry
        directly), then Overrides: re-finalized, in Small and Ninth."""
        young = self.bibs(self.young)
        self.cross(self.young, young[:3] + ["399"] + young[3:])
        self.post(f"/api/meet/races/{self.young}/final")
        self.assertNotIn("399", {f["bib"] for f in self.div(self.young, "Ninth")["finishers"]})
        self.post("/api/meet/athletes", {"first": "Wren", "last": "Late", "gender": "M", "grade": "9",
                                         "team_id": self.tid["Forest High School"], "bib": "399"})
        r = self.post(f"/api/meet/races/{self.young}/issues/399", {"action": "override"})
        self.assertEqual(r["report"]["refinalized"], [self.young])
        self.assertEqual(self.results(self.young)["issues"], [])
        self.check(self.young, "Small")
        self.check(self.young, "Ninth", team_scores=False)
        self.assertIn("399", {f["bib"] for f in self.div(self.young, "Ninth")["finishers"]})

    def test_team_and_grade_fixed_after_final(self):
        """Who is in a division is read from the athletes as they are now:
        a grade or team fixed after the race moves the runner between
        divisions at once. The race's own frozen team scores wait for
        Re-finalize, as before."""
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")
        frozen = self.race(self.young).results
        ninth = {f["bib"] for f in self.div(self.young, "Ninth")["finishers"]}
        eighth = next(b for b in self.bibs(self.young) if self.svc.meet.grade(self.athlete(b)) == "8")
        self.post(f"/api/meet/athletes/{self.athlete(eighth).id}", {"grade": "9"})
        self.assertEqual({f["bib"] for f in self.div(self.young, "Ninth")["finishers"]}, ninth | {eighth})
        ridge = next(b for b in self.bibs(self.young)
                     if self.athlete(b).team_id == self.tid["Ridge High School"])
        self.post(f"/api/meet/athletes/{self.athlete(ridge).id}",
                  {"team_id": self.tid["Forest High School"]})
        self.assertIn(ridge, {f["bib"] for f in self.div(self.young, "Small")["finishers"]})
        self.assertNotIn(ridge, {f["bib"] for f in self.div(self.young, "Large")["finishers"]})
        self.check(self.young, "Small")
        self.check(self.young, "Large")
        self.assertEqual(self.race(self.young).results, frozen)
        self.post(f"/api/meet/races/{self.young}/refinalize")
        self.check(self.young, "Small")

    def test_exhibition_runner_places_but_never_scores(self):
        self.finish_all(self.young)
        first = self.check(self.young, "Small")
        scorer = next(f for f in first["finishers"] if f["points"] is not None)
        self.post(f"/api/meet/athletes/{scorer['athlete_id']}/entry",
                  {"race": self.young, "status": "exhibition"})
        after = self.check(self.young, "Small")
        row = next(f for f in after["finishers"] if f["bib"] == scorer["bib"])
        self.assertEqual((row["event_place"], row["points"]), (scorer["event_place"], None))

    def test_scratched_runner_who_finished(self):
        self.finish_all(self.young)
        b = self.small_bib(self.young)
        self.post(f"/api/meet/athletes/{self.athlete(b).id}/entry",
                  {"race": self.young, "status": "scratched"})
        self.assertEqual([(i["bib"], i["code"]) for i in self.results(self.young)["issues"]],
                         [(b, "scratched")])
        self.check(self.young, "Small")          # left out of the standings meanwhile
        self.post(f"/api/meet/races/{self.young}/final")
        self.post(f"/api/meet/races/{self.young}/issues/{b}", {"action": "override"})   # un-scratch
        self.assertEqual(next(e for e in self.race(self.young).entries
                              if e.athlete_id == self.athlete(b).id).status, "entered")
        self.assertIn(b, {f["bib"] for f in self.div(self.young, "Small")["finishers"]})
        self.check(self.young, "Small")

    def test_divisions_added_changed_and_deleted_after_final(self):
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")
        frozen = self.race(self.young).results
        rows = self.get("/api/meet").get_json()["divisions"]
        # a missed division, added afterwards: no re-finalize needed
        rows.append({"name": "Ridge+Forest", "team_ids": [self.tid["Ridge High School"],
                                                          self.tid["Forest High School"]]})
        s = self.set_divisions(rows)["meet"]
        new = next(d for d in s["divisions"] if d["name"] == "Ridge+Forest")
        self.assertNotIn(new["id"], self.ids.values())
        self.post(f"/api/meet/races/{self.young}",
                  {"divisions": list(self.ids.values()) + [new["id"]]})
        self.check(self.young, "Ridge+Forest")
        # Small turns out to include Mesa too, and gets a better name
        for d in rows:
            if d["name"] == "Small":
                d["team_ids"].append(self.tid["Mesa High School"])
                d["name"] = "Small / Mesa"
        rows[-1]["id"] = new["id"]
        self.set_divisions(rows)
        self.check(self.young, "Small / Mesa")
        # Large deleted: gone from every race and from the results
        self.set_divisions([d for d in rows if d["name"] != "Large"])
        self.assertNotIn(self.ids["Large"], self.race(self.young).divisions)
        self.assertNotIn(self.ids["Large"], self.race(self.old).divisions)
        self.assertNotIn("Large", {d["name"] for d in self.results(self.young)["divisions"]})
        self.get(f"/api/meet/print/results.txt?race={self.young}&divisions={self.ids['Large']}", 400)
        self.assertEqual(self.race(self.young).results, frozen)
        self.assertEqual(self.race(self.young).status, "final")

    def test_bad_division_edits_are_refused_and_change_nothing(self):
        before = self.get("/api/meet").get_json()["divisions"]
        small = [self.tid[t] for t in SMALL]
        for rows in ([{"name": ""}],
                     [{"name": "A"}, {"name": "a"}],
                     [{"name": "A", "gender": "Q"}],
                     [{"name": "A", "team_ids": ["t999"]}],
                     [{"id": "d999", "name": "A"}],
                     [{"name": "A", "team_ids": "t1"}]):
            self.set_divisions(rows, 400)
        self.post("/api/meet/divisions", {"divisions": "x"}, 400)
        self.post(f"/api/meet/races/{self.young}", {"divisions": ["d999"]}, 400)
        self.post(f"/api/meet/races/{self.young}", {"divisions": "d1"}, 400)
        self.assertEqual(self.get("/api/meet").get_json()["divisions"], before)
        # gender words are accepted
        s = self.set_divisions(before + [{"name": "Both", "gender": "both", "team_ids": small}])
        self.assertEqual(s["meet"]["divisions"][-1]["gender"], "")

    def test_every_race_at_once(self):
        self.post("/api/meet/races/divisions", {"divisions": [self.ids["Ninth"]]})
        self.assertEqual(self.race(self.young).divisions, [self.ids["Ninth"]])
        self.assertEqual(self.race(self.old).divisions, [self.ids["Ninth"]])
        self.post("/api/meet/races/divisions", {"divisions": [], "races": [self.old]})
        self.assertEqual(self.race(self.old).divisions, [])
        self.assertEqual(self.race(self.young).divisions, [self.ids["Ninth"]])
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual(races[self.young]["division_ids"], [self.ids["Ninth"]])


class TestRunTogether(DivisionMeet):
    """Two races off one gun: each division scored per scoring event."""

    def setup_panes(self):
        # no panes (or guns) until the races are run together: a race that
        # has started can't be
        if getattr(self, "together", False):
            super().setup_panes()

    def setUp(self):
        super().setUp()
        self.ids = self.standard_divisions(races=[self.young])
        s = self.set_divisions(self.get("/api/meet").get_json()["divisions"]
                               + [{"name": "Everyone"}])["meet"]
        self.every = s["divisions"][-1]["id"]
        self.post(f"/api/meet/races/{self.old}", {"divisions": [self.every]})
        self.post(f"/api/meet/races/{self.young}/run-together", {"races": [self.old]})
        self.together = True
        self.setup_panes()

    def test_divisions_merge_and_never_pool_events(self):
        race = self.race(self.young)
        self.assertEqual(set(race.divisions), set(self.ids.values()) | {self.every})
        order = self.bibs(self.young)
        random.Random(5).shuffle(order)
        self.finish_all(self.young, order)
        every = self.check(self.young, "Everyone")
        evs = sorted({e.event for e in race.entries})
        self.assertEqual(len(evs), 2)
        for ev in evs:
            places = [f["event_place"] for f in every["finishers"]
                      if f["event"] == ev and f["status"] == "finished"]
            self.assertEqual(places, list(range(1, len(places) + 1)))     # restarts per event
            self.assertIn(str(ev), every["teams"])
        self.check(self.young, "Small")
        self.check(self.young, "Ninth", team_scores=False)
        self.post(f"/api/meet/races/{self.young}/final")
        self.check(self.young, "Small")

    def test_kid_entered_in_the_wrong_event_of_the_gun(self):
        """A 10-12 runner entered in 7-9 by mistake: fixed before final, they
        move to the other event's division standings."""
        race = self.race(self.young)
        evs = sorted({e.event for e in race.entries})
        b = next(x for x in self.bibs(self.young)
                 if next(e for e in race.entries if e.athlete_id == self.athlete(x).id).event == evs[0])
        self.finish_all(self.young)
        self.post(f"/api/meet/athletes/{self.athlete(b).id}/entry", {"race": self.young, "event": evs[1]})
        row = next(f for f in self.div(self.young, "Everyone")["finishers"] if f["bib"] == b)
        self.assertEqual(row["event"], evs[1])
        self.check(self.young, "Everyone")

    def test_run_separately_gives_each_race_the_divisions(self):
        # (the console refuses it once the gun went; the rule itself:)
        from rfid_finishlynx.meet import edits
        meet = self.svc.snapshot()
        race = meet.race(*self.key(self.young))
        edits.run_separately(meet, race)
        merged = set(self.ids.values()) | {self.every}
        self.assertEqual(set(race.divisions), merged)
        self.assertEqual(set(meet.race(*self.key(self.old)).divisions), merged)
        self.assertEqual(meet.validate(), [])


class TestPrinting(DivisionMeet):

    def setUp(self):
        super().setUp()
        self.ids = self.standard_divisions()
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")

    def text(self, report, q):
        return self.get(f"/api/meet/print/{report}.txt?{q}").data.decode()

    def test_results_sections(self):
        t = self.text("results", f"race={self.young}")
        self.assertIn("Division: Large", t)
        self.assertIn("Division: Small", t)
        self.assertIn("Division: Ninth", t)
        self.assertLess(t.index("Division: Large"), t.index("Division: Small"))
        only = self.text("results", f"race={self.young}&divisions={self.ids['Small']}")
        self.assertIn("Division Small", only)            # header says what it is
        self.assertIn("Division: Small", only)
        self.assertNotIn("Division: Large", only)
        self.assertEqual(only.count("Event "), 1)       # no overall section
        plain = self.text("results", f"race={self.young}&divisions=none")
        self.assertNotIn("Division", plain)
        # places filter counts division places
        top = self.text("results", f"race={self.young}&divisions={self.ids['Small']}&places=1-3")
        rows = [l for l in top.splitlines() if re.match(r"\s+\d+\s+#", l)]
        self.assertEqual([int(l.split()[0]) for l in rows], [1, 2, 3])
        # a grade filter drops the team scores in division sections too
        g = self.text("results", f"race={self.young}&divisions={self.ids['Small']}&grades=9")
        self.assertIn(printing.GRADE_FILTER_NO_TEAMS, g)
        # several races, one of them without results yet
        self.assertIn("Division: Small", self.text("meet-results", ""))
        self.get(f"/api/meet/print/results.txt?race={self.young}&divisions=zz", 400)

    def test_pdf_and_live(self):
        pdf = self.get(f"/api/meet/print/results.pdf?race={self.young}").data
        words = " ".join(" ".join(p) for p in all_text(self, pdf))
        self.assertIn("Division: Small", words)
        self.assertIn("Division: Ninth", words)
        two = self.get(f"/api/meet/print/results.pdf?race={self.young}&columns=2"
                       f"&divisions={self.ids['Large']}").data
        self.assertTrue(two.startswith(b"%PDF"))
        # a race still running prints its division standings as they stand
        self.cross(self.old, self.bibs(self.old)[:15])
        live = self.text("results", f"race={self.old}")
        self.assertIn("NOT MARKED COMPLETE", live)
        self.assertIn("Division: Small", live)
        both = self.get("/api/meet/print/meet-results.pdf").data
        self.assertTrue(both.startswith(b"%PDF"))

    def test_awards_announcer_team_scores(self):
        a = self.text("announcer", f"race={self.young}&top=3&team_count=all")
        self.assertIn(" - Small", a)
        self.assertIn(" - Ninth", a)
        self.assertNotIn(" - Ninth\n" + "=" * 72 + "\nINDIVIDUALS (3)\nTEAMS", a)
        aw = self.text("awards", f"race={self.young}")
        self.assertIn(" - Large", aw)
        ts = self.text("team-scores", f"race={self.young}")
        self.assertIn(" - Small", ts)
        self.assertNotIn(" - Ninth", ts)                 # individuals only: no team scores
        none = self.text("announcer", f"race={self.young}&divisions=none")
        self.assertNotIn(" - Small", none)
        for name in ("awards", "announcer", "team-scores", "award-labels"):
            self.assertTrue(self.get(f"/api/meet/print/{name}.pdf?race={self.young}").data
                            .startswith(b"%PDF"), name)
        # the division's winner is the first Small finisher, not the race's
        small = self.div(self.young, "Small")
        winner = self.athlete(next(f["bib"] for f in small["finishers"] if f["event_place"] == 1))
        sec = a[a.index(" - Small"):]
        line = next(l for l in sec.splitlines() if l.startswith("1st Place:"))
        self.assertIn(winner.last, line)


class TestPrintInBrowser(DivisionMeet):
    """Printouts open in a browser tab (2026-10-06): inline=1 serves them to
    be shown, not downloaded; errors come back as a page, not JSON."""

    def test_inline_and_name_hint(self):
        self.finish_all(self.young)
        self.post(f"/api/meet/races/{self.young}/final")
        r = self.get(f"/api/meet/print/results.pdf?race={self.young}&inline=1&name=Boys_JV-results.pdf")
        self.assertEqual(r.headers["Content-Disposition"], 'inline; filename="Boys_JV-results.pdf"')
        self.assertTrue(r.data.startswith(b"%PDF"))
        r = self.get(f"/api/meet/print/results.txt?race={self.young}&inline=1&name=..%2F..%2Fx")
        self.assertEqual(r.headers["Content-Disposition"], 'inline; filename="x.txt"')
        r = self.get(f"/api/meet/print/results.txt?race={self.young}")
        self.assertTrue(r.headers["Content-Disposition"].startswith("attachment; filename="))

    def test_errors_show_as_a_page(self):
        r = self.c.get(f"/api/meet/print/results.pdf?race={self.young}&inline=1&divisions=zz")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.mimetype, "text/html")
        self.assertIn(b"no division zz", r.data)
        r = self.c.get("/api/meet/print/results.pdf?race=9-1-1&inline=1")
        self.assertEqual(r.status_code, 404)
        self.assertIn(b"Printout not made", r.data)
        r = self.c.get(f"/api/meet/print/results.pdf?race={self.young}&divisions=zz")
        self.assertEqual(r.get_json()["error"], "no division zz in this meet")


class TestModel(unittest.TestCase):

    def test_round_trip_and_old_files(self):
        m = Meet(meet=MeetInfo(id="x", name="X", date="2026-10-10"))
        m.meet.divisions = [Division("d1", "Small", gender="F", team_ids=[], grades=["9", "10"],
                                     team_scores=False)]
        m.races = [Race(1, divisions=["d1"])]
        back = Meet.from_dict(m.to_dict())
        self.assertEqual(back.meet.divisions, m.meet.divisions)
        self.assertEqual(back.races[0].divisions, ["d1"])
        old = m.to_dict()
        del old["meet"]["divisions"]
        del old["races"][0]["divisions"]
        o = Meet.from_dict(old)
        self.assertEqual((o.meet.divisions, o.races[0].divisions), ([], []))
        # a newer file's unknown keys are ignored
        newer = m.to_dict()
        newer["meet"]["divisions"][0]["colour"] = "red"
        self.assertEqual(Meet.from_dict(newer).meet.divisions[0].name, "Small")

    def test_validate(self):
        m = Meet(meet=MeetInfo(id="x", name="X", date="2026-10-10"))
        m.meet.divisions = [Division("d1", "A"), Division("d1", "a", gender="X", team_ids=["t9"])]
        m.races = [Race(1, divisions=["d2", "d1", "d1"])]
        errs = "\n".join(m.validate())
        for want in ("division id d1 used twice", "gender 'X'", "no team t9",
                     "same name", "no division d2", "a division listed twice"):
            self.assertIn(want, errs)

    def test_new_ids_skip_used_ones(self):
        m = Meet(meet=MeetInfo(id="x", name="X"))
        m.meet.divisions = [Division("d3", "A")]
        m.races = [Race(1, divisions=["d7"])]
        self.assertEqual(m.new_division_id(), "d8")
        self.assertEqual(m.new_division_id(taken=["d8"]), "d9")


if __name__ == "__main__":
    unittest.main()
