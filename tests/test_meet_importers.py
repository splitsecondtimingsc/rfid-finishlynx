"""Meet importers: MileSplit entries (fixture scrubbed from a real pull),
refresh/merge without losing local work, Hy-Tek semicolon and CSV fallbacks.
All names are synthetic."""
import copy
import json
import os
import tempfile
import unittest

from rfid_finishlynx.meet import importers
from rfid_finishlynx.meet.importers import (distance_m, event_name, import_csv, import_hytek,
                                            import_milesplit, parse_hytek, parse_milesplit,
                                            refresh_milesplit, team_code)
from rfid_finishlynx.meet.model import Athlete, Entry, Meet, MeetInfo, Team

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")


def info() -> MeetInfo:
    return MeetInfo(id="2026-10-10-test", name="Test Invitational", date="2026-10-10")


def fixture() -> list:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def ms_entry(div="Varsity Girls", gender="f", code="5000", status=1, seed=""):
    return {"entryEventCode": code, "entryEventGender": gender, "entryDivisionId": 0,
            "entryDivisionName": div, "entryStatus": status, "entrySeed": seed,
            "entryNote": "", "entryLetter": ""}


def ms_athlete(ms_id, first, last, gender="f", year=2028, entries=None):
    return {"athleteFirstName": first, "athleteLastName": last, "athleteMileSplitId": ms_id,
            "athleteMileSplitUniversalId": f"U{ms_id}", "athleteYear": year,
            "athleteGender": gender, "athleteDob": None,
            "entries": entries if entries is not None else [ms_entry(gender=gender)]}


def ms_team(tid, name, abbrev, roster, id_key="teamMileSplitId"):
    return {"teamName": name, id_key: tid, "teamAbbreviation": abbrev, "teamCity": "Testville",
            "teamState": "SC", "teamLetter": "", "relays": [], "roster": roster}


def find(meet: Meet, ms_id: int, team_id=None) -> Athlete:
    hits = [a for a in meet.athletes if a.milesplit_id == ms_id
            and (team_id is None or a.team_id == team_id)]
    assert len(hits) == 1, hits
    return hits[0]


class TestMilesplitFixture(unittest.TestCase):
    def setUp(self):
        self.meet, self.rep = import_milesplit(FIXTURE, info())

    def test_imported_meet_is_valid(self):
        self.assertEqual(self.meet.validate(), [])
        self.assertEqual(self.rep.notes, [])

    def test_counts(self):
        m = self.meet
        self.assertEqual(len(m.teams), 12)
        self.assertEqual(len(m.athletes), 340)
        self.assertEqual(sum(len(r.entries) for r in m.races), 341)
        self.assertTrue(all(a.source == "milesplit" and a.milesplit_id and a.universal_id
                            for a in m.athletes))

    def test_events_named_and_numbered(self):
        self.assertEqual([(e.num, e.name, e.gender, e.distance_m, e.division)
                          for e in self.meet.events],
                         [(1, "Girls 5k Run CC Varsity", "F", 5000, "Varsity Girls"),
                          (2, "Boys 5k Run CC Varsity", "M", 5000, "Varsity Boys"),
                          (3, "Girls 5k Run CC JV", "F", 5000, "JV Girls"),
                          (4, "Boys 5k Run CC JV", "M", 5000, "JV Boys")])
        self.assertEqual([(r.key, r.order, len(r.entries)) for r in self.meet.races],
                         [((1, 1, 1), 1, 82), ((2, 1, 1), 2, 81), ((3, 1, 1), 3, 86),
                          ((4, 1, 1), 4, 92)])
        for r in self.meet.races:
            self.assertTrue(all(e.event == r.event for e in r.entries))

    def test_teams(self):
        t = self.meet.teams[0]
        self.assertEqual((t.name, t.short, t.code, t.milesplit_id, t.city, t.state),
                         ("Ridge High School", "Ridge", "RID1", 900001, "Testville", "SC"))
        codes = [t.code for t in self.meet.teams]
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(all(c == c.upper() and len(c) <= 4 for c in codes))
        self.assertEqual(len({t.short for t in self.meet.teams}), 12)

    def test_athlete_fields(self):
        a = find(self.meet, 9001001)
        self.assertEqual((a.first, a.last, a.gender, a.grad_year, a.team_id),
                         ("Avery", "Dunmore", "M", 2027, "t1"))
        self.assertEqual(self.meet.grade(a), "12")
        self.assertEqual(a.bib, "")
        self.assertEqual(sum(1 for x in self.meet.athletes if x.dob == "2010-01-01"), 7)

    def test_two_entries_and_seeds(self):
        a = find(self.meet, 9005013)
        got = sorted((r.event, e.seed) for r, e in self.meet.entries_for(a.id))
        self.assertEqual(got, [(2, "21:52.26"), (4, "")])
        seeds = [e.seed for r in self.meet.races for e in r.entries]
        self.assertEqual(seeds.count(""), 10)
        self.assertEqual(self.meet.race(1).entries[0].status, "entered")

    def test_refresh_with_same_pull_changes_nothing(self):
        before = copy.deepcopy(self.meet)
        rep = refresh_milesplit(self.meet, FIXTURE)
        self.assertTrue(rep.empty, rep.summary())
        self.assertEqual(self.meet, before)


