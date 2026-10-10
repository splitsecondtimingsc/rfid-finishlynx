"""The bib check before a race (meet/bibcheck.py, 2026-10-10): bibs handed
out vs the meet's bibs vs FinishLynx's lynx.ppl, and its fixes.
python -m unittest tests.test_meet_bibcheck"""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from rfid_finishlynx.meet import bibcheck, bibs, importers
from rfid_finishlynx.meet.model import MeetInfo
from rfid_finishlynx.meet_service import MeetError, MeetService

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")


def entries():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def numbered():
    meet, _ = importers.import_milesplit(entries(), MeetInfo(id="m", name="M", date="2026-10-10"))
    bibs.assign(meet, ranges=[[100, None]])
    return meet


def runners(meet):
    return [a for a in meet.athletes if a.bib and meet.entered(a.id)]


class TestCheck(unittest.TestCase):

    def test_nothing_handed_out_nothing_flagged(self):
        meet = numbered()
        c = bibcheck.check(meet)
        self.assertTrue(c["ok"])
        self.assertEqual(c["recorded"], 0)

    def test_a_renumber_after_printing_is_flagged_per_race(self):
        meet = numbered()
        self.assertEqual(bibcheck.record(meet), len(runners(meet)))
        a, b = runners(meet)[:2]
        a.bib, b.bib = "900", ""                   # a bulk renumber / another PC's edit
        c = bibcheck.check(meet)
        self.assertFalse(c["ok"])
        self.assertEqual([(x["name"], x["printed"], x["now"]) for x in c["changed"]],
                         [(f"{a.first} {a.last}", a.printed_bib, "900"), (f"{b.first} {b.last}", b.printed_bib, "")])
        self.assertEqual(sum(c["races"].values()), 2)

    def test_put_back_clears_whoever_holds_the_number_now(self):
        meet = numbered()
        bibcheck.record(meet)
        a, b = runners(meet)[:2]
        printed_a = a.printed_bib
        a.bib, b.bib = "", printed_a               # b was given a's number (b's own is gone)
        rep = bibcheck.restore(meet, [a.id])
        self.assertEqual(a.bib, printed_a)
        self.assertEqual(b.bib, "")
        self.assertEqual([x["athlete_id"] for x in rep["cleared"]], [b.id])
        rep = bibcheck.restore(meet)               # the rest of them
        self.assertEqual(b.bib, b.printed_bib)
        self.assertTrue(bibcheck.check(meet)["ok"])
        self.assertEqual(meet.validate(), [])

    def test_lynx_ppl_naming_someone_else(self):
        meet = numbered()
        bibcheck.record(meet)
        a, b = runners(meet)[:2]
        ppl = {int(x.bib): (x.last, x.first, "") for x in runners(meet)}
        self.assertTrue(bibcheck.check(meet, ppl)["ok"])
        ppl[int(a.bib)] = (b.last, b.first, "")    # FinishLynx's list from a separate import
        ppl[4242] = ("Ghost", "Gus", "")
        del ppl[int(b.bib)]
        lx = bibcheck.check(meet, ppl)["lynx"]
        self.assertEqual([(w["bib"], w["wearer"]) for w in lx["wrong"]],
                         [(a.bib, f"{a.first} {a.last}"), ("4242", "")])
        self.assertEqual([m["bib"] for m in lx["missing"]], [b.bib])

    def test_the_wearer_is_who_was_handed_the_number(self):
        """The meet renumbered a runner and FinishLynx followed the meet: the
        lynx.ppl now names the new holder, but the printed bib is still on
        the first runner."""
        meet = numbered()
        bibcheck.record(meet)
        a, b = runners(meet)[:2]
        a.bib, b.bib = b.bib, a.bib                 # swapped in the meet only
        ppl = {int(x.bib): (x.last, x.first, "") for x in runners(meet)}
        c = bibcheck.check(meet, ppl)
        self.assertEqual(len(c["changed"]), 2)
        self.assertEqual(sorted(w["wearer"] for w in c["lynx"]["wrong"]),
                         sorted([f"{a.first} {a.last}", f"{b.first} {b.last}"]))

    def test_read_ppl(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "LYNX.PPL")
            with open(p, "wb") as f:
                f.write("; header\r\n0101,Pérez,Ana,Ridge,,F\r\n,Nobib,Ned,Ridge,,M\r\n".encode("cp1252"))
            self.assertEqual(bibcheck.ppl_path(d), p)
            self.assertEqual(bibcheck.read_ppl(p), {101: ("Pérez", "Ana", "Ridge")})
            self.assertIsNone(bibcheck.read_ppl(os.path.join(d, "none.ppl")))


