"""Lynx start lists from the meet model (rfid_finishlynx/meet/lynx.py).

Round trips go through the console's own readers (web.parse_ppl_file,
web.evt_race_list), so a format change that would break the console fails
here. Synthetic names only; files go to temp dirs, never a Lynx folder.
python -m unittest tests.test_meet_lynx"""

import os
import tempfile
import unittest
from unittest import mock

from rfid_finishlynx import web
from rfid_finishlynx.meet import lynx
from rfid_finishlynx.meet.model import Athlete, Entry, Event, Meet, MeetInfo, Race, Team


def two_race_meet() -> Meet:
    """Girls run first although they are event 2; one scratch, one
    exhibition, one entrant without a bib, one rostered athlete with no race."""
    m = Meet(meet=MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10"))
    m.teams = [Team("t1", "Ridge High School", short="Ridge", code="RID"),
               Team("t2", "Valley High School", short="Valley", code="VAL")]
    m.athletes = [
        Athlete("a1", "Avery", "Birch", "F", team_id="t1", bib="101"),
        Athlete("a2", "Casey", "Dunmore", "F", team_id="t2", bib="102"),
        Athlete("a3", "Emery", "Fenwick", "F", team_id="t1", bib="103"),     # scratched
        Athlete("a4", "Blake", "Calder", "M", team_id="t2", bib="201"),
        Athlete("a5", "Gray", "Hollis", "M", team_id="t1", bib="202"),       # exhibition
        Athlete("a6", "Jules", "Kerr", "M", team_id="t2"),                   # no bib yet
        Athlete("a7", "Lane", "Morrow", "F", team_id="t2", bib="104"),       # in no race
    ]
    m.events = [Event(1, "Boys 5k Run CC Varsity", gender="M", distance_m=5000, code="5000"),
                Event(2, "Girls 5k Run CC Varsity", gender="F", distance_m=5000, code="5000")]
    m.races = [Race(1, order=2, entries=[Entry("a4", 1), Entry("a5", 1, status="exhibition"),
                                         Entry("a6", 1)]),
               Race(2, order=1, entries=[Entry("a2", 2), Entry("a1", 2),
                                         Entry("a3", 2, status="scratched")])]
    return m


EXPECTED_EVT = (";Created by rfid-meet\r\n"
                "2,1,1,Girls 5k Run CC Varsity,,,,,,5000\r\n"
                ",101,,Birch,Avery,Ridge\r\n"
                ",102,,Dunmore,Casey,Valley\r\n"
                "1,1,1,Boys 5k Run CC Varsity,,,,,,5000\r\n"
                ",202,,Hollis,Gray,Ridge\r\n"
                ",201,,Calder,Blake,Valley\r\n")
EXPECTED_PPL = ("101,Birch,Avery,Ridge,,F\r\n"
                "102,Dunmore,Casey,Valley,,F\r\n"
                "103,Fenwick,Emery,Ridge,,F\r\n"
                "104,Morrow,Lane,Valley,,F\r\n"
                "201,Calder,Blake,Valley,,M\r\n"
                "202,Hollis,Gray,Ridge,,M\r\n"
                ",Kerr,Jules,Valley,,M\r\n")
EXPECTED_SCH = "2,1,1\r\n1,1,1\r\n"


class TestText(unittest.TestCase):

    def test_exact_dialect(self):
        m = two_race_meet()
        self.assertEqual(lynx.evt_text(m), EXPECTED_EVT)
        self.assertEqual(lynx.ppl_text(m), EXPECTED_PPL)
        self.assertEqual(lynx.sch_text(m), EXPECTED_SCH)

    def test_counts_and_warnings(self):
        out = lynx.render(two_race_meet())
        self.assertEqual(out["counts"]["races"], 2)
        self.assertEqual(out["counts"]["entries"], 4)
        self.assertEqual(out["counts"]["scratched"], 1)
        self.assertEqual(out["counts"]["skipped_no_bib"], 1)
        self.assertEqual(out["counts"]["athletes"], 7)
        self.assertEqual(out["counts"]["athletes_without_bib"], 1)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("Jules Kerr", out["warnings"][0])
        self.assertIn("no bib", out["warnings"][0])

    def test_lanes_order_entries_and_are_written(self):
        m = two_race_meet()
        m.races[1].entries[0].lane = 1        # Dunmore lane 1, Birch lane-less after
        lines = lynx.evt_text(m).splitlines()
        self.assertEqual(lines[2:4], [",102,1,Dunmore,Casey,Valley", ",101,,Birch,Avery,Ridge"])

    def test_no_distance_and_missing_event(self):
        m = two_race_meet()
        m.events[0].distance_m = 0
        m.races.append(Race(9, order=3))
        out = lynx.render(m)
        self.assertIn("1,1,1,Boys 5k Run CC Varsity,,,,,,\r\n", out["evt"])
        self.assertIn("9,1,1,Event 9,,,,,,\r\n", out["evt"])
        self.assertTrue(any("no event 9" in w for w in out["warnings"]))

    def test_race_name_overrides_event_name(self):
        m = two_race_meet()
        m.races[0].name = "Boys 5k Open"
        self.assertIn("1,1,1,Boys 5k Open,,,,,,5000", lynx.evt_text(m))

    def test_gender_the_console_reads_from_the_name_is_checked(self):
        m = two_race_meet()
        m.events[0].name = "Varsity 5k"       # console would treat it as mixed
        out = lynx.render(m)
        self.assertTrue(any("gender mixed" in w and "event is M" in w for w in out["warnings"]))
        m.events[0].name = "Women's 5k"       # wrong word entirely
        self.assertTrue(any("gender F" in w for w in lynx.render(m)["warnings"]))

    def test_name_gender_matches_the_console(self):
        for name in ("Boys 5k", "Girls 5k", "Men's 800", "Women's 800", "WOMEN 5000",
                     "Varsity 5k", "JV Girls", "Open", "Mens Open"):
            self.assertEqual(lynx._name_gender(name), web._determine_event_gender(name), name)

    def test_unknown_athlete_and_double_entry_are_left_out(self):
        m = two_race_meet()
        m.races[1].entries += [Entry("a99", 2), Entry("a1", 2)]
        out = lynx.render(m)
        self.assertEqual(out["evt"].count(",101,"), 1)
        self.assertTrue(any("unknown athlete a99" in w for w in out["warnings"]))
        self.assertTrue(any("entered twice" in w for w in out["warnings"]))

    def test_duplicate_bib_is_reported(self):
        m = two_race_meet()
        m.athletes[6].bib = "101"
        self.assertTrue(any("bib 101 on 2 athletes" in w for w in lynx.render(m)["warnings"]))

    def test_relay_race_header_only_for_now(self):
        m = two_race_meet()
        m.events.append(Event(3, "Boys 4x800 Relay", gender="M", distance_m=3200, kind="relay"))
        m.races.append(Race(3, order=3, entries=[Entry("a4", 3)]))
        out = lynx.render(m)
        self.assertTrue(out["evt"].endswith("3,1,1,Boys 4x800 Relay,,,,,,3200\r\n"))
        self.assertTrue(any("relay entries are not written yet" in w for w in out["warnings"]))
        self.assertTrue(web.evt_race_list(out["evt"])[-1]["is_relay"])

    def test_unbibbed_athletes_can_be_dropped_from_the_ppl(self):
        with mock.patch.object(lynx, "PPL_INCLUDE_UNBIBBED", False):
            self.assertNotIn("Kerr", lynx.ppl_text(two_race_meet()))


class TestCleaning(unittest.TestCase):

    def test_comma_semicolon_quote_newline(self):
        self.assertEqual(lynx.clean("Smith, Jr."), "Smith Jr.")
        self.assertEqual(lynx.clean(";Drop"), "Drop")
        self.assertEqual(lynx.clean('Al "Ace"\r\nOne'), "Al Ace One")
        self.assertEqual(lynx.clean("Tab\there"), "Tab here")
        self.assertEqual(lynx.clean(None), "")
        self.assertEqual(lynx.clean("O'Brien-Ruiz"), "O'Brien-Ruiz")

    def test_accents_fold_to_ascii(self):
        self.assertEqual(lynx.clean("José Núñez"), "Jose Nunez")
        self.assertEqual(lynx.clean("Søren Łukasz Strauß"), "Soren Lukasz Strauss")
        self.assertEqual(lynx.clean("O’Neil – Dash"), "O'Neil - Dash")
        warnings = []
        self.assertEqual(lynx.clean("李", warnings, "last name"), "?")
        self.assertEqual(len(warnings), 1)

    def test_comma_in_names_and_school_keeps_the_fields(self):
        m = two_race_meet()
        m.athletes[0].last = "Birch, Jr."
        m.athletes[0].first = "Avery\nAnn"
        m.teams[0].short = "Ridge, North"
        m.events[1].name = "Girls 5k, Varsity"
        evt = lynx.evt_text(m)
        races = web.evt_race_list(evt)
        self.assertEqual(races[0]["name"], "Girls 5k Varsity")
        self.assertEqual(races[0]["distance"], "5000")
        self.assertEqual(races[0]["bibs"], ["101", "102"])
        for line in evt.splitlines()[1:]:
            self.assertEqual(len(line.split(",")), 10 if line[0].isdigit() else 6, line)
        with tempfile.TemporaryDirectory() as d:
            lynx.write_start_lists(m, d)
            by_bib = web.parse_ppl_file(os.path.join(d, "lynx.ppl"))["by_bib"]
        self.assertEqual(by_bib["101"], {"last_name": "Birch Jr.", "first_name": "Avery Ann",
                                         "school": "Ridge North", "gender": "F"})

    def test_output_is_ascii_so_cp1252_and_utf8_readers_agree(self):
        m = two_race_meet()
        m.athletes[0].last = "Núñez"
        with tempfile.TemporaryDirectory() as d:
            lynx.write_start_lists(m, d)
            for name in ("lynx.evt", "lynx.ppl", "lynx.sch"):
                with open(os.path.join(d, name), "rb") as f:
                    f.read().decode("ascii")                  # raises if not
            self.assertEqual(web.parse_ppl_file(os.path.join(d, "lynx.ppl"))["by_bib"]["101"]
                             ["last_name"], "Nunez")


class TestRoundTrip(unittest.TestCase):
    """Written by us, read by the console exactly as it reads MeetPro's files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.meet = two_race_meet()
        self.report = lynx.write_start_lists(self.meet, self.dir)

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_report(self):
        r = self.report
        self.assertEqual(r["paths"], {k: os.path.join(self.dir, f"lynx.{k}") for k in ("evt", "ppl", "sch")})
        self.assertEqual(r["written"], ["lynx.ppl", "lynx.sch", "lynx.evt"])
        self.assertEqual((r["unchanged"], r["errors"]), ([], []))
        self.assertEqual(r["counts"]["entries"], 4)
        self.assertEqual(len(r["warnings"]), 1)
        self.assertEqual(sorted(os.listdir(self.dir)), ["lynx.evt", "lynx.ppl", "lynx.sch"])

    def test_evt_races_order_bibs_gender_distance(self):
        races = web.evt_race_list(self.read("lynx.evt"))
        self.assertEqual([(r["event"], r["round"], r["heat"]) for r in races], [(2, 1, 1), (1, 1, 1)])
        self.assertEqual([r["name"] for r in races],
                         ["Girls 5k Run CC Varsity", "Boys 5k Run CC Varsity"])
        self.assertEqual([r["gender"] for r in races], ["F", "M"])
        self.assertEqual([r["distance"] for r in races], ["5000", "5000"])
        self.assertEqual([r["is_relay"] for r in races], [False, False])
        self.assertEqual(races[0]["bibs"], ["101", "102"])          # 103 scratched
        self.assertEqual(sorted(races[1]["bibs"]), ["201", "202"])  # exhibition runs, no-bib left out

    def test_evt_heat_entries_parser(self):
        heats = web.parse_all_heat_entries(self.read("lynx.evt"))
        self.assertEqual(heats[("2", "1")]["bibs"], {"101", "102"})
        self.assertEqual(heats[("1", "1")]["gender"], "M")

    def test_ppl_names_schools_genders(self):
        data = web.parse_ppl_file(os.path.join(self.dir, "lynx.ppl"))
        self.assertTrue(data["loaded"])
        self.assertEqual(sorted(data["by_bib"]), ["101", "102", "103", "104", "201", "202"])
        for a in self.meet.athletes:
            if a.bib:
                self.assertEqual(data["by_bib"][a.bib], self.meet.athlete_lookup(a.bib), a.bib)
        self.assertEqual(data["by_school_gender"]["Ridge"], {"M": ["202"], "F": ["101", "103"]})

    def test_sch_is_run_order(self):
        rows = [l.split(",") for l in self.read("lynx.sch").splitlines() if l and not l.startswith(";")]
        self.assertEqual(rows, [["2", "1", "1"], ["1", "1", "1"]])

    def test_panes_made_from_our_evt(self):
        from rfid_finishlynx.races import RaceManager
        mgr = RaceManager(state_path=None, athlete_lookup=self.meet.athlete_lookup)
        self.assertEqual(web.create_missing_panes(mgr, web.evt_race_list(self.read("lynx.evt"))), 2)
        by_event = {r.event: r for r in mgr.races.values()}
        self.assertEqual(set(by_event[2].entries), {"101", "102"})
        self.assertEqual(by_event[1].gender, "M")


class TestWriting(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def age(self):
        """Back-date every file so a rewrite is visible in the mtime."""
        for n in os.listdir(self.dir):
            os.utime(os.path.join(self.dir, n), (1_000_000_000, 1_000_000_000))

    def mtimes(self):
        return {n: os.stat(os.path.join(self.dir, n)).st_mtime for n in os.listdir(self.dir)}

    def test_unchanged_content_is_not_rewritten(self):
        m = two_race_meet()
        lynx.write_start_lists(m, self.dir)
        self.age()
        r = lynx.write_start_lists(m, self.dir)
        self.assertEqual(r["written"], [])
        self.assertEqual(r["unchanged"], ["lynx.ppl", "lynx.sch", "lynx.evt"])
        self.assertEqual(set(self.mtimes().values()), {1_000_000_000})

    def test_only_the_changed_file_is_rewritten(self):
        m = two_race_meet()
        lynx.write_start_lists(m, self.dir)
        self.age()
        m.races[1].entries[2].status = "entered"          # 103 un-scratched: .evt only
        r = lynx.write_start_lists(m, self.dir)
        self.assertEqual(r["written"], ["lynx.evt"])
        t = self.mtimes()
        self.assertGreater(t["lynx.evt"], 1_000_000_000)
        self.assertEqual((t["lynx.ppl"], t["lynx.sch"]), (1_000_000_000, 1_000_000_000))
        self.age()
        m.athletes[6].first = "Laine"                      # roster-only change: .ppl only
        self.assertEqual(lynx.write_start_lists(m, self.dir)["written"], ["lynx.ppl"])

    def test_locked_file_is_reported_and_left_intact(self):
        m = two_race_meet()
        lynx.write_start_lists(m, self.dir)
        before = open(os.path.join(self.dir, "lynx.evt"), "rb").read()
        m.races[1].entries[2].status = "entered"
        real_replace = os.replace

        def replace(src, dst):
            if dst.endswith("lynx.evt"):
                raise PermissionError(13, "being used by another process")
            return real_replace(src, dst)

        with mock.patch.object(lynx.os, "replace", replace), \
                mock.patch.object(lynx, "REPLACE_DELAY", 0):
            r = lynx.write_start_lists(m, self.dir)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("lynx.evt", r["errors"][0])
        self.assertEqual(open(os.path.join(self.dir, "lynx.evt"), "rb").read(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ["lynx.evt", "lynx.ppl", "lynx.sch"])

    def test_missing_folder_raises(self):
        with self.assertRaises(FileNotFoundError):
            lynx.write_start_lists(two_race_meet(), os.path.join(self.dir, "nope"))

    def test_crlf_bytes_on_disk(self):
        lynx.write_start_lists(two_race_meet(), self.dir)
        with open(os.path.join(self.dir, "lynx.sch"), "rb") as f:
            self.assertEqual(f.read(), b"2,1,1\r\n1,1,1\r\n")


class TestTemplates(unittest.TestCase):

    def test_meetpro_mode_leaves_the_column_empty(self):
        self.assertNotIn("TEMPLATE", lynx.evt_text(two_race_meet()))

    def test_console_mode_is_a_fixed_point_of_the_consoles_template_pass(self):
        """With templates written the console's own .evt rewrite (monitor /
        Save .evt, sport xc) changes nothing, so it can't trigger itself."""
        text = lynx.evt_text(two_race_meet(), templates="console")
        self.assertIn(",,,TEMPLATE_XC,,,5000", text)
        processed, used, _, _ = web.process_evt_file_with_templates(text, "xc")
        self.assertEqual(processed.replace("\n", "\r\n"), text)     # text mode on Windows
        self.assertEqual(used, ["TEMPLATE_XC"])

    def test_console_mode_track_matches_the_console(self):
        m = two_race_meet()
        m.meet.kind = "track"
        m.events[0].name = "Boys 800 Meters"
        m.events[1].name = "Girls 4x800 Relay"
        text = lynx.evt_text(m, templates="console")
        processed, _, _, _ = web.process_evt_file_with_templates(text, "track", "finish")
        self.assertEqual(processed.replace("\n", "\r\n"), text)
        self.assertIn("TEMPLATE_800m_FL", text)
        self.assertIn("TEMPLATE_4x800m_FL", text)


if __name__ == "__main__":
    unittest.main()
