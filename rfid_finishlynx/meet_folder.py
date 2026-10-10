"""Close meet into the FinishLynx results folder (2026-10-05).

FinishLynx and the console share one results folder: FinishLynx writes its
files at the top (flat), the console keeps lynx.evt/ppl/sch and the
merged .lif files in RFID/. Close meet makes "<date> <meet name>/" there,
copies the files (never folders) of the results folder and of RFID into it,
and only then deletes the originals, so both are empty for the next meet.
Reopening a closed meet copies them back (restore_files), and the next
close saves into the same meet folder again.

Safety (user: thorough checks before deleting - full disk, interruption):
free space is checked before anything is copied; each copy goes to a .part
file, is flushed to disk, renamed, then read back - size and SHA-256 must
match the original; nothing is deleted until every copy has passed, and an
original that changed after it was copied is kept. The meet file,
"<meet folder> - MEET FILE.json" at the top of the meet folder, marks it,
records the state (a close that failed or was cut off is finished by
clicking Close meet again) and every file's checksum, and is the file to
open the meet with later (user, 2026-10-05: a name you'd recognise when
browsing for it).
"""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from typing import List, Optional, Tuple

MARKER_SUFFIX = " - MEET FILE.json"
PART = ".part"
CHUNK = 1 << 20
MIN_SPARE = 64 << 20          # free space kept beyond the copy itself


class CloseError(Exception):
    pass


def folder_name(date: str, name: str) -> str:
    """'2026-10-08 Jim Crawford Invitational (Crescent High)', made safe as
    a Windows folder name."""
    base = f"{date} {name}" if date else name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", base or "").strip().rstrip(". ")
    return base or "Meet"


def is_marker(name: str) -> bool:
    return name.lower().endswith(MARKER_SUFFIX.lower())


def marker_path(folder: str) -> str:
    """The meet file in folder: found by its ending, so renaming the folder
    (or the file's front part) keeps it a meet folder."""
    try:
        for n in sorted(os.listdir(folder)):
            if is_marker(n) and os.path.isfile(os.path.join(folder, n)):
                return os.path.join(folder, n)
    except OSError:
        pass
    return os.path.join(folder, os.path.basename(os.path.normpath(folder)) + MARKER_SUFFIX)


def read_marker(folder: str) -> Optional[dict]:
    try:
        with open(marker_path(folder), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_marker(folder: str, data: dict) -> None:
    path = marker_path(folder)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_meet_folder(path: str) -> bool:
    return os.path.isfile(marker_path(path))


def pending(root: str) -> Optional[str]:
    """The meet folder of a close that did not finish, or None."""
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n)
        if os.path.isdir(p):
            m = read_marker(p)
            if m and m.get("state") != "done":
                return p
    return None


def create(root: str, name: str, meet: str) -> str:
    """Make the meet folder ("name (2)" when taken) with its marker."""
    folder = os.path.join(root, name)
    n = 2
    while os.path.exists(folder):
        folder = os.path.join(root, f"{name} ({n})")
        n += 1
    os.makedirs(folder)
    write_marker(folder, {"meet": meet, "state": "middleware",
                          "started": datetime.now().isoformat(timespec="seconds"), "files": {}})
    return folder


def sources(root: str, rfid: str) -> List[Tuple[str, str]]:
    """[(src, rel)]: the files directly in root, then those directly in
    RFID (rel 'RFID/name'). Folders are left alone (2026-10-05: FinishLynx
    saves flat; hand-made folders there are not ours)."""
    def files(d: str, prefix: str):
        try:
            names = sorted(os.listdir(d))
        except FileNotFoundError:
            return []
        return [(os.path.join(d, n), prefix + n) for n in names
                if os.path.isfile(os.path.join(d, n))]
    return files(root, "") + files(rfid, os.path.basename(rfid) + "/")


