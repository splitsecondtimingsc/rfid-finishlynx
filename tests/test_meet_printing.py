"""Printed meet documents (2026-10-03): results / start list / bib sheet text
in the Hy-Tek layout, and the PDF + HTML made from that same text.
Fixtures are hand-built in the frozen-results shape scoring.py writes.
python -m unittest tests.test_meet_printing"""

import os
import re
import unittest
from datetime import datetime

from rfid_finishlynx.meet import printing
from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, Race, Scoring,
                                        Team)

PRINTED = datetime(2026, 10, 10, 12, 5)
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "printing")


def fin(place, bib, aid, elapsed, event_place, points=None, scorer=False, displacer=False,
        status="finished", event=1):
    return {"place": place, "bib": bib, "athlete_id": aid, "event": event, "elapsed": elapsed,
            "status": status, "event_place": event_place, "points": points, "scorer": scorer,
            "displacer": displacer, "fl_only": False}


def runner(aid, bib, points, scorer=True):
    return {"athlete_id": aid, "bib": bib, "points": points, "scorer": scorer}


def small_meet() -> Meet:
    """One girls race, 3 scorers + 1 displacer so the golden text stays short:
    two complete teams, a partial one, an exhibition, an unattached runner,
    a DNF and a DNS, an accented name and a name too long for its column."""
    m = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10",
                           host="Ridge High School", scoring=Scoring(scorers=3, displacers=1)))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID"),
               Team("t2", "Valley Christian Academy Lakes", short="Valley Christian", code="VCA"),
               Team("t3", "Hillcrest", short="Hill", code="HIL")]
    A = Athlete
    m.athletes = [A("a1", "Avery", "Birch", "F", "t1", "101", grad_year=2027),
                  A("a2", "Zoë", "Núñez", "F", "t2", "201", grad_year=2028),
                  A("a3", "Casey", "Dunmore", "F", "t1", "102", grad_year=2029),
                  A("a4", "Dana", "Ellery", "F", "t2", "202", grad_year=2030),
                  A("a5", "Emery", "Fontaine-Whitlock-Abernathy", "F", "t1", "103", grad_year=2027),
                  A("a6", "Finley", "Garth", "F", "t3", "301", grad_year=2028),
                  A("a7", "Gray", "Hollis", "F", "t2", "203", grad_year=2027),
                  A("a8", "Harper", "Ives", "F", "t1", "104", grad_year=2029),
                  A("a9", "Indy", "Jarvis", "F", "t2", "204", grad_year=2028),
                  A("a10", "Jules", "Keane", "F", "t3", "302", grad_year=2029),
                  A("a11", "Kai", "Lark", "F", "t1", "105", grade_text="Senior"),
                  A("a12", "Lee", "Moss", "F", None, "900", grad_year=2028)]
    m.events = [Event(1, "Girls 5k Run CC Varsity", gender="F", distance_m=5000,
                      division="Varsity Girls", code="5000")]
    entries = [Entry(f"a{i}", 1, seed=f"2{i % 10}:0{i % 6}.00") for i in range(1, 13)]
    entries[7].status = "exhibition"                       # a8 Ives
    entries.append(Entry("a13", 1, status="scratched"))
    m.athletes.append(A("a13", "Morgan", "Nash", "F", "t2", "205", grad_year=2030))
    race = Race(1, order=1, status="final", entries=entries)
    race.results = {
        "frozen_at": "2026-10-10T11:42:05",
        "finishers": [
            fin(1, "101", "a1", 1103.511, 1, 1, scorer=True),     # .511 prints .52 (round up)
            fin(2, "201", "a2", 1110.00, 2, 2, scorer=True),
            fin(3, "102", "a3", 1115.255, 3, 3, scorer=True),
            fin(4, "900", "a12", 1120.4, 4),                       # unattached
            fin(5, "202", "a4", 1124.9, 5, 4, scorer=True),
            fin(6, "301", "a6", 1130.0, 6),                        # partial team
            fin(7, "104", "a8", 1133.3, 7),                        # exhibition
            fin(8, "103", "a5", 1140.12, 8, 5, scorer=True),
            fin(9, "203", "a7", 1150.0, 9, 6, scorer=True),
            fin(10, "204", "a9", 1166.66, 10, 7, displacer=True),
            fin(None, "302", "a10", None, None, status="DNF"),
            fin(None, "105", "a11", None, None, status="DNS"),
        ],
        "teams": {"1": [
            {"rank": 1, "team_id": "t1", "score": 9, "complete": True, "count": "3/3",
             "runners": [runner("a1", "101", 1), runner("a3", "102", 3), runner("a5", "103", 5)],
             "tiebreak_points": None, "total_time": 3358.886, "avg_time": 1119.6287},
            {"rank": 2, "team_id": "t2", "score": 12, "complete": True, "count": "4/3",
             "runners": [runner("a2", "201", 2), runner("a4", "202", 4), runner("a7", "203", 6),
                         runner("a9", "204", 7, scorer=False)],
             "tiebreak_points": 7, "total_time": 3384.9, "avg_time": 1128.3},
            {"rank": None, "team_id": "t3", "score": None, "complete": False, "count": "1/3",
             "runners": [runner("a6", "301", None, scorer=False)],
             "tiebreak_points": None, "total_time": None, "avg_time": None},
        ]},
    }
    m.races = [race]
    return m


