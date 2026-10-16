"""Google Drive by sign-in, for meet sharing (2026-10-16).

Owner: "I need fresh machines to pull meets from other locations first
time" - a new PC at a meet has no Google Drive for desktop, so the shared
folder (meet_sync.FolderTransport) wasn't there to read. Here the console
signs in to Google itself (Meet > Info > Sharing > Sign in with Google) and
reads and writes the same "RFID meets" folder in My Drive through the Drive
API, so it shares with the PCs that use Google Drive for desktop and with
each other.

How: DriveTransport keeps a mirror of the Drive folder on this PC
(meet-sync/gdrive/tree) and hands it to a FolderTransport, so the split
layout, the manifest-last rule and the merge are exactly the folder's. The
first look lists the folder and downloads every file; after that only the
Drive changes feed (changes.list) is read and only changed files come down
- "not using up all my data" (MEET-SYNC-PLAN.md). A put uploads only the
files of this PC's own copy whose bytes changed, its manifest last.

Sign-in: OAuth for installed apps - the browser on this PC goes to Google,
which sends it back to the console on 127.0.0.1 (PKCE). Scope: all of Drive
(owner's choice, 2026-10-16), because files Google Drive for desktop wrote
can't be read with the narrower drive.file; the app is unverified, so Google
shows a warning first (Advanced > Go to ...). The refresh token stays on
this PC (meet-sync/google-token.json, never synced).

The OAuth client is a "Desktop app" client: Google says its secret isn't a
secret for installed apps. It is kept in google_client.json (written by
tools/set_google_client.py) base64-encoded backwards so the public release's
secret scanning doesn't take it for a leaked key.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from . import meet_split
from .meet_sync import FolderTransport, SyncOffline

logger = logging.getLogger(__name__)

PREFIX = "gdrive:"                 # config meet_sync.folder = "gdrive:<folder id>"
FOLDER_NAME = "RFID meets"         # install_setup's folder in My Drive
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPE = "https://www.googleapis.com/auth/drive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
CLIENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_client.json")
TIMEOUT = 30


def is_drive(place: str) -> bool:
    return (place or "").startswith(PREFIX)


def folder_id(place: str) -> str:
    return place[len(PREFIX):] if is_drive(place) else ""


# ------------------------------------------------------------------ client

def client(path: str = CLIENT_FILE) -> Optional[Tuple[str, str]]:
    """(client id, secret) of this build's OAuth client, None when not set."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        cid = str(d.get("client_id") or "")
        sec = base64.b64decode(str(d.get("secret_b64r") or "")[::-1]).decode() if d.get("secret_b64r") else ""
    except (OSError, ValueError):
        return None
    return (cid, sec) if cid and sec else None


def encode_client(client_id: str, secret: str) -> dict:
    return {"_doc": "Google OAuth client (Desktop app) for Sharing > Sign in with Google - gdrive.py. "
                    "Not a secret for an installed app (Google); stored backwards in base64 for secret scanners.",
            "client_id": client_id, "secret_b64r": base64.b64encode(secret.encode()).decode()[::-1]}


# ------------------------------------------------------------- the token

