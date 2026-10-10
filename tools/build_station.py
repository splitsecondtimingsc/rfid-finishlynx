"""Build the Raspberry Pi station package (2026-10-04).

    python tools/build_station.py [--ref HEAD | --worktree] [--out dist]

Makes dist/rfid-station-<date>-<commit>.tar.gz holding one folder,
rfid_station/: what a split or check-in station (mode: remote_lap) runs and
nothing else - no meet management, race panes, FinishLynx PC tools, Windows
Python/wheels, docs or tests. On the Pi:

    tar xzf rfid-station-*.tar.gz && cd rfid_station && ./install-station.sh

Files come from the commit at --ref (uncommitted edits are NOT in it), or
from the working tree with --worktree. deploy/station/ supplies the
installer, services, station config and requirements.
"""

import argparse
import io
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "rfid_station/"

# Repo files shipped as they are
KEEP = ("run.py", "range_test.py", "setup_gateway.py", "test_lora_link.py")
PACKAGE = "rfid_finishlynx/"
# Finish-line only: the station never imports these (tests/test_station_package.py)
DROP = (
    "rfid_finishlynx/meet/",
    "rfid_finishlynx/meet_api.py",
    "rfid_finishlynx/meet_service.py",
    "rfid_finishlynx/races.py",
    "rfid_finishlynx/lif_merge.py",
    "rfid_finishlynx/film_offset.py",
    "rfid_finishlynx/lynx_cfg.py",
    "rfid_finishlynx/setup_check.py",
    "rfid_finishlynx/fl_window.py",
    "rfid_finishlynx/updater.py",
    "rfid_finishlynx/support.py",
    "rfid_finishlynx/templates/console.html",
)
# deploy/station/<name> goes to the top of the package
STATION = "deploy/station/"


def git(*args) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], check=True,
                          capture_output=True, text=True).stdout


def wanted(path: str) -> bool:
    if path.startswith(STATION):
        return True
    if path in KEEP:
        return True
    return path.startswith(PACKAGE) and not path.startswith(DROP) and "__pycache__" not in path


def target(path: str) -> str:
    return TOP + (path[len(STATION):] if path.startswith(STATION) else path)


def files_at(ref: str):
    """(path, executable, bytes) for each shipped file in the commit."""
    for line in git("ls-tree", "-r", ref).splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob" and wanted(path):
            data = subprocess.run(["git", "-C", ROOT, "cat-file", "blob", sha],
                                  check=True, capture_output=True).stdout
            yield path, mode == "100755", data


def files_on_disk():
    """Same, from the working tree (tracked and new files, not ignored ones)."""
    listed = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    for path in sorted(set(listed)):
        full = os.path.join(ROOT, path)
        if wanted(path) and os.path.isfile(full):
            with open(full, "rb") as f:
                yield path, os.access(full, os.X_OK), f.read()


def build(out_dir: str, ref: str = "HEAD", worktree: bool = False) -> str:
    commit = git("rev-parse", "--short", "HEAD" if worktree else ref).strip()
    stamp = datetime.now().strftime("%Y-%m-%d")
    name = f"rfid-station-{stamp}-{commit}{'-dirty' if worktree else ''}.tar.gz"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name)
    now = time.time()

    def add(tar, arcname, data, executable=False):
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        info.mode = 0o755 if executable else 0o644
        info.mtime = now
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(out, "w:gz") as tar:
        for path, executable, data in (files_on_disk() if worktree else files_at(ref)):
            add(tar, target(path), data, executable)
        version = f"commit {commit}{' + uncommitted edits' if worktree else ''}\nbuilt {stamp}\n"
        add(tar, TOP + "VERSION.txt", version.encode())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the Raspberry Pi station package")
    ap.add_argument("--ref", default="HEAD", help="commit to package (default HEAD)")
    ap.add_argument("--worktree", action="store_true", help="package the working tree instead")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = ap.parse_args(argv)
    out = build(args.out, args.ref, args.worktree)
    print(f"{out}  ({os.path.getsize(out) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