class TestMilesplitShapes(unittest.TestCase):
    def test_data_wrapper_team_id_and_string_year(self):
        teams = [ms_team(1, "Ridge High School", "rid",
                         [ms_athlete(11, "Avery", "Birch", year="2029")], id_key="teamId")]
        m, _ = import_milesplit({"data": teams}, info())
        self.assertEqual(m.teams[0].milesplit_id, 1)
        self.assertEqual(m.teams[0].code, "RID")
        self.assertEqual(m.athletes[0].grad_year, 2029)
        self.assertEqual(m.grade(m.athletes[0]), "10")
        m2, _ = import_milesplit(json.dumps({"data": teams}), info())
        self.assertEqual(len(m2.athletes), 1)

    def test_scratch_and_other_xc_codes(self):
        roster = [ms_athlete(11, "Avery", "Birch", entries=[ms_entry(status=-1, seed="20:01.0")]),
                  ms_athlete(12, "Blake", "Calder", "m", entries=[
                      ms_entry("Middle School Boys", "m", "3200"),
                      ms_entry("Open Boys", "m", "2MILE")]),
                  ms_athlete(13, "Casey", "Dunmore", "m", entries=[ms_entry("Varsity Boys", "m",
                                                                            "8000")])]
        m, _ = import_milesplit([ms_team(1, "Ridge High School", "RID", roster)], info())
        self.assertEqual(m.validate(), [])
        self.assertEqual(m.race(1).entries[0].status, "scratched")
        self.assertEqual(m.race(1).entries[0].seed, "20:01.0")
        self.assertEqual([(e.name, e.distance_m) for e in m.events],
                         [("Girls 5k Run CC Varsity", 5000),
                          ("Boys 8k Run CC Varsity", 8000),
                          ("Boys 3200 Meter Run CC Middle School", 3200),
                          ("Boys 2 Mile Run CC Open", 3219)])

    def test_naming_helpers(self):
        self.assertEqual(distance_m("1MILE"), 1609)
        self.assertEqual(distance_m("4000"), 4000)
        self.assertEqual(distance_m("6K"), 6000)
        self.assertEqual(distance_m("HJ"), 0)
        self.assertEqual(event_name("F", "4000", "JV Girls"), "Girls 4k Run CC JV")
        self.assertEqual(event_name("M", "1MILE", "Boys"), "Boys 1 Mile Run CC")
        self.assertEqual(event_name("M", "6000", "Junior Varsity Boys"),
                         "Boys 6k Run CC Junior Varsity")
        self.assertEqual(event_name("F", "", "Varsity"), "Girls Varsity")

    def test_default_order(self):
        roster = [ms_athlete(10 + i, "Avery", f"Birch{i}", g, entries=[ms_entry(d, g)])
                  for i, (d, g) in enumerate([("Open Girls", "f"), ("JV Boys", "m"),
                                              ("Middle School Boys", "m"),
                                              ("Varsity Boys", "m"), ("JV Girls", "f"),
                                              ("Varsity Girls", "f")])]
        m, _ = import_milesplit([ms_team(1, "Ridge", "RID", roster)], info())
        self.assertEqual([e.division for e in m.events],
                         ["Varsity Girls", "Varsity Boys", "JV Girls", "JV Boys",
                          "Middle School Boys", "Open Girls"])
        self.assertEqual([r.order for r in m.races_in_order()], [1, 2, 3, 4, 5, 6])

    def test_team_codes_made_unique(self):
        teams = [ms_team(1, "Ridge High School", "RID", [ms_athlete(11, "Avery", "Birch")]),
                 ms_team(2, "Ridge Middle School", "rid", [ms_athlete(12, "Blake", "Calder")]),
                 ms_team(3, "Ridgeway Academy", "RIDGEWAY", []),
                 ms_team(4, "North Ridge High School", "", []),
                 ms_team(5, "Ridge", "", [])]
        m, _ = import_milesplit(teams, info())
        self.assertEqual([t.code for t in m.teams], ["RID", "RID2", "RIDG", "NR", "RID3"])
        self.assertEqual([t.short for t in m.teams],
                         ["Ridge", "Ridge MS", "Ridgeway Academy", "North Ridge",
                          "Ridge (RID3)"])
        self.assertEqual(m.validate(), [])
        self.assertEqual(team_code("ab", "x", [Team("t1", "A", code="AB")]), "AB2")

    def test_unattached_team_has_no_team_record(self):
        teams = [ms_team(7, "Unattached", "UNA", [ms_athlete(11, "Avery", "Birch")])]
        m, _ = import_milesplit(teams, info())
        self.assertEqual(m.teams, [])
        self.assertIsNone(m.athletes[0].team_id)
        self.assertEqual(m.validate(), [])

    def test_bad_gender_skipped_with_note(self):
        a = ms_athlete(11, "Avery", "Birch", gender="", entries=[])
        m, rep = import_milesplit([ms_team(1, "Ridge", "RID", [a])], info())
        self.assertEqual(m.athletes, [])
        self.assertIn("skipped Avery Birch", rep.notes[0])


