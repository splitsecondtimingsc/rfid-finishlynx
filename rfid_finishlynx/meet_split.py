"""A meet split into sections for a shared folder (2026-10-10).

Google Drive for desktop uploads a changed file whole - it has no delta
sync - so one gzipped meet file (265 KB for Bob Jenkins, ~2,900 athletes)
went up again for every edit, where the NAS git push carries a few hundred
bytes. Split by section, an edit only re-uploads the part it touched:

    <MACHINE>/manifest.json            what's there, a hash per file (written last)
    <MACHINE>/snapshot.json.gz         the sync header (who sent it, what it had merged)
    <MACHINE>/meet.json.gz             meet info, events, bib ranges...
    <MACHINE>/teams.json.gz
    <MACHINE>/athletes/<team>.json.gz  one file per team (a bib change = one team)
    <MACHINE>/races/<e>-<r>-<h>/entries.json.gz  a race's entries (change rarely)
    <MACHINE>/races/<e>-<r>-<h>/race.json.gz     the rest: status, marks, results
    <MACHINE>/files/pulls/entries.json.gz ...   MileSplit pulls, the upload record

A reader rebuilds the meet only when every file the manifest lists is there
with the hash it lists: Drive syncs files one by one and in no set order, so
a half-synced copy is "not ready yet", never a meet with a race missing.
join(split(snap)) gives back snap exactly (same lists in the same order), so
the sync's digests and merges don't see a difference.

Also a tool: rebuild a single meet .json from a split folder (or from a
single .json / .json.gz), and split a meet .json into a folder:

    python -m rfid_finishlynx.meet_split rebuild <folder|manifest.json|file> -o meet.json
    python -m rfid_finishlynx.meet_split rebuild <folder|manifest.json|file> -o <meet folder>
    python -m rfid_finishlynx.meet_split split <meet.json> <folder>
"""
import gzip
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional

MANIFEST = "manifest.json"
FORMAT = 1
SECTIONS = ("teams", "athletes", "races")       # everything else stays in meet.json.gz


class Incomplete(Exception):
    """A split copy that isn't all there yet (mid-sync), or isn't one."""


def _gz(obj) -> bytes:
    """Deterministic: the same section gives the same bytes (an unchanged
    file is not rewritten, so the sync app has nothing to upload)."""
    raw = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def _hash(data: bytes) -> str:
    """Enough to tell a file from its older copy (not a security check);
    short, because the manifest goes up with every edit."""
    return hashlib.sha256(data).hexdigest()[:12]


