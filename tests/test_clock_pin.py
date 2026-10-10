"""Pinning the LED clocks to one race pane's gun (a straggler from an earlier
race while the next one runs). python -m unittest tests.test_clock_pin"""

import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.app import Application
from rfid_finishlynx.races import RaceManager

GUN1 = datetime(2026, 9, 15, 9, 0, 0)
GUN2 = GUN1 + timedelta(minutes=25)


def make_app():
    app = Application.__new__(Application)
    app.race_manager = RaceManager(on_change=lambda k: None)
    app._clock_override = None
    app._camera_gun = None
    app._camera_gun_at = 0.0
    app._finishlynx_clock_gun = lambda: None
    app.lora_receiver = None
    return app


class TestClockPin(unittest.TestCase):

    def setUp(self):
        self.app = make_app()
        m = self.app.race_manager
        self.girls = m.create_race("Girls", event=1, gender="F")
        self.boys = m.create_race("Boys", event=2, gender="M")
        m.set_gun(self.girls, GUN1)
        m.set_gun(self.boys, GUN2)

    def test_auto_follows_latest_racing_pane(self):
        self.assertEqual(self.app._current_gun_with_source(), (GUN2, "race"))

    def test_override_resends_to_the_live_clocks(self):
        """A clock-chip click re-sends at once (2026-09-19: three "time of
        day" clicks changed nothing on a clock that had missed its set)."""
        from unittest import mock
        self.app.led_clock_service = mock.Mock()
        self.assertTrue(self.app.set_clock_override("tod")["success"])
        self.app.led_clock_service.resync_time_of_day.assert_called_once()
        self.app.set_clock_override("race", race_key=self.girls)
        self.assertEqual(self.app.led_clock_service.resync_time_of_day.call_count, 2)
        self.app.led_clock_service = None            # no service configured: still fine
        self.assertTrue(self.app.set_clock_override("auto")["success"])

    def test_pin_shows_earlier_race_without_touching_guns(self):
        r = self.app.set_clock_override("race", race_key=self.girls)
        self.assertTrue(r["success"], r)
        self.assertEqual(self.app._current_gun_with_source(), (GUN1, "pinned"))
        self.assertEqual(self.app.race_manager.races[self.girls].gun, GUN1)
        self.assertEqual(self.app.race_manager.races[self.boys].gun, GUN2)

    def test_pin_follows_a_corrected_gun(self):
        self.app.set_clock_override("race", race_key=self.girls)
        fixed = GUN1 - timedelta(seconds=3)
        self.app.race_manager.set_gun(self.girls, fixed, "corrected")
        self.assertEqual(self.app._current_gun(), fixed)

    def test_pin_releases_when_race_closes_or_is_deleted(self):
        self.app.set_clock_override("race", race_key=self.girls)
        self.app.race_manager.set_status(self.girls, "final")
        self.assertEqual(self.app._current_gun_with_source(), (GUN2, "race"))
        self.assertIsNone(self.app._clock_override)
        self.app.race_manager.set_status(self.girls, "racing")
        self.app.set_clock_override("race", race_key=self.girls)
        self.app.race_manager.delete_race(self.girls)
        self.assertEqual(self.app._current_gun_with_source(), (GUN2, "race"))

    def test_pin_rejections(self):
        self.assertFalse(self.app.set_clock_override("race", race_key=99)["success"])
        pending = self.app.race_manager.create_race("JV", event=3)
        self.assertFalse(self.app.set_clock_override("race", race_key=pending)["success"])
        self.app.race_manager.set_status(self.girls, "final")
        self.assertFalse(self.app.set_clock_override("race", race_key=self.girls)["success"])
        self.assertIsNone(self.app._clock_override)

    def test_auto_clears_pin(self):
        self.app.set_clock_override("race", race_key=self.girls)
        self.app.set_clock_override("auto")
        self.assertEqual(self.app._current_gun_with_source(), (GUN2, "race"))


if __name__ == "__main__":
    unittest.main()
