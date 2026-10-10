"""MeetPro-style PDFs (2026-10-03): pdfdoc (the stdlib PDF writer) and
pdf_reports (results 1-/2-column with splits, rosters). Synthetic data only:
the meets below are generated, the names made up.
python -m unittest tests.test_meet_pdf"""

import random
import re
import unittest
import zlib
from datetime import datetime

from rfid_finishlynx.meet import pdf_reports, pdfdoc, printing, scoring
from rfid_finishlynx.meet.model import (Athlete, Entry, Event, Meet, MeetInfo, Race, Team)

try:
    from tests.test_meet_printing import synthetic_meet
except ImportError:                                    # run from inside tests/
    from test_meet_printing import synthetic_meet

PRINTED = datetime(2026, 10, 3, 15, 4)

FIRST = ["Avery", "Blake", "Casey", "Dana", "Emery", "Finley", "Gray", "Harper", "Indy", "Jules",
         "Kai", "Lane", "Morgan", "Noel", "Oakley", "Parker", "Quinn", "Reese", "Rowan", "Sage",
         "Tatum", "Zoë", "José", "Renée", "Łukasz", "Søren", "Bram", "Cody", "Ellis", "Wren"]
LAST = ["Birch", "Calder", "Dunmore", "Ellery", "Fenwick", "Garth", "Hollis", "Ives", "Jarvis",
        "Keane", "Lark", "Moss", "Núñez", "Oakes", "Pryor", "Quill", "Rowe", "Sable", "Thorne",
        "Castellanos-Whitfield", "O'Day", "Vantreeck", "Wexley", "Yarrow", "Zellner",
        "Ashgrove", "Brightwater-Holloway", "Cobble", "Dorrance", "Elmsworth"]
SCHOOLS = ["Ridgeview High School", "Lakeside Christian Academy of the Pines", "North Hollow",
           "St. Bramble Prep", "Eastfield", "Westmoor A&M Academy", "Copper Basin",
           "Fernwood Charter", "Granite Falls", "Harbor Point", "Ironleaf Academy",
           "Juniper Valley High School", "Kestrel Ridge", "Larchmont Day", "Millbrook (Home School)"]


def big_meet(splits=(), n_teams=15, seed=11, estimated_every=0, missing_every=0,
             gender="M") -> Meet:
    """One ~120-runner race scored by scoring.freeze: 15 teams (two of them
    incomplete), displacers, runners past the 7th who don't score, two
    exhibitions, an unattached runner, a DNF and a DNS. `splits` = split
    labels ("Mile 1", ...); every `estimated_every`-th split is estimated and
    every `missing_every`-th finisher misses the last split."""
    rnd = random.Random(seed)
    m = Meet(meet=MeetInfo(id="2026-10-03-big", name="Synthetic Harvest Invitational",
                           date="2026-10-03", host="Ridgeview High School",
                           location="Ridgeview, SC"))
    m.events = [Event(1, "Boys 5k Run CC Varsity" if gender == "M" else "Girls 5k Run CC Varsity",
                      gender, 5000, "Varsity", "5000")]
    sizes = [12, 11, 10, 10, 9, 9, 9, 8, 8, 8, 7, 7, 6, 4, 3][:n_teams]
    race = Race(1, order=1, name="Varsity Boys" if gender == "M" else "Varsity Girls")
    n = 0
    for i, size in enumerate(sizes, 1):
        m.teams.append(Team(f"t{i}", SCHOOLS[i - 1], SCHOOLS[i - 1].split()[0], f"T{i:02d}"))
        for _ in range(size):
            n += 1
            m.athletes.append(Athlete(f"a{n}", rnd.choice(FIRST), rnd.choice(LAST), gender,
                                      f"t{i}", str(100 * i + n % 100),
                                      grad_year=rnd.choice([2027, 2027, 2028, 2028, 2029,
                                                            2030, 2031])))
            race.entries.append(Entry(f"a{n}", 1))
    m.athletes.append(Athlete("a999", "Unity", "Solo", gender, None, "999", grad_year=2028))
    race.entries.append(Entry("a999", 1))
    race.entries[3].status = "exhibition"
    race.entries[40].status = "exhibition"
    m.races = [race]
    order = [e.athlete_id for e in race.entries]
    rnd.shuffle(order)
    dnf, dns = order.pop(17), order.pop(30)
    rows, t = [], 15 * 60 + 40.0
    for k, aid in enumerate(order, 1):
        t += rnd.uniform(1.5, 9.0)
        sp = []
        for j, label in enumerate(splits, 1):
            if missing_every and j == len(splits) and k % missing_every == 0:
                continue
            sp.append({"point": f"p{j}", "label": label,
                       "elapsed": round(t * j / (len(splits) + 0.1) + rnd.uniform(-4, 4), 3),
                       "estimated": bool(estimated_every and (k * len(splits) + j)
                                         % estimated_every == 0)})
        rows.append({"bib": m.athlete(aid).bib, "finish": {"elapsed": round(t, 3)}, "splits": sp})
    rm = {"finishers": rows, "on_course": [], "no_show": [],
          "expected_points": [f"p{j}" for j in range(1, len(splits) + 1)]}
    race.marks = {m.athlete(dnf).bib: "DNF"}
    scoring.freeze(m, race, rm, now=datetime(2026, 10, 3, 11, 0))
    assert m.athlete(dns).bib                      # listed DNS by freeze
    return m


