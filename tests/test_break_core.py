"""Fixes from the break tests (2026-10-13): reads, starts, held reads,
routes. Ported from the camera branch without its standalone-only parts.
python -m pytest tests/test_break_core.py"""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 8, 10, 0, 0)
ATH = {str(b): {"gender": "M" if b % 2 else "F", "last_name": f"R{b}"} for b in range(100, 140)}


def mgr():
    return RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: ATH.get(b, {}))


def where(m, bib):
    r = sorted((x for x in m.reads.values() if x.bib == bib), key=lambda x: x.id)
    return [(x.state, x.race) for x in r]


class StartMoved(unittest.TestCase):

    def test_a_later_start_drops_impossible_finishes_and_the_real_one_counts(self):
        m = mgr()
        k = m.create_race("Boys", event=2, gender="M")
        m.races[k].entries = {"101"}
        m.races[k].roster.add("101")
        m.set_gun(k, GUN)
        m.ingest("101", GUN + timedelta(minutes=14), "FINISH")          # crossing near the start
        m.ingest("101", GUN + timedelta(minutes=33), "FINISH")          # the real finish (ignored as a repeat now)
        m.set_gun(k, GUN + timedelta(minutes=15))                       # the real start
        fin = next(r for r in m.results(k)["finishers"] if r["bib"] == "101")
        self.assertAlmostEqual(fin["finish"]["elapsed"], 18 * 60, places=3)

    def test_a_longer_distance_does_the_same(self):
        m = mgr()
        k = m.create_race("Boys", event=2, gender="M", distance="3219")
        m.races[k].roster.add("101")
        m.set_gun(k, GUN)
        m.ingest("101", GUN + timedelta(minutes=9), "FINISH")           # loop crossing taken for 2 mi
        m.ingest("101", GUN + timedelta(minutes=18), "FINISH")
        m.set_scoring(k, 5, 2, distance="5000")
        fin = next(r for r in m.results(k)["finishers"] if r["bib"] == "101")
        self.assertAlmostEqual(fin["finish"]["elapsed"], 18 * 60, places=3)

    def test_a_finish_set_by_hand_stays(self):
        m = mgr()
        k = m.create_race("Boys", event=2, gender="M")
        m.races[k].roster.add("101")
        m.set_gun(k, GUN)
        m.ingest("101", GUN + timedelta(minutes=14), "FINISH")
        m.set_finish_time(k, "101", GUN + timedelta(minutes=14))         # the operator's call
        m.set_gun(k, GUN + timedelta(minutes=1))
        self.assertEqual(where(m, "101")[0], ("assigned", k))


class Checkin(unittest.TestCase):

    def test_no_check_in_on_the_finish_reader_while_a_race_runs(self):
        m = mgr()
        a = m.create_race("Girls", event=1, gender="F")
        b = m.create_race("Boys", event=2, gender="M")
        m.set_gun(a, GUN)
        with self.assertRaises(ValueError):
            m.open_checkin(b, reader="FINISH")
        m.open_checkin(b, reader="CHUTE")


class HeldReads(unittest.TestCase):

    def test_held_ambiguous_reads_resolve_when_a_race_goes_final(self):
        m = mgr()
        a = m.create_race("Boys V", event=2, gender="M")
        b = m.create_race("Boys JV", event=3, gender="M")
        m.set_gun(a, GUN)
        m.set_gun(b, GUN)
        m.ingest("101", GUN + timedelta(minutes=17), "FINISH")
        self.assertEqual(where(m, "101")[0][0], "held")
        m.set_status(a, "final")
        self.assertEqual(where(m, "101")[0], ("assigned", b))

    def test_recheck_after_the_bib_is_given_out(self):
        known = {}
        m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: known.get(b, {}))
        k = m.create_race("Boys", event=2, gender="M")
        m.set_gun(k, GUN)
        m.ingest("555", GUN + timedelta(minutes=17), "FINISH")
        self.assertIn("unknown bib", m.reads[1].reason)
        known["555"] = {"gender": "M"}
        m.recheck_held()
        self.assertEqual(where(m, "555")[0], ("assigned", k))

    def test_a_read_before_the_gun_says_so(self):
        m = mgr()
        k = m.create_race("Boys", event=2, gender="M")
        m.set_gun(k, GUN)
        m.ingest("101", GUN - timedelta(days=5), "FINISH")
        self.assertIn("before the gun", m.reads[1].reason)