def synthetic_meet() -> Meet:
    """A realistic synthetic meet for the multi-page tests (and the sample
    PDFs): 6 teams, 50 runners, a boys race and a combined girls start
    (varsity + JV scored apart), exhibitions, a scratch, partial teams, an
    unattached runner, a DNF and a DNS. Seeded, so always the same meet.
    The frozen results come from a throwaway 5+2 scorer below, not
    scoring.py: these tests are about printing, not scoring."""
    import random
    rnd = random.Random(7)
    first = ["Avery", "Blake", "Casey", "Dana", "Emery", "Finley", "Gray", "Harper", "Indy",
             "Jules", "Kai", "Lane", "Morgan", "Noel", "Oakley", "Parker", "Quinn", "Reese",
             "Zoë", "José", "Renée", "Łukasz", "Søren"]
    last = ["Birch", "Calder", "Dunmore", "Ellery", "Fenwick", "Garth", "Hollis", "Ives",
            "Jarvis", "Keane", "Lark", "Moss", "Núñez", "Oakes", "Pryor", "Quill", "Rowe",
            "Sable", "Thorne", "Castellanos-Whitfield", "O'Day"]
    m = Meet(meet=MeetInfo(id="2026-10-10-sample", name="Synthetic Valley Invitational",
                           date="2026-10-10", host="Ridgeview High School"))
    m.teams = [Team("t1", "Ridgeview High School", "Ridgeview", "RVH"),
               Team("t2", "Lakeside Christian Academy of the Pines", "Lakeside Chr", "LCA"),
               Team("t3", "North Hollow", "N Hollow", "NHO"),
               Team("t4", "St. Bramble Prep", "St Bramble", "SBP"),
               Team("t5", "Eastfield", "Eastfield", "EFD"),
               Team("t6", "Westmoor A&M Academy", "Westmoor", "WMA")]
    m.events = [Event(1, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys", "5000"),
                Event(2, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls", "5000"),
                Event(3, "Girls 5k Run CC JV", "F", 5000, "JV Girls", "5000")]
    plan = {1: {"t1": 7, "t2": 6, "t3": 5, "t4": 6, "t5": 3, "t6": 2},
            2: {"t1": 6, "t2": 5, "t3": 2},
            3: {"t1": 3, "t4": 5}}
    races = {1: Race(1, order=1), 2: Race(2, order=2, name="Girls 5k Combined")}
    n = 0
    for ev, teams in plan.items():
        for tid, k in teams.items():
            for _ in range(k):
                n += 1
                m.athletes.append(Athlete(f"a{n}", rnd.choice(first), rnd.choice(last),
                                          "M" if ev == 1 else "F", tid,
                                          str(100 * int(tid[1:]) + n),
                                          grad_year=rnd.choice([2027, 2028, 2029, 2030])))
                races[min(ev, 2)].entries.append(Entry(
                    f"a{n}", ev, seed=f"{rnd.randint(16, 24)}:{rnd.randint(0, 59):02d}.00"))
    m.athletes.append(Athlete("a99", "Unity", "Solo", "M", None, "999", grad_year=2028))
    races[1].entries.append(Entry("a99", 1))
    races[1].entries[2].status = "exhibition"
    races[1].entries[9].status = "exhibition"
    races[1].entries[5].status = "scratched"
    m.races = list(races.values())
    for race in m.races:
        _freeze(m, race, rnd)
    return m


def _freeze(m: Meet, race: Race, rnd, scorers=5, displacers=2):
    live = [e for e in race.entries if e.status != "scratched"]
    rnd.shuffle(live)
    base = 960 if race.event == 1 else 1140
    times, t = [], base
    for e in live:
        t += rnd.uniform(2, 14)
        times.append((round(t, 3), e))
    out = [times.pop(4)[1], times.pop(9)[1]]                  # DNF, DNS
    finishers, teams_out, by_event = [], {}, {}
    for place, (t, e) in enumerate(times, 1):
        f = fin(place, m.athlete(e.athlete_id).bib, e.athlete_id, t, None, event=e.event)
        finishers.append(f)
        by_event.setdefault(e.event, []).append((f, e))
    for ev, rows in by_event.items():
        per: dict = {}
        for i, (f, e) in enumerate(rows, 1):
            f["event_place"] = i
            tid = m.athlete(e.athlete_id).team_id
            if e.status != "exhibition" and tid:
                per.setdefault(tid, []).append(f)
        complete = {t for t, fs in per.items() if len(fs) >= scorers}
        pts = 0
        for f, e in rows:
            tid = m.athlete(e.athlete_id).team_id
            if tid in complete and f in per[tid][:scorers + displacers]:
                pts += 1
                idx = per[tid].index(f)
                f.update(points=pts, scorer=idx < scorers, displacer=idx >= scorers)
        ranked, partial = [], []
        for tid, fs in per.items():
            rs = [runner(f["athlete_id"], f["bib"], f["points"], f["scorer"])
                  for f in fs[:scorers + displacers]]
            row = {"rank": None, "team_id": tid, "score": None, "complete": tid in complete,
                   "count": f"{len(fs)}/{scorers}", "runners": rs, "tiebreak_points": None,
                   "total_time": None, "avg_time": None}
            if tid in complete:
                tt = sum(f["elapsed"] for f in fs[:scorers])
                row.update(score=sum(f["points"] for f in fs[:scorers]), total_time=tt,
                           avg_time=tt / scorers,
                           tiebreak_points=fs[scorers]["points"] if len(fs) > scorers else None)
                ranked.append(row)
            else:
                partial.append(row)
        ranked.sort(key=lambda r: (r["score"], r["tiebreak_points"] or 10 ** 6))
        for i, row in enumerate(ranked, 1):
            row["rank"] = i
        teams_out[str(ev)] = ranked + partial
    for e, status in zip(out, ("DNF", "DNS")):
        finishers.append(fin(None, m.athlete(e.athlete_id).bib, e.athlete_id, None, None,
                             status=status, event=e.event))
    race.results = {"frozen_at": "2026-10-10T11:42:05", "finishers": finishers,
                    "teams": teams_out}
    race.status = "final"


class TestFormatting(unittest.TestCase):
    def test_time_rounds_up_to_hundredths(self):
        self.assertEqual(printing.format_time(983.52), "16:23.52")      # float noise ignored
        self.assertEqual(printing.format_time(983.521), "16:23.53")
        self.assertEqual(printing.format_time(983.5204), "16:23.52")    # below 1 ms: noise
        self.assertEqual(printing.format_time(983.5206), "16:23.53")    # 983.521 s
        self.assertEqual(printing.format_time(5037.35), "1:23:57.35")
        self.assertEqual(printing.format_time(59.999), "1:00.00")
        self.assertEqual(printing.format_time(None), "")

    def test_pace_per_mile(self):
        self.assertEqual(printing.format_pace(983.52, 5000), "5:17")    # 316.6 s
        self.assertEqual(printing.format_pace(983.52, 0), "")


def golden(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class TestResultsText(unittest.TestCase):
    def setUp(self):
        self.m = small_meet()
        self.race = self.m.races[0]

    def text(self):
        return printing.results_text(self.m, self.race, PRINTED)

    def test_golden(self):
        """Exact layout: header, places, rounding up, EX, unattached, partial
        team, DNF/DNS at the bottom, team scores with *4 for a 3+1 meet."""
        self.assertEqual(self.text(), golden("results-small.txt"))

    def test_columns_never_shift(self):
        lines = self.text().splitlines()
        start = lines.index(next(l for l in lines if l.startswith(" Pl ")))
        rows = lines[start + 1:start + 13]
        self.assertEqual(len(rows), 12)
        # every column boundary is a space on every row, whatever the name length
        seps, pos = [], 0
        for _, w, _ in printing.RESULT_COLS[:-1]:
            pos += w
            seps.append(pos)
            pos += 1
        for row in rows:
            for s in seps:
                if s < len(row):                                  # lines are rstripped
                    self.assertEqual(row[s], " ", f"col boundary {s} in {row!r}")
            self.assertRegex(row[:3], r"^( *\d+| --)$")
            self.assertRegex(row[4:10], r"^# *\d+$")
        finals = [l[seps[4] + 1:seps[5]].strip() for l in rows]
        self.assertEqual(finals[0], "18:23.52")
        self.assertEqual(finals[-2:], ["DNF", "DNS"])
        long_name = next(l for l in rows if "Fontaine" in l)
        self.assertIn("Fontaine-Whitlock-Aberna 12 ", long_name)          # cut at 24

    def test_long_team_name_uses_short_name(self):
        self.assertIn(" Valley Christian ", self.text())
        self.assertNotIn("Academy Lakes", self.text())

    def test_not_final_is_not_printed_as_results(self):
        """Reopen keeps race.results but sets status back (scoring contract)."""
        self.race.status = "scheduled"
        text = self.text()
        self.assertIn("Results not final (race reopened).", text)
        self.assertNotIn("Birch", text)
        self.assertIn("No final results yet.", printing.meet_results_text(self.m, PRINTED))
        self.race.results = None
        self.assertIn("Results not final.", self.text())

    def test_tied_teams_show_the_tiebreak(self):
        teams = self.race.results["teams"]["1"]
        teams[1]["score"] = 9
        teams[1]["rank"] = 1                               # shared rank, scoring decides
        teams[0]["tiebreak_points"] = None
        text = self.text()
        self.assertIn("Tie-break *6: none", text)
        self.assertIn("Tie-break *6: 7", text)
        self.assertEqual(len(re.findall(r"^   1 ", text, re.M)), 2)

    def test_combined_start_prints_a_section_per_scoring_event(self):
        m = self.m
        m.events.append(Event(2, "Girls 5k Run CC JV", gender="F", distance_m=5000))
        for aid in ("a7", "a9"):                              # Hollis, Jarvis run JV
            next(e for e in self.race.entries if e.athlete_id == aid).event = 2
        for f in self.race.results["finishers"]:
            if f["athlete_id"] in ("a7", "a9"):
                f.update(event=2, points=None, scorer=False, displacer=False)
        self.race.results["finishers"][8]["event_place"] = 1
        self.race.results["finishers"][9]["event_place"] = 2
        text = self.text()
        self.assertIn("Event 1  Girls 5k Run CC Varsity", text)
        self.assertIn("Event 2  Girls 5k Run CC JV", text)
        jv = text[text.index("Event 2 "):]
        self.assertRegex(jv, r"\n  1 #  203 Hollis, Gray")
        self.assertRegex(jv, r"\n  2 #  204 Jarvis, Indy")
        self.assertNotIn("Birch", jv)
        self.assertNotIn("Team Scores", jv)                   # no teams scored in JV

    def test_unknown_bib_still_prints(self):
        self.race.results["finishers"].insert(3, fin(4, "777", None, 1119.0, 4))
        self.assertIn("#  777 Unknown bib 777", self.text())

    def test_meet_results_in_run_order(self):
        m = synthetic_meet()
        text = printing.meet_results_text(m, PRINTED)
        self.assertEqual(text.count("rfid-meet  10/10/2026"), 1)
        order = [text.index(f"Event {n}  ") for n in (1, 2, 3)]
        self.assertEqual(order, sorted(order))
        m.races[0].order, m.races[1].order = 2, 1
        text = printing.meet_results_text(m, PRINTED)
        self.assertLess(text.index("Event 2  "), text.index("Event 1  "))


class TestStartListAndBibs(unittest.TestCase):
    def test_start_list_golden(self):
        m = small_meet()
        self.assertEqual(printing.start_list_text(m, m.races[0], PRINTED),
                         golden("startlist-small.txt"))

    def test_team_bibs_golden_one_team_per_page(self):
        text = printing.team_bibs_text(small_meet(), printed_at=PRINTED)
        self.assertEqual(text, golden("teambibs-small.txt"))
        self.assertEqual(text.count("\f"), 3)                 # 3 teams + unattached
        self.assertLess(text.index("201 Núñez"), text.index("205 Nash"))

    def test_team_bibs_selected_teams(self):
        text = printing.team_bibs_text(small_meet(), ["t3"], printed_at=PRINTED)
        self.assertIn("Hillcrest (HIL)", text)
        self.assertNotIn("Ridge", text.split("\n", 3)[3])
        self.assertNotIn("Unattached", text)

    def test_start_time_in_the_repeated_header(self):
        m = small_meet()
        m.races[0].start_time = "08:00"
        text = printing.start_list_text(m, m.races[0], PRINTED)
        header, _ = printing.split_header(text)
        self.assertEqual(header[2].strip(), "Start: 8:00 AM")     # on every page
        # otherwise the golden sheet, line for line
        self.assertEqual(text.replace(header[2] + "\n", ""), golden("startlist-small.txt"))

    def test_all_start_lists_in_run_order_one_race_per_page(self):
        m = two_race_meet()
        text = printing.start_lists_text(m, printed_at=PRINTED)
        header, chunks = printing.split_header(text)
        self.assertIn("Start Lists", header[1])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "Race 1 of 2: Boys Varsity · Start: 9:15 AM")
        self.assertEqual(chunks[1][0], "Race 2 of 2: Girls 5k Run CC Varsity")    # no time set
        self.assertIn("Event 2  Boys 5k Run CC Varsity", "\n".join(chunks[0]))
        self.assertIn("#  101 Birch, Avery", "\n".join(chunks[1]))
        self.assertTrue(printing.to_pdf(text).startswith(b"%PDF"))

    def test_schedule(self):
        m = two_race_meet()
        rows = printing.schedule_rows(m)
        self.assertEqual([(r["n"], r["rk"], r["start"], r["race"], r["entries"]) for r in rows],
                         [(1, "2-1-1", "9:15 AM", "Boys Varsity", 1),
                          (2, "1-1-1", "", "Girls 5k Run CC Varsity", 12)])
        self.assertEqual(rows[0]["divisions"], ["Boys 5k Run CC Varsity"])
        text = printing.schedule_text(m, PRINTED)
        self.assertIn("/ Schedule", text)
        lines = text.splitlines()
        row = next(l for l in lines if "Boys Varsity" in l)
        self.assertTrue(row.startswith(" 9:15 AM   1 2-1-1   Boys Varsity"), row)
        self.assertTrue(row.endswith(" 1"))
        self.assertIn("2 races, 13 runners", text)
        self.assertNotIn("No start times", text)
        m.races[1].start_time = ""
        m.races[0].start_time = ""
        self.assertIn("No start times set yet", printing.schedule_text(m, PRINTED))


def two_race_meet() -> Meet:
    """small_meet plus a boys race run FIRST with a start time and a custom name."""
    m = small_meet()
    m.events.append(Event(2, "Boys 5k Run CC Varsity", gender="M", distance_m=5000))
    m.athletes.append(Athlete("a20", "Rowan", "Quill", "M", "t1", "150", grad_year=2028))
    m.races[0].order = 2
    m.races.append(Race(2, order=1, name="Boys Varsity", start_time="09:15",
                        entries=[Entry("a20", 2)]))
    return m


class TestResultFilters(unittest.TestCase):
    """places / grades / team_ids on results (2026-10-03): real places kept,
    team scores full / per school / omitted, filters combine."""

    def setUp(self):
        self.m = small_meet()
        self.race = self.m.races[0]

    def text(self, **kw):
        return printing.results_text(self.m, self.race, PRINTED, **kw)

    @staticmethod
    def places_printed(text):
        return [l[:3].strip() for l in text.splitlines() if re.match(r"^( *\d+| --) #", l)]

    def test_describe_filter(self):
        d = printing.describe_filter
        self.assertEqual(d(self.m), "")
        self.assertEqual(d(self.m, places=(1, 25), grades={"10", "9"}, team_ids=["t1", "t3"]),
                         "Places 1-25 · Grades 9, 10 · Ridge High School, Hillcrest")
        self.assertEqual(d(self.m, places=(26, None)), "Places 26+")
        self.assertEqual(d(self.m, places=(3, 3), grades=["SR"]), "Place 3 · Grade 12")
        self.assertEqual(d(self.m, team_ids=["t2", None]), "Valley Christian, Unattached")
        with self.assertRaises(ValueError):
            d(self.m, places=(5, 2))

    def test_places_keep_real_places_and_full_team_scores(self):
        text = self.text(places=(2, 5))
        self.assertEqual(text.splitlines()[2].strip(), "Places 2-5")
        self.assertEqual(text.splitlines()[3], "")                # header ends, repeats per page
        self.assertEqual(self.places_printed(text), ["2", "3", "4", "5"])
        self.assertRegex(text, r"\n  2 #  201 Núñez, Zoë ")
        self.assertNotIn("DNF", text)
        self.assertNotIn("DNS", text)
        self.assertIn("Team Scores", text)
        self.assertIn("   1 Ridge High School              9", text)
        self.assertIn("Hillcrest                    1/3  incomplete", text)
        self.assertEqual(self.places_printed(self.text(places=(9, None))), ["9", "10"])

    def test_grade_filter_omits_team_scores(self):
        text = self.text(grades={"12"})
        self.assertEqual(self.places_printed(text), ["1", "8", "9", "--"])
        self.assertIn("Lark, Kai", text)                          # 'Senior' counts as 12
        self.assertNotIn("Team Scores", text)
        self.assertEqual(text.count(printing.GRADE_FILTER_NO_TEAMS), 1)
        self.assertEqual(self.places_printed(self.text(grades=[11, "fr"])),   # 'fr' = 9
                         ["2", "4", "5", "6", "10"])

    def test_school_filter_shows_only_that_schools_team_rows(self):
        text = self.text(team_ids=["t2"])
        self.assertEqual(self.places_printed(text), ["2", "5", "9", "10"])
        self.assertNotIn("Birch", text)
        self.assertIn("Team Scores", text)
        self.assertIn("   2 Valley Christian              12    2    4    6    7", text)
        self.assertNotIn("Ridge High School  ", text.split("Team Scores")[1])
        self.assertNotIn("Hillcrest", text)
        text = self.text(team_ids=["t3"])                         # partial team only
        self.assertEqual(self.places_printed(text), ["6", "--"])
        self.assertIn("Hillcrest                    1/3  incomplete", text)
        self.assertNotIn("Valley", text)

    def test_school_filter_keeps_the_tiebreak_of_a_tie_with_another_school(self):
        teams = self.race.results["teams"]["1"]
        teams[1]["score"] = 9
        text = self.text(team_ids=["t1"])
        self.assertIn("Tie-break *6: none", text)

    def test_combinations(self):
        text = self.text(places=(1, 5), team_ids=["t1"])
        self.assertEqual(text.splitlines()[2].strip(), "Places 1-5 · Ridge High School")
        self.assertEqual(self.places_printed(text), ["1", "3"])
        self.assertIn("Team Scores", text)
        text = self.text(grades={"9", "10"}, team_ids=["t1", "t3"])
        self.assertEqual(self.places_printed(text), ["3", "7", "--"])   # Dunmore, Ives EX, Keane DNF
        self.assertIn("EX = exhibition", text)
        self.assertNotIn("Team Scores", text)
        text = self.text(places=(1, 5), grades={"9", "10"}, team_ids=["t1"])
        self.assertEqual(self.places_printed(text), ["3"])

    def test_no_match(self):
        text = self.text(places=(50, None))
        self.assertIn("Event 1  Girls 5k Run CC Varsity", text)
        self.assertIn(printing.NO_MATCH, text)
        self.assertNotIn("Team Scores", text)
        m = synthetic_meet()
        text = printing.meet_results_text(m, PRINTED, grades=["7"])
        self.assertIn(printing.NO_MATCH, text)

    def test_filter_results_data(self):
        before = repr(self.race.results)
        res = printing.filter_results(self.m, self.race, places=(1, 3), team_ids=["t1", "t2"])
        self.assertEqual(repr(self.race.results), before)         # a new dict, input untouched
        self.assertEqual([(f["bib"], f["event_place"], f["points"]) for f in res["finishers"]],
                         [("101", 1, 1), ("201", 2, 2), ("102", 3, 3)])
        self.assertTrue(res["show_team_scores"])
        self.assertEqual([t["team_id"] for t in res["teams"]["1"]], ["t1", "t2"])
        self.assertEqual(res["filter"], {"places": [1, 3], "grades": None,
                                         "team_ids": ["t1", "t2"], "hide": None,
                                         "events": None,
                                         "text": "Places 1-3 · Ridge High School, "
                                                 "Valley Christian"})
        self.assertEqual(res["frozen_at"], "2026-10-10T11:42:05")
        res = printing.filter_results(self.m, self.race, grades=["Senior"])
        self.assertFalse(res["show_team_scores"])
        self.assertEqual(res["filter"]["grades"], ["12"])
        full = printing.filter_results(self.m, self.race)
        self.assertEqual(len(full["finishers"]), 12)
        self.assertEqual(full["filter"]["text"], "")
        self.race.status = "scheduled"
        with self.assertRaises(ValueError):
            printing.filter_results(self.m, self.race)

    def test_missing_event_place_filled_with_the_real_place(self):
        for f in self.race.results["finishers"]:
            f["event_place"] = None
        res = printing.filter_results(self.m, self.race, team_ids=["t2"])
        self.assertEqual([f["event_place"] for f in res["finishers"]], [2, 5, 9, 10])

    def test_meet_results_school_filter_skips_empty_sections(self):
        m = synthetic_meet()
        text = printing.meet_results_text(m, PRINTED, team_ids=["t4"])
        self.assertIn("St. Bramble Prep", text.splitlines()[2])
        self.assertIn("Event 1  ", text)
        self.assertNotIn("Event 2  ", text)                       # no St Bramble varsity girls
        self.assertIn("Event 3  ", text)
        self.assertNotIn("Ridgeview  ", text.split("\n", 3)[3])

    def test_long_filter_wraps_in_the_header(self):
        for i in range(4, 30):
            self.m.teams.append(printing.Team(f"t{i}", f"Synthetic School Number {i}"))
        ids = [t.id for t in self.m.teams]
        text = self.text(team_ids=ids)
        head, _ = printing.split_header(text)
        self.assertEqual(len(head), 5)                            # stamp, title, 3 filter lines
        self.assertTrue(head[-1].endswith("..."))
        self.assertTrue(all(len(l) <= printing._width(printing.RESULT_COLS) for l in head))
        self.assertIn(b"/MediaBox [0 0 612 792]", printing.to_pdf(text))


class TestResultPrintOptions(unittest.TestCase):
    """hide (DNF / DNS / DQ) and events (one division of a combined start),
    the results print options added 2026-10-03."""

    def setUp(self):
        self.m = small_meet()
        self.race = self.m.races[0]

    def text(self, **kw):
        return printing.results_text(self.m, self.race, PRINTED, **kw)

    places_printed = staticmethod(TestResultFilters.places_printed)

    def test_describe(self):
        d = printing.describe_filter
        self.assertEqual(d(self.m, hide=["dns", "DNF"]), "No DNF, DNS")
        self.assertEqual(d(self.m, hide="DQ"), "No DQ")
        self.assertEqual(d(self.m, events=[1]), "Event 1 Girls 5k Run CC Varsity")
        self.assertEqual(d(self.m, places=(1, 10), events=["2", 1], hide=("DQ", "DNS", "DNF")),
                         "Events 1, 2 · Places 1-10 · No DNF, DQ, DNS")
        with self.assertRaises(ValueError):
            d(self.m, hide=["DNX"])

    def test_hide_any_subset(self):
        text = self.text(hide=["DNF"])
        self.assertEqual(text.splitlines()[2].strip(), "No DNF")
        self.assertNotIn("Keane", text)
        self.assertIn("Lark, Kai", text)                          # DNS still listed
        self.assertEqual(self.places_printed(text)[-1], "--")
        text = self.text(hide=("DNF", "DNS", "DQ"))
        self.assertEqual(self.places_printed(text), [str(i) for i in range(1, 11)])
        self.assertIn("   1 Ridge High School              9", text)   # team scores unchanged
        res = printing.filter_results(self.m, self.race, hide=["DNS", "DNF"])
        self.assertEqual(res["filter"]["hide"], ["DNF", "DNS"])
        self.assertEqual(len(res["finishers"]), 10)
        self.assertTrue(res["show_team_scores"])

    def test_hide_with_a_dq(self):
        self.race.results["finishers"][10]["status"] = "DQ"      # Keane
        self.assertRegex(self.text(hide=["DNS"]), r"Keane, Jules .* DQ\n")
        text = self.text(hide=["DQ"])
        self.assertNotIn("Keane", text)
        self.assertIn("Lark, Kai", text)

    def test_events_prints_one_division_of_a_combined_start(self):
        m = synthetic_meet()
        race = m.races[1]                                         # varsity (2) + JV (3)
        text = printing.results_text(m, race, PRINTED, events=[3])
        self.assertEqual(text.splitlines()[2].strip(), "Event 3 Girls 5k Run CC JV")
        self.assertIn("Event 3  Girls 5k Run CC JV", text)
        self.assertNotIn("Event 2  ", text)
        self.assertIn("Team Scores", text)
        self.assertIn("St. Bramble Prep", text)                   # JV teams only
        self.assertNotIn("Lakeside", text)                        # varsity-only school
        res =printing.filter_results(m, race, events=[3])
        self.assertEqual(set(res["teams"]), {"3"})
        self.assertEqual({f["event"] for f in res["finishers"]}, {3})
        self.assertEqual(res["filter"]["events"], [3])
        full = printing.filter_results(m, race)
        jv = [f["event_place"] for f in full["finishers"] if f["event"] == 3]
        self.assertEqual([f["event_place"] for f in res["finishers"]], jv)   # real places

    def test_events_across_the_meet(self):
        m = synthetic_meet()
        text = printing.meet_results_text(m, PRINTED, events=[3], hide=["DNF", "DNS"])
        self.assertEqual(text.splitlines()[2].strip(), "Event 3 Girls 5k Run CC JV · No DNF, DNS")
        self.assertEqual(re.findall(r"^Event (\d+)  ", text, re.M), ["3"])
        self.assertNotRegex(text, r"(?m)^ -- ")
        text = printing.results_text(m, m.races[0], PRINTED, events=[3])  # not in that race
        self.assertIn(printing.NO_MATCH, text)


def roster_meet() -> Meet:
    """Two schools for the rosters golden: girls and boys in V / JV / MS
    races, an exhibition, a missing bib, a scratch-only and a roster-only
    athlete (both left out), a long name, and a one-gender school."""
    m = Meet(meet=MeetInfo(id="2026-10-10-rost", name="Roster Test Meet", date="2026-10-10",
                           host="Ridge High School", full_rosters=True))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID"),
               Team("t2", "Valley Christian Academy", short="Valley", code="VCA")]
    m.events = [Event(1, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls"),
                Event(2, "Girls 5k Run CC JV", "F", 5000, "JV Girls"),
                Event(3, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys"),
                Event(4, "Boys 2 Mile Run CC Middle School", "M", 3219, "Middle School Boys")]
    A = Athlete
    m.athletes = [A("a1", "Avery", "Birch", "F", "t1", "101", grad_year=2027),
                  A("a2", "Zoë", "Núñez", "F", "t1", "102", grad_year=2029),
                  A("a3", "Casey", "Dunmore", "F", "t1", "", grad_year=2030),        # no bib
                  A("a4", "Emery", "Fontaine-Whitlock-Abernathy", "M", "t1", "110",
                    grad_year=2028),
                  A("a5", "Harper", "Ives", "M", "t1", "111", grade_text="Senior"),
                  A("a6", "Kai", "Lark", "M", "t1", "112", grad_year=2032),          # 7th grade
                  A("a7", "Lee", "Moss", "F", "t1", "103", grad_year=2028),          # scratched
                  A("a8", "Noel", "Oakes", "M", "t1", "", grad_year=2028),           # roster only
                  A("a9", "Quinn", "Rowe", "M", "t2", "201", grad_year=2027)]
    m.races = [Race(1, order=1, name="Girls 5k Combined",
                    entries=[Entry("a1", 1), Entry("a2", 2), Entry("a3", 1, status="exhibition"),
                             Entry("a7", 1, status="scratched")]),
               Race(3, order=2, entries=[Entry("a4", 3), Entry("a5", 3), Entry("a9", 3)]),
               Race(4, order=3, entries=[Entry("a6", 4)])]
    return m


def big_school_meet(girls: int, boys: int) -> Meet:
    """One school with that many entered girls and boys (synthetic names)."""
    m = Meet(meet=MeetInfo(id="2026-10-10-big", name="Big School Meet", date="2026-10-10"))
    m.teams = [Team("t1", "Synthetic Prep", code="SYN")]
    m.events = [Event(1, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls"),
                Event(2, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys")]
    m.races = [Race(1, order=1), Race(2, order=2)]
    n = 0
    for gender, count, race in (("F", girls, m.races[0]), ("M", boys, m.races[1])):
        for i in range(count):
            n += 1
            m.athletes.append(Athlete(f"a{n}", f"First{i:03d}", f"Last{gender}{i:03d}", gender,
                                      "t1", str(1000 + n), grad_year=2028))
            race.entries.append(Entry(f"a{n}", race.event))
    return m


class TestRosters(unittest.TestCase):
    CAP = printing.ROSTER_ROWS_PER_COLUMN

    def test_geometry_is_computed_from_the_pdf_page(self):
        """90 chars = portrait Letter at 10 pt, 60 lines: 60 - 3 - 7 = 50 (7: + the gender totals line, 2026-10-03)."""
        self.assertEqual(printing.ROSTER_WIDTH, 90)
        self.assertEqual(printing.text_lines_per_page(printing.ROSTER_WIDTH), 60)
        self.assertEqual(self.CAP, 60 - printing.ROSTER_HEADER_LINES
                         - printing.ROSTER_FIXED_LINES)
        self.assertEqual(self.CAP, 50)

    def test_golden(self):
        text = printing.rosters_text(roster_meet(), printed_at=PRINTED)
        self.assertEqual(text, golden("rosters-small.txt"))

    def test_who_is_listed(self):
        text = printing.rosters_text(roster_meet(), printed_at=PRINTED)
        self.assertNotIn("Moss", text)                            # scratched only
        self.assertNotIn("Oakes", text)                           # roster only, never entered
        self.assertRegex(text, r"\n ---- Dunmore, Casey +9 V 5K +EX")
        self.assertIn("Girls 3 · Boys 3 · 1 without a bib", text)
        self.assertIn("Fontaine-Whitlock-Abern 11 V 5K", text)    # cut at 23, columns hold

    def test_race_short_labels(self):
        m = roster_meet()
        self.assertEqual([printing.race_short_label(m, n) for n in (1, 2, 3, 4, 9)],
                         ["V 5K", "JV 5K", "V 5K", "MS 2M", "Ev 9"])
        lab = printing._distance_label
        self.assertEqual([lab(3200), lab(3218.688), lab(2500), lab(4000), lab(1234)],
                         ["2M", "2M", "2.5K", "4K", "1234m"])
        self.assertEqual([lab(0, "Boys 2 Mile Run"), lab(0, "Girls 3200"), lab(0, "Open 6k")],
                         ["2M", "2M", "6K"])
        m.events[1] = Event(2, "Girls 4k Junior Varsity", "F", 0)
        self.assertEqual(printing.race_short_label(m, 2), "JV 4K")

    def test_selected_schools_in_the_order_given(self):
        text = printing.rosters_text(roster_meet(), ["t2", "t1"], printed_at=PRINTED)
        self.assertLess(text.index("Valley Christian Academy (VCA)"),
                        text.index("Ridge High School (RID)"))
        self.assertEqual(text.count("\f"), 1)

    def pages(self, girls, boys, **kw):
        return printing.roster_pages(big_school_meet(girls, boys), **kw)

    def shape(self, pages):
        return [[(c["heading"], len(c["rows"])) for c in p["columns"]] for p in pages]

    def test_boundary_exactly_capacity_is_one_page(self):
        cap = self.CAP
        self.assertEqual(self.shape(self.pages(cap, cap)), [[("Girls", cap), ("Boys", cap)]])
        self.assertEqual(self.shape(self.pages(cap, 0)), [[("Girls", cap), ("", 0)]])

    def test_boundary_capacity_plus_one_splits_by_gender(self):
        cap = self.CAP
        pages = self.pages(cap + 1, 3)
        self.assertEqual(self.shape(pages), [[("Girls", cap), ("Girls (cont'd)", 1)],
                                             [("Boys", 3), ("", 0)]])
        self.assertEqual([(p["page"], p["pages"]) for p in pages], [(1, 2), (2, 2)])
        self.assertEqual(pages[0]["counts"], {"F": cap + 1, "M": 3})
        self.assertEqual(self.shape(self.pages(3, cap + 1)),
                         [[("Girls", 3), ("", 0)], [("Boys", cap), ("Boys (cont'd)", 1)]])

    def test_one_gender_flows_both_columns_then_more_pages(self):
        cap = self.CAP
        self.assertEqual(self.shape(self.pages(0, cap + 1)),
                         [[("Boys", cap), ("Boys (cont'd)", 1)]])
        self.assertEqual(self.shape(self.pages(0, 2 * cap)),
                         [[("Boys", cap), ("Boys (cont'd)", cap)]])
        self.assertEqual(self.shape(self.pages(2 * cap + 1, 2)),
                         [[("Girls", cap), ("Girls (cont'd)", cap)],
                          [("Girls (cont'd)", 1), ("", 0)], [("Boys", 2), ("", 0)]])

    def test_column_order_follows_the_bibs(self):
        """The gender with the school's lowest bib takes the left column, so
        the numbers run left to right whichever order bibs went out in;
        girls left when the bibs can't tell. (2026-10-05)"""
        m = big_school_meet(3, 2)
        for a in m.athletes:                                  # boys numbered first
            a.bib = str(int(a.bib) + (100 if a.gender == "F" else 0))
        (page,) = printing.roster_pages(m)
        self.assertEqual(self.shape([page]), [[("Boys", 2), ("Girls", 3)]])
        self.assertEqual(page["genders"], ("M", "F"))
        text = printing.rosters_text(m, printed_at=PRINTED)
        self.assertIn("Boys 2 · Girls 3", text)
        self.assertIn("BOYS                                           GIRLS", text)
        self.assertIn("Total boys: 2", text.split("Total girls")[0])
        # overflow: the boys' page comes first too
        self.assertEqual(self.shape(printing.roster_pages(m, rows_per_column=2)),
                         [[("Boys", 2), ("", 0)], [("Girls", 2), ("Girls (cont'd)", 1)]])
        # no bibs at all, or only one gender numbered: girls left
        for a in m.athletes:
            a.bib = ""
        self.assertEqual(self.shape(printing.roster_pages(m))[0][0][0], "Girls")
        m.athletes[-1].bib = "5"
        self.assertEqual(self.shape(printing.roster_pages(m))[0][0][0], "Girls")

    def test_column_order_after_assign(self):
        """End to end with the bib dialog's default sort (boys before girls)."""
        from rfid_finishlynx.meet import bibs
        m = big_school_meet(3, 2)
        for a in m.athletes:
            a.bib = ""
        bibs.assign(m, ranges="100-", sort=bibs.DEFAULT_SORT)
        (page,) = printing.roster_pages(m)
        nums = [int(r["bib"]) for c in page["columns"] for r in c["rows"]]
        self.assertEqual(self.shape([page]), [[("Boys", 2), ("Girls", 3)]])
        self.assertEqual(nums, sorted(nums))                  # consecutive left to right

    def test_rows_per_column_is_a_parameter(self):
        self.assertEqual(self.shape(self.pages(3, 3, rows_per_column=3)),
                         [[("Girls", 3), ("Boys", 3)]])
        self.assertEqual(self.shape(self.pages(4, 3, rows_per_column=3)),
                         [[("Girls", 3), ("Girls (cont'd)", 1)], [("Boys", 3), ("", 0)]])

    def test_rows_flow_alphabetically_down_then_across(self):
        cap = self.CAP
        (page,) = self.pages(0, cap + 5)
        lasts = [r["last"] for c in page["columns"] for r in c["rows"]]
        self.assertEqual(lasts, sorted(lasts))
        row = page["columns"][1]["rows"][0]
        self.assertEqual(set(row), {"athlete_id", "bib", "last", "first", "grade", "race",
                                    "exhibition"})

    def assert_pdf_pages_match(self, text):
        """Every text page fits a PDF page exactly: to_pdf's pagination adds
        no page of its own and cuts no page short."""
        per_page = printing.text_lines_per_page(max(len(l) for l in text.split("\n")))
        pages = printing.paginate(text, per_page)
        self.assertEqual(len(pages), text.count("\f") + 1)
        self.assertTrue(all(len(p) <= per_page for p in pages))
        pdf = printing.to_pdf(text, title="Rosters")
        self.assertIn(f"/Count {len(pages)} ".encode(), pdf)
        self.assertIn(b"/MediaBox [0 0 612 792]", pdf)
        return pages

    def test_text_boundary_matches_the_pdf(self):
        cap = self.CAP
        text = printing.rosters_text(big_school_meet(cap, cap), printed_at=PRINTED)
        pages = self.assert_pdf_pages_match(text)
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]), 60)                       # full to the last line
        self.assertEqual(pages[0][-1], printing.ROSTER_LEGEND)
        text = printing.rosters_text(big_school_meet(cap + 1, cap), printed_at=PRINTED)
        self.assertEqual(len(self.assert_pdf_pages_match(text)), 2)
        self.assertIn("Synthetic Prep (SYN)", text.split("\f")[1])
        self.assertIn("page 2 of 2", text.split("\f")[1])
        self.assertIn("GIRLS                                          GIRLS (CONT'D)", text)

    def test_long_meet_name_cannot_change_the_page(self):
        m = big_school_meet(self.CAP, 1)
        m.meet.name = "Synthetic " * 12
        m.meet.host = "A Very Long Host School Name"
        text = printing.rosters_text(m, printed_at=PRINTED)
        self.assertTrue(all(len(l) <= printing.ROSTER_WIDTH for l in text.split("\n")))
        self.assertEqual(len(self.assert_pdf_pages_match(text)), 1)

    def test_synthetic_meet_columns_hold(self):
        text = printing.rosters_text(synthetic_meet(), printed_at=PRINTED)
        self.assertEqual(text.count("\f"), 6)                     # 6 schools + unattached
        for line in text.split("\n"):
            self.assertLessEqual(len(line), printing.ROSTER_WIDTH)
            m = re.match(r"^( *\d+| ----) .{23} .. .{7}", line)
            if m and len(line) > printing.ROSTER_COL_W + printing.ROSTER_GUTTER:
                right = line[printing.ROSTER_COL_W + printing.ROSTER_GUTTER:]
                self.assertRegex(right, r"^( *\d+| ----) ")
        self.assert_pdf_pages_match(text)


class TestPdf(unittest.TestCase):
    def parse(self, pdf: bytes) -> dict:
        """Tiny reader: follow startxref, check every xref offset lands on
        its 'n 0 obj', return {n: body}."""
        self.assertTrue(pdf.startswith(b"%PDF-1.4\n"))
        self.assertTrue(pdf.endswith(b"%%EOF\n"))
        xref_at = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf).group(1))
        self.assertEqual(pdf[xref_at:xref_at + 5], b"xref\n")
        head = re.match(rb"xref\n0 (\d+)\n", pdf[xref_at:])
        count = int(head.group(1))
        table = pdf[xref_at + head.end():]
        objs = {}
        for n in range(count):
            entry = table[n * 20:(n + 1) * 20]
            self.assertEqual(len(entry), 20)
            if n == 0:
                self.assertEqual(entry, b"0000000000 65535 f \n")
                continue
            off = int(entry[:10])
            self.assertEqual(entry[10:], b" 00000 n \n")
            prefix = f"{n} 0 obj\n".encode()
            self.assertEqual(pdf[off:off + len(prefix)], prefix, f"object {n}")
            end = pdf.index(b"\nendobj\n", off)
            objs[n] = pdf[off + len(prefix):end]
        self.assertIn(f"/Size {count}".encode(), pdf[xref_at:])
        return objs

    def streams(self, objs: dict):
        out = []
        for body in objs.values():
            m = re.match(rb"<< /Length (\d+) >>\nstream\n", body)
            if m:
                data = body[m.end():]
                self.assertTrue(data.endswith(b"endstream"))
                self.assertEqual(len(data) - len(b"endstream"), int(m.group(1)))
                out.append(data[:int(m.group(1))])
        return out

    def test_structure_and_xref(self):
        m = small_meet()
        pdf = printing.to_pdf(printing.results_text(m, m.races[0], PRINTED), title="Results")
        objs = self.parse(pdf)
        self.assertIn(b"/BaseFont /Courier", objs[3])
        self.assertIn(b"/Encoding /WinAnsiEncoding", objs[3])
        self.assertIn(b"/Count 1", objs[2])
        self.assertIn(b"/MediaBox [0 0 612 792]", pdf)            # Letter portrait
        (content,) = self.streams(objs)
        self.assertIn(b"(  1 #  101 Birch, Avery", content)
        self.assertIn(b"(Page 1 of 1) Tj", content)

    def test_accented_names_in_winansi(self):
        m = small_meet()
        m.athlete("a1").first = "Łukasz"                          # not in WinAnsi: Ł -> L
        pdf = printing.to_pdf(printing.results_text(m, m.races[0], PRINTED), title="Zoë")
        (content,) = self.streams(self.parse(pdf))
        self.assertIn(rb"N\372\361ez, Zo\353", content)           # Núñez, Zoë in cp1252
        self.assertIn(b"Birch, Lukasz", content)
        self.assertTrue(all(b < 128 for b in content))
        self.assertIn(b"/Title <FEFF005A006F00EB>", pdf)          # UTF-16 title
        self.assertEqual(printing._pdf_string("a(b)\\c"), r"(a\(b\)\\c)")

    def test_pages_split_with_header_repeated(self):
        m = synthetic_meet()
        text = printing.meet_results_text(m, PRINTED)
        pdf = printing.to_pdf(text, title="Results")
        objs = self.parse(pdf)
        pages = self.streams(objs)
        self.assertGreaterEqual(len(pages), 2)
        self.assertIn(f"/Count {len(pages)}".encode(), objs[2])
        for i, page in enumerate(pages, 1):
            self.assertIn(b"(                             rfid-meet  10/10/2026 12:05 PM) Tj",
                          page)
            self.assertIn(f"(Page {i} of {len(pages)}) Tj".encode(), page)
        # every body line lands on exactly one page, in order
        body = [l for l in printing.split_header(text)[1][0] if l]
        printed = [l for p in printing.paginate(text, 60) for l in p[3:] if l]
        self.assertEqual(printed, body)

    def test_paginate_keeps_block_starts_off_the_page_bottom(self):
        text = "Head 1\nHead 2\n\n" + "\n".join(f"row {i}" for i in range(15)) + \
               "\n\nEvent 2\n" + "\n".join(f"next {i}" for i in range(10)) + "\n"
        pages = printing.paginate(text, 20)                 # 17 body lines per page
        self.assertEqual([p[:3] for p in pages], [["Head 1", "Head 2", ""]] * 2)
        self.assertEqual(pages[0][-1], "row 14")             # Event 2 not stranded
        self.assertEqual(pages[1][3], "Event 2")
        self.assertTrue(all(len(p) <= 20 for p in pages))

    def test_form_feed_starts_a_new_page(self):
        pages = printing.paginate("H\n\na\nb\n\fc\n", 60)
        self.assertEqual(pages, [["H", "", "a", "b"], ["H", "", "c"]])

    def test_long_lines_go_landscape(self):
        pdf = printing.to_pdf("x" * 130 + "\n")
        self.assertIn(b"/MediaBox [0 0 792 612]", pdf)
        self.parse(pdf)
        self.assertIn(b"/MediaBox [0 0 612 792]", printing.to_pdf("x" * 100 + "\n"))


class TestHtml(unittest.TestCase):
    def test_escaped_pre_page(self):
        m = small_meet()
        m.teams[2].name = "Hill <A&M>"
        text = printing.results_text(m, m.races[0], PRINTED)
        page = printing.to_html(text, title="Results <Girls> & more")
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("<title>Results &lt;Girls&gt; &amp; more</title>", page)
        self.assertIn("Hill &lt;A&amp;M&gt;", page)
        self.assertNotIn("<A&M>", page)
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("Núñez, Zoë", page)
        self.assertEqual(page.count('<pre class="page">'), 1)

    def test_form_feeds_become_printed_pages_with_the_header(self):
        page = printing.to_html(printing.team_bibs_text(small_meet(), printed_at=PRINTED))
        self.assertEqual(page.count('<pre class="page">'), 4)
        self.assertEqual(page.count("Team Bib Sheet"), 4)
        self.assertIn("page-break-before: always", page)
        self.assertNotIn("\f", page)


if __name__ == "__main__":
    unittest.main()
