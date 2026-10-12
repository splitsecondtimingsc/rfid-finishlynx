"""The console's meet: one active meet, its edits, start lists and sends.

Glue between the meet package (rfid_finishlynx/meet/, pure functions over
the model) and the running console (RaceManager panes, the RFID folder
FinishLynx reads, Socket.IO, MileSplit). The REST layer (meet_api.py) is a
thin shell over this class; the contract both follow is docs/MEET-API.md.

Every mutation goes the same way (2026-10-03):
    lock -> change -> validate -> save meet.json -> write lynx.evt/.ppl/.sch
    into the RFID folder -> unlock -> emit meet_update
so the file FinishLynx reads can never lag what the operator saw succeed,
and a change that would break the meet (two athletes on one bib, an entry
for nobody) is rolled back instead of saved.

Locking (2026-10-03). Three locks meet here: ours (an RLock around the
meet), RaceManager's RLock, and web.py's monitor locks. RaceManager calls
`athlete_lookup` while it holds its own lock, and its on_change hook runs
on whatever thread made the change. So:
  - `athlete_lookup` never takes our lock: it reads an immutable bib index
    that is swapped in whole after each change (one attribute store);
  - we never call into RaceManager (or web's .evt autoload, which calls
    RaceManager) while holding our lock: pane links and results are
    fetched first, then the lock is taken (`_assert_unlocked` checks it);
  - on_races_changed only takes our lock briefly to drop cached results.
So no thread ever waits for RaceManager's lock while holding ours, and the
two can't deadlock.

Preview (hardware_allowed False): nothing is written where FinishLynx
reads (summary.start_lists.skipped = "preview") and every MileSplit network
call is refused, like refresh_merged_lifs and _preview_refusal in app.py.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from .meet import (bibcheck, bibs, divisions, edits, exports, idmatch, importers, lynx, milesplit,
                   milesplit_web, scoring)
from .meet.model import (DIVISION_GENDERS, ENTRY_STATUSES, RACE_MARKS, Athlete, Division, Entry,
                         Event, Meet, MeetStore, Race, Team, bib_number, grade_key,
                         parse_start_time, start_time_text, time_text)

logger = logging.getLogger(__name__)

ACTIVE_FILE = "ACTIVE"
UPLOADS_FILE = "milesplit-uploads.json"
KINDS = ("xc", "track")

# mp_results_problems lines that are worth showing but must not stop an
# upload: athletes still in the id review queue simply go up as write-ins
# (exports.ms_ids_usable), which is the documented, safe behaviour. Bibs
# not in the meet are left out of the upload; in a FINAL race they can only
# be stray tags the operator marked DNF/DQ/DNS (scoring.freeze refuses an
# unknown bib with a time), so leaving them out is right. (2026-10-03)
NON_BLOCKING_UPLOAD = ("still in the MileSplit id review queue",
                       "not in the meet, left out")


class MeetError(Exception):
    """A request the operator can fix: HTTP status + one line + details
    (MEET-API.md conventions: 409 = a conflict the UI resolves)."""

    def __init__(self, message: str, status: int = 400, **details):
        super().__init__(message)
        self.status = status
        self.details = details

    def payload(self) -> dict:
        return {"error": str(self), **self.details}


def _bibcheck_text(chk: dict) -> str:
    """The bib check's one line for the Info page and the meet badge, with
    one example of each kind of problem (2026-10-10)."""
    parts = []
    ch, wrong, missing = chk["changed"], chk["lynx"]["wrong"], chk["lynx"]["missing"]
    if ch:
        c = ch[0]
        parts.append(f"{len(ch)} runner{'s' if len(ch) != 1 else ''} with another bib than the one handed out "
                     f"({c['name']}: printed {c['printed']}, now {c['now'] or 'none'})")
    if wrong:
        w = wrong[0]
        parts.append(f"FinishLynx's start list names someone else for {len(wrong)} bib{'s' if len(wrong) != 1 else ''} "
                     f"({w['bib']}: {w['lynx']} there, " + (f"{w['wearer']} wearing it)" if w['wearer'] else "nobody has it)"))
    if missing:
        m = missing[0]
        parts.append(f"{len(missing)} bib{'s' if len(missing) != 1 else ''} missing from FinishLynx's start list "
                     f"({m['bib']} {m['wearer']})")
    return "Bib check: " + "; ".join(parts)


def _same_person_error(meet, e) -> "MeetError":
    """409 + duplicate: the athlete already in the meet (edits.SamePerson);
    the console offers Use them / Add anyway (force: true)."""
    return MeetError(str(e), 409, duplicate=athlete_view(meet, meet.athlete(e.view["athlete_id"])))


# --------------------------------------------------------------- helpers

def parse_rk(rk) -> Tuple[int, int, int]:
    """'3-1-1' -> (3, 1, 1); a bare '3' is event 3, round 1, heat 1."""
    text = str(rk or "").strip()
    m = re.fullmatch(r"(\d+)(?:-(\d+)-(\d+))?", text)
    if not m:
        raise MeetError(f"race {text!r} is not <event>-<round>-<heat>", 400)
    return int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)


def rk_of(key) -> str:
    return "-".join(str(int(x)) for x in key)


def _today() -> str:
    return date.today().isoformat()


def _iso_date(value, what="date") -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise MeetError(f"{what} must be YYYY-MM-DD, not {text!r}", 400) from None


def _grade_fields(value) -> Tuple[Optional[int], str]:
    """The console's one grade box -> (grad_year, grade_text): a 4-digit
    year is a graduation year (MileSplit's athleteYear), anything else is
    kept as typed ("9", "Sr")."""
    text = str(value if value is not None else "").strip()
    if re.fullmatch(r"(19|20)\d\d", text):
        return int(text), ""
    return None, text


def _entry_index(meet: Meet) -> Dict[str, List[Tuple[Race, Entry]]]:
    """athlete id -> [(race, entry)] in meet.entries_for order, built once
    so list views don't rescan every race per athlete (1,000+ athletes)."""
    idx: Dict[str, List[Tuple[Race, Entry]]] = {}
    for r in meet.races:
        for e in r.entries:
            idx.setdefault(e.athlete_id, []).append((r, e))
    return idx


def _status(pairs) -> str:
    sts = {e.status for _, e in pairs or ()}
    for s in ("entered", "exhibition", "scratched"):
        if s in sts:
            return s
    return "roster"


def athlete_view(meet: Meet, a: Athlete, idx=None) -> dict:
    """edits._view (same keys, same values) + status / unattached /
    ms_match, from a prebuilt entry index (MEET-API.md athlete view)."""
    pairs = (idx if idx is not None else _entry_index(meet)).get(a.id, [])
    races = [list(r.key) for r, e in pairs if e.status != "scratched"]
    # The race a scratched entry is in: `races` leaves scratches out, and the
    # grid needs it for "SCR · Girls JV 5K" and to know where Un-scratch
    # goes (2026-10-03, found building the UI).
    scr = next((r for r, e in pairs if e.status == "scratched"), None)
    t = meet.team(a.team_id)
    return {"athlete_id": a.id, "first": a.first, "last": a.last, "gender": a.gender,
            "grade": meet.grade(a), "team_id": a.team_id,
            "team": (t.short or t.name) if t else "", "bib": a.bib,
            "races": races, "entered": bool(races), "milesplit_id": a.milesplit_id,
            "status": _status(pairs), "unattached": bool(a.unattached), "ms_match": a.ms_match,
            "scratched_race": rk_of(scr.key) if scr is not None else None,
            "source": a.source, "added_at": a.added_at}


ATHLETE_SORTS = ("bib", "name", "grade", "gender", "team", "race", "milesplit", "status")
_STATUS_ORDER = {"entered": 0, "exhibition": 1, "scratched": 2, "roster": 3}


def _athlete_sort_key(meet: Meet, idx, sort: str):
    """key(athlete) for the grid's clickable headers; None = blank (always
    sorted last, whichever way). (2026-10-03)"""
    run = {r.key: i for i, r in enumerate(meet.races_in_order())}

    def grade(a):
        g = str(meet.grade(a) or "").strip()
        return None if not g else ((0, int(g), "") if g.lstrip("-").isdigit() else (1, 0, g.lower()))

    def team(a):
        t = meet.team(a.team_id)
        return idmatch.norm(t.short or t.name) if t is not None else None

    def race(a):
        pairs = idx.get(a.id, [])
        live = [run.get(r.key, len(run)) for r, e in pairs if e.status != "scratched"]
        scr = [run.get(r.key, len(run)) for r, e in pairs if e.status == "scratched"]
        return min(live) if live else (min(scr) if scr else None)

    def milesplit(a):
        if a.ms_match == "pending":
            return 0
        if a.milesplit_id:
            return 1
        return 2 if a.ms_match == "declined" else None

    return {
        "bib": lambda a: bib_number(a.bib),
        "name": lambda a: (idmatch.norm(a.last), idmatch.norm(a.first)),
        "grade": grade,
        "gender": lambda a: a.gender or None,
        "team": team,
        "race": race,
        "milesplit": milesplit,
        "status": lambda a: _STATUS_ORDER[_status(idx.get(a.id, []))],
    }[sort]


def _bib_str(n: Optional[int]) -> Optional[str]:
    return str(n) if n is not None else None


def _id_num(ident: str) -> int:
    m = re.search(r"\d+", ident or "")
    return int(m.group()) if m else 0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _copy(meet: Meet) -> Meet:
    return Meet.from_dict(copy.deepcopy(meet.to_dict()))


# --------------------------------------------------------------- service

def _bib_events(meet: Meet, race: Race) -> Dict[str, int]:
    """bib -> the scoring event its runner is entered in, in this race."""
    out = {}
    for e in race.entries:
        a = meet.athlete(e.athlete_id)
        if a is not None and a.bib:
            out[str(a.bib)] = e.event
    return out


def _short_labels(names: List[str]) -> List[str]:
    """Event names without the words they all share: "JV Boys 7-9" and
    "JV Boys 10-12" -> "7-9", "10-12"; one name stays whole."""
    words = [n.split() for n in names]
    if len(words) < 2:
        return list(names)
    common = set(words[0]).intersection(*map(set, words[1:]))
    return [" ".join(w for w in ws if w not in common) or n for ws, n in zip(words, names)]


def _gender_block(meet: Meet, race: Race, a) -> str:
    """Why `a` can't be switched into `race`: a girl in a boys' race or the
    other way round (mixed races take anyone). 2026-10-05: a bib swapped
    across genders put the reads in the wrong race; the automatic switch
    already skipped them, the close dialog did not."""
    # A girls race run together with a boys race takes either (2026-10-05).
    gs = {(ev.gender if ev else "X").upper()
          for ev in (meet.event(n) for n in [race.event] + race.together)}
    g, ag = (gs.pop() if len(gs) == 1 else "X"), (a.gender or "").upper()[:1]
    if g in ("M", "F") and ag in ("M", "F") and ag != g:
        return f"{'a girl' if ag == 'F' else 'a boy'} in a {'boys' if g == 'M' else 'girls'}' race (check the bib)"
    return ""


class MeetService:
    """The active meet and everything the console does to it.

    root: the meet store (`meet/` next to the race state file). app: the
    Application (or a stand-in with race_manager, hardware_allowed,
    lif_dirs(), config.event_directory). token_path / transport: the
    MileSplit client's token cache and HTTP transport (tests pass a fake).
    """

    def __init__(self, root: str, app=None, *, token_path: Optional[str] = None,
                 transport: Optional[milesplit.Transport] = None,
                 emit: Optional[Callable[[dict], None]] = None,
                 autoload_evt: Optional[Callable[[str], object]] = None):
        self.root = root
        self.app = app
        self.store = MeetStore(root)
        self._lock = threading.RLock()
        self.meet: Optional[Meet] = None
        self._lookup: Dict[int, dict] = {}          # bib number -> athlete_lookup dict
        self._undo: Optional[Tuple[str, dict]] = None
        self._auto_lock = threading.Lock()            # automatic entry switch
        self._auto_pending: set = set()
        self._auto_running = False
        self._pulls: Dict[str, object] = {}
        self._live_cache: Dict[tuple, dict] = {}    # race key -> {pane, results, unresolved}
        self._uploads: Dict[str, dict] = {}
        self._start_lists = self._blank_start_lists()
        self._needs_autoload: Optional[str] = None
        self._autoloaded: set = set()
        self._emit = emit
        self._autoload_evt = autoload_evt
        self._token_path = token_path
        self._transport = transport
        self._client: Optional[milesplit.MileSplitClient] = None
        self._web = milesplit_web.WebSession()
        self._pusher: Optional[milesplit.LivePusher] = None
        # A closed meet opened without FinishLynx (2026-10-05): its files
        # stay in its meet folder, no start lists are written, no race panes
        # made, results are not changed; leaving it saves the meet file back
        # into the meet folder.
        self.file_only = False
        # The meet sync (a SyncGroup) while it runs, set by the app
        # (2026-10-10): a round before a meet is created or bibs are
        # assigned, and bib edits sent at once.
        self.meet_sync = None
        self._ppl_seen: Optional[tuple] = None        # (path, mtime, size), parsed lynx.ppl
        self._load_active()

    # ------------------------------------------------------------ plumbing

    def _sync_round(self) -> Optional[str]:
        """One sync round now, so this PC has the other PCs' meets and bibs
        before it creates a meet or hands out numbers (2026-10-10). The
        error when no shared place could be reached, None when it worked or
        sync is off. Never under our lock: the round merges into the open
        meet through apply_synced."""
        self._assert_unlocked()
        group = self.meet_sync
        if group is None:
            return None
        try:
            rep = group.sync_now()
        except Exception as e:                     # never stops the edit itself
            logger.exception("Meet sync round before an edit failed")
            return str(e)
        return rep.get("error") or None

    def _refuse_second_import(self, *, milesplit_meet_id=None, name: str = "", day: str = "") -> None:
        """A meet that is already here (made on this PC, or another PC's
        brought in by sync) is never imported again: a second import numbers
        its bibs again, and every runner after the first entry that changed
        on MileSplit gets someone else's number (2026-10-10, Jim Crawford and
        Bob Jenkins). Refresh is how a meet takes MileSplit's changes."""
        same = self.store.find(milesplit_meet_id=milesplit_meet_id, name=name, date_iso=day)
        if not same:
            return
        try:
            existing = self.store.load(same[0]).meet.name or same[0]
        except (OSError, ValueError, KeyError, TypeError):
            existing = same[0]
        raise MeetError(f"{existing} is already on this PC (made here or brought in from "
                        f"another PC). Open it and use Refresh to take MileSplit's changes: "
                        f"importing it again would number its bibs again.", 409,
                        existing=same[0], existing_name=existing)

    def check_new_meet(self, milesplit_meet_id=None, name: str = "", day: str = "") -> None:
        """With no meet open (an import would create one): refuse a meet that
        is already here, before fetching anything."""
        mid = _opt_int(milesplit_meet_id, "meet_id")
        with self._lock:
            if self.meet is None:
                self._refuse_second_import(milesplit_meet_id=mid, name=name,
                                           day=_iso_date(day or _today()) if name else "")

    def _sync_note(self, error: Optional[str]) -> List[str]:
        if not error:
            return []
        return [f"Couldn't reach the other PCs to check for this meet ({error}). If another PC "
                f"already has it, don't assign bibs here: use that PC's copy once sync is back."]

    @property
    def preview(self) -> bool:
        return not getattr(self.app, "hardware_allowed", True) if self.app is not None else False

    def _mgr(self):
        return getattr(self.app, "race_manager", None) if self.app is not None else None

    def _assert_unlocked(self) -> None:
        """We must not wait on RaceManager (or web's autoload) while holding
        our lock: RaceManager calls athlete_lookup under its own."""
        owned = getattr(self._lock, "_is_owned", None)
        if owned is not None and owned():
            raise RuntimeError("MeetService: RaceManager called with the meet lock held")

    def client(self) -> milesplit.MileSplitClient:
        if self._client is None:
            # The saved login lives beside the token (default: %LOCALAPPDATA%
            # \rfid_finishlynx), DPAPI-encrypted - see milesplit.CredentialStore.
            creds_path = (os.path.join(os.path.dirname(os.path.abspath(self._token_path)),
                                       "milesplit-login.bin") if self._token_path else None)
            self._client = milesplit.MileSplitClient(
                self._token_path, self._transport,
                credentials=milesplit.CredentialStore(creds_path))
        return self._client

    def _refuse_network(self) -> None:
        if self.preview:
            raise MeetError("preview mode", 409)

    def _blank_start_lists(self) -> dict:
        return {"dir": "", "written_at": None, "warnings": [], "errors": [], "skipped": None}

    def _require(self) -> Meet:
        if self.meet is None:
            raise MeetError("no meet is open", 409)
        return self.meet

    def _race(self, meet: Meet, rk) -> Race:
        key = rk if isinstance(rk, tuple) else parse_rk(rk)
        race = meet.race(*key)
        if race is None:
            raise MeetError(f"no race {rk_of(key)} in this meet", 404)
        return race

    def _athlete(self, meet: Meet, aid: str) -> Athlete:
        a = meet.athlete(str(aid))
        if a is None:
            raise MeetError(f"no athlete {aid}", 404)
        return a

    # ------------------------------------------------------------ active meet

    def _active_path(self) -> str:
        return os.path.join(self.root, ACTIVE_FILE)

    def _load_active(self) -> None:
        try:
            with open(self._active_path(), encoding="utf-8") as f:
                lines = f.read().split()
        except OSError:
            return
        if not lines:
            return
        meet_id = lines[0]
        try:
            meet = self.store.load(meet_id)
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception(f"Active meet {meet_id} could not be loaded; legacy mode")
            return
        with self._lock:
            self._set_meet_locked(meet)
            self.file_only = "file-only" in lines[1:]
        logger.info(f"Meet mode: {meet.meet.name or meet_id} ({len(meet.athletes)} athletes, "
                    f"{len(meet.races)} races)")

    def _set_meet_locked(self, meet: Optional[Meet]) -> None:
        same = self.meet is not None and meet is not None and self.meet.meet.id == meet.meet.id
        if not same:
            self._stop_pusher_locked()
            self._undo = None
            self._pulls = {}
            self._uploads = {}
            self._start_lists = self._blank_start_lists()
        self.meet = meet
        self._live_cache.clear()
        self._reindex_locked()
        if meet is not None and not same:
            for kind in ("entries", "rosters"):
                try:
                    data = self.store.load_pull(meet.meet.id, kind)
                except (OSError, ValueError):
                    data = None
                if data is not None:
                    self._pulls[kind] = data
            try:
                with open(os.path.join(self.root, meet.meet.id, UPLOADS_FILE), encoding="utf-8") as f:
                    self._uploads = json.load(f)
            except (OSError, ValueError):
                self._uploads = {}

    def _write_active(self, meet_id: Optional[str], file_only: bool = False) -> None:
        path = self._active_path()
        if meet_id is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(self.root, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(meet_id + "\n" + ("file-only\n" if file_only else ""))
        os.replace(tmp, path)

    def _reindex_locked(self) -> None:
        """Rebuild the bib index athlete_lookup reads (first holder wins, as
        Meet.athlete_by_bib). Swapped in whole: readers never see half."""
        lk: Dict[int, dict] = {}
        meet = self.meet
        if meet is not None:
            teams = {t.id: t for t in meet.teams}
            for a in meet.athletes:
                n = bib_number(a.bib)
                if n is None or n in lk:
                    continue
                t = teams.get(a.team_id) if a.team_id else None
                lk[n] = {"last_name": a.last, "first_name": a.first,
                         "school": (t.short or t.name) if t else "", "gender": a.gender}
        self._lookup = lk

    def athlete_lookup(self, bib) -> Optional[dict]:
        """RaceManager's athlete_lookup over the active meet; None for a bib
        the meet doesn't know (web.get_athlete_info then falls back to the
        .ppl). Lock-free on purpose: see the module notes."""
        n = bib_number(bib)
        info = self._lookup.get(n) if n is not None else None
        return dict(info) if info else None

    @property
    def active(self) -> bool:
        return self.meet is not None

    # ------------------------------------------------------------ commit

    def _rfid_dir(self) -> str:
        app = self.app
        if app is None or not hasattr(app, "lif_dirs"):
            return ""
        try:
            return app.lif_dirs()[1] or ""
        except Exception:
            logger.exception("lif_dirs failed")
            return ""

    def _write_start_lists_locked(self) -> None:
        """Rewrite lynx.evt/.ppl/.sch into the RFID folder, never in preview.
        An empty meet (just created, nothing imported) writes nothing: it
        would blank a start list FinishLynx may still be using."""
        meet = self.meet
        sl = self._blank_start_lists()
        directory = self._rfid_dir()
        sl["dir"] = directory
        if meet is None:
            return
        if self.file_only:
            sl["skipped"] = "file only"
        elif self.preview:
            sl["skipped"] = "preview"
        elif not directory:
            sl["skipped"] = "no folder"
        elif not meet.races and not meet.athletes:
            sl["skipped"] = "empty meet"
        else:
            try:
                os.makedirs(directory, exist_ok=True)
                rep = lynx.write_start_lists(meet, directory)
                sl["warnings"] = rep["warnings"]
                sl["errors"] = rep["errors"]
                sl["written_at"] = datetime.now().isoformat(timespec="seconds")
                sl["written"] = rep["written"]
                if rep["written"] and directory not in self._autoloaded:
                    self._needs_autoload = directory
            except OSError as e:
                logger.error(f"Start lists not written to {directory}: {e}")
                sl["errors"] = [str(e)]
        self._start_lists = sl

    def _commit_locked(self, write: bool = True) -> dict:
        meet = self._require()
        self.store.save(meet)
        self._reindex_locked()
        self._live_cache.clear()
        if write:
            self._write_start_lists_locked()
        return self._summary_locked()

    def _post(self, summary: dict) -> None:
        """After the lock is released: let the .evt monitor see a folder we
        just wrote into for the first time, tell the consoles, nudge Live."""
        self._assert_unlocked()
        directory, self._needs_autoload = self._needs_autoload, None
        if directory:
            self._ensure_monitored(directory)
        self._sync_panes()
        self._emit_summary(summary)
        pusher = self._pusher
        if pusher is not None:
            pusher.notify()

    def pane_specs(self) -> List[dict]:
        """The meet's races as web.evt_race_list rows (run order), the shape
        web.create_missing_panes takes."""
        with self._lock:
            meet = self.meet
            if meet is None:
                return []
            bib_of = {a.id: a.bib for a in meet.athletes}
            specs = []
            for race in meet.races_in_order():
                ev = meet.event(race.event)
                specs.append({
                    "event": race.event, "round": race.round, "heat": race.heat,
                    "name": lynx.race_name(meet, race),
                    "gender": (ev.gender if ev and ev.gender in ("F", "M") else "X"),
                    "is_relay": bool(ev and ev.kind == "relay"),
                    "distance": str(ev.distance_m) if ev and ev.distance_m else "",
                    "bibs": [bib_of[e.athlete_id] for e in race.entries
                             if e.status != "scratched" and bib_of.get(e.athlete_id)],
                })
            return specs

    def _sync_panes(self) -> int:
        """Meet mode: every meet race gets its race pane without the operator's
        "Races from .evt" step (2026-10-03) - rfid-meet owns the races, the
        .evt it writes only feeds FinishLynx and the entries sync. Creates
        missing panes only (web.create_missing_panes is idempotent); entries
        of existing panes keep flowing through the .evt monitor as before."""
        self._assert_unlocked()
        mgr = self._mgr()
        if mgr is None or self.file_only:
            return 0
        specs = self.pane_specs()
        if not specs:
            return 0
        try:
            from . import web
            made = web.create_missing_panes(mgr, specs)
            if made:
                logger.info(f"Meet mode: {made} race pane(s) created from the meet")
            return made
        except Exception:
            logger.exception("Could not create race panes for the meet")
            return 0

    def _ensure_monitored(self, directory: str) -> None:
        """The existing .evt monitor (web._autoload_evt) re-reads the .evt we
        write and syncs entries into the race panes; it only starts watching
        once a meet folder's .evt is loaded, so load it after our first write."""
        from . import web
        try:
            with web._evt_monitor_lock:
                watching = web._evt_monitor_state.get("monitored_dir")
            if watching and os.path.normcase(os.path.abspath(watching)) == \
                    os.path.normcase(os.path.abspath(directory)):
                self._autoloaded.add(directory)
                return
            event_dir = getattr(getattr(self.app, "config", None), "event_directory", "") or directory
            if self._autoload_evt is not None:
                self._autoload_evt(event_dir)
            else:
                # what "Load meet folder" does: the .ppl (web's school/gender
                # bib data + its own change monitor) and the .evt (panes)
                web.load_ppl_from_directory(event_dir)
                web._autoload_evt(event_dir)
            self._autoloaded.add(directory)
        except Exception:
            logger.exception("Could not start watching the start lists we wrote")

    def _emit_summary(self, summary: dict) -> None:
        try:
            if self._emit is not None:
                self._emit(summary)
            else:
                from . import web
                web.emit_meet_update(summary)
        except Exception:
            logger.debug("meet_update push skipped", exc_info=True)

    def _mutate(self, fn: Callable[[Meet], object], *, undo: str = "", check: bool = True,
                write: bool = True):
        """lock -> fn(meet) -> validate -> save -> start lists -> unlock ->
        emit. Anything fn raises, or a validation problem fn introduced,
        rolls the meet back. undo = a label: this change is the one
        /api/meet/undo takes back (athlete and bib edits only)."""
        with self._lock:
            meet = self._require()
            before = meet.to_dict()
            known = set(meet.validate()) if check else set()
            try:
                result = fn(meet)
                if check:
                    new = [e for e in self.meet.validate() if e not in known]
                    if new:
                        raise MeetError(new[0] if len(new) == 1 else
                                        f"{new[0]} (+{len(new) - 1} more)", 400, problems=new)
            except BaseException:
                self.meet = Meet.from_dict(before)
                self._reindex_locked()
                raise
            self._undo = (undo, before) if undo else None
            bibs_moved = ({a["id"]: a["bib"] for a in before["athletes"]}
                          != {a.id: a.bib for a in self.meet.athletes})
            summary = self._commit_locked(write)
        self._post(summary)
        group = self.meet_sync
        if bibs_moved and group is not None:
            group.notify_saved(self.meet.meet.id if self.meet else "", urgent=True)   # 2026-10-10
        return result, summary

    # ------------------------------------------------------------ summary

    def summary(self) -> dict:
        with self._lock:
            return self._summary_locked()

    def start_lists_status(self) -> Optional[dict]:
        """The last start-list write for the Setup check (2026-10-05):
        {dir, written_at, warnings, errors, skipped}, or None with no meet
        open. Cheap: no MileSplit or counts, unlike summary()."""
        with self._lock:
            if self.meet is None:
                return None
            return {k: copy.deepcopy(self._start_lists.get(k))
                    for k in ("dir", "written_at", "warnings", "errors", "skipped")}

    def _summary_locked(self) -> dict:
        meet = self.meet
        if meet is None:
            return {"active": False}
        idx = _entry_index(meet)
        counts = dict.fromkeys(("athletes", "entered", "roster_only", "teams", "races",
                                "final_races", "with_bib", "missing_bib", "scratched",
                                "exhibition", "unattached", "ms_ids"), 0)
        counts.update(athletes=len(meet.athletes), teams=len(meet.teams), races=len(meet.races),
                      final_races=sum(1 for r in meet.races if r.status == "final"))
        for a in meet.athletes:
            st = _status(idx.get(a.id))
            runs = st in ("entered", "exhibition")
            counts["entered"] += runs
            counts["roster_only"] += st == "roster"
            counts["scratched"] += st == "scratched"
            counts["exhibition"] += st == "exhibition"
            counts["with_bib"] += bool(a.bib)
            counts["missing_bib"] += runs and not a.bib
            counts["unattached"] += a.team_id is None or bool(a.unattached)
            counts["ms_ids"] += runs and a.milesplit_id is not None
        cov = idmatch.id_coverage(meet)
        counts["ms_share"] = round(cov.share, 4)

        fix = []
        conf = bibs.conflicts(meet)
        if conf.missing:
            fix.append({"kind": "no_bib", "text": f"{len(conf.missing)} entered athlete(s) "
                        "without a bib (left out of the .evt)", "count": len(conf.missing),
                        "detail": [m["athlete_id"] for m in conf.missing]})
        pending = [a.id for a in meet.athletes if a.ms_match == "pending"]
        if pending:
            fix.append({"kind": "id_review", "text": f"{len(pending)} athlete(s) waiting for "
                        "a MileSplit id decision", "count": len(pending), "detail": pending})
        ms_meet = bool(meet.meet.milesplit.meet_id or self._pulls.get("entries"))
        if ms_meet and cov.athletes and not cov.ok:
            low = [rk_of(k) for k, c in idmatch.coverage_by_race(meet).items() if not c.ok]
            fix.append({"kind": "id_coverage", "text": f"only {cov.with_id}/{cov.athletes} "
                        "entered athletes have a MileSplit id (MileSplit needs half)",
                        "count": cov.athletes - cov.with_id, "detail": low})
        bad = conf.duplicates + conf.invalid
        if bad:
            fix.append({"kind": "bib_conflict", "text": f"{len(bad)} bib problem(s)",
                        "count": len(bad), "detail": bad})
        flagged = {rk_of(r.key): len((r.results or {}).get("issues") or [])
                   for r in meet.races_in_order() if r.status == "final"}
        flagged = {k: n for k, n in flagged.items() if n}
        if flagged:
            fix.append({"kind": "issues", "text": "final results leave out flagged runners: " + ", ".join(
                        f"{race_label(meet, meet.race(*parse_rk(rk)))} ({n})" for rk, n in flagged.items())
                        + " - resolve them on the Results page", "count": sum(flagged.values()),
                        "detail": list(flagged)})
        stale = stale_finals(meet)
        if stale:
            fix.append({"kind": "stale_final", "text": "final results show old bibs: " + ", ".join(
                        f"{race_label(meet, meet.race(*parse_rk(rk)))} ({n})" for rk, n in stale.items())
                        + " - re-finalize", "count": sum(stale.values()), "detail": list(stale)})
        if self._start_lists.get("errors"):
            errs = self._start_lists["errors"]
            fix.append({"kind": "start_lists", "text": "start lists not written: " + errs[0],
                        "count": len(errs), "detail": list(errs)})
        chk = self._bibcheck_locked()
        if not chk["ok"]:
            fix.append({"kind": "bib_check", "text": _bibcheck_text(chk),
                        "count": len(chk["changed"]) + len(chk["lynx"]["wrong"]) + len(chk["lynx"]["missing"]),
                        "detail": sorted(chk["races"])})

        info = asdict(meet.meet)
        info.pop("id", None)
        live = None
        if self._pusher is not None:
            try:
                live = self._pusher.status()
            except Exception:
                live = None
        try:
            logged_in = self.client().logged_in()
            remembered = self.client().remembered()
        except Exception:
            logged_in, remembered = False, None
        sl = {k: self._start_lists.get(k) for k in ("dir", "written_at", "warnings", "errors",
                                                    "skipped")}
        return {"active": True, "id": meet.meet.id, "file_only": self.file_only,
                "closed_to": self.archived_folder(meet.meet.id) if self.file_only else None,
                "info": info, "counts": counts, "fix": fix,
                # the bib check per race, for the race panes' warning (2026-10-10)
                "bib_check": {"races": chk["races"], "recorded": chk["recorded"], "runners": chk["runners"]},
                "start_lists": sl, "milesplit": {"logged_in": logged_in, "live": live, "can_remember": os.name == "nt",
                              "remembered": remembered},
                "preview": self.preview, "bib_ranges": [list(r) for r in meet.bib_ranges],
                # divisions with their summary line and races (2026-10-06)
                "divisions": [divisions.view(meet, d) for d in meet.meet.divisions],
                "updated_at": meet.updated_at, "can_undo": bool(self._undo)}

    # ------------------------------------------------------------ meet CRUD

    def list_meets(self) -> List[dict]:
        out = []
        for meet_id in self.store.list():
            try:
                with open(self.store.path(meet_id), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            m = d.get("meet") or {}
            out.append({"id": meet_id, "name": m.get("name", ""), "date": m.get("date", ""),
                        "updated_at": d.get("updated_at", ""), "closed_to": self.archived_folder(meet_id)})
        out.sort(key=lambda m: (m["date"], m["id"]), reverse=True)
        return out

    def _activate(self, meet: Meet, write: bool = True, file_only: bool = False) -> dict:
        with self._lock:
            if self.meet is None or self.meet.meet.id != meet.meet.id:
                self._save_back_locked()
            self._set_meet_locked(meet)
            self.file_only = file_only
            self._write_active(meet.meet.id, file_only)
            summary = self._commit_locked(write)
        self._post(summary)
        return summary

    def new_meet(self, d: dict) -> dict:
        name = str(d.get("name") or "").strip()
        if not name:
            raise MeetError("a meet needs a name", 400)
        day = _iso_date(d.get("date") or _today())
        kind = str(d.get("kind") or "xc")
        if kind not in KINDS:
            raise MeetError(f"kind must be one of {', '.join(KINDS)}", 400)
        self._sync_round()
        with self._lock:
            self._refuse_second_import(name=name, day=day)
            meet = self.store.create(name, day, host=str(d.get("host") or ""),
                                     location=str(d.get("location") or ""), kind=kind)
        logger.info(f"Meet created: {meet.meet.id}")
        return self._activate(meet)

    def open_meet(self, meet_id: str, file_only: bool = False) -> dict:
        meet_id = str(meet_id or "").strip()
        if not meet_id or not re.fullmatch(r"[A-Za-z0-9._-]+", meet_id) or not self.store.exists(meet_id):
            raise MeetError(f"no meet {meet_id!r}", 404)
        meet = self.store.load(meet_id)
        logger.info(f"Meet opened: {meet_id}" + (" (meet file only)" if file_only else ""))
        return self._activate(meet, file_only=file_only)

    def open_path(self, path: str) -> dict:
        """Open another… → Browse… (2026-10-03): a meet.json (or the folder
        holding one) from anywhere - a closed-meets archive, a USB stick,
        another console's store. Already this store's own copy: just opened.
        Otherwise its folder (meet.json, pulls, uploads) is copied into the
        store first, under a new id ("-copy", "-copy2"...) if that id is
        taken, so nothing here is ever overwritten."""
        path = os.path.abspath(str(path or "").strip())
        rebuilt = self._rebuilt_meet(path)
        if rebuilt is not None:
            return rebuilt
        if os.path.isdir(path):
            path = os.path.join(path, MeetStore.FILENAME)
        if not os.path.isfile(path):
            raise MeetError(f"no meet file at {path}", 404)
        try:
            with open(path, encoding="utf-8") as f:
                meet = Meet.from_dict(json.load(f))
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise MeetError(f"{path} is not a meet file: {e}", 400) from None
        meet_id = meet.meet.id
        if not re.fullmatch(r"[A-Za-z0-9._-]+", meet_id or ""):
            raise MeetError(f"{path}: bad meet id {meet_id!r}", 400)
        src = os.path.dirname(path)
        if os.path.normcase(src) == os.path.normcase(os.path.abspath(os.path.join(self.root, meet_id))):
            return self.open_meet(meet_id)
        with self._lock:
            new_id, n = meet_id, 1
            while self.store.exists(new_id):
                new_id, n = f"{meet_id}-copy{'' if n == 1 else n}", n + 1
            shutil.copytree(src, os.path.join(self.root, new_id), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp", "ACTIVE"))
            meet.meet.id = new_id
            self.store.save(meet)
        logger.info(f"Meet opened from {path} as {new_id}")
        return self._activate(meet)

    def _rebuilt_meet(self, path: str) -> Optional[dict]:
        """A meet from a shared folder's copy (2026-10-10): a machine's split
        folder, its manifest.json, or a single <MACHINE>.json.gz - rebuilt
        and added to this store (a new id if taken), nothing overwritten.
        None when `path` is none of those (an ordinary meet.json)."""
        from . import meet_split
        split_dir = path if meet_split.is_split_folder(path) else (
            os.path.dirname(path) if os.path.basename(path).lower() == meet_split.MANIFEST else None)
        if split_dir is None and not path.lower().endswith(".json.gz"):
            return None
        try:
            snap = meet_split.read_any(path)
        except meet_split.Incomplete as e:
            raise MeetError(f"{path}: not complete yet ({e}) - wait for the sync app, then open it again", 409) from None
        except (OSError, ValueError) as e:
            raise MeetError(f"{path} is not a meet: {e}", 400) from None
        snap.pop("sync", None)
        whole = meet_split.is_snapshot(snap)          # a sync copy: the meet + its pulls
        try:
            meet = Meet.from_dict(snap["meet"] if whole else snap)
        except (ValueError, TypeError, KeyError) as e:
            raise MeetError(f"{path} is not a meet file: {e}", 400) from None
        meet_id = meet.meet.id
        if not re.fullmatch(r"[A-Za-z0-9._-]+", meet_id or ""):
            raise MeetError(f"{path}: bad meet id {meet_id!r}", 400)
        with self._lock:
            new_id, n = meet_id, 1
            while self.store.exists(new_id):
                new_id, n = f"{meet_id}-copy{'' if n == 1 else n}", n + 1
            meet.meet.id = new_id
            if whole:
                # MileSplit pulls and the upload record come along
                from .meet_sync import write_snapshot
                snap["meet"]["meet"]["id"] = new_id
                write_snapshot(self.root, new_id, snap)
            self.store.save(meet)
        logger.info(f"Meet rebuilt from {path} as {new_id}")
        return self._activate(meet)

    # Closed meets (2026-10-05): Close meet saves into "<date> <name>" in
    # the FinishLynx results folder (web.py, meet_folder.py) and notes it
    # here, so opening the meet again restores it from there.
    ARCHIVE_NOTE = "archive.json"

    def archived_folder(self, meet_id: str) -> Optional[str]:
        """The meet folder `meet_id` was closed into, or None."""
        try:
            with open(os.path.join(self.root, meet_id, self.ARCHIVE_NOTE), encoding="utf-8") as f:
                return json.load(f).get("folder") or None
        except (OSError, ValueError, AttributeError):
            return None

    def note_archive(self, meet_id: str, folder: str) -> None:
        path = os.path.join(self.root, meet_id, self.ARCHIVE_NOTE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"folder": folder}, f)
        os.replace(path + ".tmp", path)

    def reopen_from(self, meet_dir: str, folder: str, file_only: bool = False) -> dict:
        """Reopen a closed meet: the copy saved in its meet folder (meet.json,
        pulls, uploads) replaces this console's copy, then it is opened
        (file_only: without FinishLynx, see self.file_only)."""
        try:
            with open(os.path.join(meet_dir, MeetStore.FILENAME), encoding="utf-8") as f:
                meet_id = Meet.from_dict(json.load(f)).meet.id
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise MeetError(f"{meet_dir} has no readable meet file: {e}", 400) from None
        if not re.fullmatch(r"[A-Za-z0-9._-]+", meet_id or ""):
            raise MeetError(f"{meet_dir}: bad meet id {meet_id!r}", 400)
        with self._lock:
            shutil.copytree(meet_dir, os.path.join(self.root, meet_id), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp", "ACTIVE", self.ARCHIVE_NOTE))
            self.note_archive(meet_id, folder)
            meet = self.store.load(meet_id)
        logger.info(f"Meet reopened from {folder}" + (" (meet file only)" if file_only else ""))
        return self._activate(meet, file_only=file_only)

    def detach(self) -> dict:
        """Back to legacy mode; meet.json and the start lists stay as they are."""
        with self._lock:
            if self.meet is not None:
                logger.info(f"Meet detached: {self.meet.meet.id}")
            self._save_back_locked()
            self._set_meet_locked(None)
            self.file_only = False
            self._write_active(None)
            summary = {"active": False}
        self._emit_summary(summary)
        return summary

    def save_back(self) -> Optional[str]:
        """A meet opened without FinishLynx: write its meet file back into
        the meet folder it was closed to (Middleware/meet), nothing else.
        The folder, or None when there is none (the console's copy only)."""
        with self._lock:
            return self._save_back_locked()

    def _save_back_locked(self) -> Optional[str]:
        # Leaving a meet-file-only meet any way (Save & close, another meet,
        # detach) keeps its meet folder up to date: the next reopen copies
        # the meet folder's meet.json over this console's copy.
        if not self.file_only or self.meet is None:
            return None
        folder = self.archived_folder(self.meet.meet.id)
        if not folder or not os.path.isdir(folder):
            self.store.save(self.meet)
            return None
        try:
            self.archive_to(os.path.join(folder, "Middleware"))
        except OSError as e:
            raise MeetError(f"could not save the meet file back into {folder}: {e}", 500) from None
        logger.info(f"Meet file saved back into {folder}")
        return folder

    def archive_to(self, archive_dir: str) -> Optional[str]:
        """Close meet: copy meet/<id>/ (meet.json, pulls, uploads) into the
        closed-meets folder, so the archive holds who ran as well as the
        reads. Raises OSError (the close is then aborted)."""
        with self._lock:
            if self.meet is None:
                return None
            self.store.save(self.meet)
            src = os.path.join(self.root, self.meet.meet.id)
            dest = os.path.join(archive_dir, "meet")
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copytree(src, dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp"))
            return dest

    def set_info(self, d: dict) -> dict:
        def change(meet: Meet):
            info = meet.meet
            for k in ("name", "host", "location"):
                if k in d:
                    setattr(info, k, str(d[k] or "").strip())
            if "name" in d and not info.name:
                raise MeetError("a meet needs a name", 400)
            if "date" in d:
                info.date = _iso_date(d["date"])
            if "kind" in d:
                if d["kind"] not in KINDS:
                    raise MeetError(f"kind must be one of {', '.join(KINDS)}", 400)
                info.kind = d["kind"]
            if "full_rosters" in d:
                info.full_rosters = bool(d["full_rosters"])
            ms = d.get("milesplit")
            if isinstance(ms, dict):
                for k in ("tc_id", "meet_id"):
                    if k in ms:
                        setattr(info.milesplit, k, _opt_int(ms[k], f"milesplit.{k}"))
            sc = d.get("scoring")
            if isinstance(sc, dict):
                if "scorers" in sc:
                    info.scoring.scorers = _int_in(sc["scorers"], 1, 20, "scorers")
                if "displacers" in sc:
                    info.scoring.displacers = _int_in(sc["displacers"], 0, 20, "displacers")
        return self._mutate(change)[1]

    def set_divisions(self, d: dict) -> dict:
        """Replace the meet's divisions with {"divisions": [{id?, name,
        gender "" | M | F, team_ids [], grades [], team_scores}]} (the
        Divisions dialog saves the whole list). A row without an id is a new
        division; a division left out is deleted and taken off every race.
        [] = no divisions: the meet scores as it always has. Divisions are
        worked out when shown (divisions.py), so a final race needs no
        re-finalizing and the start lists aren't rewritten. (2026-10-06)"""
        rows = d.get("divisions")
        if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
            raise MeetError("divisions must be a list", 400)

        def change(meet: Meet):
            team_ids = {t.id for t in meet.teams}
            old = {x.id: x for x in meet.meet.divisions}
            out: List[Division] = []
            names = set()
            for i, row in enumerate(rows, 1):
                name = str(row.get("name") or "").strip()
                if not name:
                    raise MeetError(f"division {i} needs a name", 400)
                if name.casefold() in names:
                    raise MeetError(f"two divisions are named {name}", 400)
                names.add(name.casefold())
                gender = str(row.get("gender") or "").strip().upper()
                if gender in ("B", "X", "BOTH"):
                    gender = ""
                if gender not in DIVISION_GENDERS:
                    raise MeetError(f"{name}: gender must be M, F or blank (both)", 400)
                tids = row.get("team_ids") or []
                if not isinstance(tids, list):
                    raise MeetError(f"{name}: team_ids must be a list", 400)
                tids = list(dict.fromkeys(str(t) for t in tids))
                bad = [t for t in tids if t not in team_ids]
                if bad:
                    raise MeetError(f"{name}: no team {', '.join(bad)} in this meet", 400)
                grades = row.get("grades") or []
                if not isinstance(grades, list):
                    raise MeetError(f"{name}: grades must be a list", 400)
                grades = list(dict.fromkeys(k for k in (grade_key(g) for g in grades) if k))
                ident = str(row.get("id") or "")
                if ident and ident not in old:
                    raise MeetError(f"no division {ident} in this meet", 400)
                if not ident or ident in {x.id for x in out}:
                    ident = meet.new_division_id(taken=[x.id for x in out])
                out.append(Division(id=ident, name=name, gender=gender, team_ids=tids,
                                    grades=grades, team_scores=bool(row.get("team_scores", True))))
            kept = {x.id for x in out}
            for r in meet.races:
                r.divisions = [i for i in r.divisions if i in kept]
            meet.meet.divisions = out
        return self._mutate(change, write=False)[1]

    # ------------------------------------------------------------ import

    def _pool(self, meet: Meet) -> idmatch.CandidatePool:
        return idmatch.CandidatePool.from_milesplit(meet, entries=self._pulls.get("entries"),
                                                    rosters=self._pulls.get("rosters"))

    def _match_one(self, meet: Meet, a: Athlete) -> Optional[dict]:
        """idmatch for one new local athlete against the stored pulls:
        exact match -> ids attached ("auto"), plausible -> review queue
        ("pending", returned as the QueueItem), nothing -> None."""
        if not (self._pulls.get("entries") or self._pulls.get("rosters")):
            return None
        out = idmatch.match_athlete(meet, self._pool(meet), a)
        if out.action == "auto":
            c = out.ranked[0].candidate
            a.milesplit_id, a.universal_id, a.ms_match = int(c.milesplit_id), c.universal_id or None, "auto"
        elif out.action == "review":
            a.ms_match = "pending"
            return idmatch.queue_item(meet, a, out.ranked).to_dict()
        return None

    def fetch_milesplit(self, tc_id, meet_id, full_rosters: bool) -> Tuple[object, object]:
        """Entries (+ the attending teams' rosters) from MileSplit. Network:
        refused in preview, never called with our lock held."""
        self._refuse_network()
        self._assert_unlocked()
        tc, mid = _opt_int(tc_id, "tc_id"), _opt_int(meet_id, "meet_id")
        if not (tc and mid):
            raise MeetError("tc_id and meet_id are needed to fetch from MileSplit", 400)
        client = self.client()
        try:
            entries = client.entries(tc, mid)
            rosters = None
            if full_rosters:
                ids = [t.get("teamMileSplitId") or t.get("teamId") for t in entries or []
                       if isinstance(t, dict) and not str(t.get("teamName") or "").lower()
                       .startswith("unattached")]
                rosters = client.team_rosters(tc, mid, [i for i in ids if i])
                names = {_opt_int(t.get("teamMileSplitId") or t.get("teamId"), "team"): t.get("teamName")
                         for t in entries or [] if isinstance(t, dict)}
                for t in rosters if isinstance(rosters, list) else []:
                    if isinstance(t, dict) and not t.get("teamName"):
                        t["teamName"] = names.get(t.get("teamMileSplitId")) or ""
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        return entries, rosters

    def import_milesplit(self, entries, rosters=None, *, name: str = "", day: str = "",
                         tc_id=None, meet_id=None, host: str = "", location: str = "") -> Tuple[dict, dict]:
        """No active meet: a new meet from the pull, activated. Active meet:
        a refresh, three-way against the stored pull. The pull is kept
        beside meet.json for the next refresh and for idmatch."""
        if entries is None:
            raise MeetError("no entries given", 400)
        tc, mid = _opt_int(tc_id, "tc_id"), _opt_int(meet_id, "meet_id")
        sync_error = self._sync_round() if self.meet is None else None
        with self._lock:
            creating = self.meet is None
            if creating:
                self._refuse_second_import(milesplit_meet_id=mid, name=name, day=_iso_date(day or _today()))
                base = self.store.create(name or (f"MileSplit meet {mid}" if mid else "Imported meet"),
                                         _iso_date(day or _today()), host=host, location=location)
                info = base.meet
                info.milesplit.tc_id, info.milesplit.meet_id = tc, mid
                try:
                    meet, report = importers.import_milesplit(entries, info, rosters=rosters)
                except (ValueError, TypeError, AttributeError) as e:
                    shutil.rmtree(os.path.join(self.root, info.id), ignore_errors=True)
                    raise MeetError(f"not a MileSplit entries pull: {e}", 400) from None
                report.notes += self._sync_note(sync_error)
                self._set_meet_locked(meet)
                self._write_active(meet.meet.id)
                self._save_pulls_locked(entries, rosters)
                summary = self._commit_locked()
        if creating:
            self._post(summary)
            logger.info(f"Meet imported from MileSplit: {meet.meet.id} ({report.summary()})")
            return report.to_dict(), summary

        def change(meet: Meet):
            if tc:
                meet.meet.milesplit.tc_id = tc
            if mid:
                meet.meet.milesplit.meet_id = mid
            # A meet first pulled by bare id is "MileSplit meet <id>"; a
            # refresh that knows the real name renames it (2026-10-03, user:
            # "don't use the # as the title"). A name the operator typed stays.
            info = meet.meet
            if name and info.name in ("", f"MileSplit meet {info.milesplit.meet_id}"):
                info.name = name
            if host and not info.host:
                info.host = host
            if location and not info.location:
                info.location = location
            prev_r = self._pulls.get("rosters") if rosters is not None else None
            rep = importers.refresh_milesplit(meet, entries, previous=self._pulls.get("entries"),
                                              rosters=rosters, previous_rosters=prev_r)
            if rosters is not None:
                info.full_rosters = True           # rosters added to an entries-only meet (2026-10-08)
            self._save_pulls_locked(entries, rosters)
            idmatch.match_meet(meet, self._pool(meet))
            return rep
        report, summary = self._mutate(change, check=False)
        logger.info(f"Meet refreshed from MileSplit: {report.summary()}")
        return report.to_dict(), summary

    def _save_pulls_locked(self, entries, rosters) -> None:
        meet_id = self.meet.meet.id
        self.store.save_pull(meet_id, "entries", entries)
        self._pulls["entries"] = entries
        if rosters is not None:
            self.store.save_pull(meet_id, "rosters", rosters)
            self._pulls["rosters"] = rosters

    def import_file(self, data: bytes, filename: str, include_roster_only: bool = False, *,
                    name: str = "", day: str = "",
                    file_bibs: Optional[bool] = None) -> Tuple[dict, dict]:
        """Hy-Tek semicolon (.txt) or plain .csv: a new meet when none is
        active, else merged into the active one (no previous pull: identity
        by name within a team). file_bibs: take the file's bib numbers
        (importers.merge; None = only while the meet has no bibs yet)."""
        is_csv = filename.lower().endswith(".csv")
        try:
            pull = importers.parse_csv(data) if is_csv else importers.parse_hytek(data, include_roster_only)
        except (ValueError, UnicodeDecodeError) as e:
            raise MeetError(f"{filename}: {e}", 400) from None
        sync_error = self._sync_round() if self.meet is None else None
        with self._lock:
            creating = self.meet is None
            if creating:
                stem = os.path.splitext(os.path.basename(filename))[0] or "Imported meet"
                self._refuse_second_import(name=name or stem, day=_iso_date(day or _today()))
                base = self.store.create(name or stem, _iso_date(day or _today()))
                meet = Meet(meet=base.meet)
                report = importers.merge(meet, pull)
                report.notes += self._sync_note(sync_error)
                self._set_meet_locked(meet)
                self._write_active(meet.meet.id)
                summary = self._commit_locked()
        if creating:
            self._post(summary)
            logger.info(f"Meet imported from {filename}: {meet.meet.id} ({report.summary()})")
            return report.to_dict(), summary

        def change(meet: Meet):
            rep = importers.merge(meet, pull, file_bibs=file_bibs)
            if self._pulls:
                idmatch.match_meet(meet, self._pool(meet))
            return rep
        report, summary = self._mutate(change, check=False)
        return report.to_dict(), summary

    def refresh_fetch(self, name: str = "", host: str = "", location: str = "",
                      full_rosters: bool = False) -> Tuple[dict, dict]:
        """full_rosters: the console's "full team rosters" box. Ticked on an
        entries-only meet, this refresh brings the rosters in and the meet
        keeps them from then on (2026-10-08: the box was ignored on Refresh,
        so a meet imported entries-only could never get its rosters). A meet
        that has them always fetches them (they'd all be reported gone)."""
        with self._lock:
            meet = self._require()
            tc, mid = meet.meet.milesplit.tc_id, meet.meet.milesplit.meet_id
            full = meet.meet.full_rosters or full_rosters
        if not (tc and mid):
            raise MeetError("this meet is not linked to a MileSplit meet (set tc_id and meet_id)", 409)
        entries, rosters = self.fetch_milesplit(tc, mid, full)
        return self.import_milesplit(entries, rosters, name=name, host=host, location=location)

    # ------------------------------------------------------------ teams / athletes

    def teams(self) -> List[dict]:
        with self._lock:
            meet = self._require()
            idx = _entry_index(meet)
            out = []
            for t in meet.teams:
                members = [a for a in meet.athletes if a.team_id == t.id]
                sts = [_status(idx.get(a.id)) for a in members]
                d = asdict(t)
                d.update(girls=sum(1 for a in members if a.gender == "F"),
                         boys=sum(1 for a in members if a.gender == "M"),
                         entered=sum(1 for s in sts if s in ("entered", "exhibition")),
                         roster_only=sts.count("roster"),
                         bibs=bibs.summarize_bibs(a.bib for a in members if a.bib))
                out.append(d)
            return out

    def update_team(self, tid: str, d: dict) -> dict:
        def change(meet: Meet):
            t = meet.team(tid)
            if t is None:
                raise MeetError(f"no team {tid}", 404)
            if "name" in d:
                t.name = str(d["name"] or "").strip()
                if not t.name:
                    raise MeetError("a team needs a name", 400)
            if "short" in d:
                t.short = str(d["short"] or "").strip()
            if "code" in d:
                t.code = str(d["code"] or "").strip().upper()
            if "score" in d:
                t.score = bool(d["score"])
        return self._mutate(change)[1]

    def add_team(self, d: dict) -> Tuple[str, dict]:
        """A team that isn't in the entries (a school that turns up with a
        walk-up). The code must be unique (.ppl/.evt and Hy-Tek key on it):
        a code given and taken is a 409; none given is made from the name
        the way imports make one (importers.team_code). (2026-10-03)"""
        def change(meet: Meet):
            name = str(d.get("name") or "").strip()
            if not name:
                raise MeetError("a team needs a name", 400)
            if any(idmatch.norm(t.name) == idmatch.norm(name) for t in meet.teams):
                raise MeetError(f"there is already a team called {name}", 409)
            code = re.sub(r"\s+", "", str(d.get("code") or "")).upper()
            if code:
                if len(code) > 4:
                    raise MeetError("a team code is at most 4 characters", 400)
                other = next((t for t in meet.teams if t.code.upper() == code), None)
                if other is not None:
                    raise MeetError(f"code {code} is {other.name}'s", 409, team_id=other.id)
            else:
                code = importers.team_code("", name, meet.teams)
            short = str(d.get("short") or "").strip() or importers.short_name(name, meet.teams, code)
            t = Team(id=meet.new_team_id(), name=name, short=short, code=code)
            meet.teams.append(t)
            return t.id
        tid, summary = self._mutate(change)
        logger.info(f"Meet: team {tid} added")
        return tid, summary

    def athletes(self, q: str = "", team: str = "", gender: str = "", race: str = "",
                 roster: Optional[str] = None, limit: Optional[int] = None,
                 offset: int = 0, source: str = "", sort: str = "",
                 desc: bool = False, nobib_first: bool = False) -> dict:
        """The athlete grid: meet order (never re-sorted under the operator),
        filtered; q matches a bib exactly or a name part.

        sort (one of ATHLETE_SORTS, a clicked column header): that order,
        desc flips it; blanks (no bib, no race...) stay last either way and
        ties keep meet order, so paging stays stable. (2026-10-03)

        source (milesplit | hytek | csv | local): only athletes from there,
        NEWEST FIRST - source=local is the "Recently added walk-ups" list,
        kept on the server so every browser sees the same one (2026-10-03)."""
        with self._lock:
            meet = self._require()
            idx = _entry_index(meet)
            key = parse_rk(race) if race else None
            qn = idmatch.norm(q) if q else ""
            qbib = bib_number(q) if q and q.strip().isdigit() else None
            rows = []
            pool = meet.athletes
            if source:
                # added_at, then the id number (ids grow and are never reused)
                pool = sorted((a for a in meet.athletes if a.source == source),
                              key=lambda a: (a.added_at, _id_num(a.id)), reverse=True)
            for a in pool:
                if team:
                    if team in ("none", "unattached"):
                        if a.team_id is not None:
                            continue
                    elif a.team_id != team:
                        continue
                if gender and a.gender != gender.upper():
                    continue
                pairs = idx.get(a.id, [])
                if key is not None and not any(r.key == key for r, _ in pairs):
                    continue
                st = _status(pairs)
                if roster == "1" and st != "roster":
                    continue
                if roster == "0" and st == "roster":
                    continue
                if q:
                    if qbib is not None:
                        if bib_number(a.bib) != qbib:
                            continue
                    elif qn not in idmatch.norm(f"{a.first} {a.last}") and \
                            qn not in idmatch.norm(f"{a.last} {a.first}"):
                        continue
                rows.append(a)
            if sort:
                if sort not in ATHLETE_SORTS:
                    raise MeetError(f"sort must be one of {', '.join(ATHLETE_SORTS)}", 400)
                key = _athlete_sort_key(meet, idx, sort)
                keyed = [(key(a), a) for a in rows]
                rows = [a for _, a in sorted((p for p in keyed if p[0] is not None),
                                             key=lambda p: p[0], reverse=bool(desc))]
                rows += [a for k, a in keyed if k is None]
            missing = 0
            if nobib_first:
                # Runners without a bib (the "no bib" count) on top: by team,
                # gender, name - or by the clicked column, like the rest; a
                # bib given moves them into the rest (2026-10-08, user)
                need = [a for a in rows if not a.bib and _status(idx.get(a.id)) in ("entered", "exhibition")]
                if not sort:
                    teams = {t.id: idmatch.norm(t.short or t.name) for t in meet.teams}
                    need.sort(key=lambda a: (a.team_id is None, teams.get(a.team_id, ""), a.gender or "~",
                                             idmatch.norm(a.last), idmatch.norm(a.first)))
                ids = {a.id for a in need}
                rows = need + [a for a in rows if a.id not in ids]
                missing = len(need)
            total = len(rows)
            offset = max(0, int(offset or 0))
            page = rows[offset:offset + int(limit)] if limit else rows[offset:]
            return {"athletes": [athlete_view(meet, a, idx) for a in page], "total": total,
                    "nobib_total": missing}

    def search(self, first: str = "", last: str = "", team: Optional[str] = None) -> List[dict]:
        with self._lock:
            meet = self._require()
            team_id = ... if team in (None, "") else (None if team in ("none", "unattached") else team)
            hits = edits.search_team(meet, team_id, first, last)
            idx = _entry_index(meet)
            return [athlete_view(meet, meet.athlete(h["athlete_id"]), idx) for h in hits]

    def _enter(self, meet: Meet, a: Athlete, rk, event=None, status: str = "entered") -> Optional[Race]:
        if rk in (None, ""):
            return None
        race = self._race(meet, rk)
        if race.status == "final":
            raise MeetError(f"race {rk_of(race.key)} is final: reopen it first", 409)
        ev = race.event if event in (None, "") else _int_in(event, 1, 10 ** 6, "event")
        if meet.event(ev) is None:
            raise MeetError(f"no event {ev}", 400)
        if status not in ENTRY_STATUSES:
            raise MeetError(f"status must be one of {', '.join(ENTRY_STATUSES)}", 400)
        race.entries.append(Entry(a.id, ev, status=status))
        return race

    def add_athlete(self, d: dict) -> Tuple[dict, Optional[dict], dict]:
        def change(meet: Meet):
            first, last = str(d.get("first") or "").strip(), str(d.get("last") or "").strip()
            if not (first and last):
                raise MeetError("first and last name are needed", 400)
            gender = str(d.get("gender") or "").strip().upper()
            if gender not in ("M", "F"):
                raise MeetError("gender is needed (M or F)", 400)
            team_id = d.get("team_id") or None
            if team_id is not None and meet.team(team_id) is None:
                raise MeetError(f"no team {team_id}", 400)
            same = None if d.get("force") else edits.same_person(meet, team_id, first, last)
            if same is not None:                   # on the roster already: use them (2026-10-08)
                raise _same_person_error(meet, edits.SamePerson(same))
            gy, gt = _grade_fields(d.get("grade"))
            a = Athlete(meet.new_athlete_id(), first, last, gender, team_id=team_id,
                        grad_year=gy, grade_text=gt, source="local", added_at=_now())
            meet.athletes.append(a)
            self._enter(meet, a, d.get("race"), d.get("event"))
            if str(d.get("bib") or "").strip():
                self._set_bib(meet, a.id, d["bib"])
                bibcheck.follow(meet, {}, [a.id])           # a spare handed out (2026-10-10)
            match = self._match_one(meet, a)
            return a.id, match
        (aid, match), summary = self._mutate(change, undo="add athlete")
        with self._lock:
            view = athlete_view(self.meet, self.meet.athlete(aid))
        logger.info(f"Meet: athlete {aid} {view['first']} {view['last']} added"
                    + (f" (MileSplit id {view['milesplit_id']})" if view["milesplit_id"] else ""))
        return view, match, summary

    def update_athlete(self, aid: str, d: dict) -> Tuple[dict, dict]:
        def change(meet: Meet):
            a = self._athlete(meet, aid)
            for k in ("first", "last"):
                if k in d:
                    v = str(d[k] or "").strip()
                    if not v:
                        raise MeetError(f"{k} name can't be blank", 400)
                    setattr(a, k, v)
            if "gender" in d:
                g = str(d["gender"] or "").strip().upper()
                if g not in ("M", "F"):
                    raise MeetError("gender must be M or F", 400)
                a.gender = g
            if "grade" in d:
                a.grad_year, a.grade_text = _grade_fields(d["grade"])
            if "team_id" in d:
                tid = d["team_id"] or None
                if tid is not None and meet.team(tid) is None:
                    raise MeetError(f"no team {tid}", 400)
                a.team_id = tid
            if "unattached" in d:
                a.unattached = bool(d["unattached"])
        summary = self._mutate(change, undo="edit athlete")[1]
        return self._view(aid), summary

    def set_entry(self, aid: str, d: dict) -> Tuple[dict, dict]:
        """Move race / scratch / exhibition / un-enter (MEET-API.md). race
        null = out of every race; a race already holding the athlete keeps
        the entry and only changes event/status; another race = moved."""
        def change(meet: Meet):
            a = self._athlete(meet, aid)
            pairs = meet.entries_for(a.id)
            rk = d.get("race")
            status = d.get("status")
            if status is not None and status not in ENTRY_STATUSES:
                raise MeetError(f"status must be one of {', '.join(ENTRY_STATUSES)}", 400)
            target = self._race(meet, rk) if rk not in (None, "") else None
            for r, _ in pairs:
                if r.status == "final" and r is not target:
                    raise MeetError(f"{a.first} {a.last} is in {lynx.race_name(meet, r)}, which "
                                    "is final: reopen it first", 409)
            if target is not None and target.status == "final":
                raise MeetError(f"{lynx.race_name(meet, target)} is final: reopen it first", 409)
            here = next((e for r, e in pairs if r is target), None)
            if target is not None and here is not None:
                if "event" in d and d["event"] not in (None, ""):
                    ev = _int_in(d["event"], 1, 10 ** 6, "event")
                    if meet.event(ev) is None:
                        raise MeetError(f"no event {ev}", 400)
                    here.event = ev
                if status:
                    here.status = status
                return
            old_status = pairs[0][1].status if pairs else "entered"
            for r, e in pairs:
                r.entries.remove(e)
            if target is not None:
                self._enter(meet, a, rk, d.get("event"), status or
                            (old_status if old_status != "scratched" else "entered"))
        summary = self._mutate(change, undo="change entry")[1]
        return self._view(aid), summary

    def delete_athlete(self, aid: str) -> dict:
        def change(meet: Meet):
            a = self._athlete(meet, aid)
            finals = [r for r, _ in meet.entries_for(a.id) if r.status == "final"]
            if finals:
                raise MeetError(f"{a.first} {a.last} is in {lynx.race_name(meet, finals[0])}, "
                                "which is final", 409)
            for r in meet.races:
                r.entries = [e for e in r.entries if e.athlete_id != a.id]
            meet.athletes.remove(a)
        return self._mutate(change, undo="delete athlete")[1]

    def undo(self) -> dict:
        with self._lock:
            self._require()
            if not self._undo:
                raise MeetError("nothing to undo", 409)
            label, before = self._undo
            self._undo = None
            self.meet = Meet.from_dict(before)
            summary = self._commit_locked()
        logger.info(f"Meet: undo {label}")
        self._post(summary)
        return summary

    def _view(self, aid: str) -> dict:
        with self._lock:
            meet = self._require()
            return athlete_view(meet, self._athlete(meet, aid))

    # ------------------------------------------------------------ bibs

    def _set_bib(self, meet: Meet, aid: str, bib) -> dict:
        self._athlete(meet, aid)
        res = bibs.set_bib(meet, aid, bib)
        if res.ok:
            return res.to_dict()
        new, _ = bibs.normalize_bib(bib)
        holder = meet.athlete_by_bib(new) if new else None
        if holder is not None and holder.id != aid:
            raise MeetError(res.error, 409, holder=athlete_view(meet, holder),
                            swap={"a": aid, "b": holder.id})
        raise MeetError(res.error, 400)

    def set_bib(self, aid: str, bib) -> Tuple[dict, dict]:
        def change(meet: Meet):
            before = {aid: (meet.athlete(aid).bib if meet.athlete(aid) else "")}
            res = self._set_bib(meet, aid, bib)
            bibcheck.follow(meet, before, [aid])          # a bib typed at the table (2026-10-10)
            return res
        return self._mutate(change, undo="bib")

    def swap_bibs(self, a: str, b: str) -> Tuple[dict, dict]:
        def change(meet: Meet):
            before = {x.id: x.bib for x in meet.athletes if x.id in (str(a), str(b))}
            res = bibs.swap_bibs(meet, str(a), str(b))
            if not res.ok:
                raise MeetError(res.error, 404 if "unknown" in res.error else 400)
            bibcheck.follow(meet, before, [str(a), str(b)])
            return res.to_dict()
        return self._mutate(change, undo="swap bibs")

    def bib_context(self, bib: str) -> dict:
        """edits.bib_context + next_free: what the holder would get with
        holder_action next_free (the bib asked about is skipped, as
        edits._settle_holder skips it), so the popover can say "next free
        bib (105)" before anyone commits (2026-10-03)."""
        with self._lock:
            meet = self._require()
            ctx = edits.bib_context(meet, bib)
            ctx["next_free"] = _bib_str(bibs.next_free(meet, skip=[ctx["bib"]]))
            return ctx

    def next_free_bib(self) -> Optional[str]:
        """The next bib assign would hand out (meet ranges, held bibs
        skipped); None without ranges or when they are used up."""
        with self._lock:
            return _bib_str(bibs.next_free(self._require()))

    def wearer(self, bib: str, d: dict) -> Tuple[dict, Optional[dict], dict]:
        """Someone else is wearing `bib`: an athlete already in the meet
        (athlete_id) or a new one (new: {...}). A field left out takes the
        holder's prefill; null / "" leaves it blank (edits.add_wearer)."""
        action = d.get("holder_action") or "next_free"

        def race_arg(src: dict):
            if "race" not in src:
                return ...
            return list(parse_rk(src["race"])) if src["race"] not in (None, "") else None

        def event_arg(src: dict):
            if "event" not in src:
                return ...
            return None if src["event"] in (None, "") else _int_in(src["event"], 1, 10 ** 6, "event")

        def change(meet: Meet):
            if d.get("athlete_id"):
                wearer = self._athlete(meet, d["athlete_id"])
                before = {wearer.id: wearer.bib}
                rep = edits.give_bib_to(meet, bib, str(d["athlete_id"]), race=race_arg(d),
                                        event=event_arg(d), holder_action=action)
                bibcheck.follow(meet, before, [wearer.id])  # they wore it (2026-10-10)
                return rep.to_dict(), None
            new = d.get("new")
            if not isinstance(new, dict):
                raise MeetError("give athlete_id or new: {first, last, ...}", 400)
            kw = {"race": race_arg(new), "event": event_arg(new), "holder_action": action}
            if "gender" in new:
                kw["gender"] = new["gender"]
            if "team_id" in new:
                kw["team_id"] = new["team_id"] or None
            if "grade" in new:
                kw["grad_year"], kw["grade_text"] = _grade_fields(new["grade"])
            try:
                before = {x.id: x.bib for x in meet.athletes}
                rep = edits.add_wearer(meet, bib, str(new.get("first") or ""),
                                       str(new.get("last") or ""),
                                       allow_same=bool(new.get("force") or d.get("force")), **kw)
            except edits.SamePerson as e:
                raise _same_person_error(meet, e) from None
            bibcheck.follow(meet, before, [rep.athlete_id])
            match = None
            if rep.created:
                added = meet.athlete(rep.athlete_id)
                added.added_at = _now()            # a walk-up too (source local)
                match = self._match_one(meet, added)
            return rep.to_dict(), match
        (report, match), summary = self._mutate(change, undo="bib wearer")
        return report, match, summary

    def assign(self, d: dict) -> Tuple[dict, Optional[dict]]:
        rows = d.get("rows")
        ranges = bibs.parse_range_rows(rows) if rows else None
        kw = {"sort": tuple(d.get("sort") or bibs.DEFAULT_SORT),
              "retain_existing": bool(d.get("retain", True)),
              "per_team_blocks": d.get("per_team_blocks") or None,
              "entered_only": d.get("entered_only"),
              "athlete_ids": d.get("athlete_ids"),
              "skip": d.get("skip") or ()}
        if d.get("dry_run"):
            with self._lock:
                rep = bibs.assign(self._require(), ranges=ranges, dry_run=True, **kw)
            return rep.to_dict(), None
        # the other PCs' bibs first, so the next free number is free on
        # every PC (2026-10-10); the new ones go out as soon as they're saved
        self._sync_round()

        def change(meet: Meet):
            if ranges is not None:
                meet.bib_ranges = ranges
            return bibs.assign(meet, **kw).to_dict()
        return self._mutate(change, undo="assign bibs")

    def clear_bibs(self, d: dict) -> Tuple[dict, dict]:
        ids = d.get("athlete_ids")
        if not d.get("all") and not ids:
            raise MeetError("give athlete_ids or all: true", 400)
        return self._mutate(lambda m: bibs.clear(m, None if d.get("all") else list(ids)).to_dict(),
                            undo="clear bibs")

    def conflicts(self) -> dict:
        with self._lock:
            return bibs.conflicts(self._require()).to_dict()

    # ------------------------------------------------------------ bib check

    def _bibcheck_locked(self) -> dict:
        """bibcheck.check against the lynx.ppl in the folder FinishLynx reads
        (parsed again only when the file moved). Not read when the meet was
        opened without FinishLynx or in preview: nothing is written there."""
        meet = self._require()
        ppl, path = None, ""
        directory = self._start_lists.get("dir") or self._rfid_dir()
        if directory and not self.file_only and not self.preview:
            path = bibcheck.ppl_path(directory)
            try:
                st = os.stat(path)
                sig = (path, st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig is not None:
                if self._ppl_seen is None or self._ppl_seen[0] != sig:
                    self._ppl_seen = (sig, bibcheck.read_ppl(path))
                ppl = self._ppl_seen[1]
        return bibcheck.check(meet, ppl, path)

    def bib_check(self) -> dict:
        with self._lock:
            return self._bibcheck_locked()

    def record_handed_out(self, team_ids=None) -> int:
        """Rosters / team bib lists / competitor labels printed: the runners
        on them (team_ids None = everyone) were handed their current bibs,
        for runners not recorded yet. Never stops the printout."""
        with self._lock:
            meet = self.meet
            if meet is None:
                return 0
            want = None
            if team_ids:
                teams = set(team_ids)
                want = [a.id for a in meet.athletes if a.team_id in teams and meet.entered(a.id)]
            if not any(a.bib and not a.printed_bib for a in meet.athletes
                       if want is None or a.id in set(want)):
                return 0
        n, _ = self._mutate(lambda m: bibcheck.record(m, want), check=False)
        if n:
            logger.info(f"Bib check: {n} runner(s) recorded as handed their bibs (printout)")
        return n

    def fix_bibs(self, d: dict) -> Tuple[dict, dict]:
        """The bib check's fixes (2026-10-10, user: an easy way to address
        what it flags, not just a flag). action:
          restore  put the printed numbers back (athlete_ids, default every
                   runner whose bib changed); a runner holding one of them
                   who wasn't handed it loses it
          accept   the current bibs are the ones handed out (athlete_ids,
                   default every changed runner; all: true = every runner)
          write    write FinishLynx's start lists (lynx.ppl/.evt) again"""
        action = d.get("action")
        ids = d.get("athlete_ids")
        if ids is not None and not isinstance(ids, list):
            raise MeetError("athlete_ids must be a list", 400)
        if action == "restore":
            def change(meet: Meet):
                rep = bibcheck.restore(meet, ids)
                if not rep["restored"]:
                    raise MeetError("nothing to put back: no runner's bib differs from the printed one", 409)
                return rep
            return self._mutate(change, undo="put back printed bibs")
        if action == "accept":
            def change(meet: Meet):
                if d.get("all"):
                    return {"recorded": bibcheck.record(meet, None, overwrite=True)}
                changed = [c["athlete_id"] for c in bibcheck.check(meet)["changed"]]
                pick = changed if ids is None else [i for i in ids if i in set(changed)]
                return {"recorded": bibcheck.record(meet, pick, overwrite=True)}
            return self._mutate(change, check=False, undo="accept bibs")
        if action == "write":
            if self.file_only:
                raise MeetError("this meet was opened without FinishLynx: no start lists are written", 409)
            with self._lock:
                self._require()
                self._write_start_lists_locked()
                if self._start_lists.get("errors"):
                    raise MeetError("start lists not written: " + self._start_lists["errors"][0], 500)
                summary = self._summary_locked()
            self._post(summary)
            return {"written": self._start_lists.get("dir")}, summary
        raise MeetError("action must be restore, accept or write", 400)

    # ------------------------------------------------------------ id review

    def idreview(self) -> dict:
        with self._lock:
            meet = self._require()
            queue = idmatch.review_queue(meet, self._pool(meet))
            return {"queue": [q.to_dict() for q in queue],
                    "coverage": idmatch.id_coverage(meet).to_dict(),
                    "by_race": {rk_of(k): c.to_dict()
                                for k, c in idmatch.coverage_by_race(meet).items()}}

    def approve(self, aid: str, candidate) -> dict:
        def change(meet: Meet):
            self._athlete(meet, aid)
            if not isinstance(candidate, dict):
                raise MeetError("candidate is needed", 400)
            try:
                idmatch.approve(meet, aid, candidate)
            except idmatch.IdConflict as e:
                raise MeetError(str(e), 409) from None
        return self._mutate(change)[1]

    def decline(self, aid: str) -> dict:
        def change(meet: Meet):
            self._athlete(meet, aid)
            idmatch.decline(meet, aid)
        return self._mutate(change)[1]

    # ------------------------------------------------------------ races

    def apply_synced(self, meet_id: str, merge) -> Optional[bool]:
        """Meet sync (meet_sync.py, 2026-10-07): another machine's changes to
        the meet OPEN here. merge(current snapshot) -> new snapshot or None;
        the merge runs under our lock so no edit slips in between, then the
        meet is reloaded as Open does. None = not the open meet (the sync
        writes the store itself); False = a race is running: not now."""
        from . import meet_sync
        if self.meet is None or self.meet.meet.id != meet_id:
            return None
        mgr = self._mgr()
        links = self._pane_links()
        if mgr is not None and any((mgr.races.get(k) is not None and mgr.races[k].status == "racing")
                                   for k in links.values()):
            return False
        with self._lock:
            if self.meet is None or self.meet.meet.id != meet_id:
                return None
            new = merge(meet_sync.read_snapshot(self.root, meet_id))
            if new is None:
                return True
            meet_sync.write_snapshot(self.root, meet_id, new)
            self._set_meet_locked(self.store.load(meet_id))    # same meet: Live keeps running
            self._undo = None                      # an undo would also take back their changes
            for kind in ("entries", "rosters"):
                try:
                    data = self.store.load_pull(meet_id, kind)
                except (OSError, ValueError):
                    data = None
                if data is not None:
                    self._pulls[kind] = data
            try:
                with open(os.path.join(self.root, meet_id, UPLOADS_FILE), encoding="utf-8") as f:
                    self._uploads = json.load(f)
            except (OSError, ValueError):
                pass
            summary = self._commit_locked()
        logger.info(f"Meet sync: {meet_id} updated from another machine")
        self._post(summary)
        return True

    def _pane_links(self) -> Dict[tuple, int]:
        """(event, round, heat) -> RaceManager pane key, first pane in the
        console's order when several share one (find_by_event ignores the
        round, so it can't be used here)."""
        self._assert_unlocked()
        mgr = self._mgr()
        if mgr is None:
            return {}
        out: Dict[tuple, int] = {}
        with mgr._lock:
            for k in list(mgr.order) + list(mgr.races):
                r = mgr.races.get(k)
                if r is not None:
                    out.setdefault((int(r.event), int(r.round or 1), int(r.heat or 1)), k)
        return out

    def _pane_status(self, pane: Optional[int]) -> Optional[str]:
        mgr = self._mgr()
        if mgr is None or pane is None:
            return None
        r = mgr.races.get(pane)
        return r.status if r is not None else None

    def _rm_results(self, pane: Optional[int]) -> Optional[dict]:
        self._assert_unlocked()
        mgr = self._mgr()
        if mgr is None or pane is None:
            return None
        try:
            return mgr.results(pane)
        except KeyError:
            return None

    def pane_divisions(self) -> Dict[str, dict]:
        """Race panes that hold several scoring events (run together, or
        girls and boys scored apart): each bib's event and its place in that
        event, for the pane's race tags and "race place" column (2026-10-05,
        user). {pane: {"events": {num: short name}, "bibs": {bib: [num, place]}}}"""
        links = self._pane_links()
        if self.meet is None:
            return {}
        multi = {ident: k for ident, k in links.items()
                 if (r := self.meet.race(*ident)) is not None and len({e.event for e in r.entries}) > 1}
        rms = {ident: self._rm_results(k) for ident, k in multi.items()}
        out = {}
        with self._lock:
            meet = self.meet
            if meet is None:
                return {}
            for ident, pane in multi.items():
                race = meet.race(*ident)
                if race is None:
                    continue
                nums = sorted({e.event for e in race.entries})
                names = [meet.event(n).name if meet.event(n) else f"Event {n}" for n in nums]
                res = race.results if race.status == "final" else \
                    self._live(ident, pane, rms.get(ident))["results"]
                place = {str(f.get("bib")): f.get("event_place") for f in (res or {}).get("finishers", [])
                         if f.get("status") == "finished"}
                bibs = {b: [n, place.get(b)] for b, n in _bib_events(meet, race).items()}
                out[str(pane)] = {"events": dict(zip(map(str, nums), _short_labels(names))), "bibs": bibs}
        return out

    def races(self) -> List[dict]:
        links = self._pane_links()
        rms = {ident: self._rm_results(k) for ident, k in links.items()}
        pane_status = {ident: self._pane_status(k) for ident, k in links.items()}
        with self._lock:
            meet = self._require()
            out = []
            for race in meet.races_in_order():
                pane = links.get(race.key)
                rm = rms.get(race.key)
                nums = sorted({e.event for e in race.entries if e.status != "scratched"}) or [race.event]
                status = "final" if race.status == "final" else (pane_status.get(race.key) or race.status)
                # per division (a run-together race is listed once per division
                # on the Results page, 2026-10-05)
                ev_of = _bib_events(meet, race)
                names = [meet.event(n).name if meet.event(n) else f"Event {n}" for n in nums]
                up = self._uploads.get(rk_of(race.key)) or {}
                out.append({
                    "rk": rk_of(race.key), "event": race.event, "round": race.round,
                    "heat": race.heat, "name": lynx.race_name(meet, race), "order": race.order,
                    "status": status, "meet_status": race.status,
                    "divisions": [{"event": n, "name": name, "short": short,
                                   "entries": sum(1 for e in race.entries
                                                  if e.event == n and e.status != "scratched"),
                                   "finished": sum(1 for f in rm.get("finishers", [])
                                                   if ev_of.get(str(f.get("bib"))) == n) if rm else None}
                                  for n, name, short in zip(nums, names, _short_labels(names))],
                    "split_genders": edits.is_split_by_gender(meet, race),
                    "run_together": edits.is_run_together(meet, race),
                    # flagged runners left out of its results (2026-10-06)
                    "issues": len(((race.results if race.status == "final" else
                                    (self._live(race.key, pane, rm)["results"] if rm else None))
                                   or {}).get("issues") or []),
                    # the meet's divisions this race is scored in (2026-10-06);
                    # "divisions" above are its scoring events
                    "division_ids": [x.id for x in divisions.for_race(meet, race)],
                    "entries": sum(1 for e in race.entries if e.status != "scratched"),
                    "scratched": sum(1 for e in race.entries if e.status == "scratched"),
                    "on_course": len(rm.get("on_course", [])) if rm else None,
                    "finished": len(rm.get("finishers", [])) if rm else None,
                    "pane_key": pane,
                    "start_time": race.start_time, "start_time_text": start_time_text(race),
                    # last mpResults upload (Results page "Last sent", 2026-10-04)
                    "uploaded_at": up.get("uploaded_at"), "upload_ok": up.get("ok")})
            return out

    def race_order(self, order: List[str]) -> dict:
        def change(meet: Meet):
            keys = [parse_rk(rk) for rk in order or []]
            if len(set(keys)) != len(keys):
                raise MeetError("a race is listed twice", 400)
            listed = [self._race(meet, k) for k in keys]
            rest = [r for r in meet.races_in_order() if r not in listed]
            for i, r in enumerate(listed + rest, 1):
                r.order = i
        return self._mutate(change)[1]

    def update_race(self, rk: str, d: dict) -> dict:
        def change(meet: Meet):
            race = self._race(meet, rk)
            if "name" in d:
                race.name = str(d["name"] or "").strip()
            if "start_time" in d:
                race.start_time = _start_time(d["start_time"])
            if "divisions" in d:
                race.divisions = _division_ids(meet, d["divisions"])
        # A start time or the divisions aren't in lynx.evt/.sch: no rewrite
        # of what FinishLynx reads for them (the pusher still hears about it
        # through _post).
        return self._mutate(change, write=not set(d) <= {"start_time", "divisions"})[1]

    def set_race_divisions(self, ids, rks: Optional[List[str]] = None) -> dict:
        """The same divisions for several races at once ("every race" in a
        race's Divisions dialog, 2026-10-06); rks None = every race."""
        def change(meet: Meet):
            picked = _division_ids(meet, ids)
            races = meet.races if rks is None else [self._race(meet, rk) for rk in rks]
            for race in races:
                race.divisions = list(picked)
        return self._mutate(change, write=False)[1]

    def fill_start_times(self, d: dict) -> Tuple[int, dict]:
        """"Fill times…": the k-th race in run order (0-based) gets first +
        k * every_min, so a time already set never shifts the others. Only
        races without a time unless overwrite. 400 when the times would run
        past midnight. Returns (races changed, summary). (2026-10-03)"""
        first = _start_time(d.get("first"))
        if not first:
            raise MeetError("give the first race's start time", 400)
        every = _int_in(d.get("every_min") or 0, 0, 600, "every_min")
        overwrite = bool(d.get("overwrite"))
        base = int(first[:2]) * 60 + int(first[3:])

        def change(meet: Meet):
            n = 0
            races = meet.races_in_order()
            last = base + every * (len(races) - 1) if races else base
            if last >= 24 * 60:
                raise MeetError(f"{len(races)} races every {every} min from "
                                f"{time_text(first)} run past midnight", 400)
            for k, race in enumerate(races):
                if race.start_time and not overwrite:
                    continue
                t = base + every * k
                new = f"{t // 60:02d}:{t % 60:02d}"
                if race.start_time != new:
                    race.start_time = new
                    n += 1
            return n
        return self._mutate(change, write=False)

    def add_race(self, d: dict) -> Tuple[str, dict]:
        """A race that isn't in the entries (an extra heat, an open race):
        for an existing scoring event (`event`) or a new one (`new`), last in
        the run order. Its race pane follows by itself (_post -> _sync_panes)
        and the start lists are rewritten. (2026-10-03)"""
        def change(meet: Meet):
            new = d.get("new")
            if isinstance(new, dict):
                name = str(new.get("name") or "").strip()
                if not name:
                    raise MeetError("a new event needs a name", 400)
                gender = str(new.get("gender") or "X").strip().upper()
                if gender not in ("M", "F", "X"):
                    raise MeetError("gender must be M, F or X (mixed)", 400)
                num = _int_in(d["event"], 1, 10 ** 6, "event") if d.get("event") not in (None, "") \
                    else max((e.num for e in meet.events), default=0) + 1
                if meet.event(num) is not None:
                    raise MeetError(f"event {num} already exists", 409)
                dist = new.get("distance_m")
                meet.events.append(Event(num, name, gender=gender,
                                         distance_m=_int_in(dist, 0, 10 ** 6, "distance_m")
                                         if dist not in (None, "") else 0,
                                         division=str(new.get("division") or "").strip()))
            else:
                if d.get("event") in (None, ""):
                    raise MeetError("give event (an existing event number) or new: {name, ...}", 400)
                num = _int_in(d["event"], 1, 10 ** 6, "event")
                if meet.event(num) is None:
                    raise MeetError(f"no event {num}", 400)
            rnd = _int_in(d.get("round") or 1, 1, 99, "round")
            if d.get("heat") not in (None, ""):
                heat = _int_in(d["heat"], 1, 999, "heat")
            else:              # the next heat of that event and round
                heat = max((r.heat for r in meet.races if (r.event, r.round) == (num, rnd)),
                           default=0) + 1
            if meet.race(num, rnd, heat) is not None:
                raise MeetError(f"race {num}-{rnd}-{heat} already exists", 409)
            race = Race(num, rnd, heat, order=max((r.order for r in meet.races), default=0) + 1,
                        name=str(d.get("name") or "").strip(),
                        start_time=_start_time(d.get("start_time")))
            meet.races.append(race)
            return rk_of(race.key)
        rk, summary = self._mutate(change)
        logger.info(f"Meet: race {rk} added")
        return rk, summary

    def cancel_race(self, rk: str) -> dict:
        """Take a race out of the meet before it is run: its entries become
        not entered (the athletes stay), its pane goes if it never got a
        read. Refused (409) once anything happened: finishers, frozen
        results, a gun, or a pane holding reads - those must not vanish
        with a click. An event no race or entry uses any more goes too
        (a "+ New race" made by mistake). (2026-10-03)"""
        ident = parse_rk(rk)
        pane, mgr = self._refuse_if_run(ident, "it can't be cancelled")

        def change(meet: Meet):
            race = self._race(meet, ident)
            if race.status == "final" or race.results is not None:
                raise MeetError(f"{lynx.race_name(meet, race)} has results: it can't be cancelled",
                                409)
            meet.races.remove(race)
            gone = {race.event} | {e.event for e in race.entries}
            used = {r.event for r in meet.races} | {e.event for r in meet.races for e in r.entries}
            meet.events = [e for e in meet.events if e.num in used or e.num not in gone]
            return len(race.entries)
        n, summary = self._mutate(change)
        if mgr is not None and pane is not None:
            try:
                mgr.delete_race(pane)
            except KeyError:
                pass
        logger.info(f"Meet: race {rk_of(ident)} cancelled ({n} entries now not entered)")
        return summary

    def _refuse_if_run(self, ident, what: str):
        """409 once the race's pane holds reads or has started: those must
        not vanish with a click. Returns (pane key, RaceManager)."""
        pane = self._pane_links().get(ident)
        mgr = self._mgr()
        if mgr is not None and pane is not None:
            with mgr._lock:
                p = mgr.races.get(pane)
                reads = sum(1 for r in mgr.reads.values() if r.race == pane)
                started = p is not None and (p.gun is not None or p.status in ("racing", "final"))
            if reads or started:
                raise MeetError(f"race {rk_of(ident)} has {'reads' if reads else 'started'}: "
                                f"{what} (delete its pane's reads first)", 409)
        return pane, mgr

    def clear_entries(self, rk: str) -> dict:
        """Empty a race before it is run (2026-10-04, race menu "Clear
        entries"): its athletes stay in the meet, not entered, bibs kept;
        the start lists are rewritten. Refused like cancel_race once the
        race has reads, a gun or results. One /api/meet/undo takes it back."""
        ident = parse_rk(rk)
        self._refuse_if_run(ident, "its entries can't be cleared")

        def change(meet: Meet):
            race = self._race(meet, ident)
            if race.status == "final" or race.results is not None:
                raise MeetError(f"{lynx.race_name(meet, race)} has results: its entries can't be "
                                "cleared", 409)
            n = len(race.entries)
            race.entries = []
            return n
        n, summary = self._mutate(change, undo=f"Cleared {rk_of(ident)}'s entries")
        logger.info(f"Meet: race {rk_of(ident)} entries cleared ({n} now not entered)")
        return summary

    def split_genders(self, rk: str, on: bool) -> dict:
        def change(meet: Meet):
            race = self._race(meet, rk)
            if race.status == "final":
                raise MeetError("the race is final: reopen it before changing how it is scored", 409)
            rep = edits.split_genders(meet, race) if on else edits.combine_genders(meet, race)
            return rep.to_dict()
        return self._mutate(change)[1]

    def run_together(self, rk: str, others: List[str]) -> Tuple[dict, dict]:
        """Run `others` at the same time as `rk`, scored separately
        (edits.run_together): their entries join this race, the races go,
        the start lists carry one Lynx event. The folded-in races must not
        have been run yet (reads or a gun); their empty panes go."""
        ident = parse_rk(rk)
        idents = [parse_rk(o) for o in others or []]
        if not idents:
            raise MeetError("pick at least one other race", 400)
        if ident in idents or len(set(idents)) != len(idents):
            raise MeetError("a race is listed twice", 400)
        panes = [self._refuse_if_run(i, "it can't be run with another race")[0] for i in idents]

        def change(meet: Meet):
            target = self._race(meet, ident)
            try:
                return edits.run_together(meet, target, [self._race(meet, i) for i in idents]).to_dict()
            except ValueError as e:
                raise MeetError(str(e), 409)
        rep, summary = self._mutate(change)
        mgr = self._mgr()
        if mgr is not None:
            for pane in panes:
                if pane is not None:
                    try:
                        mgr.delete_race(pane)
                    except KeyError:
                        pass
        logger.info(f"Meet: {', '.join(rk_of(i) for i in idents)} run together with "
                    f"{rk_of(ident)} ({rep['moved']} entries moved, scored separately)")
        return rep, summary

    def run_separately(self, rk: str) -> Tuple[dict, dict]:
        """Undo run_together before the race is run (reads or a gun refuse
        it: those reads belong to the one pane)."""
        ident = parse_rk(rk)
        self._refuse_if_run(ident, "its races can't be run separately")

        def change(meet: Meet):
            try:
                return edits.run_separately(meet, self._race(meet, ident)).to_dict()
            except ValueError as e:
                raise MeetError(str(e), 409)
        rep, summary = self._mutate(change)
        logger.info(f"Meet: {rk_of(ident)} run separately again ({', '.join(rep['removed'])})")
        return rep, summary

    # ------------------------------------------------------------ results

    def _live(self, ident: tuple, pane: Optional[int], rm: Optional[dict]) -> dict:
        """Live (unfrozen) results + what would stop a freeze, cached per
        race until RaceManager or the meet changes. Caller holds the lock."""
        hit = self._live_cache.get(ident)
        if hit is not None and hit["pane"] == pane:
            return hit
        race = self.meet.race(*ident)
        if rm is None or race is None:
            entry = {"pane": pane, "results": None, "unresolved": []}
        else:
            _, warnings = scoring.build_results(self.meet, race, rm, final=True)
            entry = {"pane": pane, "results": scoring.live_scores(self.meet, race, rm),
                     "unresolved": scoring.unresolved(warnings)}
        self._live_cache[ident] = entry
        return entry

    def race_results(self, rk: str) -> dict:
        ident = parse_rk(rk)
        links = self._pane_links()
        pane = links.get(ident)
        rm = self._rm_results(pane)
        pstatus = self._pane_status(pane)
        with self._lock:
            meet = self._require()
            race = self._race(meet, ident)
            official = scoring.official_results(race)
            if official is not None:
                results, unresolved = official, []
            else:
                live = self._live(ident, pane, rm)
                results, unresolved = live["results"], live["unresolved"]
            up = self._uploads.get(rk_of(ident)) or {}
            # Division standings (2026-10-06): worked out from these results,
            # never stored; [] for a race with no divisions.
            div_rows = [{"id": dv.id, "name": dv.name, "team_scores": dv.team_scores,
                         "text": divisions.describe(dv, meet), "results": dres}
                        for dv, dres in divisions.race_results(meet, race, results)]
            issue_rows = self._issue_rows(meet, race, results)
            return {"rk": rk_of(ident),
                    "divisions": div_rows,
                    # flagged runners, left out of the results (2026-10-06)
                    "issues": issue_rows,
                    "ignored": [dict(x, name=(lambda a: f"{a.last}, {a.first}" if a else "")(
                        meet.athlete(x["athlete_id"]) if x.get("athlete_id") else None))
                        for x in (results or {}).get("ignored") or []],
                    "status": "final" if race.status == "final" else (pstatus or race.status),
                    "official": official is not None, "results": results,
                    "unresolved": unresolved, "team_scores": (results or {}).get("teams", {}),
                    "split_labels": (results or {}).get("split_labels", []),
                    "pane_key": pane,
                    "not_entered": [{k: i[k] for k in ("bib", "athlete_id", "name", "team", "gender",
                                                       "blocked")} | {"from": i["entered_in"]}
                                    for i in issue_rows if i["code"] == "not_entered"],
                    "milesplit": {"uploaded_at": up.get("uploaded_at"), "ok": up.get("ok"),
                                  "message": up.get("message", "")}}

    def switch_entries(self, rk: str, aids: List[str], why: str = "by hand") -> dict:
        """Move these athletes into race `rk` and out of every other race
        (exhibition stays exhibition, a scratch becomes entered): the close
        dialog's "Switch", and the automatic switch for runners FinishLynx
        timed here. One undo. Athletes in a final race elsewhere are left
        where they are and reported."""
        ident = parse_rk(rk)
        report = {"switched": [], "skipped": []}

        def change(meet: Meet):
            target = self._race(meet, ident)
            if target.status == "final":
                raise MeetError(f"{lynx.race_name(meet, target)} is final: reopen it first", 409)
            for aid in aids or []:
                a = self._athlete(meet, aid)
                pairs = meet.entries_for(a.id)
                if any(r is target for r, _ in pairs):
                    continue
                final = next((r for r, _ in pairs if r.status == "final"), None)
                if final is not None:
                    report["skipped"].append({"athlete_id": a.id, "reason": f"{lynx.race_name(meet, final)} is final"})
                    continue
                wrong = _gender_block(meet, target, a)
                if wrong:
                    report["skipped"].append({"athlete_id": a.id, "reason": wrong})
                    continue
                status = next((e.status for _, e in pairs if e.status != "scratched"), "entered")
                for r, e in pairs:
                    r.entries.remove(e)
                self._enter(meet, a, ident, None, status)
                report["switched"].append({"athlete_id": a.id, "bib": a.bib, "name": f"{a.first} {a.last}",
                                           "from": [lynx.race_name(meet, r) for r, _ in pairs]})
        if not aids:
            return dict(report, meet=self.summary())
        summary = self._mutate(change, undo="switch entries")[1]
        for s in report["switched"]:
            logger.info(f"Meet: {s['name']} (bib {s['bib']}) switched into {rk_of(ident)} "
                        f"from {', '.join(s['from']) or 'no race'} ({why})")
        return dict(report, meet=summary)

    # Open races (2026-10-06, user: "Always flag UNLESS the current race is
    # totally empty of entries. If there are 0 entries to that race, no
    # flagging"). A race with no entries of its own - none, or only ones
    # this rule made (Entry.auto) - enters every athlete who finishes it,
    # moving them out of their own race like Override; unless that race is
    # final, the race's gender rules them out, or the operator ignored them.
    # Every other race flags a runner who isn't entered (scoring.ISSUES) and
    # waits for the operator: nothing is switched by itself any more, not
    # even a runner FinishLynx timed (that hid swapped bibs).
    def _open_panes(self, panes) -> List[int]:
        links = self._pane_links()
        with self._lock:
            meet = self.meet
            if meet is None:
                return []
            out = []
            for ident, k in links.items():
                race = meet.race(*ident)
                if k in panes and race is not None and race.status != "final" and _is_open(race):
                    out.append(k)
            return out

    def _auto_switch_soon(self, panes) -> None:
        mgr = self._mgr()
        if mgr is None or self.meet is None or self.preview:
            return
        panes = self._open_panes(set(panes) or set(self._pane_links().values()))
        if not panes:
            return
        with self._auto_lock:
            self._auto_pending.update(panes)
            if self._auto_running:
                return
            self._auto_running = True
        threading.Thread(target=self._auto_switch_worker, daemon=True).start()

    def _auto_switch_worker(self) -> None:
        while True:
            with self._auto_lock:
                panes, self._auto_pending = self._auto_pending, set()
                if not panes:
                    self._auto_running = False
                    return
            try:
                for pane in panes:
                    self._auto_switch_pane(pane)
            except Exception:
                logger.exception("Automatic entry into an open race failed")

    def _auto_switch_pane(self, pane: int) -> None:
        mgr = self._mgr()
        ident = next((i for i, k in self._pane_links().items() if k == pane), None)
        if mgr is None or ident is None:
            return
        rm = self._rm_results(pane)
        with mgr._lock:
            timed = set(mgr._fl_timed.get(pane) or {})
        crossed = {str(f.get("bib")) for f in (rm or {}).get("finishers", [])} | {str(b) for b in timed}
        with self._lock:
            meet = self.meet
            race = meet.race(*ident) if meet is not None else None
            if race is None or race.status == "final" or not _is_open(race):
                return
            aids = []
            for bib in sorted(crossed):
                a = meet.athlete_by_bib(bib)
                if a is None or race.ignored.get(bib) == a.id:
                    continue
                pairs = meet.entries_for(a.id)
                if any(r is race for r, _ in pairs) or any(r.status == "final" for r, _ in pairs):
                    continue
                if _gender_block(meet, race, a):
                    continue
                aids.append(a.id)
        if not aids:
            return

        def change(meet: Meet):
            race = self._race(meet, ident)
            if not _is_open(race):
                return []
            done = []
            for aid in aids:
                a = self._athlete(meet, aid)
                pairs = meet.entries_for(a.id)
                if any(r is race for r, _ in pairs):
                    continue
                status = next((e.status for _, e in pairs if e.status != "scratched"), "entered")
                for r, e in pairs:
                    r.entries.remove(e)
                race.entries.append(Entry(a.id, _event_for(meet, race, a, pairs), status=status,
                                          auto=True))
                done.append(f"{a.first} {a.last} (bib {a.bib})")
            return done
        done, _ = self._mutate(change)
        if done:
            logger.info(f"Meet: open race {rk_of(ident)}: entered {', '.join(done)} (they finished it)")

    # ------------------------------------------------------------ flagged runners

    ISSUE_ACTIONS = ("ignore", "unignore", "override", "swap", "give")

    def resolve_issue(self, rk: str, bib, d: dict) -> Tuple[dict, dict]:
        """One of the menu's answers for a runner flagged in race `rk`
        (2026-10-06, user): {"action": "ignore"} takes the bib out of this
        race's results (undo: "unignore"); "override" enters its holder here
        and scores them (out of their own race; a scratch here becomes
        entered); "swap" + athlete_id: the kid who really ran swaps bibs with
        the holder; "give" + athlete_id: an unknown bib goes to that athlete.
        Works on a final race too: every final race the change touches is
        worked out again and frozen (Re-finalize), as the user chose.
        Returns ({action, refinalized: [rk], errors: [text],
        milesplit_sent: [rk]}, summary)."""
        ident = parse_rk(rk)
        bib = str(bib or "").strip()
        action = str((d or {}).get("action") or "")
        if action not in self.ISSUE_ACTIONS:
            raise MeetError(f"action must be one of {', '.join(self.ISSUE_ACTIONS)}", 400)
        if not bib:
            raise MeetError("which bib?", 400)
        other = str((d or {}).get("athlete_id") or "")
        if action in ("swap", "give") and not other:
            raise MeetError("pick the athlete", 400)
        self._refuse_file_only("changed")

        def change(meet: Meet):
            race = self._race(meet, ident)
            holder = meet.athlete_by_bib(bib)
            touched = {race.key}
            if action == "ignore":
                race.ignored[bib] = holder.id if holder else ""
            elif action == "unignore":
                race.ignored.pop(bib, None)
            elif action == "override":
                if holder is None:
                    raise MeetError(f"nobody has bib {bib}: give it to an athlete first", 400)
                wrong = _gender_block(meet, race, holder)
                if wrong:
                    raise MeetError(f"{holder.first} {holder.last} is {wrong}", 409)
                pairs = meet.entries_for(holder.id)
                here = next((e for r, e in pairs if r is race), None)
                if here is not None:
                    if here.status == "scratched":
                        here.status = "entered"
                else:
                    status = next((e.status for _, e in pairs if e.status != "scratched"), "entered")
                    for r, e in pairs:
                        r.entries.remove(e)
                        touched.add(r.key)
                    race.entries.append(Entry(holder.id, _event_for(meet, race, holder, pairs),
                                              status=status))
                race.ignored.pop(bib, None)
            else:
                ran = self._athlete(meet, other)
                if action == "swap":
                    if holder is None:
                        raise MeetError(f"nobody has bib {bib}: use Give bib", 400)
                    if holder.id == ran.id:
                        raise MeetError(f"{ran.first} {ran.last} already has bib {bib}", 400)
                    res = bibs.swap_bibs(meet, holder.id, ran.id)
                else:
                    if holder is not None:
                        raise MeetError(f"bib {bib} belongs to {holder.first} {holder.last}: "
                                        "use Swap bib", 409)
                    res = bibs.set_bib(meet, ran.id, bib)
                if not res.ok:
                    raise MeetError(res.error, 400)
                for a in (holder, ran):
                    if a is not None:
                        touched |= {r.key for r, _ in meet.entries_for(a.id)}
            touched |= {parse_rk(x) for x in stale_finals(meet)}
            return sorted(k for k in touched if (r := meet.race(*k)) is not None and r.status == "final")
        finals, _ = self._mutate(change)
        report = {"action": action, "refinalized": [], "errors": [], "milesplit_sent": []}
        for key in finals:
            try:
                _, _, sent = self.refinalize(rk_of(key))
                report["refinalized"].append(rk_of(key))
                if sent:
                    report["milesplit_sent"].append(rk_of(key))
            except MeetError as e:
                report["errors"].append(f"{rk_of(key)}: {e}")
        logger.info(f"Meet: race {rk_of(ident)} bib {bib}: {action}"
                    + (f" -> {other}" if other else "")
                    + (f"; re-finalized {', '.join(report['refinalized'])}" if report["refinalized"] else ""))
        return report, self.summary()

    def _issue_rows(self, meet: Meet, race: Race, results: Optional[dict]) -> List[dict]:
        """results["issues"] with what the console shows and offers."""
        out = []
        for i in (results or {}).get("issues") or []:
            a = meet.athlete(i.get("athlete_id")) if i.get("athlete_id") else None
            t = meet.team(a.team_id) if a else None
            elsewhere = [r for r, _ in meet.entries_for(a.id) if r is not race] if a else []
            out.append(dict(i, label=ISSUE_LABELS.get(i["code"], i["code"]),
                            name=f"{a.last}, {a.first}" if a else "",
                            team=(t.short or t.name) if t else "",
                            team_id=a.team_id if a else None,
                            gender=a.gender if a else "",
                            entered_in=[lynx.race_name(meet, r) for r in elsewhere],
                            blocked=(_gender_block(meet, race, a) if a else "")
                            or next((f"{lynx.race_name(meet, r)} is final" for r in elsewhere
                                     if r.status == "final"), "")))
        return out

    def pane_issues(self) -> Dict[str, dict]:
        """Race panes' flagged and ignored runners for the race tables
        (2026-10-06): {pane: {"issues": {bib: {code, label, text, name,
        team, entered_in}}, "ignored": [bib]}}, only panes that have any."""
        links = self._pane_links()
        rms = {ident: self._rm_results(k) for ident, k in links.items()}
        out = {}
        with self._lock:
            meet = self.meet
            if meet is None:
                return {}
            for ident, pane in links.items():
                race = meet.race(*ident)
                if race is None:
                    continue
                res = race.results if race.status == "final" else \
                    self._live(ident, pane, rms.get(ident))["results"]
                rows = self._issue_rows(meet, race, res)
                ign = [x["bib"] for x in (res or {}).get("ignored") or []]
                if rows or ign:
                    out[str(pane)] = {"issues": {r["bib"]: {k: r[k] for k in (
                        "code", "label", "text", "name", "team", "team_id", "athlete_id", "event",
                        "entered_in", "blocked")} for r in rows},
                        "ignored": ign}
        return out

    def mark_final(self, rk: str) -> Tuple[dict, List[dict]]:
        """Freeze the race (scoring.freeze: 409 with the unresolved
        finishers) and set its pane final, as the pane's own Final does."""
        ident = parse_rk(rk)
        self._refuse_file_only("marked final")
        pane = self._pane_links().get(ident)
        if pane is None:
            raise MeetError("no race pane for this race: create it from the .evt first", 409)
        rm = self._rm_results(pane)

        def change(meet: Meet):
            race = self._race(meet, ident)
            try:
                return scoring.freeze(meet, race, rm)
            except scoring.Unresolved as u:
                raise MeetError(f"{len(u.issues)} finisher(s) to resolve before this race can "
                                f"be final: {u.issues[0]['text']}", 409,
                                unresolved=u.issues) from None
        warnings, summary = self._mutate(change)
        mgr = self._mgr()
        if mgr is not None:
            mgr.set_status(pane, "final")
        logger.info(f"Meet: race {rk_of(ident)} final ({len(warnings)} warning(s))")
        return summary, warnings

    def refinalize(self, rk: str) -> Tuple[dict, List[dict], bool]:
        """Reopen + Mark final in one step (2026-10-05, user's ask), after a
        bib changed in a final race: its results are worked out again from
        the reads with the bibs athletes hold now and frozen again. All or
        nothing: anything unresolved raises 409 (with the list) and the race
        stays final with its old results. The pane stays final throughout.
        Returns (summary, warnings, sent: its results were on MileSplit
        before, so the corrected ones should be sent again)."""
        ident = parse_rk(rk)
        self._refuse_file_only("re-finalized")
        pane = self._pane_links().get(ident)
        if pane is None:
            raise MeetError("no race pane for this race: its reads are not loaded", 409)
        rm = self._rm_results(pane)
        with self._lock:
            race = self._race(self._require(), ident)
            if race.status != "final":
                raise MeetError("the race is not final: use Mark final", 409)
            frozen = race.results
        if frozen and frozen.get("finishers") and not (rm and rm.get("finishers")):
            raise MeetError("this race's reads are not loaded, so its results could not be "
                            "worked out again: reopen the meet from its meet folder first", 409)

        def change(meet: Meet):
            race = self._race(meet, ident)
            scoring.reopen(meet, race)
            try:
                return scoring.freeze(meet, race, rm)
            except scoring.Unresolved as u:
                raise MeetError(f"not re-finalized, the old results stay: {len(u.issues)} finisher(s) "
                                f"to resolve first: {u.issues[0]['text']}", 409,
                                unresolved=u.issues) from None
        warnings, summary = self._mutate(change)
        with self._lock:
            sent = bool((self._uploads.get(rk_of(ident)) or {}).get("uploaded_at"))
        logger.info(f"Meet: race {rk_of(ident)} re-finalized with the current bibs "
                    f"({len(warnings)} warning(s))")
        return summary, warnings, sent

    def import_lif(self, rk: str, text: str, name: str = "") -> dict:
        """"Select .lif file" on the Results tab (2026-10-09): the file's
        results become the truth for the race's pane (RaceManager.import_lif),
        and a final race is re-finalized from them at once. A re-finalize
        that can't go through leaves the old official results (the reason
        is handed back) while the pane already shows the file."""
        ident = parse_rk(rk)
        self._refuse_file_only("changed from a .lif")
        pane = self._pane_links().get(ident)
        mgr = self._mgr()
        if pane is None or mgr is None:
            raise MeetError("no race pane for this race: its reads are not loaded", 409)
        with self._lock:
            final = self._race(self._require(), ident).status == "final"
        lif = mgr.import_lif(pane, text, name)          # ValueError -> 400
        logger.info(f"Meet: race {rk_of(ident)} results from picked .lif {lif['name']} "
                    f"({lif['timed']} timed, {len(lif['moved'])} moved)")
        if hasattr(self.app, "refresh_merged_lifs"):
            self.app.refresh_merged_lifs([pane])
        out = {"lif": lif, "refinalized": False, "milesplit_sent": False, "warnings": []}
        if final:
            try:
                out["meet"], out["warnings"], out["milesplit_sent"] = self.refinalize(rk)
                out["refinalized"] = True
            except MeetError as e:
                out["refinalize_error"] = str(e)
                out["unresolved"] = e.details.get("unresolved") or []
        return out

    def _refuse_file_only(self, what: str) -> None:
        if self.file_only:
            raise MeetError(f"this meet was opened without FinishLynx, so results cannot be {what}: "
                            "close it, then open it with FinishLynx to fix a result", 409)

    def reopen(self, rk: str) -> dict:
        ident = parse_rk(rk)
        self._refuse_file_only("reopened")
        pane = self._pane_links().get(ident)
        # 2026-10-05: re-freezing a race whose reads are not loaded (a closed
        # meet opened without its race state) would replace good results
        # with an empty race.
        rm = self._rm_results(pane)
        with self._lock:
            frozen = self._race(self._require(), ident).results
        if frozen and frozen.get("finishers") and not (rm and rm.get("finishers")):
            raise MeetError("this race's reads are not loaded, so its results could not be "
                            "fixed: reopen the meet from its meet folder first", 409)

        def change(meet: Meet):
            scoring.reopen(meet, self._race(meet, ident))
        summary = self._mutate(change)[1]
        mgr = self._mgr()
        if mgr is not None and pane is not None:
            mgr.set_status(pane, "racing")
        logger.info(f"Meet: race {rk_of(ident)} reopened")
        return summary

    def set_mark(self, rk: str, bib, mark) -> dict:
        def change(meet: Meet):
            race = self._race(meet, rk)
            if race.status == "final":
                raise MeetError("the race is final: reopen it first", 409)
            n = bib_number(bib)
            if n is None:
                raise MeetError(f"bib {bib!r} is not a bib number", 400)
            m = str(mark).upper() if mark not in (None, "") else None
            if m is not None and m not in RACE_MARKS:
                raise MeetError(f"mark must be one of {', '.join(RACE_MARKS)} or null", 400)
            if m is None:
                race.marks.pop(str(n), None)
            else:
                race.marks[str(n)] = m
        return self._mutate(change)[1]

    # ------------------------------------------------------------ RaceManager hook

    def on_races_changed(self, race_keys) -> None:
        """Application._on_races_changed: reads moved, so cached live
        results are stale ([] = all panes) and MileSplit Live is due; a
        runner FinishLynx timed may now need switching in."""
        keys = set(race_keys or [])
        self._auto_switch_soon(keys)
        with self._lock:
            if not keys:
                self._live_cache.clear()
            else:
                for ident in [i for i, v in self._live_cache.items() if v["pane"] in keys]:
                    del self._live_cache[ident]
            pusher = self._pusher
        if pusher is not None:
            pusher.notify()

    # ------------------------------------------------------------ MileSplit

    def login(self, email: str, password: str, remember: bool = False) -> dict:
        """remember=True: also save the login on this PC (encrypted for this
        Windows user) so the middleware logs itself in again later."""
        self._refuse_network()
        if not (email and password):
            raise MeetError("email and password are needed", 400)
        try:
            expires = self.client().login(email, password, remember=bool(remember))
        except milesplit.CredentialsUnavailable as e:
            raise MeetError(f"logged in, but the login could not be saved: {e}", 500) from None
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        logger.info("MileSplit: logged in")
        self._web.remember(email, password)  # website pages (milesplit_web), memory only
        self._emit_summary(self.summary())
        return {"logged_in": True, "expires_at": expires.isoformat(),
                "remembered": self.client().remembered()}

    def milesplit_status(self) -> dict:
        """Login state without a meet open (the no-meet page's login box)."""
        try:
            c = self.client()
            return {"logged_in": c.logged_in(), "remembered": c.remembered(), "can_remember": os.name == "nt"}
        except Exception:
            return {"logged_in": False, "remembered": None, "can_remember": os.name == "nt"}

    def logout(self, forget: bool = False) -> dict:
        """Drop the token; forget=True also deletes the saved login (else the
        next MileSplit call logs in again by itself)."""
        with self._lock:
            self._stop_pusher_locked()
        self.client().logout(forget=bool(forget))
        self._web.forget()
        logger.info("MileSplit: logged out" + (" and forgot the saved login" if forget else ""))
        self._emit_summary(self.summary())
        return {"logged_in": False, "remembered": self.client().remembered()}

    def milesplit_meets(self, month=None, year=None, tc=None) -> dict:
        self._refuse_network()
        today = date.today()
        month = _int_in(month or today.month, 1, 12, "month")
        year = _int_in(year or today.year, 2000, 2100, "year")
        client = self.client()
        try:
            companies = client.companies()
            meets = []
            for c in companies:
                cid = _opt_int(c.get("id") if isinstance(c, dict) else None, "company id")
                if cid is None or (tc and int(tc) != cid):
                    continue
                for m in client.meets(cid, month, year) or []:
                    if isinstance(m, dict):
                        meets.append(dict(m, tc_id=cid))
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        return {"companies": companies, "meets": meets, "web": self._attach_setup(meets)}

    def _web_ready(self) -> bool:
        if not self._web.has_login:
            creds = self.client().credentials
            saved = creds.load() if creds is not None else None
            if saved:
                self._web.remember(*saved)
        return self._web.has_login

    def _attach_setup(self, meets: List[dict]) -> dict:
        """Each listed meet's Setup page (website): entries close date/time
        and the header counts, as m["setup"] (2026-10-03, user's ask: pull it
        when the month is listed; the deadline comes from the registration
        Close Date/Time, not the notes). One login per state site first,
        then the pages 4 at a time. A meet that fails gets m["setup_error"]."""
        if not meets:
            return {"ok": True}
        if not self._web_ready():
            return {"ok": False, "error": "log in to MileSplit in the console to read entry close dates"}
        from concurrent.futures import ThreadPoolExecutor
        jobs = []
        for m in meets:
            try:
                jobs.append((m, milesplit_web.meet_host(m.get("meetUrl")), int(m.get("meetId") or m.get("id"))))
            except (milesplit_web.WebError, TypeError, ValueError):
                m["setup_error"] = "no MileSplit meet address"
        try:
            for host in sorted({h for _, h, _ in jobs}):
                self._web.login(host)
        except milesplit_web.WebError as e:
            return {"ok": False, "error": str(e)}

        def one(job):
            m, host, mid = job
            try:
                m["setup"] = milesplit_web.setup_summary(self._web.get(host, f"/meets/{mid}/manager/setup"))
            except milesplit_web.WebError as e:
                m["setup_error"] = str(e)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, jobs))
        return {"ok": True}

    def milesplit_meet_counts(self, tc, meet_id) -> dict:
        """The meet list's Count button (2026-10-03, user's ask): teams and
        athletes with an entry that isn't scratched, from the meet's entries
        (the list itself only carries an entry count, which can lag)."""
        self._refuse_network()
        tc, meet_id = _int_in(tc, 1, 10 ** 9, "tc"), _int_in(meet_id, 1, 10 ** 9, "meet id")
        try:
            data = self.client().entries(tc, meet_id)
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        if isinstance(data, dict):
            data = data.get("data") or []
        teams, athletes, entries = set(), set(), 0
        for t in data or []:
            for a in t.get("roster") or []:
                live = [e for e in a.get("entries") or [] if e.get("entryStatus") != -1]
                if live:
                    teams.add(t.get("teamMileSplitId") or t.get("teamId") or t.get("teamName"))
                    athletes.add(a.get("athleteMileSplitId") or (a.get("athleteFirstName"), a.get("athleteLastName")))
                    entries += len(live)
        return {"meet_id": meet_id, "teams": len(teams), "athletes": len(athletes), "entries": entries}

    def milesplit_meet_page(self, meet_id, url, page: str = "setup", raw: bool = False) -> dict:
        """A meet manager page from the MileSplit website, parsed into form
        fields (2026-10-03, user's ask: entries close date/time, dates,
        teams). Needs the console login since this process started (or a
        saved login on Windows); the API token alone isn't accepted there."""
        self._refuse_network()
        meet_id = _int_in(meet_id, 1, 10 ** 9, "meet id")
        self._web_ready()
        try:
            host = milesplit_web.meet_host(url)
            if page == "details":
                return self._web.meet_details(host, meet_id)
            if raw and page in milesplit_web.READ_ONLY_PAGES:
                return {"html": self._web.get(host, f"/meets/{meet_id}/manager/{page}")}
            return self._web.meet_page(host, meet_id, page)
        except milesplit_web.NotLoggedIn as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit_web.WebError as e:
            raise MeetError(str(e), 502) from None

    def upload(self, rk: str) -> dict:
        """mpResults for EVERY final race in one upload, sent from any final
        race's Send button. 409 when that race isn't final or a problem
        blocks; the upload itself runs without our lock (it can take a
        while) and its answer is kept for each race it carried.

        One upload, not one per race (2026-10-09, Jim Crawford): an
        mpResults upload replaces the meet's whole results page on
        MileSplit, so girls then boys sent apart left only the boys up
        (both answered "Meet Results Imported."). MeetPro sends the whole
        meet too. A reopened race that was sent before blocks: an upload
        without it would take it off MileSplit."""
        self._refuse_network()
        ident = parse_rk(rk)
        with self._lock:
            meet = self._require()
            race = self._race(meet, ident)
            if not exports.is_official(race):
                raise MeetError("the race is not final", 409)
            races = [r for r in meet.races_in_order() if exports.is_official(r)]
            payload = exports.mp_results(meet, races=races)
            problems = exports.mp_results_problems(meet, races, payload=payload)
            for r in meet.races_in_order():
                if not exports.is_official(r) and (self._uploads.get(rk_of(r.key)) or {}).get("ok"):
                    problems.insert(0, f"{lynx.race_name(meet, r)} is on MileSplit but reopened: mark it "
                                       "final again first (an upload replaces every race on MileSplit, "
                                       "so sending now would take it off)")
            blocking = [p for p in problems if not any(s in p for s in NON_BLOCKING_UPLOAD)]
            if blocking:
                raise MeetError(blocking[0], 409, problems=problems)
            tc, mid = meet.meet.milesplit.tc_id, meet.meet.milesplit.meet_id
            meet_id = meet.meet.id
            sent_rks = [rk_of(r.key) for r in races]
        try:
            res = self.client().upload_results(tc, mid, payload)
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        record = {"uploaded_at": datetime.now().isoformat(timespec="seconds"), "ok": res.ok,
                  "message": res.message, "results_url": res.results_url,
                  "fingerprint": exports.fingerprint(payload), "races": sent_rks}
        with self._lock:
            if self.meet is not None and self.meet.meet.id == meet_id:
                for k in sent_rks:
                    self._uploads[k] = dict(record)
                path = os.path.join(self.root, meet_id, UPLOADS_FILE)
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(self._uploads, f, indent=1)
                    os.replace(tmp, path)
                except OSError:
                    logger.exception("Could not record the MileSplit upload")
        (logger.info if res.ok else logger.warning)(
            f"MileSplit upload {', '.join(sent_rks)} (one upload): {'ok' if res.ok else 'refused'} "
            f"{res.message}")
        result = asdict(res)
        return {"result": result, "problems": problems, "races": sent_rks}

    def set_live(self, on: bool) -> dict:
        if not on:
            with self._lock:
                self._stop_pusher_locked()
            self._emit_summary(self.summary())
            return {"live": None}
        self._refuse_network()
        with self._lock:
            meet = self._require()
            mid = meet.meet.milesplit.meet_id
            if not mid:
                raise MeetError("this meet is not linked to a MileSplit meet (meet_id)", 409)
            if not self.client().logged_in():
                raise MeetError("log in to MileSplit first", 409, logged_in=False)
            if self._pusher is None:
                epoch = int(time.time())
                self._pusher = milesplit.LivePusher(
                    self.client(), mid, source=lambda: self._live_source(epoch))
                self._pusher.start()
                self._pusher.notify()
                logger.info(f"MileSplit Live on for meet {mid}")
            status = self._pusher.status()
        self._emit_summary(self.summary())
        return {"live": status}

    def send_start_times(self) -> dict:
        """"Send start times to Live": every upcoming race's scheduled start
        in one PATCH /events (milesplit.live_schedule_payload), with or
        without the pusher running - typically before the meet. The wire
        format is UNVERIFIED (2026-10-03), so Live's answer is handed back
        as it came. 409 when switched off, not linked, logged out or no
        upcoming race has a time; 502 when Live refuses."""
        self._refuse_network()
        if not milesplit.LIVE_SEND_START_TIMES:
            raise MeetError("sending start times to MileSplit Live is switched off "
                            "(milesplit.LIVE_SEND_START_TIMES)", 409)
        links = self._pane_links()
        rms = {ident: self._rm_results(k) for ident, k in links.items()}
        with self._lock:
            meet = self._require()
            mid = meet.meet.milesplit.meet_id
            if not mid:
                raise MeetError("this meet is not linked to a MileSplit meet (meet_id)", 409)

            def live(race: Race):
                return self._live(race.key, links.get(race.key), rms.get(race.key))["results"]
            entries = milesplit.live_schedule_payload(meet, live_results=live)
            races = [{"rk": rk_of(r.key), "start_time_text": start_time_text(r)}
                     for r in meet.races_in_order() if r.start_time and
                     milesplit.race_live_status(r, r.results if r.status == "final" else live(r))
                     is None]
        if not entries:
            raise MeetError("no upcoming race has a start time to send", 409)
        try:
            r = self.client().live_events(mid, entries)
        except milesplit.AuthError as e:
            raise MeetError(str(e), 401, logged_in=False) from None
        except milesplit.MileSplitError as e:
            raise MeetError(f"MileSplit: {e}", 502) from None
        if not r.ok:
            raise MeetError(f"MileSplit Live refused the start times: HTTP {r.http_status} "
                            f"{r.message}".strip(), 502, sent=entries)
        pusher = self._pusher
        if pusher is not None:
            pusher.note_start_times(entries)
        logger.info(f"MileSplit Live: start times sent for {len(races)} race(s)")
        return {"sent": len(entries), "races": races, "entries": entries,
                "message": r.message}

    def _live_source(self, epoch: int) -> List[dict]:
        """LivePusher.source, on the pusher thread: frozen results for
        final races, live ones from the panes for the rest."""
        links = self._pane_links()
        rms = {ident: self._rm_results(k) for ident, k in links.items()}
        with self._lock:
            meet = self.meet
            if meet is None:
                return []

            def live(race: Race):
                return self._live(race.key, links.get(race.key), rms.get(race.key))["results"]
            return milesplit.live_payloads(meet, live_results=live, epoch=epoch)

    def _stop_pusher_locked(self) -> None:
        # No join: the pusher thread may be waiting for our lock inside
        # _live_source; it sees the stop flag on its next wait and exits.
        pusher, self._pusher = self._pusher, None
        if pusher is not None:
            pusher.stop(timeout=0)
            logger.info("MileSplit Live off")

    # ------------------------------------------------------------ printing

    def snapshot(self) -> Meet:
        with self._lock:
            return _copy(self._require())

    def live_results_for(self, rk: str) -> Optional[dict]:
        ident = parse_rk(rk)
        pane = self._pane_links().get(ident)
        rm = self._rm_results(pane)
        with self._lock:
            self._require()
            if self.meet.race(*ident) is None:
                raise MeetError(f"no race {rk_of(ident)} in this meet", 404)
            return self._live(ident, pane, rm)["results"]


def stale_finals(meet: Meet) -> Dict[str, int]:
    """Final races whose frozen results show a bib the athlete no longer
    holds (a swap or a fix after the race went final, 2026-10-05):
    {rk: how many finishers}. Compared as numbers, as the reader reports."""
    def num(b) -> str:
        b = str(b or "").strip()
        return b.lstrip("0") or ("0" if b else "")
    out: Dict[str, int] = {}
    for race in meet.races_in_order():
        res = race.results if race.status == "final" else None
        if not res:
            continue
        n = 0
        for f in res.get("finishers") or []:
            a = meet.athlete(f.get("athlete_id"))
            if a is not None and num(a.bib) != num(f.get("bib")):
                n += 1
        if n:
            out[rk_of(race.key)] = n
    return out


ISSUE_LABELS = {"not_entered": "Not in this race", "unknown_bib": "Unknown bib",
                "scratched": "Scratched"}


def _is_open(race: Race) -> bool:
    """An open race (2026-10-06): no entries of its own - none, or only the
    ones made by finishing it (Entry.auto)."""
    return all(e.auto for e in race.entries)


def _event_for(meet: Meet, race: Race, a: Athlete, pairs) -> int:
    """The scoring event an athlete gets when entered into `race` by hand
    or by an open race: their old event when the race runs it, else the
    race's event for their gender (girls and boys run together), else the
    race's own event."""
    events = [race.event] + list(race.together)
    old = [e.event for _, e in pairs if e.event in events]
    if old:
        return old[0]
    g = (a.gender or "").upper()[:1]
    same = [n for n in events if (ev := meet.event(n)) is not None and ev.gender == g]
    return same[0] if same else race.event


def race_label(meet: Meet, race: Optional[Race]) -> str:
    if race is None:
        return "?"
    ev = meet.event(race.event)
    return race.name or (ev.name if ev else f"race {race.event}")


# --------------------------------------------------------------- arg checks

def _opt_int(v, what: str) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise MeetError(f"{what} must be a whole number", 400) from None


def _division_ids(meet: Meet, v) -> List[str]:
    """A race's division ids from a request: a list of ids the meet has,
    kept in the meet's division order; 400 otherwise."""
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise MeetError("divisions must be a list of division ids", 400)
    unknown = [x for x in v if meet.division(x) is None]
    if unknown:
        raise MeetError(f"no division {', '.join(unknown)} in this meet", 400)
    want = set(v)
    return [x.id for x in meet.meet.divisions if x.id in want]


def _start_time(v) -> str:
    """Typed start time -> "HH:MM" / "" (model.parse_start_time); 400 on garbage."""
    try:
        return parse_start_time(v)
    except ValueError as e:
        raise MeetError(str(e), 400) from None


def _int_in(v, lo: int, hi: int, what: str) -> int:
    n = _opt_int(v, what)
    if n is None or not lo <= n <= hi:
        raise MeetError(f"{what} must be {lo}-{hi}", 400)
    return n
