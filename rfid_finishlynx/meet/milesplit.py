"""MileSplit client: login + token cache, entries/rosters pulls, results upload,
and MileSplit Live (payload builders + a debounced pusher).

Ported from ~/milesplit-probe (probe.py, upload_test.py, live_upload.py,
mslive_push.py), field-tested at Woodmont 2026-10-03 (MSLIVE.md, MAPPING.md).
Two separate MileSplit services, and pushing to one does nothing to the other:

- v3 API (api.prod.milesplit.com/int/v3): login, meet/entries/roster reads,
  and the mpResults results upload (the official results page). The upload
  answers HTTP 201 even when it refuses the document; the body's "status"
  ("Success" / "error") and "statusMessage" are what count.
- MileSplit Live (a Google Cloud Function): per-runner entries keyed by
  bib, per-race status, per-race team scores. PATCH upserts only. Every
  DELETE route wipes the WHOLE meet's collection (MeetPro does it on Live
  start-up), so this client refuses DELETE outright (2026-10-03).

Every request goes through an injectable transport, a callable
    transport(method, url, headers, body) -> (status, parsed json | text)
(body is the Python object to send as JSON, or None). The default uses
urllib; tests always pass a fake, so nothing here touches the network in
tests. The 30-day token is cached per machine (default
%LOCALAPPDATA%\\rfid_finishlynx\\milesplit-token.json), never in the repo and
never with the password.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import importers
from .exports import finisher_rows, hundredths, int_id, is_official, is_scored
from .model import Meet, Race

logger = logging.getLogger(__name__)

API_ROOT = "https://api.prod.milesplit.com/int/v3/"
LIVE_ROOT = "https://us-central1-milesplit-live-results.cloudfunctions.net/api"
USER_AGENT = "rfid-meet/1"
TIMEOUT_S = 60                   # a 2,472-entry meet took ~13 s to come back (MAPPING.md)
TOKEN_LIFETIME = dt.timedelta(days=30)
TOKEN_MARGIN = dt.timedelta(hours=1)   # treat as expired a little early, never mid-push
LIVE_BATCH = 495                 # MeetPro's entries batch size
LOGO = "https://assets.sp.milesplit.com/team_logos/{id}?v={epoch}"

# Scheduled start times on MileSplit Live (2026-10-03, user's ask: "include
# this on a live results push so that spectators/athletes can see when a
# race is expected to start"). See live_schedule_payload.
#
# UNVERIFIED wire format (2026-10-03): confirm against milesplit.live before
# relying on it. What we were told: each race on Live can carry a session
# with a plain-text time ("8:00 AM") that Live shows exactly as sent, only
# while the race status reads "Upcoming" (In Progress / Completed replace
# it), and only when the meet's "showStartTimes" setting is "visible"
# (Woodmont's was). No sample we captured (MSLIVE.md, data/mslive-*) carries
# a session, so the field path below is our best guess: one place to change
# it once a real MeetPro push is captured, and LIVE_SEND_START_TIMES = False
# switches the whole thing off without touching anything else.
LIVE_SEND_START_TIMES = True
LIVE_START_TIME_ROUND = "F"                      # XC uses round F only (MSLIVE.md)
LIVE_START_TIME_PATH = ("rounds", LIVE_START_TIME_ROUND, "session", "time")

Transport = Callable[[str, str, Dict[str, str], object], Tuple[int, object]]


class MileSplitError(Exception):
    pass


class AuthError(MileSplitError):
    """Not logged in, token older than 30 days, or the token was rejected."""


class ApiError(MileSplitError):
    def __init__(self, status: int, body):
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


def default_token_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "rfid_finishlynx", "milesplit-token.json")


def default_credentials_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "rfid_finishlynx", "milesplit-login.bin")


# ---------------------------------------------------------- saved login

# "Remember me on this computer" (2026-10-03, user: no logging back in each
# time the middleware opens). The token already survives restarts (30
# days); this keeps the email + password so the client logs itself in again
# when the token runs out or MileSplit rejects it. Encrypted with Windows
# DPAPI (CryptProtectData, current user): only this Windows account on this
# PC can read it, nothing new to install. Never in config.yaml, the repo or
# the logs.

class CredentialsUnavailable(Exception):
    """Saved logins need Windows DPAPI; elsewhere remember-me is refused."""


def _dpapi(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        raise CredentialsUnavailable("saving the MileSplit login needs Windows") from None
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    entropy = b"rfid-meet milesplit"
    ebuf = ctypes.create_string_buffer(entropy, len(entropy))
    blob_ent = BLOB(len(entropy), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
            0x01, ctypes.byref(blob_out))           # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise CredentialsUnavailable("Windows could not "
                                     + ("protect" if protect else "read")
                                     + " the saved MileSplit login")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class CredentialStore:
    """The remembered MileSplit login, DPAPI-encrypted in one file.
    protect/unprotect are injectable (tests; non-Windows dev machines)."""

    def __init__(self, path: Optional[str] = None,
                 protect: Optional[Callable[[bytes], bytes]] = None,
                 unprotect: Optional[Callable[[bytes], bytes]] = None):
        self.path = path or default_credentials_path()
        self._protect = protect or (lambda b: _dpapi(True, b))
        self._unprotect = unprotect or (lambda b: _dpapi(False, b))
        self._lock = threading.Lock()

    def save(self, email: str, password: str) -> None:
        blob = self._protect(json.dumps({"email": email, "password": password}).encode("utf-8"))
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, self.path)

    def load(self) -> Optional[Tuple[str, str]]:
        with self._lock:
            try:
                with open(self.path, "rb") as f:
                    blob = f.read()
            except OSError:
                return None
        try:
            d = json.loads(self._unprotect(blob).decode("utf-8"))
            return (str(d["email"]), str(d["password"])) if d.get("email") else None
        except (CredentialsUnavailable, ValueError, KeyError, TypeError):
            logger.warning("Saved MileSplit login could not be read (another Windows "
                           "user or PC?); log in again")
            return None

    def email(self) -> Optional[str]:
        got = self.load()
        return got[0] if got else None

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass


def urllib_transport(method: str, url: str, headers: Dict[str, str], body=None,
                     timeout: float = TIMEOUT_S) -> Tuple[int, object]:
    """The real transport (stdlib). Returns (HTTP status, parsed JSON or raw
    text); HTTP errors come back as a status, not an exception, because
    MileSplit puts the useful message in the body."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise MileSplitError(f"network error: {getattr(e, 'reason', e)}") from None
    # MeetPro scrubs smart quotes before parsing; so did the probe.
    raw = raw.replace("“", '"').replace("”", '"')
    try:
        return status, (json.loads(raw) if raw.strip() else None)
    except ValueError:
        return status, raw