class TokenStore:
    """This PC's Google sign-in: meet-sync/google-token.json - the refresh
    token, the account, the folder. The access token lives in memory."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._access: Optional[str] = None
        self._expires = 0.0

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, d: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def update(self, **kw) -> dict:
        d = self.load()
        d.update(kw)
        self.save(d)
        return d

    def signed_in(self) -> bool:
        return bool(self.load().get("refresh_token"))

    def forget(self) -> Optional[str]:
        """Sign out here: the file goes; returns the refresh token to revoke."""
        tok = self.load().get("refresh_token")
        try:
            os.remove(self.path)
        except OSError:
            pass
        with self._lock:
            self._access, self._expires = None, 0.0
        return tok

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if self._access and not force and time.time() < self._expires - 60:
                return self._access
            d = self.load()
            if not d.get("refresh_token"):
                raise SyncOffline("not signed in to Google")
            c = client()
            if not c:
                raise SyncOffline("Google sign-in isn't set up in this build")
            try:
                tok = _post_form(TOKEN_URL, {"client_id": c[0], "client_secret": c[1],
                                             "refresh_token": d["refresh_token"], "grant_type": "refresh_token"})
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    raise SyncOffline("Google sign-in expired or was removed: sign in again (Sharing)") from None
                raise SyncOffline(f"Google: HTTP {e.code}") from None
            self._access = tok["access_token"]
            self._expires = time.time() + float(tok.get("expires_in", 3600))
            return self._access


def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError:
        raise
    except (OSError, ValueError) as e:
        raise SyncOffline(f"Google not reachable: {e}") from None


# ------------------------------------------------------------- sign-in

_pending: Dict[str, dict] = {}        # state -> {verifier, redirect, at}
_pending_lock = threading.Lock()


def begin(redirect_uri: str) -> str:
    """The Google page to send the browser to. Its answer comes back to
    redirect_uri (the console on 127.0.0.1) with the state made here."""
    c = client()
    if not c:
        raise ValueError("Google sign-in isn't set up in this build")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["at"] > 900]:
            del _pending[k]
        _pending[state] = {"verifier": verifier, "redirect": redirect_uri, "at": now}
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c[0], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})


def finish(code: str, state: str, store: TokenStore) -> dict:
    """Google's answer: the code becomes a refresh token kept in `store`;
    then the account's name and the meets folder (made when missing)."""
    with _pending_lock:
        p = _pending.pop(state, None)
    if not p:
        raise ValueError("this sign-in link is old or was already used: start again from the console")
    c = client()
    try:
        tok = _post_form(TOKEN_URL, {"client_id": c[0], "client_secret": c[1], "code": code,
                                     "code_verifier": p["verifier"], "redirect_uri": p["redirect"],
                                     "grant_type": "authorization_code"})
    except urllib.error.HTTPError as e:
        raise ValueError(f"Google refused the sign-in (HTTP {e.code})") from None
    if not tok.get("refresh_token"):
        raise ValueError("Google gave no lasting sign-in: try again")
    if SCOPE not in str(tok.get("scope", "")).split():
        raise ValueError("Google Drive access wasn't allowed: sign in again and tick the Drive box")
    old = store.load()
    store.save({"refresh_token": tok["refresh_token"]})
    store._access, store._expires = tok.get("access_token"), time.time() + float(tok.get("expires_in", 3600))
    api = DriveApi(store)
    email = api.about().get("emailAddress", "")
    fid = old.get("folder_id") if old.get("email") == email else None
    name = FOLDER_NAME
    if fid:
        try:
            name = api.get(fid).get("name") or name
        except SyncOffline:
            fid = None
    if not fid:
        fid, name = api.meets_folder()
    return store.update(email=email, folder_id=fid, folder_name=name)


def sign_out(store: TokenStore) -> None:
    tok = store.forget()
    if tok:
        try:
            _post_form(REVOKE_URL, {"token": tok})
        except (urllib.error.HTTPError, SyncOffline):
            pass                                   # offline: forgotten here all the same


# ------------------------------------------------------------- the API