# ------------------------------------------------------------- PDF reading

def parse(test: unittest.TestCase, pdf: bytes) -> dict:
    """Tiny reader: follow startxref, check every xref offset lands on its
    'n 0 obj', return {n: body}."""
    test.assertTrue(pdf.startswith(b"%PDF-1.4\n"))
    test.assertTrue(pdf.endswith(b"%%EOF\n"))
    xref_at = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf).group(1))
    test.assertEqual(pdf[xref_at:xref_at + 5], b"xref\n")
    head = re.match(rb"xref\n0 (\d+)\n", pdf[xref_at:])
    count = int(head.group(1))
    table = pdf[xref_at + head.end():]
    objs = {}
    for n in range(1, count):
        entry = table[n * 20:(n + 1) * 20]
        test.assertEqual(entry[10:], b" 00000 n \n")
        off = int(entry[:10])
        prefix = f"{n} 0 obj\n".encode()
        test.assertEqual(pdf[off:off + len(prefix)], prefix, f"object {n}")
        objs[n] = pdf[off + len(prefix):pdf.index(b"\nendobj\n", off)]
    test.assertEqual(table[:20], b"0000000000 65535 f \n")
    test.assertIn(f"/Size {count}".encode(), pdf[xref_at:])
    return objs


def page_streams(test: unittest.TestCase, pdf: bytes) -> list:
    """Decompressed content stream of every page, in page order."""
    objs = parse(test, pdf)
    kids = re.search(rb"/Kids \[([^\]]*)\]", objs[2]).group(1)
    out = []
    for k in re.findall(rb"(\d+) 0 R", kids):
        content = int(re.search(rb"/Contents (\d+) 0 R", objs[int(k)]).group(1))
        body = objs[content]
        m = re.match(rb"<< /Length (\d+) /Filter /FlateDecode >>\nstream\n", body)
        test.assertIsNotNone(m)
        data = body[m.end():m.end() + int(m.group(1))]
        test.assertEqual(body[m.end() + int(m.group(1)):], b"\nendstream")
        out.append(zlib.decompress(data))
    test.assertIn(f"/Count {len(out)}".encode(), objs[2])
    return out


_TJ = re.compile(rb"/(F\d) ([\d.]+) Tf ([-\d.]+) ([-\d.]+) Td \(((?:\\.|[^\\)])*)\) Tj")


