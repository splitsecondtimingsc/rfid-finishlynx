"""2026-10-10: the Send-logs key lives in the private repo (private/support.key)
so every hand-out zip gets it, and must never reach a package's file tree or
the public GitHub release. Skipped outside a git checkout (an install).
python -m unittest tests.test_private_key
"""
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = shutil.which("git")


@unittest.skipUnless(GIT and os.path.isdir(os.path.join(ROOT, ".git")), "not a git checkout")
class TestPrivateKey(unittest.TestCase):

    def attr(self, path):
        out = subprocess.run([GIT, "-C", ROOT, "check-attr", "export-ignore", "--", path],
                             capture_output=True, text=True, check=True).stdout
        return out.strip().rsplit(": ", 1)[-1]

    def test_private_folder_is_never_packaged(self):
        for p in ("private/support.key", "private/meets-access.json", "private/README.md",
                  "private/anything-new.txt"):
            self.assertEqual(self.attr(p), "set", p)

    def test_the_key_is_tracked_but_other_keys_are_not(self):
        ignored = subprocess.run([GIT, "-C", ROOT, "check-ignore", "-q", "support.key"]).returncode
        self.assertEqual(ignored, 0)                       # an install's support.key stays out
        ignored = subprocess.run([GIT, "-C", ROOT, "check-ignore", "-q", "private/support.key"]).returncode
        self.assertEqual(ignored, 1)                       # the private repo's copy is kept

    def test_the_publisher_refuses_a_github_key(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import publish_release
        finally:
            sys.path.pop(0)
        fake = "github_pat_" + "A1b2C3d4" * 6
        self.assertTrue(publish_release.KEY_RE.search(fake.encode()))


if __name__ == "__main__":
    unittest.main()
