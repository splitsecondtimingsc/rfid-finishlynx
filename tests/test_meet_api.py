"""Meet management REST API (docs/MEET-API.md) against a real RaceManager,
a temp RFID folder and a fake MileSplit transport: the console's Meet tab
contract end to end, plus the legacy-mode guarantees (2026-10-03).

Hardware- and network-free: python -m unittest tests.test_meet_api"""

import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import web
from rfid_finishlynx.meet_service import MeetService
from rfid_finishlynx.races import RaceManager
from tests.test_meet_milesplit import FakeTransport

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")
GUN = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)


def load_entries():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class MeetApiBase(unittest.TestCase):
    """A console with a meet service, no meet open yet."""

    def setUp(self):
        p = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp(prefix="meetapi-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lynx = os.path.join(self.tmp, "Lynx")
        os.makedirs(self.lynx)
        self.rfid = os.path.join(self.lynx, "RFID")          # created by the first write
        self.tr = FakeTransport([
            ("POST auth/login", 200, {"data": {"token30": "tok-abc"}}),
            ("GET timing-company/myall", 200, {"data": [{"id": 42, "name": "Test Timing"}]}),
            ("GET timing-company/42/meets", 200, {"data": [{"meetId": 761226, "name": "Test XC"}]}),
            ("POST mpResults", 201, {"status": "Success", "statusMessage": "ok",
                                     "resultsURL": "https://example.invalid/r"}),
        ])
        self.mgr = RaceManager(athlete_lookup=web.get_athlete_info,
                               on_change=lambda keys: self.svc and self.svc.on_races_changed(keys),
                               finish_readers={"FINISH"},
                               state_path=os.path.join(self.tmp, "races-state.json"))
        self.app = SimpleNamespace(race_manager=self.mgr, hardware_allowed=True,
                                   lif_dirs=lambda: (self.lynx, self.rfid),
                                   config=SimpleNamespace(event_directory=self.lynx),
                                   offer_recent_starts=lambda k: None, meet_service=None)
        self.emitted, self.autoloads = [], []
        self.svc = None
        self.svc = self.make_service()
        self.app.meet_service = self.svc
        web._application = self.app
        self.addCleanup(setattr, web, "_application", None)
        self.addCleanup(lambda: self.svc and self.svc.set_live(False))
        self.c = web.app.test_client()

    def make_service(self):
        return MeetService(os.path.join(self.tmp, "meet"), self.app,
                           token_path=os.path.join(self.tmp, "ms", "token.json"),
                           transport=self.tr, emit=self.emitted.append,
                           autoload_evt=self.autoloads.append)

    # ---------------------------------------------------------- helpers

    def post(self, path, body=None, status=200):
        r = self.c.post(path, data=json.dumps(body or {}), content_type="application/json")
        self.assertEqual(r.status_code, status, (path, r.get_json()))
        return r.get_json()

    def get(self, path, status=200):
        r = self.c.get(path)
        self.assertEqual(r.status_code, status, (path, r.get_data()[:300]))
        return r

    def import_fixture(self, **extra):
        body = {"entries": load_entries(), "name": "Test XC", "date": "2026-10-10",
                "tc_id": 42, "meet_id": 761226}
        body.update(extra)
        return self.post("/api/meet/import/milesplit", body)

    def athletes(self, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return self.get(f"/api/meet/athletes?{qs}").get_json()

    def assign_all(self):
        return self.post("/api/meet/bibs/assign", {"rows": [["100", ""]], "dry_run": False})

    def rfid_file(self, name):
        with open(os.path.join(self.rfid, name), encoding="cp1252") as f:
            return f.read()

    def make_panes(self):
        content = self.rfid_file("lynx.evt")
        web.create_missing_panes(self.mgr, web.evt_race_list(content))
        return {(r.event, r.round, r.heat): r.key for r in self.mgr.races.values()}


# --------------------------------------------------------------- legacy mode

class TestLegacyMode(MeetApiBase):

    def test_no_meet_changes_nothing(self):
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        saved = web._athlete_data
        web._athlete_data = dict(saved, by_bib={"101": {"last_name": "Ppl", "first_name": "Pat",
                                                        "school": "North", "gender": "F"}})
        self.addCleanup(setattr, web, "_athlete_data", saved)
        self.assertEqual(web.get_athlete_info("101")["last_name"], "Ppl")
        self.assertEqual(web.get_athlete_info("555"), {})
        self.assertFalse(os.path.exists(self.rfid))           # nothing written
        self.post("/api/meet/athletes", {"first": "A", "last": "B", "gender": "F"}, 409)

    def test_no_service_at_all(self):
        self.app.meet_service = None
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        self.get("/api/meet/athletes", 503)
        self.assertEqual(web.get_athlete_info("777"), {})


# --------------------------------------------------------------- meet + import

class TestImport(MeetApiBase):

    def test_import_creates_activates_and_writes_start_lists(self):
        d = self.import_fixture()
        s = d["meet"]
        self.assertTrue(s["active"])
        self.assertEqual(s["counts"]["athletes"], 340)
        self.assertEqual(s["counts"]["races"], 4)
        self.assertEqual(s["counts"]["teams"], 12)
        self.assertEqual(s["info"]["milesplit"], {"tc_id": 42, "meet_id": 761226})
        self.assertIn("added", d["report"])
        self.assertEqual(s["start_lists"]["dir"], self.rfid)
        self.assertIsNotNone(s["start_lists"]["written_at"])
        self.assertIsNone(s["start_lists"]["skipped"])
        for name in ("lynx.evt", "lynx.ppl", "lynx.sch"):
            self.assertTrue(os.path.isfile(os.path.join(self.rfid, name)), name)
        self.assertEqual(self.autoloads, [self.lynx])        # .evt monitor told once
        self.assertEqual([k["kind"] for k in s["fix"]], ["no_bib"])  # no bibs yet
        meet_id = s["id"]
        with open(os.path.join(self.tmp, "meet", "ACTIVE"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), meet_id)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "meet", meet_id, "pulls", "entries.json")))
        self.assertEqual(self.emitted[-1]["id"], meet_id)
        self.assertEqual(self.get("/api/meet").get_json()["id"], meet_id)
        self.assertEqual(self.get("/api/meet/list").get_json()["meets"][0]["id"], meet_id)

        # second edit: no second autoload
        self.assign_all()
        self.assertEqual(self.autoloads, [self.lynx])
        # refresh with the same pull: nothing to do
        d = self.post("/api/meet/refresh", {"entries": load_entries()})
        self.assertEqual(d["report"]["added"], [])

        # a restart picks the active meet back up
        again = self.make_service()
        self.assertEqual(again.summary()["id"], meet_id)

    def test_new_open_detach_info(self):
        s = self.post("/api/meet/new", {"name": "Dual", "date": "2026-10-11"})["meet"]
        self.assertTrue(s["active"])
        self.assertEqual(s["start_lists"]["skipped"], "empty meet")
        self.assertFalse(os.path.exists(self.rfid))
        self.post("/api/meet/new", {"name": "x", "date": "10/11/2026"}, 400)
        s = self.post("/api/meet/info", {"host": "Ridge HS", "scoring": {"scorers": 4},
                                         "milesplit": {"meet_id": "5"}})["meet"]
        self.assertEqual(s["info"]["host"], "Ridge HS")
        self.assertEqual(s["info"]["scoring"]["scorers"], 4)
        self.assertEqual(s["info"]["milesplit"]["meet_id"], 5)
        self.assertEqual(self.post("/api/meet/detach"), {"ok": True, "active": False})
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        self.post("/api/meet/open", {"id": "nope"}, 404)
        s = self.post("/api/meet/open", {"id": "2026-10-11-dual"})["meet"]
        self.assertEqual(s["info"]["name"], "Dual")

    def test_import_hytek_file(self):
        text = ("I;Runner;Alex;;M;;RID;Ridge High;;12;\r\n"
                "D;Runner;Alex;;M;;RID;Ridge High;;12;5000;;M;;\r\n")
        from io import BytesIO
        r = self.c.post("/api/meet/import/file",
                        data={"file": (BytesIO(text.encode()), "entries.txt"), "name": "HT"},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["meet"]["counts"]["athletes"], 1)


# --------------------------------------------------------------- athletes + bibs

