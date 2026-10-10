"""Meet sync inside the console (2026-10-07): the meet OPEN here is merged
under the meet service's lock and reloaded (start lists rewritten), waits
while one of its races runs, and a clash can be settled per record.
python -m unittest tests.test_meet_sync_console"""

import os
from unittest import mock
from types import SimpleNamespace

from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet.model import MeetStore
from tests.test_meet_api import GUN, MeetApiBase


class ConsoleSync(MeetApiBase):

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.mid = self.svc.meet.meet.id
        self.shared = os.path.join(self.tmp, "Drive", "RFID meets")
        os.makedirs(self.shared)
        self.here = ms.MeetSync(ms.ServiceStore(self.svc), ms.FolderTransport(self.shared), "TIMINGPC",
                                os.path.join(self.tmp, "sync-here"))
        self.other_root = os.path.join(self.tmp, "other", "meet")
        os.makedirs(self.other_root)
        self.other = ms.MeetSync(ms.DirStore(self.other_root), ms.FolderTransport(self.shared), "MEETPRO",
                                 os.path.join(self.tmp, "sync-other"))
        self.here.sync_once()
        self.other.sync_once()

    def edit_other(self, fn):
        store = MeetStore(self.other_root)
        m = store.load(self.mid)
        fn(m)
        store.save(m)

    def test_open_meet_takes_the_other_machines_edit(self):
        a = self.svc.meet.athletes[0]
        self.edit_other(lambda m: setattr(m.athlete(a.id), "bib", "777"))
        self.other.sync_once()
        rep = self.here.sync_once()
        self.assertEqual(rep["merged"], [self.mid])
        self.assertEqual(self.svc.meet.athlete(a.id).bib, "777")           # in memory, not just on disk
        self.assertIn("777,", self.rfid_file("lynx.ppl"))                    # start lists rewritten
        self.assertEqual(MeetStore(os.path.join(self.tmp, "meet")).load(self.mid).athlete(a.id).bib, "777")

    def test_an_edit_here_meanwhile_is_kept(self):
        a, b = self.svc.meet.athletes[0], self.svc.meet.athletes[1]
        self.post(f"/api/meet/athletes/{b.id}/bib", {"bib": "888"})
        self.edit_other(lambda m: setattr(m.athlete(a.id), "bib", "777"))
        self.other.sync_once()
        self.here.sync_once()
        self.assertEqual((self.svc.meet.athlete(a.id).bib, self.svc.meet.athlete(b.id).bib), ("777", "888"))
        self.other.sync_once()
        o = MeetStore(self.other_root).load(self.mid)
        self.assertEqual((o.athlete(a.id).bib, o.athlete(b.id).bib), ("777", "888"))

    def test_waits_while_a_race_runs(self):
        self.assign_all()
        panes = self.make_panes()
        self.mgr.set_gun(next(iter(panes.values())), GUN)
        a = self.svc.meet.athletes[0]
        before = a.bib
        self.edit_other(lambda m: setattr(m.athlete(a.id), "first", "Renamed"))
        self.other.sync_once()
        rep = self.here.sync_once()
        self.assertEqual(rep.get("waiting"), [self.mid])
        self.assertNotEqual(self.svc.meet.athlete(a.id).first, "Renamed")
        for k in panes.values():
            self.mgr.set_status(k, "closed")
        self.here.sync_once()                                   # next round: applied
        self.assertEqual(self.svc.meet.athlete(a.id).first, "Renamed")
        self.assertEqual(self.svc.meet.athlete(a.id).bib, before)

    def test_take_theirs_settles_a_clash(self):
        a = self.svc.meet.athletes[0]
        self.post(f"/api/meet/athletes/{a.id}/bib", {"bib": "901"})
        self.edit_other(lambda m: setattr(m.athlete(a.id), "bib", "902"))
        self.other.sync_once()
        self.here.sync_once()
        self.assertEqual(self.svc.meet.athlete(a.id).bib, "901")             # ours kept
        clash = self.here.state["conflicts"][self.mid]
        self.assertEqual((clash[0]["mine"], clash[0]["theirs"]), ("901", "902"))
        self.assertTrue(self.here.resolve(self.mid, 0, "theirs"))
        self.assertEqual(self.svc.meet.athlete(a.id).bib, "902")
        self.assertNotIn(self.mid, self.here.state["conflicts"])