class TestTwoTeamAthlete(unittest.TestCase):
    """(2026-10-03) one MileSplit athlete on two teams = two Athlete records
    sharing the MileSplit ids, one per team, each with its own bib."""

    def pull(self):
        both = 501
        return [ms_team(1, "Ridge High School", "RID",
                        [ms_athlete(both, "Avery", "Birch"), ms_athlete(502, "Blake", "Calder")]),
                ms_team(2, "Valley Club", "VAL",
                        [ms_athlete(both, "Avery", "Birch",
                                    entries=[ms_entry("JV Girls")])])]

    def test_two_records_two_bibs(self):
        m, _ = import_milesplit(self.pull(), info())
        recs = [a for a in m.athletes if a.milesplit_id == 501]
        self.assertEqual(len(recs), 2)
        self.assertEqual({a.team_id for a in recs}, {"t1", "t2"})
        self.assertEqual(recs[0].universal_id, recs[1].universal_id)
        recs[0].bib, recs[1].bib = "101", "201"
        self.assertEqual(m.validate(), [])
        self.assertEqual(m.athlete_lookup("201")["school"], "Valley Club")
        self.assertEqual([r.event for r, _ in m.entries_for(recs[0].id)], [1])
        self.assertEqual([r.event for r, _ in m.entries_for(recs[1].id)], [2])
        rep = refresh_milesplit(m, self.pull())
        self.assertTrue(rep.empty, rep.summary())
        self.assertEqual([a.bib for a in m.athletes if a.milesplit_id == 501], ["101", "201"])

    def test_same_team_listed_twice_is_one_athlete(self):
        p = self.pull()
        p[0]["roster"].append(ms_athlete(501, "Avery", "Birch", entries=[ms_entry("JV Girls")]))
        m, _ = import_milesplit(p, info())
        a = find(m, 501, "t1")
        self.assertEqual(sorted(r.event for r, _ in m.entries_for(a.id)), [1, 2])


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.old = fixture()
        self.meet, _ = import_milesplit(copy.deepcopy(self.old), info())
        m = self.meet
        for i, a in enumerate(m.athletes, 1):        # operator work
            a.bib = str(1000 + i)
        m.events[0].name = "Girls Varsity 5K (Championship)"
        m.race(1).name = "Girls Varsity"
        m.race(1).order, m.race(4).order = 4, 1
        self.exh = m.race(2).entries[1]                    # not the one scratched upstream
        self.exh.status = "exhibition"
        m.athletes.append(Athlete(m.new_athlete_id(), "Walk", "Up", "F", team_id="t1",
                                  bib="999", source="local"))
        m.race(1).entries.append(Entry(m.athletes[-1].id, 1))

        self.new = copy.deepcopy(self.old)
        ridge = self.new[0]["roster"]
        self.gone_ms = ridge.pop(1)["athleteMileSplitId"]          # dropped upstream
        ridge.append(ms_athlete(9001999, "Quinn", "Holt", "m",
                                entries=[ms_entry("Varsity Boys", "m")]))
        ridge[0]["athleteLastName"] = "Dunmore-Elwood"             # renamed upstream
        ridge[0]["entries"][0]["entryStatus"] = -1                 # scratched upstream
        self.new[1]["roster"][0]["entries"].append(ms_entry("JV Girls", "f"))   # 2nd entry
        self.new.append(ms_team(900099, "Pine High School", "PIN",
                                [ms_athlete(9099001, "Sage", "Voss")]))

    def test_refresh_keeps_local_work_and_reports(self):
        m = self.meet
        bibs = {a.id: a.bib for a in m.athletes}
        rep = refresh_milesplit(m, self.new)
        self.assertEqual(m.validate(), [])
        # local work survives
        self.assertEqual({a.id: a.bib for a in m.athletes if a.id in bibs}, bibs)
        self.assertEqual(m.events[0].name, "Girls Varsity 5K (Championship)")
        self.assertEqual((m.race(1).name, m.race(1).order, m.race(4).order),
                         ("Girls Varsity", 4, 1))
        self.assertEqual(self.exh.status, "exhibition")
        self.assertIsNotNone(m.athlete_by_bib("999"))
        # nothing deleted
        self.assertEqual(len(m.athletes), 341 + 2)
        self.assertTrue(any(a.milesplit_id == self.gone_ms for a in m.athletes))
        # report
        added = [c.text for c in rep.added]
        self.assertIn("team Pine High School (PIN)", added)
        self.assertIn("Quinn Holt (Ridge): Boys 5k Run CC Varsity", added)
        self.assertIn("Sage Voss (Pine): Girls Varsity 5K (Championship)", added)
        self.assertEqual([c.text for c in rep.added if c.kind == "entry"],
                         ["Tatum Garrow (Valley): Girls 5k Run CC JV"])
        changed = "\n".join(c.text for c in rep.changed)
        self.assertIn("last 'Dunmore' -> 'Dunmore-Elwood'", changed)
        self.assertIn("Boys 5k Run CC Varsity entered -> scratched", changed)
        a = find(m, 9001001)
        self.assertEqual(a.last, "Dunmore-Elwood")
        self.assertEqual(m.entries_for(a.id)[0][1].status, "scratched")
        gone = [c for c in rep.gone]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0].kind, "athlete")
        self.assertIn("not in the MileSplit entries (kept, 1 entry)", gone[0].text)
        va = find(m, 9002001)
        self.assertEqual(sorted(r.event for r, _ in m.entries_for(va.id)), [1, 3])

    def test_second_refresh_reports_nothing_new(self):
        refresh_milesplit(self.meet, self.new)
        rep = refresh_milesplit(self.meet, self.new)
        self.assertEqual((rep.added, rep.changed), ([], []))
        self.assertEqual(len(rep.gone), 1)          # still gone upstream, still kept

    def test_upstream_scratch_vs_operator_exhibition_is_a_conflict(self):
        a = find(self.meet, 9001001)
        self.meet.entries_for(a.id)[0][1].status = "exhibition"
        for _ in range(2):                          # stays open until the operator decides
            rep = refresh_milesplit(self.meet, self.new)
            self.assertEqual(self.meet.entries_for(a.id)[0][1].status, "exhibition")
            self.assertIn("is scratched upstream, kept local exhibition",
                          rep.conflicts[0].text)

    def test_operator_scratch_survives_upstream_entered(self):
        a = find(self.meet, 9001001)
        self.meet.entries_for(a.id)[0][1].status = "scratched"
        rep = refresh_milesplit(self.meet, self.old)
        self.assertEqual(self.meet.entries_for(a.id)[0][1].status, "scratched")
        self.assertEqual(rep.changed, [])

    def test_new_event_upstream_numbered_after_existing(self):
        self.new[0]["roster"].append(ms_athlete(9001888, "Tatum", "Upton", "f",
                                                entries=[ms_entry("Middle School Girls")]))
        rep = refresh_milesplit(self.meet, self.new)
        ev = self.meet.event(5)
        self.assertEqual(ev.name, "Girls 5k Run CC Middle School")
        self.assertEqual(self.meet.race(5).order, 5)
        self.assertIn("event 5 Girls 5k Run CC Middle School", [c.text for c in rep.added])
        self.assertEqual(self.meet.validate(), [])


