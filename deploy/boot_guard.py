"""Safety net for the console's self-update (2026-10-01).

start-console.bat runs this before every launch. After an update the
console leaves update-pending.json at the install root; the new version
removes it once it has run for a while (updater.confirm). If it never gets
that far - three launches in a row die - this restores the files the update
replaced from update-backups\\<stamp>\\ and the old version starts again.

    python deploy\\boot_guard.py              before each launch
    python deploy\\boot_guard.py --rollback   undo the last update now

Standard library only, and the updater never replaces this file, so a
broken update cannot break the way back.
"""

import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_LAUNCHES = 3


def paths(root=ROOT):
    return (os.path.join(root, "update-pending.json"), os.path.join(root, "update-backups"),
            os.path.join(root, "update.log"))


def log(msg, root=ROOT):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        with open(paths(root)[2], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _inside(root, rel):
    dest = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(dest)]) != os.path.abspath(root):
        raise ValueError(f"path outside the install: {rel}")
    return dest


def latest_backup(root=ROOT):
    """Newest update-backups\\<stamp> with a manifest, or None."""
    base = paths(root)[1]
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return None
    for n in names:
        if not n.endswith("-rolled-back") and os.path.isfile(os.path.join(base, n, "manifest.json")):
            return os.path.join(base, n)
    return None


def rollback(backup_dir, root=ROOT):
    """Put back what the update in `backup_dir` replaced and remove what it
    added. Returns the number of files restored."""
    with open(os.path.join(backup_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    restored = 0
    for rel in manifest.get("replaced", []):
        src = os.path.join(backup_dir, "files", rel)
        if os.path.isfile(src):
            dest = _inside(root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            restored += 1
    for rel in manifest.get("added", []):
        dest = _inside(root, rel)
        if os.path.isfile(dest):
            os.remove(dest)
    pending = paths(root)[0]
    if os.path.exists(pending):
        os.remove(pending)
    os.rename(backup_dir, backup_dir + "-rolled-back")
    log(f"Rolled back the update to {manifest.get('to')}: {restored} file(s) restored, "
        f"back on {manifest.get('from')}", root)
    return restored


def guard(root=ROOT):
    """Count this launch against a pending update; roll back after
    MAX_LAUNCHES launches that never confirmed."""
    pending = paths(root)[0]
    if not os.path.exists(pending):
        return
    try:
        with open(pending, encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError):
        os.remove(pending)
        return
    p["launches"] = int(p.get("launches", 0)) + 1
    if p["launches"] > MAX_LAUNCHES and p.get("backup") and os.path.isdir(p["backup"]):
        log(f"Update to {p.get('to')} did not start after {MAX_LAUNCHES} tries - rolling back", root)
        rollback(p["backup"], root)
        return
    with open(pending, "w", encoding="utf-8") as f:
        json.dump(p, f)


if __name__ == "__main__":
    try:
        if "--rollback" in sys.argv:
            b = latest_backup()
            if b:
                rollback(b)
            else:
                print("No update backup to roll back to.")
        else:
            guard()
    except Exception as e:                      # never stop the console from starting
        log(f"boot guard error: {e}")
