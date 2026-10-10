"""Meet sync (meet_sync.py, docs/MEET-SYNC-PLAN.md): the per-record merge,
then two "machines" (two meet stores) sharing meets through a shared folder
(Google Drive for desktop) and through a private git repo (a local bare
repo stands in).
python -m unittest tests.test_meet_sync"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet import importers
from rfid_finishlynx.meet.model import MeetInfo, MeetStore

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "milesplit", "entries-xc.json")


class TestMerge(unittest.TestCase):

    def meet(self, **athletes):
        return {"meet": {"meet": {"name": "M"}, "athletes": [
            {"id": k, "first": v[0], "bib": v[1]} for k, v in athletes.items()], "updated_at": "1"},
            "files": {}}

    def test_different_athletes_both_kept(self):
        base = self.meet(a1=("Ann", "100"), a2=("Bo", "101"))
        mine = self.meet(a1=("Ann", "200"), a2=("Bo", "101"))
        theirs = self.meet(a1=("Ann", "100"), a2=("Bo", "201"))
        out, c = ms.merge3(base, mine, theirs)
        self.assertEqual([a["bib"] for a in out["meet"]["athletes"]], ["200", "201"])
        self.assertEqual(c, [])

    def test_different_fields_of_one_athlete_both_kept(self):
        base = self.meet(a1=("Ann", "100"))
        mine = self.meet(a1=("Anne", "100"))
        theirs = self.meet(a1=("Ann", "300"))
        out, c = ms.merge3(base, mine, theirs)
        self.assertEqual(out["meet"]["athletes"][0], {"id": "a1", "first": "Anne", "bib": "300"})
        self.assertEqual(c, [])

    def test_same_field_both_sides_keeps_mine_and_reports(self):
        base, mine, theirs = self.meet(a1=("Ann", "100")), self.meet(a1=("Ann", "200")), self.meet(a1=("Ann", "300"))
        out, c = ms.merge3(base, mine, theirs)
        self.assertEqual(out["meet"]["athletes"][0]["bib"], "200")
        self.assertEqual([(x["path"], x["mine"], x["theirs"]) for x in c],
                         [("meet.athletes[a1].bib", "200", "300")])

    def test_added_on_both_sides_and_removed_on_one(self):
        base = self.meet(a1=("Ann", "100"), a2=("Bo", "101"))
        mine = self.meet(a1=("Ann", "100"), a2=("Bo", "101"), a3=("Cy", "102"))
        theirs = self.meet(a1=("Ann", "100"), a4=("Di", "103"))            # a2 removed there
        out, c = ms.merge3(base, mine, theirs)
        self.assertEqual([a["id"] for a in out["meet"]["athletes"]], ["a1", "a3", "a4"])
        self.assertEqual(c, [])

    def test_race_entries_merge_per_athlete(self):
        def race(*ids):
            return {"meet": {"races": [{"event": 1, "round": 1, "heat": 1,
                                        "entries": [{"athlete_id": i, "event": 1, "status": "entered"} for i in ids]}]},
                    "files": {}}
        out, c = ms.merge3(race("a1"), race("a1", "a2"), race("a1", "a3"))
        self.assertEqual([e["athlete_id"] for e in out["meet"]["races"][0]["entries"]], ["a1", "a2", "a3"])

    def test_updated_at_and_pulls(self):
        base = {"meet": {"updated_at": "1"}, "files": {"pulls/entries.json": [1]}}
        mine = {"meet": {"updated_at": "3"}, "files": {"pulls/entries.json": [1, 2]}}
        theirs = {"meet": {"updated_at": "2"}, "files": {"pulls/entries.json": [1, 3]}}
        out, c = ms.merge3(base, mine, theirs)
        self.assertEqual(out["meet"]["updated_at"], "3")
        self.assertEqual(out["files"]["pulls/entries.json"], [1, 2])          # one file = one value
        self.assertEqual([x["path"] for x in c], ["files.pulls/entries.json"])


def make_meet(store_root: str, name="Sync XC") -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        entries = json.load(f)
    store = MeetStore(store_root)
    meet, _ = importers.import_milesplit(entries, MeetInfo(id="2026-10-10-sync-xc", name=name, date="2026-10-10"))
    store.save(meet)
    store.save_pull(meet.meet.id, "entries", entries)
    return meet.meet.id


class Machines:
    """Two consoles' meet stores and their sync engines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsync-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.a_store, self.b_store = (os.path.join(self.tmp, n, "meet") for n in ("a", "b"))
        os.makedirs(self.a_store)
        os.makedirs(self.b_store)
        self.a = ms.MeetSync(ms.DirStore(self.a_store), self.transport("A"), "TIMINGPC",
                             os.path.join(self.tmp, "a", "state"))
        self.b = ms.MeetSync(ms.DirStore(self.b_store), self.transport("B"), "MEETPRO",
                             os.path.join(self.tmp, "b", "state"))

    def edit(self, store_root, meet_id, fn):
        store = MeetStore(store_root)
        m = store.load(meet_id)
        fn(m)
        store.save(m)

    def bib(self, store_root, meet_id, aid):
        return MeetStore(store_root).load(meet_id).athlete(aid).bib

    def test_a_meet_made_on_one_machine_appears_on_the_other(self):
        mid = make_meet(self.a_store)
        self.assertEqual(self.a.sync_once()["published"], [mid])
        rep = self.b.sync_once()
        self.assertEqual(rep["added"], [mid])
        b = MeetStore(self.b_store).load(mid)
        self.assertEqual(b.meet.name, "Sync XC")
        self.assertEqual(len(b.athletes), len(MeetStore(self.a_store).load(mid).athletes))
        self.assertIsNotNone(MeetStore(self.b_store).load_pull(mid, "entries"))

    def test_edits_on_both_machines_meet_in_the_middle(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a2"), "bib", "902"))
        self.a.sync_once()
        rep = self.b.sync_once()
        self.assertEqual(rep["merged"], [mid])
        self.a.sync_once()
        for store in (self.a_store, self.b_store):
            self.assertEqual((self.bib(store, mid, "a1"), self.bib(store, mid, "a2")), ("901", "902"))
        self.assertEqual(self.a.state["conflicts"], {})
        self.assertEqual(self.b.state["conflicts"], {})

    def test_same_bib_changed_on_both_keeps_each_own_and_records_it(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "902"))
        self.a.sync_once()
        rep = self.b.sync_once()
        self.assertEqual(self.bib(self.b_store, mid, "a1"), "902")
        c = rep["conflicts"][mid][0]
        self.assertEqual((c["path"], c["mine"], c["theirs"]), ("meet.athletes[a1].bib", "902", "901"))
        self.assertEqual(self.b.state["conflicts"][mid][0]["machine"], "TIMINGPC")

    def test_a_save_that_changed_nothing_is_not_sent(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.edit(self.a_store, mid, lambda m: None)              # updated_at only
        self.assertEqual(self.a.sync_once()["published"], [])

    def test_a_new_shared_place_gets_every_meet(self):
        # 2026-10-08: switched from the NAS to a Drive folder; nothing had
        # changed since the last send, so the new folder stayed empty
        mid = make_meet(self.a_store)
        self.a.sync_once()
        new = os.path.join(self.tmp, "My Drive", "Meet Backups")
        os.makedirs(new)
        self.a.transport = ms.FolderTransport(new)
        self.assertEqual(self.a.sync_once()["published"], [mid])
        # split by section (2026-10-10): the machine's own folder, manifest last
        self.assertEqual(os.listdir(os.path.join(new, mid)), ["TIMINGPC"])
        self.assertTrue(os.path.isfile(os.path.join(new, mid, "TIMINGPC", "manifest.json")))
        self.assertEqual(self.a.sync_once()["published"], [])

    def test_a_check_is_not_a_send(self):
        # "synced 2 min ago" read as sending when nothing had changed
        # (2026-10-08, user): last_sent / last_received move only when a
        # meet actually goes out or comes in
        mid = make_meet(self.a_store)
        self.a.sync_once()
        sent = self.a.state["last_sent"]
        self.assertIsNotNone(sent)
        self.assertIsNone(self.a.state["last_received"])
        self.a.state["last_sent"] = "2026-10-08T08:00:00"
        self.a.sync_once()                                         # nothing new: a look only
        self.assertEqual(self.a.state["last_sent"], "2026-10-08T08:00:00")
        self.b.sync_once()
        self.assertIsNotNone(self.b.state["last_received"])
        # state from before last_sent was kept: taken from the copies we sent
        self.a.state.pop("last_sent")
        self.a._save_state()
        again = ms.MeetSync(ms.DirStore(self.a_store), self.transport("A"), "TIMINGPC",
                            os.path.join(self.tmp, "a", "state"))
        self.assertEqual(again.state["last_sent"][:10], sent[:10])

    def test_a_quiet_check_reads_no_meet_files(self):
        # 2026-10-08, user: check "in the most data-friendly way possible" -
        # files whose size and time haven't moved aren't opened again
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.b.sync_once()                         # its own file, as sent, seen once
        reads = []
        unpack, read_snapshot = ms.unpack, ms.read_snapshot
        with mock.patch.object(ms, "unpack", lambda d: reads.append("theirs") or unpack(d)), \
                mock.patch.object(ms, "read_snapshot", lambda r, m: reads.append("ours") or read_snapshot(r, m)):
            self.assertEqual(self.b.sync_once(), {"merged": [], "added": [], "published": [], "conflicts": {}})
            self.assertEqual(reads, [])
            self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
            self.assertEqual(self.a.sync_once()["published"], [mid])
            reads.clear()
            self.assertEqual(self.b.sync_once()["merged"], [mid])
            self.assertIn("theirs", reads)
        self.assertEqual(self.bib(self.b_store, mid, "a1"), "901")

    def test_local_files_stay_local(self):
        mid = make_meet(self.a_store)
        with open(os.path.join(self.a_store, mid, "archive.json"), "w") as f:
            json.dump({"folder": "C:\\Finish_Lynx_Results\\2026-10-10 Sync XC"}, f)
        self.a.sync_once()
        self.b.sync_once()
        self.assertFalse(os.path.exists(os.path.join(self.b_store, mid, "archive.json")))


class TestFolder(Machines, unittest.TestCase):
    """The public build: a shared folder (Google Drive for desktop)."""

    def transport(self, who):
        self.shared = os.path.join(self.tmp, "Google Drive", "RFID meets")
        os.makedirs(self.shared, exist_ok=True)
        return ms.FolderTransport(self.shared)

    def test_each_machine_writes_only_its_own_file(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "902"))
        self.b.sync_once()
        self.assertEqual(sorted(os.listdir(os.path.join(self.shared, mid))), ["MEETPRO", "TIMINGPC"])

    def test_three_machines(self):
        c_store = os.path.join(self.tmp, "c", "meet")
        os.makedirs(c_store)
        c = ms.MeetSync(ms.DirStore(c_store), ms.FolderTransport(self.shared), "G15FL",
                        os.path.join(self.tmp, "c", "state"))
        mid = make_meet(self.a_store)
        for m in (self.a, self.b, c):
            m.sync_once()
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a2"), "bib", "902"))
        self.edit(c_store, mid, lambda m: setattr(m.athlete("a3"), "bib", "903"))
        for _ in range(2):
            for m in (self.a, self.b, c):
                m.sync_once()
        for store in (self.a_store, self.b_store, c_store):
            self.assertEqual([self.bib(store, mid, a) for a in ("a1", "a2", "a3")], ["901", "902", "903"])
        for m in (self.a, self.b, c):
            self.assertEqual(m.state["conflicts"], {}, m.machine)

    def test_folder_gone_is_offline_and_nothing_changes(self):
        make_meet(self.a_store)
        shutil.rmtree(self.shared)
        with self.assertRaises(ms.SyncOffline):
            self.a.sync_once()
        self.assertIn("not found", self.a.state["last_error"])
        self.assertEqual(self.a.state["published"], {})