class TestRefreshThreeWay(unittest.TestCase):
    """With the previous pull, upstream changes apply where local still
    equals the last pull; operator edits are kept."""

    def setUp(self):
        self.old = fixture()
        self.meet, _ = import_milesplit(copy.deepcopy(self.old), info())
        self.jv = find(self.meet, 9001002)            # Ridge, JV Boys (event 4)
        self.var = find(self.meet, 9001001)           # Ridge, Varsity Boys (event 2)

    def move_upstream(self, data, ms_id, div):
        for t in data:
            for a in t["roster"]:
                if a["athleteMileSplitId"] == ms_id:
                    a["entries"][0]["entryDivisionName"] = div

    def test_upstream_division_move_applies(self):
        new = copy.deepcopy(self.old)
        self.move_upstream(new, 9001002, "Varsity Boys")
        rep = refresh_milesplit(self.meet, new, previous=self.old)
        self.assertEqual([r.event for r, _ in self.meet.entries_for(self.jv.id)], [2])
        self.assertIn("moved Boys 5k Run CC JV -> Boys 5k Run CC Varsity",
                      rep.changed[0].text)
        self.assertEqual(rep.gone, [])
        self.assertEqual(self.meet.validate(), [])

    def test_upstream_move_without_previous_is_a_conflict(self):
        new = copy.deepcopy(self.old)
        self.move_upstream(new, 9001002, "Varsity Boys")
        rep = refresh_milesplit(self.meet, new)
        self.assertEqual([r.event for r, _ in self.meet.entries_for(self.jv.id)], [4])
        self.assertEqual(len(rep.conflicts), 1)
        self.assertIn("(kept local)", rep.conflicts[0].text)

    def test_local_move_survives_refresh(self):
        r4, en = self.meet.entries_for(self.jv.id)[0]
        r4.entries.remove(en)
        en.event = 2
        self.meet.race(2).entries.append(en)
        for prev in (self.old, None):
            rep = refresh_milesplit(self.meet, self.old, previous=prev)
            self.assertEqual([r.event for r, _ in self.meet.entries_for(self.jv.id)], [2])
        self.assertEqual(len(rep.conflicts), 1)          # no previous: asked, not applied

    def test_operator_name_edit_kept_upstream_change_conflicts(self):
        self.var.first = "Averie"                       # operator fixed a typo
        rep = refresh_milesplit(self.meet, self.old, previous=self.old)
        self.assertTrue(rep.empty)
        self.assertEqual(self.var.first, "Averie")
        new = copy.deepcopy(self.old)
        new[0]["roster"][0]["athleteFirstName"] = "Avry"
        rep = refresh_milesplit(self.meet, new, previous=self.old)
        self.assertEqual(self.var.first, "Averie")
        self.assertFalse(rep.changed[0].applied)
        # untouched athlete: upstream change applies
        new[0]["roster"][2]["athleteLastName"] = "Lomax-Pryor"
        refresh_milesplit(self.meet, new, previous=self.old)
        self.assertEqual(find(self.meet, 9001003).last, "Lomax-Pryor")

    def test_upstream_unscratch_applies_only_when_local_matches(self):
        old = copy.deepcopy(self.old)
        old[0]["roster"][0]["entries"][0]["entryStatus"] = -1
        meet, _ = import_milesplit(old, info())
        a = find(meet, 9001001)
        rep = refresh_milesplit(meet, self.old, previous=old)
        self.assertEqual(meet.entries_for(a.id)[0][1].status, "entered")
        self.assertIn("scratched -> entered", rep.changed[0].text)

    def test_upstream_drop_reported_gone_operator_removal_respected(self):
        new = copy.deepcopy(self.old)
        dropped = new[0]["roster"].pop(2)["athleteMileSplitId"]
        rep = refresh_milesplit(self.meet, new, previous=self.old)
        self.assertEqual([c.athlete_id for c in rep.gone], [find(self.meet, dropped).id])
        # operator removed an entry that is still upstream: not re-added
        r, en = self.meet.entries_for(self.var.id)[0]
        r.entries.remove(en)
        rep = refresh_milesplit(self.meet, self.old, previous=self.old)
        self.assertEqual(self.meet.entries_for(self.var.id), [])
        self.assertEqual(rep.added, [])

    def test_operator_team_move_is_followed(self):
        self.jv.team_id = "t2"
        rep = refresh_milesplit(self.meet, self.old, previous=self.old)
        self.assertTrue(rep.empty, rep.summary())
        self.assertEqual(len([a for a in self.meet.athletes if a.milesplit_id == 9001002]), 1)


