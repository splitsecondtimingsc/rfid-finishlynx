"""Meet lifecycle fixes from the break test (2026-10-13; ported from the
camera branch without its standalone mode).

- New / Open / Open file never take over another meet's race cards: 409
  while race cards are in use or reads count; Open on the open meet is a
  no-op; a detached meet's own cards stay; leftovers (empty cards, a stray
  read after Close) are archived away.
- A race marked final before it was run (everyone DNS) can be reopened.
- meet.json is saved durably with a .bak; a damaged one is restored from it
  and the console says so; both unreadable: /api/meet and /api/status say so.
- Re-finalize refuses to turn finishers into flagged runners unless forced,
  and Undo takes it back.
- A finisher's team / gender changed after final makes the race stale.
- app._race_start_time reads the meet's date: scheduled check-in opens.
- Close meet with no meet open but race cards belonging to one (detached, or
  its file unreadable) is refused.
- POST /api/meet/bibs/assign with a body of the wrong shape is a 400.
python -m pytest tests/test_meet_lifecycle.py
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx.app import Application
from rfid_finishlynx.meet.model import MeetStore
from rfid_finishlynx.meet_service import MeetService
from tests.test_meet_api import GUN, MeetApiBase


class Raced(MeetApiBase):
    """Test XC open, race 1-1-1 run (gun + 5 finishers)."""

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.assign_all()
        self.panes = self.make_panes()
        self.pane = self.panes[(1, 1, 1)]
        self.meet_id = self.svc.meet.meet.id
        race = self.svc.meet.race(1, 1, 1)
        self.mgr.set_gun(self.pane, GUN)
        for i, e in enumerate(race.entries[:5]):
            self.mgr.ingest(self.svc.meet.athlete(e.athlete_id).bib,
                            GUN + timedelta(minutes=18, seconds=i), "FINISH")

    def finished(self, rk="1-1-1"):
        res = self.svc.meet.race(*map(int, rk.split("-"))).results or {}
        return [(f["bib"], f["elapsed"]) for f in res.get("finishers") or [] if f["status"] == "finished"]

    def other_meet(self, name="Tuesday Dual"):
        meet = self.svc.store.create(name, "2026-10-14")
        return meet.meet.id


class NewAndOpen(Raced):

    def test_new_meet_refused_while_race_cards_are_loaded(self):
        reads = len(self.mgr.reads)
        d = self.post("/api/meet/new", {"name": "Tuesday Dual", "date": "2026-10-14"}, status=409)
        self.assertIn("close the current meet first: 1 race card(s) / 5 read(s) are loaded", d["error"])
        self.assertEqual(self.svc.meet.meet.id, self.meet_id)
        self.assertEqual(len(self.mgr.reads), reads)
        self.assertEqual(self.svc.store.find(name="Tuesday Dual", date_iso="2026-10-14"), [])

    def test_open_or_open_file_of_another_meet_refused(self):
        other = self.other_meet()
        d = self.post("/api/meet/open", {"id": other}, status=409)
        self.assertIn("close the current meet first", d["error"])
        self.post("/api/meet/open-file", {"path": self.svc.store.path(other)}, status=409)
        self.svc.detach()                                 # detached: still this meet's cards
        d = self.post("/api/meet/open", {"id": other}, status=409)
        self.assertEqual(d["cards_meet"], self.meet_id)
        self.assertIsNone(self.svc.meet)

    def test_open_the_open_meet_is_a_no_op(self):
        reads = len(self.mgr.reads)
        d = self.post("/api/meet/open", {"id": self.meet_id})
        self.assertTrue(d["already_open"])
        self.assertEqual(d["meet"]["id"], self.meet_id)
        self.assertEqual(len(self.mgr.reads), reads)

    def test_detached_meet_opens_again_with_its_own_cards(self):
        self.post("/api/meet/close")                       # an archive exists ...
        self.post("/api/meet/open", {"id": self.meet_id})
        self.mgr.ingest("999", GUN + timedelta(minutes=30), "FINISH")
        n = len(self.mgr.reads)
        self.svc.detach()
        d = self.post("/api/meet/open", {"id": self.meet_id})
        self.assertEqual(d["meet"]["id"], self.meet_id)
        self.assertEqual(len(self.mgr.reads), n)          # ... but the newer cards stay
        self.assertNotIn("reopened", d)

    def test_stray_read_after_close_does_not_block_open(self):
        self.post("/api/meet/close")
        self.assertEqual((len(self.mgr.races), len(self.mgr.reads)), (0, 0))
        self.mgr.ingest("1422", GUN + timedelta(hours=2), "FINISH")    # reader still running
        d = self.post("/api/meet/open", {"id": self.meet_id})
        self.assertEqual(d["reopened"]["reads"], 5)
        self.assertEqual(sorted(r.bib for r in self.mgr.reads.values()).count("1422"), 0)
        root = os.path.join(self.tmp, "closed-meets")
        left = [n for n in os.listdir(root) if n.endswith("-leftovers")]
        self.assertEqual(len(left), 1)
        with open(os.path.join(root, left[0], "races-state.json"), encoding="utf-8") as f:
            self.assertEqual([r["bib"] for r in json.load(f)["reads"]], ["1422"])

    def test_new_meet_after_close_with_a_stray_read(self):
        self.post("/api/meet/close")
        self.mgr.ingest("1422", GUN + timedelta(hours=2), "FINISH")
        d = self.post("/api/meet/new", {"name": "Tuesday Dual", "date": "2026-10-14"})
        self.assertNotEqual(d["meet"]["id"], self.meet_id)
        self.assertEqual(len(self.mgr.reads), 0)
        self.assertEqual(self.svc.cards_owner, d["meet"]["id"])


class Close(Raced):

    def test_close_refused_after_detach(self):
        self.svc.detach()
        d = self.post("/api/meet/close", status=409)
        self.assertIn("belong to Test XC", d["error"])
        self.assertEqual(len(self.mgr.reads), 5)
        self.post("/api/meet/open", {"id": self.meet_id})
        r = self.post("/api/meet/close")
        self.assertTrue(r["meet_archived"])
        self.assertIsNone(self.svc.cards_owner)
        self.post("/api/meet/close")                       # nothing loaded: fine

    def test_close_refused_when_the_meet_file_could_not_be_read(self):
        for p in (self.svc.store.path(self.meet_id), self.svc.store.backup_path(self.meet_id)):
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"meet": {"id": ')
        self.svc = self.make_service()
        self.app.meet_service = self.svc
        self.assertIsNone(self.svc.meet)
        s = self.get("/api/meet").get_json()
        self.assertFalse(s["active"])
        self.assertIn("could not be read", s["load_error"]["text"])
        self.assertIn("could not be read", Application._meet_file_problem(self.app))
        d = self.post("/api/meet/close", status=409)
        self.assertIn("Nothing was closed", d["error"])
        self.assertEqual(len(self.mgr.reads), 5)
        self.post("/api/meet/new", {"name": "Tuesday Dual", "date": "2026-10-14"}, status=409)


class FinalBeforeRun(Raced):

    def test_reopen_a_race_final_before_it_was_run(self):
        self.post("/api/meet/races/2-1-1/final")
        res = self.svc.meet.race(2, 1, 1).results
        self.assertTrue(res["finishers"])
        self.assertEqual({f["status"] for f in res["finishers"]}, {"DNS"})
        self.post("/api/meet/races/2-1-1/refinalize")
        self.post("/api/meet/races/2-1-1/reopen")
        self.assertEqual(self.svc.meet.race(2, 1, 1).status, "scheduled")


class Refinalize(Raced):

    def setUp(self):
        super().setUp()
        self.post("/api/meet/races/1-1-1/final")
        self.before = self.finished()
        self.assertEqual(len(self.before), 5)

    def test_refused_when_finishers_would_be_flagged_forced_and_undone(self):
        self.post("/api/meet/bibs/clear", {"all": True})
        d = self.post("/api/meet/races/1-1-1/refinalize", status=409)
        self.assertEqual(len(d["flagged"]), 5)
        self.assertEqual({x["code"] for x in d["flagged"]}, {"unknown_bib"})
        self.assertTrue(all(x["name"] for x in d["flagged"]))
        self.assertEqual(self.finished(), self.before)            # the old results stay
        self.post("/api/meet/races/1-1-1/refinalize", {"force": True})
        self.assertEqual(self.finished(), [])
        self.post("/api/meet/undo")
        self.assertEqual(self.finished(), self.before)
        self.assertEqual(self.svc.meet.race(1, 1, 1).status, "final")

    def test_undo_after_a_refinalize(self):
        race = self.svc.meet.race(1, 1, 1)
        a, b = (self.svc.meet.athlete(e.athlete_id) for e in race.entries[:2])
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        self.post("/api/meet/races/1-1-1/refinalize")
        after = self.svc.meet.race(1, 1, 1).results["finishers"][0]["athlete_id"]
        self.assertEqual(after, b.id)
        self.post("/api/meet/undo")
        self.assertEqual(self.svc.meet.race(1, 1, 1).results["finishers"][0]["athlete_id"], a.id)


class StaleTeamGender(Raced):

    def stale(self):
        return [f for f in self.get("/api/meet").get_json()["fix"] if f["kind"] == "stale_final"]

    def test_team_and_gender_changes_after_final_are_stale(self):
        self.post("/api/meet/races/1-1-1/final")
        self.assertEqual(self.stale(), [])
        row = self.svc.meet.race(1, 1, 1).results["finishers"][0]
        a = self.svc.meet.athlete(row["athlete_id"])
        self.assertEqual((row["team_id"], row["gender"]), (a.team_id, a.gender))
        other = next(t.id for t in self.svc.meet.teams if t.id != a.team_id)
        self.post(f"/api/meet/athletes/{a.id}", {"team_id": other})
        self.assertEqual(self.stale()[0]["detail"], ["1-1-1"])
        self.post("/api/meet/races/1-1-1/refinalize")
        self.assertEqual(self.stale(), [])
        self.post(f"/api/meet/athletes/{a.id}", {"gender": "M" if a.gender == "F" else "F"})
        self.assertEqual(self.stale()[0]["count"], 1)


class StartTime(Raced):

    def test_scheduled_checkin_opens(self):
        self.post("/api/meet/races/2-1-1", {"start_time": "17:00"})
        pane = self.panes[(2, 1, 1)]
        start = Application._race_start_time(self.app, self.mgr.races[pane])
        self.assertEqual(start, datetime(2026, 10, 10, 17, 0))
        self.mgr.start_time_of = lambda race: Application._race_start_time(self.app, race)
        self.mgr.set_checkin_default(schedule={"open_before": 30, "close_before": 5})
        self.assertEqual(self.mgr.tick_checkins(now=datetime(2026, 10, 10, 16, 40)), [pane])
        self.assertEqual(self.mgr.races[pane].status, "checkin")


class BibAssignBody(Raced):

    def test_wrong_shapes_are_400(self):
        for body in ({"rows": 1e308}, {"rows": "100"}, {"rows": [[1, 2, 3]]}, {"sort": 5},
                     {"athlete_ids": 7}, {"per_team_blocks": 1e308}, {"skip": 3.5},
                     {"sort": [1, 2]}):
            self.post("/api/meet/bibs/assign", body, status=400)


class StoreBackup(unittest.TestCase):

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="meetstore-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = MeetStore(os.path.join(self.tmp, "meet"))

    def test_save_keeps_a_backup_and_load_falls_back(self):
        meet = self.store.create("Test XC", "2026-10-10")
        path = self.store.path(meet.meet.id)
        self.assertFalse(os.path.exists(path + ".bak"))
        meet.meet.host = "Ridge"
        self.store.save(meet)
        with open(path + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meet"]["host"], "")
        meet.meet.host = "Valley"
        self.store.save(meet)
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2])                 # power cut
        loaded = self.store.load(meet.meet.id)
        self.assertEqual(loaded.meet.host, "Ridge")
        self.assertIn("restored from its backup", self.store.restored[meet.meet.id])
        self.store.save(loaded)                              # the good .bak is kept
        self.assertTrue(os.path.exists(path + ".damaged"))
        with open(path + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meet"]["host"], "Ridge")
        self.assertNotIn(meet.meet.id, self.store.restored)

    def test_console_opens_from_the_backup_and_says_so(self):
        root = os.path.join(self.tmp, "meet")
        meet = self.store.create("Test XC", "2026-10-10")
        self.store.save(meet)
        with open(os.path.join(root, "ACTIVE"), "w", encoding="utf-8") as f:
            f.write(meet.meet.id + "\n")
        with open(self.store.path(meet.meet.id), "w", encoding="utf-8") as f:
            f.write("{")
        svc = MeetService(root, None)
        self.assertEqual(svc.meet.meet.id, meet.meet.id)
        s = svc.summary()
        self.assertIn("restored from its backup", s["load_warning"])
        self.assertIn("meet_file", [f["kind"] for f in s["fix"]])
        svc.set_info({"host": "Ridge"})                     # still said after a save
        self.assertIn("restored from its backup", svc.summary()["load_warning"])
        self.assertIn("restored", Application._meet_file_problem(SimpleNamespace(meet_service=svc)))


if __name__ == "__main__":
    unittest.main()
