"""Meet files shared between machines (docs/MEET-SYNC-PLAN.md, 2026-10-07).

Every machine publishes its own copy of each meet - one compressed snapshot
per meet per machine, <meet-id>/<MACHINE>.json.gz - and only ever writes its
own file. It reads the other machines' copies and merges them into its meet
store record by record (teams, athletes, events, races, a race's entries -
by id), three-way against the copy of that machine it merged last time. So
nothing in between ever sees two machines write one file: no git merge
conflicts, no Google Drive "conflicted copies".

Two transports, one engine (user, 2026-10-07: "one repo, two builds"):
  GitTransport     a private git repo over SSH (the owner's Forgejo on the
                   NAS): pushes are deltas, the history is the backup.
  FolderTransport  a plain shared folder - Google Drive for desktop on a
                   friend's PC, or any synced/shared folder. No OAuth.

What travels: meet.json, pulls/*.json (MileSplit pulls) and
milesplit-uploads.json. archive.json (where this machine closed the meet to)
and anything temporary stay local. A save that changed only updated_at is
not a change. Deleting a meet here never deletes it elsewhere.

When the same record was changed on both sides (relative to the last merge)
this machine's value is kept and the clash is recorded in `conflicts`, for
the console to offer "Take theirs" per record. Nothing is lost: the other
machine's copy stays in its own file (and in git history).

    python -m rfid_finishlynx.meet_sync --once      # one sync with config.yaml
"""

import copy
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import meet_split

logger = logging.getLogger(__name__)

SNAP_EXT = ".json.gz"
MEET_FILE = "meet.json"
SHARED_FILES = ("milesplit-uploads.json",)      # besides meet.json and pulls/*.json
MISSING = object()

# list -> the field(s) that identify an element; "*" = any element of the list above
ID_KEYS = {
    ("meet", "teams"): ("id",),
    ("meet", "athletes"): ("id",),
    ("meet", "events"): ("num",),
    ("meet", "races"): ("event", "round", "heat"),
    ("meet", "races", "*", "entries"): ("athlete_id", "event"),
}


def machine_name(configured: str = "") -> str:
    """This machine's name in file names and commit messages."""
    name = configured or os.environ.get("COMPUTERNAME") or socket.gethostname() or "machine"
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name.split(".")[0]).strip("-")
    return (name or "machine").upper()


# ------------------------------------------------------------- snapshots

def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_snapshot(store_root: str, meet_id: str) -> Optional[dict]:
    """{"meet": meet.json, "files": {"pulls/entries.json": ..., ...}} or None."""
    d = os.path.join(store_root, meet_id)
    try:
        meet = _read_json(os.path.join(d, MEET_FILE))
    except (OSError, ValueError):
        return None
    files = {}
    pulls = os.path.join(d, "pulls")
    if os.path.isdir(pulls):
        for n in sorted(os.listdir(pulls)):
            if n.endswith(".json"):
                try:
                    files[f"pulls/{n}"] = _read_json(os.path.join(pulls, n))
                except (OSError, ValueError):
                    pass
    for n in SHARED_FILES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            try:
                files[n] = _read_json(p)
            except (OSError, ValueError):
                pass
    return {"meet": meet, "files": files}


