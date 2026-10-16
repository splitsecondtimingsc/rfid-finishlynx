"""Google Drive by sign-in (rfid_finishlynx/gdrive.py, 2026-10-16): a fresh PC
with no Google Drive for desktop shares meets through the Drive API. A fake
Drive stands in for Google (ids, parents, md5s, the changes feed).
python -m unittest tests.test_gdrive
"""
import hashlib
import itertools
import json
import os
import re
import shutil
import tempfile
import unittest

from rfid_finishlynx import gdrive
from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet.model import MeetStore

from tests.test_meet_sync import Machines, make_meet

FOLDER = gdrive.FOLDER_MIME


class FakeDrive:
    """Just enough of Drive v3 for DriveApi's callers."""

    def __init__(self):
        self.files = {}
        self.log = []                        # the changes feed: file ids in order
        self.ids = itertools.count(1)
        self.downloads = 0
        self.uploads = 0
        self.root = self.mkdir(gdrive.FOLDER_NAME, "root")["id"]

    def _meta(self, f):
        return {k: f[k] for k in ("id", "name", "mimeType", "parents", "md5Checksum", "trashed") if k in f}

    def _touch(self, fid):
        self.log.append(fid)

    # DriveApi's methods
    def about(self):
        return {"emailAddress": "timer@example.com"}

    def get(self, fid):
        if fid not in self.files or self.files[fid]["trashed"]:
            e = ms.SyncOffline("Google Drive: HTTP 404")
            e.code = 404
            raise e
        return self._meta(self.files[fid])

    def list(self, q):
        name = re.search(r"name='([^']*)'", q).group(1)
        return [self._meta(f) for f in self.files.values()
                if f["name"] == name and "root" in f["parents"] and not f["trashed"]]

    def children(self, parents):
        return [self._meta(f) for f in self.files.values()
                if not f["trashed"] and any(p in parents for p in f["parents"])]

    def meets_folder(self):
        return self.root, gdrive.FOLDER_NAME

    def mkdir(self, name, parent):
        fid = f"f{next(self.ids)}"
        self.files[fid] = {"id": fid, "name": name, "mimeType": FOLDER, "parents": [parent], "trashed": False}
        self._touch(fid)
        return self._meta(self.files[fid])

    def start_token(self):
        return str(len(self.log))

    def changes(self, token):
        out, seen = [], set()
        for fid in reversed(self.log[int(token):]):
            if fid in seen:
                continue
            seen.add(fid)
            f = self.files.get(fid)
            out.append({"fileId": fid, "removed": True} if f is None else {"fileId": fid, "file": self._meta(f)})
        return out[::-1], str(len(self.log))

    def download(self, fid):
        self.downloads += 1
        return self.files[fid]["data"]

    def create(self, name, parent, data):
        self.uploads += 1
        fid = f"x{next(self.ids)}"
        self.files[fid] = {"id": fid, "name": name, "mimeType": "application/octet-stream", "parents": [parent],
                           "trashed": False, "data": data, "md5Checksum": hashlib.md5(data).hexdigest()}
        self._touch(fid)
        return self._meta(self.files[fid])

    def update(self, fid, data):
        self.uploads += 1
        f = self.files[fid]
        f["data"], f["md5Checksum"] = data, hashlib.md5(data).hexdigest()
        self._touch(fid)
        return self._meta(f)

    def delete(self, fid):
        self.files.pop(fid, None)
        self._touch(fid)

    # Google Drive for desktop on another PC: its local folder <-> Drive
    def _find(self, parent, name):
        return next((f for f in self.files.values() if f["name"] == name and parent in f["parents"]
                     and not f["trashed"]), None)

    def upload_folder(self, local):
        """What Drive for desktop does with a PC's local "RFID meets" folder."""
        for dirpath, _, names in os.walk(local):
            rel = os.path.relpath(dirpath, local)
            parent = self.root
            for part in ([] if rel == "." else rel.split(os.sep)):
                f = self._find(parent, part)
                parent = f["id"] if f else self.mkdir(part, parent)["id"]
            for n in names:
                with open(os.path.join(dirpath, n), "rb") as fh:
                    data = fh.read()
                f = self._find(parent, n)
                if f is None:
                    self.create(n, parent, data)
                elif f["data"] != data:
                    self.update(f["id"], data)

    def download_folder(self, local, fid=None, path=""):
        for f in [f for f in self.files.values() if (fid or self.root) in f["parents"] and not f["trashed"]]:
            p = os.path.join(local, path, f["name"])
            if f["mimeType"] == FOLDER:
                os.makedirs(p, exist_ok=True)
                self.download_folder(local, f["id"], os.path.join(path, f["name"]))
            else:
                with open(p, "wb") as fh:
                    fh.write(f["data"])


