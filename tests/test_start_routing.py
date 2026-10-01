"""Where a start signal (camera / FinishLynx clock) lands when FinishLynx
has not announced an event. Runs without hardware: python -m unittest
tests.test_start_routing"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from rfid_finishlynx.app import Application
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 9, 15, 16, 38, 0, 554000)


def make_app(event=0, heat=0):
    app = Application.__new__(Application)
    app.race_manager = RaceManager(on_change=lambda k: None)
    app.scoreboard_listener = SimpleNamespace(event=event, heat=heat)
    return app


class TestRouteStart(unittest.TestCase):

    def test_armed_event_wins_and_arms(self):
        app = make_app(event=2, heat=1)
        boys = app.race_manager.create_race("Boys", event=1, gender="M")
        girls = app.race_manager.create_race("Girls", event=2, gender="F")
        self.assertEqual(app._route_start(GUN, "camera start"), [girls])
        self.assertEqual(app.race_manager.races[girls].gun, GUN)
        self.assertEqual(app.race_manager.races[girls].status, "racing")
        self.assertEqual(app.race_manager.races[boys].gun_candidates, [])

    def test_checkin_pane_takes_it(self):
        app = make_app()
        boys = app.race_manager.create_race("Boys", event=1, gender="M")
        girls = app.race_manager.create_race("Girls", event=2, gender="F")
        app.race_manager.set_status(girls, "checkin")
        self.assertEqual(app._route_start(GUN, "camera start"), [girls])
        self.assertEqual(app.race_manager.races[girls].gun, GUN)
        self.assertEqual(app.race_manager.races[boys].gun_candidates, [])

    def test_several_pending_panes_all_get_it_unarmed(self):
        # The 2026-09-15 16:38 case: two fresh pending panes, FinishLynx silent.
        app = make_app()
        boys = app.race_manager.create_race("Boys", event=1, gender="M")
        girls = app.race_manager.create_race("Girls", event=2, gender="F")
        self.assertEqual(app._route_start(GUN, "camera start"), [boys, girls])
        for k in (boys, girls):
            race = app.race_manager.races[k]
            self.assertIsNone(race.gun)
            self.assertEqual(race.status, "pending")
            self.assertEqual([c["source"] for c in race.gun_candidates], ["camera start"])
        # the same gun re-heard from FinishLynx's clock is not listed twice
        app._route_start(GUN, "FinishLynx clock")
        self.assertEqual(len(app.race_manager.races[boys].gun_candidates), 1)

    def test_pane_with_a_gun_already_is_skipped(self):
        app = make_app()
        boys = app.race_manager.create_race("Boys", event=1, gender="M")
        girls = app.race_manager.create_race("Girls", event=2, gender="F")
        jv = app.race_manager.create_race("JV", event=3, gender="M")
        app.race_manager.set_gun(boys, GUN)             # racing now -> awaiting-start picks it
        self.assertEqual(app._route_start(GUN, "camera start"), [boys])
        # once it is final, a new start is offered to the two panes still without a gun
        app.race_manager.set_status(boys, "final")
        later = GUN.replace(minute=50)
        self.assertEqual(app._route_start(later, "camera start"), [girls, jv])

    def test_same_start_reheard_is_logged_once(self):
        # FinishLynx's running clock implies the same gun ten times a second;
        # 2026-09-18 that was 30k "offered to 4 pane(s)" lines in an hour.
        app = make_app()
        app.race_manager.create_race("Boys", event=1, gender="M")
        app.race_manager.create_race("Girls", event=2, gender="F")
        with self.assertLogs("rfid_finishlynx.app", level="INFO") as logs:
            for ms in (0, 11, 23, 36):
                app._route_start(GUN.replace(microsecond=554000 + ms * 1000), "FinishLynx clock")
            app._route_start(GUN.replace(minute=50), "camera start")     # a new start: logged
        offered = [m for m in logs.output if "offered to" in m]
        self.assertEqual(len(offered), 2)

    def test_no_pane_at_all(self):
        app = make_app()
        self.assertEqual(app._route_start(GUN, "camera start"), [])
        app.race_manager = None
        self.assertEqual(app._route_start(GUN, "camera start"), [])


if __name__ == "__main__":
    unittest.main()