def _write_json(path: str, data, indent=None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def write_snapshot(store_root: str, meet_id: str, snap: dict) -> None:
    """Into the store as the console writes it (meet.json indent=1)."""
    d = os.path.join(store_root, meet_id)
    for name, data in (snap.get("files") or {}).items():
        if name.startswith("pulls/") or name in SHARED_FILES:
            _write_json(os.path.join(d, *name.split("/")), data)
    _write_json(os.path.join(d, MEET_FILE), snap["meet"], indent=1)


def _without_stamp(snap: Optional[dict]) -> Optional[dict]:
    """The content alone: no updated_at, no sync header."""
    if snap is None:
        return None
    s = {k: v for k, v in snap.items() if k != "sync"}
    s["meet"] = {k: v for k, v in snap["meet"].items() if k != "updated_at"}
    return s


def _without_header(snap: Optional[dict]) -> Optional[dict]:
    return None if snap is None else {k: v for k, v in snap.items() if k != "sync"}


def digest(snap: Optional[dict]) -> str:
    """Content hash, blind to updated_at."""
    if snap is None:
        return ""
    raw = json.dumps(_without_stamp(snap), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def pack(snap: dict) -> bytes:
    """Deterministic gzip (mtime 0): the same meet gives the same bytes."""
    raw = json.dumps(snap, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def unpack(data: bytes) -> dict:
    """A snapshot from gzip (shared folder) or plain JSON (git repo)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


# ----------------------------------------------------------------- merge

def _shape(path: tuple) -> tuple:
    return tuple("*" if isinstance(p, tuple) else p for p in path)


def _label(path: tuple) -> str:
    out = ""
    for p in path:
        if isinstance(p, tuple):
            out += "[" + ",".join(str(x) for x in p) + "]"
        else:
            out += ("." if out else "") + str(p)
    return out


def merge3(base, mine, theirs, path: tuple = ()) -> Tuple[object, List[dict]]:
    """Three-way merge of JSON values. Lists named in ID_KEYS merge per
    element by id; other lists and scalars are one value. A value changed on
    both sides to different things keeps `mine` and is reported."""
    conflicts: List[dict] = []
    merged = _merge(base, mine, theirs, path, conflicts)
    return merged, conflicts


def _atomic(path: tuple) -> bool:
    # whole files other than meet.json (MileSplit pulls, upload log) are one value each
    return len(path) == 2 and path[0] == "files"


def _merge(base, mine, theirs, path, conflicts):
    if mine == theirs:
        return mine
    if base == mine:
        return theirs
    if base == theirs:
        return mine
    if not _atomic(path):
        if isinstance(mine, dict) and isinstance(theirs, dict):
            b = base if isinstance(base, dict) else {}
            out = {}
            for k in list(mine) + [k for k in theirs if k not in mine]:
                if path == ("meet",) and k == "updated_at":
                    out[k] = max(str(mine.get(k) or ""), str(theirs.get(k) or ""))
                    continue
                v = _merge(b.get(k, MISSING), mine.get(k, MISSING), theirs.get(k, MISSING),
                           path + (k,), conflicts)
                if v is not MISSING:
                    out[k] = v
            return out
        keys = ID_KEYS.get(_shape(path))
        if keys and isinstance(mine, list) and isinstance(theirs, list):
            def index(lst):
                return {tuple(e.get(k) for k in keys): e for e in lst or [] if isinstance(e, dict)}
            b, m, t = index(base if isinstance(base, list) else []), index(mine), index(theirs)
            order = list(m) + [i for i in t if i not in m]
            out = []
            for i in order:
                v = _merge(b.get(i, MISSING), m.get(i, MISSING), t.get(i, MISSING), path + (i,), conflicts)
                if v is not MISSING:
                    out.append(v)
            return out
    conflicts.append({"path": _label(path), "steps": [list(p) if isinstance(p, tuple) else p for p in path],
                      "theirs_missing": theirs is MISSING,
                      "base": None if base is MISSING else base,
                      "mine": None if mine is MISSING else mine,
                      "theirs": None if theirs is MISSING else theirs})
    return mine


def set_at(snap: dict, steps: list, value, remove: bool = False) -> None:
    """Put `value` at a conflict's steps (remove=True: take it out)."""
    cur, path = snap, ()
    for i, step in enumerate(steps):
        last = i == len(steps) - 1
        if isinstance(step, list):
            keys = ID_KEYS[_shape(path)]
            idx = next((j for j, e in enumerate(cur) if [e.get(k) for k in keys] == step), None)
            if last:
                if remove:
                    if idx is not None:
                        del cur[idx]
                elif idx is None:
                    cur.append(value)
                else:
                    cur[idx] = value
                return
            cur, path = cur[idx], path + (tuple(step),)
        else:
            if last:
                if remove:
                    cur.pop(step, None)
                else:
                    cur[step] = value
                return
            cur, path = cur[step], path + (step,)


# ------------------------------------------------------------ transports

class Copy(bytes):
    """A machine's file as read from the shared place, with its file's
    (size, mtime) so the next round can skip it if neither moved."""
    sig: Optional[tuple] = None
    path: str = ""
    unchanged = False
    single = True             # False: rebuilt from a split folder (meet_split)


def _file_sig(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns)


class SyncOffline(Exception):
    """The shared place can't be reached right now; try again later."""


class FolderTransport:
    """A shared folder: Google Drive for desktop, a network share, a USB stick.
    <root>/<meet-id>/<MACHINE>/ split by section (meet_split.py: a manifest,
    meet / teams, athletes per team, one file per race); each machine writes
    only its own, and only the files an edit changed (2026-10-10: Drive
    uploads a changed file whole, so the single <MACHINE>.json.gz went up
    again - 265 KB for Bob Jenkins - on every edit). The single file is
    still read (a machine not updated yet) and replaced by the folder on
    our next write.
    plain=True (the git repo's working tree): <MACHINE>.json, readable in
    the git server's web pages and diffed line by line, so a push carries
    only the changed lines (2026-10-07: gzip made every edit a whole file)."""

    name = "folder"

    def __init__(self, root: str, plain: bool = False):
        self.root = root
        self.plain = plain
        self.splits = not plain                   # writes the split folder (meet_split)

    def _check(self):
        if not self.root or not os.path.isdir(self.root):
            raise SyncOffline(f"sync folder not found: {self.root or '(not set)'}")

    def fetch(self, seen: Optional[Dict[str, tuple]] = None) -> Dict[str, Dict[str, bytes]]:
        self._check()
        return self.read_tree(seen)

    def read_tree(self, seen: Optional[Dict[str, tuple]] = None) -> Dict[str, Dict[str, bytes]]:
        """seen: path -> (size, mtime) already dealt with; such a file isn't
        read again, its entry is an empty Copy with unchanged=True
        (2026-10-08, user: check "in the most data-friendly way possible")."""
        out: Dict[str, Dict[str, bytes]] = {}
        for meet_id in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, meet_id)
            if meet_id.startswith(".") or not os.path.isdir(d):
                continue                          # .git, the sync app's own folders
            for n in sorted(os.listdir(d)):
                if not self.plain and not n.startswith(".") and meet_split.is_split_folder(os.path.join(d, n)):
                    data = self._read_split(os.path.join(d, n), seen)
                    if data is not None:
                        out.setdefault(meet_id, {})[n] = data       # the folder wins over a single file
                    continue
                ext = SNAP_EXT if n.endswith(SNAP_EXT) else ".json" if n.endswith(".json") else None
                if ext is None or n.startswith("."):
                    continue
                machine = n[:-len(ext)]
                if machine in out.get(meet_id, {}) and (ext != (".json" if self.plain else SNAP_EXT)
                                                        or not getattr(out[meet_id][machine], "single", True)):
                    continue                      # both forms (a switch): this transport's wins
                if not self.plain and meet_split.is_split_folder(os.path.join(d, machine)):
                    continue                      # its split folder is the copy (read above or below)
                p = os.path.join(d, n)
                try:
                    sig = _file_sig(os.stat(p))
                    if seen is not None and seen.get(p) == sig:
                        data = Copy()
                        data.unchanged = True
                    else:
                        with open(p, "rb") as f:
                            data = Copy(f.read())
                except OSError:
                    continue                      # mid-download by the sync app: next time
                data.sig, data.path = sig, p
                out.setdefault(meet_id, {})[machine] = data
        return out

    @staticmethod
    def _read_split(folder: str, seen: Optional[Dict[str, tuple]]) -> Optional["Copy"]:
        """A split copy as one snapshot (pack()ed). Its manifest is written
        last and lists every file's hash, so the manifest's size/mtime stands
        for the whole folder. None while it's half-synced: next round."""
        mpath = os.path.join(folder, meet_split.MANIFEST)
        try:
            sig = _file_sig(os.stat(mpath))
            if seen is not None and seen.get(mpath) == sig:
                data = Copy()
                data.unchanged = True
            else:
                data = Copy(pack(meet_split.join(meet_split.read_folder(folder))))
        except meet_split.Incomplete as e:
            logger.info(f"Meet sync: {folder} not complete yet ({e}); next round")
            return None
        except (OSError, ValueError, EOFError):
            return None                           # mid-download by the sync app: next time
        data.sig, data.path, data.single = sig, mpath, False
        return data

    def put(self, meet_id: str, machine: str, data: bytes) -> None:
        self._check()
        d = os.path.join(self.root, meet_id)
        os.makedirs(d, exist_ok=True)
        if not self.plain:
            # split by section: only the files this edit changed are written
            # (and uploaded by the sync app); the single file goes
            written = meet_split.write_folder(os.path.join(d, machine), meet_split.split(unpack(data)))
            for old in (machine + SNAP_EXT, machine + ".json"):
                if os.path.exists(os.path.join(d, old)):
                    os.remove(os.path.join(d, old))
            logger.info(f"Meet sync: {meet_id}/{machine}: {len(written)} file(s) written"
                        + (f" ({', '.join(written[:4])}{' ...' if len(written) > 4 else ''})" if written else ""))
            return
        ext, other = (".json", SNAP_EXT) if self.plain else (SNAP_EXT, ".json")
        if self.plain and data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        path = os.path.join(d, machine + ext)
        stale = os.path.join(d, machine + other)
        if os.path.exists(stale):
            os.remove(stale)
        tmp = os.path.join(d, f".{machine}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def finish(self, message: str) -> None:
        pass


def find_git(configured: str = "") -> Optional[str]:
    """git: the configured path, PATH, then portable MinGit (trailer PC)."""
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("git")
    if found:
        return found
    mingit = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "MinGit", "cmd", "git.exe")
    return mingit if os.path.isfile(mingit) else None


# ssh keepalives end a stalled transfer instead of letting it sit until the
# timeout (2026-10-08: the trailer's hotspot dropped big packets mid-clone).
GIT_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"


def run_git(cmd: List[str], timeout: float, env: dict) -> subprocess.CompletedProcess:
    """subprocess.run, but a timeout kills git's helpers too. (2026-10-08) On
    Windows run() kills only git.exe and then waits, with no limit, for the
    output pipes to close; ssh.exe/git-remote-https still hold them, so a
    stalled fetch hung the sync thread for good. Raises TimeoutExpired."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, env=env, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=10)
            else:
                os.killpg(p.pid, 9)
        except (OSError, subprocess.SubprocessError):
            pass
        p.kill()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass   # a helper we couldn't kill keeps its pipe: leave it, don't block
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


class GitTransport:
    """A private git repo (e.g. on your own git server): a working clone in
    `clone_dir`, fetch/rebase in, commit/push out. Only this machine's files
    change in its commits, so a rebase never conflicts."""

    name = "git"
    BRANCH = "main"

    def __init__(self, remote: str, clone_dir: str, machine: str, git: str = "",
                 timeout: float = 60.0):
        self.remote = remote
        self.dir = clone_dir
        self.machine = machine
        self.git_exe = find_git(git)
        self.timeout = timeout

    def _git(self, *args, check=True) -> subprocess.CompletedProcess:
        if not self.git_exe:
            raise SyncOffline("git not found")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", GIT_SSH))
        cmd = [self.git_exe, "-C", self.dir, "-c", f"user.name={self.machine}",
               "-c", f"user.email={self.machine.lower()}@meet-sync", *args]
        try:
            r = run_git(cmd, self.timeout, env)
        except subprocess.TimeoutExpired:
            raise SyncOffline(f"git {args[0]} timed out") from None
        if check and r.returncode != 0:
            raise SyncOffline(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    def _ensure_clone(self) -> None:
        if os.path.isdir(os.path.join(self.dir, ".git")):
            return
        os.makedirs(self.dir, exist_ok=True)
        self._git("init", "-q", "-b", self.BRANCH)
        self._git("remote", "add", "origin", self.remote)

    def fetch(self, seen: Optional[Dict[str, tuple]] = None) -> Dict[str, Dict[str, bytes]]:
        self._ensure_clone()
        self._git("fetch", "-q", "origin")
        if self._git("rev-parse", "-q", "--verify", f"origin/{self.BRANCH}", check=False).returncode == 0:
            if self._git("rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0:
                self._git("reset", "-q", "--hard", f"origin/{self.BRANCH}")
            else:
                self._git("rebase", "-q", f"origin/{self.BRANCH}")
        return FolderTransport(self.dir, plain=True).read_tree(seen)

    def put(self, meet_id: str, machine: str, data: bytes) -> None:
        self._ensure_clone()
        FolderTransport(self.dir, plain=True).put(meet_id, machine, data)

    def finish(self, message: str) -> None:
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self._git("commit", "-q", "-m", message)
        for _ in range(3):
            if self._git("push", "-q", "origin", f"HEAD:{self.BRANCH}", check=False).returncode == 0:
                return
            self._git("fetch", "-q", "origin")
            self._git("rebase", "-q", f"origin/{self.BRANCH}")
        raise SyncOffline("git push refused three times")


# ---------------------------------------------------------------- engine

class DirStore:
    """The console's meet store on disk (meet/<id>/...)."""

    def __init__(self, root: str):
        self.root = root

    def ids(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isfile(os.path.join(self.root, d, MEET_FILE)))

    def read(self, meet_id: str) -> Optional[dict]:
        return read_snapshot(self.root, meet_id)

    def stamp(self, meet_id: str) -> Optional[tuple]:
        """(name, size, mtime) of every file read_snapshot reads: an equal
        stamp is an equal snapshot, without reading the files."""
        d = os.path.join(self.root, meet_id)
        names = [MEET_FILE] + list(SHARED_FILES)
        pulls = os.path.join(d, "pulls")
        if os.path.isdir(pulls):
            names += [f"pulls/{n}" for n in sorted(os.listdir(pulls)) if n.endswith(".json")]
        out = []
        for n in names:
            try:
                out.append((n,) + _file_sig(os.stat(os.path.join(d, n))))
            except OSError:
                if n == MEET_FILE:
                    return None
        return tuple(out)

    def write(self, meet_id: str, snap: dict) -> None:
        write_snapshot(self.root, meet_id, snap)

    def update(self, meet_id: str, merge: Callable[[Optional[dict]], Optional[dict]]) -> bool:
        """merge(current) -> new snapshot, or None for no change. False =
        not now (try next round)."""
        new = merge(self.read(meet_id))
        if new is not None:
            self.write(meet_id, new)
        return True


class ServiceStore(DirStore):
    """The store as the console sees it: the meet open in the console is
    merged inside the meet service (its lock, then reloaded like Open), and
    waits while one of its races is running."""

    def __init__(self, service):
        super().__init__(service.root)
        self.service = service

    def update(self, meet_id, merge):
        done = self.service.apply_synced(meet_id, merge)
        return super().update(meet_id, merge) if done is None else done


class MeetSync:
    """One sync round at a time (sync_once); the caller schedules them."""

    STATE = "state.json"

    def __init__(self, store, transport, machine: str, state_dir: str,
                 on_merged: Optional[Callable[[str, List[str]], None]] = None):
        self.store = store
        self.transport = transport
        self.machine = machine
        self.state_dir = state_dir
        self.on_merged = on_merged
        os.makedirs(state_dir, exist_ok=True)
        self.state = self._load_state()
        # not read again while their files haven't moved (in memory: after a
        # restart everything is read once): shared-place path -> (size,
        # mtime), and meet -> (stamp of our files, digest)
        self.seen: Dict[str, tuple] = {}
        self.own_digests: Dict[str, Tuple[tuple, str]] = {}

    # -- state: what we published, what we merged from whom, open conflicts
    def _load_state(self) -> dict:
        try:
            s = _read_json(os.path.join(self.state_dir, self.STATE))
        except (OSError, ValueError):
            s = {}
        s.setdefault("published", {})      # meet -> digest of our last snapshot
        s.setdefault("merged", {})         # meet -> machine -> digest merged
        s.setdefault("conflicts", {})      # meet -> [ {path, mine, theirs, machine, at} ]
        s.setdefault("last_ok", None)          # last check, whether or not anything moved
        s.setdefault("last_error", "")
        # when something actually went out / came in - "synced 2 min ago" read
        # as sending when it was only a look (2026-10-08, user)
        s.setdefault("last_received", None)
        if "last_sent" not in s:
            s["last_sent"] = self._newest_own()
        return s

    def _newest_own(self) -> Optional[str]:
        """When we last published, from the copies kept of what we sent
        (state written before last_sent was kept)."""
        newest = 0.0
        root = os.path.join(self.state_dir, "bases")
        try:
            meets = os.listdir(root)
        except OSError:
            return None
        for m in meets:
            own = os.path.join(root, m, "_own")
            try:
                newest = max([newest] + [os.path.getmtime(os.path.join(own, n)) for n in os.listdir(own)])
            except OSError:
                continue
        return datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None

    def _save_state(self) -> None:
        _write_json(os.path.join(self.state_dir, self.STATE), self.state, indent=1)

    def _base_path(self, meet_id: str, machine: str) -> str:
        return os.path.join(self.state_dir, "bases", meet_id, machine + SNAP_EXT)

    def _base(self, meet_id: str, machine: str) -> Optional[dict]:
        try:
            with open(self._base_path(meet_id, machine), "rb") as f:
                return unpack(f.read())
        except (OSError, ValueError):
            return None

    def _set_base(self, meet_id: str, machine: str, data: bytes) -> None:
        p = self._base_path(meet_id, machine)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)

    KEEP_OWN = 20                         # our last published versions kept per meet

    def _own_path(self, meet_id: str, d: str) -> str:
        return os.path.join(self.state_dir, "bases", meet_id, "_own", d + SNAP_EXT)

    def _keep_own(self, meet_id: str, d: str, data: bytes) -> None:
        p = self._own_path(meet_id, d)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        own = sorted((os.path.join(os.path.dirname(p), n) for n in os.listdir(os.path.dirname(p))),
                     key=os.path.getmtime)
        for old in own[:-self.KEEP_OWN]:
            os.remove(old)

    def _own(self, meet_id: str, d: str) -> Optional[dict]:
        try:
            with open(self._own_path(meet_id, d), "rb") as f:
                return unpack(f.read())
        except (OSError, ValueError):
            return None

    def _pick_base(self, meet_id: str, machine: str, theirs: dict) -> Optional[dict]:
        """The common version to merge against: what we merged from them
        last time; the first time, the version of ours their copy says it
        took in, or ours if their copy simply is one of ours."""
        base = self._base(meet_id, machine)
        if base is not None:
            return base
        took = (theirs.get("sync") or {}).get("merged") or {}
        if took.get(self.machine):
            own = self._own(meet_id, took[self.machine])
            if own is not None:
                return own
        for other, d in took.items():             # a third machine's copy we both took in
            b = self._base(meet_id, other) if other != self.machine else None
            if b is not None and digest(b) == d:
                return b
        return self._own(meet_id, digest(theirs))

    def sync_once(self) -> dict:
        """Fetch, merge others in, publish ours. Returns a report; raises
        SyncOffline (state keeps last_error) when the shared place is away."""
        report = {"merged": [], "added": [], "published": [], "conflicts": {}}
        try:
            remote = self.transport.fetch(self.seen)
            for meet_id, copies in remote.items():
                for machine, data in copies.items():
                    if getattr(data, "unchanged", False):
                        continue
                    if machine == self.machine or self._merge_in(meet_id, machine, data, report):
                        if getattr(data, "sig", None):
                            self.seen[data.path] = data.sig
            published = []
            for meet_id in self.store.ids():
                d = self._digest_own(meet_id)
                # also when our copy isn't there: sharing moved to another
                # folder/repo, or our file was deleted - "published" only says
                # what we last sent, not where (2026-10-08: switched from the
                # NAS to Google Drive, the Drive folder stayed empty)
                own = remote.get(meet_id, {}).get(self.machine)
                # our copy still the old single file: write it split (once)
                legacy = own is not None and getattr(self.transport, "splits", False)                     and getattr(own, "single", True)
                if not d or (self.state["published"].get(meet_id) == d and own is not None and not legacy):
                    continue
                snap = self.store.read(meet_id)
                if snap is None:
                    continue
                d = digest(snap)
                snap["sync"] = {"machine": self.machine,
                                "merged": dict(self.state["merged"].get(meet_id) or {})}
                data = pack(snap)
                self.transport.put(meet_id, self.machine, data)
                self._keep_own(meet_id, d, data)
                self.state["published"][meet_id] = d
                published.append(meet_id)
            report["published"] = published
            if published:
                names = ", ".join(self._name(m) for m in published[:3]) + (" ..." if len(published) > 3 else "")
                self.transport.finish(f"{names}: edited on {self.machine}")
        except SyncOffline as e:
            self.state["last_error"] = str(e)
            self._save_state()
            raise
        now = datetime.now().isoformat(timespec="seconds")
        self.state["last_ok"] = now
        self.state["last_error"] = ""
        if report["published"]:
            self.state["last_sent"] = now
        if report["merged"] or report["added"]:
            self.state["last_received"] = now
        self._save_state()
        return report

    def unsent(self) -> List[str]:
        """Meets changed here since we last published them."""
        return [m for m in self.store.ids()
                if self.state["published"].get(m) != self._digest_own(m)]

    def _digest_own(self, meet_id: str) -> str:
        """digest() of our copy, read again only when one of its files moved."""
        stamp = getattr(self.store, "stamp", lambda m: None)(meet_id)
        hit = self.own_digests.get(meet_id)
        if stamp is not None and hit and hit[0] == stamp:
            return hit[1]
        d = digest(self.store.read(meet_id))
        if stamp is not None:
            self.own_digests[meet_id] = (stamp, d)
        return d

    def resolve(self, meet_id: str, index: int, take: str) -> bool:
        """A recorded clash: "theirs" puts the other machine's value in
        (sent on the next round), "mine" keeps ours. False = not now (race)."""
        clashes = self.state["conflicts"].get(meet_id) or []
        if not 0 <= index < len(clashes):
            raise IndexError(f"no conflict {index} for {meet_id}")
        c = clashes[index]
        if take == "theirs":
            def put(mine):
                if mine is None:
                    return None
                new = copy.deepcopy(mine)
                set_at(new, c["steps"], copy.deepcopy(c["theirs"]), remove=c.get("theirs_missing", False))
                return new
            if self.store.update(meet_id, put) is False:
                return False
        del clashes[index]
        if not clashes:
            self.state["conflicts"].pop(meet_id, None)
        self._save_state()
        return True

    def _name(self, meet_id: str) -> str:
        snap = self.store.read(meet_id)
        return ((snap or {}).get("meet", {}).get("meet", {}) or {}).get("name") or meet_id

    def _merge_in(self, meet_id: str, machine: str, data: bytes, report: dict) -> None:
        try:
            theirs = unpack(data)
        except (OSError, ValueError, EOFError):
            logger.warning(f"Meet sync: unreadable copy of {meet_id} from {machine}, skipped")
            return False
        d = digest(theirs)
        if self.state["merged"].get(meet_id, {}).get(machine) == d:
            return True
        base = _without_header(self._pick_base(meet_id, machine, theirs))
        theirs = _without_header(theirs)
        out = {"conflicts": [], "changed": False, "added": False}

        def merge(mine):
            if mine is None:
                merged, out["added"] = theirs, True
            else:
                merged, out["conflicts"] = merge3(base, mine, theirs)
            out["changed"] = digest(merged) != digest(mine)
            return merged if out["changed"] else None

        if self.store.update(meet_id, merge) is False:
            report.setdefault("waiting", []).append(meet_id)      # a race is running: next round
            return False
        conflicts = out["conflicts"]
        if out["added"]:
            report["added"].append(meet_id)
        if out["changed"]:
            report["merged"].append(meet_id)
            if self.on_merged:
                self.on_merged(meet_id, [machine])
        self._set_base(meet_id, machine, data)
        self.state["merged"].setdefault(meet_id, {})[machine] = d
        if conflicts:
            at = datetime.now().isoformat(timespec="seconds")
            kept = self.state["conflicts"].setdefault(meet_id, [])
            kept += [dict(c, machine=machine, at=at) for c in conflicts]
            report["conflicts"][meet_id] = conflicts
            logger.warning(f"Meet sync: {len(conflicts)} change(s) to {meet_id} clash with {machine}'s; kept ours")
        return True


# ---------------------------------------------------------------- runner

class SyncRunner:
    """The background thread: a push about a minute after the last save
    (at most every 5 min while editing), a look for others' changes every
    5 min (15 while a race runs), backing off 1-2-5-15 min while offline.
    sync_now() for the console's Sync now."""

    QUIET, MIN_PUSH = 60.0, 300.0
    IDLE_EVERY, RACE_EVERY = 300.0, 900.0
    BACKOFF = (60.0, 120.0, 300.0, 900.0)

    def __init__(self, sync: MeetSync, is_racing: Callable[[], bool] = lambda: False,
                 on_report: Optional[Callable[[dict], None]] = None, clock=time.monotonic):
        import threading
        self.sync = sync
        self.is_racing = is_racing
        self.on_report = on_report
        self.clock = clock
        self.lock = threading.Lock()          # one round at a time
        self.wake = threading.Event()
        self.saved_at: Optional[float] = None
        self.last_push = -1e9
        self.last_round = -1e9
        self.fails = 0
        self.next_try = 0.0
        self.running = False
        self.last_report: dict = {}

    def notify_saved(self, meet_id: str = "") -> None:
        self.saved_at = self.clock()

    def due(self) -> bool:
        now = self.clock()
        if now < self.next_try:
            return False
        if self.fails:
            return True                           # offline: retry on the backoff, not the 5 min
        if self.saved_at is not None and now - self.saved_at >= self.QUIET \
                and now - self.last_push >= self.MIN_PUSH:
            return True
        return now - self.last_round >= (self.RACE_EVERY if self.is_racing() else self.IDLE_EVERY)

    def run_round(self) -> dict:
        with self.lock:
            now = self.clock()
            self.last_round = now
            try:
                rep = self.sync.sync_once()
            except SyncOffline as e:
                self.fails += 1
                self.next_try = now + self.BACKOFF[min(self.fails, len(self.BACKOFF)) - 1]
                rep = {"error": str(e)}
            else:
                self.fails, self.next_try = 0, 0.0
                if rep.get("published"):
                    self.last_push = now
                # saves made by the merge itself are already out
                if self.saved_at is not None and (self.saved_at <= now or not self.sync.unsent()):
                    self.saved_at = None
            self.last_report = rep
        if self.on_report:
            try:
                self.on_report(rep)
            except Exception:
                logger.exception("meet sync report hook failed")
        return rep

    def sync_now(self) -> dict:
        self.next_try = 0.0
        return self.run_round()

    def start(self) -> None:
        import threading
        if self.running:
            return
        self.running = True

        def loop():
            while self.running:
                try:
                    if self.due():
                        self.run_round()
                except Exception:
                    logger.exception("Meet sync round failed")
                self.wake.wait(15.0)
                self.wake.clear()
        threading.Thread(target=loop, name="meet-sync", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self.wake.set()

    def status(self) -> dict:
        st = self.sync.state
        return {"machine": self.sync.machine, "transport": self.sync.transport.name,
                "last_ok": st.get("last_ok"), "last_error": st.get("last_error") or "",
                "last_sent": st.get("last_sent"), "last_received": st.get("last_received"),
                "waiting_to_send": self.saved_at is not None,
                "conflicts": {m: len(c) for m, c in (st.get("conflicts") or {}).items() if c},
                "last": self.last_report}


# ------------------------------------------------------------------- CLI

class SyncGroup:
    """Several shared places at once (2026-10-08, user: "select one or both
    options as backups"): the NAS and a shared folder, each its own MeetSync
    + SyncRunner with its own state (what was sent where, bases, clashes),
    all merging into the one meet store. One place offline never holds the
    other up. Same face as a SyncRunner for the app and the API."""

    def __init__(self, runners: List[Tuple[str, "SyncRunner"]]):
        self.runners = runners

    @property
    def sync(self) -> MeetSync:                     # the first place (CLI, older callers)
        return self.runners[0][1].sync

    def runner(self, target: str) -> "SyncRunner":
        for name, r in self.runners:
            if name == target:
                return r
        raise KeyError(target)

    def start(self) -> None:
        for _, r in self.runners:
            r.start()

    def stop(self) -> None:
        for _, r in self.runners:
            r.stop()

    def notify_saved(self, meet_id: str = "") -> None:
        for _, r in self.runners:
            r.notify_saved(meet_id)

    def sync_now(self) -> dict:
        out = {"merged": [], "added": [], "published": [], "waiting": [], "errors": {}}
        for name, r in self.runners:
            rep = r.sync_now()
            if rep.get("error"):
                out["errors"][name] = rep["error"]
            for k in ("merged", "added", "published", "waiting"):
                out[k] += [m for m in rep.get(k) or [] if m not in out[k]]
        if out["errors"] and len(out["errors"]) == len(self.runners):
            out["error"] = "; ".join(f"{TARGET_LABEL[n]}: {e}" for n, e in out["errors"].items())
        return out

    def status(self) -> dict:
        each = [(name, r.status()) for name, r in self.runners]
        oks = [s["last_ok"] for _, s in each]
        conflicts: Dict[str, int] = {}
        for _, s in each:
            for m, n in s["conflicts"].items():
                conflicts[m] = conflicts.get(m, 0) + n
        return {"machine": each[0][1]["machine"],
                "transport": "+".join(s["transport"] for _, s in each),
                "last_ok": None if None in oks else min(oks),          # when every place last took it
                "last_sent": max((s["last_sent"] for _, s in each if s["last_sent"]), default=None),
                "last_received": max((s["last_received"] for _, s in each if s["last_received"]), default=None),
                "last_error": "; ".join(f"{TARGET_LABEL[n]}: {s['last_error']}" for n, s in each if s["last_error"]),
                "waiting_to_send": any(s["waiting_to_send"] for _, s in each),
                "conflicts": conflicts,
                "targets": [{"target": n, "label": TARGET_LABEL[n], **s} for n, s in each],
                "last": {n: s["last"] for n, s in each}}

    def clashes(self) -> List[dict]:
        out = []
        for name, r in self.runners:
            for meet_id, cs in (r.sync.state.get("conflicts") or {}).items():
                for i, x in enumerate(cs):
                    out.append({"target": name, "meet": meet_id, "index": i, "path": x["path"],
                                "machine": x.get("machine"), "mine": x.get("mine"),
                                "theirs": x.get("theirs"), "at": x.get("at")})
        return out


TARGET_LABEL = {"git": "NAS", "folder": "Shared folder"}


def is_git_remote(place: str) -> bool:
    """A git remote (host:path, ssh://, https://, *.git) rather than a folder
    (G:\\My Drive\\..., \\\\server\\share, /mnt/...). The NAS place may be either
    (2026-10-08: a folder on a network share works as well as the repo)."""
    p = (place or "").strip()
    return p.endswith(".git") or "://" in p or bool(re.match(r"^[^\\/:]{2,}:", p))


def edition(base: Optional[str] = None) -> dict:
    """The installed package's EDITION.json (tools/build_package.py), next to
    the rfid_finishlynx package; {} in a development checkout."""
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(base, "EDITION.json"), encoding="utf-8") as f:
            ed = json.load(f)
        return ed if isinstance(ed, dict) else {}
    except (OSError, ValueError):
        return {}


def allowed_targets(ed: Optional[dict] = None) -> Tuple[str, ...]:
    """Where this install may share meets: "folder" (Google Drive for
    desktop) and/or "git" (the owner's NAS). The public edition: the folder
    only (2026-10-10, user) - also a package built before sync_targets
    existed, by its name."""
    ed = edition() if ed is None else ed
    targets = ed.get("sync_targets")
    if isinstance(targets, list) and targets:
        return tuple(t for t in ("git", "folder") if t in targets)
    if ed.get("edition") == "public":
        return ("folder",)
    return ("git", "folder")


def engines_from_config(config, store_root: str, base_dir: str, store=None) -> List[Tuple[str, MeetSync]]:
    """One engine per place config.meet_sync turns on, NAS first; [] when off.
    State: the NAS in meet-sync/ (where it always was), the shared folder in
    meet-sync/drive/, so each knows what it sent where. A place the edition
    doesn't allow (the public one: no NAS) is never started, whatever
    config.yaml says."""
    ms = getattr(config, "meet_sync", None)
    if ms is None or not ms.enabled or ms.mode not in ("git", "folder", "both"):
        return []
    allowed = allowed_targets()
    if "git" not in allowed and ms.mode in ("git", "both"):
        logger.warning("Meet sync: this edition shares through a shared folder only - the NAS setting is ignored")
        if ms.mode == "git":
            return []
    machine = machine_name(ms.machine)
    state = os.path.join(base_dir, "meet-sync")
    store = store or DirStore(store_root)
    out = []
    if ms.mode in ("git", "both") and "git" in allowed:
        transport = (GitTransport(ms.remote, os.path.join(state, "clone"), machine, ms.git)
                     if is_git_remote(ms.remote) else FolderTransport(ms.remote))
        out.append(("git", MeetSync(store, transport, machine, state)))
    if ms.mode in ("folder", "both"):
        drive = os.path.join(state, "drive")
        if ms.mode == "folder":
            _move_folder_state(state, drive)
        out.append(("folder", MeetSync(store, FolderTransport(ms.folder), machine, drive)))
    return out


def _move_folder_state(state: str, drive: str) -> None:
    """Before 2026-10-08 a folder-only PC kept its state in meet-sync/ itself:
    move it to meet-sync/drive/ once, so its bases aren't lost (no git clone
    there = it was never the NAS's)."""
    if os.path.isdir(drive) or os.path.isdir(os.path.join(state, "clone")) \
            or not os.path.isfile(os.path.join(state, MeetSync.STATE)):
        return
    os.makedirs(drive)
    for n in (MeetSync.STATE, "bases"):
        if os.path.exists(os.path.join(state, n)):
            os.replace(os.path.join(state, n), os.path.join(drive, n))


def from_config(config, store_root: str, base_dir: str, store=None) -> Optional[MeetSync]:
    """The first engine for config.meet_sync, or None when sync is off."""
    engines = engines_from_config(config, store_root, base_dir, store)
    return engines[0][1] if engines else None


# --------------------------------------------------------------- install

def find_google_drive() -> Optional[str]:
    """Google Drive for desktop's "My Drive": a drive letter (G: unless
    taken) when streaming, a folder under the profile when mirroring."""
    cands = [f"{c}:\\My Drive" for c in "GHIJKLMNOPQRSTUVWXYZDEF"] if os.name == "nt" else []
    home = os.path.expanduser("~")
    cands += [os.path.join(home, "My Drive"), os.path.join(home, "Google Drive", "My Drive"),
              os.path.join(home, "Google Drive")]
    return next((c for c in cands if os.path.isdir(c)), None)


def install_setup(base: str, ask: Callable[[str], str] = input) -> int:
    """INSTALL.bat's meet-sharing step (2026-10-07): set config.yaml's
    meet_sync from the package's EDITION.json - the fleet's NAS repo, or a
    folder in Google Drive for desktop. A PC already sharing is left alone."""
    import yaml
    cfg = os.path.join(base, "config.yaml")
    if not os.path.isfile(cfg):
        example = os.path.join(base, "config.example.yaml")
        if os.path.isfile(example):
            shutil.copyfile(example, cfg)
    try:
        with open(cfg, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        data = {}
    cur = data.get("meet_sync") or {}
    if cur.get("enabled") and cur.get("mode") in ("git", "folder", "both"):
        where = {"git": cur.get("remote"), "folder": cur.get("folder")}.get(
            cur.get("mode"), f"{cur.get('remote')} + {cur.get('folder')}")
        print(f"  Meet sharing is already set: {cur.get('mode')} ({where}). Left as it is.")
        return 0
    try:
        with open(os.path.join(base, "EDITION.json"), encoding="utf-8") as f:
            edition = json.load(f)
    except (OSError, ValueError):
        print("  No edition file: set meet sharing in the console (Meet > Info > Sharing).")
        return 0
    want = edition.get("meet_sync") or {}
    new = {"enabled": True, "mode": want.get("mode"), "folder": "", "remote": "",
           "machine": str(cur.get("machine") or ""), "git": str(cur.get("git") or "")}
    if want.get("mode") == "git":
        new["remote"] = want.get("remote", "")
        git = find_git(new["git"])
        if not git:
            print("  git was not found (fleet-setup installs it). Sharing is set and starts once git is there.")
        else:
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=GIT_SSH)
            try:
                ok = run_git([git, "ls-remote", new["remote"]], 30, env).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            print(f"  The NAS meets repo is {'reachable' if ok else 'not reachable right now'}"
                  f" ({new['remote']}).{'' if ok else ' Sharing is set and catches up when it is.'}")
    elif want.get("mode") == "folder":
        drive = find_google_drive()
        if not drive:
            print("  Google Drive for desktop was not found. To share meets between this team's PCs:")
            print("    1. Install Google Drive for desktop and sign in (the same account on every PC).")
            print("    2. In the console: Meet > Info > Sharing > A shared folder > pick a folder in My Drive.")
            return 0
        folder = os.path.join(drive, "RFID meets")
        try:
            answer = ask(f"  Share meets with your other PCs through {folder} ? [Y/n] ").strip().lower()
        except EOFError:
            answer = ""
        if answer.startswith("n"):
            print("  Not shared. Set it later in the console: Meet > Info > Sharing.")
            return 0
        os.makedirs(folder, exist_ok=True)
        new["folder"] = folder
    else:
        print("  This edition doesn't share meets.")
        return 0
    data["meet_sync"] = new
    tmp = cfg + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.replace(tmp, cfg)
    print(f"  Meet sharing on: {'the NAS repo' if new['mode'] == 'git' else new['folder']}, "
          f"as {machine_name(new['machine'])}. If the console is open, restart it.")
    return 0


def main(argv=None) -> int:
    import argparse
    from .config import load_config
    p = argparse.ArgumentParser(description="Sync the meet store once (see docs/MEET-SYNC-PLAN.md)")
    p.add_argument("--config", default=None)
    p.add_argument("--once", action="store_true", help="one sync round (the default)")
    p.add_argument("--install", action="store_true",
                   help="INSTALL.bat: set meet sharing from EDITION.json in the current folder")
    a = p.parse_args(argv)
    if a.install:
        return install_setup(os.getcwd())
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(a.config)
    base = os.path.dirname(os.path.abspath(cfg.races.state_file or "races-state.json"))
    engines = engines_from_config(cfg, os.path.join(base, "meet"), base)
    if not engines:
        print("meet sync is off (meet_sync.enabled / mode in config.yaml)")
        return 1
    rc = 0
    for name, sync in engines:
        print(f"{TARGET_LABEL[name]}:")
        try:
            print(json.dumps(sync.sync_once(), indent=1))
        except SyncOffline as e:
            print(f"offline: {e}")
            rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