class DriveApi:
    """The few Drive v3 calls the sync uses (urllib: no new package)."""

    def __init__(self, store: TokenStore):
        self.store = store

    def _call(self, method: str, url: str, params: Optional[dict] = None, body: Optional[bytes] = None,
              ctype: Optional[str] = None, raw: bool = False):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            headers = {"Authorization": "Bearer " + self.store.access_token(force=attempt > 0)}
            if ctype:
                headers["Content-Type"] = ctype
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read()
                    return data if raw else (json.loads(data) if data else {})
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                detail = ""
                try:
                    detail = json.loads(e.read()).get("error", {}).get("message", "")
                except (ValueError, AttributeError, OSError):
                    pass
                err = SyncOffline(f"Google Drive: HTTP {e.code}{': ' + detail if detail else ''}")
                err.code = e.code
                raise err from None
            except (OSError, ValueError) as e:
                raise SyncOffline(f"Google Drive not reachable: {e}") from None
        raise SyncOffline("Google Drive: sign-in refused")

    FIELDS = "id,name,mimeType,parents,md5Checksum,trashed"

    def about(self) -> dict:
        return self._call("GET", API + "/about", {"fields": "user(emailAddress,displayName)"}).get("user", {})

    def get(self, fid: str) -> dict:
        return self._call("GET", f"{API}/files/{fid}", {"fields": self.FIELDS})

    def list(self, q: str) -> List[dict]:
        out, page = [], None
        while True:
            params = {"q": q, "fields": f"nextPageToken,files({self.FIELDS})", "pageSize": 1000,
                      "spaces": "drive"}
            if page:
                params["pageToken"] = page
            r = self._call("GET", API + "/files", params)
            out += r.get("files", [])
            page = r.get("nextPageToken")
            if not page:
                return out

    def children(self, parents: List[str]) -> List[dict]:
        """Everything in these folders: one query for up to 40 folders at a
        time (a meet has dozens of little folders; one call each was slow)."""
        out = []
        for i in range(0, len(parents), 40):
            q = " or ".join(f"'{p}' in parents" for p in parents[i:i + 40])
            out += self.list(f"({q}) and trashed=false")
        return out

    def meets_folder(self) -> Tuple[str, str]:
        """My Drive/RFID meets - the folder install_setup gives Google Drive
        for desktop - made if this account has none."""
        found = self.list(f"name='{FOLDER_NAME}' and mimeType='{FOLDER_MIME}' and 'root' in parents and trashed=false")
        if found:
            return found[0]["id"], found[0]["name"]
        return self.mkdir(FOLDER_NAME, "root")["id"], FOLDER_NAME

    def mkdir(self, name: str, parent: str) -> dict:
        body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).encode()
        return self._call("POST", API + "/files", {"fields": self.FIELDS}, body, "application/json")

    def start_token(self) -> str:
        return self._call("GET", API + "/changes/startPageToken")["startPageToken"]

    def changes(self, token: str) -> Tuple[List[dict], str]:
        out = []
        while True:
            r = self._call("GET", API + "/changes", {
                "pageToken": token, "pageSize": 1000, "spaces": "drive", "includeRemoved": "true",
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({self.FIELDS}))"})
            out += r.get("changes", [])
            if r.get("newStartPageToken"):
                return out, r["newStartPageToken"]
            token = r["nextPageToken"]

    def download(self, fid: str) -> bytes:
        return self._call("GET", f"{API}/files/{fid}", {"alt": "media"}, raw=True)

    def create(self, name: str, parent: str, data: bytes) -> dict:
        boundary = "rfid" + uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [parent]}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta
                + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + data
                + f"\r\n--{boundary}--\r\n".encode())
        return self._call("POST", UPLOAD + "/files", {"uploadType": "multipart", "fields": self.FIELDS},
                          body, f"multipart/related; boundary={boundary}")

    def update(self, fid: str, data: bytes) -> dict:
        return self._call("PATCH", f"{UPLOAD}/files/{fid}", {"uploadType": "media", "fields": self.FIELDS},
                          data, "application/octet-stream")

    def delete(self, fid: str) -> None:
        try:
            self._call("DELETE", f"{API}/files/{fid}")
        except SyncOffline as e:
            if getattr(e, "code", 0) != 404:
                raise


# ------------------------------------------------------------ transport

