"""The fleet reaches the NAS meets repo without an SSH key or Tailscale
(2026-10-16, owner: a fresh PC at a meet must pull the meets "with my Forgejo
access baked in"): meets-access.json -> git over HTTPS, its token in a header
only. Only the fleet edition: never the public one.
python -m unittest tests.test_nas_https
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet.model import MeetStore

from tests.test_meet_sync import make_meet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCESS = {"url": "https://nas.example:8443/leavelw/meets.git", "user": "meets-sync", "token": "tok-123"}


class TestAccessFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nashttps-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, *where):
        os.makedirs(os.path.join(self.tmp, *where[:-1]), exist_ok=True)
        with open(os.path.join(self.tmp, *where), "w") as f:
            json.dump(ACCESS, f)

    def test_package_root_or_private(self):
        self.assertIsNone(ms.nas_https(self.tmp))
        self.write("private", "meets-access.json")
        self.assertEqual(ms.nas_https(self.tmp)["user"], "meets-sync")
        shutil.rmtree(os.path.join(self.tmp, "private"))
        self.write("meets-access.json")
        self.assertEqual(ms.nas_https(self.tmp)["token"], "tok-123")

    def test_never_in_the_public_edition(self):
        self.write("meets-access.json")
        with mock.patch.object(ms, "edition", lambda base=None: {"edition": "public", "sync_targets": ["folder"]}):
            self.assertIsNone(ms.nas_https(self.tmp))

    def test_the_token_goes_in_a_header_and_no_prompt_ever_shows(self):
        env = ms.git_env(ACCESS)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertEqual((env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]), ("credential.helper", ""))
        self.assertNotIn("GIT_CONFIG_KEY_0", ms.git_env(None))


@unittest.skipUnless(ms.find_git(), "git not installed")
class TestFallback(unittest.TestCase):
    """SSH (the fleet alias) doesn't answer on a fresh PC: HTTPS does. A
    local bare repo stands in for the HTTPS URL."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nasfall-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare = os.path.join(self.tmp, "meets.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        self.store = os.path.join(self.tmp, "meet")
        os.makedirs(self.store)

    def engine(self, who, remote, https):
        t = ms.GitTransport(remote, os.path.join(self.tmp, who, "clone"), who.upper(),
                            https=dict(ACCESS, url=https) if https else None)
        store = os.path.join(self.tmp, who, "meet")
        os.makedirs(store, exist_ok=True)
        return ms.MeetSync(ms.DirStore(store), t, who.upper(), os.path.join(self.tmp, who, "state")), store

    def test_fresh_pc_pulls_over_https_when_ssh_fails(self):
        home, home_store = self.engine("home", self.bare, None)        # a fleet PC: SSH works
        mid = make_meet(home_store)
        home.sync_once()
        nowhere = os.path.join(self.tmp, "no-such-alias.git")
        fresh, fresh_store = self.engine("fresh", nowhere, self.bare)
        envs = []
        real = ms.run_git
        with mock.patch.object(ms, "run_git", lambda cmd, t, env: envs.append((cmd, env)) or real(cmd, t, env)):
            rep = fresh.sync_once()
        self.assertEqual(rep["added"], [mid])
        self.assertEqual(MeetStore(fresh_store).load(mid).meet.name, "Sync XC")
        # the header only on the HTTPS tries; the token never on a command line or in .git/config
        fetches = [(c, e) for c, e in envs if "fetch" in c]
        self.assertNotIn("GIT_CONFIG_KEY_0", fetches[0][1])
        self.assertIn("GIT_CONFIG_KEY_0", fetches[1][1])
        self.assertFalse(any("tok-123" in " ".join(c) for c, _ in envs))
        with open(os.path.join(self.tmp, "fresh", "clone", ".git", "config")) as f:
            self.assertNotIn("tok-123", f.read())
        # its edits go back the same way, and the way that worked is tried first next time
        m = MeetStore(fresh_store).load(mid)
        m.athlete("a1").bib = "555"
        MeetStore(fresh_store).save(m)
        envs.clear()
        with mock.patch.object(ms, "run_git", lambda cmd, t, env: envs.append((cmd, env)) or real(cmd, t, env)):
            fresh.sync_once()
        self.assertEqual(len([c for c, _ in envs if "fetch" in c]), 1)
        home.sync_once()
        self.assertEqual(MeetStore(home_store).load(mid).athlete("a1").bib, "555")

    def test_both_ways_down_is_offline_with_both_reasons(self):
        fresh, _ = self.engine("fresh", os.path.join(self.tmp, "a.git"), os.path.join(self.tmp, "b.git"))
        with self.assertRaises(ms.SyncOffline) as e:
            fresh.sync_once()
        self.assertIn("over HTTPS", str(e.exception))


class TestPackage(unittest.TestCase):
    """build_package: the access file and MinGit go only into an edition
    that asks for them (the fleet)."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        self.addCleanup(sys.path.pop, 0)
        import build_package
        self.bp = build_package

    def test_public_edition_gets_neither(self):
        with open(os.path.join(ROOT, "tools", "editions.json"), encoding="utf-8") as f:
            eds = json.load(f)
        self.assertFalse(eds["public"].get("nas_https") or eds["public"].get("mingit"))
        self.assertEqual(self.bp.fleet_extras(eds["public"]), {})

    def test_fleet_edition_gets_both(self):
        tmp = tempfile.mkdtemp(prefix="bpkg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "private"))
        with open(os.path.join(tmp, "private", "meets-access.json"), "w") as f:
            json.dump(ACCESS, f)
        import zipfile
        mg = os.path.join(tmp, "MinGit.zip")
        with zipfile.ZipFile(mg, "w") as z:
            z.writestr("cmd/git.exe", b"MZ")
        with mock.patch.object(self.bp, "ROOT", tmp), mock.patch.object(self.bp, "mingit_zip", lambda: mg):
            out = self.bp.fleet_extras({"nas_https": True, "mingit": True})
        self.assertIn("rfid_finishlynx/meets-access.json", out)
        self.assertEqual(out["rfid_finishlynx/mingit/cmd/git.exe"], b"MZ")


if __name__ == "__main__":
    unittest.main()
