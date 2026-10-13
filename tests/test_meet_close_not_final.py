"""Close meet's plan names the races that are started but not final, and
the dialog warns (field test, 2026-10-13; from the camera branch).
python -m pytest tests/test_meet_close_not_final.py
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 13, 17, 0, 0)


class Close(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="close-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mgr = RaceManager(finish_readers={"FINISH"}, state_path=os.path.join(self.tmp, "races-state.json"))
        self.k = self.mgr.create_race("Boys", event=2, gender="M")
        self.mgr.create_race("Girls", event=1, gender="F")          # no gun: not listed
        self.mgr.set_gun(self.k, GUN)
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.mgr, meet_service=None, hardware_allowed=False)
        self.c = web.app.test_client()

    def test_plan_names_races_not_final(self):
        d = self.c.get("/api/meet/close/plan").get_json()
        self.assertEqual(d["not_final"], ["Boys"])
        self.mgr.set_status(self.k, "final")
        self.assertEqual(self.c.get("/api/meet/close/plan").get_json()["not_final"], [])

    def test_the_dialog_warns(self):
        with open(os.path.join(os.path.dirname(web.__file__), "templates", "console.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("plan.not_final", html)
        self.assertIn("Not final yet", html)


if __name__ == "__main__":
    unittest.main()
