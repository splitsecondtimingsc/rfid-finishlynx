"""A finish-line read goes to FinishLynx only when it belongs to the race
being timed: LapTime packets carry no event, so FinishLynx files every read
under the armed event. 2026-09-30: bib 1190 (Men's 5000, 61 min) crossed
while the Women's 5000 ran and landed in the girls' results in FinishLynx.
python -m unittest tests.test_finish_other_race"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import fl_window
from rfid_finishlynx.races import RaceManager
from tests.test_finishlynx_feed import make_app, read

# On the timing PC a real FinishLynx window is open: never let it leak in.
_OPEN = {"value": None}


def setUpModule():
    p = mock.patch.object(fl_window, "open_event", lambda: _OPEN["value"])
    p.start()
    unittest.addModuleCleanup(p.stop)

ATHLETES = {"1190": {"gender": "M", "last_name": "devita"}, "1192": {"gender": "M", "last_name": "hanko"},
            "2001": {"gender": "F", "last_name": "Ames"}}


def meet():
    """Men's 5000 closed (1190 on its roster), Women's 5000 running."""
    app = make_app("finish")
    app.watch_finishlynx_window = True   # the real console reads FinishLynx's window (stubbed here)
    mgr = RaceManager(athlete_lookup=ATHLETES.get, state_path=None)
    app.race_manager = mgr
    now = datetime.now()
    men = mgr.create_race("Men's 5000 Meters", event=1, gender="M", distance="5000")
    mgr.set_status(men, "racing")
    mgr.set_gun(men, now - timedelta(minutes=61))
    mgr.races[men].roster.update({"1190", "1192"})
    mgr.set_status(men, "final")
    women = mgr.create_race("Women's 5000 Meters", event=2, gender="F", distance="5000")
    mgr.set_status(women, "racing")
    mgr.set_gun(women, now - timedelta(minutes=16))
    return app, mgr, men, women


def finish(bib):
    r = read("FINISH", bib)
    r.timestamp = datetime.now()
    return r


class TestStragglerFromAClosedRace(unittest.TestCase):

    def test_boy_from_the_closed_race_stays_out_of_finishlynx(self):
        app, mgr, men, women = meet()
        app._on_tag_read(finish("1190"))
        self.assertEqual(app.finishlynx.sent, [])
        r = next(r for r in mgr.reads.values() if r.bib == "1190")
        self.assertEqual(r.unsent, "other")
        if r.state == "assigned":                      # in his own race's pane, flagged
            row = next(x for x in mgr.results(r.race)["finishers"] if x["bib"] == "1190")
            self.assertTrue(row["finish"]["not_in_finishlynx"])

    def test_girl_in_the_running_race_goes(self):
        app, mgr, men, women = meet()
        app._on_tag_read(finish("2001"))
        self.assertEqual(app.finishlynx.sent, ["2001"])

    def test_boy_held_with_no_boys_race_open_stays_out(self):
        app, mgr, men, women = meet()
        mgr.races[men].roster.discard("1192")          # not placed anywhere
        app._on_tag_read(finish("1192"))
        self.assertEqual(app.finishlynx.sent, [])

    def test_unknown_athlete_still_goes(self):
        # not in the .ppl: FinishLynx stays the record for finishes
        app, mgr, men, women = meet()
        app._on_tag_read(finish("9999"))
        self.assertEqual(app.finishlynx.sent, ["9999"])

    def test_straggler_goes_when_nothing_else_is_running(self):
        app, mgr, men, women = meet()
        mgr.set_status(women, "final")
        app._on_tag_read(finish("1190"))
        self.assertEqual(app.finishlynx.sent, ["1190"])


class TestStragglerMovedFromHeld(unittest.TestCase):
    """The operator's flow: a Men's straggler crosses after the Men's race
    was closed and the girls run -> Held; the operator assigns him to the
    closed Men's race -> he must NOT go to FinishLynx (it has the girls
    open); his chip finish goes into the .lif at once although FinishLynx
    lists him untimed (user's rule 2026-10-03: placing him IS the release)."""

    def tearDown(self):
        _OPEN["value"] = None

    def test_assigning_by_hand_does_not_send_him_into_the_girls_event(self):
        app, mgr, men, women = meet()
        _OPEN["value"] = (2, 1, 1, "Women's 5000 Meters")
        app._on_tag_read(finish("1190"))
        r = next(r for r in mgr.reads.values() if r.bib == "1190")
        self.assertEqual((r.state, r.unsent), ("held", "other"))
        mgr.assign_read(r.id, men)
        note = app.send_restored_read(r.id)
        self.assertEqual(app.finishlynx.sent, [])
        self.assertTrue(note and note.startswith("not sent"), note)
        self.assertEqual((r.state, r.race, r.unsent), ("assigned", men, "other"))
        mgr._fl_untimed[men] = {"1190"}                 # FinishLynx's Men's file: entered, no time
        fin = next(x for x in mgr.results(men)["finishers"] if x["bib"] == "1190")["finish"]
        self.assertTrue(fin["not_in_finishlynx"])
        self.assertEqual((fin["fl_untimed"], fin["chip_in_lif"]), (False, True))

    def test_assigned_to_the_open_race_it_does_go(self):
        app, mgr, men, women = meet()
        _OPEN["value"] = (1, 1, 1, "Men's 5000 Meters")    # operator went back to the Men's event
        mgr.set_status(women, "final")
        mgr.set_status(men, "racing")
        mgr.races[men].roster.discard("1190")
        app.race_manager.athlete_lookup = lambda b: None   # unknown bib: held, sent as before
        app._on_tag_read(finish("1190"))
        self.assertEqual(app.finishlynx.sent, ["1190"])


class TestHeldReadDetails(unittest.TestCase):
    """Held reads carry who the bib is and the film correction a finish
    would get, so the Assign dialog can show the time in each race."""

    def test_held_row_has_name_gender_team(self):
        app, mgr, men, women = meet()
        mgr.film.set_manual(0.3)
        app._on_tag_read(finish("1192"))
        h = next(x for x in mgr.held_reads() if x["bib"] == "1192")
        self.assertEqual((h["last_name"], h["gender"], h["finish"]), ("hanko", "M", True))
        self.assertEqual(h["corrections"], {str(men): 0.3, str(women): 0.3})   # per race

    def test_unknown_bib_is_blank_not_an_error(self):
        app, mgr, men, women = meet()
        app._on_tag_read(finish("4242"))
        h = next(x for x in mgr.held_reads() if x["bib"] == "4242")
        self.assertEqual((h["last_name"], h["gender"], h["school"]), ("", "", ""))


class TestArmedEvent(unittest.TestCase):

    def test_finisher_of_a_race_finishlynx_has_not_armed_stays_out(self):
        app, mgr, men, women = meet()
        mgr.set_status(men, "racing")                  # both running (reopened)
        app.scoreboard_listener = SimpleNamespace(event=2, round=1, heat=1)
        app._on_tag_read(finish("1190"))
        self.assertEqual(app.finishlynx.sent, [])
        app._on_tag_read(finish("2001"))
        self.assertEqual(app.finishlynx.sent, ["2001"])

    def test_armed_event_unknown_two_running_both_go(self):
        # event 0 on the scoreboard feed (as on this rig): no basis to judge
        app, mgr, men, women = meet()
        mgr.set_status(men, "racing")
        app.scoreboard_listener = SimpleNamespace(event=0, round=0, heat=0)
        app._on_tag_read(finish("1190"))
        app._on_tag_read(finish("2001"))
        self.assertEqual(app.finishlynx.sent, ["1190", "2001"])


class TestOpenEventFromTheWindow(unittest.TestCase):
    """The 9/30 case exactly: both races running (the Men's kept open for
    stragglers), scoreboard feed silent about the event, FinishLynx's window
    'FinishLynx - [Women's 5000 Meters (2-1-1)]'."""

    def tearDown(self):
        _OPEN["value"] = None

    def test_boy_of_the_other_running_race_stays_out(self):
        app, mgr, men, women = meet()
        mgr.set_status(men, "racing")
        app.scoreboard_listener = SimpleNamespace(event=0, round=0, heat=0)
        _OPEN["value"] = (2, 1, 1, "Women's 5000 Meters")
        app._on_tag_read(finish("1190"))
        app._on_tag_read(finish("2001"))
        self.assertEqual(app.finishlynx.sent, ["2001"])
        r = next(r for r in mgr.reads.values() if r.bib == "1190")
        self.assertEqual((r.state, r.race, r.unsent), ("assigned", men, "other"))
        row = next(x for x in mgr.results(men)["finishers"] if x["bib"] == "1190")
        self.assertTrue(row["finish"]["not_in_finishlynx"])

    def test_scoreboard_event_wins_over_the_window(self):
        app, mgr, men, women = meet()
        mgr.set_status(men, "racing")
        app.scoreboard_listener = SimpleNamespace(event=1, round=1, heat=1)
        _OPEN["value"] = (2, 1, 1, "Women's 5000 Meters")
        self.assertEqual(app.finishlynx_event(), 1)

    def test_title_parsing(self):
        self.assertEqual(fl_window.parse_title("FinishLynx - [Women's 5000 Meters (2-1-1)]"),
                         (2, 1, 1, "Women's 5000 Meters"))
        self.assertIsNone(fl_window.parse_title("FinishLynx"))
        self.assertIsNone(fl_window.parse_title("Options"))


class TestPanesOff(unittest.TestCase):

    def test_no_race_manager_sends_everything(self):
        app = make_app("finish")
        app._on_tag_read(finish("1190"))
        self.assertEqual(app.finishlynx.sent, ["1190"])


if __name__ == "__main__":
    unittest.main()