HYTEK = """\
I;Birch;Avery;;F;01/02/2010;RID;Ridge High School;16;11;;;;;;;;;;;;R1;101;
I;Calder;Blake;;M;;RID;Ridge High School;;SR;;;;;;;;;;;;R2;;
I;Dunmore;Casey;;f;;RID;Ridge High School;;9;;;;;;;;;;;;R3;;
I;Elwood;Drew;;M;;VAL;Valley High School;;12;;;;;;;;;;;;;102;
D;Birch;Avery;;F;01/02/2010;RID;Ridge High School;16;11;5000;19:40.00;M;;101;;D;;;
D;Calder;Blake;;M;;RID;Ridge High School;;SR;5000;;M;JV;;;S;;;
D;Elwood;Drew;;M;;VAL;Valley High School;;12;2MILE;;E;;102;;;;;
D;Fairley;Emery;;F;;;;;10;5000;;M;;;;;;;
H;Some Meet;10/10/2026
"""


class TestHytek(unittest.TestCase):
    def test_import(self):
        m, rep = import_hytek(HYTEK, info())
        self.assertEqual(m.validate(), [])
        self.assertEqual([(a.first, a.last, a.bib, a.grade_text, a.source) for a in m.athletes],
                         [("Avery", "Birch", "101", "11", "hytek"),
                          ("Blake", "Calder", "", "SR", "hytek"),
                          ("Drew", "Elwood", "102", "12", "hytek"),
                          ("Emery", "Fairley", "", "10", "hytek")])
        self.assertEqual(m.athletes[0].dob, "2010-01-02")
        self.assertIsNone(m.athletes[3].team_id)                # no team = unattached
        self.assertEqual([(t.code, t.name) for t in m.teams],
                         [("RID", "Ridge High School"), ("VAL", "Valley High School")])
        self.assertEqual([e.name for e in m.events],
                         ["Girls 5k Run CC", "Boys 2 Mile Run CC", "Boys 5k Run CC JV"])
        calder = m.athletes[1]
        self.assertEqual(m.entries_for(calder.id)[0][1].status, "scratched")
        self.assertEqual(m.race(1).entries[0].seed, "19:40.00")
        self.assertIn("skipped 1 roster-only athletes (lowercase gender, no entries)", rep.notes)
        self.assertIn("ignored 1 lines that are not I or D records", rep.notes)

    def test_roster_only_file_and_cp1252(self):
        text = "I;Birch;Ávery;;f;;RID;Ridge;;9;;;;;;;;;;;;;;\n"
        pull = parse_hytek(text.encode("cp1252"))
        self.assertEqual([a.first for _, a in pull.athletes()], ["Ávery"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "entries.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(HYTEK)
            m, _ = import_hytek(p, info(), include_roster_only=True)
        self.assertEqual(len(m.athletes), 5)

    def test_refresh_hytek_matches_by_name(self):
        from rfid_finishlynx.meet.importers import merge
        m, _ = import_hytek(HYTEK, info())
        m.athletes[1].bib = "555"
        rep = merge(m, parse_hytek(HYTEK.replace("D;Fairley", "X;Fairley")))
        self.assertEqual(m.athletes[1].bib, "555")
        self.assertEqual([c.text for c in rep.gone],
                         ["Emery Fairley (unattached) is not in the Hy-Tek file (kept, 1 entry)"])
        self.assertEqual(rep.added, [])


CSV_TEXT = """\
First Name,Last Name,Sex,Grade,School,Bib #,Division
Avery,Birch,F,2028,Ridge High School,201,Varsity Girls
Blake,Calder,Boy,10,Ridge High School,,JV Boys
Casey,Dunmore,?,9,Ridge High School,,
Drew,Elwood,M,Senior,Valley High School,201,Varsity Boys
Emery,Fairley,female,11,,,
"""


class TestCsv(unittest.TestCase):
    def test_import(self):
        m, rep = import_csv(CSV_TEXT, info())
        self.assertEqual(m.validate(), [])
        self.assertEqual([(a.first, a.gender, a.grad_year, a.grade_text, a.bib, a.source)
                          for a in m.athletes],
                         [("Avery", "F", 2028, "", "201", "csv"),
                          ("Blake", "M", None, "10", "", "csv"),
                          ("Drew", "M", None, "Senior", "", "csv"),
                          ("Emery", "F", None, "11", "", "csv")])
        self.assertEqual(m.grade(m.athletes[0]), "11")
        self.assertIsNone(m.athletes[3].team_id)
        self.assertEqual([e.name for e in m.events],
                         ["Girls Varsity", "Boys Varsity", "Boys JV"])
        self.assertEqual(m.entries_for(m.athletes[3].id), [])
        notes = "\n".join(rep.notes)
        self.assertIn("row 4: skipped Casey Dunmore", notes)
        self.assertIn("bib 201 already used", notes)

    def test_name_column_semicolon_and_event_code(self):
        text = "Name;Gender;Team;Event;Division\nBirch, Avery;F;Ridge;5000;Varsity Girls\n" \
               "Blake Calder;M;Ridge;2 Mile;\n"
        m, _ = import_csv(text, info())
        self.assertEqual([(a.first, a.last) for a in m.athletes],
                         [("Avery", "Birch"), ("Blake", "Calder")])
        self.assertEqual([e.name for e in m.events],
                         ["Girls 5k Run CC Varsity", "Boys 2 Mile Run CC"])
        self.assertEqual(m.validate(), [])

    def test_missing_columns_is_an_error(self):
        with self.assertRaises(ValueError):
            import_csv("Team,Bib\nRidge,1\n", info())


class TestParseOnly(unittest.TestCase):
    def test_parse_milesplit_folds_duplicates(self):
        p = parse_milesplit([ms_team(1, "Ridge", "RID", [ms_athlete(11, "Avery", "Birch")]),
                             ms_team(1, "Ridge", "RID", [ms_athlete(11, "Avery", "Birch")])])
        self.assertEqual(len(p.teams), 1)
        self.assertEqual(len(list(p.athletes())), 1)
        self.assertEqual(len(p.teams[0].athletes[0].entries), 1)


if __name__ == "__main__":
    unittest.main()


class TestFullRosters(unittest.TestCase):
    """Scope "entered + full rosters": the attending teams' rostered
    athletes come in without entries, ids attached (2026-10-03)."""

    def setUp(self):
        import copy
        from rfid_finishlynx.meet.model import MeetInfo
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "milesplit",
                               "entries-xc.json"), encoding="utf-8") as f:
            self.entries = json.load(f)
        # team-rosters: the first 3 teams' entered athletes (no entries key,
        # as a roster pull would have them) plus 2 rostered-only each, and
        # a team that is not attending
        self.rosters = []
        for ti, t in enumerate(self.entries[:3]):
            roster = []
            for a in t["roster"]:
                a = copy.deepcopy(a)
                a.pop("entries", None)
                roster.append(a)
            for k in range(2):
                roster.append({"athleteFirstName": f"Roster{k}", "athleteLastName": f"Only{ti}",
                               "athleteGender": "f" if k else "m", "athleteYear": "2029",
                               "athleteMileSplitId": 8800000 + ti * 10 + k,
                               "athleteMileSplitUniversalId": f"R{ti}{k}"})
            self.rosters.append(dict(t, roster=roster))
        self.rosters.append({"teamName": "Not Attending HS", "teamMileSplitId": 999999,
                             "roster": [{"athleteFirstName": "No", "athleteLastName": "Show",
                                         "athleteGender": "m", "athleteYear": "2028",
                                         "athleteMileSplitId": 8899999}]})
        self.info = lambda: MeetInfo(id="m", name="Roster Test", date="2026-10-10")

    def test_entered_only_by_default(self):
        meet, _ = importers.import_milesplit(self.entries, self.info())
        self.assertFalse(meet.meet.full_rosters)
        self.assertTrue(all(meet.entered(a.id) for a in meet.athletes))

    def test_rosters_add_not_entered_athletes_with_ids(self):
        plain, _ = importers.import_milesplit(self.entries, self.info())
        meet, rep = importers.import_milesplit(self.entries, self.info(), rosters=self.rosters)
        self.assertTrue(meet.meet.full_rosters)
        self.assertEqual(meet.validate(), [])
        self.assertEqual(len(meet.athletes), len(plain.athletes) + 6)   # not the non-attending team
        extra = [a for a in meet.athletes if not meet.entered(a.id)]
        self.assertEqual(len(extra), 6)
        self.assertTrue(all(a.milesplit_id and a.grad_year == 2029 for a in extra))
        self.assertIsNone(next((t for t in meet.teams if t.name == "Not Attending HS"), None))
        self.assertTrue(any("rosters: 6 athletes" in n for n in rep.notes))
        # entries are unchanged by the roster (no duplicate athletes, same entries)
        self.assertEqual(sum(len(r.entries) for r in meet.races),
                         sum(len(r.entries) for r in plain.races))

    def test_refresh_needs_rosters_and_is_quiet_when_nothing_changed(self):
        meet, _ = importers.import_milesplit(self.entries, self.info(), rosters=self.rosters)
        with self.assertRaises(ValueError):
            importers.refresh_milesplit(meet, self.entries)
        rep = importers.refresh_milesplit(meet, self.entries, previous=self.entries,
                                          rosters=self.rosters, previous_rosters=self.rosters)
        self.assertTrue(rep.empty, rep.summary())

    def test_walk_up_from_roster_is_one_entry_and_bibs_skip_the_rest(self):
        from rfid_finishlynx.meet import bibs, lynx
        from rfid_finishlynx.meet.model import Entry
        meet, _ = importers.import_milesplit(self.entries, self.info(), rosters=self.rosters)
        bibs.set_ranges(meet, "100-")
        bibs.assign(meet, entered_only=True)
        extra = [a for a in meet.athletes if not meet.entered(a.id)]
        self.assertTrue(all(not a.bib for a in extra))
        ppl = lynx.ppl_text(meet)
        self.assertNotIn("Only0", ppl)                      # no bib, not entered: left out
        walk = next(a for a in extra if a.gender == "F")
        race = next(r for r in meet.races if meet.event(r.event).gender == "F")
        race.entries.append(Entry(walk.id, race.event))
        bibs.assign(meet, entered_only=True)                # numbers just the walk-up
        self.assertTrue(walk.bib)
        self.assertIn(f",{walk.bib},", lynx.evt_text(meet))
        self.assertEqual(meet.validate(), [])