@unittest.skipUnless(ms.find_git(), "git not installed")
class TestGit(Machines, unittest.TestCase):
    """A private git repo on the team's own server (a local bare repo stands in)."""

    def transport(self, who):
        self.remote = os.path.join(self.tmp, "meets.git")
        if not os.path.isdir(self.remote):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        machine = "TIMINGPC" if who == "A" else "MEETPRO"
        return ms.GitTransport(self.remote, os.path.join(self.tmp, who.lower(), "clone"), machine)

    def log(self):
        return subprocess.run(["git", "-C", self.remote, "log", "--format=%an|%s", "--name-only", "main"],
                              capture_output=True, text=True).stdout

    def test_commits_name_the_machine_and_touch_only_its_file(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "902"))
        self.b.sync_once()
        log = self.log()
        self.assertIn("MEETPRO|Sync XC: edited on MEETPRO", log)
        self.assertIn("TIMINGPC|Sync XC: edited on TIMINGPC", log)
        first = log.split("\n\n")[0]
        self.assertIn(f"{mid}/MEETPRO.json", log.split("MEETPRO|", 1)[1].split("TIMINGPC|")[0])
        self.assertTrue(first)

    def test_repo_holds_readable_json_and_a_bib_change_is_a_small_diff(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        clone = os.path.join(self.tmp, "a", "clone")
        with open(os.path.join(clone, mid, "TIMINGPC.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meet"]["meet"]["name"], "Sync XC")      # plain JSON
        self.assertFalse(os.path.exists(os.path.join(clone, mid, "TIMINGPC.json.gz")))
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.a.sync_once()
        diff = subprocess.run(["git", "-C", self.remote, "show", "-U0", "--format=", "main"],
                              capture_output=True, text=True).stdout.splitlines()
        changed = [l[0] + l[1:].strip() for l in diff if l[:1] in "+-" and l[:3] not in ("+++", "---")
                   and '"updated_at"' not in l]          # the save's clock may tick a second (2026-10-08)
        self.assertEqual(changed, ['-"bib": "",', '+"bib": "901",'])               # one line changed
        self.b.sync_once()
        self.assertEqual(self.bib(self.b_store, mid, "a1"), "901")

    def test_gzip_copies_from_before_the_switch_are_read_and_replaced(self):
        mid = make_meet(self.a_store)
        snap = ms.read_snapshot(self.a_store, mid)
        clone = os.path.join(self.tmp, "a", "clone")
        os.makedirs(os.path.join(clone, mid))
        subprocess.run(["git", "-C", clone, "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", clone, "remote", "add", "origin", self.remote], check=True)
        with open(os.path.join(clone, mid, "TIMINGPC.json.gz"), "wb") as f:
            f.write(ms.pack(snap))
        self.a.transport.finish("old gzip copy")
        self.b.sync_once()
        self.assertEqual(MeetStore(self.b_store).load(mid).meet.name, "Sync XC")
        self.a.sync_once()
        names = os.listdir(os.path.join(clone, mid))
        self.assertIn("TIMINGPC.json", names)
        self.assertNotIn("TIMINGPC.json.gz", names)

    def test_both_push_without_fetching_first(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.edit(self.b_store, mid, lambda m: setattr(m.athlete("a2"), "bib", "902"))
        self.a.sync_once()
        self.b.sync_once()                       # its push lands after A's: rebased, no clash
        self.a.sync_once()
        self.assertEqual((self.bib(self.a_store, mid, "a1"), self.bib(self.a_store, mid, "a2")), ("901", "902"))

    def test_unreachable_remote_is_offline(self):
        make_meet(self.a_store)
        self.a.transport.remote = os.path.join(self.tmp, "nowhere.git")
        shutil.rmtree(os.path.join(self.tmp, "a", "clone"), ignore_errors=True)
        with self.assertRaises(ms.SyncOffline):
            self.a.sync_once()
        self.assertIn("git fetch", self.a.state["last_error"])


class TestRunGit(unittest.TestCase):
    def test_timeout_kills_helpers_holding_the_pipe(self):
        # git.exe whose ssh.exe child keeps stdout open: subprocess.run's
        # timeout hung forever on Windows here (2026-10-08).
        import sys, time
        child = "import time; time.sleep(60)"
        parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
        t = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            ms.run_git([sys.executable, "-c", parent], 1, dict(os.environ))
        self.assertLess(time.monotonic() - t, 20)


if __name__ == "__main__":
    unittest.main()


class TestRunner(unittest.TestCase):
    """SyncRunner's timing: a push a minute after the last save, at most one
    every 5 min while editing, a look every 5 min (15 in a race), back off
    while the shared place is away."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsync-run-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "meet")
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(self.store)
        os.makedirs(self.shared)
        self.t = [1000.0]
        self.racing = [False]
        sync = ms.MeetSync(ms.DirStore(self.store), ms.FolderTransport(self.shared), "PC1",
                           os.path.join(self.tmp, "state"))
        self.r = ms.SyncRunner(sync, is_racing=lambda: self.racing[0], clock=lambda: self.t[0])

    def at(self, t):
        self.t[0] = t
        return self.r.due()

    def test_timing(self):
        self.assertTrue(self.at(1000))
        self.r.run_round()
        self.assertFalse(self.at(1010))
        mid = make_meet(self.store)
        self.r.notify_saved(mid)
        self.assertFalse(self.at(1060))                  # still inside the quiet minute
        self.assertTrue(self.at(1071))
        self.assertEqual(self.r.run_round()["published"], [mid])
        self.r.notify_saved(mid)
        self.assertFalse(self.at(1300))                  # quiet, but pushed under 5 min ago
        self.assertTrue(self.at(1372))                   # 5 min after the last round
        self.r.run_round()
        self.racing[0] = True
        self.assertFalse(self.at(1372 + 400))            # a race: every 15 min
        self.assertTrue(self.at(1372 + 901))

    def test_offline_backs_off(self):
        shutil.rmtree(self.shared)
        self.r.run_round()
        self.assertIn("error", self.r.last_report)
        self.assertFalse(self.at(1030))
        self.assertTrue(self.at(1061))                   # 1 min, then 2, 5, 15
        self.r.run_round()
        self.assertFalse(self.at(1061 + 100))
        self.assertTrue(self.at(1061 + 121))


class TestPlaces(unittest.TestCase):
    """config.meet_sync -> one engine per place (2026-10-08: NAS, shared folder or both)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsync-places-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, mode, remote="", folder=""):
        from types import SimpleNamespace
        return SimpleNamespace(meet_sync=SimpleNamespace(enabled=mode != "off", mode=mode, remote=remote,
                                                         folder=folder, machine="PC1", git=""))

    def test_git_remote_or_folder(self):
        for p in ("forgejo:me/meets.git", "git@host:me/meets", "ssh://git@h:30143/x", "C:/x/meets.git"):
            self.assertTrue(ms.is_git_remote(p), p)
        for p in ("G:\\My Drive\\Meet Backups", r"\\truenas\meets", "/mnt/tank/meets", "", "D:meets"):
            self.assertFalse(ms.is_git_remote(p), p)

    def test_engines_per_mode(self):
        store = os.path.join(self.tmp, "meet")
        e = ms.engines_from_config(self.cfg("both", "forgejo:me/meets.git", self.tmp), store, self.tmp)
        self.assertEqual([(n, s.transport.name) for n, s in e], [("git", "git"), ("folder", "folder")])
        self.assertEqual([s.state_dir for _, s in e],
                         [os.path.join(self.tmp, "meet-sync"), os.path.join(self.tmp, "meet-sync", "drive")])
        e = ms.engines_from_config(self.cfg("git", r"\\truenas\meets"), store, self.tmp)
        self.assertEqual([(n, s.transport.name) for n, s in e], [("git", "folder")])   # a NAS share
        self.assertEqual(ms.engines_from_config(self.cfg("off"), store, self.tmp), [])

    def test_folder_only_state_moves_once(self):
        st = os.path.join(self.tmp, "meet-sync")
        os.makedirs(os.path.join(st, "bases", "m1"))
        with open(os.path.join(st, "state.json"), "w") as f:
            json.dump({"published": {"m1": "abc"}}, f)
        (_, sync), = ms.engines_from_config(self.cfg("folder", folder=self.tmp), os.path.join(self.tmp, "meet"), self.tmp)
        self.assertEqual(sync.state["published"], {"m1": "abc"})
        self.assertTrue(os.path.isdir(os.path.join(st, "drive", "bases", "m1")))
        self.assertFalse(os.path.exists(os.path.join(st, "state.json")))

    def test_config_reads_both(self):
        from rfid_finishlynx.config import load_config
        p = os.path.join(self.tmp, "config.yaml")
        with open(p, "w") as f:
            f.write("meet_sync:\n  mode: both\n  remote: forgejo:me/meets.git\n  folder: G:/x\n")
        c = load_config(p).meet_sync
        self.assertEqual((c.enabled, c.mode), (True, "both"))


class TestInstall(unittest.TestCase):
    """INSTALL.bat's meet-sharing step from the package's EDITION.json."""

    def setUp(self):
        import yaml
        self.yaml = yaml
        self.base = tempfile.mkdtemp(prefix="meetsync-inst-")
        self.addCleanup(shutil.rmtree, self.base, True)
        shutil.copyfile(os.path.join(os.path.dirname(__file__), "..", "config.example.yaml"),
                        os.path.join(self.base, "config.example.yaml"))

    def edition(self, **meet_sync):
        with open(os.path.join(self.base, "EDITION.json"), "w") as f:
            json.dump({"edition": "x", "meet_sync": meet_sync}, f)

    def cfg(self):
        with open(os.path.join(self.base, "config.yaml")) as f:
            return self.yaml.safe_load(f).get("meet_sync")

    def test_public_google_drive_folder(self):
        from unittest import mock
        drive = os.path.join(self.base, "G", "My Drive")
        os.makedirs(drive)
        self.edition(mode="folder", folder="google-drive")
        with mock.patch.object(ms, "find_google_drive", return_value=drive):
            ms.install_setup(self.base, ask=lambda q: "")
        c = self.cfg()
        self.assertEqual((c["enabled"], c["mode"], c["folder"]), (True, "folder", os.path.join(drive, "RFID meets")))
        self.assertTrue(os.path.isdir(c["folder"]))
        from rfid_finishlynx.config import load_config
        self.assertEqual(load_config(os.path.join(self.base, "config.yaml")).meet_sync.mode, "folder")

    def test_public_without_google_drive_changes_nothing(self):
        from unittest import mock
        self.edition(mode="folder", folder="google-drive")
        with mock.patch.object(ms, "find_google_drive", return_value=None):
            ms.install_setup(self.base, ask=lambda q: self.fail("asked"))
        self.assertFalse((self.cfg() or {}).get("enabled"))

    def test_declined(self):
        from unittest import mock
        drive = os.path.join(self.base, "My Drive")
        os.makedirs(drive)
        self.edition(mode="folder", folder="google-drive")
        with mock.patch.object(ms, "find_google_drive", return_value=drive):
            ms.install_setup(self.base, ask=lambda q: "n")
        self.assertFalse((self.cfg() or {}).get("enabled"))

    @unittest.skipUnless(ms.find_git(), "git not installed")
    def test_fleet_edition_uses_the_nas_repo_and_keeps_a_set_pc(self):
        remote = os.path.join(self.base, "meets.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        self.edition(mode="git", remote=remote)
        ms.install_setup(self.base)
        c = self.cfg()
        self.assertEqual((c["enabled"], c["mode"], c["remote"]), (True, "git", remote))
        self.edition(mode="folder", folder="google-drive")        # a later package: left alone
        ms.install_setup(self.base, ask=lambda q: self.fail("asked"))
        self.assertEqual(self.cfg()["mode"], "git")