class Routes(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.k = self.m.create_race("Boys", event=2, gender="M")
        self.m.races[self.k].roster.add("101")
        self.m.ingest("101", GUN + timedelta(minutes=17), "FINISH")      # read on 2026-10-08
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, meet_service=None,
                                           offer_recent_starts=lambda k: None)
        self.c = web.app.test_client()

    def post(self, path, body):
        r = self.c.post(path, data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    def test_a_gun_typed_as_a_time_of_day_takes_the_race_day(self):
        self.assertEqual(self.post(f"/api/races/{self.k}/gun", {"clock": "10:00:00.5"})[0], 200)
        self.assertEqual(self.m.races[self.k].gun, datetime(2026, 10, 8, 10, 0, 0, 500000))

    def test_typed_finish_bib_is_cleaned(self):
        """"01417" is 1417, junk is refused (bib_input.clean_bib, 2026-10-13)."""
        self.post(f"/api/races/{self.k}/gun", {"clock": "2026-10-08T10:00:00"})
        code, d = self.post(f"/api/races/{self.k}/finish_time", {"bib": "0105", "time": "17:40.00"})
        self.assertEqual(code, 200, d)
        self.assertEqual(sorted({r.bib for r in self.m.reads.values()}), ["101", "105"])
        for bib in ("<b>", "x" * 11, {"a": 1}, "000"):
            self.assertEqual(self.post(f"/api/races/{self.k}/finish_time", {"bib": bib, "time": "17:40.00"})[0],
                             400, bib)

    def test_clean_bib(self):
        from rfid_finishlynx.bib_input import clean_bib
        self.assertEqual([clean_bib(x) for x in ("01417", " 12 ", "A-12", 7)], ["1417", "12", "A-12", "7"])
        self.assertEqual(clean_bib("", allow_empty=True), "")
        for bad in ("", "0", "1 2", True, 1.5, "12345678901"):
            with self.assertRaises(ValueError):
                clean_bib(bad)

    def test_junk_race_times_are_refused_not_500(self):
        self.post(f"/api/races/{self.k}/gun", {"clock": "2026-10-08T10:00:00"})
        for t in ("inf", "nan", "1e12", "100000:00"):
            self.assertEqual(self.post(f"/api/races/{self.k}/finish_time", {"bib": "101", "time": t})[0], 400, t)


class CloseMeetSaveFailure(unittest.TestCase):
    """Audit R-01: a failed state save must not archive stale state and clear."""

    def test_archive_is_the_state_in_memory_and_a_failure_keeps_the_meet(self):
        import os, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: ATH.get(b, {}),
                            state_path=os.path.join(d, "races-state.json"))
            k = m.create_race("Boys", event=2, gender="M")
            m.set_gun(k, GUN)
            real_open = open

            def failing(path, *a, **kw):                      # only the state file's .tmp fails
                if str(path).endswith("races-state.json.tmp") and os.path.dirname(str(path)) == d:
                    raise OSError("disk full")
                return real_open(path, *a, **kw)
            with mock.patch("builtins.open", failing):
                m.set_finish_time(k, "101", GUN + timedelta(seconds=1000))   # in memory only
            self.assertIn("disk full", m.save_error)
            arch = os.path.join(d, "arch")
            out = m.close_meet(arch)
            with real_open(os.path.join(arch, "races-state.json")) as f:
                saved = json.load(f)
            self.assertEqual([r["bib"] for r in saved["reads"]], ["101"])
            self.assertEqual(out["reads"], 1)
            self.assertIsNone(m.save_error)                    # the next good save clears it

    def test_an_archive_that_cannot_be_written_leaves_the_meet(self):
        import os, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            m = RaceManager(finish_readers={"FINISH"}, state_path=os.path.join(d, "s.json"))
            m.create_race("Boys", event=2, gender="M")
            m.ingest("101", GUN, "FINISH")
            with mock.patch("json.dump", side_effect=OSError("no space")):
                with self.assertRaises(OSError):
                    m.close_meet(os.path.join(d, "arch"))
            self.assertEqual((len(m.races), len(m.reads)), (1, 1))

    def test_the_console_gets_the_save_error(self):
        m = mgr()
        m.save_error = "race state not saved (10:00:00): disk full"
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=m, meet_service=None)
        d = web.app.test_client().get("/api/races").get_json()
        self.assertEqual(d["save_error"], m.save_error)


