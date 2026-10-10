"""Meet-day rehearsal for running two races off one gun, scored separately
(2026-10-05, user: "running two JV boys races together. One race is grades
7-9 and the other race is grades 10 through 12"). A mock roster goes in
through the console API, the races run together (one Lynx event), every
finish comes from a FinishLynx .lif (no chip reads), and after Mark final
each race must come back out as its own results list: places, grades, team
scores and the printed sheet, checked against scoring done independently
here. python -m unittest tests.test_run_together_meet"""

import csv
import io
import math
import os
import random
import re
import unittest
from collections import defaultdict
from datetime import timedelta

from tests.test_meet_api import GUN, MeetApiBase

TEAMS = ["Ridge High School", "Valley High School", "Mesa High School",
         "Forest High School", "Grove High School", "Bluff High School"]
FIRST = ["Avery", "Blake", "Casey", "Drew", "Emery", "Finn", "Gray", "Hayden", "Jules",
         "Kai", "Lane", "Milo", "Noel", "Owen", "Parker", "Quinn", "Reese", "Sage"]
LAST = ["Birch", "Calder", "Dunmore", "Elwood", "Fairley", "Garrow", "Holt", "Jessup",
        "Kestrel", "Lowry", "Marsh", "Norcross", "Pryor", "Rowan", "Stroud", "Voss"]
# division -> (bib range, grades)
DIVISIONS = {"JV Boys 7-9": (range(300, 400), {7, 8, 9}),
             "JV Boys 10-12": (range(500, 600), {10, 11, 12})}


def roster_csv() -> str:
    """Per team 7 runners in 7-9 (bibs 3xx) and 8 in 10-12 (bibs 5xx);
    Bluff has only 4 in 7-9: an incomplete team that must not score."""
    rnd = random.Random(7)
    names = iter(rnd.sample([(f, l) for f in FIRST for l in LAST], 200))
    rows = ["First Name,Last Name,Sex,Grade,School,Bib #,Division"]
    young, old = 300, 500
    for t in TEAMS:
        for _ in range(4 if t.startswith("Bluff") else 7):
            young += 1
            first, last = next(names)
            rows.append(f"{first},{last},M,{rnd.choice([7, 8, 9])},{t},{young},JV Boys 7-9")
        for _ in range(8):
            old += 1
            first, last = next(names)
            rows.append(f"{first},{last},M,{rnd.choice([10, 11, 12])},{t},{old},JV Boys 10-12")
    return "\n".join(rows) + "\n"


def lif_text(event: str, name: str, finishers) -> str:
    """What FinishLynx saves: header, then place,bib,lane,last,first,team,time..."""
    lines = [f"{event},1,1,{name},,,,,,5000,09:00:00.000"]
    for i, (bib, last, first, team, secs) in enumerate(finishers, 1):
        m, s = divmod(secs, 60)
        t = f"{int(m)}:{s:06.3f}"
        lines.append(f"{i},{bib},,{last},{first},{team},{t},,{t},,{t},09:00:00.000,M,,,{t},{t},4.5,{t}")
    return "\r\n".join(lines) + "\r\n"


def expected(finishers, bibs: range):
    """Independent XC scoring of one division from the .lif order: 5 score,
    2 displace, a team under 5 neither scores nor displaces; ties broken by
    the 6th runner. Returns (rows [(bib, team, secs, points)], teams
    [(team, score, places)], incomplete teams)."""
    mine = [f for f in finishers if int(f[0]) in bibs]
    count = defaultdict(int)
    for f in mine:
        count[f[3]] += 1
    complete = {t for t, n in count.items() if n >= 5}
    per, rows, nxt = defaultdict(list), [], 1
    for bib, _, _, team, secs in mine:
        pts = None
        if team in complete and len(per[team]) < 7:
            pts = nxt
            per[team].append(nxt)
            nxt += 1
        rows.append((bib, team, secs, pts))
    teams = sorted(((t, sum(p[:5]), p) for t, p in per.items()),
                   key=lambda x: (x[1], x[2][5] if len(x[2]) > 5 else 10 ** 6))
    return rows, teams, sorted(t for t in count if t not in complete)


def xc_time(secs: float) -> str:
    """Printed finish time: rounded up to the hundredth."""
    t = math.ceil(round(secs * 1000) / 10) / 100
    return f"{int(t // 60)}:{t % 60:05.2f}"