def _safe(name: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._") or "_"
    out, n = base, 2
    while out.lower() in taken:                  # Windows / Drive: names are case-blind
        out, n = f"{base}~{n}", n + 1
    taken.add(out.lower())
    return out


def _natural(aid) -> tuple:
    m = re.fullmatch(r"(\D*)(\d+)", str(aid))
    return (m.group(1), int(m.group(2)), "") if m else (str(aid), -1, str(aid))


def is_snapshot(doc) -> bool:
    """The sync's snapshot ({"meet": <meet.json>, "files": {...}, "sync": ...})
    rather than a bare meet.json (whose "meet" is the meet's info)."""
    return isinstance(doc, dict) and isinstance(doc.get("meet"), dict) and (
        "files" in doc or any(k in doc["meet"] for k in SECTIONS))


def split(doc: dict) -> Dict[str, bytes]:
    """relative path -> bytes; MANIFEST included (plain JSON, readable).
    A sync snapshot: its meet split by section, each of its other files
    (MileSplit pulls, the upload record) its own file under files/, the
    rest (the sync header) in snapshot.json.gz. A bare meet.json: the meet
    split by section."""
    files: Dict[str, bytes] = {}
    manifest: dict = {"format": FORMAT}
    if is_snapshot(doc):
        manifest["kind"] = "snapshot"
        files["snapshot.json.gz"] = _gz({k: v for k, v in doc.items() if k not in ("meet", "files")})
        _split_meet(doc["meet"], files, manifest)
        if "files" in doc:
            taken: set = set()
            shared = {}
            for rel in sorted(doc["files"]):
                parts = [_safe(p, set()) for p in str(rel).split("/")]
                path = "files/" + "/".join(parts) + ".gz"
                while path.lower() in taken:
                    path = path[:-3] + "~.gz"
                taken.add(path.lower())
                files[path] = _gz(doc["files"][rel])
                shared[path] = rel                # its own name, as the store keeps it
            manifest["shared"] = shared
    else:
        manifest["kind"] = "meet"
        _split_meet(doc, files, manifest)
    manifest["files"] = {rel: _hash(data) for rel, data in sorted(files.items())}
    # one entry a line, no padding: readable, and small (it goes up every time)
    files[MANIFEST] = json.dumps(manifest, indent=0, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return files


def _split_meet(meet: dict, files: Dict[str, bytes], manifest: dict) -> None:
    files["meet.json.gz"] = _gz({k: v for k, v in meet.items() if k not in SECTIONS})
    manifest["sections"] = [k for k in SECTIONS if k in meet]
    if "teams" in meet:
        files["teams.json.gz"] = _gz(meet["teams"])

    athletes = meet.get("athletes")
    if athletes is not None:
        ids = [a.get("id") if isinstance(a, dict) else None for a in athletes]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            files["athletes/_all.json.gz"] = _gz(athletes)        # can't key them: one file, as is
            manifest["athlete_order"] = "as stored"
        else:
            groups: Dict[str, List[dict]] = {}
            for a in athletes:
                groups.setdefault(str(a.get("team_id") or ""), []).append(a)
            taken: set = set()
            for team in sorted(groups):
                files[f"athletes/{_safe(team or '_no_team', taken)}.json.gz"] = _gz(groups[team])
            # rebuilt in id order (a1, a2 ... as the meet keeps them); any
            # other order is written down so it comes back the same
            if ids != sorted(ids, key=_natural):
                manifest["athlete_order"] = ids

    races = meet.get("races")
    if races is not None:
        taken = set()
        names = []
        for r in races:
            key = "-".join(str(r.get(k, "")) for k in ("event", "round", "heat")) if isinstance(r, dict) else ""
            folder = f"races/{_safe(key or 'race', taken)}"
            if isinstance(r, dict) and "entries" in r:
                # a DNF mark or a result doesn't send the entry list again
                files[f"{folder}/entries.json.gz"] = _gz(r["entries"])
                files[f"{folder}/race.json.gz"] = _gz({k: v for k, v in r.items() if k != "entries"})
            else:
                files[f"{folder}/race.json.gz"] = _gz(r)
            names.append(folder)
        manifest["races"] = names                 # in the meet's order


def join(files: Dict[str, bytes]) -> dict:
    """The snapshot (or meet) back from split()'s files. Incomplete when the
    manifest is missing, or a file it lists is missing or not the one it
    lists."""
    raw = files.get(MANIFEST)
    if raw is None:
        raise Incomplete("no manifest.json")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise Incomplete(f"manifest.json unreadable: {e}") from None
    if manifest.get("format") != FORMAT:
        raise Incomplete(f"manifest format {manifest.get('format')!r} (this console reads {FORMAT})")
    want = manifest.get("files") or {}
    for rel, h in want.items():
        data = files.get(rel)
        if data is None:
            raise Incomplete(f"{rel} not there yet")
        if _hash(data) != h:
            raise Incomplete(f"{rel} is not the one the manifest lists (still syncing?)")

    def load(rel):
        return json.loads(gzip.decompress(files[rel]).decode("utf-8"))

    meet = _join_meet(load, want, manifest)
    if manifest.get("kind") != "snapshot":
        return meet
    doc = load("snapshot.json.gz")
    doc["meet"] = meet
    shared = manifest.get("shared")
    if shared is not None:
        doc["files"] = {rel: load(path) for path, rel in shared.items()}
    return doc


def _join_meet(load, want: dict, manifest: dict) -> dict:
    meet = load("meet.json.gz")
    sections = manifest.get("sections") or []
    if "teams" in sections:
        meet["teams"] = load("teams.json.gz")
    if "athletes" in sections:
        athletes: List[dict] = []
        for rel in sorted(r for r in want if r.startswith("athletes/")):     # every team's file
            athletes += load(rel)
        order = manifest.get("athlete_order")
        if order == "as stored":
            pass
        elif isinstance(order, list):
            pos = {aid: i for i, aid in enumerate(order)}
            athletes.sort(key=lambda a: pos.get(a.get("id"), len(pos)))
        else:
            athletes.sort(key=lambda a: _natural(a.get("id")))
        meet["athletes"] = athletes
    if "races" in sections:
        races = []
        for folder in manifest.get("races") or []:
            r = load(f"{folder}/race.json.gz")
            if f"{folder}/entries.json.gz" in want:
                r["entries"] = load(f"{folder}/entries.json.gz")
            races.append(r)
        meet["races"] = races
    return meet


def read_folder(folder: str) -> Dict[str, bytes]:
    """Every file under a split copy's folder, by relative path ("/")."""
    out: Dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for n in filenames:
            if n.startswith("."):
                continue
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, folder).replace(os.sep, "/")
            with open(p, "rb") as f:
                out[rel] = f.read()
    return out


def is_split_folder(path: str) -> bool:
    return os.path.isdir(path) and os.path.isfile(os.path.join(path, MANIFEST))


def read_any(path: str) -> dict:
    """A meet from a split folder (or its manifest.json), a .json.gz or a
    single .json. Incomplete for a half-synced split copy; ValueError for
    anything that isn't a meet."""
    path = os.path.abspath(str(path or "").strip())
    if os.path.basename(path).lower() == MANIFEST:
        path = os.path.dirname(path)
    if is_split_folder(path):
        return join(read_folder(path))
    if os.path.isdir(path):
        for n in ("meet.json",):
            if os.path.isfile(os.path.join(path, n)):
                path = os.path.join(path, n)
                break
        else:
            raise ValueError(f"{path}: no meet.json or manifest.json there")
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    snap = json.loads(data.decode("utf-8"))
    if not isinstance(snap, dict) or "meet" not in snap:
        raise ValueError(f"{path} is not a meet file")
    return snap


def write_folder(folder: str, files: Dict[str, bytes], prune: bool = True) -> List[str]:
    """Write split()'s files into `folder`: only those whose bytes changed,
    each atomically, the manifest last; then (prune) delete what the
    manifest no longer lists. Returns the paths written."""
    written = []
    for rel in sorted(r for r in files if r != MANIFEST) + [MANIFEST]:
        p = os.path.join(folder, *rel.split("/"))
        try:
            with open(p, "rb") as f:
                if f.read() == files[rel]:
                    continue
        except OSError:
            pass
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = os.path.join(os.path.dirname(p), "." + os.path.basename(p) + ".tmp")
        with open(tmp, "wb") as f:
            f.write(files[rel])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        written.append(rel)
    if prune:
        keep = {r.lower() for r in files}
        for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
            for n in filenames:
                p = os.path.join(dirpath, n)
                rel = os.path.relpath(p, folder).replace(os.sep, "/")
                if rel.lower() not in keep and (n.endswith(".json.gz") or n.endswith(".tmp")):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            if dirpath != folder and not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return written


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Rebuild a meet from a split shared-folder copy, or split one")
    sub = p.add_subparsers(dest="cmd", required=True)
    rb = sub.add_parser("rebuild", help="a split folder / manifest.json / .json.gz / .json -> a meet .json "
                                         "(-o x.json), or a meet folder with its pulls (-o folder)")
    rb.add_argument("source")
    rb.add_argument("-o", "--out", default="meet.json")
    sp = sub.add_parser("split", help="a meet .json -> a split folder")
    sp.add_argument("source")
    sp.add_argument("folder")
    a = p.parse_args(argv)
    try:
        snap = read_any(a.source)
    except Incomplete as e:
        print(f"not complete yet: {e}")
        return 2
    except (OSError, ValueError) as e:
        print(f"cannot read {a.source}: {e}")
        return 1
    if a.cmd == "rebuild":
        meet = snap["meet"] if is_snapshot(snap) else snap
        if a.out.lower().endswith(".json") or not is_snapshot(snap):
            with open(a.out, "w", encoding="utf-8") as f:     # the meet alone, one file
                json.dump(meet, f, indent=1, ensure_ascii=False)
        else:
            # a folder: meet.json plus the pulls and upload record, as a
            # console's meet store keeps them (Open another... takes it)
            from .meet_sync import write_snapshot
            snap.pop("sync", None)
            write_snapshot(os.path.dirname(os.path.abspath(a.out)), os.path.basename(os.path.abspath(a.out)), snap)
        print(f"{a.out}: {len(meet.get('athletes') or [])} athletes, {len(meet.get('races') or [])} races")
        return 0
    os.makedirs(a.folder, exist_ok=True)
    written = write_folder(a.folder, split(snap))
    print(f"{a.folder}: {len(written)} file(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