class TestDriveApiTransport(Machines, unittest.TestCase):
    """The shared-folder tests, with both PCs signed in to Google."""

    def transport(self, who):
        if not hasattr(self, "drive"):
            self.drive = FakeDrive()
        return gdrive.DriveTransport(self.drive, self.drive.root, os.path.join(self.tmp, who.lower(), "gdrive"))

    def published(self):
        """(every file in the Drive folder, the largest one's size)"""
        files = [f for f in self.drive.files.values() if f["mimeType"] != FOLDER]
        return [f["name"] for f in files], max((len(f["data"]) for f in files), default=0)

    def test_a_quiet_look_downloads_nothing(self):
        mid = make_meet(self.a_store)
        for m in (self.a, self.b, self.a, self.b):        # each has the other's copy
            m.sync_once()
        before = self.drive.downloads
        self.b.sync_once()
        self.a.sync_once()
        self.assertEqual(self.drive.downloads, before)
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        self.a.sync_once()
        before = self.drive.downloads
        self.b.sync_once()
        self.assertEqual(self.bib(self.b_store, mid, "a1"), "901")
        self.assertLessEqual(self.drive.downloads - before, 4)      # the changed sections, not the meet

    def test_a_bib_change_uploads_only_what_changed(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        whole = self.drive.uploads
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a1"), "bib", "901"))
        before = self.drive.uploads
        self.a.sync_once()
        self.assertLess(self.drive.uploads - before, whole / 2)

    def test_index_survives_a_restart(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        b2 = gdrive.DriveTransport(self.drive, self.drive.root, os.path.join(self.tmp, "b", "gdrive"))
        self.assertIsNotNone(b2.token)
        before = self.drive.downloads
        b2.fetch()
        self.assertEqual(self.drive.downloads, before)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "b", "gdrive", "tree", mid, "TIMINGPC")))

    def test_folder_gone_is_offline(self):
        make_meet(self.a_store)
        self.drive.files[self.drive.root]["trashed"] = True
        with self.assertRaises(ms.SyncOffline):
            self.a.sync_once()

    def test_a_lost_page_token_looks_again(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.b.sync_once()
        self.edit(self.a_store, mid, lambda m: setattr(m.athlete("a2"), "bib", "902"))
        self.a.sync_once()

        def gone(token):
            e = ms.SyncOffline("Google Drive: HTTP 410")
            e.code = 410
            raise e
        drive = self.drive

        class Lost:                          # the feed refuses the old token, once
            def __getattr__(self, n):
                return getattr(drive, n)
        self.b.transport.api = Lost()
        self.b.transport.api.changes = gone
        self.b.sync_once()
        self.b.transport.api = self.drive
        self.assertEqual(self.bib(self.b_store, mid, "a2"), "902")

    def test_names_that_would_leave_the_mirror_are_ignored(self):
        self.drive.create("..", self.drive.root, b"x")
        evil = self.drive.mkdir("..", self.drive.root)["id"]
        self.drive.create("escape.json", evil, b"{}")
        self.a.sync_once()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "gdrive", "escape.json")))