class TestTwoJvBoysRacesOneGun(MeetApiBase):

    def races(self):
        return {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}

    def setUp(self):
        super().setUp()
        r = self.c.post("/api/meet/import/file", data={
            "file": (io.BytesIO(roster_csv().encode()), "jv-rehearsal.csv"),
            "name": "JV Rehearsal", "date": "2026-10-10"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        races = self.races()
        self.young = next(k for k, v in races.items() if "7-9" in v["name"])
        self.old = next(k for k, v in races.items() if "10-12" in v["name"])
        self.assertEqual((races[self.young]["entries"], races[self.old]["entries"]), (39, 48))

    def run_race(self):
        """Run together, FinishLynx times all 87 (the fields mixed), final."""
        self.post(f"/api/meet/races/{self.young}/run-together", {"races": [self.old]})
        meet = self.svc.meet
        key = tuple(map(int, self.young.split("-")))
        race = meet.race(*key)
        teams = {t.id: t.name for t in meet.teams}
        rnd = random.Random(11)
        field = []
        for e in race.entries:
            a = meet.athlete(e.athlete_id)
            base = 19 * 60 if e.event == race.event else 17 * 60     # 7-9 a bit slower
            field.append((a.bib, a.last, a.first, teams[a.team_id], base + rnd.uniform(0, 300)))
        field.sort(key=lambda f: f[4])
        pane = self.make_panes()[key]
        self.mgr.set_gun(pane, GUN)
        path = os.path.join(self.lynx, self.mgr.lif_name(self.mgr.races[pane]))
        with open(path, "w", newline="") as f:
            f.write(lif_text(str(key[0]), self.races()[self.young]["name"], field))
        t = (GUN + timedelta(minutes=30)).timestamp()
        os.utime(path, (t, t))
        self.mgr.write_merged_lif(pane, self.lynx, self.rfid)
        d = self.get(f"/api/meet/races/{self.young}/results").get_json()
        self.assertEqual(d["unresolved"], [])
        self.post(f"/api/meet/races/{self.young}/final")
        return field

    def test_one_lynx_event_with_everyone(self):
        self.post(f"/api/meet/races/{self.young}/run-together", {"races": [self.old]})
        races = self.races()
        self.assertEqual(list(races), [self.young])
        self.assertTrue(races[self.young]["run_together"])
        self.assertEqual(races[self.young]["entries"], 87)
        self.assertEqual(self.rfid_file("lynx.sch").split(), [self.young.replace("-", ",")])
        rows = [r for r in csv.reader(self.rfid_file("lynx.evt").splitlines()) if r and r[0] == ""]
        self.assertEqual(len(rows), 87)

    def test_results_come_back_out_per_race(self):
        field = self.run_race()
        meet = self.svc.meet
        d = self.get(f"/api/meet/races/{self.young}/results").get_json()
        self.assertTrue(d["official"])
        res = d["results"]
        names = {t.id: t.name for t in meet.teams}
        events = {e.num: e for e in meet.events}
        self.assertEqual(sorted(res["teams"]), sorted(str(n) for n in events))
        by_bib = {a.bib: a for a in meet.athletes}
        for num, ev in events.items():
            bibs, grades = DIVISIONS[ev.division]
            rows, teams, incomplete = expected(field, bibs)
            fs = [f for f in res["finishers"] if f["event"] == num and f["status"] == "finished"]
            with self.subTest(ev.name):
                self.assertEqual([f["bib"] for f in fs], [r[0] for r in rows])
                self.assertEqual([f["event_place"] for f in fs], list(range(1, len(rows) + 1)))
                self.assertEqual([f["points"] for f in fs], [r[3] for r in rows])
                self.assertTrue({int(meet.grade(by_bib[f["bib"]])) for f in fs} <= grades)
                ranked = [t for t in res["teams"][str(num)] if t["complete"]]
                self.assertEqual([(names[t["team_id"]], t["score"]) for t in ranked],
                                 [(t, s) for t, s, _ in teams])
                self.assertEqual(sorted(names[t["team_id"]] for t in res["teams"][str(num)]
                                        if not t["complete"]), incomplete)
        self.assertEqual(expected(field, DIVISIONS["JV Boys 7-9"][0])[2], ["Bluff High School"])

    def test_printed_sheet_has_each_race_on_its_own(self):
        field = self.run_race()
        text = self.get(f"/api/meet/print/results.txt?race={self.young}").get_data(as_text=True)
        events = {e.division: e for e in self.svc.meet.events}
        for division, (bibs, grades) in DIVISIONS.items():
            ev = events[division]
            rows, teams, incomplete = expected(field, bibs)
            sec = text.split(f"Event {ev.num}  {ev.name}\n", 1)[1].split("\nEvent ", 1)[0]
            lines = re.findall(r"^\s*(\d+) #\s+(\d+) .{24}\s+(\d+) (.{22}) +(\d+:\d\d\.\d\d)"
                               r"\s*(\d*)\s*$", sec, re.M)
            with self.subTest(division):
                self.assertEqual(
                    [(int(p), b, team.strip(), t, int(pts) if pts else None)
                     for p, b, _, team, t, pts in lines],
                    [(i, bib, team, xc_time(secs), pts)
                     for i, (bib, team, secs, pts) in enumerate(rows, 1)])
                self.assertTrue({int(y) for _, _, y, _, _, _ in lines} <= grades)
                scores = re.findall(r"^\s+\d+ (.{26}) +(\d+)((?:\s+\d+)+)\s*$",
                                    sec.split("Team Scores")[1], re.M)
                self.assertEqual([(t.strip(), int(s), [int(x) for x in r.split()])
                                  for t, s, r in scores], teams)
                for t in incomplete:
                    self.assertIn(f"{t}", sec.split("Team Scores")[1])
                    self.assertIn("incomplete, not scored", sec)


if __name__ == "__main__":
    unittest.main()