class ApiJunk(unittest.TestCase):
    """Break test 5: junk requests get 4xx, never 500."""

    def setUp(self):
        self.m = mgr()
        self.k = self.m.create_race("Boys", event=2, gender="M")
        self.m.set_gun(self.k, GUN)
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, meet_service=None,
                                           offer_recent_starts=lambda k: None, get_reads=lambda: [])
        self.c = web.app.test_client()

    def raw(self, path, body):
        return self.c.post(path, data=body, content_type="application/json").status_code

    def test_bodies_that_are_not_objects(self):
        for path in (f"/api/races/{self.k}/add_read", f"/api/races/{self.k}/arm", f"/api/races/{self.k}/status",
                     "/api/races/move", "/api/races/create", "/api/film_offset", f"/api/races/{self.k}/remove_bib"):
            for body in ("[1,2]", "123", "{bad json"):
                self.assertEqual(self.raw(path, body), 400, (path, body))

    def test_unknown_ids_and_bad_numbers(self):
        j = lambda p, b: self.raw(p, json.dumps(b))
        self.assertEqual(j("/api/races/999/status", {"status": "racing"}), 404)
        self.assertEqual(j(f"/api/races/{self.k}/status", {}), 400)
        self.assertEqual(j("/api/reads/999999/ignore", {}), 404)
        self.assertEqual(j("/api/reads/1/assign", {"race": 999}), 404)
        self.assertEqual(j("/api/reads/1/assign", {}), 400)
        self.assertEqual(j("/api/races/move", {"bib": "1", "from": "abc", "to": 1}), 400)
        self.assertEqual(j("/api/races/move", {"bib": "1", "from": self.k, "to": 999}), 404)
        self.assertEqual(j("/api/races/create", {"event": "abc"}), 400)
        self.assertEqual(j("/api/races/create", {"event": 3, "scorers": -1}), 400)
        self.assertEqual(j("/api/film_offset", {"manual": float("nan")}), 400)
        self.assertEqual(j(f"/api/races/{self.k}/gun", {"race_time": "nan"}), 400)
        self.assertEqual(j(f"/api/races/{self.k}/scoring", {"scorers": 0, "displacers": 2}), 400)
        self.assertEqual(j(f"/api/races/{self.k}/scoring", {"scorers": 5}), 400)
        self.assertEqual(j(f"/api/races/{self.k}/finish_time", {"bib": "101", "time": "1:75"}), 400)
        self.assertEqual(self.c.get("/api/races").status_code, 200)

    def test_an_empty_body_is_an_empty_object(self):
        self.assertEqual(self.c.post(f"/api/races/{self.k}/arm").status_code, 200)

    def test_scorers_out_of_range_refused_by_the_manager(self):
        with self.assertRaises(ValueError):
            self.m.set_scoring(self.k, 0, 2)
        with self.assertRaises(ValueError):
            self.m.set_scoring(self.k, 5, 51)


if __name__ == "__main__":
    unittest.main()
