"""The meet modules together, the way race day uses them: MileSplit entries
-> meet -> bibs -> start lists -> the console's own .evt reader and a real
RaceManager -> reads at the finish -> freeze -> printed results; and a
walk-up through idmatch. Each module has its own tests; this one catches
contract drift between them (2026-10-03)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.meet import (bibs, exports, idmatch, importers, lynx, milesplit,
                                  printing, scoring)
from rfid_finishlynx.meet.model import Athlete, MeetInfo, MeetStore
from rfid_finishlynx.races import RaceManager
from rfid_finishlynx.web import create_missing_panes, evt_race_list

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")
GUN = datetime(2026, 10, 10, 9, 0, 0)


def load_entries():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class TestMeetEndToEnd(unittest.TestCase):

    def setUp(self):
        self.entries = load_entries()
        self.meet, report = importers.import_milesplit(
            self.entries, MeetInfo(id="2026-10-10-e2e", name="E2E Invitational",
                                   date="2026-10-10", host="Ridge HS"))
        self.assertEqual(self.meet.validate(), [])
        bibs.set_ranges(self.meet, "100-")
        out = bibs.assign(self.meet)
        self.assertEqual(out.unassigned, 0)
        self.assertEqual(self.meet.validate(), [])

    def test_race_day_chain(self):
        meet = self.meet
        with tempfile.TemporaryDirectory() as lynx_dir:
            written = lynx.write_start_lists(meet, lynx_dir)
            self.assertFalse(written["errors"])
            with open(os.path.join(lynx_dir, "lynx.evt"), encoding="cp1252") as f:
                evt = evt_race_list(f.read())

        # The console reads our .evt exactly as it reads MeetPro's
        self.assertEqual([(r["event"], r["round"], r["heat"]) for r in evt],
                         [r.key for r in meet.races_in_order()])

        mgr = RaceManager(athlete_lookup=meet.athlete_lookup, chute_reader="CHUTE",
                          finish_readers={"FIN"}, point_labels={"S1": "Mile 1"})
        create_missing_panes(mgr, evt)
        race = meet.races_in_order()[0]
        key = mgr.find_by_event(race.event)
        entered = [e for e in race.entries if e.status != "scratched"]
        bib_of = {a.id: a.bib for a in meet.athletes}
        self.assertEqual(mgr.races[key].entries, {bib_of[e.athlete_id] for e in entered})

        mgr.set_status(key, "racing")
        mgr.set_gun(key, GUN)
        finishers = entered[:-2]                      # the last two never show
        for i, e in enumerate(finishers):
            mgr.ingest(bib_of[e.athlete_id], GUN + timedelta(minutes=5, seconds=40 + 2 * i), "S1")
        for i, e in enumerate(finishers):
            mgr.ingest(bib_of[e.athlete_id], GUN + timedelta(minutes=17, seconds=7 * i), "FIN")

        rm = mgr.results(key)
        self.assertEqual(len(rm["finishers"]), len(finishers))
        warnings = scoring.freeze(meet, race, rm, now=datetime(2026, 10, 10, 9, 40))
        self.assertEqual(scoring.unresolved(warnings), [])
        res = scoring.official_results(race)
        self.assertIsNotNone(res)
        self.assertEqual(sum(1 for f in res["finishers"] if f["status"] == "finished"),
                         len(finishers))
        self.assertEqual(sum(1 for f in res["finishers"] if f["status"] == "DNS"), 2)
        self.assertEqual(res["split_labels"], ["Mile 1"])     # splits reach the printouts
        first = res["finishers"][0]
        self.assertEqual([sp["label"] for sp in first["splits"]], ["Mile 1"])
        self.assertAlmostEqual(first["splits"][0]["elapsed"], 340.0, places=2)
        teams = res["teams"][str(race.event)]
        self.assertTrue(any(t["complete"] for t in teams))

        text = printing.results_text(meet, race, printed_at=datetime(2026, 10, 10, 9, 41))
        self.assertIn("E2E Invitational", text)
        self.assertIn("Team Scores", text)
        self.assertIn(meet.event(race.event).name, text)
        self.assertTrue(printing.to_pdf(text, title="Results").startswith(b"%PDF"))

        with tempfile.TemporaryDirectory() as root:
            store = MeetStore(root)
            store.save(meet)
            store.save_pull(meet.meet.id, "entries", self.entries)
            again = store.load(meet.meet.id)
            self.assertEqual(again.validate(), [])
            self.assertEqual(scoring.official_results(again.race(*race.key)), res)
            report = importers.refresh_milesplit(again, self.entries,
                                                 previous=store.load_pull(meet.meet.id, "entries"))
            self.assertTrue(report.empty)             # same pull again: nothing to do

        # Send: the mpResults upload and a Live push, built offline (no network)
        meet.meet.milesplit.meet_id = 761226
        meet.meet.milesplit.tc_id = 42
        payload = exports.mp_results(meet, races=[race])
        self.assertEqual(exports.mp_results_problems(meet, [race], payload=payload), [])
        self.assertTrue(exports.id_coverage(payload)["ok"])
        live = milesplit.live_race_payload(meet, race)
        self.assertEqual(live["race"], list(race.key))
        live_bibs = {e["id"] for e in live["entries"]}
        self.assertTrue({bib_of[e.athlete_id] for e in finishers} <= {str(b) for b in live_bibs})

        # Reopened: no longer official, so it can't be uploaded as final
        scoring.reopen(meet, race)
        self.assertIsNone(scoring.official_results(race))
        self.assertTrue(exports.mp_results_problems(meet, [race]))

    def test_walk_up_gets_milesplit_id_only_on_exact_match(self):
        meet = self.meet
        src = meet.athletes[0]
        # a walk-up typed exactly as MileSplit has a teammate who is NOT in the
        # meet: take a real one out of the meet and add it back as local
        meet.athletes.remove(src)
        for r in meet.races:
            r.entries = [e for e in r.entries if e.athlete_id != src.id]
        walk = Athlete(meet.new_athlete_id(), src.first, src.last, src.gender,
                       team_id=src.team_id, grad_year=src.grad_year, source="local")
        meet.athletes.append(walk)
        typo = Athlete(meet.new_athlete_id(), src.first + "e", src.last, src.gender,
                       team_id=src.team_id, grad_year=src.grad_year, source="local")
        meet.athletes.append(typo)
        pool = idmatch.CandidatePool.from_milesplit(meet, entries=self.entries)
        report = idmatch.match_meet(meet, pool)
        self.assertIn(walk.id, report.auto)
        self.assertEqual(walk.milesplit_id, src.milesplit_id)
        self.assertNotIn(typo.id, report.auto)
        self.assertIsNone(typo.milesplit_id)
        self.assertEqual(meet.validate(), [])


if __name__ == "__main__":
    unittest.main()
