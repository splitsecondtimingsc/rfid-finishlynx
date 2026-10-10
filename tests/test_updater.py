"""Self-update from GitHub + boot-guard rollback + Send logs (2026-10-01).
python -m unittest tests.test_updater"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import support, updater, web
from rfid_finishlynx.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_GUARD = os.path.join(HERE, os.pardir, "deploy", "boot_guard.py")

OLD = {
    "VERSION.txt": "RFID FinishLynx middleware\ncommit 1111111 (2026-10-01 10:00)\n",
    "requirements.txt": "flask\n",
    "rfid_finishlynx/__init__.py": "",
    "rfid_finishlynx/main.py": "X = 'old'\n",
    "rfid_finishlynx/web.py": "",
    "rfid_finishlynx/app.py": "",
    "deploy/start-console.bat": "old launcher\n",
    "config.yaml": "support:\n  logs_token: secret\n",
    "drivers-from-install.sys": "keep me\n",
}
NEW = {
    "VERSION.txt": "RFID FinishLynx middleware\ncommit 2222222 (2026-10-02 09:00)\n",
    "requirements.txt": "flask\n",
    "rfid_finishlynx/__init__.py": "",
    "rfid_finishlynx/main.py": "X = 'new'\n",
    "rfid_finishlynx/web.py": "",
    "rfid_finishlynx/app.py": "",
    "rfid_finishlynx/brand_new.py": "Y = 1\n",
    "deploy/start-console.bat": "new launcher\n",
}


def release_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, text in files.items():
            z.writestr("rfid-finishlynx-release/" + rel, text)
    return buf.getvalue()


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


class InstallTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel, text in OLD.items():
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:     # installs are LF, like the zip
                f.write(text)
        shutil.copy(REAL_GUARD, os.path.join(self.root, "deploy", "boot_guard.py"))

    def apply(self, files=NEW):
        return updater.apply(release_zip(files), root=self.root, python=sys.executable, run_pip=False)

    def guard(self):
        return updater._guard(self.root)


class TestVersionAndManaged(InstallTestCase):

    def test_parse(self):
        self.assertEqual(updater.parse_version("commit a40f594 (2026-10-01 18:08)"),
                         {"commit": "a40f594", "stamp": "2026-10-01 18:08"})
        self.assertIsNone(updater.parse_version("nothing"))

    def test_packaged_install_is_managed_dev_copies_are_not(self):
        self.assertEqual(updater.managed(self.root), (True, ""))
        os.makedirs(os.path.join(self.root, ".git"))
        self.assertFalse(updater.managed(self.root)[0])
        shutil.rmtree(os.path.join(self.root, ".git"))
        os.remove(os.path.join(self.root, "VERSION.txt"))
        self.assertFalse(updater.managed(self.root)[0])

    def test_check_compares_with_github(self):
        with mock.patch.object(updater, "remote_version", return_value={"commit": "2222222", "stamp": ""}):
            out = updater.check("x/y", self.root)
        self.assertTrue(out["available"])
        with mock.patch.object(updater, "remote_version", side_effect=OSError("offline")):
            out = updater.check("x/y", self.root)
        self.assertFalse(out["available"])
        self.assertIn("offline", out["error"])


class TestApply(InstallTestCase):

    def test_installs_backs_up_and_leaves_the_pc_files_alone(self):
        out = self.apply()
        self.assertEqual((out["from"]["commit"], out["to"]["commit"]), ("1111111", "2222222"))
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'new'\n")
        self.assertEqual(read(self.root, "rfid_finishlynx/brand_new.py"), "Y = 1\n")
        self.assertEqual(updater.local_version(self.root)["commit"], "2222222")
        self.assertEqual(read(self.root, "config.yaml"), OLD["config.yaml"])
        self.assertEqual(read(self.root, "deploy/start-console.bat"), "old launcher\n")   # cmd is running it
        self.assertEqual(read(self.root, "drivers-from-install.sys"), "keep me\n")      # not in the release
        self.assertTrue(os.path.exists(os.path.join(self.root, "update-pending.json")))
        with open(os.path.join(out["backup"], "manifest.json")) as f:
            m = json.load(f)
        self.assertEqual(sorted(m["replaced"]), ["VERSION.txt", "rfid_finishlynx/main.py"])
        self.assertEqual(m["added"], ["rfid_finishlynx/brand_new.py"])

    def test_install_key_survives_updates(self):
        with open(os.path.join(self.root, "support.key"), "w") as f:
            f.write("github_pat_FROMZIP\n")
        self.apply(dict(NEW, **{"support.key": "something else\n"}))
        self.assertEqual(read(self.root, "support.key"), "github_pat_FROMZIP\n")

    def test_broken_release_changes_nothing(self):
        broken = dict(NEW, **{"rfid_finishlynx/main.py": "def oops(:\n"})
        with self.assertRaises(updater.UpdateError):
            self.apply(broken)
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'old'\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "update-backups")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "update-pending.json")))

    def test_not_a_release(self):
        with self.assertRaises(updater.UpdateError):
            self.apply({"README.md": "hi"})

    def test_dev_copy_refuses(self):
        os.makedirs(os.path.join(self.root, ".git"))
        with self.assertRaises(updater.UpdateError):
            self.apply()

    def test_confirm_keeps_the_update(self):
        self.apply()
        self.assertTrue(updater.confirm(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, "update-pending.json")))
        self.assertFalse(updater.confirm(self.root))

    def test_manual_rollback(self):
        self.apply()
        out = updater.rollback(self.root)
        self.assertEqual(out["to"], "1111111")
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'old'\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "rfid_finishlynx/brand_new.py")))
        self.assertEqual(updater.local_version(self.root)["commit"], "1111111")
        with self.assertRaises(updater.UpdateError):
            updater.rollback(self.root)                       # nothing left to undo

    def test_keeps_three_backups(self):
        for i in range(5):
            with mock.patch.object(updater, "datetime") as dt:
                dt.now.return_value.strftime.return_value = f"2026100{i}-000000"
                dt.now.return_value.isoformat.return_value = "x"
                files = dict(NEW, **{"rfid_finishlynx/main.py": f"X = {i}\n"})
                self.apply(files)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "update-backups"))), 3)


class TestBootGuard(InstallTestCase):

    def test_three_failed_launches_roll_back(self):
        self.apply()
        g = self.guard()
        for _ in range(3):
            g.guard(self.root)                                # the new version keeps dying
            self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'new'\n")
        g.guard(self.root)                                    # 4th launch: back to the old one
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'old'\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "update-pending.json")))
        self.assertIn("Rolled back", read(self.root, "update.log"))

    def test_nothing_pending_is_a_no_op(self):
        self.guard().guard(self.root)
        self.assertEqual(read(self.root, "rfid_finishlynx/main.py"), "X = 'old'\n")


class TestSupport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = Config()
        cfg.support.logs_token = "github_pat_SECRET"
        cfg.support.site_name = "Bob's PC #2"
        cfg.logging.file = os.path.join(self.tmp, "rfid_timing.log")
        with open(cfg.logging.file, "w") as f:
            f.write("line\n" * 10)
        cfg.races.state_file = os.path.join(self.tmp, "nope.json")
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        with open(self.cfg_path, "w") as f:
            f.write("support:\n  logs_token: github_pat_SECRET\n  site_name: x\n")
        self.app = SimpleNamespace(config=cfg, config_path=self.cfg_path,
                                   get_status=lambda: {"running": True})

    def test_bundle_has_the_pieces_and_never_the_key(self):
        data = support.bundle(self.app, "reader dropped at 10:02")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            self.assertTrue({"NOTE.txt", "config.yaml", "rfid_timing.log", "status.json"} <= names)
            self.assertIn("reader dropped", z.read("NOTE.txt").decode())
            self.assertIn("races-state.json", z.read("MISSING.txt").decode())
            for n in names:
                self.assertNotIn(b"github_pat_SECRET", z.read(n), n)

    def test_site_name_is_a_safe_folder(self):
        self.assertEqual(support.site_name(self.app), "Bob-s-PC-2")

    def test_send_puts_one_file_in_the_logs_repo(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(req, timeout=0):
            seen["url"], seen["auth"], seen["method"] = req.full_url, req.headers["Authorization"], req.get_method()
            return Resp(json.dumps({"content": {"html_url": "https://github.com/x"}}).encode())
        with mock.patch("urllib.request.urlopen", fake):
            out = support.send(self.app, "hi")
        self.assertTrue(seen["url"].startswith("https://api.github.com/repos/splitsecondtimingsc/rfid-logs/contents/Bob-s-PC-2/"))
        self.assertEqual((seen["auth"], seen["method"]), ("Bearer github_pat_SECRET", "PUT"))
        self.assertTrue(out["path"].endswith(".zip"))

    def test_refused_key_says_so(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(support.SendError, "refused"):
                support.send(self.app)

    def test_no_key_yet(self):
        self.app.config.support.logs_token = ""
        with mock.patch.object(support, "bundled_key", return_value=""):
            with self.assertRaisesRegex(support.SendError, "no support key"):
                support.send(self.app)

    def test_install_key_is_the_default(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "support.key"), "w") as f:
            f.write("github_pat_FROMZIP\n")
        self.assertEqual(support.bundled_key(root), "github_pat_FROMZIP")
        self.app.config.support.logs_token = ""
        with mock.patch.object(support, "bundled_key", return_value="github_pat_FROMZIP"):
            self.assertEqual(support.effective_token(self.app.config), "github_pat_FROMZIP")
            self.app.config.support.logs_token = "github_pat_TYPED"
            self.assertEqual(support.effective_token(self.app.config), "github_pat_TYPED")   # typed one wins


class TestPublishGuard(unittest.TestCase):
    """GitHub revokes its keys found in a public repo: never publish one."""

    def test_key_pattern(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "publish_release", os.path.join(HERE, os.pardir, "tools", "publish_release.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))
        self.addCleanup(sys.path.remove, os.path.join(HERE, os.pardir, "tools"))
        spec.loader.exec_module(mod)
        # built at run time: this file itself must not hold a key-shaped string
        self.assertTrue(mod.KEY_RE.search(b"key: github_pat_" + b"11" + b"A" * 30))
        self.assertTrue(mod.KEY_RE.search(b"ghp_" + b"a" * 36))
        self.assertFalse(mod.KEY_RE.search(b"logs_token: github_pat_SECRET"))   # test fixtures
        self.assertFalse(mod.KEY_RE.search(b"placeholder=\"Support key (github_pat_...)\""))


class TestWebEndpoints(unittest.TestCase):

    def setUp(self):
        cfg = Config()
        cfg.support.logs_token = "github_pat_SECRET"
        self.saved = []
        mgr = SimpleNamespace(races={1: SimpleNamespace(name="Girls 5K", status="racing")})
        web._application = SimpleNamespace(config=cfg, race_manager=mgr, save_config=lambda: self.saved.append(1),
                                           _finishlynx_clock_gun=lambda: None)
        self.addCleanup(setattr, web, "_application", None)
        self.client = web.app.test_client()

    def test_config_never_shows_the_key(self):
        d = self.client.get("/api/config").get_json()
        self.assertTrue(d["support"]["token_set"])
        self.assertNotIn("github_pat_SECRET", json.dumps(d))

    def test_update_waits_while_racing(self):
        with mock.patch.object(updater, "download") as dl:
            d = self.client.post("/api/update/apply", json={}).get_json()
        self.assertEqual(d["busy"], ["racing: Girls 5K"])
        dl.assert_not_called()

    def test_settings_store_the_key(self):
        d = self.client.post("/api/support/settings", json={"token": " new ", "site_name": "Bob"}).get_json()
        self.assertEqual((d["token_set"], d["site_name"]), (True, "Bob"))
        self.assertEqual(web._application.config.support.logs_token, "new")
        self.assertEqual(self.saved, [1])


if __name__ == "__main__":
    unittest.main()
