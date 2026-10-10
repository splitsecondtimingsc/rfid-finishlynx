"""MileSplit client, Live payloads and the debounced Live pusher, against a fake
transport. Nothing here may touch the network: urlopen is patched to fail
the test if anything reaches it (2026-10-03)."""
import copy
import datetime as dt
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from rfid_finishlynx.meet import milesplit as M
from tests.test_meet_exports import build_meet

T0 = dt.datetime(2026, 10, 10, 8, 0, tzinfo=dt.timezone.utc)


class FakeTransport:
    """Records calls; answers from a list of (match, status, body) rules,
    first rule whose method+substring matches wins; default 200 {}."""

    def __init__(self, rules=()):
        self.rules = list(rules)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body):
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": dict(headers),
                               "body": copy.deepcopy(body)})
        for match, status, resp in self.rules:
            m, sub = match.split(" ", 1)
            if m == method and sub in url:
                return (status(body) if callable(status) else status), \
                       (resp(body) if callable(resp) else copy.deepcopy(resp))
        return 200, {}

    def of(self, method, sub=""):
        return [c for c in self.calls if c["method"] == method and sub in c["url"]]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class NoNetwork(unittest.TestCase):
    def setUp(self):
        p = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_path = os.path.join(self.tmp.name, "sub", "milesplit-token.json")
        self.now = T0

    def client(self, rules=(), logged_in=True):
        self.tr = FakeTransport(rules)
        c = M.MileSplitClient(self.token_path, self.tr, now=lambda: self.now)
        if logged_in:
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as f:
                json.dump({"token30": "tok-abc", "acquired": T0.isoformat()}, f)
        return c


# --------------------------------------------------------------- client

