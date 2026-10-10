"""Self-update from the public GitHub release (2026-10-01).

An install made by INSTALL.bat from a hand-out zip has VERSION.txt at its
root; that is what makes it updatable. The Console chip's Update button:

  check()   compares VERSION.txt with the release branch's on GitHub
  apply()   the release zip -> pip (only if requirements changed) and an
            import check in a scratch copy -> back up every file it will
            replace -> copy the new files in -> update-pending.json.
            The console then restarts (start-console.bat relaunches it).
  confirm() the new version ran long enough: drop update-pending.json.
            If it never does, deploy/boot_guard.py rolls back after three
            failed launches. rollback() is the operator's own way back.

A development copy (staging, or the live copy promote.py keeps on the
timing PC) has no VERSION.txt, a .git folder or PREVIEW_ONLY, and never
updates itself. Files that belong to the PC (config.yaml, the race state)
are not in the release; the launcher and the boot guard are never replaced
while they exist (cmd.exe is running the one, the other is the way back).
"""

import importlib.util
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEVER_REPLACE = ("config.yaml", "races-state.json", "support.key", "EDITION.json",
                 "deploy/start-console.bat", "deploy/boot_guard.py")
KEEP_BACKUPS = 3
USER_AGENT = "rfid-finishlynx-updater"


class UpdateError(Exception):
    pass


def parse_version(text: str) -> Optional[Dict[str, str]]:
    """'commit a40f594 (2026-10-01 18:08)' -> {commit, stamp}."""
    m = re.search(r"commit\s+([0-9a-f]{4,40})\s*(?:\(([^)]*)\))?", text or "")
    return {"commit": m.group(1), "stamp": m.group(2) or ""} if m else None


def local_version(root: str = ROOT) -> Optional[Dict[str, str]]:
    try:
        with open(os.path.join(root, "VERSION.txt"), encoding="utf-8", errors="replace") as f:
            return parse_version(f.read())
    except OSError:
        return None


def managed(root: str = ROOT):
    """(True, "") when this install updates itself, else (False, why)."""
    if os.path.isdir(os.path.join(root, ".git")) or os.path.exists(os.path.join(root, "PREVIEW_ONLY")):
        return False, "a development copy (updated with git / promote.py)"
    if local_version(root) is None:
        return False, "no VERSION.txt - this copy is updated with promote.py, not from GitHub"
    return True, ""


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_version(repo: str) -> Optional[Dict[str, str]]:
    return parse_version(_get(f"https://raw.githubusercontent.com/{repo}/release/VERSION.txt", 15)
                         .decode("utf-8", "replace"))


