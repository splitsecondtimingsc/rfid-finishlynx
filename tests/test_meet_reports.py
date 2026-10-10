"""Meet reports (2026-10-03): team scores (+ breakdown), awards and award
labels, finish line sheets, team / competitor labels, split scores, and a
mixed race scored by gender through all of them. Synthetic names only.
The meet here is frozen by scoring.freeze itself (not hand-written results),
so the reports are tested against the real frozen contract, splits included.
python -m unittest tests.test_meet_reports"""

import json
import os
import unittest
from datetime import datetime

from rfid_finishlynx.meet import edits, pdf_reports, pdfdoc, printing, reports, scoring
from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, Race, Scoring,
                                        Team)

from tests.test_meet_edits import mixed_meet, rm_for
from tests.test_meet_pdf import all_text, big_meet, page_streams, texts
from tests.test_meet_printing import big_school_meet, synthetic_meet

PRINTED = datetime(2026, 10, 10, 12, 5)
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "reports")

# Girls race finish order (bib, elapsed). Points by hand, 3 scorers + 1
# displacer: 101 t1 1, 201 t2 2, 401 t4 3, 900 unattached -, 102 t1 4,
# 402 t4 5, 301 t3 (incomplete) -, 103 t1 6, 105 t1 exhibition -, 202 t2 7,
# 104 t1 8 (displacer), 203 t2 9, 403 t4 10, 204 t2 11 (displacer), 302 -.
# t1 1-4-6(8) = 11; t2 2-7-9(11) = 18 and t4 3-5-10 = 18: the tie goes to
# t2 (it has a 4th runner, t4 doesn't). 303 never shows: DNS.
GIRLS = [("101", 1103.511), ("201", 1110.0), ("401", 1112.25), ("900", 1115.4),
         ("102", 1120.0), ("402", 1124.9), ("301", 1130.0), ("103", 1133.3),
         ("105", 1135.0), ("202", 1140.12), ("104", 1150.0), ("203", 1166.66),
         ("403", 1170.0), ("204", 1175.5), ("302", 1190.0)]
# Boys race: finish order, Mile 1 times (114 has no Mile 1 read) and the
# 2 Mile split in finish order.
BOYS = ["111", "211", "112", "212", "113", "311", "213", "114", "214"]
MILE1 = {"211": 330.0, "212": 331.0, "111": 332.0, "213": 333.0, "112": 334.0,
         "311": 335.0, "113": 336.0, "214": 337.0}