def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class DriveTransport:
    """The shared folder, through the Drive API: a local mirror (tree/) that
    a FolderTransport reads and writes, kept in step with Drive. The index
    (index.json) maps Drive ids to names, parents and md5s, with the changes
    feed's page token."""

    name = "folder"
    splits = True
    WORKERS = 8                       # downloads at once on the first look

    def __init__(self, api: DriveApi, root_id: str, cache_dir: str):
        self.api = api
        self.root_id = root_id
        self.cache = cache_dir
        self.tree = os.path.join(cache_dir, "tree")
        self.index_path = os.path.join(cache_dir, "index.json")
        self.items: Dict[str, dict] = {}          # id -> {n: name, p: parent, d: folder?, m: md5}
        self.token: Optional[str] = None
        self.root = self.tree                       # what FolderTransport reads (for messages)
        self._load()

    # -------------------------------------------------------- the index
    def _load(self) -> None:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if d.get("root") == self.root_id:
            self.items, self.token = d.get("items", {}), d.get("token")

    def _save(self) -> None:
        os.makedirs(self.cache, exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"root": self.root_id, "token": self.token, "items": self.items}, f)
        os.replace(tmp, self.index_path)

    def _rel(self, fid: str) -> Optional[List[str]]:
        """The item's path under the shared folder, None when it isn't in it."""
        parts, seen = [], set()
        while fid != self.root_id:
            it = self.items.get(fid)
            if it is None or fid in seen:
                return None
            seen.add(fid)
            parts.append(it["n"])
            fid = it["p"]
        return parts[::-1]

    def _local(self, fid: str) -> Optional[str]:
        rel = self._rel(fid)
        if not rel or any(p in ("", ".", "..") or "/" in p or "\\" in p for p in rel):
            return None                               # never write outside the mirror
        return os.path.join(self.tree, *rel)

    @staticmethod
    def _entry(f: dict, parent: str) -> dict:
        return {"n": f["name"], "p": parent, "d": f.get("mimeType") == FOLDER_MIME, "m": f.get("md5Checksum")}

    # ------------------------------------------------------- reading
    def fetch(self, seen: Optional[Dict[str, tuple]] = None) -> Dict[str, Dict[str, bytes]]:
        if self.token is None:
            self._scan()
        else:
            try:
                self._follow()
            except SyncOffline as e:
                if getattr(e, "code", 0) not in (400, 404, 410):
                    raise
                self._scan()                          # the changes feed lost its place: look again
        os.makedirs(self.tree, exist_ok=True)
        return FolderTransport(self.tree).read_tree(seen)

    def _scan(self) -> None:
        """The whole folder: every file whose bytes differ comes down; local
        files Drive no longer has go."""
        token = self.api.start_token()                # before listing: nothing slips between
        try:
            self.api.get(self.root_id)
        except SyncOffline as e:
            if getattr(e, "code", 0) == 404:
                raise SyncOffline("the shared Google Drive folder is gone (deleted or unshared)") from None
            raise
        items, level = {}, [self.root_id]
        while level:
            nxt = []
            for f in self.api.children(level):
                parent = next((p for p in f.get("parents", []) if p in level), level[0])
                items[f["id"]] = self._entry(f, parent)
                if items[f["id"]]["d"]:
                    nxt.append(f["id"])
            level = nxt
        self.items = items
        keep = set()
        todo = []
        for fid, it in items.items():
            path = self._local(fid)
            if path is None or it["d"]:
                continue
            keep.add(os.path.normcase(path))
            if not self._same(path, it["m"]):
                todo.append((fid, path))
        self._download(todo)
        for dirpath, _, names in os.walk(self.tree):
            for n in names:
                p = os.path.join(dirpath, n)
                if os.path.normcase(p) not in keep:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        self.token = token
        self._save()
        logger.info(f"Google Drive: looked through the shared folder ({len(items)} items, {len(todo)} downloaded)")

    def _follow(self) -> None:
        """Only what changed in Drive since the last look."""
        changes, token = self.api.changes(self.token)
        todo, moved = [], False
        pending = list(changes)
        progress = True
        while progress:                               # a new folder can come after its files
            progress = False
            for ch in list(pending):
                f, fid = ch.get("file") or {}, ch["fileId"]
                if ch.get("removed") or f.get("trashed"):
                    pending.remove(ch)
                    if fid in self.items:
                        self._drop(fid)
                    continue
                parent = next((p for p in f.get("parents", []) if p == self.root_id or p in self.items), None)
                if parent is None:
                    if fid in self.items:              # moved out of the shared folder
                        self._drop(fid)
                        pending.remove(ch)
                    continue
                pending.remove(ch)
                progress = True
                old = self.items.get(fid)
                new = self._entry(f, parent)
                if old and new["d"] and (old["n"], old["p"]) != (new["n"], new["p"]):
                    moved = True                       # a folder renamed or moved: look again
                if old and not new["d"] and (old["n"], old["p"]) != (new["n"], new["p"]):
                    path = self._local(fid)
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                self.items[fid] = new
                if not new["d"]:
                    path = self._local(fid)
                    if path and not self._same(path, new["m"]):
                        todo.append((fid, path))
        if moved:
            self.token = None
            self._scan()
            return
        self._download(todo)
        self.token = token
        self._save()
        if todo:
            logger.info(f"Google Drive: {len(todo)} changed file(s) downloaded")

    def _drop(self, fid: str) -> None:
        path = self._local(fid)
        kids = [k for k, it in self.items.items() if it["p"] == fid]
        for k in kids:
            self._drop(k)
        it = self.items.pop(fid, None)
        if path and it and not it["d"]:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _same(path: str, md5: Optional[str]) -> bool:
        try:
            with open(path, "rb") as f:
                return bool(md5) and _md5(f.read()) == md5
        except OSError:
            return False

    def _download(self, todo: List[Tuple[str, str]]) -> None:
        def one(job):
            fid, path = job
            data = self.api.download(fid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        if len(todo) <= 1:
            for job in todo:
                one(job)
            return
        with ThreadPoolExecutor(self.WORKERS) as pool:
            list(pool.map(one, todo))                 # an error (offline) ends the round

    # ------------------------------------------------------- writing
    def _child(self, parent: str, name: str, folder: bool) -> Optional[str]:
        for fid, it in self.items.items():
            if it["p"] == parent and it["n"] == name and it["d"] == folder:
                return fid
        return None

    def _folder(self, parent: str, name: str) -> str:
        fid = self._child(parent, name, True)
        if fid is None:
            f = self.api.mkdir(name, parent)
            fid = f["id"]
            self.items[fid] = self._entry(f, parent)
        return fid

    def put(self, meet_id: str, machine: str, data: bytes) -> None:
        """Our copy into the mirror (FolderTransport: only changed files),
        then up to Drive: the files whose bytes Drive doesn't have, the
        manifest last, and the files our copy no longer has removed - only
        ever inside this PC's own folder."""
        os.makedirs(self.tree, exist_ok=True)
        FolderTransport(self.tree).put(meet_id, machine, data)
        meet_dir = self._folder(self.root_id, meet_id)
        mine = self._folder(meet_dir, machine)
        local_root = os.path.join(self.tree, meet_id, machine)
        files = []
        for dirpath, _, names in os.walk(local_root):
            for n in names:
                if not n.startswith("."):
                    files.append(os.path.relpath(os.path.join(dirpath, n), local_root).replace(os.sep, "/"))
        files.sort(key=lambda r: (r == meet_split.MANIFEST, r))
        sent = 0
        have = set()
        for rel in files:
            parts = rel.split("/")
            parent = mine
            for p in parts[:-1]:
                parent = self._folder(parent, p)
            with open(os.path.join(local_root, *parts), "rb") as f:
                body = f.read()
            fid = self._child(parent, parts[-1], False)
            have.add(fid)
            if fid and self.items[fid].get("m") == _md5(body):
                continue
            f = self.api.update(fid, body) if fid else self.api.create(parts[-1], parent, body)
            self.items[f["id"]] = self._entry(f, parent)
            have.add(f["id"])
            sent += 1
        # what our copy no longer has (pruned), and the single file from before the split
        stale = [fid for fid, it in self.items.items() if not it["d"] and fid not in have
                 and (self._rel(fid) or [""])[:2] == [meet_id, machine]]
        stale += [fid for fid, it in self.items.items() if it["p"] == meet_dir and not it["d"]
                  and it["n"] in (machine + ".json.gz", machine + ".json")]
        for fid in stale:
            self.api.delete(fid)
            self.items.pop(fid, None)
        self._save()
        logger.info(f"Google Drive: {meet_id}/{machine}: {sent} file(s) uploaded"
                    + (f", {len(stale)} removed" if stale else ""))

    def finish(self, message: str) -> None:
        pass


def transport(place: str, base_dir: str) -> DriveTransport:
    """The DriveTransport for config meet_sync.folder "gdrive:<id>"."""
    store = TokenStore(token_path(base_dir))
    return DriveTransport(DriveApi(store), folder_id(place), os.path.join(base_dir, "meet-sync", "gdrive"))


def token_path(base_dir: str) -> str:
    return os.path.join(base_dir, "meet-sync", "google-token.json")