def unwrap(payload):
    """The list/object inside an optional {"data": ...} envelope."""
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
        return payload["data"]
    return payload


@dataclass
class UploadResult:
    ok: bool
    http_status: int
    status: str = ""             # body "status": "Success" | "error"
    message: str = ""            # body "statusMessage"
    results_url: str = ""
    body: object = None


@dataclass
class LiveResponse:
    ok: bool
    http_status: int
    message: str = ""
    body: object = None


# --------------------------------------------------------------- client

class MileSplitClient:
    """Token cache + v3 API + Live calls. Thread-safe enough for one
    pusher thread plus console requests: the token cache is guarded and
    every call is independent."""

    def __init__(self, token_path: Optional[str] = None, transport: Optional[Transport] = None,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 api_root: str = API_ROOT, live_root: str = LIVE_ROOT,
                 credentials: Optional[CredentialStore] = None):
        self.token_path = token_path or default_token_path()
        # remembered login: used to log in again by itself (CredentialStore)
        self.credentials = credentials
        self.transport = transport or urllib_transport
        self.now = now
        self.api_root = api_root
        self.live_root = live_root
        self._lock = threading.Lock()

    # ---------------------------------------------------------- token

    def _read_cache(self) -> Optional[dict]:
        try:
            with open(self.token_path, encoding="utf-8") as f:
                d = json.load(f)
            dt.datetime.fromisoformat(d["acquired"])
            return d if d.get("token30") else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def token_expires_at(self) -> Optional[dt.datetime]:
        d = self._read_cache()
        return dt.datetime.fromisoformat(d["acquired"]) + TOKEN_LIFETIME if d else None

    def logged_in(self) -> bool:
        exp = self.token_expires_at()
        return bool(exp and self.now() < exp - TOKEN_MARGIN)

    def remembered(self) -> Optional[str]:
        """The email of the saved login, if any."""
        return self.credentials.email() if self.credentials is not None else None

    def _relogin(self) -> bool:
        """Log in again with the saved login; False when there is none or
        MileSplit refuses it (then the saved login is wrong: forget it)."""
        creds = self.credentials.load() if self.credentials is not None else None
        if not creds:
            return False
        try:
            self.login(*creds)
            logger.info("MileSplit: logged in again with the saved login")
            return True
        except AuthError:
            logger.warning("MileSplit refused the saved login; it was forgotten")
            self.credentials.clear()
            return False

    def token(self) -> str:
        with self._lock:
            d = self._read_cache()
        fresh = bool(d) and self.now() < (dt.datetime.fromisoformat(d["acquired"])
                                          + TOKEN_LIFETIME - TOKEN_MARGIN)
        if not fresh and self._relogin():
            with self._lock:
                d = self._read_cache()
            fresh = bool(d)
        if not d:
            raise AuthError("not logged in to MileSplit")
        if not fresh:
            raise AuthError("MileSplit login is older than 30 days; log in again")
        return d["token30"]

    def login(self, email: str, password: str, remember: Optional[bool] = None) -> dt.datetime:
        """POST auth/login and cache data.token30; returns when it expires.
        Only the token and the time it was issued go in the token file.
        remember=True also saves the login (CredentialStore, encrypted) so
        the client logs itself in again; False forgets a saved one; None
        leaves it as it is."""
        status, body = self.transport("POST", self.api_root + "auth/login", self._headers(None),
                                      {"email": email, "password": password})
        token = None
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            token = body["data"].get("token30")
        if status >= 300 or not token:
            msg = body.get("message") if isinstance(body, dict) else None
            raise AuthError(f"MileSplit login failed (HTTP {status}{': ' + str(msg) if msg else ''})")
        acquired = self.now()
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.token_path)), exist_ok=True)
            tmp = self.token_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"token30": token, "acquired": acquired.isoformat()}, f)
            os.replace(tmp, self.token_path)
        if remember is not None:
            if self.credentials is None:
                if remember:
                    raise CredentialsUnavailable("no place to save the MileSplit login")
            elif remember:
                self.credentials.save(email, password)
            else:
                self.credentials.clear()
        return acquired + TOKEN_LIFETIME

    def logout(self, forget: bool = False) -> None:
        """Drop the token; forget=True also deletes the saved login."""
        with self._lock:
            try:
                os.remove(self.token_path)
            except FileNotFoundError:
                pass
        if forget and self.credentials is not None:
            self.credentials.clear()

    # ---------------------------------------------------------- HTTP

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json",
             "User-Agent": USER_AGENT}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _call(self, method: str, url: str, body=None) -> Tuple[int, object]:
        status, parsed = self.transport(method, url, self._headers(self.token()), body)
        if status == 401:
            # A rejected token is useless; drop it. With a saved login, log
            # in again and retry once; else the console asks for a login.
            self.logout()
            if self._relogin():
                status, parsed = self.transport(method, url, self._headers(self.token()), body)
                if status != 401:
                    return status, parsed
                self.logout()
            raise AuthError("MileSplit rejected the login token; log in again")
        return status, parsed

    def _get(self, route: str):
        status, parsed = self._call("GET", self.api_root + route)
        if status >= 300:
            raise ApiError(status, parsed)
        return unwrap(parsed)

    # ---------------------------------------------------------- v3 reads

    def companies(self) -> list:
        return self._get("timing-company/myall") or []

    def meets(self, tc: int, month: int, year: int) -> list:
        q = urllib.parse.urlencode({"month": int(month), "year": int(year)})
        return self._get(f"timing-company/{int(tc)}/meets?{q}") or []

    def entries(self, tc: int, meet: int) -> list:
        """The meet's entries: list of teams with rosters (MEET-RESEARCH.md §2).
        No pagination: a 2,472-entry meet came back in one response."""
        return self._get(f"timing-company/{int(tc)}/meets/{int(meet)}/entries") or []

    def attending(self, meet: int) -> list:
        return self._get(f"team-schedule/attending/{int(meet)}") or []

    # 2026-10-08, Bob Jenkins (97 teams): all teams in one request -> HTTP 500;
    # 50 or 25 per request silently returned fewer athletes (12406 / 12437
    # vs 12445 at 10, the same every time). A few teams per request.
    ROSTER_BATCH = 5
    RETRY_429 = (2.0, 5.0, 15.0)              # MileSplit's rate limit: wait, then again

    def team_rosters(self, tc: int, meet: int, team_ids: Iterable[int],
                     sleep: Callable[[float], None] = time.sleep):
        ids = [int(t) for t in team_ids]
        teams: list = []
        for i in range(0, len(ids), self.ROSTER_BATCH):
            route = (f"timing-company/{int(tc)}/meets/{int(meet)}/team-rosters?teamIds="
                     + ",".join(map(str, ids[i:i + self.ROSTER_BATCH])))
            for wait in self.RETRY_429 + (None,):
                try:
                    teams += importers.roster_teams(self._get(route))
                    break
                except ApiError as e:
                    if e.status != 429 or wait is None:
                        raise
                    sleep(wait)
        return teams

    # ---------------------------------------------------------- upload

    def upload_results(self, tc: int, meet: int, payload: dict) -> UploadResult:
        """POST the mpResults document. Re-uploading replaces the race on the
        results page (Woodmont). MileSplit answers 201 even on failure, so the
        body decides: ok only when HTTP < 300 AND status isn't "error"."""
        if int(payload.get("meet_id") or 0) != int(meet):
            raise ValueError(f"payload is for meet {payload.get('meet_id')}, not {meet}")
        body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        status, parsed = self._call(
            "POST", self.api_root + f"timing-company/{int(tc)}/meets/{int(meet)}/mpResults", body)
        if isinstance(parsed, dict):
            st = str(parsed.get("status") or "")
            msg = str(parsed.get("statusMessage") or parsed.get("message") or "")
            url = str(parsed.get("resultsURL") or "")
        else:
            st, msg, url = "", str(parsed or "")[:300], ""
        ok = status < 300 and isinstance(parsed, dict) and st.lower() != "error"
        if not ok and not msg:
            msg = f"HTTP {status}"
        return UploadResult(ok, status, st, msg, url if ok else "", parsed)

    # ---------------------------------------------------------- Live

    def live(self, method: str, path: str, body=None) -> LiveResponse:
        """One Live call. DELETE is refused: every DELETE route clears the
        whole meet's collection on MileSplit Live (MSLIVE.md)."""
        method = method.upper()
        if method not in ("GET", "PATCH"):
            raise MileSplitError(f"refusing {method} {path}: MileSplit Live is PATCH/GET only")
        status, parsed = self._call(method, self.live_root + path, body)
        ok = status < 300 and isinstance(parsed, dict) and not parsed.get("err")
        msg = ""
        if isinstance(parsed, dict):
            err = parsed.get("error")
            msg = str(parsed.get("message") or (err.get("value") if isinstance(err, dict) else err) or "")
        elif parsed:
            msg = str(parsed)[:200]
        return LiveResponse(ok, status, msg, parsed)

    def live_get(self, path: str):
        r = self.live("GET", path)
        if not r.ok:
            raise ApiError(r.http_status, r.body)
        return unwrap(r.body)

    def live_entries(self, meet: int, entries: List[dict]) -> List[LiveResponse]:
        return [self.live("PATCH", f"/meets/{int(meet)}/entries", entries[i:i + LIVE_BATCH])
                for i in range(0, len(entries), LIVE_BATCH)]

    def live_events(self, meet: int, events: List[dict]) -> LiveResponse:
        return self.live("PATCH", f"/meets/{int(meet)}/events", events)

    def live_team_scores(self, meet: int, groups: List[dict]) -> LiveResponse:
        return self.live("PATCH", f"/meets/{int(meet)}/teamScores", groups)


