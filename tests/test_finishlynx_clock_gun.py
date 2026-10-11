"""The LED clocks follow FinishLynx's running clock even when no race pane is
matched to the event (restart mid-race, or no pane created)."""

import time
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx import app as app_mod
from rfid_finishlynx.app import Application


def bare_app():
    """Application without __init__ (which builds hardware services)."""
    a = Application.__new__(Application)
    a.race_manager = None
    a.scoreboard_listener = None
    a.lora_receiver = None
    a._fl_clock_gun = None
    a._fl_clock_at = 0.0
    a._clock_override = None
    a._gun_broadcast_epoch = None
    a._gun_broadcast_at = 0.0
    a._camera_gun = None
    a._camera_gun_at = 0.0
    a._recent_starts = []
    return a


class TestRecentStarts(unittest.TestCase):

    def test_starts_noted_and_deduped_with_camera_preferred(self):
        a = bare_app()
        a._handle_running_time(5.0)                       # FinishLynx clock start ~5 s ago
        fl = a._recent_starts[0]["ts"]
        cam = fl + timedelta(milliseconds=70)
        a._note_start(cam, "camera start")                # same gun heard by the camera
        self.assertEqual(len(a._recent_starts), 1)
        self.assertEqual(a._recent_starts[0], {"ts": cam, "source": "camera start"})
        a._note_start(cam + timedelta(minutes=30), "FinishLynx clock")
        self.assertEqual([s["source"] for s in a.recent_starts()],
                         ["FinishLynx clock", "camera start"])     # newest first

    def test_new_pane_gets_recent_starts_unarmed(self):
        a = app_with_races()
        a._recent_starts = []
        a._note_start(datetime.now() - timedelta(minutes=10), "camera start")
        a._note_start(datetime.now() - timedelta(hours=5), "camera start")   # too old
        key = a.race_manager.create_race("JV Girls", event=4)
        a.offer_recent_starts(key)
        race = a.race_manager.races[key]
        self.assertIsNone(race.gun)
        self.assertEqual(len(race.gun_candidates), 1)


class FakeScoreboard:
    def __init__(self, event=0, heat=0):
        self.event, self.heat = event, heat


def app_with_races(event=0, heat=0):
    from rfid_finishlynx.races import RaceManager

    a = bare_app()
    a.race_manager = RaceManager(athlete_lookup=lambda bib: None, on_change=lambda keys: None,
                                 chute_reader="CHUTE", finish_readers={"FINISH"},
                                 point_labels={}, state_path=None)
    a.scoreboard_listener = FakeScoreboard(event, heat)
    return a


class TestRunningTimeArmsPane(unittest.TestCase):
    """FinishLynx's running clock should fire the gun on the pane that is
    waiting for it, even when the scoreboard feed never named the event."""

    def test_event_match_still_wins(self):
        a = app_with_races(event=2, heat=1)
        k1 = a.race_manager.create_race("Boys 5K", event=1)
        k2 = a.race_manager.create_race("Girls 5K", event=2)
        a.race_manager.set_status(k1, "racing")
        a.race_manager.set_status(k2, "checkin")
        a._handle_running_time(12.0)
        self.assertIsNone(a.race_manager.races[k1].gun)
        self.assertIsNotNone(a.race_manager.races[k2].gun)
        self.assertEqual(a.race_manager.races[k2].status, "racing")

    def test_unknown_event_arms_first_open_pane_without_gun(self):
        a = app_with_races()                       # event 0 = never announced
        k1 = a.race_manager.create_race("Boys 5K", event=1)
        k2 = a.race_manager.create_race("Girls 5K", event=2)
        a.race_manager.set_status(k1, "checkin")
        a.race_manager.open_checkin(k2, reader="CHUTE2")   # one race per reader (2026-10-04)
        a._handle_running_time(3.2)
        r1, r2 = a.race_manager.races[k1], a.race_manager.races[k2]
        self.assertIsNotNone(r1.gun)
        self.assertEqual((r1.status, r1.gun_source), ("racing", "FinishLynx clock"))
        self.assertIsNone(r2.gun)
        # A second start later (clock restarted) goes to the next waiting pane.
        a._handle_running_time(0.5)
        self.assertIsNotNone(r2.gun)

    def test_restarted_clock_offers_candidate_on_racing_pane(self):
        a = app_with_races()
        k = a.race_manager.create_race("Boys 5K", event=1)
        a.race_manager.set_gun(k, datetime(2026, 9, 12, 7, 0, 0), "manual")
        a._handle_running_time(30.0)
        race = a.race_manager.races[k]
        self.assertEqual(race.gun, datetime(2026, 9, 12, 7, 0, 0))   # not overridden
        self.assertEqual(len(race.gun_candidates), 2)                # but offered in the picker

    def test_zero_running_time_does_nothing(self):
        a = app_with_races()
        k = a.race_manager.create_race("Boys 5K", event=1)
        a.race_manager.set_status(k, "checkin")
        a._handle_running_time(0.0)
        self.assertIsNone(a.race_manager.races[k].gun)