def report_meet(marks=None) -> Meet:
    m = Meet(meet=MeetInfo(id="2026-10-10-rep", name="Report Test Meet", date="2026-10-10",
                           host="Ridge High School", scoring=Scoring(scorers=3, displacers=1)))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID"),
               Team("t2", "Valley Christian Academy Lakes", short="Valley Christian",
                    code="VCA"),
               Team("t3", "Hillcrest", short="Hill", code="HIL"),
               Team("t4", "Brookfield", short="Brook", code="BRK")]
    m.events = [Event(1, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls", "5000"),
                Event(2, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys", "5000")]
    A = Athlete
    girls = [("101", "Avery", "Birch", "t1", 2027), ("102", "Casey", "Dunmore", "t1", 2029),
             ("103", "Emery", "Fenwick", "t1", 2027), ("104", "Harper", "Ives", "t1", 2029),
             ("105", "Kai", "Lark", "t1", 2028), ("201", "Zoë", "Núñez", "t2", 2028),
             ("202", "Dana", "Ellery", "t2", 2030), ("203", "Gray", "Hollis", "t2", 2027),
             ("204", "Indy", "Jarvis", "t2", 2028), ("205", "Morgan", "Nash", "t2", 2030),
             ("301", "Finley", "Garth", "t3", 2028), ("302", "Jules", "Keane", "t3", 2029),
             ("303", "Noel", "Oakes", "t3", 2027), ("401", "Blake", "Calder", "t4", 2027),
             ("402", "Lee", "Moss", "t4", 2028), ("403", "Parker", "Pryor", "t4", 2029),
             ("900", "Unity", "Solo", None, 2028)]
    boys = [("111", "Reese", "Quill", "t1", 2027), ("112", "Oakley", "Rowe", "t1", 2028),
            ("113", "Blake", "Sable", "t1", 2029), ("114", "Noel", "Thorne", "t1", 2030),
            ("211", "Gray", "Abbot", "t2", 2027), ("212", "José", "Bellamy", "t2", 2028),
            ("213", "Søren", "Castellanos-Whitfield", "t2", 2029),
            ("214", "Lane", "Dorsey", "t2", 2030), ("311", "Quinn", "Ewing", "t3", 2028)]
    girls_race, boys_race = Race(1, order=1), Race(2, order=2)
    n = 0
    for rows, gender, race in ((girls, "F", girls_race), (boys, "M", boys_race)):
        for bib, first, last, tid, grad in rows:
            n += 1
            m.athletes.append(A(f"a{n}", first, last, gender, tid, bib, grad_year=grad))
            race.entries.append(Entry(f"a{n}", race.event))
    by_bib = {a.bib: a.id for a in m.athletes}
    next(e for e in girls_race.entries if e.athlete_id == by_bib["105"]).status = "exhibition"
    next(e for e in girls_race.entries if e.athlete_id == by_bib["205"]).status = "scratched"
    m.races = [girls_race, boys_race]
    girls_race.marks = dict(marks or {})
    scoring.freeze(m, girls_race, {"finishers": [{"bib": b, "finish": {"elapsed": t}}
                                                 for b, t in GIRLS],
                                   "on_course": [], "no_show": [], "expected_points": []})
    rows = []
    for i, bib in enumerate(BOYS, 1):
        splits = []
        if bib in MILE1:
            splits.append({"point": "m1", "label": "Mile 1", "elapsed": MILE1[bib]})
        splits.append({"point": "m2", "label": "2 Mile", "elapsed": 700.0 + i,
                       "estimated": bib == "214"})
        rows.append({"bib": bib, "finish": {"elapsed": 1000.0 + 10 * i}, "splits": splits})
    scoring.freeze(m, boys_race, {"finishers": rows, "on_course": [], "no_show": [],
                                  "expected_points": ["m1", "m2"]})
    return m


def golden(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def by_team(rows):
    return {t["team_id"]: t for t in rows}


class TestTeamScores(unittest.TestCase):
    def setUp(self):
        self.m = report_meet()

    def test_data(self):
        data = reports.team_scores(self.m)
        json.dumps(data)
        self.assertEqual([(s["race"], s["event"]) for s in data["sections"]],
                         [([1, 1, 1], 1), ([2, 1, 1], 2)])
        girls = data["sections"][0]
        self.assertEqual([(t["rank"], t["team_id"], t["score"], t["order"])
                          for t in girls["teams"]],
                         [(1, "t1", 11, "1-4-6(8)"), (2, "t2", 18, "2-7-9(11)"),
                          (3, "t4", 18, "3-5-10")])
        t2, t4 = girls["teams"][1], girls["teams"][2]
        self.assertTrue(t2["tied"] and t4["tied"])
        self.assertEqual((t2["tiebreak_text"], t4["tiebreak_text"]),
                         ("Tie-break *4: 11", "Tie-break *4: none"))
        self.assertFalse(girls["teams"][0]["tied"])
        # spread from the printed times: 103 18:53.30 - 101 18:23.52 (1103.511 rounds up)
        t1 = girls["teams"][0]
        self.assertEqual(t1["spread_text"], "0:29.78")
        self.assertEqual(t1["total_text"], printing.format_time(1103.511 + 1120.0 + 1133.3))
        self.assertEqual([(r["place"], r["bib"], r["points_text"]) for r in t1["runners"]],
                         [(1, "101", "1"), (5, "102", "4"), (8, "103", "6"), (11, "104", "(8)")])
        (hill,) = girls["incomplete"]
        self.assertEqual((hill["team_id"], hill["count"], hill["rank"]), ("t3", "2/3", None))
        self.assertEqual(data["not_final"], [])

    def test_golden_with_breakdown(self):
        text = reports.team_scores_text(self.m, PRINTED, breakdown=True)
        self.assertEqual(text, golden("team-scores-small.txt"))

    def test_without_breakdown_one_line_per_team(self):
        text = reports.team_scores_text(self.m, PRINTED)
        self.assertNotIn("Birch", text)
        self.assertIn("   1 Ridge High School           11 1-4-6(8)", text)
        self.assertIn("     Tie-break *4: none", text)
        self.assertLessEqual(max(len(l) for l in text.splitlines()), 95)

    def test_shared_rank_stays_shared(self):
        m = report_meet(marks={"204": "DNF"})          # t2 loses its 4th runner
        girls = reports.team_scores(m)["sections"][0]
        self.assertEqual([(t["rank"], t["team_id"], t["score"]) for t in girls["teams"]],
                         [(1, "t1", 11), (2, "t2", 18), (2, "t4", 18)])

    def test_chosen_races_and_not_final(self):
        data = reports.team_scores(self.m, [[2, 1, 1]])
        self.assertEqual([s["event"] for s in data["sections"]], [2])
        scoring.reopen(self.m, self.m.races[1])
        data = reports.team_scores(self.m)
        self.assertEqual(data["not_final"], [{"race": [2, 1, 1],
                                              "name": "Boys 5k Run CC Varsity"}])
        self.assertIn("Not final, not included: Event 2 Boys 5k Run CC Varsity",
                      reports.team_scores_text(self.m, PRINTED))
        with self.assertRaises(ValueError):
            reports.team_scores(self.m, [(9, 1, 1)])


class TestAwards(unittest.TestCase):
    def setUp(self):
        self.m = report_meet()

    def test_individuals_skip_exhibition_keep_unattached(self):
        data = reports.award_winners(self.m, awards={1: 9}, default_n=2)
        json.dumps(data)
        girls, boys = data["sections"]
        self.assertEqual([(w["award"], w["place"], w["bib"]) for w in girls["individuals"]],
                         [(1, 1, "101"), (2, 2, "201"), (3, 3, "401"), (4, 4, "900"),
                          (5, 5, "102"), (6, 6, "402"), (7, 7, "301"), (8, 8, "103"),
                          (9, 10, "202")])                    # 105 (EX) at 9 skipped
        self.assertEqual(girls["individuals"][3]["school"], "Unattached")
        self.assertEqual(girls["individuals"][8]["award_text"], "9th Place")
        self.assertEqual(len(boys["individuals"]), 2)
        with_ex = reports.award_winners(self.m, awards={"1": 9}, include_exhibition=True)
        self.assertEqual(with_ex["sections"][0]["individuals"][8]["bib"], "105")
        short = reports.award_winners(self.m, default_n=20)["sections"][1]
        self.assertEqual((len(short["individuals"]), short["short"]), (9, 11))

    def test_team_awards_and_ties(self):
        data = reports.award_winners(self.m, team_awards={1: 2})
        girls = data["sections"][0]
        self.assertEqual([(t["rank"], t["team_id"], t["award_text"]) for t in girls["teams"]],
                         [(1, "t1", "1st Place"), (2, "t2", "2nd Place")])   # 6th broke it
        m = report_meet(marks={"204": "DNF"})             # t2 and t4 share 2nd
        girls = reports.award_winners(m, team_awards={1: 2})["sections"][0]
        self.assertEqual([(t["rank"], t["team_id"], t["award_text"]) for t in girls["teams"]],
                         [(1, "t1", "1st Place"), (2, "t2", "2nd Place (tie)"),
                          (2, "t4", "2nd Place (tie)")])
        girls = reports.award_winners(m, default_teams=1)["sections"][0]
        self.assertEqual([t["team_id"] for t in girls["teams"]], ["t1"])
        text = reports.awards_text(m, PRINTED, default_n=3, team_awards={1: 2})
        self.assertIn("2nd (tie)  Valley Christian Academy Lakes    18", text)
        self.assertIn("2nd (tie)  Brookfield", text)

    def test_awards_text(self):
        text = reports.awards_text(self.m, PRINTED, default_n=3, default_teams=1)
        self.assertIn("Individual awards: top 3", text)
        self.assertIn("1st      1 Birch, Avery             Ridge High School           18:23.52",
                      text)
        self.assertIn("2nd      2 Núñez, Zoë               Valley Christian            18:30.00",
                      text)
        self.assertIn("1st        Ridge High School                 11", text)

    def test_award_labels(self):
        data = reports.award_winners(self.m, default_n=3, default_teams=1)
        labels = reports.award_labels(data)
        self.assertEqual(len(labels), 3 + 1 + 3 + 1)
        self.assertEqual([l["text"] for l in labels[2]["lines"]],
                         ["Girls 5k Run CC Varsity", "3rd Place", "Blake Calder", "Brookfield",
                          "18:32.25"])
        self.assertEqual([l["text"] for l in labels[3]["lines"]],
                         ["Girls 5k Run CC Varsity", "1st Place Team", "Ridge High School",
                          "Score 11"])
        self.assertEqual(len(reports.award_labels(data, teams=False)), 6)
        self.assertTrue(all(l["style"] in reports.LABEL_STYLES
                            for lab in labels for l in lab["lines"]))


class TestAnnouncer(unittest.TestCase):
    """Announcer version (2026-10-03): top N finishers and N teams or all,
    both read from the bottom up, teams last."""

    def setUp(self):
        self.m = report_meet()

    def test_reverse_order_and_team_count(self):
        data = reports.announcer(self.m, top=3, teams=2)
        json.dumps(data)
        girls = data["sections"][0]
        self.assertEqual([w["award"] for w in girls["individuals"]], [3, 2, 1])
        self.assertEqual([w["bib"] for w in girls["individuals"]], ["401", "201", "101"])
        self.assertEqual([t["rank"] for t in girls["teams"]], [2, 1])
        self.assertEqual(girls["teams"][-1]["team_id"], "t1")      # champion read last

    def test_all_teams_means_every_scored_team(self):
        girls = reports.announcer(self.m, top=1, teams="all")["sections"][0]
        self.assertEqual([t["rank"] for t in girls["teams"]], [3, 2, 1])   # t3 incomplete
        self.assertEqual(girls["scored_teams"], 3)
        girls = reports.announcer(self.m, top=0, teams=0)["sections"]
        self.assertEqual(girls, [])
        with self.assertRaises(ValueError):
            reports.announcer(self.m, teams="some")

    def test_text_and_pdf(self):
        text = reports.announcer_text(self.m, PRINTED, top=3, teams="All")
        self.assertIn("all teams, read from the bottom up", text)
        i3, i1 = text.index("3rd Place: Blake Calder"), text.index("1st Place: Avery Birch")
        self.assertLess(i3, i1)
        self.assertLess(text.index("INDIVIDUALS"), text.index("TEAMS"))
        self.assertIn("1st Place: Ridge High School, 11 points", text)
        pdf = pdf_reports.announcer_pdf(self.m, PRINTED, top=10, teams="all")
        self.assertTrue(pdf.startswith(b"%PDF"))
        flat = [t for page in all_text(self, pdf) for t in page]
        self.assertIn("Announcer", flat)
        self.assertLess(flat.index("10th Place:"), flat.index("1st Place:"))


class TestLabels(unittest.TestCase):
    def test_avery_5160_geometry(self):
        g = reports.AVERY_5160
        self.assertEqual((g["page_w"], g["page_h"], g["label_w"], g["label_h"]),
                         (612.0, 792.0, 189.0, 72.0))
        self.assertEqual((g["top"], g["left"], g["col_pitch"], g["row_pitch"]),
                         (36.0, 13.5, 198.0, 72.0))
        # the sheet is symmetric: right margin = left, bottom = top
        self.assertEqual(g["left"] + 2 * g["col_pitch"] + g["label_w"], g["page_w"] - g["left"])
        self.assertEqual(g["top"] + 10 * g["row_pitch"], g["page_h"] - g["top"])

    def test_31_labels_make_2_sheets(self):
        m = big_school_meet(16, 15)
        sheets = reports.label_sheets(reports.competitor_labels(m))
        json.dumps(sheets)
        self.assertEqual([(s["sheet"], s["sheets"], len(s["labels"])) for s in sheets],
                         [(1, 2, 30), (2, 2, 1)])
        first, last = sheets[0]["labels"][0], sheets[0]["labels"][29]
        self.assertEqual((first["slot"], first["row"], first["col"], first["x"], first["y"]),
                         (1, 1, 1, 13.5, 684.0))
        self.assertEqual((last["slot"], last["row"], last["col"], last["x"], last["y"]),
                         (30, 10, 3, 409.5, 36.0))
        self.assertEqual(sheets[0]["labels"][1]["col"], 2)          # across, then down
        self.assertEqual(sheets[1]["labels"][0]["slot"], 1)
        text = reports.labels_text(m, sheets, "Competitor Labels", PRINTED)
        self.assertEqual(text.count("\f"), 1)
        self.assertIn("Avery 5160 · 31 labels · 2 sheets", text)
        skipped = reports.label_sheets(reports.competitor_labels(m), skip=29)
        self.assertEqual([len(s["labels"]) for s in skipped], [1, 30])
        self.assertEqual(skipped[0]["labels"][0]["slot"], 30)
        self.assertEqual(reports.label_sheets([]), [])

    def test_competitor_labels_entered_only_sorted(self):
        m = report_meet()
        m.meet.full_rosters = True
        m.athletes.append(Athlete("a99", "Roster", "Only", "F", "t1", ""))     # never entered
        labels = reports.competitor_labels(m)
        ids = [l["athlete_id"] for l in labels]
        self.assertNotIn("a99", ids)
        self.assertNotIn(m.athlete_by_bib("205").id, ids)                   # scratched only
        teams = [l["team_id"] for l in labels]
        self.assertEqual(teams, sorted(teams, key=lambda t: ["t4", "t3", "t1", "t2", None]
                                       .index(t)))              # Brookfield .. Valley, unattached
        first = labels[0]
        self.assertEqual([(l["text"], l["style"]) for l in first["lines"]],
                         [("CALDER, Blake", "bold"), ("Brookfield", "normal"),
                          ("401", "big"), ("V 5K", "normal")])
        ridge = [l["lines"][0]["text"] for l in labels if l["team_id"] == "t1"]
        self.assertEqual(ridge, sorted(ridge))
        self.assertEqual(ridge[:2], ["BIRCH, Avery", "DUNMORE, Casey"])
        only = reports.competitor_labels(m, ["t3", None])
        self.assertEqual([l["bib"] for l in only], ["311", "301", "302", "303", "900"])
        self.assertEqual(only[-1]["lines"][1]["text"], "Unattached")

    def test_team_labels(self):
        m = report_meet()
        labels = reports.team_labels(m)
        json.dumps(labels)
        self.assertEqual([l["team_id"] for l in labels], ["t4", "t3", "t1", "t2"])
        ridge = labels[2]
        self.assertEqual(ridge["counts"], {"F": 5, "M": 4})
        self.assertEqual(ridge["bibs"], "101-105, 111-114")
        self.assertEqual([l["text"] for l in ridge["lines"]],
                         ["Ridge High School", "RID · Girls 5 · Boys 4", "Bibs 101-105, 111-114",
                          "Girls V 5K · Boys V 5K"])
        valley = labels[3]
        self.assertEqual(valley["counts"], {"F": 4, "M": 4})                 # Nash scratched
        self.assertEqual(len(reports.team_labels(m, ["t3"], copies=3)), 3)


class TestFinishLineSheets(unittest.TestCase):
    def test_rows_rule(self):
        self.assertEqual(reports.finish_line_rows(0), 40)
        self.assertEqual(reports.finish_line_rows(36), 40)         # 39.6 -> 40
        self.assertEqual(reports.finish_line_rows(37), 80)         # 40.7 -> 41 -> 80
        self.assertEqual(reports.finish_line_rows(200), 240)       # 220 -> 240
        self.assertEqual(reports.finish_line_rows(16, 10), 20)     # 17.6 -> 18 -> 20

    def test_data(self):
        m = report_meet()
        sheets = reports.finish_line_sheets(m)
        json.dumps(sheets)
        self.assertEqual([(s["race"], s["entered"], s["rows"]) for s in sheets],
                         [([1, 1, 1], 16, 40), ([2, 1, 1], 9, 40)])
        (s,) = reports.finish_line_sheets(m, [1], rows=45)
        self.assertEqual([(p["first"], p["last"]) for p in s["pages"]], [(1, 40), (41, 45)])

    def test_golden_continued_page(self):
        m = report_meet()
        text = reports.finish_line_text(m, PRINTED, races=[m.races[0]], rows_per_page=10)
        self.assertEqual(text, golden("finish-line-small.txt"))
        self.assertEqual(text.count("\f"), 1)
        pages = printing.paginate(text, 60)
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(p[:3] == pages[0][:3] for p in pages))   # header repeats

    def test_default_page_fits_one_pdf_page(self):
        m = report_meet()
        text = reports.finish_line_text(m, PRINTED, races=[m.races[0]])
        per_page = printing.text_lines_per_page(max(len(l) for l in text.split("\n")))
        self.assertEqual(len(printing.paginate(text, per_page)), 1)


class TestSplitScores(unittest.TestCase):
    def setUp(self):
        self.m = report_meet()
        self.boys = self.m.races[1]

    def test_hand_computed(self):
        data = reports.split_scores(self.m, self.boys)
        json.dumps(data)
        self.assertTrue(data["official"])
        self.assertEqual(data["split_labels"], ["Mile 1", "2 Mile"])
        (sec,) = data["sections"]
        mile1, mile2 = sec["splits"]
        # Mile 1: 211 t2 1, 212 t2 2, 111 t1 3, 213 t2 4, 112 t1 5, 311 -, 113 t1 6,
        # 214 t2 7 (displacer); 114 has no Mile 1 time.
        self.assertEqual(mile1["missing"], ["114"])
        self.assertEqual(mile1["timed"], 8)
        self.assertEqual([(t["rank"], t["team_id"], t["score"], t["order"])
                          for t in mile1["teams"]],
                         [(1, "t2", 7, "1-2-4(7)"), (2, "t1", 14, "3-5-6")])
        t2 = mile1["teams"][0]
        self.assertEqual((t2["total_text"], t2["avg_text"], t2["spread_text"]),
                         ("16:34.00", "5:31.34", "0:03.00"))
        self.assertEqual([(r["place"], r["bib"], r["time"]) for r in t2["runners"]],
                         [(1, "211", "5:30.00"), (2, "212", "5:31.00"), (4, "213", "5:33.00"),
                          (8, "214", "5:37.00")])
        self.assertEqual([(t["team_id"], t["count"]) for t in mile1["incomplete"]],
                         [("t3", "1/3")])
        # 2 Mile = finish order: t1 1-3-5(7) = 9, t2 2-4-6(8) = 12; one estimate.
        self.assertEqual([(t["team_id"], t["score"], t["order"]) for t in mile2["teams"]],
                         [("t1", 9, "1-3-5(7)"), ("t2", 12, "2-4-6(8)")])
        self.assertEqual((mile2["missing"], mile2["estimated"]), ([], 1))
        # the same rules as the finish: 2 Mile order == finish order here
        fin = by_team(self.boys.results["teams"]["2"])
        self.assertEqual({t["team_id"]: t["score"] for t in mile2["teams"]},
                         {tid: fin[tid]["score"] for tid in ("t1", "t2")})

    def test_dnf_counts_at_the_splits_it_reached(self):
        m = report_meet()
        boys = m.races[1]
        for f in boys.results["finishers"]:
            if f["bib"] == "212":
                f["status"], f["elapsed"] = "DNF", None
        mile1 = reports.split_scores(m, boys)["sections"][0]["splits"][0]
        self.assertEqual(mile1["teams"][0]["order"], "1-2-4(7)")       # unchanged at Mile 1
        f = next(f for f in boys.results["finishers"] if f["bib"] == "213")
        f["status"] = "DQ"
        mile1 = reports.split_scores(m, boys)["sections"][0]["splits"][0]
        self.assertEqual(mile1["teams"][0]["order"], "1-2-6")          # 213 out: 112 is 4, 113 5, no 4th
        self.assertEqual(mile1["teams"][1]["order"], "3-4-5")

    def test_text(self):
        text = reports.split_scores_text(self.m, self.boys, PRINTED)
        self.assertIn("Split: Mile 1  (8 runners timed)", text)
        self.assertIn("Split: 2 Mile  (9 runners timed, 1 estimated)", text)
        self.assertIn("   1 Valley Christian             7 1-2-4(7)", text)
        self.assertIn("No Mile 1 time, left out: bib 114", text)
        self.assertLess(text.index("Mile 1"), text.index("2 Mile"))
        girls = reports.split_scores_text(self.m, self.m.races[0], PRINTED)
        self.assertIn("No splits in this race.", girls)

    def test_live_results_and_not_final(self):
        live = json.loads(json.dumps(self.boys.results))
        live["frozen_at"] = None
        scoring.reopen(self.m, self.boys)
        with self.assertRaises(ValueError):
            reports.split_scores(self.m, self.boys)
        data = reports.split_scores(self.m, self.boys, results=live)
        self.assertFalse(data["official"])
        self.assertIn("UNOFFICIAL", reports.split_scores_text(self.m, self.boys, PRINTED,
                                                              results=live))


class TestMixedRaceScoredByGender(unittest.TestCase):
    """edits.split_genders: one gun, girls and boys scored apart; every
    report gives each gender its own section (2026-10-03)."""

    def setUp(self):
        self.m = mixed_meet()
        self.race = self.m.races[0]
        self.rep = edits.split_genders(self.m, self.race)
        rm = rm_for(self.m, self.race)
        for i, row in enumerate(rm["finishers"]):
            row["splits"] = [{"point": "m1", "label": "Mile 1", "elapsed": 400.0 + i}]
        rm["expected_points"] = ["m1"]
        scoring.freeze(self.m, self.race, rm)
        self.events = [self.rep.events["F"], self.rep.events["M"]]

    def test_every_report_has_a_section_per_gender(self):
        names = ["Girls 2 Mile Run CC Middle School", "Boys 2 Mile Run CC Middle School"]
        ts = reports.team_scores(self.m)
        self.assertEqual([s["event"] for s in ts["sections"]], self.events)
        self.assertEqual([s["event_name"] for s in ts["sections"]], names)
        for s, g in zip(ts["sections"], ("F", "M")):
            self.assertEqual({t["team_id"] for t in s["teams"]}, {"t1", "t2"})
            self.assertTrue(all(self.m.athlete(r["athlete_id"]).gender == g
                                for t in s["teams"] for r in t["runners"]))
        aw = reports.award_winners(self.m, default_n=3)
        self.assertEqual([s["event"] for s in aw["sections"]], self.events)
        for s, g in zip(aw["sections"], ("F", "M")):
            self.assertEqual([w["award"] for w in s["individuals"]], [1, 2, 3])
            self.assertTrue(all(self.m.athlete(w["athlete_id"]).gender == g
                                for w in s["individuals"]))
        sp = reports.split_scores(self.m, self.race)
        self.assertEqual([s["event"] for s in sp["sections"]], self.events)
        text = reports.team_scores_text(self.m, PRINTED)
        self.assertLess(text.index(names[0]), text.index(names[1]))
        (sheet,) = reports.finish_line_sheets(self.m)                 # one chute sheet per gun
        self.assertEqual(sheet["entered"], 24)
        labels = reports.competitor_labels(self.m)
        self.assertEqual({l["race"] for l in labels}, {"MS 2M"})
        girls_only = printing.results_text(self.m, self.race, PRINTED, events=[self.events[0]])
        self.assertIn(names[0], girls_only)
        self.assertNotIn(names[1], girls_only)


class TestSyntheticMeet(unittest.TestCase):
    def test_everything_runs_and_serialises(self):
        m = synthetic_meet()
        for data in (reports.team_scores(m, breakdown=True), reports.award_winners(m),
                     reports.finish_line_sheets(m), reports.team_labels(m),
                     reports.competitor_labels(m)):
            json.dumps(data)
        text = reports.team_scores_text(m, PRINTED, breakdown=True)
        # the combined girls start: varsity and JV each have their own section
        self.assertIn("Event 2  Girls 5k Run CC Varsity", text)
        self.assertIn("Event 3  Girls 5k Run CC JV", text)
        self.assertLessEqual(max(len(l) for l in text.splitlines()),
                             printing._width(reports.TEAM_SCORE_COLS))
        jv = reports.team_scores_text(m, PRINTED, events=[3])
        self.assertNotIn("Event 2  ", jv)
        self.assertIn("Event 3 Girls 5k Run CC JV", jv.splitlines()[2])


class TestReportPdfs(unittest.TestCase):
    """pdf_reports' renderers for the reports above: page counts and what
    lands on each page (2026-10-03). Parsed with test_meet_pdf's reader."""

    def setUp(self):
        self.m = report_meet()

    def words(self, pdf):
        return all_text(self, pdf)

    def test_team_scores(self):
        pages = self.words(pdf_reports.team_scores_pdf(self.m, breakdown=True,
                                                       printed_at=PRINTED))
        self.assertEqual(len(pages), 1)
        w = pages[0]
        for s in ("Team Scores", "Score Breakdown", "Event 1 · Girls 5k Run CC Varsity",
                  "Event 2 · Boys 5k Run CC Varsity", "1-4-6(8)", "2-7-9(11)", "18*",
                  "BIRCH, Avery", "(8)", "2/3 — not scored", "0:29.8", "Page 1"):
            self.assertIn(s, w)
        self.assertEqual(w.count("TEAM SCORES"), 2)
        self.assertTrue(any(x.startswith("* tied score") for x in w))
        plain = self.words(pdf_reports.team_scores_pdf(self.m, printed_at=PRINTED))[0]
        self.assertNotIn("BIRCH, Avery", plain)
        self.assertIn("1-4-6(8)", plain)
        big = self.words(pdf_reports.team_scores_pdf(synthetic_meet(), breakdown=True,
                                                     printed_at=PRINTED))
        self.assertGreaterEqual(len(big), 2)
        for i, page in enumerate(big, 1):
            self.assertIn(f"Page {i}", page)
            self.assertIn("OFFICIAL MEET REPORT", page)

    def test_awards(self):
        m = report_meet(marks={"204": "DNF"})
        (w,) = self.words(pdf_reports.awards_pdf(m, PRINTED, default_n=3,
                                                 team_awards={1: 2}))
        for s in ("INDIVIDUAL AWARDS — TOP 3", "TEAM AWARDS — TOP 2", "BIRCH, Avery",
                  "18:23.52", "2nd (tie)", "Brookfield"):
            self.assertIn(s, w)
        self.assertEqual(w.count("2nd (tie)"), 2)
        with_gap = self.words(pdf_reports.awards_pdf(self.m, PRINTED, awards={1: 9}))[0]
        self.assertIn("Pl = place in the race; exhibition runners take no award", with_gap)

    def test_finish_line(self):
        pages = self.words(pdf_reports.finish_line_pdf(self.m, printed_at=PRINTED))
        self.assertEqual(len(pages), 2)                       # two races, 40 rows each
        self.assertIn("Girls 5k Run CC Varsity", pages[0])
        self.assertIn("16 entered · places 1-40 of 40 · page 1 of 1", pages[0])
        self.assertEqual([x for x in pages[0] if x.isdigit()][-40:],
                         [str(i) for i in range(1, 41)])
        pages = self.words(pdf_reports.finish_line_pdf(self.m, races=[1], rows=45,
                                                       printed_at=PRINTED))
        self.assertEqual(len(pages), 2)
        self.assertIn("Girls 5k Run CC Varsity (cont'd)", pages[1])
        self.assertIn("PLACES 41-45", pages[1])
        self.assertIn("45", pages[1])
        self.assertNotIn("46", pages[1])

    def test_split_scores(self):
        boys = self.m.races[1]
        (w,) = self.words(pdf_reports.split_scores_pdf(self.m, boys, printed_at=PRINTED))
        for s in ("Split Scores", "MILE 1", "2 MILE", "1-2-4(7)", "3-5-6", "1-3-5(7)",
                  "8 runners timed at Mile 1 · no Mile 1 time, left out: bib 114",
                  "9 runners timed at 2 Mile · 1 estimated"):
            self.assertIn(s, w)
        self.assertLess(w.index("MILE 1"), w.index("2 MILE"))
        live = json.loads(json.dumps(boys.results))
        live["frozen_at"] = None
        (w,) = self.words(pdf_reports.split_scores_pdf(self.m, boys, results=live,
                                                       printed_at=PRINTED))
        self.assertIn("Split Scores — unofficial", w)
        big = big_meet(splits=("Mile 1", "2 Mile"))
        pages = self.words(pdf_reports.split_scores_pdf(big, big.races[0], breakdown=True,
                                                        printed_at=PRINTED))
        self.assertGreater(len(pages), 2)
        self.assertTrue(any("MILE 1 (cont'd)" in p for p in pages))

    def test_label_sheets(self):
        m = big_school_meet(16, 15)
        sheets = reports.label_sheets(reports.competitor_labels(m))
        pdf = pdf_reports.labels_pdf(m, sheets, "Competitor Labels")
        streams = page_streams(self, pdf)
        self.assertEqual(len(streams), 2)
        self.assertIn(b"/MediaBox [0 0 612 792]", pdf)
        g = reports.AVERY_5160
        for stream, sheet in zip(streams, sheets):
            drawn = texts(stream)
            self.assertEqual(len(drawn), 4 * len(sheet["labels"]))     # 4 lines a label
            for i, lab in enumerate(sheet["labels"]):
                for font, size, x, y, text in drawn[4 * i:4 * i + 4]:
                    self.assertGreaterEqual(x, lab["x"])
                    self.assertLessEqual(x + pdfdoc.measure(text, pdfdoc.HELVETICA
                                                            if font == "F1" else pdfdoc.BOLD,
                                                            size), lab["x"] + lab["w"])
                    self.assertGreater(y, lab["y"])
                    self.assertLess(y, lab["y"] + g["label_h"])
            bib = drawn[2]
            self.assertEqual((bib[0], bib[1], bib[4]), ("F2", 20.0, sheet["labels"][0]["bib"]))
        self.assertNotIn(b" re S", streams[0])                       # no outlines by default
        outlined = page_streams(self, pdf_reports.labels_pdf(m, sheets, outline=True))
        self.assertEqual(outlined[0].count(b" re S"), 30)

    def test_long_label_lines_shrink_then_cut(self):
        m = report_meet()
        m.teams[1].name = "Valley Christian Academy of the Lakes and Rivers Upper School"
        sheets = reports.label_sheets(reports.team_labels(m))
        (stream,) = page_streams(self, pdf_reports.labels_pdf(m, sheets))
        width = reports.AVERY_5160["label_w"] - 2 * pdf_reports.LABEL_PAD
        for font, size, x, y, text in texts(stream):
            face = pdfdoc.BOLD if font == "F2" else pdfdoc.HELVETICA
            self.assertLessEqual(pdfdoc.measure(text, face, size), width + 0.01)
            self.assertGreaterEqual(size, pdf_reports.LABEL_MIN_SIZE)

    def test_results_pdf_print_options(self):
        m = synthetic_meet()
        (w, *_) = self.words(pdf_reports.results_pdf(m, m.races[1], events=[3],
                                                     hide=["DNF", "DNS"], printed_at=PRINTED))
        self.assertIn("Filtered: Event 3 Girls 5k Run CC JV · No DNF, DNS", w)
        self.assertEqual(w.count("TEAM SCORING SUMMARY"), 1)
        self.assertNotIn("DNF", w)
        self.assertNotIn("DNS", w)


if __name__ == "__main__":
    unittest.main()