class TestAthletesAndBibs(MeetApiBase):

    def setUp(self):
        super().setUp()
        self.import_fixture()

    def test_list_filters_and_search(self):
        d = self.athletes()
        self.assertEqual(d["total"], 340)
        v = d["athletes"][0]
        for k in ("athlete_id", "first", "last", "bib", "races", "status", "unattached", "ms_match"):
            self.assertIn(k, v)
        self.assertEqual(v["status"], "entered")
        d = self.athletes(q=v["last"], limit=5)
        self.assertTrue(any(a["athlete_id"] == v["athlete_id"] for a in d["athletes"]))
        self.assertLessEqual(len(d["athletes"]), 5)
        d = self.athletes(team=v["team_id"], gender=v["gender"])
        self.assertTrue(all(a["team_id"] == v["team_id"] for a in d["athletes"]))
        d = self.athletes(race="1-1-1")
        self.assertEqual(d["total"], 82)
        m = self.get(f"/api/meet/athletes/search?first={v['first']}&last={v['last']}").get_json()
        self.assertEqual(m["matches"][0]["athlete_id"], v["athlete_id"])

    def test_list_sorts_by_a_clicked_column(self):
        self.post("/api/meet/bibs/assign", {"rows": [["100", "199"]], "dry_run": False})
        whole = self.athletes(sort="bib")
        self.assertEqual(whole["total"], 340)
        bibs = [int(a["bib"]) for a in whole["athletes"] if a["bib"]]
        self.assertEqual(len(bibs), 100)
        self.assertEqual(bibs, sorted(bibs))
        self.assertFalse(any(a["bib"] for a in whole["athletes"][100:]))      # blanks last
        down = self.athletes(sort="bib", desc=1)
        self.assertEqual(down["athletes"][0]["bib"], "199")
        self.assertFalse(any(a["bib"] for a in down["athletes"][100:]))       # ...either way
        page2 = self.athletes(sort="bib", limit=10, offset=10)["athletes"]
        self.assertEqual([a["athlete_id"] for a in page2],
                         [a["athlete_id"] for a in whole["athletes"][10:20]])  # paging follows the sort
        names = [(a["last"].lower(), a["first"].lower()) for a in self.athletes(sort="name")["athletes"]]
        self.assertEqual(names, sorted(names))
        for k in ("grade", "gender", "team", "race", "milesplit", "status"):
            self.assertEqual(self.athletes(sort=k)["total"], 340, k)
        self.get("/api/meet/athletes?sort=shoe", status=400)

    def test_add_runs_idmatch(self):
        v = self.athletes()["athletes"][0]
        # a walk-up typed exactly as MileSplit has them, after the entry is removed
        self.post(f"/api/meet/athletes/{v['athlete_id']}/entry", {"race": None})
        r = self.c.delete(f"/api/meet/athletes/{v['athlete_id']}")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = self.post("/api/meet/athletes", {"first": v["first"], "last": v["last"],
                                            "gender": v["gender"], "team_id": v["team_id"],
                                            "grade": v["grade"], "race": "1-1-1"})
        self.assertEqual(d["athlete"]["milesplit_id"], v["milesplit_id"])
        self.assertEqual(d["athlete"]["ms_match"], "auto")
        self.assertIsNone(d["id_match"])
        self.assertEqual(d["athlete"]["races"], [[1, 1, 1]])
        # a near miss goes to the review queue
        d = self.post("/api/meet/athletes", {"first": v["first"] + "e", "last": v["last"],
                                            "gender": v["gender"], "team_id": v["team_id"]})
        self.assertIsNotNone(d["id_match"])
        self.assertEqual(d["athlete"]["ms_match"], "pending")
        self.assertEqual(d["athlete"]["status"], "roster")
        q = self.get("/api/meet/idreview").get_json()
        item = next(i for i in q["queue"] if i["athlete_id"] == d["athlete"]["athlete_id"])
        self.assertIn("id_review", [f["kind"] for f in d["meet"]["fix"]])
        s = self.post(f"/api/meet/idreview/{item['athlete_id']}/decline")["meet"]
        self.assertNotIn("id_review", [f["kind"] for f in s["fix"]])

    def test_someone_already_in_the_meet_is_used_not_added_again(self):
        # 2026-10-08, user: changing athletes or adding one with a new number
        # takes them from the roster first (Caroline King came in twice)
        self.assign_all()
        v, other = self.athletes(limit=2)["athletes"]
        n = self.athletes()["total"]
        walk = {"first": v["first"].upper(), "last": " " + v["last"], "gender": v["gender"],
                "team_id": v["team_id"], "bib": "9002"}
        d = self.post("/api/meet/athletes", walk, 409)
        self.assertEqual((d["duplicate"]["athlete_id"], d["duplicate"]["milesplit_id"]),
                         (v["athlete_id"], v["milesplit_id"]))
        self.assertNotIn("holder", d)
        wear = {"new": {"first": v["first"], "last": v["last"], "team_id": v["team_id"]}}
        d = self.post(f"/api/meet/bibs/{other['bib']}/wearer", wear, 409)
        self.assertEqual(d["duplicate"]["athlete_id"], v["athlete_id"])
        self.assertEqual(self.athletes()["total"], n)                       # nothing added, holder untouched
        self.assertEqual(self.svc.meet.athlete(other["athlete_id"]).bib, other["bib"])
        # same name on another team, or the operator says it's someone else: added
        team2 = next(t["id"] for t in self.get("/api/meet/teams").get_json()["teams"] if t["id"] != v["team_id"])
        self.post("/api/meet/athletes", dict(walk, team_id=team2, bib=""))
        self.post("/api/meet/athletes", dict(walk, force=True))
        wear["new"]["force"] = True
        self.post(f"/api/meet/bibs/{other['bib']}/wearer", wear)
        self.assertEqual(self.athletes()["total"], n + 3)

    def test_no_bib_first(self):
        # 2026-10-08, user: click "no bib" -> those runners on top (team, gender,
        # name, or the clicked column), the rest below; a bib moves one down
        self.assign_all()
        for v in self.athletes(sort="name", desc=1, limit=5)["athletes"]:
            self.post("/api/meet/bibs/clear", {"athlete_ids": [v["athlete_id"]]})
        d = self.athletes(nobib=1, roster=0)
        n = d["nobib_total"]
        top, rest = d["athletes"][:n], d["athletes"][n:]
        self.assertEqual((n, self.get("/api/meet").get_json()["counts"]["missing_bib"]), (5, 5))
        self.assertTrue(all(not v["bib"] and v["entered"] for v in top))
        self.assertTrue(all(v["bib"] for v in rest))
        teams = {t["id"]: t.get("short") or t["name"] for t in self.get("/api/meet/teams").get_json()["teams"]}
        key = [(teams[v["team_id"]].lower(), v["gender"], v["last"].lower(), v["first"].lower()) for v in top]
        self.assertEqual(key, sorted(key))
        d = self.athletes(nobib=1, roster=0, sort="name")                  # a clicked column sorts both parts
        top, rest = d["athletes"][:5], d["athletes"][5:]
        for part in (top, rest):
            names = [(v["last"].lower(), v["first"].lower()) for v in part]
            self.assertEqual(names, sorted(names))
        self.post(f"/api/meet/athletes/{top[0]['athlete_id']}/bib", {"bib": "9999"})
        d = self.athletes(nobib=1, roster=0, sort="name")
        self.assertEqual(d["nobib_total"], 4)
        self.assertNotIn(top[0]["athlete_id"], [v["athlete_id"] for v in d["athletes"][:4]])
        self.assertEqual(self.athletes()["total"], self.athletes(nobib=1)["total"])   # same rows, reordered

    def test_bib_conflict_then_swap_and_undo(self):
        self.assign_all()
        a, b = self.athletes(limit=2)["athletes"]
        d = self.post(f"/api/meet/athletes/{a['athlete_id']}/bib", {"bib": b["bib"]}, 409)
        self.assertEqual(d["holder"]["athlete_id"], b["athlete_id"])
        self.assertEqual(d["swap"], {"a": a["athlete_id"], "b": b["athlete_id"]})
        self.post("/api/meet/athletes/a1/bib", {"bib": "12x"}, 400)
        d = self.post("/api/meet/bibs/swap", d["swap"])
        self.assertTrue(d["result"]["ok"])
        after = {v["athlete_id"]: v["bib"] for v in self.athletes(limit=2)["athletes"]}
        self.assertEqual(after, {a["athlete_id"]: b["bib"], b["athlete_id"]: a["bib"]})
        self.post("/api/meet/undo")
        after = {v["athlete_id"]: v["bib"] for v in self.athletes(limit=2)["athletes"]}
        self.assertEqual(after, {a["athlete_id"]: a["bib"], b["athlete_id"]: b["bib"]})
        self.post("/api/meet/undo", status=409)               # one level only

    def test_bib_context_and_wearer(self):
        self.assign_all()
        holder = self.athletes(limit=1)["athletes"][0]
        ctx = self.get(f"/api/meet/bibs/{holder['bib']}/context").get_json()
        self.assertEqual(ctx["holder"]["athlete_id"], holder["athlete_id"])
        # a new athlete, team and race left blank on purpose
        d = self.post(f"/api/meet/bibs/{holder['bib']}/wearer",
                      {"new": {"first": "Wally", "last": "Walkup", "team_id": None,
                               "race": None}, "holder_action": "next_free"})
        rep = d["report"]
        self.assertTrue(rep["created"])
        self.assertIsNone(rep["entered_in"])
        new = self.athletes(q="Walkup")["athletes"][0]
        self.assertIsNone(new["team_id"])
        self.assertEqual(new["bib"], holder["bib"])
        self.assertEqual(new["gender"], holder["gender"])      # prefilled
        self.assertNotEqual(self.svc.meet.athlete(holder["athlete_id"]).bib, holder["bib"])
        # an existing athlete takes it back; the walk-up's bib is cleared
        d = self.post(f"/api/meet/bibs/{holder['bib']}/wearer",
                      {"athlete_id": holder["athlete_id"], "holder_action": "clear"})
        self.assertEqual(d["report"]["holder_id"], new["athlete_id"])
        self.assertEqual(self.svc.meet.athlete(holder["athlete_id"]).bib, holder["bib"])
        self.post("/api/meet/bibs/abc/wearer", {"athlete_id": "a1"}, 400)

    def test_assign_dry_run_then_apply_and_clear(self):
        d = self.post("/api/meet/bibs/assign", {"rows": [["500", "699"], ["900", ""]],
                                                "sort": ["team", "last"], "dry_run": True})
        self.assertNotIn("meet", d)
        self.assertEqual(len(d["report"]["assigned"]), 340)
        self.assertFalse(d["report"]["applied"])
        self.assertEqual(self.athletes(limit=1)["athletes"][0]["bib"], "")
        d = self.post("/api/meet/bibs/assign", {"rows": [["500", "699"], ["900", ""]],
                                                "sort": ["team", "last"], "dry_run": False})
        self.assertTrue(d["report"]["applied"])
        self.assertEqual(d["meet"]["bib_ranges"], [[500, 699], [900, None]])
        self.assertEqual(d["meet"]["counts"]["with_bib"], 340)
        self.assertEqual(self.get("/api/meet/bibs/conflicts").get_json()["ok"], True)
        self.post("/api/meet/bibs/assign", {"rows": [["9", "1"]]}, 400)
        d = self.post("/api/meet/bibs/clear", {"athlete_ids": ["a1", "a2"]})
        self.assertEqual(len(d["report"]["cleared"]), 2)
        self.post("/api/meet/bibs/clear", {}, 400)

    def test_entry_moves_scratches_and_edits(self):
        aid = "a1"
        d = self.post(f"/api/meet/athletes/{aid}/entry", {"race": "2-1-1", "status": "scratched"})
        self.assertEqual(d["athlete"]["status"], "scratched")
        d = self.post(f"/api/meet/athletes/{aid}/entry", {"race": "2-1-1", "status": "exhibition"})
        self.assertEqual(d["athlete"]["status"], "exhibition")
        d = self.post(f"/api/meet/athletes/{aid}/entry", {"race": "4-1-1"})
        self.assertEqual(d["athlete"]["races"], [[4, 1, 1]])
        d = self.post(f"/api/meet/athletes/{aid}/entry", {"race": None})
        self.assertEqual(d["athlete"]["status"], "roster")
        d = self.post(f"/api/meet/athletes/{aid}", {"first": "Renamed", "unattached": True})
        self.assertEqual(d["athlete"]["first"], "Renamed")
        self.assertTrue(d["athlete"]["unattached"])
        self.post(f"/api/meet/athletes/{aid}", {"gender": "Q"}, 400)
        self.post("/api/meet/athletes/a99999", {"first": "x"}, 404)
        t = self.get("/api/meet/teams").get_json()["teams"][0]
        self.assertIn("girls", t)
        s = self.post(f"/api/meet/teams/{t['id']}", {"short": "Ridge2"})["meet"]
        self.assertTrue(s["active"])
        self.post(f"/api/meet/teams/{t['id']}", {"code": "TOOLONG"}, 400)

    def test_next_free_bib(self):
        self.assertIsNone(self.get("/api/meet/bibs/next").get_json()["bib"])   # no ranges yet
        self.assign_all()                                     # 100- for 340 athletes
        self.assertEqual(self.get("/api/meet/bibs/next").get_json(), {"bib": "440"})
        self.post("/api/meet/bibs/clear", {"athlete_ids": [self.svc.meet.athlete_by_bib("100").id]})
        free = self.get("/api/meet/bibs/next").get_json()["bib"]
        self.assertEqual(free, "100")
        holder = self.svc.meet.athlete_by_bib("101")
        ctx = self.get("/api/meet/bibs/101/context").get_json()
        self.assertEqual(ctx["holder"]["athlete_id"], holder.id)
        self.assertEqual(ctx["next_free"], "100")
        ctx = self.get("/api/meet/bibs/100/context").get_json()   # the free one: skip it
        self.assertEqual(ctx["next_free"], "440")

    def test_scratched_race_in_the_view(self):
        d = self.post("/api/meet/athletes/a1/entry", {"race": "2-1-1", "status": "scratched"})
        v = d["athlete"]
        self.assertEqual((v["status"], v["races"], v["scratched_race"]), ("scratched", [], "2-1-1"))
        v = self.athletes(q=v["last"])["athletes"]
        self.assertEqual(next(x for x in v if x["athlete_id"] == "a1")["scratched_race"], "2-1-1")
        d = self.post("/api/meet/athletes/a1/entry", {"race": "2-1-1", "status": "entered"})
        self.assertIsNone(d["athlete"]["scratched_race"])

    def test_walkups_listed_by_the_server_newest_first(self):
        self.assign_all()
        self.assertEqual(self.athletes(source="local")["total"], 0)
        first = self.post("/api/meet/athletes", {"first": "Wren", "last": "Walkup", "gender": "F"})
        second = self.post("/api/meet/athletes", {"first": "Ash", "last": "Latecomer",
                                                 "gender": "M", "race": "2-1-1"})
        holder = self.athletes(limit=1)["athletes"][0]
        third = self.post(f"/api/meet/bibs/{holder['bib']}/wearer",
                          {"new": {"first": "Bo", "last": "Wearer"}})
        d = self.athletes(source="local")
        ids = [a["athlete_id"] for a in d["athletes"]]
        self.assertEqual(ids, [third["report"]["athlete_id"], second["athlete"]["athlete_id"],
                               first["athlete"]["athlete_id"]])
        self.assertTrue(all(a["source"] == "local" and a["added_at"] for a in d["athletes"]))
        self.assertEqual(self.athletes(source="local", limit=1)["total"], 3)
        self.assertEqual(self.athletes()["athletes"][0]["added_at"], "")   # imported

    def test_add_team(self):
        d = self.post("/api/meet/teams", {"name": "Copper Basin High School"})
        t = next(t for t in self.get("/api/meet/teams").get_json()["teams"] if t["id"] == d["team_id"])
        self.assertEqual((t["name"], t["short"]), ("Copper Basin High School", "Copper Basin"))
        self.assertTrue(t["code"] and len(t["code"]) <= 4)
        self.assertTrue(d["meet"]["active"])
        d2 = self.post("/api/meet/teams", {"name": "Copper Hollow", "short": "Hollow"})
        codes = [t["code"] for t in self.get("/api/meet/teams").get_json()["teams"]]
        self.assertEqual(len(codes), len(set(codes)))         # made unique
        self.assertEqual(self.svc.meet.team(d2["team_id"]).short, "Hollow")
        self.post("/api/meet/teams", {"name": "Zed Prep", "code": t["code"]}, 409)
        self.post("/api/meet/teams", {"name": "Zed Prep", "code": "ZEDPR"}, 400)
        self.post("/api/meet/teams", {"name": "copper basin high school"}, 409)
        self.post("/api/meet/teams", {"name": " "}, 400)
        d = self.post("/api/meet/teams", {"name": "Zed Prep", "code": "zp"})
        self.assertEqual(self.svc.meet.team(d["team_id"]).code, "ZP")
        # a walk-up can run for it straight away
        a = self.post("/api/meet/athletes", {"first": "Ida", "last": "Zed", "gender": "F",
                                            "team_id": d["team_id"]})["athlete"]
        self.assertEqual(a["team_id"], d["team_id"])

    def test_get_athlete_info_uses_the_meet(self):
        self.assign_all()
        a = self.athletes(limit=1)["athletes"][0]
        info = web.get_athlete_info(a["bib"])
        self.assertEqual((info["first_name"], info["last_name"]), (a["first"], a["last"]))
        self.assertEqual(info["school"], a["team"])
        self.assertEqual(web.get_athlete_info("0" + a["bib"])["last_name"], a["last"])
        saved = web._athlete_data
        web._athlete_data = dict(saved, by_bib={"99001": {"last_name": "FromPpl"}})
        self.addCleanup(setattr, web, "_athlete_data", saved)
        self.assertEqual(web.get_athlete_info("99001")["last_name"], "FromPpl")   # fallback