class TestCameraStart(unittest.TestCase):

    def _event(self, code, wall):
        from rfid_finishlynx.camera_start import CameraEvent
        return CameraEvent(code, 0, wall, wall.timestamp() + 0.2, 1 if code == 0x1140 else 0)

    def test_camera_start_arms_waiting_pane_and_clocks(self):
        a = app_with_races()
        a._camera_gun = None
        a._camera_gun_at = 0.0
        k = a.race_manager.create_race("Girls 5K", event=2)
        a.race_manager.set_status(k, "checkin")
        gun = datetime.now()
        a._on_camera_event(self._event(0x1140, gun))
        race = a.race_manager.races[k]
        self.assertEqual((race.gun, race.gun_source, race.status), (gun, "camera start", "racing"))
        self.assertEqual(a._current_gun_with_source(), (gun, "race"))

    def test_camera_start_without_pane_drives_clocks(self):
        a = bare_app()
        a._camera_gun = None
        a._camera_gun_at = 0.0
        a._handle_running_time(500.0)              # FinishLynx clock running from earlier
        gun = datetime.now()
        a._on_camera_event(self._event(0x1140, gun))
        self.assertEqual(a._current_gun_with_source(), (gun, "camera"))   # camera beats FinishLynx clock

    def test_wired_start_drives_clocks(self):
        # word 5 == 1 (wired start sensor) was invisible until 2026-10-11
        a = bare_app()
        a._handle_running_time(500.0)
        gun = datetime.now()
        a._on_camera_event(self._event(1, gun))
        self.assertEqual(a._current_gun_with_source(), (gun, "camera"))

    def test_other_input_code_ignored(self):
        a = bare_app()
        a._camera_gun = None
        a._camera_gun_at = 0.0
        a._on_camera_event(self._event(0x120b, datetime.now()))
        self.assertIsNone(a._camera_gun)


class TestClockOverride(unittest.TestCase):

    def test_elapsed_override_beats_finishlynx_clock(self):
        a = bare_app()
        a._handle_running_time(100.0)
        r = a.set_clock_override("elapsed", 754.0)
        self.assertTrue(r["success"])
        gun, source = a._current_gun_with_source()
        self.assertEqual(source, "manual")
        self.assertAlmostEqual((datetime.now() - gun).total_seconds(), 754.0, delta=0.5)

    def test_now_starts_at_zero(self):
        a = bare_app()
        a.set_clock_override("now")
        gun, _ = a._current_gun_with_source()
        self.assertLess(abs((datetime.now() - gun).total_seconds()), 0.5)

    def test_tod_forces_time_of_day_and_auto_restores(self):
        a = bare_app()
        a._handle_running_time(100.0)
        a.set_clock_override("tod")
        self.assertEqual(a._current_gun_with_source(), (None, "manual_tod"))
        a.set_clock_override("auto")
        gun, source = a._current_gun_with_source()
        self.assertEqual(source, "finishlynx")
        self.assertIsNotNone(gun)

    def test_bad_values_rejected(self):
        a = bare_app()
        self.assertFalse(a.set_clock_override("elapsed", -5)["success"])
        self.assertFalse(a.set_clock_override("bogus")["success"])
        self.assertIsNone(a._clock_override)


class TestFinishLynxClockGun(unittest.TestCase):

    def test_running_time_drives_clock_without_race_pane(self):
        a = bare_app()
        self.assertIsNone(a._current_gun())
        a._handle_running_time(42 * 60 + 5.3)
        gun = a._current_gun()
        self.assertIsNotNone(gun)
        elapsed = (datetime.now() - gun).total_seconds()
        self.assertAlmostEqual(elapsed, 2525.3, delta=0.5)

    def test_zero_packet_is_armed_not_running(self):
        a = bare_app()
        a._handle_running_time(0.0)
        self.assertIsNone(a._current_gun())

    def test_goes_stale_when_packets_stop(self):
        a = bare_app()
        a._handle_running_time(10.0)
        a._fl_clock_at = time.time() - app_mod.FL_CLOCK_FRESH_SEC - 1
        self.assertIsNone(a._current_gun())

    def test_racing_pane_gun_wins_over_finishlynx_clock(self):
        from rfid_finishlynx.races import RaceManager

        a = bare_app()
        mgr = RaceManager(athlete_lookup=lambda bib: None, on_change=lambda keys: None,
                          chute_reader="CHUTE", finish_readers={"FINISH"},
                          point_labels={}, state_path=None)
        a.race_manager = mgr
        key = mgr.create_race("Boys 5K", event=1)
        pane_gun = datetime(2026, 9, 12, 7, 0, 0)
        mgr.set_gun(key, pane_gun, "manual")
        a._handle_running_time(30.0)
        self.assertEqual(a._current_gun(), pane_gun)


if __name__ == "__main__":
    unittest.main()
