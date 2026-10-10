"""Check-in per reader (2026-10-04, the user's spec):

- each race checks in on a reader of its own; several races can be open at
  once, one per reader; taking a busy reader needs take=True and closes the
  other race's check-in;
- the button closes check-in again: nothing more joins, check-ins stay, and
  the 2-minute after-gun grace is gone - the gun keeps it;
- any reader can check in; once closed it counts as its Location again;
- "Close in" minutes, and a start-time schedule (meet default or the race's
  own, or off) that opens and closes check-in by itself, never reopening
  what the operator closed, waiting when its reader is busy;
- all of it survives a restart; older state (one chute_target) loads;
- the API: open/close, 409 for a busy reader, settings and default.

python -m unittest tests.test_checkin_readers
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.config import Config
from rfid_finishlynx.races import CheckinBusy, RaceManager

ATH = {b: {"gender": g, "last_name": f"R{b}", "school": "Red"}
       for b, g in (("11", "F"), ("12", "F"), ("13", "F"), ("21", "M"), ("22", "M"))}
T0 = datetime(2026, 10, 10, 9, 0, 0)


def manager(state_path=None):
    m = RaceManager(athlete_lookup=ATH.get, state_path=state_path,
                    point_labels={"S1": "Mile 1"}, point_distances={"S1": 1609.0})
    girls = m.create_race("Girls 5K", event=1, gender="F", distance="5000")
    boys = m.create_race("Boys 5K", event=2, gender="M", distance="5000")
    return m, girls, boys


class TestReaders(unittest.TestCase):

    def test_two_races_check_in_at_once_on_their_own_readers(self):
        m, girls, boys = manager()
        self.assertEqual(m.open_checkin(girls), "CHUTE")
        self.assertEqual(m.open_checkin(boys, reader="CHUTE2"), "CHUTE2")
        self.assertEqual(m.ingest("11", T0, "CHUTE")["race"], girls)
        self.assertEqual(m.ingest("21", T0, "CHUTE2")["race"], boys)
        self.assertEqual((m.races[girls].status, m.races[boys].status), ("checkin", "checkin"))
        self.assertEqual(m.snapshot()["checkin_open"], {"CHUTE": girls, "CHUTE2": boys})

    def test_busy_reader_needs_take(self):
        m, girls, boys = manager()
        m.open_checkin(girls)
        with self.assertRaises(CheckinBusy) as cm:
            m.open_checkin(boys)
        self.assertEqual((cm.exception.reader, cm.exception.race_key), ("CHUTE", girls))
        m.open_checkin(boys, take=True)
        self.assertEqual(m.races[girls].status, "pending")
        self.assertEqual(m.ingest("21", T0, "CHUTE")["race"], boys)

    def test_closing_stops_check_ins_but_keeps_them(self):
        m, girls, _ = manager()
        m.open_checkin(girls)
        m.ingest("11", T0, "CHUTE")
        m.close_checkin(girls)
        self.assertEqual(m.races[girls].status, "pending")
        res = m.ingest("12", T0 + timedelta(seconds=5), "CHUTE")
        self.assertEqual(res["state"], "ignored")
        self.assertEqual(set(m.races[girls].checkins), {"11"})
        # closed by hand: no grace after the gun either
        m.set_gun(girls, T0 + timedelta(minutes=1))
        self.assertEqual(m.ingest("13", T0 + timedelta(minutes=1, seconds=30), "CHUTE")["state"], "ignored")

    def test_gun_keeps_the_late_grace(self):
        m, girls, _ = manager()
        m.open_checkin(girls)
        m.set_gun(girls, T0)
        self.assertEqual(m.ingest("12", T0 + timedelta(seconds=90), "CHUTE")["race"], girls)
        self.assertEqual(m.ingest("13", T0 + timedelta(seconds=150), "CHUTE")["state"], "ignored")

    def test_any_reader_returns_to_its_location_when_closed(self):
        m, girls, _ = manager()
        m.open_checkin(girls, reader="S1")
        self.assertEqual(m.ingest("11", T0, "S1")["state"], "roster")
        m.set_gun(girls, T0 + timedelta(minutes=1))
        # gun closed it, grace window: still a check-in for 2 minutes
        self.assertEqual(m.ingest("12", T0 + timedelta(minutes=2), "S1")["state"], "roster")
        # after that S1 is Mile 1 again
        res = m.ingest("11", T0 + timedelta(minutes=8), "S1")
        self.assertEqual((res["state"], res["point"]), ("assigned", "S1"))

    def test_cannot_open_a_started_race(self):
        m, girls, _ = manager()
        m.set_gun(girls, T0)
        self.assertRaises(ValueError, m.open_checkin, girls)


class TestTimers(unittest.TestCase):

    def test_close_in(self):
        m, girls, _ = manager()
        m.open_checkin(girls, close_in_min=10, now=T0)
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=9)), [])
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=10)), [girls])
        self.assertEqual(m.races[girls].status, "pending")

    def schedule(self):
        m, girls, boys = manager()
        starts = {girls: T0 + timedelta(minutes=60), boys: T0 + timedelta(minutes=100)}
        m.start_time_of = lambda r: starts.get(r.key)
        m.set_checkin_default(schedule={"open_before": 30, "close_before": 5})
        return m, girls, boys

    def test_default_schedule_opens_and_closes(self):
        m, girls, boys = self.schedule()
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=29)), [])
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=30)), [girls])
        self.assertEqual(m.races[girls].status, "checkin")
        self.assertEqual(m.snapshot()["races"][str(girls)]["checkin"]["open_on"], "CHUTE")
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=55)), [girls])
        self.assertEqual(m.races[girls].status, "pending")
        # never reopened, even inside the window
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=56)), [])
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=70)), [boys])

    def test_operator_close_is_not_undone(self):
        m, girls, _ = self.schedule()
        m.tick_checkins(T0 + timedelta(minutes=31))
        m.close_checkin(girls)
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=32)), [])
        self.assertEqual(m.races[girls].status, "pending")

    def test_race_own_schedule_and_off(self):
        m, girls, boys = self.schedule()
        m.set_checkin_settings(girls, reader="CHUTE2", auto={"open_before": 10, "close_before": None})
        m.set_checkin_settings(boys, auto="off")
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=45)), [])
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=50)), [girls])
        self.assertEqual(m.checkin_open, {"CHUTE2": girls})
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=59)), [])      # at the gun
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=95)), [])      # boys: off
        self.assertIsNone(m.snapshot()["races"][str(boys)]["checkin"]["schedule"])

    def test_scheduled_open_waits_for_a_busy_reader(self):
        m, girls, boys = self.schedule()
        m.set_checkin_settings(girls, auto="off")
        m.open_checkin(girls)                                   # holding CHUTE by hand
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=71)), [boys])
        self.assertEqual(m.races[boys].status, "pending")
        self.assertIn("Girls 5K", m.races[boys].checkin_waiting)
        m.close_checkin(girls)
        self.assertEqual(m.tick_checkins(T0 + timedelta(minutes=72)), [boys])
        self.assertEqual(m.races[boys].status, "checkin")

    def test_no_start_time_no_schedule(self):
        m, girls, _ = manager()
        m.set_checkin_default(schedule={"open_before": 30, "close_before": 5})
        self.assertEqual(m.tick_checkins(T0), [])
        self.assertRaises(ValueError, m.set_checkin_default, None, {"open_before": 5, "close_before": 10})


class TestPersistence(unittest.TestCase):

    def test_restart_keeps_check_ins(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m, girls, boys = manager(path)
            m.set_checkin_default(reader="CHUTE2", schedule={"open_before": 20, "close_before": None})
            m.set_checkin_settings(boys, reader="S1", auto="off")
            m.open_checkin(girls, reader="CHUTE", close_in_min=15, now=T0)
            n = RaceManager(athlete_lookup=ATH.get, state_path=path)
            self.assertEqual(n.checkin_open, {"CHUTE": girls})
            self.assertEqual(n.checkin_default, {"reader": "CHUTE2", "open_before": 20, "close_before": None})
            self.assertEqual((n.races[boys].checkin_reader, n.races[boys].checkin_auto), ("S1", {"off": True}))
            self.assertEqual(n.races[girls].checkin_closes, m.races[girls].checkin_closes)
            self.assertEqual(n.ingest("11", T0, "CHUTE")["race"], girls)

    def test_older_state_with_one_chute_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m, girls, _ = manager(path)
            m.open_checkin(girls)
            with open(path) as f:
                state = json.load(f)
            for k in ("checkin_open", "checkin_last", "checkin_default"):
                del state[k]
            state["chute_target"] = girls
            with open(path, "w") as f:
                json.dump(state, f)
            n = RaceManager(athlete_lookup=ATH.get, state_path=path)
            self.assertEqual(n.chute_target, girls)
            self.assertEqual(n.ingest("12", T0, "CHUTE")["race"], girls)


class TestApi(unittest.TestCase):

    def setUp(self):
        self.m, self.girls, self.boys = manager()
        web._application = SimpleNamespace(config=Config(), race_manager=self.m, save_config=lambda: None)
        self.addCleanup(setattr, web, "_application", None)
        self.c = web.app.test_client()

    def test_open_busy_take_close(self):
        r = self.c.post(f"/api/races/{self.girls}/checkin", json={"open": True, "close_in_min": 10})
        self.assertEqual((r.status_code, r.get_json()["reader"]), (200, "CHUTE"))
        r = self.c.post(f"/api/races/{self.boys}/checkin", json={"open": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["busy"], {"reader": "CHUTE", "race": self.girls, "name": "Girls 5K"})
        r = self.c.post(f"/api/races/{self.boys}/checkin", json={"open": True, "take": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.races[self.girls].status, "pending")
        self.c.post(f"/api/races/{self.boys}/checkin", json={"open": False})
        self.assertEqual(self.m.races[self.boys].status, "pending")
        payload = self.c.get("/api/races").get_json()
        self.assertEqual(payload["checkin_open"], {})
        self.assertIn("checkin", payload["races"][str(self.girls)])

    def test_settings_default_and_validation(self):
        r = self.c.post(f"/api/races/{self.girls}/checkin-settings",
                        json={"reader": "S1", "auto": {"open_before": 20, "close_before": 2}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.races[self.girls].checkin_auto, {"open_before": 20, "close_before": 2})
        r = self.c.post(f"/api/races/{self.girls}/checkin-settings", json={"auto": {"open_before": 2, "close_before": 5}})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/races/checkin-default", json={"reader": "CHUTE", "schedule": {"open_before": 30}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.checkin_default["open_before"], 30)
        r = self.c.post("/api/races/checkin-default", json={"schedule": None})
        self.assertIsNone(self.m.checkin_default["open_before"])
        r = self.c.post(f"/api/races/{self.girls}/checkin", json={"open": True, "close_in_min": 0})
        self.assertEqual(r.status_code, 400)
        self.m.set_gun(self.boys, T0)
        r = self.c.post(f"/api/races/{self.boys}/status", json={"status": "checkin"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