# --------------------------------------------------------------- races + start lists

class TestRacesAndStartLists(MeetApiBase):

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.assign_all()

    def test_order_rewrites_start_lists_but_not_in_preview(self):
        self.assertEqual(self.rfid_file("lynx.sch").split(), ["1,1,1", "2,1,1", "3,1,1", "4,1,1"])
        s = self.post("/api/meet/races/order", {"order": ["4-1-1", "3-1-1"]})["meet"]
        self.assertEqual(self.rfid_file("lynx.sch").split(), ["4,1,1", "3,1,1", "1,1,1", "2,1,1"])
        self.assertIsNone(s["start_lists"]["skipped"])
        races = self.get("/api/meet/races").get_json()["races"]
        self.assertEqual([r["rk"] for r in races], ["4-1-1", "3-1-1", "1-1-1", "2-1-1"])
        self.post("/api/meet/races/order", {"order": ["4-1-1", "4-1-1"]}, 400)
        self.post("/api/meet/races/order", {"order": ["9-1-1"]}, 404)

        before = self.rfid_file("lynx.evt")
        self.app.hardware_allowed = False
        s = self.post("/api/meet/races/1-1-1", {"name": "Girls Varsity Preview"})["meet"]
        self.assertEqual(s["start_lists"]["skipped"], "preview")
        self.assertTrue(s["preview"])
        self.assertEqual(self.rfid_file("lynx.evt"), before)
        self.app.hardware_allowed = True
        self.post("/api/meet/races/1-1-1", {"name": "Girls Varsity Live"})
        self.assertIn("Girls Varsity Live", self.rfid_file("lynx.evt"))

    def test_split_genders(self):
        self.post("/api/meet/athletes/a1/entry", {"race": "1-1-1"})       # a boy in the girls' race
        s = self.post("/api/meet/races/1-1-1/split-genders", {"on": True})["meet"]
        race = next(r for r in self.get("/api/meet/races").get_json()["races"] if r["rk"] == "1-1-1")
        self.assertTrue(race["split_genders"])
        self.assertEqual(len(race["divisions"]), 2)
        self.post("/api/meet/races/1-1-1/split-genders", {"on": False})
        race = next(r for r in self.get("/api/meet/races").get_json()["races"] if r["rk"] == "1-1-1")
        self.assertFalse(race["split_genders"])
        self.assertTrue(s["active"])

    def test_run_together_and_separately(self):
        # 2026-10-05: two races off one gun, one Lynx event, scored apart
        before = self.races()
        n = before["1-1-1"]["entries"] + before["2-1-1"]["entries"]
        out = self.post("/api/meet/races/1-1-1/run-together", {"races": ["2-1-1"]})
        self.assertEqual(out["report"]["removed"], ["2-1-1"])
        races = self.races()
        self.assertNotIn("2-1-1", races)
        self.assertTrue(races["1-1-1"]["run_together"])
        self.assertEqual(races["1-1-1"]["entries"], n)
        self.assertEqual(len(races["1-1-1"]["divisions"]), 2)
        self.assertNotIn("2,1,1", self.rfid_file("lynx.sch").split())
        self.import_fixture()                    # a MileSplit re-pull keeps them together
        self.assertNotIn("2-1-1", self.races())
        self.assertEqual(self.races()["1-1-1"]["entries"], n)
        self.post("/api/meet/races/1-1-1/run-together", {"races": []}, 400)
        self.post("/api/meet/races/1-1-1/run-together", {"races": ["1-1-1"]}, 400)
        self.post("/api/meet/races/1-1-1/run-together", {"races": ["9-1-1"]}, 404)

        out = self.post("/api/meet/races/1-1-1/run-separately")
        self.assertEqual(out["report"]["removed"], ["2-1-1"])
        races = self.races()
        self.assertFalse(races["1-1-1"]["run_together"])
        self.assertEqual(races["2-1-1"]["entries"], before["2-1-1"]["entries"])
        self.assertIn("2,1,1", self.rfid_file("lynx.sch").split())
        self.post("/api/meet/races/1-1-1/run-separately", None, 409)

    def races(self):
        return {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}

    def test_start_times(self):
        evt = self.rfid_file("lynx.evt")
        self.post("/api/meet/races/1-1-1", {"start_time": "8:00 am"})
        r = self.races()["1-1-1"]
        self.assertEqual((r["start_time"], r["start_time_text"]), ("08:00", "8:00 AM"))
        self.assertEqual(self.rfid_file("lynx.evt"), evt)          # FinishLynx doesn't care
        for typed, want in [("20:15", "8:15 PM"), ("0930", "9:30 AM")]:
            self.post("/api/meet/races/1-1-1", {"start_time": typed})
            self.assertEqual(self.races()["1-1-1"]["start_time_text"], want)
        d = self.post("/api/meet/races/1-1-1", {"start_time": "half past"}, 400)
        self.assertIn("half past", d["error"])
        self.assertEqual(self.races()["1-1-1"]["start_time"], "09:30")      # unchanged
        self.post("/api/meet/races/1-1-1", {"start_time": ""})
        self.assertEqual(self.races()["1-1-1"]["start_time_text"], "")

    def test_fill_start_times_down_the_run_order(self):
        self.post("/api/meet/races/order", {"order": ["4-1-1", "3-1-1"]})   # 4, 3, 1, 2
        self.post("/api/meet/races/1-1-1", {"start_time": "07:00"})
        d = self.post("/api/meet/races/start-times", {"first": "8:00 AM", "every_min": 30})
        self.assertEqual(d["filled"], 3)
        got = [(r["rk"], r["start_time_text"]) for r in self.get("/api/meet/races").get_json()["races"]]
        self.assertEqual(got, [("4-1-1", "8:00 AM"), ("3-1-1", "8:30 AM"), ("1-1-1", "7:00 AM"),
                               ("2-1-1", "9:30 AM")])           # a set time is kept, slots don't shift
        d = self.post("/api/meet/races/start-times", {"first": "9:00", "every_min": 45,
                                                       "overwrite": True})
        self.assertEqual(d["filled"], 4)
        self.assertEqual([r["start_time_text"] for r in self.get("/api/meet/races").get_json()["races"]],
                         ["9:00 AM", "9:45 AM", "10:30 AM", "11:15 AM"])
        self.post("/api/meet/races/start-times", {"first": "11 pm", "every_min": 30,
                                                   "overwrite": True}, 400)    # past midnight
        self.post("/api/meet/races/start-times", {"every_min": 30}, 400)
        self.post("/api/meet/races/start-times", {"first": "8:00", "every_min": -5}, 400)
        self.assertEqual(self.races()["4-1-1"]["start_time"], "09:00")       # nothing half-done

    def test_new_race_then_cancel(self):
        d = self.post("/api/meet/races", {"new": {"name": "Open 5K", "gender": "x",
                                                  "distance_m": 5000, "division": "Open"},
                                          "start_time": "11:00"})
        rk = d["race"]
        self.assertEqual(rk, "5-1-1")
        races = self.get("/api/meet/races").get_json()["races"]
        self.assertEqual((races[-1]["rk"], races[-1]["name"], races[-1]["start_time_text"]),
                         ("5-1-1", "Open 5K", "11:00 AM"))
        self.assertEqual(self.rfid_file("lynx.sch").split()[-1], "5,1,1")
        self.assertIn((5, 1, 1), {(r.event, r.round, r.heat) for r in self.mgr.races.values()})
        # another heat of an existing event; duplicates and unknown events refused
        self.assertEqual(self.post("/api/meet/races", {"event": 1, "name": "Girls V heat 2"})["race"],
                         "1-1-2")
        self.post("/api/meet/races", {"event": 1, "heat": 2}, 409)
        self.post("/api/meet/races", {"event": 99}, 400)
        self.post("/api/meet/races", {}, 400)
        self.post("/api/meet/races", {"new": {"name": "Bad", "gender": "Q"}}, 400)
        self.post("/api/meet/races", {"event": 1, "start_time": "later"}, 400)
        # cancel: its runners become not entered, its pane and new event go
        self.post("/api/meet/athletes/a1/entry", {"race": rk})
        r = self.c.delete(f"/api/meet/races/{rk}")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn(rk, self.races())
        v = self.post("/api/meet/athletes/a1", {})["athlete"]
        self.assertEqual(v["status"], "roster")
        self.assertIsNone(self.svc.meet.event(5))
        self.assertNotIn((5, 1, 1), {(r.event, r.round, r.heat) for r in self.mgr.races.values()})
        self.assertNotIn("5,1,1", self.rfid_file("lynx.sch"))
        self.assertIsNotNone(self.svc.meet.event(1))                       # still used
        self.assertEqual(self.c.delete("/api/meet/races/9-1-1").status_code, 404)


