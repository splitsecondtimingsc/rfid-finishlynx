"""MeetService internals the REST tests don't reach: the lock design
(athlete_lookup never blocks, RaceManager is never called under the meet
lock), validation rollback, the athlete view contract, and the
Application hooks (2026-10-03). python -m unittest tests.test_meet_service"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace

from rfid_finishlynx.app import Application
from rfid_finishlynx.meet import edits
from rfid_finishlynx.meet_service import MeetError, MeetService, athlete_view, parse_rk

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")


class ServiceBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsvc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = SimpleNamespace(race_manager=None, hardware_allowed=False,
                                   lif_dirs=lambda: ("", ""), config=SimpleNamespace(event_directory=""))
        self.emitted = []
        self.svc = MeetService(os.path.join(self.tmp, "meet"), self.app,
                               token_path=os.path.join(self.tmp, "tok.json"),
                               transport=lambda *a: (500, None), emit=self.emitted.append,
                               autoload_evt=lambda d: None)
        with open(FIXTURE, encoding="utf-8") as f:
            self.svc.import_milesplit(json.load(f), name="Svc", day="2026-10-10")
        self.svc.assign({"rows": [["100", ""]]})


class TestLocks(ServiceBase):

    def test_athlete_lookup_never_waits_for_the_meet_lock(self):
        bib = self.svc.meet.athletes[0].bib
        got = []
        with self.svc._lock:                       # another thread mid-edit
            t = threading.Thread(target=lambda: got.append(self.svc.athlete_lookup(bib)))
            t.start()
            t.join(2)
        self.assertFalse(t.is_alive(), "athlete_lookup blocked on the meet lock")
        self.assertEqual(got[0]["last_name"], self.svc.meet.athletes[0].last)
        self.assertIsNone(self.svc.athlete_lookup("98765"))
        self.assertIsNone(self.svc.athlete_lookup("abc"))

    def test_racemanager_is_never_called_under_the_meet_lock(self):
        self.app.race_manager = SimpleNamespace(_lock=threading.RLock(), order=[], races={})
        self.assertEqual(self.svc._pane_links(), {})
        with self.svc._lock:
            with self.assertRaises(RuntimeError):
                self.svc._pane_links()

    def test_on_races_changed_drops_cached_results(self):
        self.svc._live_cache[(1, 1, 1)] = {"pane": 3, "results": None, "unresolved": []}
        self.svc._live_cache[(2, 1, 1)] = {"pane": 4, "results": None, "unresolved": []}
        self.svc.on_races_changed([3])
        self.assertEqual(list(self.svc._live_cache), [(2, 1, 1)])
        self.svc.on_races_changed([])
        self.assertEqual(self.svc._live_cache, {})


class TestMutations(ServiceBase):

    def test_a_change_that_breaks_the_meet_is_rolled_back(self):
        a, b = self.svc.meet.athletes[:2]
        before = (a.bib, b.bib)
        saved = os.path.getmtime(self.svc.store.path(self.svc.meet.meet.id))

        def dup(meet):
            meet.athletes[1].bib = meet.athletes[0].bib
        with self.assertRaises(MeetError) as cm:
            self.svc._mutate(dup)
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual((self.svc.meet.athletes[0].bib, self.svc.meet.athletes[1].bib), before)
        self.assertEqual(os.path.getmtime(self.svc.store.path(self.svc.meet.meet.id)), saved)
        self.assertEqual(self.svc.athlete_lookup(before[1])["last_name"], b.last)

    def test_preview_never_writes_start_lists(self):
        s = self.svc.summary()
        self.assertEqual(s["start_lists"]["skipped"], "preview")
        self.assertTrue(s["preview"])

    def test_view_matches_edits_view(self):
        meet = self.svc.meet
        for a in meet.athletes[:20]:
            v = athlete_view(meet, a)
            base = edits._view(meet, a)
            self.assertEqual({k: v[k] for k in base}, base)
            self.assertIn(v["status"], ("entered", "scratched", "exhibition", "roster"))

    def test_start_times_reach_the_live_source_and_nudge_the_pusher(self):
        """A start-time edit doesn't rewrite FinishLynx's files but does reach
        MileSplit Live's next push (2026-10-03)."""
        nudged = []
        self.svc._pusher = SimpleNamespace(notify=lambda: nudged.append(1),
                                           stop=lambda timeout=0: None, status=lambda: {})
        self.addCleanup(setattr, self.svc, "_pusher", None)
        rk = "-".join(map(str, self.svc.meet.races_in_order()[0].key))
        self.svc.update_race(rk, {"start_time": "8:15 am"})
        self.assertEqual(nudged, [1])
        payloads = self.svc._live_source(epoch=1)
        sched = [e for p in payloads for e in p["schedule"]]
        self.assertTrue(sched)
        self.assertIn("8:15 AM", json.dumps(sched))
        with self.assertRaises(MeetError) as cm:
            self.svc.update_race(rk, {"start_time": "8:75"})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.svc.meet.race(*parse_rk(rk)).start_time, "08:15")

    def test_fill_and_cancel_without_a_race_manager(self):
        n, _ = self.svc.fill_start_times({"first": "08:00", "every_min": 20})
        self.assertEqual(n, len(self.svc.meet.races))
        self.assertEqual([r.start_time for r in self.svc.meet.races_in_order()][:2],
                         ["08:00", "08:20"])
        rk, _ = self.svc.add_race({"new": {"name": "Open"}})
        self.svc.cancel_race(rk)                       # no panes to check: it just goes
        self.assertIsNone(self.svc.meet.race(*parse_rk(rk)))

    def test_parse_rk(self):
        self.assertEqual(parse_rk("3-1-2"), (3, 1, 2))
        self.assertEqual(parse_rk("3"), (3, 1, 1))
        with self.assertRaises(MeetError):
            parse_rk("3-x")


