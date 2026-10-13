"""The hand-out package's test tree collects (audit 2026-10-13, S-04).

INSTALL-WINDOWS.md tells a timer to check an install with
`python -m pytest tests -q`. The package (tools/build_package.py: git
archive) and the public release leave out the export-ignored files
(.gitattributes: bench/, tools/promote.py, ...); a test that still loaded
one of them made collection fail. This copies this checkout's files as the
package would carry them into a temp folder and runs pytest --collect-only
there.

Builds from git: skipped in a hand-out install.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = shutil.which("git")
CHECKOUT = bool(GIT) and os.path.exists(os.path.join(ROOT, ".git"))
# not needed to collect the tests (and large): the offline installers, documents
SKIP_PREFIXES = ("wheels/", "python/", "docs/")


def git(*args, stdin=None) -> str:
    return subprocess.run([GIT, "-C", ROOT, *args], input=stdin, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def package_files():
    files = [f for f in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0") if f]
    # export-ignore as git archive applies it: on the file or any folder above it
    # ("bench/ export-ignore" matches the folder "bench/", not "bench/x.py")
    dirs = sorted({f.rsplit("/", n)[0] + "/" for f in files for n in range(1, f.count("/") + 1)})
    ignored = set()
    out = git("check-attr", "--stdin", "-z", "export-ignore", stdin="\0".join(files + dirs) + "\0").split("\0")
    for path, _attr, value in zip(out[0::3], out[1::3], out[2::3]):
        if value == "set":
            ignored.add(path)

    def exported(f):
        parts = f.split("/")
        return f not in ignored and not any("/".join(parts[:n]) + "/" in ignored for n in range(1, len(parts)))

    return [f for f in files if exported(f)
            and not f.startswith(SKIP_PREFIXES) and os.path.isfile(os.path.join(ROOT, f))]


@unittest.skipUnless(CHECKOUT, "not a git checkout")
class PackageCollects(unittest.TestCase):

    def test_package_tree_has_no_collection_errors(self):
        files = package_files()
        self.assertNotIn("bench/split_lab.py", files)
        self.assertNotIn("tools/promote.py", files)
        tmp = tempfile.mkdtemp(prefix="pubcollect-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for f in files:
            dest = os.path.join(tmp, *f.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(ROOT, f), dest)
        env = dict(os.environ, RFID_PREVIEW="1")
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
                           cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
        text = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, text[-3000:])
        self.assertNotIn("error", text.splitlines()[-1].lower(), text[-3000:])


if __name__ == "__main__":
    unittest.main()