# --------------------------------------------------------------- results, final, print, MileSplit

class TestResults(MeetApiBase):

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.assign_all()
        self.panes = self.make_panes()
        self.pane = self.panes[(1, 1, 1)]
        race = self.svc.meet.race(1, 1, 1)
        self.bibs = [self.svc.meet.athlete(e.athlete_id).bib for e in race.entries]
        self.mgr.set_gun(self.pane, GUN)
        for i, bib in enumerate(self.bibs[:40]):
            self.mgr.ingest(bib, GUN + timedelta(minutes=18, seconds=3 * i), "FINISH")
        # a stray tag nobody in the meet wears, placed in the race by hand
        self.mgr.ingest("99999", GUN + timedelta(minutes=25), "FINISH")
        stray = next(r for r in self.mgr.reads.values() if r.bib == "99999")
        self.mgr.assign_read(stray.id, self.pane)

    def test_final_with_a_flagged_runner_then_reopen(self):
        # 2026-10-06: the stray tag is flagged and left out of the results;
        # it no longer stops Mark final (user: "remove the result from the
        # overall results but keep the row there to edit later")
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertFalse(d["official"])
        self.assertEqual(d["pane_key"], self.pane)
        self.assertEqual(d["unresolved"], [])
        self.assertEqual([(i["bib"], i["code"], i["label"]) for i in d["issues"]],
                         [("99999", "unknown_bib", "Unknown bib")])
        self.assertEqual(sum(f["status"] == "finished" for f in d["results"]["finishers"]), 40)
        self.assertNotIn("99999", {f["bib"] for f in d["results"]["finishers"]})
        self.assertEqual(self.get("/api/meet/races").get_json()["races"][0]["issues"], 1)

        self.post("/api/meet/races/1-1-1/marks", {"bib": "99999", "mark": "XX"}, 400)
        self.post("/api/meet/races/1-1-1/marks", {"bib": self.bibs[0], "mark": "DQ"})
        self.post("/api/meet/races/1-1-1/marks", {"bib": self.bibs[0], "mark": None})
        self.assertEqual(self.svc.meet.race(1, 1, 1).marks, {})

        s = self.post("/api/meet/races/1-1-1/final")["meet"]
        self.assertEqual(s["counts"]["final_races"], 1)
        self.assertEqual(self.mgr.races[self.pane].status, "final")
        self.assertEqual([f["kind"] for f in s["fix"] if f["kind"] == "issues"], ["issues"])
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertTrue(d["official"])
        self.assertEqual(d["status"], "final")
        self.assertIn("1", d["team_scores"])
        self.assertEqual([i["bib"] for i in d["issues"]], ["99999"])           # kept to edit later
        self.post("/api/meet/races/1-1-1/marks", {"bib": "99999", "mark": "DNF"}, 409)
        self.post("/api/meet/races/1-1-1/split-genders", {"on": True}, 409)

        self.post("/api/meet/races/1-1-1/reopen")
        self.assertEqual(self.mgr.races[self.pane].status, "racing")
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertFalse(d["official"])
        self.post("/api/meet/races/2-1-1/final", status=200)   # nobody ran: all DNS, no blockers

    def test_live_results_follow_new_reads(self):
        n = lambda: sum(f["status"] == "finished" for f in   # noqa: E731
                        self.get("/api/meet/races/1-1-1/results").get_json()["results"]["finishers"])
        self.assertEqual(n(), 40)                     # the stray tag is flagged, not placed
        self.mgr.ingest(self.bibs[50], GUN + timedelta(minutes=24), "FINISH")
        self.assertEqual(n(), 41)                               # cache dropped by on_change
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual(races["1-1-1"]["finished"], 42)
        self.assertEqual(races["1-1-1"]["status"], "racing")
        self.assertEqual(races["3-1-1"]["status"], "pending")

    def runners_from_other_races(self, gender, n):
        """n bibbed athletes of `gender` entered in a race other than 1-1-1."""
        here = {e.athlete_id for e in self.svc.meet.race(1, 1, 1).entries}
        out = []
        for r in self.svc.meet.races_in_order():
            for e in r.entries:
                a = self.svc.meet.athlete(e.athlete_id)
                if a.id not in here and a.bib and (a.gender or "")[:1] == gender and a not in out:
                    out.append(a)
        return out[:n]

    def test_runner_from_another_race_is_flagged_not_switched(self):
        # 2026-10-06 (user): a runner who isn't entered here is flagged "Not
        # in this race" and waits for the operator - also when FinishLynx
        # timed them here (that used to switch them by itself and hid
        # swapped bibs). Override switches them in (out of their own race).
        g = self.mgr.races[self.pane].gender
        jv, timed = self.runners_from_other_races(g, 2)
        other = self.svc.meet.entries_for(jv.id)[0][0]
        for i, a in enumerate((jv, timed)):
            self.mgr.ingest(a.bib, GUN + timedelta(minutes=20, seconds=i), "FINISH")
        with self.mgr._lock:
            self.mgr._fl_timed[self.pane] = {timed.bib: {"time": 1200.0}}
        self.svc.on_races_changed([self.pane])
        deadline = time.time() + 1
        while time.time() < deadline and self.svc._auto_running:
            time.sleep(0.02)
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        flagged = {i["bib"]: i for i in d["issues"]}
        self.assertEqual(set(flagged) - {"99999"}, {jv.bib, timed.bib})
        self.assertEqual(flagged[jv.bib]["code"], "not_entered")
        self.assertEqual(flagged[jv.bib]["label"], "Not in this race")
        self.assertTrue(flagged[jv.bib]["entered_in"])
        self.assertEqual(flagged[jv.bib]["blocked"], "")
        self.assertNotEqual([rc.key for rc, _ in self.svc.meet.entries_for(timed.id)], [(1, 1, 1)])
        ne = {x["bib"]: x for x in d["not_entered"]}             # still in the API, from the flags
        self.assertEqual(ne[jv.bib]["athlete_id"], jv.id)
        pi = web._races_payload()["issues"][str(self.pane)]
        self.assertEqual(pi["issues"][jv.bib]["code"], "not_entered")

        # Override: into this race, out of the other one
        r = self.post(f"/api/meet/races/1-1-1/issues/{jv.bib}", {"action": "override"})
        self.assertEqual(r["report"]["refinalized"], [])
        self.assertEqual([rc.key for rc, _ in self.svc.meet.entries_for(jv.id)], [(1, 1, 1)])
        self.assertNotIn(jv.id, {e.athlete_id for e in other.entries if other.key != (1, 1, 1)})
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertNotIn(jv.bib, {i["bib"] for i in d["issues"]})
        self.assertIn(jv.bib, {f["bib"] for f in d["results"]["finishers"] if f["status"] == "finished"})
        row = next(f for f in self.mgr.results(self.pane)["finishers"] if f["bib"] == jv.bib)
        self.assertNotEqual(row["finish"]["lif_why"], "off_list")       # .lif hold gone too

        # a runner of the other gender can't be overridden into this race
        (wrong,) = self.runners_from_other_races("M" if g == "F" else "F", 1)
        self.mgr.ingest(wrong.bib, GUN + timedelta(minutes=21), "FINISH")
        read = max((r for r in self.mgr.reads.values() if r.bib == wrong.bib), key=lambda r: r.id)
        self.mgr.assign_read(read.id, self.pane)
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertIn("race", next(i for i in d["issues"] if i["bib"] == wrong.bib)["blocked"])
        self.post(f"/api/meet/races/1-1-1/issues/{wrong.bib}", {"action": "override"}, 409)
        self.post("/api/meet/races/1-1-1/switch-in", {"athletes": "x"}, 400)
        self.post(f"/api/meet/races/1-1-1/issues/{jv.bib}", {"action": "nope"}, 400)

    def finalize(self):
        self.post("/api/meet/races/1-1-1/marks", {"bib": "99999", "mark": "DNF"})
        self.post("/api/meet/races/1-1-1/final")

    def test_print_routes(self):
        self.finalize()
        cases = [("results", "race=1-1-1"), ("results", "race=1-1-1&columns=2&places=1-10"),
                 ("meet-results", "hide=DNF,DNS"), ("team-scores", "breakdown=1"),
                 ("awards", ""), ("rosters", ""), ("split-scores", "race=1-1-1"),
                 ("start-list", "race=2-1-1"), ("team-bibs", "teams=t1,t2"),
                 ("announcer", "top=5&team_count=all"), ("announcer", "race=1-1-1&team_count=2")]
        for name, q in cases:
            for fmt in ("pdf", "txt"):
                if fmt == "txt" and "columns=2" in q:
                    continue
                r = self.get(f"/api/meet/print/{name}.{fmt}?{q}")
                self.assertIn("attachment; filename=", r.headers["Content-Disposition"])
                if fmt == "pdf":
                    self.assertTrue(r.data.startswith(b"%PDF"), name)
                    self.assertEqual(r.mimetype, "application/pdf")
                else:
                    self.assertTrue(r.mimetype.startswith("text/plain"), name)
                    self.assertTrue(r.data.strip(), name)
        for name, q in [("award-labels", "skip=3"), ("team-labels", ""),
                        ("competitor-labels", "teams=t1"), ("finish-line", "race=3-1-1")]:
            self.assertTrue(self.get(f"/api/meet/print/{name}.pdf?{q}").data.startswith(b"%PDF"))
            self.get(f"/api/meet/print/{name}.txt?{q}", 400)            # PDF only
        self.get("/api/meet/print/results.txt", 400)                     # needs race
        self.get("/api/meet/print/nope.pdf", 400)
        self.get("/api/meet/print/rosters.pdf?places=1-3", 400)          # filter it doesn't take
        self.get("/api/meet/print/results.txt?race=1-1-1&columns=2", 400)
        self.get("/api/meet/print/results.pdf?race=9-1-1", 404)
        # split scores of a race still running print from live results
        self.assertTrue(self.get("/api/meet/print/split-scores.txt?race=1-1-1").data)

    def test_print_picked_races(self):
        self.finalize()
        self.post("/api/meet/races/2-1-1/final")
        whole = self.get("/api/meet/print/meet-results.txt").data.decode()
        one = self.get("/api/meet/print/meet-results.txt?races=2-1-1").data.decode()
        self.assertLess(len(one), len(whole))
        self.assertIn("No final results yet.",
                      self.get("/api/meet/print/meet-results.txt?races=3-1-1").data.decode())
        self.assertTrue(self.get("/api/meet/print/meet-results.pdf?races=1-1-1,2-1-1")
                        .data.startswith(b"%PDF"))
        for name in ("team-scores", "awards", "announcer"):
            self.assertTrue(self.get(f"/api/meet/print/{name}.txt?races=1-1-1").data.strip())
        self.assertTrue(self.get("/api/meet/print/award-labels.pdf?races=1-1-1")
                        .data.startswith(b"%PDF"))
        self.get("/api/meet/print/meet-results.txt?races=9-1-1", 404)
        self.get("/api/meet/print/awards.txt?race=1-1-1&races=2-1-1", 400)
        self.get("/api/meet/print/rosters.txt?races=1-1-1", 400)

    def test_remember_me_logs_in_again_after_logout(self):
        d = self.post("/api/meet/milesplit/login", {"email": "t@example.org", "password": "pw",
                                                    "remember": True})
        self.assertEqual(d["remembered"], "t@example.org")
        self.assertEqual(self.get("/api/meet").get_json()["milesplit"]["remembered"],
                         "t@example.org")
        # (+ can_remember on Windows, where the login can be remembered)
        st = self.get("/api/meet/milesplit/status").get_json()
        self.assertEqual({k: st[k] for k in ("logged_in", "remembered")},
                         {"logged_in": True, "remembered": "t@example.org"})
        self.post("/api/meet/milesplit/logout")                  # token gone, login kept
        d = self.get("/api/meet/milesplit/meets?month=10&year=2026").get_json()
        self.assertEqual(d["companies"][0]["id"], 42)            # logged itself in again
        self.assertEqual(len(self.tr.of("POST", "auth/login")), 2)
        d = self.post("/api/meet/milesplit/logout", {"forget": True})
        self.assertIsNone(d["remembered"])
        self.post("/api/meet/milesplit/meets", status=405)
        self.get("/api/meet/milesplit/meets?month=10&year=2026", status=401)

    def test_milesplit_login_meets_upload_live(self):
        self.post("/api/meet/races/1-1-1/milesplit/upload", status=409)   # not final
        d = self.post("/api/meet/milesplit/login", {"email": "t@example.org", "password": "pw"})
        self.assertTrue(d["logged_in"])
        self.assertTrue(self.get("/api/meet").get_json()["milesplit"]["logged_in"])
        d = self.get("/api/meet/milesplit/meets?month=10&year=2026").get_json()
        self.assertEqual(d["companies"][0]["id"], 42)
        self.assertEqual(d["meets"][0]["tc_id"], 42)

        self.finalize()
        self.post("/api/meet/milesplit/logout")
        d = self.post("/api/meet/races/1-1-1/milesplit/upload", status=401)   # not logged in
        self.assertFalse(d["logged_in"])
        self.post("/api/meet/milesplit/login", {"email": "t@example.org", "password": "pw"})
        d = self.post("/api/meet/races/1-1-1/milesplit/upload")
        self.assertTrue(d["result"]["ok"])
        self.assertEqual(len(self.tr.of("POST", "mpResults")), 1)
        r = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertTrue(r["milesplit"]["ok"])
        self.assertIsNotNone(r["milesplit"]["uploaded_at"])
        # the race list carries it too: the Results page's "Last sent"
        races = {x["rk"]: x for x in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual(races["1-1-1"]["uploaded_at"], r["milesplit"]["uploaded_at"])
        self.assertTrue(races["1-1-1"]["upload_ok"])

        d = self.post("/api/meet/milesplit/live", {"on": True})
        self.assertIsNotNone(d["live"])
        report = self.svc._pusher.flush()
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(self.tr.of("PATCH", "/entries"))
        self.assertIsNotNone(self.get("/api/meet").get_json()["milesplit"]["live"])
        self.assertIsNone(self.post("/api/meet/milesplit/live", {"on": False})["live"])
        self.assertFalse(self.post("/api/meet/milesplit/logout")["logged_in"])

    def test_upload_sends_every_final_race_in_one_upload(self):
        # 2026-10-09 (Jim Crawford): girls then boys sent as two uploads left
        # only the boys on MileSplit - an upload replaces the meet's results.
        pane2 = self.panes[(2, 1, 1)]
        race2 = self.svc.meet.race(2, 1, 1)
        bibs2 = [self.svc.meet.athlete(e.athlete_id).bib for e in race2.entries]
        self.mgr.set_gun(pane2, GUN)
        for i, bib in enumerate(bibs2[:10]):
            self.mgr.ingest(bib, GUN + timedelta(minutes=17, seconds=3 * i), "FINISH")
        self.finalize()
        self.post("/api/meet/races/2-1-1/final")
        self.post("/api/meet/milesplit/login", {"email": "t@example.org", "password": "pw"})

        d = self.post("/api/meet/races/2-1-1/milesplit/upload")       # from either race
        self.assertEqual(d["races"], ["1-1-1", "2-1-1"])
        (call,) = self.tr.of("POST", "mpResults")
        sent = {r["event"]["event_num"] for t in call["body"]["teams"] for a in t["roster"]
                for r in a["results"] if "mark" in r}
        self.assertEqual(sent, {1, 2})
        races = {x["rk"]: x for x in self.get("/api/meet/races").get_json()["races"]}
        self.assertTrue(races["1-1-1"]["upload_ok"] and races["2-1-1"]["upload_ok"])

        # a race on MileSplit that was reopened blocks: sending without it
        # would take it off the results page
        self.post("/api/meet/races/1-1-1/reopen")
        d = self.post("/api/meet/races/2-1-1/milesplit/upload", status=409)
        self.assertIn("reopened", d["error"])
        self.assertEqual(len(self.tr.of("POST", "mpResults")), 1)

    def test_select_lif_file_on_a_final_race(self):
        # 2026-10-09 (Jim Crawford): a walk back over the mat replaced a
        # finish; the picked .lif is the truth and the race re-finalizes.
        self.finalize()
        last = self.bibs[39]                        # 40th on the mat, 1st in the file
        lif = ("2,1,1,Girls 5K,,,,,,5000,\r\n"
               f"1,{last},,,,,17:00.000,,,,,,,,,,,,\r\n"
               f"2,{self.bibs[0]},,,,,18:00.000,,,,,,,,,,,,\r\n")
        post = lambda body, name: self.c.post(           # noqa: E731
            "/api/meet/races/1-1-1/import-lif",
            data={"file": (io.BytesIO(body.encode("cp1252")), name)},
            content_type="multipart/form-data")
        r = post("not a lif\r\n", "x.lif")
        self.assertEqual(r.status_code, 400, r.get_json())
        r = post(lif, "001-1-01.lif")
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertTrue(d["refinalized"], d)
        self.assertEqual(d["lif"]["timed"], 2)
        self.assertEqual(d["lif"]["moved"][0]["bib"], last)
        res = self.get("/api/meet/races/1-1-1/results").get_json()
        self.assertTrue(res["official"])
        fin = [f for f in res["results"]["finishers"] if (f.get("status") or "finished") == "finished"]
        self.assertEqual([str(f["bib"]) for f in fin[:2]], [last, self.bibs[0]])
        self.assertAlmostEqual(fin[0]["elapsed"], 1020.0, places=2)
        self.assertEqual(len(fin), 40)                  # the rest still from the reads
        races = self.get("/api/races").get_json()["races"]
        self.assertEqual(races[str(self.pane)]["lif_truth"]["name"], "001-1-01.lif")
        self.c.post(f"/api/races/{self.pane}/clear_lif")
        self.assertIsNone(self.get("/api/races").get_json()["races"][str(self.pane)]["lif_truth"])

    def test_cancel_refused_once_a_race_has_reads_or_results(self):
        d = self.c.delete("/api/meet/races/1-1-1")
        self.assertEqual(d.status_code, 409, d.get_json())
        self.assertIn("reads", d.get_json()["error"])
        self.assertIn((1, 1, 1), {(r.event, r.round, r.heat) for r in self.mgr.races.values()})
        self.finalize()
        self.svc.meet.race(1, 1, 1)
        self.assertEqual(self.c.delete("/api/meet/races/1-1-1").status_code, 409)
        self.assertIsNotNone(self.svc.meet.race(1, 1, 1))
        # a gun alone is enough to refuse it
        self.mgr.set_gun(self.panes[(3, 1, 1)], GUN)
        self.assertEqual(self.c.delete("/api/meet/races/3-1-1").status_code, 409)
        # nothing happened in 4-1-1: it goes
        self.assertEqual(self.c.delete("/api/meet/races/4-1-1").status_code, 200)

    def test_clear_entries(self):
        # refused like cancel: reads in 1-1-1, a gun in 3-1-1
        d = self.c.post("/api/meet/races/1-1-1/clear-entries", json={})
        self.assertEqual(d.status_code, 409, d.get_json())
        self.mgr.set_gun(self.panes[(3, 1, 1)], GUN)
        self.assertEqual(self.c.post("/api/meet/races/3-1-1/clear-entries", json={}).status_code, 409)
        # 4-1-1 is untouched: emptied, athletes kept with their bibs, undoable
        race = self.svc.meet.race(4, 1, 1)
        aids = [e.athlete_id for e in race.entries]
        self.assertTrue(aids)
        bibs = {a.id: a.bib for a in self.svc.meet.athletes if a.id in aids}
        s = self.post("/api/meet/races/4-1-1/clear-entries")["meet"]
        self.assertTrue(s["active"])
        self.assertEqual(self.svc.meet.race(4, 1, 1).entries, [])
        self.assertEqual({a.id: a.bib for a in self.svc.meet.athletes if a.id in aids}, bibs)
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual(races["4-1-1"]["entries"], 0)
        self.post("/api/meet/undo")
        self.assertEqual([e.athlete_id for e in self.svc.meet.race(4, 1, 1).entries], aids)
        self.assertEqual(self.c.post("/api/meet/races/9-1-1/clear-entries", json={}).status_code, 404)

    def test_all_start_lists_and_schedule_print(self):
        self.post("/api/meet/races/start-times", {"first": "8:00", "every_min": 30})
        txt = self.get("/api/meet/print/start-list.txt").data.decode("utf-8")
        self.assertEqual(txt.count("\f"), 3)                        # 4 races, one per page
        self.assertIn("Race 1 of 4:", txt)
        self.assertIn("Start: 8:00 AM", txt)
        self.assertIn("Race 4 of 4:", txt)
        r = self.get("/api/meet/print/start-list.pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))
        self.assertIn("start-list.pdf", r.headers["Content-Disposition"])
        one = self.get("/api/meet/print/start-list.txt?race=2-1-1").data.decode("utf-8")
        self.assertIn("Start: 8:30 AM", one)
        self.assertNotIn("Race 1 of", one)
        sched = self.get("/api/meet/print/schedule.txt").data.decode("utf-8")
        self.assertIn("/ Schedule", sched)
        self.assertIn(" 9:30 AM   4 4-1-1", sched)
        self.assertTrue(self.get("/api/meet/print/schedule.pdf").data.startswith(b"%PDF"))
        self.get("/api/meet/print/schedule.pdf?race=1-1-1", 400)       # takes no params

    def test_send_start_times_to_live(self):
        self.post("/api/meet/races/start-times", {"first": "8:00", "every_min": 30})
        self.post("/api/meet/milesplit/live/start-times", status=401)    # not logged in
        self.post("/api/meet/milesplit/login", {"email": "t@example.org", "password": "pw"})
        d = self.post("/api/meet/milesplit/live/start-times")
        # 1-1-1 has finishers (live on MileSplit): only the three upcoming races
        self.assertEqual([r["rk"] for r in d["races"]], ["2-1-1", "3-1-1", "4-1-1"])
        [call] = self.tr.of("PATCH", "/meets/761226/events")
        self.assertEqual(call["body"], d["entries"])
        self.assertEqual(d["sent"], len(call["body"]))
        times = {e["number"]: e["rounds"]["F"]["session"]["time"] for e in call["body"]}
        self.assertEqual(times[self.svc.meet.race(2, 1, 1).entries[0].event], "8:30 AM")
        self.assertNotIn("status", str(call["body"]))
        self.assertNotIn(1, times)
        # Live refusing the (unverified) format is reported, not swallowed
        self.tr.rules.insert(0, ("PATCH /events", 400, {"statusCode": 400, "message": "nope"}))
        d = self.post("/api/meet/milesplit/live/start-times", status=502)
        self.assertIn("nope", d["error"])
        self.post("/api/meet/races/start-times", {"first": "", "every_min": 0}, 400)
        for rk in ("2-1-1", "3-1-1", "4-1-1"):
            self.post(f"/api/meet/races/{rk}", {"start_time": ""})
        self.post("/api/meet/milesplit/live/start-times", status=409)    # nothing to send
        with mock.patch("rfid_finishlynx.meet.milesplit.LIVE_SEND_START_TIMES", False):
            d = self.post("/api/meet/milesplit/live/start-times", status=409)
            self.assertIn("switched off", d["error"])
        self.assertEqual([c for c in self.tr.calls if c["method"] == "DELETE"], [])

    def test_network_refused_in_preview(self):
        self.finalize()
        self.app.hardware_allowed = False
        for path, body in [("/api/meet/milesplit/login", {"email": "a", "password": "b"}),
                           ("/api/meet/races/1-1-1/milesplit/upload", {}),
                           ("/api/meet/milesplit/live", {"on": True}),
                           ("/api/meet/milesplit/live/start-times", {}),
                           ("/api/meet/refresh", {"fetch": True}),
                           ("/api/meet/import/milesplit", {"fetch": True, "tc_id": 1, "meet_id": 2})]:
            self.assertEqual(self.post(path, body, 409)["error"], "preview mode", path)
        self.assertEqual(self.get("/api/meet/milesplit/meets", 409).get_json()["error"], "preview mode")
        self.assertEqual(self.tr.calls, [])

    def test_close_archives_the_meet(self):
        meet_id = self.svc.summary()["id"]
        d = self.post("/api/meet/close")
        self.assertTrue(d["success"])
        self.assertTrue(os.path.isfile(os.path.join(d["meet_archived"], "meet.json")))
        self.assertTrue(os.path.isfile(os.path.join(d["meet_archived"], "pulls", "entries.json")))
        self.assertTrue(d["meet_archived"].startswith(d["archive_dir"]))
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "meet", "ACTIVE")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "meet", meet_id, "meet.json")))