class TestRealRosterShape(unittest.TestCase):
    """team-rosters as MileSplit really sends it (2026-10-05, Crescent pull):
    {team id: {universal id: {"athlete": {...}}}}. It used to add nobody."""

    def setUp(self):
        from rfid_finishlynx.meet.importers import roster_teams
        from rfid_finishlynx.meet.model import MeetInfo
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "milesplit",
                               "entries-xc.json"), encoding="utf-8") as f:
            self.entries = json.load(f)
        t = self.entries[0]
        entered = t["roster"][0]

        def rec(i, first, last, gender, grad, deleted=0):
            return {"athlete_id": str(i), "team_id": t["teamMileSplitId"], "season_year": 2026,
                    "athlete": {"id": str(i), "first_name": first, "last_name": last, "gender": gender,
                                "hs_graduation_year": grad, "birth_date": None, "is_deleted": deleted}}
        raw = {str(t["teamMileSplitId"]): {
            entered["athleteMileSplitUniversalId"] or "E": rec(entered["athleteMileSplitId"], entered["athleteFirstName"],
                                                               entered["athleteLastName"], entered["athleteGender"],
                                                               entered["athleteYear"]),
            "NEW*ONE": rec(7700001, "Walk", "Up", "f", 2029),
            "OLD*GRAD": rec(7700002, "Past", "Senior", "m", 2026),
            "OLD*2010": rec(7700003, "Long", "Gone", "m", 2010),
            "DEL*ETED": rec(7700004, "Deleted", "Profile", "f", 2028, deleted=1)},
            "999999": {"X*Y": rec(7700005, "Not", "Attending", "m", 2028)}}
        self.rosters = roster_teams(raw)
        self.info = MeetInfo(id="m", name="Crescent", date="2026-10-08")

    def test_only_current_not_entered_athletes_are_added(self):
        meet, rep = importers.import_milesplit(self.entries, self.info, rosters=self.rosters)
        extra = [a for a in meet.athletes if not meet.entered(a.id)]
        self.assertEqual([(a.first, a.last, a.gender, a.grad_year, a.milesplit_id) for a in extra],
                         [("Walk", "Up", "F", 2029, 7700001)])
        self.assertTrue(any("1 athletes not entered added; 2 graduated athletes left out" in n
                            for n in rep.notes), rep.notes)
        self.assertEqual(meet.validate(), [])

    def test_class_floor(self):
        self.assertEqual(importers.class_floor("2026-10-08"), 2027)
        self.assertEqual(importers.class_floor("2027-04-10"), 2027)
        self.assertIsNone(importers.class_floor(""))

    def test_walk_up_candidates_come_from_the_real_shape(self):
        from rfid_finishlynx.meet import idmatch
        meet, _ = importers.import_milesplit(self.entries, self.info)
        pool = idmatch.CandidatePool.from_milesplit(meet, entries=self.entries, rosters=self.rosters)
        self.assertTrue(any(c.milesplit_id == 7700001 for c in pool._by_key.values()))