class SyncApi(MeetApiBase):
    """/api/meet/sync*: status, settings saved and the runner restarted."""

    def setUp(self):
        super().setUp()
        self.shared = os.path.join(self.tmp, "Drive")
        os.makedirs(self.shared)
        saved = []
        self.app.config = SimpleNamespace(meet_sync=SimpleNamespace(enabled=False, mode="off", folder="",
                                                                     remote="", machine="TEST", git=""))
        self.app.save_config = lambda: saved.append(True)
        self.app.meet_sync = None
        self.saved = saved

        def start():                                    # as app.start_meet_sync does
            engines = ms.engines_from_config(self.app.config, self.svc.root, os.path.join(self.tmp, "st"),
                                             store=ms.ServiceStore(self.svc))
            self.app.meet_sync = ms.SyncGroup([(n, ms.SyncRunner(s)) for n, s in engines]) if engines else None
        self.app.start_meet_sync = start

    def test_settings_then_sync_now(self):
        self.assertEqual(self.get("/api/meet/sync").get_json()["mode"], "off")
        self.post("/api/meet/sync", status=409)
        self.post("/api/meet/sync/settings", {"mode": "folder", "folder": os.path.join(self.tmp, "nope")}, 400)
        d = self.post("/api/meet/sync/settings", {"mode": "folder", "folder": self.shared})
        self.assertEqual((d["sync"]["mode"], d["sync"]["enabled"], self.saved), ("folder", True, [True]))
        self.import_fixture()
        d = self.post("/api/meet/sync")
        self.assertEqual(d["report"]["published"], [self.svc.meet.meet.id])
        self.assertTrue(os.path.isfile(os.path.join(self.shared, self.svc.meet.meet.id, "TEST", "manifest.json")))
        self.assertEqual(self.get("/api/meet/sync").get_json()["last_error"], "")

    def test_the_public_edition_shares_through_google_drive_only(self):
        # 2026-10-10, user: the public package has no NAS sync at all
        nas = os.path.join(self.tmp, "NAS share")
        os.makedirs(nas)
        with mock.patch.object(ms, "edition", return_value={"edition": "public", "sync_targets": ["folder"]}):
            d = self.get("/api/meet/sync").get_json()
            self.assertEqual(d["targets_allowed"], ["folder"])
            self.post("/api/meet/sync/settings", {"mode": "git", "remote": nas}, 400)
            self.post("/api/meet/sync/settings", {"mode": "both", "folder": self.shared, "remote": nas}, 400)
            d = self.post("/api/meet/sync/settings", {"mode": "folder", "folder": self.shared})
            self.assertEqual([t["target"] for t in d["sync"]["targets"]], ["folder"])
            # config.yaml edited by hand to the NAS: still never started
            self.app.config.meet_sync.mode, self.app.config.meet_sync.remote = "both", nas
            self.app.start_meet_sync()
            self.assertEqual([t["target"] for t in self.get("/api/meet/sync").get_json()["targets"]], ["folder"])
            self.assertEqual(self.get("/api/meet/sync").get_json()["mode"], "folder")
            self.app.config.meet_sync.mode = "git"
            self.app.start_meet_sync()
            self.assertIsNone(self.app.meet_sync)
        # a package built before sync_targets: known by its name
        self.assertEqual(ms.allowed_targets({"edition": "public"}), ("folder",))
        self.assertEqual(ms.allowed_targets({"edition": "fleet"}), ("git", "folder"))
        self.assertEqual(ms.allowed_targets({}), ("git", "folder"))           # a development checkout

    def test_both_places_get_every_meet_and_one_offline_holds_nothing_up(self):
        # 2026-10-08: the NAS (here a folder, as a network share) and Google Drive at once
        nas = os.path.join(self.tmp, "NAS share")
        os.makedirs(nas)
        self.post("/api/meet/sync/settings", {"mode": "both", "folder": self.shared,
                                               "remote": os.path.join(self.tmp, "nope")}, 400)
        d = self.post("/api/meet/sync/settings", {"mode": "both", "folder": self.shared, "remote": nas})
        self.assertEqual([t["target"] for t in d["sync"]["targets"]], ["git", "folder"])
        self.import_fixture()
        mid = self.svc.meet.meet.id
        d = self.post("/api/meet/sync")
        self.assertEqual((d["report"]["published"], d["report"]["errors"]), ([mid], {}))
        for place in (nas, self.shared):
            self.assertTrue(os.path.isfile(os.path.join(place, mid, "TEST", "manifest.json")), place)
        os.rename(self.shared, self.shared + "-away")                # Drive not mounted
        self.post(f"/api/meet/athletes/{self.svc.meet.athletes[0].id}/bib", {"bib": "4321"})
        d = self.post("/api/meet/sync")
        self.assertEqual(d["report"]["published"], [mid])             # the NAS still took it
        self.assertIn("folder", d["report"]["errors"])
        self.assertIn("Shared folder:", d["sync"]["last_error"])
        os.rename(self.shared + "-away", self.shared)
        d = self.post("/api/meet/sync")
        self.assertEqual((d["report"]["published"], d["report"]["errors"], d["sync"]["last_error"]), ([mid], {}, ""))