# --------------------------------------------------------------- Live payloads

def perf_text(sec: float) -> str:
    """Live's result text: 17:32.94, 1:02:03.45."""
    cs = round(sec * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s = f"{rem // 100:02d}.{rem % 100:02d}"
    return f"{h}:{m:02d}:{s}" if h else f"{m}:{s}"


def logo(ms_team_id, epoch) -> str:
    """Team logo URL; only for a real MileSplit team id. The cache-buster is
    fixed per pusher: a fresh one per rebuild made every rebuild look new and
    nothing settled at Woodmont (MSLIVE.md lesson 2)."""
    return LOGO.format(id=ms_team_id, epoch=epoch) if ms_team_id and ms_team_id > 1 else ""


def _gender_obj(g: str) -> dict:
    sex = (g or "x").upper()
    return {"sex": sex, "name": {"M": "Men", "F": "Women"}.get(sex, "Mixed")}


def _ms_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def race_live_status(race: Race, results: Optional[dict]) -> Optional[str]:
    """pending -> live (first finisher) -> complete (race final, frozen)."""
    if is_official(race):
        return "complete"
    if results and any((f.get("status") or "finished") == "finished"
                       for f in results.get("finishers", [])):
        return "live"
    return None


def live_race_payload(meet: Meet, race: Race, results: Optional[dict] = None,
                      status: Optional[str] = None, epoch: int = 0) -> dict:
    """Everything MileSplit Live needs for one physical race, shaped as
    MeetPro 2.3.139 sends it (verified field-for-field against MeetPro's own
    pushes at Woodmont).

    results: the race's results in the frozen shape; default race.results.
    Pass provisional (unfrozen) results while the race is running.
    One race can carry several scoring events (combined JV start); Live is
    per event, so entries carry their scoring event and there is one status
    and one team-score group per event. place = the place in the scoring
    event. Returns {"race": key, "events": [status patches], "entries":
    [finishers, keyed by bib], "teamScores": [groups], "withdrawn": {bib:
    status} for entered runners not (or no longer) finished}."""
    res = results if results is not None else race.results
    status = status if status is not None else race_live_status(race, res)
    rows = finisher_rows(meet, race, res) if res else []
    entries: List[dict] = []
    withdrawn: Dict[int, str] = {}
    by_event: Dict[int, list] = {}
    for f, a, e, ev_num in rows:
        bib = (a.bib if a else str(f.get("bib") or "")).strip()
        if a is None or not bib.isdigit():
            continue          # MeetPro's sync skips rows with no bib or team too
        st = f.get("status") or "finished"
        if st != "finished" or f.get("elapsed") is None:
            withdrawn[int(bib)] = st if st in ("DNF", "DNS", "DQ") else "DNF"
            continue
        t = meet.team(a.team_id)
        tid = _ms_int(t.milesplit_id) if t else 0
        exh = bool(e and e.status == "exhibition")
        units = hundredths(f["elapsed"])
        place = f.get("event_place") or f.get("place")
        entries.append({
            "id": int(bib), "event": ev_num, "round": "F", "heat": 1, "exhibition": exh,
            "athlete": {
                "id": int(bib), "firstName": a.first, "lastName": a.last,
                "teamId": tid, "mileSplitId": _ms_int(a.milesplit_id) if a.ms_match not in
                ("pending", "declined") else 0,
                "sex": a.gender.lower(), "grade": meet.grade(a) or None,
                "team": {"abbreviation": (t.code if t else "") or "UNAT",
                         "name": t.name if t else "Unattached", "number": tid,
                         "division": {"number": 0, "name": None}, "logoUrl": logo(tid, epoch)},
                "compNo": int(bib), "exhibition": "EXH" if exh else None},
            "result": {"place": int(place) if place else 0, "units": units,
                       "text": perf_text(units),
                       "teamPoints": int(f["points"]) if is_scored(f) and not exh else 0,
                       "isHand": False},
        })
        by_event.setdefault(ev_num, []).append((f, a, e, t, units))

    events_in_race = sorted({n for _f, _a, _e, n in rows} | {e.event for e in race.entries})
    events = [{"number": n, "rounds": {"F": {"status": status}}} for n in events_in_race] \
        if status else []
    groups = [g for g in (_team_group(meet, n, by_event.get(n, []), (res or {}).get("teams", {}),
                                      epoch) for n in events_in_race) if g]
    return {"race": list(race.key), "events": events, "entries": entries,
            "teamScores": groups, "withdrawn": withdrawn}


def _team_group(meet: Meet, ev_num: int, rows, team_rows: dict, epoch: int) -> Optional[dict]:
    """One teamScores group: every finisher of a scoring team in finish
    order; scorers and displacers carry their team-scoring place as place
    and points, the rest 0/0. A team short of a full score is listed with
    place 0, points 0 (MeetPro). Non-scoring teams, unattached runners and
    exhibition runners are left out: they neither score nor displace."""
    ev = meet.event(ev_num)
    if ev is None or not rows:
        return None
    ranks = {r.get("team_id"): r for r in team_rows.get(str(ev_num), []) or []}
    scores: Dict[str, dict] = {}
    for f, a, e, t, units in rows:
        if t is None or not t.score or a.unattached or (e and e.status == "exhibition"):
            continue
        key = str(int_id(t.id))
        if key not in scores:
            row = ranks.get(t.id) or {}
            done = bool(row.get("complete"))
            ms = _ms_int(t.milesplit_id) or None
            scores[key] = {"team": {"id": int_id(t.id), "mileSplitId": ms, "name": t.name,
                                    "logoUrl": logo(ms or 0, epoch), "abbreviation": t.code,
                                    "divisionNumber": 0, "divisionName": None},
                           "place": int(row.get("rank") or 0) if done else 0,
                           "points": int(row.get("score") or 0) if done else 0,
                           "entries": []}
        scored = is_scored(f)
        pts = int(f["points"]) if scored else 0
        scorer = bool(scored and f.get("scorer"))
        scores[key]["entries"].append({
            "athlete": {"firstName": a.first, "lastName": a.last, "grade": meet.grade(a) or None,
                        "id": int_id(a.id), "mileSplitId": 0, "sex": a.gender.lower()},
            "team": {"abbreviation": t.code, "name": t.name, "division": {"number": 0, "name": ""},
                     "logoUrl": logo(_ms_int(t.milesplit_id), epoch), "number": int_id(t.id)},
            "place": pts, "units": units, "isDisplacer": scored and not scorer,
            "isScorer": scorer, "points": pts})
    if not scores:
        return None
    # "name" carries MeetPro's trailing space (custom name + empty division name).
    return {"name": f"{ev.name} ", "gender": _gender_obj(ev.gender), "divisionNo": 0,
            "eventNo": ev_num, "scores": scores}


def _race_results(race: Race, live_results: Optional[Callable[[Race], Optional[dict]]]):
    """Frozen results for a final race, else live_results(race) when given."""
    res = race.results
    if race.status != "final" and live_results is not None:
        res = live_results(race)
    return res


def live_payloads(meet: Meet, races: Optional[Iterable[Race]] = None,
                  live_results: Optional[Callable[[Race], Optional[dict]]] = None,
                  epoch: int = 0) -> List[dict]:
    """Payloads for several races: frozen results for final races, else
    live_results(race) (provisional, frozen_at None) when given. A reopened
    race keeps its last frozen results but isn't final, so it goes back to
    provisional results and status "live" (2026-10-03).

    Each payload also carries "schedule": live_schedule_payload() for that
    race with blanks included, so the pusher can send a start time while
    the race is upcoming and clear one the operator removed (2026-10-03)."""
    out = []
    for race in (list(races) if races is not None else meet.races_in_order()):
        res = _race_results(race, live_results)
        p = live_race_payload(meet, race, res, epoch=epoch)
        p["schedule"] = live_schedule_payload(meet, [race], live_results=lambda _r, res=res: res,
                                              include_blank=True)
        out.append(p)
    return out


def _nested(path: Tuple[str, ...], value) -> dict:
    out = value
    for key in reversed(path):
        out = {key: out}
    return out


def live_start_time(entry: dict) -> Optional[str]:
    """The start-time text inside one live_schedule_payload entry."""
    cur = entry
    for key in LIVE_START_TIME_PATH:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur if isinstance(cur, str) else None


def live_schedule_payload(meet: Meet, races: Optional[Iterable[Race]] = None,
                          live_results: Optional[Callable[[Race], Optional[dict]]] = None,
                          include_blank: bool = False) -> List[dict]:
    """PATCH /meets/<id>/events entries giving MileSplit Live each UPCOMING
    race's scheduled start time, e.g.
        [{"number": 3, "rounds": {"F": {"session": {"time": "9:30 AM"}}}}]
    one per scoring event of the race (Live is per event, like the status).

    UNVERIFIED wire format (2026-10-03): confirm against milesplit.live
    before relying on it - the field path is LIVE_START_TIME_PATH, and
    LIVE_SEND_START_TIMES = False turns this into [] everywhere.

    Upcoming = no Live status yet (race_live_status None: not final, no
    finisher in its results). A started race is left out entirely, and no
    entry here ever carries a status, so a start-time push can't turn a
    race that is running (or done) back into "Upcoming".

    races: default every race in run order. live_results(race): the
    provisional results of a race that isn't final (as live_payloads).
    include_blank: also list upcoming races with no start time, with time
    "" (the pusher uses that to clear a time it sent earlier; it never sends
    a blank it didn't need to)."""
    if not LIVE_SEND_START_TIMES:
        return []
    from .model import start_time_text
    out: List[dict] = []
    for race in (list(races) if races is not None else meet.races_in_order()):
        res = _race_results(race, live_results)
        if race_live_status(race, res) is not None:
            continue
        text = start_time_text(race)
        if not text and not include_blank:
            continue
        # The same events live_race_payload sends a status for: a race with
        # no entries isn't on Live, and an upsert must not invent an event.
        nums = sorted({e.event for e in race.entries})
        out += [{"number": n, **_nested(LIVE_START_TIME_PATH, text)} for n in nums]
    return out


def _strip(entry: dict) -> str:
    """An entry minus its logo URL, as a stable comparison key."""
    e = copy.deepcopy(entry)
    e["athlete"]["team"]["logoUrl"] = ""
    return json.dumps(e, sort_keys=True)


def _group_key(group: dict) -> str:
    g = copy.deepcopy(group)
    for s in g["scores"].values():
        s["team"]["logoUrl"] = ""
        for e in s["entries"]:
            e["team"]["logoUrl"] = ""
    return json.dumps(g, sort_keys=True)


def live_fingerprint(payloads: List[dict]) -> str:
    """Hash of everything a push would carry, minus logo cache-busters."""
    body = [{"events": p["events"], "entries": sorted(_strip(e) for e in p["entries"]),
             "teamScores": sorted(_group_key(g) for g in p["teamScores"]),
             "withdrawn": sorted(p["withdrawn"].items()),
             "schedule": p.get("schedule", [])} for p in payloads]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def withdrawn_entry(sent: dict, status: str) -> dict:
    """A runner MileSplit Live already shows as a finisher who no longer is
    one (misread bib corrected, DQ, DNF). Live has no per-runner delete, so
    the same bib is re-sent with no place and the status. NOT yet checked on
    Live (2026-10-03): result.status exists on Live's stored rows ("" for
    finishers) but nobody has sent a non-empty one; check on a test meet."""
    e = copy.deepcopy(sent)
    e["result"] = {"place": 0, "units": 0, "text": status, "teamPoints": 0, "isHand": False,
                   "status": status}
    return e


# --------------------------------------------------------------- pusher

@dataclass
class PushReport:
    ok: bool
    entries: int = 0
    withdrawn: int = 0
    statuses: List[Tuple[int, str]] = field(default_factory=list)
    team_scores: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: str = ""            # why nothing was sent ("unchanged", ...)
    # (event, time text) start times sent ("" = cleared); a refused
    # start-time push lands in start_time_error, never in errors (below).
    start_times: List[Tuple[int, str]] = field(default_factory=list)
    start_time_error: str = ""


class LivePusher:
    """Debounced MileSplit Live push.

    notify() is called on every results change (it will hook
    Application._on_races_changed) and only records the time. tick() pushes
    when due:
      - after `settle` s with no change, or at most `max_wait` s after the
        first unpushed change while results keep moving (a busy chute never
        goes quiet; Woodmont used 5 s / 15 s);
      - only what changed since the last accepted push: entries diffed per
        (event, bib), team groups and race status per event; an identical
        rebuild sends nothing;
      - one push in flight at a time; a failed push backs off (backoff_base
        doubling up to backoff_max) and retries the whole diff afterwards;
      - runners it showed as finishers who no longer are get re-sent as
        withdrawn (see withdrawn_entry), when retract is on;
      - scheduled start times of upcoming races (payload "schedule", see
        live_schedule_payload) diffed per event, sent in their own PATCH
        after the statuses. That wire format is UNVERIFIED (2026-10-03), so
        a refusal of it never fails the push or backs off results: it is
        reported (status()["start_time_error"]) and the same body isn't
        retried until the start times change.
    Thread-safe: notify() from any thread, tick() from the pusher thread
    (start()) or a test. clock is injectable (monotonic seconds).

    source() returns the race payloads to keep in sync (live_payloads()).
    """

    def __init__(self, client: MileSplitClient, meet_id: int, source: Callable[[], List[dict]],
                 settle: float = 5.0, max_wait: float = 15.0, backoff_base: float = 5.0,
                 backoff_max: float = 120.0, retract: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.meet_id = int(meet_id)
        self.source = source
        self.settle = settle
        self.max_wait = max_wait
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retract = retract
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = False
        self._first_at = 0.0
        self._changed_at = 0.0
        self._in_flight = False
        self._errors = 0
        self._backoff_until = 0.0
        self._sent_entries: Dict[Tuple[int, int], dict] = {}   # (event, bib) -> entry sent
        self._sent_groups: Dict[int, str] = {}
        self._sent_status: Dict[int, str] = {}
        self._sent_times: Dict[int, str] = {}           # event -> start time text sent
        self._times_refused: Optional[str] = None       # body Live refused last time
        self._times_error = ""
        self._last_fp: Optional[str] = None
        self._last_error = ""
        self._last_push_at: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------------------------------------------------------- triggers

    def notify(self) -> None:
        now = self.clock()
        with self._lock:
            if not self._pending:
                self._pending = True
                self._first_at = now
            self._changed_at = now

    def due(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        with self._lock:
            return self._due(now, False)

    def _due(self, now: float, force: bool) -> bool:
        if self._in_flight or not self._pending or now < self._backoff_until:
            return False
        return force or now - self._changed_at >= self.settle or now - self._first_at >= self.max_wait

    def flush(self) -> Optional[PushReport]:
        """Push now, without waiting to settle (race marked final). Still one
        in flight and still respects an error back-off."""
        self.notify()
        return self.tick(force=True)

    # ---------------------------------------------------------- push

    def tick(self, force: bool = False) -> Optional[PushReport]:
        """Push if due; None when nothing was attempted."""
        now = self.clock()
        with self._lock:
            if not self._due(now, force):
                return None
            self._in_flight = True
            self._pending = False
            first_at = self._first_at
        try:
            report = self._push()
        except AuthError as e:
            report = PushReport(False, errors=[str(e)])
        except Exception as e:            # noqa: BLE001 - never kill the pusher thread
            logger.exception("MileSplit Live push failed")
            report = PushReport(False, errors=[f"{type(e).__name__}: {e}"])
        with self._lock:
            self._in_flight = False
            done = self.clock()
            if report.ok:
                self._errors = 0
                self._backoff_until = 0.0
                self._last_error = ""
                if not report.skipped:
                    self._last_push_at = done
            else:
                self._errors += 1
                delay = min(self.backoff_base * 2 ** (self._errors - 1), self.backoff_max)
                self._backoff_until = done + delay
                self._last_error = "; ".join(report.errors)
                if not self._pending:         # retry this change once the back-off ends
                    self._pending = True
                    self._first_at = first_at
                    self._changed_at = first_at
                logger.warning("MileSplit Live push failed (retry in %.0f s): %s", delay,
                               self._last_error)
        return report

    def _plan(self, payloads: List[dict]):
        entries, sent_after = [], {}
        statuses, groups = [], []
        withdrawn = 0
        for p in payloads:
            current = set()
            for e in p["entries"]:
                k = (e["event"], e["id"])
                current.add(k)
                if k not in self._sent_entries or _strip(self._sent_entries[k]) != _strip(e):
                    entries.append(e)
                    sent_after[k] = e
            if self.retract:
                race_events = {ev["number"] for ev in p["events"]} | {e["event"] for e in p["entries"]}
                for k, old in self._sent_entries.items():
                    if k[0] in race_events and k not in current and old["result"].get("place"):
                        w = withdrawn_entry(old, p["withdrawn"].get(k[1], "DNS"))
                        entries.append(w)
                        sent_after[k] = w
                        withdrawn += 1
            for ev in p["events"]:
                st = ev["rounds"]["F"]["status"]
                if self._sent_status.get(ev["number"]) != st:
                    statuses.append(ev)
            for g in p["teamScores"]:
                if self._sent_groups.get(g["eventNo"]) != _group_key(g):
                    groups.append(g)
        return entries, sent_after, statuses, groups, withdrawn

    def _plan_times(self, payloads: List[dict]) -> List[dict]:
        """Start-time entries that differ from what Live was last sent. A
        blank goes out only to clear a time we sent: an event never given a
        time gets nothing (the field is unverified, keep its use minimal)."""
        out = []
        for p in payloads:
            for ev in p.get("schedule", []):
                text = live_start_time(ev) or ""
                if self._sent_times.get(ev["number"], "") != text:
                    out.append(ev)
        if out and json.dumps(out, sort_keys=True) == self._times_refused:
            return []
        return out

    def note_start_times(self, entries: List[dict]) -> None:
        """Start times Live accepted from a manual send (MeetService
        .send_start_times), so the pusher doesn't send them again."""
        with self._lock:
            for ev in entries:
                self._sent_times[ev["number"]] = live_start_time(ev) or ""
            self._times_refused, self._times_error = None, ""

    def _push(self) -> PushReport:
        payloads = self.source() or []
        fp = live_fingerprint(payloads)
        if fp == self._last_fp:
            return PushReport(True, skipped="unchanged")
        entries, sent_after, statuses, groups, withdrawn = self._plan(payloads)
        times = self._plan_times(payloads)
        if not (entries or statuses or groups or times):
            self._last_fp = fp
            return PushReport(True, skipped="nothing new")
        report = PushReport(True, entries=len(entries), withdrawn=withdrawn)
        # Entries first, then status, then team scores (MeetPro's order).
        ok_entries = True
        for i in range(0, len(entries), LIVE_BATCH):
            batch = entries[i:i + LIVE_BATCH]
            r = self.client.live("PATCH", f"/meets/{self.meet_id}/entries", batch)
            if r.ok:
                with self._lock:          # status() reads these from other threads
                    for e in batch:
                        self._sent_entries[(e["event"], e["id"])] = sent_after[(e["event"], e["id"])]
            else:
                ok_entries = False
                report.errors.append(f"entries: HTTP {r.http_status} {r.message}")
        if statuses:
            r = self.client.live("PATCH", f"/meets/{self.meet_id}/events", statuses)
            if r.ok:
                with self._lock:
                    for ev in statuses:
                        st = ev["rounds"]["F"]["status"]
                        self._sent_status[ev["number"]] = st
                        report.statuses.append((ev["number"], st))
            else:
                report.errors.append(f"events: HTTP {r.http_status} {r.message}")
        if groups:
            r = self.client.live("PATCH", f"/meets/{self.meet_id}/teamScores", groups)
            if r.ok:
                with self._lock:
                    for g in groups:
                        self._sent_groups[g["eventNo"]] = _group_key(g)
                        report.team_scores.append(g["eventNo"])
            else:
                report.errors.append(f"teamScores: HTTP {r.http_status} {r.message}")
        if times:
            self._push_times(times, report)
        report.ok = not report.errors
        if report.ok and ok_entries:
            self._last_fp = fp
        return report

    def _push_times(self, times: List[dict], report: PushReport) -> None:
        try:
            r = self.client.live("PATCH", f"/meets/{self.meet_id}/events", times)
            ok, why = r.ok, f"HTTP {r.http_status} {r.message}".strip()
        except AuthError:
            raise
        except Exception as e:            # noqa: BLE001 - see the class notes
            ok, why = False, f"{type(e).__name__}: {e}"
        with self._lock:
            if ok:
                for ev in times:
                    text = live_start_time(ev) or ""
                    self._sent_times[ev["number"]] = text
                    report.start_times.append((ev["number"], text))
                self._times_refused, self._times_error = None, ""
            else:
                self._times_refused = json.dumps(times, sort_keys=True)
                self._times_error = report.start_time_error = f"start times: {why}"
        if not ok:
            logger.warning("MileSplit Live refused the start times (unverified format): %s", why)

    # ---------------------------------------------------------- thread

    def start(self, interval: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run():
            while not self._stop.wait(interval):
                self.tick()

        self._thread = threading.Thread(target=run, name="milesplit-live", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def status(self) -> dict:
        """For the console: pending change, errors, back-off, counts."""
        now = self.clock()
        with self._lock:
            return {"pending": self._pending, "in_flight": self._in_flight,
                    "errors": self._errors, "last_error": self._last_error,
                    "retry_in": max(0.0, self._backoff_until - now) if self._errors else 0.0,
                    "last_push_age": None if self._last_push_at is None else now - self._last_push_at,
                    "runners_sent": len(self._sent_entries),
                    "events": dict(self._sent_status),
                    "start_times": sum(1 for t in self._sent_times.values() if t),
                    "start_time_error": self._times_error}