class TestGarbledNames(unittest.TestCase):
    """UTF-8 names that were read as Windows-1252 upstream are repaired on
    import (2026-10-08: MileSplit's Crescent roster had "Nyâ€™An")."""

    def test_fix_text(self):
        fix = importers.fix_text
        for bad, good in (("Nyâ€™An", "Ny’An"), ("JosÃ©", "José"),
                          ("MuÃ±oz", "Muñoz"), ("Ã\u0081lvarez", "Álvarez"),
                          ("JÃƒÂ©rÃƒÂ´me", "Jérôme")):   # twice garbled
            self.assertEqual(fix(bad), good, bad)
        for ok in ("José", "Zoë", "SÃO", "Ã", "O'Brien", "Ny’Angel", "", None, 12):
            self.assertEqual(fix(ok), ok, ok)

    def test_milesplit_csv_and_team_names(self):
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "milesplit",
                               "entries-xc.json"), encoding="utf-8") as f:
            entries = json.load(f)
        entries[0]["teamName"] = "Escuela San JosÃ©"
        entries[0]["roster"][0]["athleteFirstName"] = "Nyâ€™An"
        meet, _ = importers.import_milesplit(entries, MeetInfo(id="m", name="x", date="2026-10-10"))
        self.assertIn("Ny’An", [a.first for a in meet.athletes])
        self.assertIn("Escuela San José", [t.name for t in meet.teams])
        pull = importers.parse_csv("first,last,gender,team\nRenÃ©e,DÃ­az,F,Ridge\n".encode("utf-8"))
        _, a = next(pull.athletes())
        self.assertEqual((a.first, a.last), ("Renée", "Díaz"))
