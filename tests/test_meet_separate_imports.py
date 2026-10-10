"""The same meet imported on two PCs is never merged, and bibs stay the same
on every PC (2026-10-10: the Jim Crawford and Bob Jenkins meets were imported
from MileSplit on two PCs a day apart, MileSplit's entries had changed in
between, every bib after the first change was numbered differently, and the
copies were merged by athlete id - so race day showed the wrong names).
python -m unittest tests.test_meet_separate_imports"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet import bibs, importers
from rfid_finishlynx.meet.model import MeetInfo, MeetStore
from rfid_finishlynx.meet_service import MeetError, MeetService

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")
MID = "2026-10-10-sync-xc"


def entries(drop_first_runner=False):
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    if drop_first_runner:                       # MileSplit a day later: one entry gone
        data[0]["roster"] = data[0]["roster"][1:]
    return data


def import_here(store_root, data, origin=""):
    """One PC's own import of the meet, numbered from 100 (as on Tuesday)."""
    store = MeetStore(store_root)
    meet, _ = importers.import_milesplit(data, MeetInfo(id=MID, name="Sync XC", date="2026-10-10",
                                                        origin=origin))
    bibs.assign(meet, ranges=[[100, None]])
    store.save(meet)
    return meet


class Pcs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsep-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(self.shared)
        self.stores = {}
        self.pcs = {n: self.pc(n) for n in ("MEETPRO", "FEDORA")}

    def pc(self, name):
        root = os.path.join(self.tmp, name, "meet")
        os.makedirs(root)
        self.stores[name] = root
        return ms.MeetSync(ms.DirStore(root), ms.FolderTransport(self.shared), name,
                           os.path.join(self.tmp, name, "state"))

    def load(self, name):
        return MeetStore(self.stores[name]).load(MID)

    def bibs_by_name(self, name):
        return {(a.first, a.last): a.bib for a in self.load(name).athletes}