class TestLogin(NoNetwork):
    def test_login_caches_only_the_token(self):
        c = self.client([("POST auth/login", 200, {"data": {"token30": "tok-new"}})], logged_in=False)
        self.assertFalse(c.logged_in())
        with self.assertRaises(M.AuthError):
            c.token()
        exp = c.login("timer@example.org", "s3cret")
        self.assertEqual(exp, T0 + dt.timedelta(days=30))
        self.assertEqual(self.tr.calls[0]["body"], {"email": "timer@example.org", "password": "s3cret"})
        self.assertNotIn("Authorization", self.tr.calls[0]["headers"])
        with open(self.token_path, encoding="utf-8") as f:
            saved = f.read()
        self.assertNotIn("s3cret", saved)
        self.assertNotIn("timer@", saved)
        self.assertEqual(c.token(), "tok-new")
        self.assertTrue(c.logged_in())

    def test_failed_login(self):
        c = self.client([("POST auth/login", 401, {"message": "Invalid credentials"})], logged_in=False)
        with self.assertRaises(M.AuthError) as cm:
            c.login("a@example.org", "x")
        self.assertIn("Invalid credentials", str(cm.exception))
        self.assertFalse(os.path.exists(self.token_path))

    def test_expiry(self):
        c = self.client()
        self.now = T0 + dt.timedelta(days=29, hours=22)
        self.assertEqual(c.token(), "tok-abc")
        self.now = T0 + dt.timedelta(days=29, hours=23, minutes=30)   # inside the 1 h margin
        self.assertFalse(c.logged_in())
        with self.assertRaises(M.AuthError):
            c.token()

    def test_rejected_token_is_dropped(self):
        c = self.client([("GET myall", 401, {"message": "Unauthorized"})])
        with self.assertRaises(M.AuthError):
            c.companies()
        self.assertFalse(os.path.exists(self.token_path))

    def test_corrupt_cache_means_logged_out(self):
        c = self.client(logged_in=False)
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write("{nope")
        self.assertFalse(c.logged_in())

    def test_default_paths(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            self.assertEqual(M.default_token_path(),
                             os.path.join(r"C:\Users\x\AppData\Local", "rfid_finishlynx",
                                          "milesplit-token.json"))
        c = M.MileSplitClient(self.token_path)
        self.assertIs(c.transport, M.urllib_transport)          # constructed, never called


class TestRememberedLogin(NoNetwork):
    """Remember me on this computer (2026-10-03): the login is saved
    encrypted (DPAPI on Windows; a stand-in cipher here) and used to log in
    again when the token runs out or is rejected."""

    def store(self):
        flip = lambda b: bytes(x ^ 0x5A for x in b)          # stand-in for DPAPI
        return M.CredentialStore(os.path.join(self.tmp.name, "sub", "milesplit-login.bin"),
                                 protect=flip, unprotect=flip)

    def remembering_client(self, rules):
        self.tr = FakeTransport(rules)
        return M.MileSplitClient(self.token_path, self.tr, now=lambda: self.now,
                                 credentials=self.store())

    def test_remember_saves_encrypted_and_logout_keeps_it(self):
        c = self.remembering_client([("POST auth/login", 200, {"data": {"token30": "t1"}})])
        c.login("timer@example.org", "s3cret", remember=True)
        with open(c.credentials.path, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"s3cret", raw)
        self.assertNotIn(b"timer@", raw)
        self.assertEqual(c.remembered(), "timer@example.org")
        c.logout()
        self.assertFalse(c.logged_in())
        self.assertEqual(c.token(), "t1")                  # logs itself in again
        self.assertEqual(len(self.tr.of("POST", "auth/login")), 2)
        c.logout(forget=True)
        self.assertIsNone(c.remembered())
        with self.assertRaises(M.AuthError):
            c.token()

    def test_expired_token_relogs_in(self):
        c = self.remembering_client([("POST auth/login", 200, {"data": {"token30": "t2"}})])
        c.login("timer@example.org", "s3cret", remember=True)
        self.now = T0 + dt.timedelta(days=31)
        self.assertEqual(c.token(), "t2")
        self.assertTrue(c.logged_in())

    def test_rejected_token_relogs_in_and_retries_once(self):
        calls = {"n": 0}

        def myall_status(body):
            calls["n"] += 1
            return 401 if calls["n"] == 1 else 200

        c = self.remembering_client([("POST auth/login", 200, {"data": {"token30": "t3"}}),
                                     ("GET myall", myall_status, {"data": [{"id": 1}]})])
        c.login("timer@example.org", "s3cret", remember=True)
        self.assertEqual(c.companies(), [{"id": 1}])
        self.assertEqual(len(self.tr.of("GET", "myall")), 2)

    def test_refused_saved_login_is_forgotten(self):
        ok = {"v": True}
        c = self.remembering_client([("POST auth/login",
                                      lambda b: 200 if ok["v"] else 401,
                                      lambda b: {"data": {"token30": "t4"}} if ok["v"]
                                      else {"message": "Invalid credentials"})])
        c.login("timer@example.org", "old-pw", remember=True)
        ok["v"] = False                                     # password changed on MileSplit
        c.logout()
        with self.assertRaises(M.AuthError):
            c.token()
        self.assertIsNone(c.remembered())

    def test_remember_false_forgets_and_unreadable_file_is_ignored(self):
        c = self.remembering_client([("POST auth/login", 200, {"data": {"token30": "t5"}})])
        c.login("a@example.org", "pw", remember=True)
        c.login("a@example.org", "pw", remember=False)
        self.assertIsNone(c.remembered())
        with open(c.credentials.path, "wb") as f:
            f.write(b"\x00garbage")
        self.assertIsNone(c.remembered())

    def test_dpapi_round_trip_on_windows(self):
        if os.name != "nt":
            self.skipTest("DPAPI is Windows only")
        st = M.CredentialStore(os.path.join(self.tmp.name, "real.bin"))
        st.save("a@example.org", "pw")
        self.assertEqual(st.load(), ("a@example.org", "pw"))


class TestReads(NoNetwork):
    def test_routes_and_unwrap(self):
        c = self.client([
            ("GET timing-company/myall", 200, {"data": [{"id": 700, "name": "Test Timing"}]}),
            ("GET /meets?", 200, {"data": [{"meetId": 800100}]}),
            ("GET /entries", 200, {"data": [{"teamName": "Ridge", "roster": []}]}),
            ("GET team-schedule/attending/800100", 200, {"data": [{"id": 9001}, {"id": 9002}]}),
            ("GET team-rosters", 200, {"data": {"9001": []}}),
        ])
        self.assertEqual(c.companies(), [{"id": 700, "name": "Test Timing"}])
        self.assertEqual(c.meets(700, 10, 2026), [{"meetId": 800100}])
        self.assertEqual(c.entries(700, 800100), [{"teamName": "Ridge", "roster": []}])
        self.assertEqual(c.attending(800100), [{"id": 9001}, {"id": 9002}])
        self.assertEqual(c.team_rosters(700, 800100, [9001, 9002]),
                         [{"teamMileSplitId": 9001, "teamName": "", "roster": []}])
        self.assertEqual(c.team_rosters(700, 800100, []), [])
        urls = [x["url"] for x in self.tr.calls]
        self.assertEqual(urls, [
            M.API_ROOT + "timing-company/myall",
            M.API_ROOT + "timing-company/700/meets?month=10&year=2026",
            M.API_ROOT + "timing-company/700/meets/800100/entries",
            M.API_ROOT + "team-schedule/attending/800100",
            M.API_ROOT + "timing-company/700/meets/800100/team-rosters?teamIds=9001,9002"])
        self.assertTrue(all(x["headers"]["Authorization"] == "Bearer tok-abc" for x in self.tr.calls))

    def test_team_rosters_a_few_teams_per_request(self):
        # 2026-10-08: 97 teams in one request -> HTTP 500, 25-50 -> athletes silently missing
        busy = iter([True])

        def status(body):
            return 429 if next(busy, False) else 200

        def answer(body):
            url = self.tr.calls[-1]["url"]
            ids = url.split("teamIds=")[1].split(",")
            return {"data": {t: {"u" + t: {"athlete_id": int(t), "athlete": {"id": int(t), "first_name": "A",
                                                                              "last_name": t, "gender": "F"}}}
                             for t in ids}}
        c = self.client([("GET team-rosters", status, answer)])
        waits = []
        teams = c.team_rosters(700, 800100, range(1, 13), sleep=waits.append)
        self.assertEqual([t["teamMileSplitId"] for t in teams], list(range(1, 13)))
        self.assertEqual([len(t["roster"]) for t in teams], [1] * 12)
        asked = [x["url"].split("teamIds=")[1] for x in self.tr.of("GET", "team-rosters")]
        self.assertEqual(asked, ["1,2,3,4,5", "1,2,3,4,5", "6,7,8,9,10", "11,12"])   # the first one again after a 429
        self.assertEqual(waits, [2.0])

    def test_http_error(self):
        c = self.client([("GET entries", 500, "Internal error")])
        with self.assertRaises(M.ApiError) as cm:
            c.entries(700, 1)
        self.assertEqual(cm.exception.status, 500)

    def test_unwrap(self):
        self.assertEqual(M.unwrap({"data": [1], "message": "x"}), [1])
        self.assertEqual(M.unwrap([1]), [1])
        big = {"data": 1, "a": 1, "b": 2, "c": 3}
        self.assertIs(M.unwrap(big), big)


class TestUpload(NoNetwork):
    PAYLOAD = {"meet_id": 800100, "teams": [], "_match": {"x": 1}}

    def test_201_with_error_body_is_a_failure(self):
        c = self.client([("POST mpResults", 201, {
            "status": "error", "statusMessage": "At least 50% of athletes and teams must have valid IDs.",
            "resultsURL": ""})])
        r = c.upload_results(700, 800100, self.PAYLOAD)
        self.assertFalse(r.ok)
        self.assertEqual(r.http_status, 201)
        self.assertIn("50%", r.message)
        self.assertEqual(self.tr.calls[0]["url"],
                         M.API_ROOT + "timing-company/700/meets/800100/mpResults")
        self.assertNotIn("_match", self.tr.calls[0]["body"])

    def test_success(self):
        c = self.client([("POST mpResults", 201, {
            "status": "Success", "statusMessage": "Meet Results Imported.",
            "resultsURL": "https://example.invalid/meets/800100/results/1"})])
        r = c.upload_results(700, 800100, self.PAYLOAD)
        self.assertTrue(r.ok)
        self.assertEqual(r.message, "Meet Results Imported.")
        self.assertTrue(r.results_url.endswith("/results/1"))

    def test_http_failure_and_text_body(self):
        c = self.client([("POST mpResults", 502, "<html>Bad gateway</html>")])
        r = c.upload_results(700, 800100, self.PAYLOAD)
        self.assertFalse(r.ok)
        self.assertIn("Bad gateway", r.message)

    def test_wrong_meet_refused(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.upload_results(700, 1, self.PAYLOAD)
        self.assertEqual(self.tr.calls, [])


class TestLiveCalls(NoNetwork):
    def test_delete_is_refused(self):
        c = self.client()
        for method in ("DELETE", "delete", "POST", "PUT"):
            with self.assertRaises(M.MileSplitError):
                c.live(method, "/meets/800100/entries")
        self.assertEqual(self.tr.calls, [])

    def test_batches_and_err_body(self):
        c = self.client([("PATCH /entries", 202, {"statusCode": 202, "message": "Entry changes accepted."}),
                         ("PATCH /events", 200, {"err": True, "error": {"value": "bad event"}})])
        rs = c.live_entries(800100, [{"id": i} for i in range(1000)])
        self.assertEqual([len(x["body"]) for x in self.tr.of("PATCH", "/entries")], [495, 495, 10])
        self.assertTrue(all(r.ok for r in rs))
        r = c.live_events(800100, [])
        self.assertFalse(r.ok)
        self.assertEqual(r.message, "bad event")
        self.assertEqual(self.tr.calls[0]["url"], M.LIVE_ROOT + "/meets/800100/entries")


# --------------------------------------------------------------- Live payloads

def entry_by_bib(payload, bib):
    return next(e for e in payload["entries"] if e["id"] == bib)


class TestLivePayload(unittest.TestCase):
    def setUp(self):
        self.meet = build_meet()

    def test_entry_shape(self):
        p = M.live_race_payload(self.meet, self.meet.races[0], epoch=123)
        self.assertEqual(p["events"], [{"number": 1, "rounds": {"F": {"status": "complete"}}}])
        e = entry_by_bib(p, 101)
        self.assertEqual(e, {
            "id": 101, "event": 1, "round": "F", "heat": 1, "exhibition": False,
            "athlete": {"id": 101, "firstName": "Avery", "lastName": "Alder", "teamId": 9001,
                        "mileSplitId": 5000001, "sex": "m", "grade": "12",
                        "team": {"abbreviation": "RID", "name": "Ridge High School", "number": 9001,
                                 "division": {"number": 0, "name": None},
                                 "logoUrl": "https://assets.sp.milesplit.com/team_logos/9001?v=123"},
                        "compNo": 101, "exhibition": None},
            "result": {"place": 1, "units": 983.52, "text": "16:23.52", "teamPoints": 1,
                       "isHand": False}})

    def test_flags_and_non_finishers(self):
        p = M.live_race_payload(self.meet, self.meet.races[0])
        exh = entry_by_bib(p, 115)
        self.assertTrue(exh["exhibition"])
        self.assertEqual(exh["athlete"]["exhibition"], "EXH")
        self.assertEqual(exh["result"]["teamPoints"], 0)
        self.assertEqual(entry_by_bib(p, 106)["result"]["teamPoints"], 11)       # displacer
        una = entry_by_bib(p, 16)
        self.assertEqual((una["athlete"]["team"]["abbreviation"], una["athlete"]["teamId"],
                          una["athlete"]["team"]["logoUrl"]), ("UNAT", 0, ""))
        self.assertEqual(entry_by_bib(p, 16)["result"]["units"], 1060.45)
        self.assertEqual(len(p["entries"]), 15)
        self.assertEqual(p["withdrawn"], {206: "DNF", 303: "DQ", 302: "DNF", 304: "DNS"})

    def test_team_scores(self):
        p = M.live_race_payload(self.meet, self.meet.races[0])
        [g] = p["teamScores"]
        self.assertEqual((g["name"], g["gender"], g["divisionNo"], g["eventNo"]),
                         ("Boys 5k Run CC Varsity ", {"sex": "M", "name": "Men"}, 0, 1))
        self.assertEqual(sorted(g["scores"]), ["1", "2", "3"])        # Lakeside doesn't score
        ridge = g["scores"]["1"]
        self.assertEqual((ridge["place"], ridge["points"]), (1, 24))
        self.assertEqual([(x["place"], x["isScorer"], x["isDisplacer"]) for x in ridge["entries"]],
                         [(1, True, False), (3, True, False), (5, True, False), (7, True, False),
                          (8, True, False), (11, False, True)])          # exhibition a15 left out
        self.assertEqual(ridge["team"]["mileSplitId"], 9001)
        summit = g["scores"]["3"]
        self.assertEqual((summit["place"], summit["points"]), (0, 0))   # short team
        self.assertEqual(summit["entries"][0]["points"], 0)

    def test_combined_start_two_events(self):
        p = M.live_race_payload(self.meet, self.meet.races[1])
        self.assertEqual([e["number"] for e in p["events"]], [2, 4])
        self.assertEqual([(e["id"], e["event"], e["result"]["place"]) for e in p["entries"]],
                         [(120, 2, 1), (320, 4, 1), (121, 2, 2), (220, 4, 2)])
        self.assertEqual(sorted(g["eventNo"] for g in p["teamScores"]), [2, 4])

    def test_live_unfrozen_results_and_reopen(self):
        race = self.meet.races[2]
        live = {"frozen_at": None, "teams": {},
                "finishers": [{"place": 1, "bib": "130", "athlete_id": "a24", "event": 3,
                               "elapsed": 1200.0, "status": "finished", "event_place": 1,
                               "points": None, "scorer": False, "displacer": False}]}
        self.assertEqual(M.live_race_payload(self.meet, race)["events"], [])   # nothing yet
        p = M.live_race_payload(self.meet, race, live)
        self.assertEqual(p["events"], [{"number": 3, "rounds": {"F": {"status": "live"}}}])
        self.assertEqual(entry_by_bib(p, 130)["result"]["text"], "20:00.00")
        # A reopened race goes back to provisional results and "live".
        self.meet.races[0].status = "scheduled"
        ps = M.live_payloads(self.meet, live_results=lambda r: live if r.event == 3 else
                             (r.results if r.event == 1 else None))
        self.assertEqual(ps[0]["events"][0]["rounds"]["F"]["status"], "live")
        self.assertEqual(ps[1]["events"][0]["rounds"]["F"]["status"], "complete")
        self.assertEqual(ps[2]["events"][0]["rounds"]["F"]["status"], "live")

    def test_perf_text_and_logo(self):
        self.assertEqual(M.perf_text(1052.94), "17:32.94")
        self.assertEqual(M.perf_text(3723.456), "1:02:03.46")
        self.assertEqual(M.perf_text(59.999), "1:00.00")
        self.assertEqual(M.logo(0, 5), "")
        self.assertEqual(M.logo(1, 5), "")


# --------------------------------------------------------------- pusher

class TestPusher(NoNetwork):
    ACCEPT = [("PATCH /entries", 202, {"statusCode": 202, "message": "Entry changes accepted."}),
              ("PATCH /events", 202, {"statusCode": 202, "message": "Event changes accepted."}),
              ("PATCH /teamScores", 202, {"statusCode": 202, "message": "Team score changes accepted."})]

    def setUp(self):
        super().setUp()
        self.meet = build_meet()
        self.clock = Clock()
        self.epoch = 1
        self.c = self.client(self.ACCEPT)
        self.p = M.LivePusher(self.c, 800100, self.source, settle=5, max_wait=15,
                              backoff_base=5, backoff_max=40, clock=self.clock)

    def source(self):
        self.epoch += 1         # a fresh logo cache-buster every rebuild must not count as a change
        return M.live_payloads(self.meet, races=self.meet.races[:2], epoch=self.epoch)

    def test_settle_then_push_everything_once(self):
        self.assertIsNone(self.p.tick())                 # nothing pending
        self.p.notify()
        self.clock.t += 4
        self.assertIsNone(self.p.tick())                 # not settled
        self.clock.t += 1
        r = self.p.tick()
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.entries, 19)
        self.assertEqual(sorted(r.statuses), [(1, "complete"), (2, "complete"), (4, "complete")])
        self.assertEqual(sorted(r.team_scores), [1, 2, 4])
        n = len(self.tr.calls)
        # Same results again: nothing sent, not even a diff.
        self.p.notify()
        self.clock.t += 5
        r = self.p.tick()
        self.assertEqual(r.skipped, "unchanged")
        self.assertEqual(len(self.tr.calls), n)

    def test_max_wait_while_results_keep_changing(self):
        self.p.notify()
        for _ in range(8):
            self.clock.t += 2
            self.p.notify()
            if self.clock.t - 1000 < 15:
                self.assertIsNone(self.p.tick())
        self.assertIsNotNone(self.p.tick())              # 16 s after the first change

    def test_only_changed_runners_are_sent(self):
        self.p.flush()
        fin = self.meet.races[1].results["finishers"]
        fin[0]["elapsed"] = 1099.0                       # one time corrected
        self.p.notify()
        self.clock.t += 5
        r = self.p.tick()
        self.assertEqual(r.entries, 1)
        self.assertEqual(r.statuses, [])
        last = self.tr.of("PATCH", "/entries")[-1]["body"]
        self.assertEqual([e["id"] for e in last], [120])

    def test_withdrawn_runner_is_resent_without_a_place(self):
        self.p.flush()
        fin = self.meet.races[1].results["finishers"]
        fin[3].update(status="DQ", place=None, elapsed=None, event_place=None)
        r = self.p.flush()
        self.assertEqual(r.withdrawn, 1)
        sent = self.tr.of("PATCH", "/entries")[-1]["body"]
        self.assertEqual([(e["id"], e["result"]["place"], e["result"]["status"]) for e in sent],
                         [(220, 0, "DQ")])
        r = self.p.flush()                               # not withdrawn twice
        self.assertIn(r.skipped, ("unchanged", "nothing new"))

    def test_back_off_on_errors_then_retry(self):
        state = {"fail": True}
        self.tr.rules.insert(0, ("PATCH /entries", lambda b: 503 if state["fail"] else 202,
                                 {"statusCode": 503, "message": "unavailable"}))
        self.p.notify()
        self.clock.t += 5
        r = self.p.tick()
        self.assertFalse(r.ok)
        self.assertIn("HTTP 503", r.errors[0])
        self.assertEqual(self.p.status()["errors"], 1)
        self.clock.t += 4
        self.assertIsNone(self.p.tick())                 # backing off 5 s
        self.clock.t += 1
        self.assertFalse(self.p.tick().ok)               # second failure: 10 s
        self.clock.t += 9
        self.assertIsNone(self.p.tick())
        state["fail"] = False
        self.clock.t += 1
        r = self.p.tick()
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.entries, 19)                  # the whole diff, nothing lost
        st = self.p.status()
        self.assertEqual((st["errors"], st["last_error"], st["pending"]), (0, "", False))

    def test_back_off_is_capped(self):
        self.tr.rules.insert(0, ("PATCH /entries", 500, {"message": "down"}))
        for _ in range(6):
            self.p.flush()
            self.clock.t += 1000
        self.p.flush()
        self.assertLessEqual(self.p.status()["retry_in"], 40)

    def test_events_and_team_scores_failing_are_retried_alone(self):
        self.tr.rules.insert(0, ("PATCH /teamScores", 500, {"message": "down"}))
        r = self.p.flush()
        self.assertFalse(r.ok)
        self.tr.rules.pop(0)
        self.clock.t += 10
        r = self.p.tick()
        self.assertTrue(r.ok)
        self.assertEqual((r.entries, r.statuses), (0, []))
        self.assertEqual(sorted(r.team_scores), [1, 2, 4])

    def test_auth_error_backs_off_without_crashing(self):
        os.remove(self.token_path)
        r = self.p.flush()
        self.assertFalse(r.ok)
        self.assertIn("not logged in", r.errors[0])

    def test_one_push_in_flight(self):
        gate, entered = threading.Event(), threading.Event()

        def slow(_body):
            entered.set()
            gate.wait(5)
            return 202
        self.tr.rules.insert(0, ("PATCH /entries", slow, {"statusCode": 202}))
        self.p.notify()
        self.clock.t += 5
        t = threading.Thread(target=self.p.tick)
        t.start()
        self.assertTrue(entered.wait(5))
        self.assertTrue(self.p.status()["in_flight"])
        self.p.notify()                                  # a change during the push
        self.clock.t += 20
        self.assertIsNone(self.p.tick())                 # refused: one already in flight
        gate.set()
        t.join(5)
        self.assertFalse(self.p.status()["in_flight"])
        self.assertTrue(self.p.status()["pending"])     # the change made mid-push is kept
        self.assertEqual(len(self.tr.of("PATCH", "/entries")), 1)

    def test_notify_is_thread_safe(self):
        threads = [threading.Thread(target=lambda: [self.p.notify() for _ in range(200)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(self.p.status()["pending"])

    def test_never_deletes(self):
        self.p.flush()
        self.meet.races[1].results["finishers"].pop()
        self.p.flush()
        self.assertEqual([c for c in self.tr.calls if c["method"] not in ("PATCH",)], [])

    def test_background_thread_starts_and_stops(self):
        p = M.LivePusher(self.c, 800100, lambda: [], clock=self.clock)
        p.start(interval=0.01)
        p.stop()
        self.assertIsNone(p._thread)


# --------------------------------------------------------------- start times

FIRST_FINISHER = {"frozen_at": None, "teams": {},
                  "finishers": [{"place": 1, "bib": "130", "athlete_id": "a24", "event": 3,
                                 "elapsed": 1200.0, "status": "finished", "event_place": 1,
                                 "points": None, "scorer": False, "displacer": False}]}


class TestLiveSchedulePayload(unittest.TestCase):
    """live_schedule_payload (2026-10-03). The wire format is UNVERIFIED:
    these tests pin what we send, not what Live accepts."""

    def setUp(self):
        self.meet = build_meet()
        for race, t in zip(self.meet.races, ("08:00", "08:30", "09:30")):
            race.start_time = t

    def test_only_upcoming_races_and_never_a_status(self):
        # races 1 and 2 are final (complete on Live): their times are not sent
        self.assertEqual(M.live_schedule_payload(self.meet),
                         [{"number": 3, "rounds": {"F": {"session": {"time": "9:30 AM"}}}}])
        self.assertNotIn("status", json.dumps(M.live_schedule_payload(self.meet)))

    def test_started_race_is_left_out(self):
        live = lambda r: FIRST_FINISHER if r.event == 3 else None    # noqa: E731
        self.assertEqual(M.live_schedule_payload(self.meet, live_results=live), [])
        # a reopened race goes back to "live", never to upcoming
        self.meet.races[0].status = "scheduled"
        self.assertEqual(M.live_schedule_payload(self.meet, [self.meet.races[0]],
                                                 live_results=lambda r: r.results), [])

    def test_one_entry_per_scoring_event_of_a_combined_start(self):
        race = self.meet.races[1]
        race.status, race.results = "scheduled", None
        out = M.live_schedule_payload(self.meet, [race])
        self.assertEqual([(e["number"], M.live_start_time(e)) for e in out],
                         [(2, "8:30 AM"), (4, "8:30 AM")])

    def test_blank_times(self):
        self.meet.races[2].start_time = ""
        self.assertEqual(M.live_schedule_payload(self.meet), [])
        self.assertEqual(M.live_schedule_payload(self.meet, include_blank=True),
                         [{"number": 3, "rounds": {"F": {"session": {"time": ""}}}}])

    def test_switch_and_field_path_are_module_constants(self):
        with mock.patch.object(M, "LIVE_SEND_START_TIMES", False):
            self.assertEqual(M.live_schedule_payload(self.meet), [])
            self.assertEqual(M.live_payloads(self.meet)[2]["schedule"], [])
        with mock.patch.object(M, "LIVE_START_TIME_PATH", ("rounds", "F", "startTime")):
            [e] = M.live_schedule_payload(self.meet)
            self.assertEqual(e, {"number": 3, "rounds": {"F": {"startTime": "9:30 AM"}}})
            self.assertEqual(M.live_start_time(e), "9:30 AM")

    def test_live_payloads_carry_the_schedule(self):
        ps = M.live_payloads(self.meet)
        self.assertEqual([p["schedule"] for p in ps[:2]], [[], []])
        self.assertEqual(M.live_start_time(ps[2]["schedule"][0]), "9:30 AM")
        self.assertEqual(ps[2]["events"], [])                # still no status: upcoming


class TestPusherStartTimes(NoNetwork):
    def setUp(self):
        super().setUp()
        self.meet = build_meet()
        self.meet.races[2].start_time = "09:30"
        self.live3 = None                                   # race 3's provisional results
        self.refuse = False
        self.clock = Clock()

        def events_status(body):
            return 400 if self.refuse and "session" in json.dumps(body) else 202
        self.c = self.client([("PATCH /events", events_status,
                               lambda b: {"statusCode": 202, "message": "Event changes accepted."}
                               if not (self.refuse and "session" in json.dumps(b)) else
                               {"statusCode": 400, "message": "bad field"})]
                             + TestPusher.ACCEPT)
        self.p = M.LivePusher(self.c, 800100, self.source, clock=self.clock, backoff_base=5)

    def source(self):
        return M.live_payloads(self.meet, live_results=lambda r: self.live3 if r.event == 3 else None)

    def times_calls(self):
        return [c["body"] for c in self.tr.of("PATCH", "/events") if "session" in json.dumps(c["body"])]

    def test_sent_once_in_its_own_patch_after_the_statuses(self):
        r = self.p.flush()
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.start_times, [(3, "9:30 AM")])
        events = self.tr.of("PATCH", "/events")
        self.assertEqual(len(events), 2)
        self.assertEqual({e["number"] for e in events[0]["body"]}, {1, 2, 4})   # statuses first
        self.assertEqual(events[1]["body"],
                         [{"number": 3, "rounds": {"F": {"session": {"time": "9:30 AM"}}}}])
        self.assertEqual(self.p.status()["start_times"], 1)
        n = len(self.tr.calls)
        self.assertIn(self.p.flush().skipped, ("unchanged", "nothing new"))
        self.assertEqual(len(self.tr.calls), n)

    def test_changed_then_cleared(self):
        self.p.flush()
        self.meet.races[2].start_time = "09:45"
        r = self.p.flush()
        self.assertEqual((r.entries, r.statuses, r.start_times), (0, [], [(3, "9:45 AM")]))
        self.meet.races[2].start_time = ""
        self.assertEqual(self.p.flush().start_times, [(3, "")])       # cleared once
        self.assertIn(self.p.flush().skipped, ("unchanged", "nothing new"))
        self.assertEqual(len(self.times_calls()), 3)

    def test_blank_never_sent_unless_clearing(self):
        self.meet.races[2].start_time = ""
        self.p.flush()
        self.assertEqual(self.times_calls(), [])

    def test_started_race_gets_its_status_not_a_time(self):
        self.p.flush()
        self.live3 = FIRST_FINISHER
        r = self.p.flush()
        self.assertEqual((r.statuses, r.start_times), ([(3, "live")], []))
        self.assertEqual(len(self.times_calls()), 1)

    def test_refused_times_never_hold_up_results(self):
        self.refuse = True
        r = self.p.flush()
        self.assertTrue(r.ok, r.errors)                     # the push itself is fine
        self.assertEqual(r.errors, [])
        self.assertIn("bad field", r.start_time_error)
        st = self.p.status()
        self.assertEqual((st["errors"], st["retry_in"]), (0, 0.0))        # no back-off
        self.assertIn("start times", st["start_time_error"])
        # the same body is not retried on every push...
        self.meet.races[0].results["finishers"][0]["elapsed"] = 983.0
        self.p.flush()
        self.assertEqual(len(self.times_calls()), 1)
        # ...only when the times change (or after a manual send)
        self.refuse = False
        self.meet.races[2].start_time = "10:00"
        self.assertEqual(self.p.flush().start_times, [(3, "10:00 AM")])
        self.assertEqual(self.p.status()["start_time_error"], "")

    def test_manual_send_is_noted(self):
        self.p.note_start_times(M.live_schedule_payload(self.meet))
        self.p.flush()
        self.assertEqual(self.times_calls(), [])


if __name__ == "__main__":
    unittest.main()