def plan(root: str, rfid: str) -> dict:
    """What a close would copy, for the console's dialog."""
    files = sources(root, rfid)
    in_rfid = sum("/" in rel for _, rel in files)
    return {"files": len(files) - in_rfid, "rfid_files": in_rfid,
            "bytes": sum(os.path.getsize(src) for src, _ in files)}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src: str, dst: str) -> dict:
    """Copy src to dst through dst.part, flushed to disk; the copy is read
    back and must match the original's size and SHA-256. Raises CloseError
    (no .part left behind)."""
    st = os.stat(src)
    h = hashlib.sha256()
    part = dst + PART
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            for chunk in iter(lambda: fi.read(CHUNK), b""):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, part)
        os.replace(part, dst)
    except OSError as e:
        try:
            os.remove(part)
        except OSError:
            pass
        raise CloseError(f"could not copy {src}: {e}") from e
    after = os.stat(src)
    if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        raise CloseError(f"{src} changed while it was being copied")
    digest = h.hexdigest()
    if os.path.getsize(dst) != st.st_size or _sha256(dst) != digest:
        raise CloseError(f"the copy of {src} does not match the original")
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}


def copy_then_clear(root: str, rfid: str, folder: str) -> dict:
    """Copy everything into folder, check every copy, then delete the
    originals (RFID itself stays). Raises CloseError: before the
    delete step nothing is deleted; after it, what is still there is listed
    and a second call finishes the job."""
    marker = read_marker(folder) or {}
    marker.setdefault("files", {})
    files = sources(root, rfid)
    need = sum(os.path.getsize(src) for src, _ in files)
    free = shutil.disk_usage(folder).free
    spare = max(MIN_SPARE, need // 20)
    if need + spare > free:
        raise CloseError(f"not enough free disk space: {need / 1e6:.1f} MB to copy, "
                         f"{free / 1e6:.1f} MB free (keeping {spare / 1e6:.0f} MB spare). Nothing was deleted")
    marker["state"] = "copying"
    write_marker(folder, marker)
    os.makedirs(os.path.join(folder, os.path.basename(rfid)), exist_ok=True)
    copied = []
    for src, rel in files:
        entry = copy_verified(src, os.path.join(folder, *rel.split("/")))
        marker["files"][rel] = entry
        copied.append((src, rel, entry))
    marker["state"] = "deleting"
    write_marker(folder, marker)

    kept = []
    for src, rel, entry in copied:
        try:
            st = os.stat(src)
            if (st.st_size, st.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                kept.append(f"{rel} (changed after it was copied)")
                continue
            os.remove(src)
        except FileNotFoundError:
            pass
        except OSError as e:
            kept.append(f"{rel} ({e.strerror or e})")
    if kept:
        marker["kept"] = kept
        write_marker(folder, marker)
        raise CloseError(f"everything was copied and checked, but {len(kept)} original(s) are still in "
                         f"the results folder: {', '.join(kept[:5])}{' ...' if len(kept) > 5 else ''}")
    marker.pop("kept", None)
    marker["state"] = "done"
    marker["finished"] = datetime.now().isoformat(timespec="seconds")
    write_marker(folder, marker)
    return {"files": len(copied), "bytes": need}


def restore_files(folder: str, root: str, rfid: str) -> List[str]:
    """Reopen a closed meet: copy its files back into the results folder
    and RFID (each copy checked), so FinishLynx and the console see the
    meet as it was. The meet folder is not changed. On failure the copies
    made so far are removed again and CloseError is raised."""
    rfid_name = os.path.basename(rfid)
    todo = [(os.path.join(folder, n), os.path.join(root, n)) for n in sorted(os.listdir(folder))
            if not is_marker(n) and os.path.isfile(os.path.join(folder, n))]
    sub = os.path.join(folder, rfid_name)
    if os.path.isdir(sub):
        todo += [(os.path.join(sub, n), os.path.join(rfid, n)) for n in sorted(os.listdir(sub))
                 if os.path.isfile(os.path.join(sub, n))]
    os.makedirs(rfid, exist_ok=True)
    done = []
    try:
        for src, dst in todo:
            copy_verified(src, dst)
            done.append(dst)
    except CloseError:
        for dst in done:
            try:
                os.remove(dst)
            except OSError:
                pass
        raise
    return done