class TestSeparateImports(Pcs):

    def test_two_imports_a_day_apart_are_not_merged(self):
        printed = import_here(self.stores["MEETPRO"], entries())
        other = import_here(self.stores["FEDORA"], entries(drop_first_runner=True))
        mine_before = self.bibs_by_name("MEETPRO")
        self.assertNotEqual(mine_before, self.bibs_by_name("FEDORA"))      # the shift
        self.pcs["FEDORA"].sync_once()
        rep = self.pcs["MEETPRO"].sync_once()
        self.assertEqual(rep["separate"], {MID: ["FEDORA"]})
        self.assertEqual(rep["merged"], [])
        self.assertEqual(self.bibs_by_name("MEETPRO"), mine_before)         # nothing merged in
        rec = self.pcs["MEETPRO"].state["separate"][MID]["FEDORA"]
        self.assertEqual(rec["why"], "bibs")
        self.assertGreater(rec["bib_diffs"], 0)
        self.assertEqual(len(printed.athletes) - 1, len(other.athletes))
        # and the other way round
        self.pcs["MEETPRO"].sync_once()
        self.assertEqual(self.pcs["FEDORA"].sync_once()["separate"], {MID: ["MEETPRO"]})
        self.assertNotEqual(self.bibs_by_name("FEDORA"), mine_before)

    def test_use_theirs_on_one_pc_then_both_are_one_meet(self):
        import_here(self.stores["MEETPRO"], entries())
        import_here(self.stores["FEDORA"], entries(drop_first_runner=True))
        printed = self.bibs_by_name("MEETPRO")
        for _ in range(2):
            for pc in self.pcs.values():
                pc.sync_once()
        # MEETPRO's bibs were handed out: FEDORA takes them
        self.assertTrue(self.pcs["FEDORA"].resolve_separate(MID, "MEETPRO", "theirs"))
        self.assertEqual(self.bibs_by_name("FEDORA"), printed)
        kept = os.path.join(self.tmp, "FEDORA", "state", "replaced", MID)
        self.assertEqual(len(os.listdir(kept)), 1)                          # its own copy saved
        self.pcs["FEDORA"].sync_once()
        self.pcs["MEETPRO"].sync_once()
        self.assertEqual(self.pcs["MEETPRO"].state["separate"], {})
        self.assertEqual(self.pcs["FEDORA"].state["separate"], {})
        # from now on edits travel both ways
        store = MeetStore(self.stores["FEDORA"])
        m = store.load(MID)
        m.athletes[0].bib = "999"
        store.save(m)
        self.pcs["FEDORA"].sync_once()
        self.assertEqual(self.pcs["MEETPRO"].sync_once()["merged"], [MID])
        self.assertEqual(self.load("MEETPRO").athletes[0].bib, "999")

    def test_keep_mine_stops_asking_until_their_copy_changes(self):
        import_here(self.stores["MEETPRO"], entries())
        import_here(self.stores["FEDORA"], entries(drop_first_runner=True))
        self.pcs["FEDORA"].sync_once()
        self.pcs["MEETPRO"].sync_once()
        self.assertTrue(self.pcs["MEETPRO"].resolve_separate(MID, "FEDORA", "mine"))
        self.pcs["MEETPRO"].seen.clear()                  # as after a restart
        self.assertNotIn("separate", self.pcs["MEETPRO"].sync_once())
        store = MeetStore(self.stores["FEDORA"])
        m = store.load(MID)
        m.athletes[0].first = "Changed"
        store.save(m)
        self.pcs["FEDORA"].sync_once()
        self.assertEqual(self.pcs["MEETPRO"].sync_once()["separate"], {MID: ["FEDORA"]})

    def test_different_origins_are_never_merged(self):
        import_here(self.stores["MEETPRO"], entries(), origin="aaaa")
        import_here(self.stores["FEDORA"], entries(), origin="bbbb")      # same entries even
        self.pcs["FEDORA"].sync_once()
        self.assertEqual(self.pcs["MEETPRO"].sync_once()["separate"], {MID: ["FEDORA"]})
        self.assertEqual(self.pcs["MEETPRO"].state["separate"][MID]["FEDORA"]["why"], "origin")

    def test_one_import_shared_merges_as_always(self):
        import_here(self.stores["MEETPRO"], entries(), origin="aaaa")
        self.pcs["MEETPRO"].sync_once()
        self.assertEqual(self.pcs["FEDORA"].sync_once()["added"], [MID])
        self.assertEqual(self.load("FEDORA").meet.origin, "aaaa")
        store = MeetStore(self.stores["FEDORA"])
        m = store.load(MID)
        m.athletes[1].bib = "555"
        store.save(m)
        self.pcs["FEDORA"].sync_once()
        rep = self.pcs["MEETPRO"].sync_once()
        self.assertEqual((rep["merged"], rep.get("separate")), ([MID], None))
        self.assertEqual(self.load("MEETPRO").athletes[1].bib, "555")

    def test_separate_copies_rules(self):
        a = {"meet": {"meet": {"origin": ""}, "teams": [{"id": "t1", "name": "Ridge"}],
                      "athletes": [{"id": "a1", "first": "Ann", "last": "Lee", "team_id": "t1",
                                    "gender": "F", "bib": "100"}]}}
        b = copy.deepcopy(a)
        self.assertIsNone(ms.separate_copies(a, b, have_base=False))
        b["meet"]["athletes"][0]["bib"] = "0101"
        self.assertEqual(ms.separate_copies(a, b, have_base=False)["examples"],
                         [{"name": "Ann Lee", "mine": "100", "theirs": "101"}])
        self.assertIsNone(ms.separate_copies(a, b, have_base=True))      # a real three-way merge
        b["meet"]["athletes"][0].update(bib="100", first="Bo")             # a1 is someone else
        self.assertEqual(ms.separate_copies(a, b, have_base=False)["why"], "ids")
        a["meet"]["meet"]["origin"] = b["meet"]["meet"]["origin"] = "x"
        self.assertIsNone(ms.separate_copies(a, b, have_base=False))


class TestBibsEverywhere(Pcs):

    def test_one_bib_on_two_athletes_after_a_merge_is_flagged_until_fixed(self):
        import_here(self.stores["MEETPRO"], entries(), origin="aaaa")
        self.pcs["MEETPRO"].sync_once()
        self.pcs["FEDORA"].sync_once()
        for name, i in (("MEETPRO", 0), ("FEDORA", 1)):        # the same next number, apart
            store = MeetStore(self.stores[name])
            m = store.load(MID)
            m.athletes[i].bib = "7000"
            store.save(m)
        self.pcs["FEDORA"].sync_once()
        rep = self.pcs["MEETPRO"].sync_once()
        self.assertEqual(list(rep["bib_dups"][MID]), ["7000"])
        self.assertEqual(list(self.pcs["MEETPRO"].state["bib_dups"][MID]), ["7000"])
        store = MeetStore(self.stores["MEETPRO"])
        m = store.load(MID)
        m.athletes[1].bib = "7001"
        store.save(m)
        self.pcs["MEETPRO"].sync_once()
        self.assertEqual(self.pcs["MEETPRO"].state["bib_dups"], {})

    def test_a_bib_edit_goes_out_on_the_next_tick(self):
        t = [1000.0]
        r = ms.SyncRunner(self.pcs["MEETPRO"], clock=lambda: t[0])
        r.run_round()
        r.notify_saved(MID)
        t[0] = 1010
        self.assertFalse(r.due())                         # an ordinary save waits a minute
        r.notify_saved(MID, urgent=True)
        self.assertTrue(r.due())
        r.run_round()
        self.assertFalse(r.due())