def _unescape(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b"\\":
            if raw[i + 1:i + 2].isdigit():
                out.append(int(raw[i + 1:i + 4], 8))
                i += 4
                continue
            out += raw[i + 1:i + 2]
            i += 2
            continue
        out += raw[i:i + 1]
        i += 1
    return out.decode("cp1252")


def texts(stream: bytes) -> list:
    """[(font tag, size, x, y_from_bottom, text)] in drawing order."""
    return [(m.group(1).decode(), float(m.group(2)), float(m.group(3)), float(m.group(4)),
             _unescape(m.group(5))) for m in _TJ.finditer(stream)]


def all_text(test, pdf: bytes) -> list:
    return [[t[4] for t in texts(s)] for s in page_streams(test, pdf)]


# ------------------------------------------------------------- pdfdoc

class TestPdfDoc(unittest.TestCase):
    def test_afm_widths(self):
        self.assertAlmostEqual(pdfdoc.measure("A", pdfdoc.HELVETICA, 10), 6.67)
        self.assertAlmostEqual(pdfdoc.measure("il", pdfdoc.HELVETICA, 1000), 444)
        self.assertAlmostEqual(pdfdoc.measure("il", pdfdoc.BOLD, 1000), 556)
        self.assertEqual(pdfdoc.measure("Wm", pdfdoc.OBLIQUE, 7),
                         pdfdoc.measure("Wm", pdfdoc.HELVETICA, 7))
        self.assertEqual(pdfdoc.measure("Wm", pdfdoc.BOLD_OBLIQUE, 7),
                         pdfdoc.measure("Wm", pdfdoc.BOLD, 7))
        self.assertAlmostEqual(pdfdoc.measure("é", pdfdoc.HELVETICA, 1000), 556)
        self.assertAlmostEqual(pdfdoc.measure("±", pdfdoc.HELVETICA, 1000), 584)   # AFM, not Arial
        self.assertEqual(len(pdfdoc.WIDTHS[pdfdoc.BOLD]), 224)

    def test_fit_truncates_by_measured_width(self):
        name = "CASTELLANOS-WHITFIELD, Bartholomew"
        cut = pdfdoc.fit(name, pdfdoc.HELVETICA, 8.5, 103)
        self.assertTrue(name.startswith(cut))
        self.assertLess(len(cut), len(name))
        self.assertLessEqual(pdfdoc.measure(cut, pdfdoc.HELVETICA, 8.5), 103)
        one_more = name[:len(cut) + 1]
        self.assertGreater(pdfdoc.measure(one_more, pdfdoc.HELVETICA, 8.5), 103)
        self.assertEqual(pdfdoc.fit("short", pdfdoc.BOLD, 8, 100), "short")
        self.assertEqual(pdfdoc.fit("iiii", pdfdoc.HELVETICA, 10, 4.5), "ii")      # 2.22 each
        self.assertEqual(pdfdoc.fit("WWW", pdfdoc.BOLD, 10, 5), "")
        self.assertEqual(pdfdoc.fit("Long name", pdfdoc.HELVETICA, 10, 30, "..."),
                         pdfdoc.fit("Long name", pdfdoc.HELVETICA, 10, 30 - 8.34) + "...")

    def test_escaping_and_encoding(self):
        self.assertEqual(pdfdoc.pdf_string("a(b)\\c"), r"(a\(b\)\\c)")
        self.assertEqual(pdfdoc.pdf_string("Núñez"), r"(N\372\361ez)")
        self.assertEqual(pdfdoc.encode("Łukasz Đorđe"), b"Lukasz Dorde")
        self.assertEqual(pdfdoc.encode("e\u0301"), b"\xe9")                # NFC first
        self.assertEqual(pdfdoc.encode("Ω→"), b"??")
        self.assertEqual(pdfdoc.encode("a\tb"), b"a b")
        self.assertEqual(pdfdoc.measure("Ł", pdfdoc.HELVETICA, 1000), 556)  # measured as "L"

    def test_document_structure(self):
        doc = pdfdoc.Document(title="Zoë (test)")
        p = doc.new_page()
        p.text(10, 20, "Hello (world)", pdfdoc.BOLD, 12)
        p.text(100, 20, "right", align="right")
        p.rect(10, 30, 50, 10, fill=0.5)
        p.rect(10, 50, 50, 10, stroke=0, line_width=2)
        p.line(0, 0, 100, 100)
        doc.new_page().text(10, 10, "two")
        pdf = doc.to_bytes()
        objs = parse(self, pdf)
        for face in pdfdoc.FONTS:
            self.assertIn(f"/BaseFont /{face} /Encoding /WinAnsiEncoding".encode(), pdf)
        self.assertIn(b"/Title <FEFF", pdf)
        self.assertIn(b"/MediaBox [0 0 612 792]", pdf)
        s1, s2 = page_streams(self, pdf)
        self.assertIn(br"/F2 12 Tf 10 772 Td (Hello \(world\)) Tj", s1)    # y flipped
        w = pdfdoc.measure("right", pdfdoc.HELVETICA, 10)
        self.assertIn(f"{100 - w:.2f}".rstrip("0").encode() + b" 772 Td (right)", s1)
        self.assertIn(b"0.5 g 10 752 50 10 re f", s1)
        self.assertIn(b"0 G 2 w 10 732 50 10 re S", s1)
        self.assertIn(b"(two) Tj", s2)
        self.assertEqual(len(objs), 1 + 1 + 4 + 1 + 4)       # catalog, pages, fonts, info, 2x2


# ------------------------------------------------------------- results

class TestResultsPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = big_meet()
        cls.race = cls.m.races[0]

    def pdf(self, **kw):
        return pdf_reports.results_pdf(self.m, kw.pop("race", self.race), printed_at=PRINTED,
                                       **kw)

    def test_page_counts_one_vs_two_columns(self):
        res = scoring.official_results(self.race)
        self.assertEqual(len(res["finishers"]), 122)       # 120 finishers + DNF + DNS
        one = page_streams(self, self.pdf(columns=1))
        two = page_streams(self, self.pdf(columns=2))
        # 1-col: 25 under the team summary, then 46 a page; 2-col: 29 + 46, then 46
        self.assertEqual(len(one), 4)
        self.assertEqual(len(two), 2)
        p2 = [t[4] for t in texts(two[0])]
        self.assertIn("INDIVIDUAL RESULTS (cont'd)", p2)  # right column of page 1
        self.assertEqual(p2.count("TEAM SCORING SUMMARY"), 1)
        for i, s in enumerate(one, 1):
            words = [t[4] for t in texts(s)]
            self.assertIn(f"Page {i}", words)
            self.assertIn("OFFICIAL MEET REPORT", words)
            self.assertIn("Final Results", words)
            self.assertIn("SYNTHETIC HARVEST INVITATIONAL", words)
            self.assertIn("Saturday, October 3, 2026", words)
            self.assertIn("printed: 10/3/2026 3:04 PM", words)
        self.assertIn("INDIVIDUAL RESULTS (cont'd)", [t[4] for t in texts(one[1])])

    def test_team_summary_and_scores(self):
        words = all_text(self, self.pdf())[0]
        res = scoring.official_results(self.race)
        first = res["teams"]["1"][0]
        order = re.compile(r"^\d+(-\d+){4}(\(\d+\)){0,2}$")
        self.assertTrue(any(order.match(w) for w in words))
        self.assertIn(str(first["score"]), words)
        self.assertIn("4/5 \u2014 not scored", words)
        self.assertIn("3/5 \u2014 not scored", words)
        self.assertTrue(any(re.fullmatch(r"1:\d\d:\d\d", w) for w in words))     # Total
        self.assertTrue(any(re.fullmatch(r"\d:\d\d\.\d", w) for w in words))      # Spread
        everything = sum(all_text(self, self.pdf()), [])
        self.assertIn("EX", everything)
        self.assertTrue(any(re.fullmatch(r"\(\d+\)", w) for w in everything))   # displacer
        self.assertIn("-", everything)
        self.assertIn("DNF", everything)
        self.assertIn("DNS", everything)
        self.assertIn("Unattached", everything)
        self.assertIn("SOLO, Unity", everything)

    def test_rows_never_overlap(self):
        """Every text drawn in a column stays inside it: names and teams are
        cut by measured width (MeetPro truncates, it never overlaps)."""
        for s in page_streams(self, self.pdf()):
            for font, size, x, y, text in texts(s):
                if font == "F1" and size == pdf_reports.ROW_FONT and 37 <= x < 40:  # name col
                    self.assertLessEqual(pdfdoc.measure(text, pdfdoc.HELVETICA, size), 103.01)

    def test_filtered_output_keeps_real_places(self):
        pdf = self.pdf(places=(10, 20))
        pages = all_text(self, pdf)
        self.assertEqual(len(pages), 1)
        words = pages[0]
        self.assertIn("Filtered: Places 10-20", words)
        res = scoring.official_results(self.race)
        tenth = next(f for f in res["finishers"] if f.get("event_place") == 10)
        a = self.m.athlete(tenth["athlete_id"])
        name = pdf_reports._athlete_name(a)
        i = next(i for i, w in enumerate(words) if name.startswith(w) and len(w) > 3)
        self.assertEqual(words[i - 1], "10")                 # real place, not renumbered to 1
        self.assertNotIn("9", words[words.index("INDIVIDUAL RESULTS"):i])
        team_words = pdf_reports._athlete_name(self.m.athlete(
            next(f for f in res["finishers"] if f.get("event_place") == 1)["athlete_id"]))
        self.assertFalse(any(team_words.startswith(w) and len(w) > 3 for w in words))

    def test_grade_filter_drops_team_summary(self):
        words = all_text(self, self.pdf(grades=["12"]))[0]
        self.assertNotIn("TEAM SCORING SUMMARY", words)
        self.assertIn(printing.GRADE_FILTER_NO_TEAMS, words)
        self.assertIn("Filtered: Grade 12", words)
        yrs = {words[i + 1] for i, w in enumerate(words) if w == "SR"}
        self.assertTrue(yrs)
        self.assertNotIn("JR", words)

    def test_not_final(self):
        race = self.race
        race.status = "scheduled"
        try:
            words = all_text(self, self.pdf())[0]
        finally:
            race.status = "final"
        self.assertIn(pdf_reports.NOT_FINAL, words)
        self.assertNotIn("Final Results", words)

    def test_year_labels(self):
        self.assertEqual([pdf_reports.year_label(g) for g in ("12", "11", "10", "9", "8", "7")],
                         ["SR", "JR", "SO", "9", "8", "7"])
        self.assertEqual(pdf_reports.year_label("Senior"), "SR")
        self.assertEqual(pdf_reports.year_label("Freshman"), "9")
        self.assertEqual(pdf_reports.year_label(""), "")

    def test_number_formats(self):
        self.assertEqual(pdf_reports.format_tenths_up(340.01), "5:40.1")
        self.assertEqual(pdf_reports.format_tenths_up(340.0), "5:40.0")
        self.assertEqual(pdf_reports.format_tenths_up(340.1000000001), "5:40.1")   # noise
        self.assertEqual(pdf_reports.format_pace(983.52, 5000, printing.METERS_PER_MILE),
                         "5:16.6")
        self.assertEqual(pdf_reports.format_pace(983.52, 5000, 1000), "3:16.7")
        self.assertEqual(pdf_reports.format_total(4934.001), "1:22:15")
        self.assertEqual(pdf_reports.format_total(4934.0), "1:22:14")

    def test_combined_start_one_section_per_event(self):
        m = synthetic_meet()
        race = m.races[1]                                    # girls varsity + JV, one gun
        words = all_text(self, pdf_reports.results_pdf(m, race, printed_at=PRINTED))[0]
        self.assertIn("Event 2 \u00b7 Girls 5k Run CC Varsity", words)
        self.assertIn("Event 3 \u00b7 Girls 5k Run CC JV", words)
        self.assertLess(words.index("Event 2 \u00b7 Girls 5k Run CC Varsity"),
                        words.index("Event 3 \u00b7 Girls 5k Run CC JV"))

    def test_meet_results_each_race_on_a_new_page(self):
        m = synthetic_meet()
        pdf = pdf_reports.meet_results_pdf(m, printed_at=PRINTED)
        pages = all_text(self, pdf)
        starts = [p[p.index("Final Results") - 2] for p in pages]
        self.assertEqual(starts[0], "Race #1")
        self.assertIn("Race #2", starts)
        self.assertEqual(starts.index("Race #2"), len(page_streams(
            self, pdf_reports.results_pdf(m, m.races[0], printed_at=PRINTED))))
        m.races[0].status = "scheduled"
        pages = all_text(self, pdf_reports.meet_results_pdf(m, printed_at=PRINTED))
        self.assertNotIn("Race #1", sum(pages, []))
        for r in m.races:
            r.status = "scheduled"
        self.assertIn("No final results yet.", sum(all_text(
            self, pdf_reports.meet_results_pdf(m, printed_at=PRINTED)), []))

    def test_columns_argument(self):
        with self.assertRaises(ValueError):
            self.pdf(columns=3)


class TestSplits(unittest.TestCase):
    def words(self, m, columns=1):
        return sum(all_text(self, pdf_reports.results_pdf(m, m.races[0], columns=columns,
                                                          printed_at=PRINTED)), [])

    def test_no_splits(self):
        words = self.words(big_meet())
        self.assertFalse(any(w.startswith("Mile") for w in words))
        self.assertIn("Avg. kM", words)

    def test_one_split_beside_both_paces(self):
        m = big_meet(splits=["Mile 1"])
        words = self.words(m)
        self.assertIn("Mile 1", words)
        self.assertIn("Avg. kM", words)
        res = scoring.official_results(m.races[0])
        first = res["finishers"][0]
        self.assertIn(pdf_reports.format_tenths_up(first["splits"][0]["elapsed"]), words)
        self.assertNotIn("Mile 1", self.words(m, columns=2))     # 2-column: no splits

    def test_two_splits(self):
        words = self.words(big_meet(splits=["Mile 1", "Mile 2"]))
        self.assertIn("Mile 1", words)
        self.assertIn("Mile 2", words)
        self.assertIn("Avg. kM", words)

    def test_four_splits_drop_avg_km(self):
        words = self.words(big_meet(splits=["1K", "2K", "3K", "4K"]))
        self.assertIn("4K", words)
        self.assertNotIn("Avg. kM", words)
        self.assertIn("Avg. Mile", words)

    def test_six_splits_go_on_a_second_line(self):
        labels = [f"{k}K" for k in range(1, 7)]
        m = big_meet(splits=labels)
        words = self.words(m)
        self.assertIn("Avg. kM", words)
        res = scoring.official_results(m.races[0])
        sp = res["finishers"][0]["splits"]
        line = "   ".join(f"{s['label']} {pdf_reports.format_tenths_up(s['elapsed'])}" for s in sp)
        self.assertTrue(any(line.startswith(w) and len(w) > 20 for w in words))
        one = page_streams(self, pdf_reports.results_pdf(m, m.races[0], printed_at=PRINTED))
        self.assertGreater(len(one), 4)                      # 20 pt rows: more pages

    def test_missing_split(self):
        m = big_meet(splits=["Mile 1", "Mile 2"], missing_every=7)
        words = self.words(m)
        self.assertIn(pdf_reports.NO_SPLIT, words)
        self.assertTrue(any("— = no split" in w for w in words))

    def test_estimated_split(self):
        m = big_meet(splits=["Mile 1", "Mile 2"], estimated_every=5)
        words = self.words(m)
        self.assertTrue(any(re.fullmatch(r"~\d+:\d\d\.\d", w) for w in words))
        self.assertTrue(any("~ = estimated" in w for w in words))
        plain = self.words(big_meet(splits=["Mile 1", "Mile 2"]))
        self.assertFalse(any(w.startswith("~") for w in plain))

    def test_split_mode_rule(self):
        self.assertEqual([pdf_reports._split_mode(n) for n in range(8)],
                         ["none", "columns", "columns", "columns", "columns_no_km",
                          "columns_no_km", "line", "line"])
        for n in range(1, 6):
            cols, _, _ = pdf_reports._individual_cols(1, [f"Mile {i}" for i in range(n)])
            self.assertLessEqual(max(c.x for c in cols) + pdf_reports.LEFT, pdf_reports.RIGHT)


# ------------------------------------------------------------- rosters

def roster_meet(girls: int, boys: int, extra_schools: int = 2) -> Meet:
    rnd = random.Random(5)
    m = Meet(meet=MeetInfo(id="r", name="Synthetic Roster Check", date="2026-10-03",
                           host="Ridgeview High School"))
    m.events = [Event(1, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls"),
                Event(2, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys"),
                Event(3, "Boys 5k Run CC JV", "M", 5000, "JV Boys")]
    races = [Race(1, order=1), Race(2, order=2), Race(3, order=3)]
    m.races = races
    n = 0
    plan = [(girls, boys)] + [(rnd.randint(4, 12), rnd.randint(5, 14))
                              for _ in range(extra_schools)]
    for i, (g, b) in enumerate(plan, 1):
        m.teams.append(Team(f"t{i}", SCHOOLS[i - 1], SCHOOLS[i - 1].split()[0], f"T{i:02d}"))
        for gender, k in (("F", g), ("M", b)):
            for j in range(k):
                n += 1
                m.athletes.append(Athlete(f"a{n}", rnd.choice(FIRST), rnd.choice(LAST), gender,
                                          f"t{i}", str(1000 + n) if n % 23 else "",
                                          grad_year=rnd.choice([2027, 2028, 2029, 2030])))
                race = races[0] if gender == "F" else races[1 + (j >= 7)]
                race.entries.append(Entry(f"a{n}", race.event,
                                          status="exhibition" if n % 17 == 0 else "entered"))
    return m


class TestRostersPdf(unittest.TestCase):
    def pages(self, m, team_ids=None):
        return page_streams(self, pdf_reports.rosters_pdf(m, team_ids, printed_at=PRINTED))

    def test_one_page_per_school_up_to_capacity(self):
        cap = pdf_reports.ROSTER_ROWS
        m = roster_meet(cap, 9)
        pages = self.pages(m, ["t1"])
        self.assertEqual(len(pages), 1)
        words = [t[4] for t in texts(pages[0])]
        self.assertIn("GIRLS", words)
        self.assertIn("BOYS", words)
        self.assertIn(f"Girls {cap} \u00b7 Boys 9", words)
        self.assertIn("Team Rosters", words)
        self.assertIn(f"Total girls: {cap}", words)          # each gender's count (2026-10-03)
        self.assertIn("Total boys: 9", words)
        self.assertEqual(len(self.pages(m)), 3)              # three normal schools

    def test_overflow_gives_a_page_per_gender(self):
        cap = pdf_reports.ROSTER_ROWS
        m = roster_meet(cap + 1, 9)
        pages = self.pages(m, ["t1"])
        self.assertEqual(len(pages), 2)
        p1 = [t[4] for t in texts(pages[0])]
        p2 = [t[4] for t in texts(pages[1])]
        self.assertIn("GIRLS", p1)
        self.assertIn("GIRLS (cont'd)", p1)
        self.assertNotIn("BOYS", p1)
        self.assertIn("BOYS", p2)
        self.assertNotIn("GIRLS", p2)
        self.assertTrue(any(w.endswith("page 1 of 2") for w in p1))
        self.assertEqual([w for w in p1 if w.startswith("Total")], [f"Total girls: {cap + 1}"])
        # the cont'd column holds exactly the one girl who didn't fit
        data = printing.roster_pages(m, ["t1"], rows_per_column=cap)
        self.assertEqual([len(c["rows"]) for c in data[0]["columns"]], [cap, 1])

    def test_boys_left_when_they_hold_the_lower_bibs(self):
        m = roster_meet(6, 8, extra_schools=0)
        for a in m.athletes:
            if a.bib:
                a.bib = str(int(a.bib) + (100 if a.gender == "F" else 0))
        (page,) = self.pages(m)
        t = {w: x for _, _, x, _, w in texts(page)}
        self.assertLess(t["BOYS"], t["GIRLS"])
        self.assertIn("Boys 8 · Girls 6", t)

    def test_schedule_one_page_with_start_times(self):
        """Schedule (2026-10-03): run order with scheduled starts, the
        MeetPro-style frame, not marked as an official report."""
        m = roster_meet(6, 8)                           # three races, run in order 1-2-3
        m.races[0].start_time = "08:00"
        m.races[1].start_time = "13:05"
        pages = all_text(self, pdf_reports.schedule_pdf(m, printed_at=PRINTED))
        self.assertEqual(len(pages), 1)
        words = pages[0]
        self.assertIn("RUN ORDER", words)
        self.assertIn("8:00 AM", words)
        self.assertIn("1:05 PM", words)
        self.assertLess(words.index("8:00 AM"), words.index("1:05 PM"))
        self.assertNotIn("OFFICIAL MEET REPORT", words)
        for r in printing.schedule_rows(m):
            self.assertIn(r["race"], words)
            self.assertIn(r["rk"], words)
        self.assertIn("Scheduled times: a race may start later than listed.", words)
        for r in m.races:
            r.start_time = ""
        words = all_text(self, pdf_reports.schedule_pdf(m, printed_at=PRINTED))[0]
        self.assertIn("—", words)                       # no time: a dash in the column
        self.assertTrue(any("No start times set yet" in w for w in words))

    def test_rows(self):
        m = roster_meet(6, 8)
        words = sum(all_text(self, pdf_reports.rosters_pdf(m, printed_at=PRINTED)), [])
        self.assertIn("V 5K", words)
        self.assertIn("JV 5K", words)
        self.assertIn("EX", words)
        self.assertIn("----", words)                        # no bib yet
        a = m.athletes[0]
        self.assertIn(f"{a.last.upper()}, {a.first}", words)


if __name__ == "__main__":
    unittest.main()