class TestApplicationHooks(unittest.TestCase):

    def test_class_default_and_race_change_hook(self):
        app = Application.__new__(Application)
        self.assertIsNone(app.meet_service)
        app.race_manager = None
        app.publishing = False
        app._broadcast_gun = lambda: None
        app._on_races_changed([1])                 # legacy: nothing to call
        seen = []
        app.meet_service = SimpleNamespace(on_races_changed=seen.append)
        app._on_races_changed([2, 3])
        self.assertEqual(seen, [[2, 3]])


if __name__ == "__main__":
    unittest.main()


class TestOpenPath(ServiceBase):
    """Open another… → Browse… (2026-10-03): a meet.json from anywhere."""

    def test_store_copy_just_opens(self):
        mid = self.svc.meet.meet.id
        self.svc.open_path(os.path.join(self.tmp, "meet", mid))          # the folder works too
        self.assertEqual(self.svc.meet.meet.id, mid)
        self.assertEqual(self.svc.store.list(), [mid])

    def test_outside_meet_is_copied_in_never_overwriting(self):
        mid = self.svc.meet.meet.id
        outside = os.path.join(self.tmp, "usb", "meet")
        shutil.copytree(os.path.join(self.tmp, "meet", mid), outside)
        self.svc.open_path(os.path.join(outside, "meet.json"))
        self.assertEqual(self.svc.meet.meet.id, mid + "-copy")          # id was taken
        self.svc.open_path(os.path.join(outside, "meet.json"))
        self.assertEqual(self.svc.meet.meet.id, mid + "-copy2")
        self.assertEqual(sorted(self.svc.store.list()), sorted([mid, mid + "-copy", mid + "-copy2"]))
        self.assertEqual(self.svc.store.load(mid).meet.id, mid)         # original untouched

    def test_not_a_meet(self):
        bad = os.path.join(self.tmp, "x.json")
        with open(bad, "w") as f:
            f.write("[1, 2]")
        for path in (bad, os.path.join(self.tmp, "nothing-here")):
            with self.assertRaises(MeetError):
                self.svc.open_path(path)


class TestMonthSetupPages(ServiceBase):
    """Listing a month reads each meet's website Setup page (2026-10-03)."""

    def test_attaches_setup_or_says_why_not(self):
        from tests.test_milesplit_web import SETUP

        class FakeWeb:
            has_login, logins, gets = True, [], []

            def login(self, host):
                self.logins.append(host)

            def get(self, host, path):
                self.gets.append(path)
                if "/2/" in path:
                    raise milesplit_web.WebError("no access")
                return SETUP

            def remember(self, *a):
                pass
        from rfid_finishlynx.meet import milesplit_web
        self.svc._web = web = FakeWeb()
        meets = [{"meetId": 1, "meetUrl": "https://sc.milesplit.com/meets/1-x"},
                 {"meetId": 2, "meetUrl": "https://sc.milesplit.com/meets/2-y"},
                 {"meetId": 3, "meetUrl": "https://example.com/3"}]
        self.assertEqual(self.svc._attach_setup(meets), {"ok": True})
        self.assertEqual(web.logins, ["sc.milesplit.com"])
        self.assertEqual(meets[0]["setup"]["entries_close"], "2026-09-29 9:00 PM")
        self.assertEqual(meets[1]["setup_error"], "no access")
        self.assertIn("setup_error", meets[2])
        web.has_login = False
        self.assertFalse(self.svc._attach_setup([{"meetId": 1}])["ok"])


class TestMilesplitName(ServiceBase):
    """A meet pulled by bare id takes MileSplit's name on refresh (2026-10-03)."""

    def test_refresh_renames_only_the_default_name(self):
        with open(FIXTURE, encoding="utf-8") as f:
            entries = json.load(f)
        self.svc.set_info({"name": "MileSplit meet 5", "milesplit": {"meet_id": 5}})
        self.svc.import_milesplit(entries, name="Woodmont XC Invitational", host="Woodmont",
                                  location="Woodmont High School, Piedmont")
        info = self.svc.meet.meet
        self.assertEqual((info.name, info.host), ("Woodmont XC Invitational", "Woodmont"))
        self.svc.import_milesplit(entries, name="Something Else")
        self.assertEqual(self.svc.meet.meet.name, "Woodmont XC Invitational")