class TestEvtMonitorHandOff(MeetApiBase):
    """No injected autoload: our first write makes web's own .evt/.ppl
    monitor watch the RFID folder, and a later edit reaches the panes
    through it (the legacy path, unchanged)."""

    def make_service(self):
        return MeetService(os.path.join(self.tmp, "meet"), self.app,
                           token_path=os.path.join(self.tmp, "ms", "token.json"),
                           transport=self.tr, emit=self.emitted.append)

    def setUp(self):
        super().setUp()
        self.app.config = SimpleNamespace(event_directory=self.lynx, evt_file="",
                                          races=SimpleNamespace(rfid_subdir="RFID"))
        self.app.save_config = lambda: None
        saved = dict(web._evt_monitor_state)
        saved_ppl = web._athlete_data
        self.addCleanup(lambda: (web.stop_evt_monitor(), web._evt_monitor_state.update(saved),
                                 setattr(web, "_athlete_data", saved_ppl)))

    def test_first_write_starts_the_monitor_and_panes_follow(self):
        self.import_fixture()
        self.assertEqual(os.path.normcase(web._evt_monitor_state["monitored_dir"]),
                         os.path.normcase(self.rfid))
        self.assertEqual(self.app.config.evt_file, "lynx.evt")
        self.assertTrue(web._athlete_data["loaded"])            # our .ppl, loaded as legacy does
        # meet mode makes the panes itself (no "Races from .evt" step)
        self.assertEqual({(r.event, r.round, r.heat) for r in self.mgr.races.values()},
                         {(1, 1, 1), (2, 1, 1), (3, 1, 1), (4, 1, 1)})
        self.assign_all()
        panes = self.make_panes()
        pane = panes[(1, 1, 1)]
        web._apply_evt_to_panes(self.rfid_file("lynx.evt"), "lynx.evt")   # monitor poll
        n = len(self.mgr.races[pane].entries)
        self.assertGreater(n, 0)
        self.post("/api/meet/athletes/a1/entry", {"race": "1-1-1"})
        # what the monitor does on its next poll
        web._apply_evt_to_panes(self.rfid_file("lynx.evt"), "lynx.evt")
        self.assertEqual(len(self.mgr.races[pane].entries), n + 1)


