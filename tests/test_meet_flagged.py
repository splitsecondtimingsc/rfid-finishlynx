"""Flagged runners (2026-10-06, user): a runner who crosses in a race they
aren't entered in, wearing a bib nobody has, or scratched there, is flagged
on their row ("Not in this race" / "Unknown bib" / "Scratched") and left out
of the results until the operator picks Ignore, Swap bib with..., Give bib
to... or Override. Closing the race without deciding leaves them out but
keeps the flag; deciding after final re-finalizes by itself. Runners of
races run together are never flagged for each other.

Through the console's API against a real RaceManager:
python -m unittest tests.test_meet_flagged"""

from rfid_finishlynx import web
from tests.test_meet_divisions import DivisionMeet


class Flagged(DivisionMeet):

    def issues(self, rk):
        return {i["bib"]: i for i in self.results(rk)["issues"]}

    def placed(self, rk):
        return [f["bib"] for f in self.results(rk)["results"]["finishers"] if f["status"] == "finished"]

    def act(self, rk, bib, action, status=200, **kw):
        return self.post(f"/api/meet/races/{rk}/issues/{bib}", dict(action=action, **kw), status)


class TestSwappedBib(Flagged):
    """The user's case: a 7-9 kid crosses wearing a 10-12 kid's bib."""

    def setUp(self):
        super().setUp()
        young = self.bibs(self.young)
        self.ran = self.athlete(young[5])                 # the 7-9 kid who really ran
        self.owner = self.athlete(self.bibs(self.old)[0])  # whose bib he wore
        self.worn = self.owner.bib
        # he crosses 6th wearing the owner's bib; his own bib is never read
        self.order = young[:5] + [self.worn] + young[6:]
        self.cross(self.young, self.order)

    def test_flag_on_the_row_left_out_of_the_places(self):
        i = self.issues(self.young)[self.worn]
        self.assertEqual((i["code"], i["label"], i["athlete_id"], i["crossed"]),
                         ("not_entered", "Not in this race", self.owner.id, 6))
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual(i["entered_in"], [races[self.old]["name"]])
        self.assertNotIn(self.worn, self.placed(self.young))
        self.assertEqual(len(self.placed(self.young)), len(self.order) - 1)       # nobody pushed back
        pane = web._races_payload()["issues"][str(self.pane(self.young))]
        self.assertEqual(pane["issues"][self.worn]["label"], "Not in this race")
        races = {r["rk"]: r for r in self.get("/api/meet/races").get_json()["races"]}
        self.assertEqual((races[self.young]["issues"], races[self.old]["issues"]), (1, 0))

    def test_swap_bib_with_the_kid_who_ran(self):
        self.act(self.young, self.worn, "swap", athlete_id=self.ran.id)
        self.assertEqual(self.ran.bib, self.worn)
        self.assertEqual(self.issues(self.young), {})
        self.assertEqual(self.placed(self.young)[5], self.worn)
        row = next(f for f in self.results(self.young)["results"]["finishers"] if f["bib"] == self.worn)
        self.assertEqual(row["athlete_id"], self.ran.id)
        # the owner now holds the bib the kid never wore: still in his own race
        self.assertEqual([r.key for r, _ in self.svc.meet.entries_for(self.owner.id)], [self.key(self.old)])

    def test_closed_without_deciding_then_swapped_after_final(self):
        self.post(f"/api/meet/races/{self.young}/final")
        res = self.race(self.young).results
        self.assertEqual([i["bib"] for i in res["issues"]], [self.worn])
        self.assertNotIn(self.worn, [f["bib"] for f in res["finishers"]])
        fix = [f for f in self.get("/api/meet").get_json()["fix"] if f["kind"] == "issues"]
        self.assertEqual(fix[0]["detail"], [self.young])
        self.svc._uploads[self.young] = {"uploaded_at": "2026-10-10T12:00:00", "ok": True}
        r = self.act(self.young, self.worn, "swap", athlete_id=self.ran.id)["report"]
        self.assertEqual((r["refinalized"], r["milesplit_sent"], r["errors"]),
                         ([self.young], [self.young], []))
        self.assertEqual(self.race(self.young).status, "final")
        self.assertEqual(self.race(self.young).results["issues"], [])
        self.assertIn(self.worn, self.placed(self.young))
        self.assertEqual([f for f in self.get("/api/meet").get_json()["fix"] if f["kind"] == "issues"], [])

    def test_override_scores_the_owner_here(self):
        r = self.act(self.young, self.worn, "override")["report"]
        self.assertEqual(r["refinalized"], [])
        self.assertEqual([r.key for r, _ in self.svc.meet.entries_for(self.owner.id)], [self.key(self.young)])
        self.assertIn(self.worn, self.placed(self.young))

    def test_ignore_and_take_it_back(self):
        self.post(f"/api/meet/races/{self.young}/final")
        r = self.act(self.young, self.worn, "ignore")["report"]
        self.assertEqual(r["refinalized"], [self.young])
        d = self.results(self.young)
        self.assertEqual(d["issues"], [])
        self.assertEqual([(x["bib"], x["athlete_id"]) for x in d["ignored"]], [(self.worn, self.owner.id)])
        self.assertEqual(web._races_payload()["issues"][str(self.pane(self.young))]["ignored"], [self.worn])
        self.act(self.young, self.worn, "unignore")
        self.assertEqual(list(self.issues(self.young)), [self.worn])
        self.assertEqual(self.results(self.young)["ignored"], [])

    def test_refused_actions_change_nothing(self):
        before = self.svc.meet.to_dict()
        self.act(self.young, self.worn, "swap", 400)                               # nobody picked
        self.act(self.young, self.worn, "swap", 400, athlete_id=self.owner.id)     # with himself
        self.act(self.young, self.worn, "give", 409, athlete_id=self.ran.id)       # bib is held
        self.act(self.young, "98765", "override", 400)                            # nobody has it
        self.act(self.young, "98765", "swap", 400, athlete_id=self.ran.id)
        self.act(self.young, self.worn, "swap", 404, athlete_id="a99999")
        self.act(self.young, self.worn, "dance", 400)
        after = self.svc.meet.to_dict()
        after.pop("updated_at"), before.pop("updated_at")
        self.assertEqual(after, before)


class TestUnknownBib(Flagged):

    def test_give_bib_to_the_kid_who_ran(self):
        young = self.bibs(self.young)
        kid = self.athlete(young[2])                     # wore a spare bib, his own unread
        self.cross(self.young, young[:2] + ["98765"] + young[3:])
        self.assertEqual(self.issues(self.young)["98765"]["label"], "Unknown bib")
        self.act(self.young, "98765", "give", athlete_id=kid.id)
        self.assertEqual(kid.bib, "98765")
        self.assertEqual(self.issues(self.young), {})
        self.assertEqual(self.placed(self.young)[2], "98765")


class TestRunTogetherNeverFlagsEachOther(Flagged):

    def setup_panes(self):
        if getattr(self, "together", False):
            super().setup_panes()

    def setUp(self):
        super().setUp()
        self.post(f"/api/meet/races/{self.young}/run-together", {"races": [self.old]})
        self.together = True
        self.setup_panes()

    def test_both_races_finish_without_a_flag(self):
        self.cross(self.young, self.bibs(self.young))
        self.assertEqual(self.issues(self.young), {})
        self.assertNotIn(str(self.pane(self.young)), web._races_payload().get("issues", {}))
        self.post(f"/api/meet/races/{self.young}/final")
        self.assertEqual(self.race(self.young).results["issues"], [])
