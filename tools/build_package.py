"""Build the hand-out zip another timer installs from (2026-09-30).

    python tools\\build_package.py [--ref HEAD] [--out dist]

Makes dist\\rfid-finishlynx-<date>-<commit>.zip holding one folder,
rfid_finishlynx\\, with INSTALL.bat at its top: the committed tree at --ref
(git archive - so uncommitted edits are NOT in it; development-only paths
are marked export-ignore in .gitattributes), plus
    VERSION.txt       commit, date
    QUICK-START.pdf   QUICK-START.html printed by Edge (if Edge is there)
    support.key       the Send-logs key, copied from support.key next to this
                      repo (git-ignored; never published - see support.py)
Python 3.12 and every package wheel are tracked in git (python\\, wheels\\),
so the zip installs with no internet. The recipient unzips it and
double-clicks INSTALL.bat - see QUICK-START.html.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "rfid_finishlynx/"
EDGE = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


def find_git() -> str:
    found = shutil.which("git")
    if found:
        return found
    mingit = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "MinGit", "cmd", "git.exe")
    if os.path.isfile(mingit):
        return mingit
    sys.exit("git not found (PATH or %LOCALAPPDATA%\\Programs\\MinGit)")


def git(*args) -> str:
    return subprocess.run([find_git(), "-C", ROOT, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def print_pdf(html_path: str, pdf_path: str) -> bool:
    edge = next((e for e in EDGE if os.path.isfile(e)), None)
    if not edge:
        return False
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={prof}", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf_path}", "file:///" + html_path.replace("\\", "/")],
                       capture_output=True, timeout=120)
    return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the hand-out zip")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args(argv)

    commit = git("rev-parse", "--short", a.ref)
    stamp = git("show", "-s", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", a.ref)
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        print("NOTE: uncommitted changes are NOT in the package (it is built from "
              f"{a.ref} = {commit}):\n  " + dirty.replace("\n", "\n  "))
    os.makedirs(a.out, exist_ok=True)
    name = f"rfid-finishlynx-{datetime.now():%Y%m%d}-{commit}.zip"
    dest = os.path.join(a.out, name)

    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.zip")
        git("archive", "--format=zip", f"--prefix={PREFIX}", "-o", raw, a.ref)
        extras = {PREFIX + "VERSION.txt": f"RFID FinishLynx middleware\ncommit {commit} ({stamp})\n"
                                          f"built {datetime.now():%Y-%m-%d %H:%M}\n".encode()}
        # The Send-logs key (support.py): in the zip, never in git
        key_file = os.path.join(ROOT, "support.key")
        if os.path.isfile(key_file):
            with open(key_file, "rb") as f:
                extras[PREFIX + "support.key"] = f.read().strip() + b"\n"
        else:
            print("NOTE: no support.key next to the repo - Send logs needs a key pasted on that PC")
        with zipfile.ZipFile(raw) as z:
            names = z.namelist()
            if PREFIX + "QUICK-START.html" in names:
                # print from a copy of the committed guide and its pictures
                z.extractall(tmp, [n for n in names if n == PREFIX + "QUICK-START.html"
                                   or n.startswith(PREFIX + "docs/quickstart/")])
                guide = os.path.join(tmp, "rfid_finishlynx", "QUICK-START.html")
                pdf = os.path.join(tmp, "QUICK-START.pdf")
                if print_pdf(guide, pdf):
                    with open(pdf, "rb") as f:
                        extras[PREFIX + "QUICK-START.pdf"] = f.read()
                else:
                    print("NOTE: Edge not found - QUICK-START.pdf not made (the .html is in the zip)")
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
                for info in z.infolist():
                    out.writestr(info, z.read(info))
                for n, data in extras.items():
                    out.writestr(n, data)

    size = os.path.getsize(dest) / 1e6
    print(f"{dest}  ({size:.1f} MB, commit {commit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
