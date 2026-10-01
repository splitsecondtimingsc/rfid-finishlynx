"""What the LED clocks follow after the operator acts (2026-09-30):

- Setting a pane's gun by hand (Gun dialog, New race start time) releases a
  Time-of-day park, so the clocks show that race. It sat on time of day at
  the 9/30 test until "Show on clocks". Starts that arrive by themselves
  (camera, FinishLynx) still leave the park alone (9/22 rule), and a pin or a
  manual count-up is kept.
- A camera start drives the clocks only until a pane takes it: once that
  race is closed the clocks go back to time of day instead of counting on
  for up to 2 hours.
python -m unittest tests.test_clock_follows_operator"""

import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.app import Application
from rfid_finishlynx.races import RaceManager


def make_app():
    app = Application.__new__(Application)
    app.race_manager = RaceManager(on_change=lambda k: None)
    app._clock_override = None
    app._camera_gun = None
    app._camera_gun_at = 0.0
    app._finishlynx_clock_gun = lambda: None
    app.lora_receiver = None
    app.led_clock_service = None
    return app


class TestOperatorGunReleasesTimeOfDay(unittest.TestCase):

    def setUp(self):
        self.app = make_app()
        self.key = self.app.race_manager.create_race("Men's 5000", event=1, gender="M")
        self.gun = datetime.now() - timedelta(minutes=2)

    def test_gun_set_by_hand_releases_the_park(self):
        self.app.set_clock_override("tod")
        self.app.race_manager.set_gun(self.key, self.gun, "manual")
        self.assertEqual(self.app._current_gun_with_source(), (None, "manual_tod"))  # the 9/30 symptom
        self.app.on_operator_gun(self.key)
        self.assertIsNone(self.app._clock_override)
        self.assertEqual(self.app._current_gun_with_source(), (self.gun, "race"))

    def test_pin_and_manual_count_are_kept(self):
        other = self.app.race_manager.create_race("Women's 5000", event=2, gender="F")
        self.app.race_manager.set_gun(other, self.gun - timedelta(minutes=5))
        self.app.set_clock_override("race", race_key=other)
        self.app.race_manager.set_gun(self.key, self.gun)
        self.app.on_operator_gun(self.key)
        self.assertEqual(self.app._current_gun_with_source()[1], "pinned")

        self.app.set_clock_override("elapsed", 125)
        self.app.on_operator_gun(self.key)
        self.assertEqual(self.app._current_gun_with_source()[1], "manual")

    def test_camera_start_still_leaves_the_park_alone(self):
        """The automatic path never calls on_operator_gun: a camera start
        armed into a pane keeps the clocks on time of day (9/22 rule)."""
        self.app.set_clock_override("tod")
        self.app.race_manager.add_gun_candidate(self.key, self.gun, "camera start")
        self.app._camera_gun, self.app._camera_gun_at = self.gun, time.time()
        self.assertEqual(self.app._current_gun_with_source(), (None, "manual_tod"))

    def _client(self):
        web._application = self.app
        self.addCleanup(setattr, web, "_application", None)
        return web.app.test_client()

    def test_gun_dialog_route_releases_the_park(self):
        self.app.set_clock_override("tod")
        r = self._client().post(f"/api/races/{self.key}/gun", json={"clock": "now", "source": "manual now"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.app._current_gun_with_source()[1], "race")

    def test_new_race_with_a_start_time_releases_the_park(self):
        self.app.set_clock_override("tod")
        self.app.offer_recent_starts = lambda key: None
        clock = self.gun.strftime("%H:%M:%S.%f")[:-3]
        r = self._client().post("/api/races/create", json={"name": "Boys JV", "event": 3, "gun": clock})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.app._current_gun_with_source()[1], "race")


class TestCameraGunEndsWithItsRace(unittest.TestCase):

    def setUp(self):
        self.app = make_app()
        self.mgr = self.app.race_manager
        self.key = self.mgr.create_race("Men's 5000", event=1, gender="M")
        self.cam = datetime.now() - timedelta(seconds=40)
        self.app._camera_gun, self.app._camera_gun_at = self.cam, time.time() - 40

    def test_untaken_camera_start_still_counts(self):
        self.assertEqual(self.app._current_gun_with_source(), (self.cam, "camera"))

    def test_closing_the_race_that_took_it_returns_to_time_of_day(self):
        # FinishLynx's version of the same gun, 72 ms earlier (as on 9/30)
        self.mgr.set_gun(self.key, self.cam - timedelta(milliseconds=72))
        self.assertEqual(self.app._current_gun_with_source()[1], "race")
        self.mgr.set_status(self.key, "final")
        self.assertEqual(self.app._current_gun_with_source(), (None, "idle"))
        # Time of day -> Auto no longer brings the old start back
        self.app.set_clock_override("tod")
        self.app.set_clock_override("auto")
        self.assertEqual(self.app._current_gun_with_source(), (None, "idle"))

    def test_close_meet_does_not_bring_it_back(self):
        self.mgr.set_gun(self.key, self.cam)
        self.app._current_gun_with_source()
        self.mgr.set_status(self.key, "final")
        self.mgr.delete_race(self.key)
        self.assertEqual(self.app._current_gun_with_source(), (None, "idle"))

    def test_a_different_start_is_not_consumed(self):
        """A pane on some other gun (earlier race) does not eat a new camera
        start nobody has taken yet."""
        self.mgr.set_gun(self.key, self.cam - timedelta(minutes=20))
        self.mgr.set_status(self.key, "final")
        self.assertEqual(self.app._current_gun_with_source(), (self.cam, "camera"))


if __name__ == "__main__":
    unittest.main()