class TestOperatorLog(MeetApiBase):

    def test_credentials_never_logged(self):
        with self.assertLogs("rfid_finishlynx.web", level="INFO") as logs:
            self.post("/api/meet/milesplit/login", {"email": "timer@example.org",
                                                    "password": "s3cret-pw"})
        text = "\n".join(logs.output)
        self.assertIn("/api/meet/milesplit/login", text)
        self.assertNotIn("s3cret-pw", text)
        self.assertNotIn("timer@example.org", text)
        self.assertIn("(hidden)", text)


class TestBibChangesAfterTheFinish(MeetApiBase):
    """Bibs fixed after runners crossed (2026-10-05, user's questions): a
    race still open follows the bibs at once (results are worked out from
    the reads through the bib each athlete holds now); a final race keeps
    its frozen results until Reopen + Mark final. Plus the walk-up form's
    taken-bib path the console's "Give bib N to ..." dialog uses."""

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.assign_all()
        pane = self.make_panes()[(1, 1, 1)]
        race = self.svc.meet.race(1, 1, 1)
        self.mgr.set_gun(pane, GUN)
        self.ath = [self.svc.meet.athlete(e.athlete_id) for e in race.entries]
        for i, a in enumerate(self.ath[:4]):
            self.mgr.ingest(a.bib, GUN + timedelta(minutes=18, seconds=i), "FINISH")
        for a in self.ath[4:]:
            self.post("/api/meet/races/1-1-1/marks", {"bib": a.bib, "mark": "DNS"})

    def placed(self):
        d = self.get("/api/meet/races/1-1-1/results").get_json()
        return [(f["bib"], f["athlete_id"]) for f in d["results"]["finishers"]]

    def test_swap_in_an_open_race_moves_the_results(self):
        a, b = self.ath[0], self.ath[1]
        bib_a, bib_b = a.bib, b.bib
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        self.assertEqual(self.placed()[:2], [(bib_a, b.id), (bib_b, a.id)])

    def test_swap_in_a_final_race_needs_reopen_and_final(self):
        a, b = self.ath[0], self.ath[1]
        bib_a, bib_b = a.bib, b.bib
        self.post("/api/meet/races/1-1-1/final")
        r = self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        self.assertTrue(any("frozen results still show the old bib" in w for w in r["result"]["warnings"]))
        self.assertEqual(self.placed()[:2], [(bib_a, a.id), (bib_b, b.id)])          # frozen
        self.post("/api/meet/races/1-1-1/reopen")
        self.post("/api/meet/races/1-1-1/final")
        self.assertEqual(self.placed()[:2], [(bib_a, b.id), (bib_b, a.id)])

    def stale(self):
        return [f for f in self.get("/api/meet").get_json()["fix"] if f["kind"] == "stale_final"]

    def test_refinalize_in_one_step(self):
        """Re-finalize = Reopen + Mark final at once, pane final throughout."""
        a, b = self.ath[0], self.ath[1]
        bib_a, bib_b = a.bib, b.bib
        self.post("/api/meet/races/1-1-1/final")
        self.assertEqual(self.stale(), [])
        self.svc._uploads["1-1-1"] = {"uploaded_at": "2026-10-10T12:00:00", "ok": True}
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        st = self.stale()
        self.assertEqual((st[0]["detail"], st[0]["count"]), (["1-1-1"], 2))
        d = self.post("/api/meet/races/1-1-1/refinalize")
        self.assertTrue(d["milesplit_sent"])                   # the console offers Send again
        self.assertEqual(self.placed()[:2], [(bib_a, b.id), (bib_b, a.id)])
        self.assertEqual(self.svc.meet.race(1, 1, 1).status, "final")
        self.assertEqual(self.stale(), [])
        pane = self.make_panes()[(1, 1, 1)]
        self.assertEqual(self.mgr.races[pane].status, "final")

    def test_refinalize_with_a_bib_nobody_has_now(self):
        """2026-10-06: a finish whose bib now belongs to nobody is flagged and
        left out (it no longer refuses the re-finalize); giving the bib back
        from the flag re-finalizes by itself and the old results return."""
        owner = self.ath[0]
        bib = owner.bib
        self.post("/api/meet/races/1-1-1/final")
        before = self.placed()
        self.post(f"/api/meet/athletes/{owner.id}/bib", {"bib": ""})
        self.post("/api/meet/races/1-1-1/refinalize")
        self.assertEqual(self.svc.meet.race(1, 1, 1).status, "final")
        res = self.svc.meet.race(1, 1, 1).results
        self.assertEqual([(i["bib"], i["code"]) for i in res["issues"]], [(bib, "unknown_bib")])
        self.assertNotIn(bib, [b for b, _ in self.placed() if b])
        r = self.post(f"/api/meet/races/1-1-1/issues/{bib}", {"action": "give", "athlete_id": owner.id})
        self.assertEqual(r["report"]["refinalized"], ["1-1-1"])
        self.assertEqual(self.placed(), before)
        self.assertEqual(self.svc.meet.race(1, 1, 1).results["issues"], [])

    def live_first(self):
        """Who MileSplit Live would show in 1st for race 1, with which bib
        (the pusher diffs entries per bib and sends what changed)."""
        p = self.svc._live_source(0)[0]
        e = next(e for e in p["entries"] if e["result"]["place"] == 1)
        return e["id"], f"{e['athlete']['firstName']} {e['athlete']['lastName']}"

    def test_live_follows_bib_fixes(self):
        """An open race: Live has the runner who wore the bib at once. A
        final race: Live keeps each athlete's frozen place (only relabelled
        with their new bib) until it is re-finalized."""
        a, b = self.ath[0], self.ath[1]
        ba = int(a.bib)
        name = lambda x: f"{x.first} {x.last}"
        self.assertEqual(self.live_first(), (ba, name(a)))
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})            # race still open
        self.assertEqual(self.live_first(), (ba, name(b)))
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})            # back
        self.post("/api/meet/races/1-1-1/final")
        self.post("/api/meet/bibs/swap", {"a": a.id, "b": b.id})
        self.assertEqual(self.live_first()[1], name(a))                     # frozen: still a in 1st
        self.post("/api/meet/races/1-1-1/refinalize")
        self.assertEqual(self.live_first(), (ba, name(b)))

    def test_refinalize_only_a_final_race(self):
        self.post("/api/meet/races/1-1-1/refinalize", status=409)

    def test_wrong_kid_wore_the_bib(self):
        owner = self.ath[0]
        bib = owner.bib
        d = self.post(f"/api/meet/bibs/{bib}/wearer", {"new": {"first": "Wrong", "last": "Kid",
                                                                "gender": owner.gender, "team_id": owner.team_id},
                                                        "holder_action": "clear"})
        kid = d["report"]["athlete_id"]
        self.assertEqual(self.placed()[0], (bib, kid))
        self.assertEqual(owner.bib, "")
        self.assertFalse(self.svc.meet.entered(owner.id))
        # a final race must be reopened first (its results are frozen)
        self.post("/api/meet/races/1-1-1/final")
        other = self.ath[1]
        r = self.post(f"/api/meet/bibs/{other.bib}/wearer", {"new": {"first": "Late", "last": "Fix",
                                                                     "gender": other.gender}, "holder_action": "clear"},
                      status=400)
        self.assertIn("reopen it", r["error"])

    def test_walkup_with_a_taken_bib(self):
        """The console catches this 409 (holder named, nothing added) and
        offers to give the bib to the walk-up, leaving the holder without."""
        holder = self.svc.meet.athletes[-1]
        n = len(self.svc.meet.athletes)
        r = self.post("/api/meet/athletes", {"first": "Walk", "last": "Up", "gender": holder.gender,
                                             "bib": holder.bib}, status=409)
        self.assertEqual(r["holder"]["athlete_id"], holder.id)
        self.assertEqual(len(self.svc.meet.athletes), n)
        bib = holder.bib
        d = self.post(f"/api/meet/bibs/{bib}/wearer", {"new": {"first": "Walk", "last": "Up", "gender": holder.gender,
                                                                "team_id": None, "grade": "", "race": None},
                                                        "holder_action": "clear"})
        walkup = self.svc.meet.athlete(d["report"]["athlete_id"])
        holder = self.svc.meet.athlete(holder.id)                # rebuilt by the 409's rollback
        self.assertEqual((walkup.bib, walkup.team_id, holder.bib), (bib, None, ""))
        self.assertFalse(self.svc.meet.entered(walkup.id))       # race left blank on the form
        self.post("/api/meet/undo")
        self.assertEqual(self.svc.meet.athlete(holder.id).bib, bib)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
