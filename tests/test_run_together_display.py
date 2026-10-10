"""Two races off one gun, on screen and on paper while they run (2026-10-05,
user): the race pane tags each runner with their own race and its place in
it; the Results page lists each race on its own (counts per division); and
the results of a race not marked final print as they stand, titled NOT
MARKED COMPLETE. python -m unittest tests.test_run_together_display"""

import io
from datetime import timedelta

from rfid_finishlynx import web
from tests.test_meet_api import GUN, MeetApiBase
from tests.test_meet_pdf import all_text
from tests.test_run_together_meet import roster_csv


class RunTogetherLive(MeetApiBase):

    def setUp(self):
        super().setUp()
        r = self.c.post("/api/meet/import/file", data={
            "file": (io.BytesIO(roster_csv().encode()), "jv.csv"), "name": "JV", "date": "2026-10-10"},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.young = next(k for k, v in races.items() if "7-9" in v["name"])
        old = next(k for k, v in races.items() if "10-12" in v["name"])
        self.post(f"/api/meet/races/{self.young}/run-together", {"races": [old]})
        meet = self.svc.meet
        key = tuple(map(int, self.young.split("-")))
        self.race = meet.race(*key)
        self.pane = self.make_panes()[key]
        for spec in web.evt_race_list(self.rfid_file("lynx.evt")):   # what the .evt monitor does
            if (spec["event"], spec["round"], spec["heat"]) == key:
                self.mgr.set_entries(self.pane, spec["bibs"])
        self.mgr.set_gun(self.pane, GUN)
        # alternate the divisions: 3xx (7-9) and 5xx (10-12)
        bibs = sorted(meet.athlete(e.athlete_id).bib for e in self.race.entries)
        self.y = [b for b in bibs if b.startswith("3")][:3]
        self.o = [b for b in bibs if b.startswith("5")][:3]
        for i, b in enumerate([self.o[0], self.y[0], self.o[1], self.y[1], self.y[2]]):
            self.mgr.ingest(b, GUN + timedelta(minutes=17, seconds=10 * i), "FINISH")
        self.ev = {b: e.event for e in self.race.entries
                   for b in [meet.athlete(e.athlete_id).bib]}

    def test_pane_tags_each_runner_with_its_race_and_place(self):
        dv = web._races_payload()["divisions"][str(self.pane)]
        self.assertEqual(sorted(dv["events"].values()), ["10-12", "7-9"])
        self.assertEqual([dv["bibs"][b][1] for b in self.o[:2]], [1, 2])
        self.assertEqual([dv["bibs"][b][1] for b in self.y], [1, 2, 3])
        self.assertEqual(dv["bibs"][self.y[0]][0], self.ev[self.y[0]])
        self.assertIsNone(dv["bibs"][self.o[2]][1])               # entered, not finished yet
        rows = self.mgr.results(self.pane)["finishers"]
        self.assertEqual([r["place"] for r in rows], [1, 2, 3, 4, 5])  # overall stays

    def test_results_page_counts_each_division(self):
        race = next(r for r in self.get("/api/meet/races").get_json()["races"] if r["rk"] == self.young)
        by = {d["short"]: d for d in race["divisions"]}
        self.assertEqual((by["7-9"]["finished"], by["10-12"]["finished"]), (3, 2))
        self.assertEqual((by["7-9"]["entries"], by["10-12"]["entries"]), (39, 48))

    def test_current_results_print_not_marked_complete(self):
        text = self.get(f"/api/meet/print/results.txt?race={self.young}").get_data(as_text=True)
        self.assertIn("NOT MARKED COMPLETE", text.split("\n\n", 1)[0])
        for b in self.y + self.o[:2]:
            self.assertIn(b, text)
        self.assertNotIn("Results not final.", text)
        pdf = self.get(f"/api/meet/print/results.pdf?race={self.young}&events="
                       f"{self.ev[self.y[0]]}").data
        words = " ".join(t for page in all_text(self, pdf) for t in page)
        self.assertIn("NOT MARKED COMPLETE", words)
        self.assertNotIn("OFFICIAL MEET REPORT", words)
        self.assertIn(self.y[0], words)
        self.assertNotIn(self.o[0], words)                        # one division only

    def test_final_race_prints_as_before(self):
        for e in self.race.entries:
            b = self.svc.meet.athlete(e.athlete_id).bib
            if b not in self.y + self.o[:2]:
                self.post(f"/api/meet/races/{self.young}/marks", {"bib": b, "mark": "DNS"})
        self.post(f"/api/meet/races/{self.young}/final")
        text = self.get(f"/api/meet/print/results.txt?race={self.young}").get_data(as_text=True)
        self.assertNotIn("NOT MARKED COMPLETE", text)
        pdf = self.get(f"/api/meet/print/results.pdf?race={self.young}").data
        words = " ".join(t for page in all_text(self, pdf) for t in page)
        self.assertIn("Final Results", words)
        self.assertIn("OFFICIAL MEET REPORT", words)

    def test_several_races_print_together_live_ones_marked(self):
        """The Results page's one print box: several races (or all) in one
        file; a race not marked final is in it, as it stands (2026-10-06)."""
        text = self.get(f"/api/meet/print/meet-results.txt?races={self.young}").get_data(as_text=True)
        self.assertIn("*** NOT MARKED COMPLETE ***", text)
        for b in self.y + self.o[:2]:
            self.assertIn(b, text)
        text_all = self.get("/api/meet/print/meet-results.txt").get_data(as_text=True)
        self.assertIn("*** NOT MARKED COMPLETE ***", text_all)
        pdf = self.get(f"/api/meet/print/meet-results.pdf?races={self.young}&events={self.ev[self.o[0]]}").data
        words = " ".join(t for page in all_text(self, pdf) for t in page)
        self.assertIn("NOT MARKED COMPLETE", words)
        self.assertNotIn("OFFICIAL MEET REPORT", words)
        self.assertIn(self.o[0], words)
        self.assertNotIn(self.y[0], words)
        self.assertTrue(self.get(f"/api/meet/print/finish-line.pdf?races={self.young}").data.startswith(b"%PDF"))