class TestIds(unittest.TestCase):

    def test_a_refresh_adds_runners_by_their_milesplit_id(self):
        """Two PCs refreshing the same meet add the same new runner under one
        id, and nobody else's: ids from a refresh are never a<n>."""
        meet, _ = importers.import_milesplit(entries(drop_first_runner=True),
                                             MeetInfo(id=MID, name="Sync XC", date="2026-10-10"))
        self.assertTrue(all(a.id.startswith("a") and a.id[1:].isdigit() for a in meet.athletes))
        other = copy.deepcopy(meet)
        importers.refresh_milesplit(meet, entries())
        importers.refresh_milesplit(other, entries())
        new = [a for a in meet.athletes if not a.id[1:].isdigit()]
        self.assertEqual([a.id for a in new], [f"ms{new[0].milesplit_id}"])
        self.assertEqual([a.id for a in other.athletes], [a.id for a in meet.athletes])


class TestNoSecondImport(unittest.TestCase):
    """A meet already on this PC (its own, or another PC's brought in by
    sync) is refused as a new import: Refresh takes MileSplit's changes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsep-svc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        app = SimpleNamespace(race_manager=None, hardware_allowed=False,
                              lif_dirs=lambda: ("", ""), config=SimpleNamespace(event_directory=""))
        self.svc = MeetService(os.path.join(self.tmp, "meet"), app,
                               token_path=os.path.join(self.tmp, "tok.json"),
                               transport=lambda *a: (500, None), emit=lambda s: None,
                               autoload_evt=lambda d: None)
        self.rounds = []
        self.svc.meet_sync = SimpleNamespace(sync_now=lambda: self.rounds.append(1) or {},
                                             notify_saved=lambda *a, **k: None)

    def test_same_milesplit_meet_is_refused(self):
        self.svc.import_milesplit(entries(), name="Sync XC", day="2026-10-10", meet_id=766577)
        self.assertEqual(len(self.rounds), 1)                     # looked at the other PCs first
        self.assertTrue(self.svc.meet.meet.origin)
        self.svc.detach()
        with self.assertRaises(MeetError) as e:
            self.svc.import_milesplit(entries(), name="Renamed", day="2026-10-11", meet_id=766577)
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(e.exception.details["existing"], "2026-10-10-sync-xc")
        with self.assertRaises(MeetError):
            self.svc.check_new_meet(766577)                       # before the fetch, too
        with self.assertRaises(MeetError):
            self.svc.new_meet({"name": "Sync XC", "date": "2026-10-10"})
        self.svc.open_meet("2026-10-10-sync-xc")                  # refresh is fine
        rep, _ = self.svc.import_milesplit(entries(), meet_id=766577)
        self.assertEqual(rep["added"], [])

    def test_offline_import_says_it_could_not_check(self):
        self.svc.meet_sync = SimpleNamespace(sync_now=lambda: {"error": "NAS: git fetch timed out"},
                                             notify_saved=lambda *a, **k: None)
        rep, _ = self.svc.import_milesplit(entries(), name="Sync XC", day="2026-10-10")
        self.assertTrue(any("Couldn't reach the other PCs" in n for n in rep["notes"]))

    def test_assign_looks_first_and_bib_edits_go_out_at_once(self):
        self.svc.import_milesplit(entries(), name="Sync XC", day="2026-10-10")
        pushed = []
        self.svc.meet_sync = SimpleNamespace(sync_now=lambda: self.rounds.append(1) or {},
                                             notify_saved=lambda m, urgent=False: pushed.append(urgent))
        before = len(self.rounds)
        self.svc.assign({"rows": [["100", ""]]})
        self.assertEqual(len(self.rounds), before + 1)
        self.assertEqual(pushed, [True])
        aid = self.svc.meet.athletes[0].id
        self.svc.update_athlete(aid, {"first": "Avery-Lynn"})        # not a bib: no rush
        self.assertEqual(pushed, [True])


if __name__ == "__main__":
    unittest.main()