class TestWithDriveForDesktop(unittest.TestCase):
    """A team PC using Google Drive for desktop and a fresh PC signed in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gdrive-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.drive = FakeDrive()
        self.local = os.path.join(self.tmp, "G", "My Drive", "RFID meets")
        os.makedirs(self.local)
        self.a_store, self.b_store = (os.path.join(self.tmp, n, "meet") for n in ("a", "b"))
        os.makedirs(self.a_store)
        os.makedirs(self.b_store)
        self.a = ms.MeetSync(ms.DirStore(self.a_store), ms.FolderTransport(self.local), "TRAILER",
                             os.path.join(self.tmp, "a", "state"))
        self.b = ms.MeetSync(ms.DirStore(self.b_store),
                             gdrive.DriveTransport(self.drive, self.drive.root, os.path.join(self.tmp, "b", "gdrive")),
                             "LAPTOP", os.path.join(self.tmp, "b", "state"))

    def test_fresh_pc_pulls_the_meets_and_its_edits_reach_the_drive_app(self):
        mid = make_meet(self.a_store)
        self.a.sync_once()
        self.drive.upload_folder(self.local)               # Drive for desktop uploads
        rep = self.b.sync_once()                            # the fresh PC's first look
        self.assertEqual(rep["added"], [mid])
        self.assertEqual(MeetStore(self.b_store).load(mid).meet.name, "Sync XC")
        m = MeetStore(self.b_store).load(mid)
        m.athlete("a1").bib = "777"
        MeetStore(self.b_store).save(m)
        self.b.sync_once()
        shutil.rmtree(self.local)
        os.makedirs(self.local)
        self.drive.download_folder(self.local)             # Drive for desktop downloads
        self.a.sync_once()
        self.assertEqual(MeetStore(self.a_store).load(mid).athlete("a1").bib, "777")


class TestSignIn(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsign-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_client_file_round_trip_hides_the_secret_from_scanners(self):
        p = os.path.join(self.tmp, "google_client.json")
        with open(p, "w") as f:
            json.dump(gdrive.encode_client("123.apps.googleusercontent.com", "GOCSPX-abcdef"), f)
        with open(p) as f:
            self.assertNotIn("GOCSPX", f.read())
        self.assertEqual(gdrive.client(p), ("123.apps.googleusercontent.com", "GOCSPX-abcdef"))
        self.assertIsNone(gdrive.client(os.path.join(self.tmp, "none.json")))

    def test_begin_makes_a_pkce_link_and_finish_refuses_a_stranger_state(self):
        orig = gdrive.client
        gdrive.client = lambda path=None: ("cid", "sec")
        try:
            url = gdrive.begin("http://127.0.0.1:5000/api/meet/sync/google/callback")
            self.assertIn("code_challenge_method=S256", url)
            self.assertIn("access_type=offline", url)
            self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A5000", url)
            with self.assertRaises(ValueError):
                gdrive.finish("code", "not-a-state", gdrive.TokenStore(os.path.join(self.tmp, "t.json")))
        finally:
            gdrive.client = orig

    def test_engine_uses_the_drive_api_for_a_gdrive_place(self):
        from rfid_finishlynx.config import Config, MeetSyncConfig
        cfg = Config()
        cfg.meet_sync = MeetSyncConfig(enabled=True, mode="folder", folder="gdrive:abc123")
        engines = ms.engines_from_config(cfg, os.path.join(self.tmp, "meet"), self.tmp)
        self.assertEqual(len(engines), 1)
        t = engines[0][1].transport
        self.assertIsInstance(t, gdrive.DriveTransport)
        self.assertEqual(t.root_id, "abc123")
        with self.assertRaises(ms.SyncOffline) as e:           # nobody signed in on this PC
            t.fetch()
        self.assertIn("not signed in", str(e.exception))


if __name__ == "__main__":
    unittest.main()