def _guard(root: str):
    """deploy/boot_guard.py of this install (one rollback implementation)."""
    path = os.path.join(root, "deploy", "boot_guard.py")
    if not os.path.isfile(path):
        path = os.path.join(ROOT, "deploy", "boot_guard.py")
    spec = importlib.util.spec_from_file_location("rfid_boot_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(repo: str, root: str = ROOT) -> dict:
    ok, why = managed(root)
    out = {"managed": ok, "why": why, "local": local_version(root), "remote": None,
           "available": False, "error": "", "can_roll_back": False}
    try:
        out["can_roll_back"] = bool(_guard(root).latest_backup(root))
    except Exception:
        pass
    if not ok:
        return out
    try:
        out["remote"] = remote_version(repo)
    except Exception as e:
        out["error"] = f"could not reach GitHub: {e}"
        return out
    out["available"] = bool(out["remote"] and out["local"]
                            and out["remote"]["commit"] != out["local"]["commit"])
    return out


def download(repo: str) -> bytes:
    return _get(f"https://github.com/{repo}/archive/refs/heads/release.zip", 180)


def release_files(zip_bytes: bytes) -> Dict[str, bytes]:
    """{relative path: content} from GitHub's archive (one top folder)."""
    files: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/", 1)
            if len(parts) == 2 and parts[1]:
                files[parts[1]] = z.read(info)
    if "rfid_finishlynx/__init__.py" not in files or "VERSION.txt" not in files:
        raise UpdateError("the download is not a console release")
    return files


def _run(cmd, cwd=None, env=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _prepare(files: Dict[str, bytes], root: str, python: str, run_pip: bool):
    """New packages if requirements changed, then make sure the new code
    imports - in a scratch copy, before anything in the install changes."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, data in files.items():
            dest = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        try:
            with open(os.path.join(root, "requirements.txt"), "rb") as f:
                old_req = f.read()
        except OSError:
            old_req = b""
        if run_pip and files.get("requirements.txt", b"") != old_req:
            r = _run([python, "-m", "pip", "install", "--no-warn-script-location",
                      "--find-links", os.path.join(tmp, "wheels"), "-r", os.path.join(tmp, "requirements.txt")])
            if r.returncode:
                raise UpdateError("installing the new packages failed:\n" + (r.stderr or r.stdout)[-800:])
        env = dict(os.environ, PYTHONPATH=tmp)
        r = _run([python, "-c", "import rfid_finishlynx.main, rfid_finishlynx.web, rfid_finishlynx.app"],
                 cwd=tmp, env=env, timeout=180)
        if r.returncode:
            raise UpdateError("the new version does not start (nothing was changed):\n" + r.stderr[-800:])


def apply(zip_bytes: bytes, root: str = ROOT, python: str = sys.executable, run_pip: bool = True) -> dict:
    """Install a downloaded release over `root`. Raises UpdateError before
    touching anything when the release is unusable."""
    ok, why = managed(root)
    if not ok:
        raise UpdateError(f"this copy does not update itself: {why}")
    files = release_files(zip_bytes)
    new = parse_version(files["VERSION.txt"].decode("utf-8", "replace")) or {}
    old = local_version(root) or {}
    _prepare(files, root, python, run_pip)

    backup = os.path.join(root, "update-backups", datetime.now().strftime("%Y%m%d-%H%M%S"))
    replaced, added, writes = [], [], []
    for rel, data in sorted(files.items()):
        dest = os.path.join(root, rel)
        if os.path.exists(dest):
            if rel in NEVER_REPLACE:
                continue
            with open(dest, "rb") as f:
                if f.read() == data:
                    continue
            replaced.append(rel)
        else:
            added.append(rel)
        writes.append((rel, dest, data))
    for rel in replaced:
        src = os.path.join(root, rel)
        b = os.path.join(backup, "files", rel)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        shutil.copy2(src, b)
    # the manifest is written before the first file changes, so even an
    # update cut off half-way can be rolled back
    os.makedirs(backup, exist_ok=True)
    with open(os.path.join(backup, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"from": old.get("commit"), "to": new.get("commit"), "at": datetime.now().isoformat(),
                   "replaced": replaced, "added": added}, f, indent=1)
    # ...and so is the pending marker, as "applying": an update cut off
    # while files change is rolled back by the boot guard at the next
    # launch instead of running mixed versions (2026-10-04 review).
    pending = os.path.join(root, "update-pending.json")
    marker = {"backup": backup, "to": new.get("commit"), "from": old.get("commit"),
              "launches": 0, "state": "applying"}
    _write_json(pending, marker)
    try:
        for rel, dest, data in writes:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".update-tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
    except Exception as e:
        logger.exception("Update failed while installing files - rolling back")
        try:
            _guard(root).rollback(backup, root)
        except Exception:
            logger.exception("Rollback after the failed update failed; the boot guard tries again")
        raise UpdateError(f"the update failed while installing ({e}); the old version was put back")
    marker["state"] = "applied"
    _write_json(pending, marker)
    _prune(root)
    logger.info(f"Update {old.get('commit')} -> {new.get('commit')}: {len(replaced)} file(s) replaced, "
                f"{len(added)} added, backup {backup}")
    return {"from": old, "to": new, "replaced": len(replaced), "added": len(added), "backup": backup}


def _write_json(path: str, obj: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _prune(root: str):
    base = os.path.join(root, "update-backups")
    try:
        names = sorted((n for n in os.listdir(base) if not n.endswith("-rolled-back")), reverse=True)
    except OSError:
        return
    for n in names[KEEP_BACKUPS:]:
        shutil.rmtree(os.path.join(base, n), ignore_errors=True)


def rollback(root: str = ROOT) -> dict:
    g = _guard(root)
    b = g.latest_backup(root)
    if not b:
        raise UpdateError("no update to roll back")
    with open(os.path.join(b, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    n = g.rollback(b, root)
    return {"restored": n, "to": m.get("from"), "undone": m.get("to")}


def confirm(root: str = ROOT) -> bool:
    """The console has been up long enough after an update: keep it."""
    pending = os.path.join(root, "update-pending.json")
    if not os.path.exists(pending):
        return False
    try:
        with open(pending, encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError):
        p = {}
    if p.get("state") == "applying":
        # cut off mid-install: running is not proof the files are whole
        logger.error(f"Update to {p.get('to')} never finished installing: not confirming it; "
                     f"the boot guard rolls it back")
        return False
    os.remove(pending)
    logger.info(f"Update to {p.get('to')} confirmed: the console is running on it")
    return True