class TestService(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bibcheck-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rfid = os.path.join(self.tmp, "Lynx", "RFID")
        app = SimpleNamespace(race_manager=None, hardware_allowed=True,
                              lif_dirs=lambda: (os.path.join(self.tmp, "Lynx"), self.rfid),
                              config=SimpleNamespace(event_directory=""))
        self.svc = MeetService(os.path.join(self.tmp, "meet"), app,
                               token_path=os.path.join(self.tmp, "tok.json"),
                               transport=lambda *a: (500, None), emit=lambda s: None,
                               autoload_evt=lambda d: None)
        self.svc.import_milesplit(entries(), name="Svc", day="2026-10-10")
        self.svc.assign({"rows": [["100", ""]]})

    def fix(self):
        return [f for f in self.svc.summary()["fix"] if f["kind"] == "bib_check"]

    def test_printing_records_then_a_renumber_is_flagged_and_put_back(self):
        self.assertEqual(self.svc.record_handed_out(), len(runners(self.svc.meet)))
        self.assertEqual(self.svc.record_handed_out(), 0)                    # already recorded
        self.assertTrue(self.svc.bib_check()["ok"])
        self.assertTrue(self.svc.bib_check()["lynx"]["checked"])           # the written lynx.ppl
        first = runners(self.svc.meet)[0]
        printed = first.printed_bib
        self.svc.clear_bibs({"all": True})
        self.svc.assign({"rows": [["500", ""]]})                            # a bulk renumber
        f = self.fix()
        self.assertEqual(len(f), 1)
        self.assertIn(f"printed {printed}", f[0]["text"])
        self.assertTrue(self.svc.summary()["bib_check"]["races"])
        report, _ = self.svc.fix_bibs({"action": "restore"})
        self.assertEqual(len(report["restored"]), len(runners(self.svc.meet)))
        self.assertEqual(self.svc.meet.athlete(first.id).bib, printed)
        self.assertEqual(self.fix(), [])
        with self.assertRaises(MeetError):
            self.svc.fix_bibs({"action": "restore"})                         # nothing left

    def test_keep_the_new_number(self):
        self.svc.record_handed_out()
        aid = runners(self.svc.meet)[0].id
        self.svc.clear_bibs({"athlete_ids": [aid]})
        self.svc.assign({"rows": [["700", ""]], "athlete_ids": [aid]})
        self.assertEqual(len(self.svc.bib_check()["changed"]), 1)
        self.svc.fix_bibs({"action": "accept", "athlete_ids": [aid]})
        self.assertEqual(self.svc.meet.athlete(aid).printed_bib, "700")
        self.assertTrue(self.svc.bib_check()["ok"])

    def test_a_bib_typed_at_the_table_follows_once_bibs_are_out(self):
        aid = runners(self.svc.meet)[0].id
        self.svc.set_bib(aid, "800")                                         # before: still assigning
        self.assertEqual(self.svc.meet.athlete(aid).printed_bib, "")
        self.svc.record_handed_out()
        self.svc.set_bib(aid, "801")                                         # a spare at the table
        self.assertEqual(self.svc.meet.athlete(aid).printed_bib, "801")
        self.assertTrue(self.svc.bib_check()["ok"])

    def test_finishlynx_start_list_from_elsewhere_and_write_again(self):
        self.svc.record_handed_out()
        ppl = bibcheck.ppl_path(self.rfid)
        a = runners(self.svc.meet)[0]
        with open(ppl, "wb") as f:                    # another PC's / program's start list
            f.write(f"{a.bib},Someone,Else,Ridge,,F\r\n".encode("cp1252"))
        chk = self.svc.bib_check()
        self.assertEqual([w["bib"] for w in chk["lynx"]["wrong"]], [a.bib])
        self.assertTrue(chk["lynx"]["missing"])
        self.svc.fix_bibs({"action": "write"})
        self.assertTrue(self.svc.bib_check()["ok"])


if __name__ == "__main__":
    unittest.main()
