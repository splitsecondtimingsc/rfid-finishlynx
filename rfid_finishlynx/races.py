"""Race management: concurrent race contexts for XC meets.

Reads are timestamped facts; a race is a roster plus a gun time. Place,
elapsed, splits, team score, and the .lif are all DERIVED - never stored -
so any edit (move, delete, add, time override, gun change) recomputes
everything downstream. Unmatched reads are held, never discarded.
"""

import itertools
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

from .film_offset import FilmOffset
from .lif_generator import _format_race_time
from .lif_merge import ENCODING as LIF_ENCODING, fl_bib_status, fl_timed, looks_complete, merge_lif

logger = logging.getLogger(__name__)

FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def _fmt_mmss(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


@dataclass
class ScoringProfile:
    scorers: int = 5
    displacers: int = 2


@dataclass
class Read:
    id: int
    bib: str
    ts: datetime          # raw capture time - never changes
    point: str            # timing point (e.g. S1, S2, FINISH) - normally the reader_id
    race: Optional[int] = None   # race key when assigned
    state: str = "held"          # assigned | held | ignored
    reason: str = ""             # why held / audit note
    override_ts: Optional[datetime] = None
    manual: bool = False
    estimated: bool = False      # operator accepted a computed time for a missing split
    # The reader that heard it, when that differs from the point: a reader
    # listed in reader_points hands out points by pass number, so its reads
    # keep the reader id here and get their point (re)computed.
    reader: str = ""
    # Pass-numbered away as "not a split" (loop course crossing a mat early);
    # ignored, but still counted as a pass so the next crossing numbers right.
    skipped: bool = False
    # Held because it came sooner after the gun than anyone could run to
    # that point (see RaceManager.min_elapsed); not evidence of a pass.
    early: bool = False
    # Film offset (seconds) subtracted from this finish read when it was
    # sent to FinishLynx - so a .lif row FinishLynx merely copied from us
    # can be recognised after we started correcting (see film_offset.py).
    sent_offset: float = 0.0
    # Reader antenna port that heard it (0 = unknown / split station); the
    # film offset is learned and applied per port.
    antenna: int = 0
    # Kept out of the LapTime feed at ingest, and why: "early" (a finish
    # held as too early - sent once a pane takes it) or "off" (publishing
    # was switched off - sent only when the operator asks). "" = sent or
    # never a candidate. (2026-09-26)
    unsent: str = ""
    # The race this read was in when its reader was set to Not in use, so
    # switching the reader back on returns it there instead of to Held.
    off_race: Optional[int] = None

    @property
    def eff_ts(self) -> datetime:
        return self.override_ts or self.ts


@dataclass
class Race:
    key: int
    name: str
    event: int
    round: int = 1
    heat: int = 1
    gender: str = ""      # 'F' / 'M' / 'X' (mixed: any gender) / '' (unknown)
    distance: str = ""
    # Fastest pace anyone in this race could run, seconds per mile; None =
    # the manager's default. Sets the earliest a read at each point can be.
    fastest_pace: Optional[float] = None
    status: str = "pending"   # pending | checkin | racing | final
    gun: Optional[datetime] = None
    gun_source: str = ""
    gun_candidates: List[dict] = field(default_factory=list)  # {"ts", "source"}
    roster: Set[str] = field(default_factory=set)
    checkins: Dict[str, str] = field(default_factory=dict)    # bib -> iso ts
    scoring: ScoringProfile = field(default_factory=ScoringProfile)
    # Missing splits the operator chose to publish without (bib -> points):
    # the athlete leaves the pane's "Needs attention" list; the .lif still
    # stops at the gap. Restorable. (2026-09-26)
    waived: Dict[str, List[str]] = field(default_factory=dict)
    # bibs whose chip finish the operator put into the .lif although
    # FinishLynx lists them untimed ("Save to .lif" - never automatic)
    chip_to_lif: Set[str] = field(default_factory=set)
    # the event's entries from MeetPro's .evt (with check-ins: the start list)
    entries: Set[str] = field(default_factory=set)

    def start_list(self) -> Set[str]:
        """Who is expected in this race: .evt entries + check-ins. Empty =
        no start list (chip times flow to the .lif as before)."""
        return set(self.entries) | set(self.checkins)


class RaceManager:
    """Owns races, classifies incoming reads, derives results and .lif files.

    Injected dependencies keep it testable:
      athlete_lookup(bib) -> dict with last_name/first_name/school/gender, or None
      on_change(race_keys) -> called after any mutation (UI push + lif write)
    """

    # Floor for a point with no known distance (a read can never be sooner
    # than this after the gun).
    MIN_PLAUSIBLE_ELAPSED_SEC = 120.0
    METRES_PER_MILE = 1609.344

    def __init__(self, athlete_lookup: Optional[Callable] = None,
                 on_change: Optional[Callable] = None,
                 chute_reader: str = "CHUTE",
                 finish_readers: Optional[Set[str]] = None,
                 point_labels: Optional[Dict[str, str]] = None,
                 state_path: Optional[str] = None,
                 point_distances: Optional[Dict[str, float]] = None,
                 default_finish_distance: float = 5000.0,
                 reader_points: Optional[Dict[str, List[str]]] = None,
                 pass_window_sec: float = 90.0,
                 fastest_pace_sec_per_mile: float = 240.0):
        self.athlete_lookup = athlete_lookup or (lambda bib: None)
        self.on_change = on_change
        self.chute_reader = chute_reader
        self.finish_readers = finish_readers or {"FINISH"}
        self.point_labels = point_labels or {}
        self.point_distances = {str(k): float(v) for k, v in (point_distances or {}).items()}
        self.default_finish_distance = default_finish_distance
        # Nobody runs faster than this (s/mile): a read at a point sooner
        # than distance x pace after the gun is held as "too early".
        self.fastest_pace = float(fastest_pace_sec_per_mile)
        self.state_path = state_path
        # reader_id -> ordered points it stands at (see RacesConfig.reader_points)
        self.reader_points: Dict[str, List[str]] = {}
        self.pass_window_sec = float(pass_window_sec)

        self.races: Dict[int, Race] = {}
        self.order: List[int] = []
        self.reads: Dict[int, Read] = {}
        self.chute_target: Optional[int] = None
        self.chute_last: Optional[int] = None      # the race check-in was last open for (late check-ins)
        self._lif_foreign: Dict[str, bool] = {}   # .lif paths FinishLynx has taken over
        self._written: Dict[str, str] = {}        # merged .lif path -> content last written
        self._fl_cache: Dict[str, str] = {}       # FinishLynx .lif path -> last complete text
        self._fl_untimed: Dict[int, Set[str]] = {}  # race -> bibs FinishLynx lists without a time
        self._fl_decided: Dict[int, Set[str]] = {}  # race -> bibs FinishLynx timed or flagged
        self._fl_timed: Dict[int, Dict[str, dict]] = {}  # race -> bib -> FinishLynx's time + names
        # Mat-vs-film finish offset learned from FinishLynx's saved .lif
        # files; subtracted from every non-manual finish this manager
        # reports (results, exports) and, via ingest(), sends.
        self.film = FilmOffset()
        self._next_read_id = itertools.count(1)
        self._next_race_key = itertools.count(1)
        self._lock = threading.RLock()
        self.set_reader_points(reader_points or {}, relabel=False)
        if state_path:
            self._load()

    # ------------------------------------------------------------- races

    def create_race(self, name: str, event: int, gender: str = "",
                    round_: int = 1, heat: int = 1, distance: str = "",
                    scorers: int = 5, displacers: int = 2) -> int:
        with self._lock:
            key = next(self._next_race_key)
            self.races[key] = Race(
                key=key, name=name, event=event, round=round_, heat=heat,
                gender=gender.upper()[:1], distance=distance,
                scoring=ScoringProfile(scorers, displacers),
            )
            self.order.append(key)
        self._changed([key])
        return key

    def set_status(self, key: int, status: str):
        with self._lock:
            race = self.races[key]
            race.status = status
            if status == "checkin":
                self.chute_target = key
                self.chute_last = key
            elif self.chute_target == key and status != "checkin":
                self.chute_target = None
        self._changed([key])

    @staticmethod
    def _near(candidates, ts: datetime, tol: float = 2.0) -> bool:
        return any(abs((_parse_ts(c["ts"]) - ts).total_seconds()) < tol
                   for c in candidates)

    def set_gun(self, key: int, ts: datetime, source: str = "manual"):
        with self._lock:
            race = self.races[key]
            race.gun = ts
            race.gun_source = source
            if not self._near(race.gun_candidates, ts):
                race.gun_candidates.append({"ts": _ts(ts), "source": source})
            if race.status in ("pending", "checkin"):
                race.status = "racing"
                if self.chute_target == key:
                    self.chute_target = None
        self._changed([key])

    def add_gun_candidate(self, key: int, ts: datetime, source: str, arm: bool = True):
        """Record a start signal (the Gun picker lists these). The first
        candidate arms the race unless arm=False - used when offering
        earlier starts to a pane created after the gun went off.

        Candidates within 2 s of a known one are the same signal re-heard
        (running-time packets repeat) and are dropped.
        """
        with self._lock:
            race = self.races[key]
            if self._near(race.gun_candidates, ts):
                return
            race.gun_candidates.append({"ts": _ts(ts), "source": source})
            first = arm and race.gun is None
            if first:
                race.gun = ts
                race.gun_source = source
                if race.status in ("pending", "checkin"):
                    race.status = "racing"
                    if self.chute_target == key:
                        self.chute_target = None
        self._changed([key])

    def delete_race(self, key: int) -> int:
        """Drop one race pane. Its reads are not lost: they go back to the
        held pool (reason 'race deleted') so they can be assigned elsewhere
        or ignored. Returns the number of reads released."""
        with self._lock:
            if key not in self.races:
                raise KeyError(key)
            released = 0
            for read in self.reads.values():
                if read.race == key:
                    read.race = None
                    read.state = "held"
                    read.reason = "race deleted"
                    released += 1
            del self.races[key]
            self.order = [k for k in self.order if k != key]
            self.film.forget_race(key)
            for d in (self._fl_untimed, self._fl_decided, self._fl_timed):
                d.pop(key, None)
            if self.chute_target == key:
                self.chute_target = None
        self._changed([])
        return released

    def close_meet(self, archive_dir: Optional[str] = None) -> dict:
        """Close out the meet: archive what we have, then start empty.

        With archive_dir, the meet CSV (results + team scores) and a copy of
        the state file land there BEFORE anything is cleared - an archive
        failure raises and leaves the meet untouched. Every race and read
        goes; id counters restart at 1 (the archived state keeps the old
        ones). Returns {"races", "reads", "archived": [paths]}.
        """
        with self._lock:
            summary = {"races": len(self.races), "reads": len(self.reads),
                       "archived": []}
            if archive_dir and (self.races or self.reads):
                os.makedirs(archive_dir, exist_ok=True)
                csv_path = os.path.join(archive_dir, "meet-results.csv")
                with open(csv_path, "w", newline="") as f:
                    f.write(self.results_csv())
                summary["archived"].append(csv_path)
                if self.state_path:
                    self._save()          # make sure the copy is current
                    if os.path.exists(self.state_path):
                        state_copy = os.path.join(archive_dir, "races-state.json")
                        shutil.copy2(self.state_path, state_copy)
                        summary["archived"].append(state_copy)
            self.races.clear()
            self.order = []
            self.reads.clear()
            self.chute_target = None
            self.chute_last = None
            self._fl_untimed.clear()
            self._fl_decided.clear()
            self._fl_timed.clear()
            self.film.clear()
            self._written.clear()
            self._fl_cache.clear()
            self._next_read_id = itertools.count(1)
            self._next_race_key = itertools.count(1)
        self._changed([])
        logger.info(f"Meet closed: {summary['races']} races and {summary['reads']} reads cleared"
                    + (f", archived to {archive_dir}" if summary["archived"] else ""))
        return summary

    def find_by_event(self, event: int, heat: int = 1) -> Optional[int]:
        with self._lock:
            for r in self.races.values():
                if r.event == event and r.heat == heat:
                    return r.key
            for r in self.races.values():
                if r.event == event:
                    return r.key
        return None

    # ------------------------------------------------------------- ingest

    def ingest(self, bib: str, ts: datetime, reader_id: str, antenna: int = 0) -> dict:
        """Classify one read. Returns {"state", "race", "reason", "point",
        "finish_offset"} - the last is the film offset the caller must
        subtract when sending a finish to FinishLynx."""
        with self._lock:
            if self.is_chute(reader_id):
                return self._ingest_chute(bib, ts)

            # A reader in reader_points counts as the point(s) configured
            # for it - a split station can be the finish line, the finish
            # mat a split. Start from its first real point so plausibility
            # is judged against the right floor; the pass numbering below
            # settles it (a "-" first slot means a later pass).
            mapped = self.reader_points.get(reader_id)
            if mapped == [self.OFF]:
                # Not in use: keep the read (Ignored pane, restorable), no
                # classification, no finish, no split.
                read = Read(id=next(self._next_read_id), bib=bib, ts=ts,
                            point=reader_id, antenna=int(antenna or 0),
                            reader=reader_id, state="ignored", reason=self.OFF_REASON)
                self.reads[read.id] = read
                return {"state": read.state, "race": None, "reason": read.reason,
                        "point": read.point, "finish_offset": 0.0}
            first = next((p for p in (mapped or []) if p != self.SKIP), None)
            read = Read(id=next(self._next_read_id), bib=bib, ts=ts,
                        point=first or reader_id, antenna=int(antenna or 0),
                        reader=reader_id if mapped else "")
            self.reads[read.id] = read
            race, reason = self._classify(read)
            if read.reader:
                # Which split this pass is depends on the athlete's earlier
                # passes at the same reader (inside the race's window).
                self._relabel_passes(bib, read.reader, race.key if race else None)
            elif self.reader_points:
                # A read elsewhere (e.g. Mile 1) can prove that a crossing
                # skipped as "not a split" was the real one after all.
                self._relabel_bib(bib, race.key if race else None)
            if read.skipped:
                pass                      # a "not a split" pass: ignored, keeps its reason
            elif race is not None:
                early = self._too_early(read, race)
                if early:
                    read.state = "held"
                    read.race = None
                    read.reason = early
                    read.early = True
                    if read.reader:
                        # not a pass after all: the others renumber
                        self._relabel_passes(bib, read.reader, race.key)
                else:
                    self._assign(read, race.key)
            else:
                read.state = "held"
                read.reason = reason
            if self.is_finish_read(read):
                # The caller sends this finish to FinishLynx corrected by
                # the film offset in force right now; remember how much.
                # Decided on the point the read counts as, so a station
                # standing in for the finish line is corrected too.
                read.sent_offset = self.film.correction(read.antenna, read.race)
            changed = [read.race] if read.race else []
        self._changed(changed)
        return {"state": read.state, "race": read.race, "reason": read.reason,
                "point": read.point, "finish_offset": read.sent_offset,
                "read_id": read.id, "early": read.early}

    # ------------------------------------------------ LapTime feed bookkeeping

    def mark_unsent(self, read_id: int, why: str):
        """The caller kept this read out of the LapTime feed ("early" /
        "off"); remembered so it can go later (see Application)."""
        with self._lock:
            read = self.reads.get(read_id)
            if read is not None:
                read.unsent = why
        self._save()

    def unsent_reads(self, why: Optional[str] = None) -> List[Read]:
        """Assigned, non-manual reads still owed to FinishLynx, oldest
        first; `why` narrows to one cause."""
        with self._lock:
            return sorted((r for r in self.reads.values()
                           if r.unsent and (why is None or r.unsent == why)
                           and r.state == "assigned" and not r.manual),
                          key=lambda r: (r.eff_ts, r.id))

    def mark_sent(self, read_id: int, offset: float = 0.0):
        with self._lock:
            read = self.reads.get(read_id)
            if read is not None:
                read.unsent = ""
                read.sent_offset = float(offset or 0.0)
        self._save()

    def waive_missing(self, race_key: int, bib: str, point: Optional[str] = None,
                      undo: bool = False) -> List[str]:
        """"Publish without splits": accept that `bib` has no read at `point`
        (all their missing points when None) so they leave the pane's
        Needs-attention list. undo=True restores the nag. Returns the
        points now waived for the athlete."""
        with self._lock:
            race = self.races[race_key]
            if undo:
                if point is None:
                    race.waived.pop(bib, None)
                else:
                    pts = [p for p in race.waived.get(bib, []) if p != point]
                    if pts:
                        race.waived[bib] = pts
                    else:
                        race.waived.pop(bib, None)
            else:
                if point is None:
                    res = self.results(race_key)
                    row = next((r for r in res["finishers"] if r["bib"] == bib), None)
                    points = list(row["missing"]) if row else []
                else:
                    points = [point]
                have = race.waived.setdefault(bib, [])
                for p in points:
                    if p not in have:
                        have.append(p)
                if not have:
                    race.waived.pop(bib, None)
            out = list(race.waived.get(bib, []))
        self._changed([race_key])
        return out

    def set_entries(self, race_key: int, bibs) -> int:
        """The event's entries from MeetPro's .evt: the race's start list
        (with check-ins). Entrants join the roster (their reads go to this
        race first; they show as "not seen yet" until read). Returns how many."""
        with self._lock:
            race = self.races[race_key]
            self._sync_entries(race, bibs)
            n = len(race.entries)
        self._changed([race_key])
        return n

    def _sync_entries(self, race: Race, bibs):
        """Make the race's .evt entries `bibs`. A bib MeetPro dropped leaves
        the roster unless they checked in - but one who already has reads in
        this race stays entered, so a scratch made after they ran never
        holds their chip time back as off-list. Returns (added, removed)."""
        new = {str(b).strip() for b in (bibs or []) if str(b).strip()}
        old = set(race.entries)
        gone = old - new
        ran = {r.bib for r in self.reads.values()
               if r.state == "assigned" and r.race == race.key} & gone
        removed = gone - ran
        added = new - old
        race.entries = new | ran
        for bib in removed:
            if bib not in race.checkins:
                race.roster.discard(bib)
        race.roster.update(added)
        return added, removed

    def apply_evt(self, evt_races: list, previous: Optional[list] = None) -> dict:
        """MeetPro rewrote lynx.evt: bring the panes made from it up to date.

        `evt_races` is web.evt_race_list() of the new file, `previous` of the
        one before (None = not known, e.g. at boot). A pane matches by
        event-round-heat. Its entries follow the file (see _sync_entries);
        its name, distance and gender follow only where they still are what
        the previous file said - a pane made or set by hand keeps its own.
        Events new to the file get no pane (the operator picks those from
        "Races from .evt"); events gone from it keep theirs. Returns
        {"entries_added", "entries_removed", "renamed": [names],
        "new_races": [names]}."""
        prev = {(r["event"], r["round"], r["heat"]): r for r in (previous or [])}
        out = {"entries_added": 0, "entries_removed": 0, "renamed": [], "new_races": []}
        changed: Set[int] = set()
        reclassify = False
        with self._lock:
            panes: Dict[tuple, List[Race]] = {}
            for race in self.races.values():
                panes.setdefault((race.event, race.round, race.heat), []).append(race)
            for e in evt_races:
                ident = (e["event"], e["round"], e["heat"])
                old = prev.get(ident)
                if previous is not None and old is None:
                    out["new_races"].append(e["name"])
                for race in panes.get(ident, []):
                    if not e.get("is_relay"):
                        added, removed = self._sync_entries(race, e.get("bibs") or [])
                        if added or removed:
                            changed.add(race.key)
                            reclassify = reclassify or bool(added)
                            out["entries_added"] += len(added)
                            out["entries_removed"] += len(removed)
                    if old is None:
                        continue
                    if e["name"] != old["name"] and race.name == old["name"]:
                        out["renamed"].append(f"{race.name} -> {e['name']}")
                        race.name = e["name"]
                        changed.add(race.key)
                    dist = str(e.get("distance", ""))
                    if dist != str(old.get("distance", "")) and race.distance == str(old.get("distance", "")):
                        race.distance = dist
                        changed.add(race.key)
                        reclassify = True
                    g = e.get("gender", "")
                    if g and g != old.get("gender", "") and race.gender == old.get("gender", ""):
                        race.gender = g
                        changed.add(race.key)
                        reclassify = True
            if reclassify:
                # new entrants / a new floor or gender: held reads may fit now
                changed |= self._reclassify_held()
        if changed:
            self._changed(sorted(changed))
        return out

    def set_chip_to_lif(self, race_key: int, bib: str, on: bool = True) -> bool:
        """The pane's "Save to .lif": put `bib`'s chip finish into the .lif
        MeetPro reads although FinishLynx lists them untimed (on=False takes
        it back out). Never automatic - a stray read at the finish mat must
        not become a result by itself. Returns the new state."""
        with self._lock:
            race = self.races[race_key]
            if on:
                race.chip_to_lif.add(bib)
            else:
                race.chip_to_lif.discard(bib)
            out = bib in race.chip_to_lif
        self._changed([race_key])
        return out

    def reader_is_finish(self, reader_id: str) -> bool:
        """Does this reader (local mat or split station) stand at the finish
        line? Unmapped: its id is its point. Mapped: any real point in its
        list is a finish (a loop-course mat crossed early still counts)."""
        pts = self.reader_points.get(reader_id)
        if pts:
            return any(p in self.finish_readers for p in pts)      # [off] -> False
        return reader_id in self.finish_readers

    def is_finish_read(self, read: Read) -> bool:
        """A read the finish-line camera also saw: its point is a finish,
        or it is an early crossing of a mat that is the finish."""
        if read.point in self.finish_readers:
            return True
        return bool(read.skipped and read.reader and self.reader_is_finish(read.reader))

    def finish_reader_ids(self) -> List[str]:
        """Every reader id whose reads count as a finish."""
        ids = [r for r in self.reader_points if self.reader_is_finish(r)]
        ids += [p for p in sorted(self.finish_readers)
                if p not in self.reader_points and p not in ids]
        return ids

    # ------------------------------------------------------ plausibility

    def race_pace(self, race: Race) -> float:
        return race.fastest_pace if race.fastest_pace else self.fastest_pace

    def min_elapsed(self, point: str, race: Race) -> float:
        """Earliest a read at `point` can be, seconds after the gun: the
        point's course distance at the fastest plausible pace."""
        d = self.point_distance(point, race)
        if d is None:
            return self.MIN_PLAUSIBLE_ELAPSED_SEC
        return d / self.METRES_PER_MILE * self.race_pace(race)

    def floors(self, race: Race) -> Dict[str, float]:
        return {p["id"]: self.min_elapsed(p["id"], race) for p in self.points()}

    def _too_early(self, read: Read, race: Race) -> Optional[str]:
        """Reason text if the read is sooner after the gun than anyone could
        reach its point, else None. Reads without a gun to measure against
        are never early; manual reads are the operator's call."""
        if race.gun is None or read.manual:
            return None
        elapsed = (read.eff_ts - race.gun).total_seconds()
        floor = self.min_elapsed(read.point, race)
        if elapsed < floor:
            return (f"too early for {self._label(read.point)}: {_fmt_mmss(elapsed)} after the gun, "
                    f"fastest allowed {_fmt_mmss(floor)} ({_fmt_mmss(self.race_pace(race))}/mile)")
        return None

    def _reclassify_held(self):
        """Re-run classification for every held read (after a race's gender
        or pace changed, or a race opened): reads held as too early or for
        want of a matching race may fit now."""
        changed: Set[int] = set()
        for read in sorted(self.reads.values(), key=lambda r: (r.eff_ts, r.id)):
            if read.state != "held" or read.manual:
                continue
            read.early = False
            race, reason = self._classify(read)
            if race is None:
                read.reason = reason
                continue
            early = self._too_early(read, race)
            if early:
                read.reason = early
                read.early = True
                continue
            self._assign(read, race.key)
            if read.reader:
                self._relabel_passes(read.bib, read.reader, race.key)
            changed.add(race.key)
        return changed

    # ------------------------------------------------- multi-point readers

    # "Not a split" token in a reader's point list (aliases normalised to it)
    SKIP = "-"
    _SKIP_ALIASES = {"-", "skip", "none", "ignore", "x", "nosplit", "not a split"}
    # A reader that is not in use at all ({FINISH: [off]}: the local mat is
    # unplugged because a station is the finish line). Its reads are ignored
    # (restorable), it is never the finish nor a split, and the console does
    # not alarm when it is down.
    OFF = "off"
    _OFF_ALIASES = {"off", "unused", "not in use", "disabled"}
    OFF_REASON = "reader not in use (Stations panel)"

    @classmethod
    def normalize_points(cls, pts) -> List[str]:
        if isinstance(pts, str):
            pts = [pts]
        out = []
        for p in (pts or []):
            p = str(p).strip()
            if not p:
                continue
            low = p.lower()
            out.append(cls.SKIP if low in cls._SKIP_ALIASES
                       else cls.OFF if low in cls._OFF_ALIASES else p)
        return out

    def reader_off(self, reader_id: str) -> bool:
        return self.reader_points.get(reader_id) == [self.OFF]

    def set_reader_points(self, reader_points: Dict[str, List[str]],
                          pass_window_sec: Optional[float] = None,
                          relabel: bool = True):
        """Replace the reader -> points map (a reader that stands at more
        than one split, e.g. Mile 1 and Mile 2 at the same spot, or a mat
        a loop course crosses once before it counts: ["-", "S2"]).

        Existing reads from a reader that is in the new map (or was in the
        old one) are relabelled in place, so the operator can flip the
        setting after the first passes have already arrived. Reads from a
        reader dropped from the map go back to their reader id as the point
        (a skipped pass returns to the held pool for the operator).

        Not in use ("off") takes a reader's reads out of the panes; putting
        it back in use returns each read to the race it was in when it was
        switched off (2026-09-26: moving the finish to a station and back
        mid-race had left every finish in Held, the race at 0 finishers).
        Reads first heard while the reader was off go to Held - they were
        never in a race, and may be warm-ups or tag checks.
        """
        clean: Dict[str, List[str]] = {}
        for reader, pts in (reader_points or {}).items():
            reader = str(reader).strip()
            pts = self.normalize_points(pts)
            if reader and pts:
                clean[reader] = pts
        with self._lock:
            old = self.reader_points
            self.reader_points = clean
            if pass_window_sec is not None:
                self.pass_window_sec = float(pass_window_sec)
            if not relabel:
                return
            touched: Set[int] = set()
            groups = set()
            back_in: List[Read] = []      # finish reads returned to a race: promote after relabel
            for r in self.reads.values():
                if r.manual:
                    continue      # typed/estimated at a point, not heard by a reader
                src = r.reader or r.point
                if src in old or src in clean:
                    if clean.get(src) == [self.chute_reader]:
                        continue      # now the check-in chute: only new reads count as check-ins
                    if clean.get(src) == [self.OFF]:
                        if r.reason == self.OFF_REASON and r.state == "ignored":
                            continue      # already off: keep the race it came from
                        # switched off: its reads leave the panes (restorable)
                        if r.race:
                            touched.add(r.race)
                        r.off_race = r.race if r.state == "assigned" else None
                        r.reader = src
                        r.point = src
                        r.skipped = False
                        r.state = "ignored"
                        r.race = None
                        r.reason = self.OFF_REASON
                        continue
                    if r.reason == self.OFF_REASON and r.state == "ignored":
                        # switched back on: back to the race it was in when
                        # switched off; heard while off -> held for the operator
                        if r.off_race is not None and r.off_race in self.races:
                            r.state = "assigned"
                            r.race = r.off_race
                            r.reason = ""
                            back_in.append(r)
                        else:
                            r.state = "held"
                            r.reason = f"{src} was not in use when heard"
                        r.off_race = None
                    if src in clean:
                        r.reader = src
                        groups.add((r.bib, src, r.race))
                    else:
                        r.reader = ""
                        r.point = src
                        if r.skipped:
                            r.skipped = False
                            r.state = "held"
                            r.reason = f"{src} no longer pass-numbered; was skipped"
                    if r.race:
                        touched.add(r.race)
            for bib, reader, race_key in groups:
                self._relabel_passes(bib, reader, race_key)
            # a finish that is back also promotes its athlete's held splits
            for r in back_in:
                if r.state == "assigned" and r.race is not None and r.point in self.finish_readers:
                    self._assign(r, r.race)
        self._changed(sorted(touched))

    def _relabel_passes(self, bib: str, reader: str, race_key: Optional[int]):
        """Recompute the point of every read of `bib` heard by `reader`
        inside one race window, by pass number.

        Passes are clusters of reads closer together than pass_window_sec
        (a lingering runner is one pass). The i-th pass gets the i-th point
        configured for the reader; passes beyond the list keep the last
        point (a duplicate, visible in the athlete dialog like any other).
        A pass whose point is SKIP ("-") is not a split: its reads are
        ignored (restorable) but still count, so the next crossing gets
        the right number. Reads the operator ignored by hand drop out of
        the numbering.

        The window is the race's reads (plus held reads since its gun,
        which a finish read would promote). Without a race - unknown bib,
        held - it is every held read since the earliest open gun, so a
        spectator's pre-race loiter does not count as Mile 1.

        Order check: the "not a split" crossing of a mat happens before the
        athlete reaches the previous split point on the course (a loop that
        crosses the Mile 2 mat before Mile 1). So once the athlete has a
        read at that previous point, a later crossing cannot be the early
        one - the skip slots are used up and it counts as the real split,
        even if the reader missed the early crossing.
        """
        points = self.reader_points.get(reader)
        if not points or points == [self.OFF]:
            return
        race = self.races.get(race_key) if race_key is not None else None
        if race is not None:
            since = race.gun
        else:
            guns = [r.gun for r in self.races.values()
                    if r.status == "racing" and r.gun is not None]
            since = min(guns) if guns else None

        def in_window(r):
            if race is not None:
                return (r.race == race.key
                        or (r.race is None and (since is None or r.eff_ts >= since)))
            return r.race is None and (since is None or r.eff_ts >= since)

        group = []
        for r in self.reads.values():
            if r.bib != bib or r.reader != reader:
                continue
            if r.state == "ignored" and not r.skipped:
                continue
            # A too-early read is noise, not a pass - unless this reader's
            # first slot is "not a split", where an early crossing is exactly
            # what that slot is for.
            if r.early and points[0] != self.SKIP:
                continue
            if in_window(r):
                group.append(r)
        group.sort(key=lambda r: (r.eff_ts, r.id))

        anchor = None                 # earliest read at the point before this reader's real one
        real = [p for p in points if p != self.SKIP]
        if self.SKIP in points and real and race is not None:
            prev_pt = self._prev_point(real[-1], race)
            if prev_pt:
                anchor_ts = [r.eff_ts for r in self.reads.values()
                             if r.bib == bib and r.point == prev_pt and r.reader != reader
                             and r.state != "ignored" and not r.skipped and in_window(r)]
                anchor = min(anchor_ts) if anchor_ts else None

        idx = -1
        prev = None
        for r in group:
            if prev is None or (r.eff_ts - prev).total_seconds() > self.pass_window_sec:
                idx += 1
            prev = r.eff_ts
            if anchor is not None and r.eff_ts > anchor:
                while idx < len(points) - 1 and points[idx] == self.SKIP:
                    idx += 1
            point = points[min(idx, len(points) - 1)]
            if point == self.SKIP:
                r.point = reader
                if not r.skipped:
                    r.skipped = True
                    r.state = "ignored"
                    r.race = None
                    r.reason = f"pass {idx + 1} at {reader} is not a split (loop course)"
            else:
                r.point = point
                if r.skipped:             # numbering shifted: this pass counts now
                    r.skipped = False
                    r.reason = ""
                    if race is not None:
                        r.state = "assigned"
                        r.race = race.key
                    else:
                        r.state = "held"

    def _relabel_bib(self, bib: str, race_key: Optional[int]):
        for reader in self.reader_points:
            self._relabel_passes(bib, reader, race_key)

    def _prev_point(self, point: str, race: Race) -> Optional[str]:
        """The timing point just before `point` in course order, or None."""
        order = [p["id"] for p in self.points()]
        if point not in order:
            return None
        i = order.index(point)
        return order[i - 1] if i > 0 else None

    # A runner who reaches the chute just after the gun still joins that race.
    LATE_CHECKIN_SEC = 120.0

    def is_chute(self, reader_id: str) -> bool:
        """The check-in chute: the reader named chute_reader, or any reader
        whose Location (Stations panel) is Check-in ({id: [CHUTE]})."""
        return reader_id == self.chute_reader or self.reader_points.get(reader_id) == [self.chute_reader]

    def _ingest_chute(self, bib: str, ts: datetime) -> dict:
        key = self.chute_target
        if key is None or self.races[key].status not in ("checkin", "pending"):
            late = self.races.get(self.chute_last) if self.chute_last is not None else None
            if not (late is not None and late.status == "racing" and late.gun is not None
                    and 0 <= (ts - late.gun).total_seconds() <= self.LATE_CHECKIN_SEC):
                return {"state": "ignored", "race": None,
                        "reason": "chute read with no check-in open"}
            key = late.key
        race = self.races[key]
        if bib not in race.roster:
            race.roster.add(bib)
            race.checkins[bib] = _ts(ts)
            self._changed([key])
        return {"state": "roster", "race": key, "reason": ""}

    def _classify(self, read: Read):
        """Roster -> gender -> plausibility -> held.

        Gender: a race marked F or M takes unrostered athletes of that
        gender; a mixed race (X) takes any athlete the .ppl knows; a race
        with no gender takes nobody by gender (roster only). Among the
        gender candidates only races whose gun is far enough back that the
        athlete could have reached this point count (see min_elapsed), so a
        read is not pinned on a race that started a minute ago.
        """
        open_races = [r for r in self.races.values()
                      if r.status == "racing" and r.gun is not None]

        by_roster = [r for r in open_races if read.bib in r.roster]
        if len(by_roster) == 1:
            return by_roster[0], ""
        if len(by_roster) > 1:
            return None, "bib on multiple open rosters"

        athlete = self.athlete_lookup(read.bib)
        gender = str((athlete or {}).get("gender", "")).upper()[:1]
        if athlete is None and not gender:
            return None, "no chute; unknown bib"
        candidates = [r for r in open_races
                      if r.gender == "X" or (gender and r.gender == gender)]
        plausible = [r for r in candidates if not self._too_early(read, r)]
        who = f"{gender} " if gender else ""
        if len(plausible) == 1:
            return plausible[0], ""
        if len(plausible) > 1:
            return None, f"ambiguous: {len(plausible)} open races take {who}athletes"
        if candidates:
            # the only race(s) this athlete could belong to started too
            # recently: hand one back so the caller holds it as "too early"
            return candidates[0], ""
        return None, f"no chute; no open {who}race (mixed races take anyone)"

    def _assign(self, read: Read, race_key: int):
        read.race = race_key
        read.state = "assigned"
        read.reason = ""
        read.early = False            # placed: no longer "noise" for pass numbering
        # A finish read validates the athlete: promote their held reads that
        # fall inside this race's time window (never reads from other races'
        # windows - a spectator's old loiter reads stay held).
        race = self.races[race_key]
        if read.point in self.finish_readers and race.gun is not None:
            for other in self.reads.values():
                # only reads between the gun and this finish can be its
                # splits - a later (or future-dated) read is never one
                if (other.state == "held" and other.bib == read.bib
                        and not other.early
                        and race.gun <= other.eff_ts <= read.eff_ts):
                    other.race = race_key
                    other.state = "assigned"
                    other.reason = ""
            race.roster.add(read.bib)
            # promoted passes now count inside this race's window
            self._relabel_bib(read.bib, race_key)

    # ------------------------------------------------------------- edits

    def move_bib(self, bib: str, from_key: int, to_key: int):
        """Move an athlete and all their reads between races.

        Timestamps never change; elapsed re-derives against the target gun.
        """
        with self._lock:
            for read in self.reads.values():
                if read.state == "assigned" and read.race == from_key and read.bib == bib:
                    read.race = to_key
            self.races[from_key].roster.discard(bib)
            self.races[to_key].roster.add(bib)
        self._changed([from_key, to_key])

    def ignore_read(self, read_id: int, reason: str = "manually removed"):
        with self._lock:
            read = self.reads[read_id]
            old = read.race
            read.state = "ignored"
            read.race = None
            read.reason = reason
            read.skipped = False          # by hand now: leaves the pass numbering
            if read.reader:
                # dropping a spurious first pass shifts the rest up
                self._relabel_passes(read.bib, read.reader, old)
        self._changed([old] if old else [])

    def remove_bib(self, race_key: int, bib: str, reason: str = "manually removed") -> int:
        """Take an athlete out of a race: every read of theirs assigned to it
        is ignored (restorable from the Ignored pane) and they leave the
        roster. Returns how many reads were ignored.

        One bib often has several reads at the line (dedup window, second
        pass, a tester walking back and forth) - deleting reads one at a time
        left the athlete listed as a finisher on the next read.
        """
        with self._lock:
            n = 0
            for read in self.reads.values():
                if read.state == "assigned" and read.race == race_key and read.bib == bib:
                    read.state = "ignored"
                    read.race = None
                    read.reason = reason
                    n += 1
            self.races[race_key].roster.discard(bib)
        self._changed([race_key])
        return n

    def assign_read(self, read_id: int, race_key: int):
        """Manually place a held/ignored read into a race.

        Restoring a pass that was skipped as "not a split" is the operator
        saying the reader missed the earlier crossing: the read leaves the
        pass numbering and takes the reader's real split point (the last
        one in its list).
        """
        with self._lock:
            read = self.reads[read_id]
            if read.skipped:
                real = [p for p in self.reader_points.get(read.reader, []) if p != self.SKIP]
                read.point = real[-1] if real else read.reader
                read.reader = ""
                read.skipped = False
            self._assign(read, race_key)
            if read.reader:
                self._relabel_passes(read.bib, read.reader, race_key)
        self._changed([race_key])

    def add_manual_read(self, race_key: int, bib: str, ts: datetime,
                        point: str, estimated: bool = False,
                        note: str = "") -> int:
        with self._lock:
            read = Read(id=next(self._next_read_id), bib=bib, ts=ts,
                        point=point, manual=True, estimated=bool(estimated),
                        reason=note)
            self.reads[read.id] = read
            self._assign(read, race_key)
            self.races[race_key].roster.add(bib)
        self._changed([race_key])
        return read.id

    # ------------------------------------------------------------- estimates

    def point_distance(self, point: str, race: Race) -> Optional[float]:
        """Course distance of a timing point in metres, or None if unknown.
        The finish is the race's .evt distance (else the default); split
        points come from config."""
        if point in self.finish_readers:
            try:
                d = float(str(race.distance).strip())
                return d if d > 0 else self.default_finish_distance
            except ValueError:
                return self.default_finish_distance
        return self.point_distances.get(point)

    def _split_points(self, rows) -> List[str]:
        """Split points in course order: those with a distance, then any
        labelled point without one, then anything else seen in these rows'
        reads. Never a finish reader."""
        cols = [p["id"] for p in self.points() if not p["finish"]]
        for p in self.point_labels:
            if p not in cols and p not in self.finish_readers:
                cols.append(p)
        for r in rows:
            for s in r["splits"]:
                if s["point"] not in cols and s["point"] not in self.finish_readers:
                    cols.append(s["point"])
        return cols

    def points(self) -> List[dict]:
        """Timing points the console can add by hand, splits first then the
        finish, with labels and distances."""
        out = []
        for p, d in sorted(self.point_distances.items(), key=lambda kv: kv[1]):
            if p not in self.finish_readers:
                out.append({"id": p, "label": self.point_labels.get(p, p), "distance": d,
                            "finish": False})
        for p in sorted(self.finish_readers):
            out.append({"id": p, "label": self.point_labels.get(p, "Finish"), "distance": None,
                        "finish": True})
        return out

    def _label(self, point: str) -> str:
        if point == "gun":
            return "gun"
        if point in self.finish_readers:
            return self.point_labels.get(point, "Finish")
        return self.point_labels.get(point, point)

    def estimate_split(self, race_key: int, bib: str, point: str) -> dict:
        """A defensible time for a split an athlete is missing.

        Two independent estimates, blended 60/40 when both exist:
          pace       - the athlete's own known points (gun, splits, finish)
                       with course distances: interpolate between the
                       nearest known points either side, else carry the
                       last segment's pace forward (or backward).
          neighbours - runners within two places of the athlete at their
                       nearest recorded point; the athlete's time there plus
                       those runners' average segment time to the target.
        Never for a finish (a finish is a result, not context), never with
        no real read for the athlete in this race, and other athletes'
        estimated reads are not used as evidence. Returns {"ok": False,
        "reason"} or {"ok": True, "suggested", "elapsed", "method", "pace",
        "neighbours"} with clock strings.
        """
        with self._lock:
            race = self.races[race_key]
            label = self._label(point)
            if point in self.finish_readers:
                return {"ok": False, "reason": "finish times are entered, never estimated"}
            own = sorted((r for r in self.reads.values()
                          if r.state == "assigned" and r.race == race_key and r.bib == bib),
                         key=lambda r: r.eff_ts)
            if not own:
                return {"ok": False, "reason": "no reads for this bib in this race yet"}
            if any(r.point == point for r in own):
                return {"ok": False, "reason": f"{label} is already recorded for this bib"}
            target_d = self.point_distance(point, race)

            # --- pace: the athlete's own known points by distance
            known = []          # (distance, ts, point)
            if race.gun is not None:
                known.append((0.0, race.gun, "gun"))
            for r in own:
                d = self.point_distance(r.point, race)
                if d is not None:
                    known.append((d, r.eff_ts, r.point))
            known.sort(key=lambda k: k[0])
            pace = None
            if target_d is not None:
                before = [k for k in known if k[0] < target_d]
                after = [k for k in known if k[0] > target_d]
                if before and after:
                    b, a = before[-1], after[0]
                    frac = (target_d - b[0]) / (a[0] - b[0])
                    ts = b[1] + (a[1] - b[1]) * frac
                    pace = (ts, f"pace between {self._label(b[2])} and {self._label(a[2])}")
                elif len(before) >= 2:
                    b2, b1 = before[-2], before[-1]
                    per_m = (b1[1] - b2[1]).total_seconds() / (b1[0] - b2[0])
                    ts = b1[1] + timedelta(seconds=(target_d - b1[0]) * per_m)
                    pace = (ts, f"pace from {self._label(b2[2])} to {self._label(b1[2])} carried forward")
                elif len(after) >= 2:
                    a1, a2 = after[0], after[1]
                    per_m = (a2[1] - a1[1]).total_seconds() / (a2[0] - a1[0])
                    ts = a1[1] - timedelta(seconds=(a1[0] - target_d) * per_m)
                    pace = (ts, f"pace from {self._label(a1[2])} to {self._label(a2[2])} carried back")

            # --- neighbours: same pack at the athlete's nearest recorded point
            neighbours = None
            if target_d is not None:
                ref = min(own, key=lambda r: abs((self.point_distance(r.point, race) or 0) - target_d))
            else:
                ref = own[-1]
            ref_point = ref.point
            at_ref: Dict[str, datetime] = {}
            at_target: Dict[str, datetime] = {}
            for r in self.reads.values():
                if r.state != "assigned" or r.race != race_key or r.estimated:
                    continue
                if r.point == ref_point and (r.bib not in at_ref or r.eff_ts < at_ref[r.bib]):
                    at_ref[r.bib] = r.eff_ts
                if r.point == point and (r.bib not in at_target or r.eff_ts < at_target[r.bib]):
                    at_target[r.bib] = r.eff_ts
            if bib in at_ref:
                order = sorted(at_ref, key=lambda b: at_ref[b])
                i = order.index(bib)
                pack = [b for b in order[max(0, i - 2):i + 3] if b != bib]
                segs = [(at_target[b] - at_ref[b]).total_seconds() for b in pack if b in at_target]
                if segs:
                    ts = at_ref[bib] + timedelta(seconds=sum(segs) / len(segs))
                    neighbours = (ts, f"{len(segs)} runner{'s' if len(segs) != 1 else ''} within "
                                      f"2 places at {self._label(ref_point)}", len(segs))

            if pace is None and neighbours is None:
                return {"ok": False, "reason": f"no basis for {label}: need a known point with a "
                                               f"distance, or pack-mates who have a {label} time"}
            if pace and neighbours:
                base = pace[0]
                suggested = base + timedelta(seconds=0.4 * (neighbours[0] - base).total_seconds())
                method = "blend: 60% pace, 40% neighbours"
            elif pace:
                suggested, method = pace[0], "pace"
            else:
                suggested, method = neighbours[0], "neighbours"

            def clock(ts):
                return {"clock": _ts(ts),
                        "elapsed": (ts - race.gun).total_seconds() if race.gun else None}

            out = {"ok": True, "bib": bib, "point": point, "label": label, "method": method,
                   "suggested": clock(suggested),
                   "pace": dict(clock(pace[0]), detail=pace[1]) if pace else None,
                   "neighbours": dict(clock(neighbours[0]), detail=neighbours[1],
                                      count=neighbours[2]) if neighbours else None}
            return out

    def estimate_missing(self, race_key: int) -> dict:
        """Fill every finisher's missing expected split with an estimate
        (marked estimated, like accepting "Use" in the console by hand).
        Returns {"added": [(bib, point)], "failed": [(bib, point, reason)]}.
        """
        with self._lock:
            res = self.results(race_key)
            todo = [(row["bib"], p) for row in res["finishers"] for p in row["missing"]]
            added, failed = [], []
            for bib, point in todo:
                est = self.estimate_split(race_key, bib, point)
                if not est.get("ok"):
                    failed.append((bib, point, est.get("reason", "")))
                    continue
                ts = _parse_ts(est["suggested"]["clock"])
                self.add_manual_read(race_key, bib, ts, point, estimated=True,
                                     note=f"estimated: {est['method']} (review, all at once)")
                added.append((bib, point))
        return {"added": added, "failed": failed}

    def override_time(self, read_id: int, new_ts: Optional[datetime],
                      note: str = ""):
        """Correct a read's time. The raw timestamp is kept for audit."""
        with self._lock:
            read = self.reads[read_id]
            read.override_ts = new_ts
            if note:
                read.reason = note
        self._changed([read.race] if read.race else [])

    def set_scoring(self, race_key: int, scorers: int, displacers: int,
                    gender: Optional[str] = None,
                    fastest_pace: Optional[float] = None,
                    keep_pace: bool = True):
        """Scoring profile, and optionally the race's gender mode ('F', 'M',
        'X' mixed, '' unknown) and fastest pace (s/mile; None with
        keep_pace=False resets to the default). Changing either re-checks
        the held reads, which may now belong here."""
        with self._lock:
            race = self.races[race_key]
            race.scoring = ScoringProfile(scorers, displacers)
            reclassify = False
            if gender is not None:
                g = str(gender).upper()[:1]
                if g not in ("F", "M", "X", ""):
                    raise ValueError("gender must be F, M, X (mixed) or blank")
                reclassify = reclassify or g != race.gender
                race.gender = g
            if fastest_pace is not None or not keep_pace:
                new = float(fastest_pace) if fastest_pace else None
                if new is not None and not 120 <= new <= 1800:
                    raise ValueError("fastest pace must be between 2:00 and 30:00 per mile")
                reclassify = reclassify or new != race.fastest_pace
                race.fastest_pace = new
            changed = {race_key}
            if reclassify:
                changed |= self._reclassify_held()
        self._changed(sorted(changed))

    # ------------------------------------------------------------- derived

    def results(self, race_key: int) -> dict:
        """Derive ordered results for one race. Pure function of reads+gun."""
        with self._lock:
            race = self.races[race_key]
            by_bib: Dict[str, List[Read]] = {}
            for read in self.reads.values():
                if read.state == "assigned" and read.race == race_key:
                    by_bib.setdefault(read.bib, []).append(read)

            rows = []
            start_list = race.start_list()
            fl_untimed = self._fl_untimed.get(race_key, set())
            fl_decided = self._fl_decided.get(race_key, set())

            def lif_hold(bib: str) -> str:
                """Why this finisher's chip time waits for "Save to .lif"
                (user's rule 2026-09-30 - never automatic), or "".
                fl_untimed: FinishLynx lists them without a time and the
                race is closed here; off_list: the race has a start list
                (.evt entries / check-in) that does not name them and
                FinishLynx does not have them either - flagged at once."""
                if bib in race.chip_to_lif or bib in fl_decided:
                    return ""
                if bib in fl_untimed:
                    return "fl_untimed" if race.status == "final" else ""
                if start_list and bib not in start_list:
                    return "off_list"
                return ""

            # Mat reads at the finish are reported film-corrected (per
            # antenna port); a time the operator typed (from the camera)
            # already is the film time.
            for bib, reads in by_bib.items():
                reads.sort(key=lambda r: r.eff_ts)
                finish = None
                for r in reads:
                    if r.point in self.finish_readers:
                        finish = r
                splits = [r for r in reads if r.point not in self.finish_readers]
                athlete = self.athlete_lookup(bib) or {}

                def elapsed(r):
                    return (r.eff_ts - race.gun).total_seconds() if race.gun else None

                fin_corr = self.film.correction(finish.antenna, race_key) \
                    if (finish is not None and not finish.manual) else 0.0
                fin_raw = elapsed(finish) if finish is not None else None
                rows.append({
                    "bib": bib,
                    "last_name": athlete.get("last_name", ""),
                    "first_name": athlete.get("first_name", ""),
                    "school": athlete.get("school", ""),
                    "splits": [{
                        "point": s.point,
                        "label": self.point_labels.get(s.point, s.point),
                        "clock": _ts(s.eff_ts),
                        "elapsed": elapsed(s),
                        "read_id": s.id,
                        "manual": s.manual,
                        "estimated": s.estimated,
                        "overridden": s.override_ts is not None,
                    } for s in splits],
                    "finish": None if finish is None else {
                        "clock": _ts(finish.eff_ts - timedelta(seconds=fin_corr)),
                        "elapsed": None if fin_raw is None else fin_raw - fin_corr,
                        "raw_elapsed": fin_raw,
                        "corrected": fin_corr != 0.0,
                        # kept out of LapTime: finished while another race ran
                        "not_in_finishlynx": finish.unsent == "other",
                        # the pane offers "Save to .lif" (see lif_hold)
                        "fl_untimed": bool(lif_hold(bib)),
                        "lif_why": lif_hold(bib),
                        "chip_in_lif": bib in race.chip_to_lif,
                        "correction": fin_corr,
                        "antenna": finish.antenna,
                        "read_id": finish.id,
                        "manual": finish.manual,
                        "estimated": finish.estimated,
                        "overridden": finish.override_ts is not None,
                    },
                    "last_point": self.point_labels.get(reads[-1].point,
                                                        reads[-1].point),
                })

            # Athletes FinishLynx has timed but the finish mat never read
            # (no chip, a dead tag, the reader missed them): listed with
            # FinishLynx's time, flagged "FL only" (2026-10-01, user's ask).
            # Its own row already carries them into the merged .lif.
            by_row = {r["bib"]: r for r in rows}
            fl_only = set()
            for bib, fl in self._fl_timed.get(race_key, {}).items():
                row = by_row.get(bib)
                if row is not None and row["finish"] is not None:
                    continue
                fl_only.add(bib)
                fin = {"clock": _ts(race.gun + timedelta(seconds=fl["time"])) if race.gun else "",
                       "elapsed": fl["time"], "raw_elapsed": fl["time"], "corrected": False,
                       "not_in_finishlynx": False, "fl_untimed": False, "lif_why": "",
                       "chip_in_lif": False, "correction": 0.0, "antenna": 0, "read_id": None,
                       "manual": False, "estimated": False, "overridden": False, "fl_only": True}
                if row is not None:                # splits read, finish missed
                    row["finish"] = fin
                    continue
                athlete = self.athlete_lookup(bib) or {}
                rows.append({"bib": bib,
                             "last_name": athlete.get("last_name") or fl["last_name"],
                             "first_name": athlete.get("first_name") or fl["first_name"],
                             "school": athlete.get("school") or fl["school"],
                             "splits": [], "finish": fin, "last_point": "Finish"})

            finishers = sorted((r for r in rows if r["finish"]),
                               key=lambda r: r["finish"]["elapsed"])
            for place, row in enumerate(finishers, 1):
                row["place"] = place
            on_course = sorted((r for r in rows if not r["finish"]),
                               key=lambda r: r["splits"][-1]["elapsed"] or 0
                               if r["splits"] else 0)
            no_show = sorted(race.roster - set(by_bib) - fl_only)
            # Rostered (checked in / placed by hand) but no read in this race
            # yet: listed at the bottom of the pane so the table is the whole
            # field, moving up as splits and the finish arrive. "no-show"
            # only once the race is final.
            not_seen = []
            for bib in sorted(no_show, key=lambda b: (race.checkins.get(b) or "", b)):
                athlete = self.athlete_lookup(bib) or {}
                not_seen.append({"bib": bib, "last_name": athlete.get("last_name", ""),
                                 "first_name": athlete.get("first_name", ""),
                                 "school": athlete.get("school", ""),
                                 "checked_in": race.checkins.get(bib), "splits": [], "finish": None})

            # Missing-split review: a split point is expected in this race
            # once any athlete has a real (reader) read there - a race with
            # only a Mile 1 station never nags about Mile 2. Finishers
            # lacking an expected point are listed for review.
            expected = [p for p in self._split_points(rows)
                        if any(s["point"] == p and not s["manual"]
                               for r in rows for s in r["splits"])]
            needs_review = 0
            for row in rows:
                have = {s["point"] for s in row["splits"]}
                gaps = [p for p in expected if p not in have] if row["finish"] else []
                waived = race.waived.get(row["bib"], [])
                # waived = the operator chose to publish without it: no nag,
                # but still shown as waived so it can be undone
                row["missing"] = [p for p in gaps if p not in waived]
                row["waived"] = [p for p in gaps if p in waived]
                if row["missing"]:
                    needs_review += 1

            return {
                "race": race_key,
                "finishers": finishers,
                "on_course": on_course,
                "no_show": no_show,
                "not_seen": not_seen,
                "expected_points": expected,
                "needs_review": needs_review,
                "teams": self._team_scores(finishers, race.scoring),
                # this race's own film offset (a new race starts at zero)
                "film": {"applied": self.film.correction(0, race_key),
                         "samples": len(self.film._values(race_key)),
                         "source": "manual" if self.film.manual is not None else "learned",
                         "apply": self.film.apply},
            }

    def _team_scores(self, finishers: List[dict], profile: ScoringProfile):
        """Standard XC scoring: complete teams only, displacers push places."""
        by_team: Dict[str, List[dict]] = {}
        for row in finishers:
            team = row["school"]
            if team:
                by_team.setdefault(team, []).append(row)

        limit = profile.scorers + profile.displacers
        complete = {t for t, rows in by_team.items()
                    if len(rows) >= profile.scorers}
        eligible = [r for r in finishers
                    if r["school"] in complete
                    and by_team[r["school"]].index(r) < limit]

        points: Dict[str, List[int]] = {}
        for pts, row in enumerate(eligible, 1):
            points.setdefault(row["school"], []).append(pts)

        scores = []
        for team in complete:
            counted = points.get(team, [])[:profile.scorers]
            if len(counted) < profile.scorers:
                continue
            tiebreak = points[team][profile.scorers] \
                if len(points[team]) > profile.scorers else 10**6
            scores.append({"team": team, "score": sum(counted),
                           "places": counted, "tiebreak": tiebreak})
        scores.sort(key=lambda s: (s["score"], s["tiebreak"]))
        for rank, s in enumerate(scores, 1):
            s["rank"] = rank
            s.pop("tiebreak")
        return scores

    # ------------------------------------------------------------- csv

    CSV_COLUMNS = ["Race", "Event", "Round", "Heat", "Status", "Place", "Bib",
                   "Last name", "First name", "School", "Time", "Gap", "Finish clock", "Splits",
                   "Estimated"]

    @staticmethod
    def _splits_text(r: dict) -> str:
        """'Mile 1 6:40.0 | ~Mile 2 13:30.1' - a tilde marks an estimated split."""
        return " | ".join(f"{'~' if s.get('estimated') else ''}{s['label']} "
                          f"{_format_race_time(s['elapsed'])}"
                          for s in r["splits"] if s["elapsed"] is not None)

    @staticmethod
    def _estimated_text(r: dict) -> str:
        return ", ".join(s["label"] for s in r["splits"] if s.get("estimated"))

    def results_rows(self, race_key: int) -> List[List[str]]:
        """Flat rows for CSV: finishers, then on course, then no-shows."""
        with self._lock:
            race = self.races[race_key]
        res = self.results(race_key)
        head = [race.name, str(race.event), str(race.round), str(race.heat)]
        rows = []
        first = res["finishers"][0]["finish"]["elapsed"] if res["finishers"] else None
        for r in res["finishers"]:
            fin = r["finish"]["elapsed"]
            status = "finished (FinishLynx only)" if r["finish"].get("fl_only") else "finished"
            rows.append(head + [status, str(r["place"]), r["bib"], r["last_name"],
                                r["first_name"], r["school"], _format_race_time(fin),
                                _format_race_time(fin - first) if r["place"] > 1 else "",
                                r["finish"]["clock"][11:23], self._splits_text(r),
                                self._estimated_text(r)])
        for r in res["on_course"]:
            rows.append(head + ["on course", "", r["bib"], r["last_name"], r["first_name"],
                                r["school"], "", "", "", self._splits_text(r),
                                self._estimated_text(r)])
        for bib in res["no_show"]:
            athlete = self.athlete_lookup(bib) or {}
            rows.append(head + ["no show", "", bib, athlete.get("last_name", ""),
                                athlete.get("first_name", ""), athlete.get("school", ""),
                                "", "", "", "", ""])
        return rows

    def team_rows(self, race_key: int) -> List[List[str]]:
        with self._lock:
            race = self.races[race_key]
        res = self.results(race_key)
        return [[race.name, str(race.event), str(race.round), str(race.heat), str(t["rank"]),
                 t["team"], str(t["score"]), " ".join(str(p) for p in t["places"])]
                for t in res["teams"]]

    def results_csv(self, race_keys: Optional[List[int]] = None) -> str:
        """CSV for one race or the whole meet: athlete rows, a blank line,
        then team scores. Excel-friendly (CRLF, quoted where needed)."""
        import csv
        import io
        keys = list(race_keys) if race_keys is not None else list(self.order)
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\r\n")
        w.writerow(self.CSV_COLUMNS)
        for k in keys:
            for row in self.results_rows(k):
                w.writerow(row)
        w.writerow([])
        w.writerow(["Race", "Event", "Round", "Heat", "Team rank", "Team", "Score", "Scoring places"])
        for k in keys:
            for row in self.team_rows(k):
                w.writerow(row)
        return out.getvalue()

    # ------------------------------------------------------------- lif

    @staticmethod
    def lif_name(race: Race) -> str:
        """FinishLynx's results file name for the event - what MeetPro's
        Splits Auto-Pull polls: three-digit event, round, two-digit heat."""
        return f"{race.event:03d}-{race.round}-{race.heat:02d}.lif"

    def lif_content(self, race_key: int, splits: bool = True,
                    fl_text: Optional[str] = None) -> str:
        """The .lif MeetPro reads for this pane - see lif_merge.py for the
        rules (FinishLynx's rows win when `fl_text` is given; splits stop at
        the first missing point and never include the finish; athletes on
        course listed with blank place/time). splits=False leaves the
        splits field empty."""
        with self._lock:
            race = self.races[race_key]
            res = self.results(race_key)
            columns = self._split_points(res["finishers"] + res["on_course"])
            # off the start list: nothing of theirs (splits, finish) goes
            # until "Save to .lif"; FinishLynx's own rows are unaffected
            sl = race.start_list()
            hold = {r["bib"] for r in res["finishers"] + res["on_course"]
                    if sl and r["bib"] not in sl} if sl else set()
            return merge_lif(race.event, race.round, race.heat, race.name, race.distance,
                             race.gun, res, columns, fl_text=fl_text, splits=splits,
                             publish_chip=set(race.chip_to_lif), hold_chip=hold)

    # FinishLynx files older than this before the gun belong to an earlier
    # race with the same event number (a previous meet left in the folder).
    FL_FILE_GRACE = timedelta(minutes=30)

    def finishlynx_lif(self, race_key: int, lynx_dir: str) -> Optional[str]:
        """FinishLynx's own .lif for the pane, if it has written one for
        THIS race; the last complete copy when it is caught mid-write."""
        with self._lock:
            race = self.races[race_key]
        if not lynx_dir:
            return None
        path = os.path.join(lynx_dir, self.lif_name(race))
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            self._fl_cache.pop(path, None)
            return None
        if race.gun is not None and mtime < race.gun - self.FL_FILE_GRACE:
            return None
        try:
            with open(path, encoding=LIF_ENCODING, errors="replace", newline="") as f:
                text = f.read()
        except OSError:
            return self._fl_cache.get(path)
        if looks_complete(text):
            self._fl_cache[path] = text
            return text
        return self._fl_cache.get(path)

    def write_merged_lif(self, race_key: int, lynx_dir: str, rfid_dir: str) -> Optional[str]:
        """Rebuild the pane's merged .lif in the RFID folder from
        FinishLynx's file (if any) and the race state. Written atomically,
        and only when the content changed - MeetPro's MS Live Sync picks a
        file up by its modified time. Returns the path, or None when the
        race has nothing to publish yet."""
        if not rfid_dir:
            return None
        same = lynx_dir and os.path.normcase(os.path.abspath(lynx_dir)) == \
            os.path.normcase(os.path.abspath(rfid_dir))
        fl_text = None if same else self.finishlynx_lif(race_key, lynx_dir)
        # who FinishLynx lists untimed / has decided: drives "Save to .lif"
        untimed, decided = fl_bib_status(fl_text)
        timed = fl_timed(fl_text)        # athletes the reader missed show from this
        with self._lock:
            changed = (self._fl_untimed.get(race_key, set()) != untimed
                       or self._fl_decided.get(race_key, set()) != decided
                       or self._fl_timed.get(race_key, {}) != timed)
            self._fl_untimed[race_key] = untimed
            self._fl_decided[race_key] = decided
            self._fl_timed[race_key] = timed
        if changed:
            self._changed([race_key])
        content = self.lif_content(race_key, fl_text=fl_text)
        with self._lock:
            path = os.path.join(rfid_dir, self.lif_name(self.races[race_key]))
        if content.count("\n") < 2:                 # header only: nothing to publish
            return None
        if self._written.get(path) == content and os.path.exists(path):
            return path
        os.makedirs(rfid_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding=LIF_ENCODING, errors="replace", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        self._written[path] = content
        return path

    def save_lif(self, race_key: int, directory: str, splits: bool = True) -> dict:
        """Operator's "Save .lif": write the pane's results under FinishLynx's
        file name into `directory` (created if needed), always - this is a
        copy for the operator, never the FinishLynx results folder, so
        nothing of FinishLynx's is overwritten. Returns the path, the
        finisher count and the finishers missing an expected split."""
        content = self.lif_content(race_key, splits=splits)
        with self._lock:
            race = self.races[race_key]
            res = self.results(race_key)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, self.lif_name(race))
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        return {
            "path": path, "finishers": len(res["finishers"]), "splits": splits,
            "missing": [{"bib": r["bib"], "last_name": r["last_name"], "missing": r["missing"]}
                        for r in res["finishers"] if r["missing"]] if splits else [],
        }

    def write_lif(self, race_key: int, directory: str) -> Optional[str]:
        """Write the event's .lif atomically under FinishLynx's file name.

        FinishLynx saves its own results to the same name in the same
        folder, so this never overwrites a file it did not write: a sidecar
        <file>.mirror holds the hash of the last content written here, and a
        file whose content no longer matches it belongs to FinishLynx and is
        left alone (returns None). Unchanged content is not rewritten.
        """
        import hashlib
        content = self.lif_content(race_key)
        with self._lock:
            race = self.races[race_key]
        path = os.path.join(directory, self.lif_name(race))
        side = path + ".mirror"
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                current = f.read()
            if current == content:
                return path
            owned = False
            if os.path.exists(side):
                with open(side) as f:
                    owned = f.read().strip() == hashlib.sha1(current.encode("utf-8", "replace")).hexdigest()
            if not owned:
                if not self._lif_foreign.get(path):
                    logger.warning(f"{os.path.basename(path)} was written by another program "
                                   f"(FinishLynx?) - not overwriting it with RFID results")
                    self._lif_foreign[path] = True
                return None
        self._lif_foreign.pop(path, None)
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
        with open(side, "w") as f:
            f.write(hashlib.sha1(content.encode("utf-8", "replace")).hexdigest())
        return path

    # ------------------------------------------------------------- pools

    def held_reads(self) -> List[dict]:
        return self._pool("held")

    def ignored_reads(self) -> List[dict]:
        return self._pool("ignored")

    def _pool(self, state: str) -> List[dict]:
        """Held / Ignored reads for the console, with who the bib is (from
        the .ppl) and - for a finish-mat read - the film correction its race
        would apply, so the Assign dialog can show the runner's time in each
        race (2026-09-30, user's ask)."""
        with self._lock:
            out = []
            for r in sorted(self.reads.values(), key=lambda r: r.eff_ts):
                if r.state != state:
                    continue
                a = self.athlete_lookup(r.bib) or {}
                fin = r.point in self.finish_readers
                out.append({
                    "id": r.id, "bib": r.bib, "point": r.point,
                    "label": self.point_labels.get(r.point, r.point),
                    "clock": _ts(r.eff_ts), "reason": r.reason,
                    "last_name": a.get("last_name", ""), "first_name": a.get("first_name", ""),
                    "gender": str(a.get("gender") or "").upper()[:1], "school": a.get("school", ""),
                    "finish": fin,
                    # race key -> film correction that race would apply (each learns its own)
                    "corrections": {str(k): self.film.correction(r.antenna, k) for k in self.races}
                                   if (fin and not r.manual) else {},
                })
            return out

    # ------------------------------------------------------------- state

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "order": list(self.order),
                "chute_target": self.chute_target,
                "chute_reader": self.chute_reader,
                "races": {str(k): {
                    "key": r.key, "name": r.name, "event": r.event,
                    "round": r.round, "heat": r.heat, "gender": r.gender,
                    "distance": r.distance, "status": r.status,
                    "gun": _ts(r.gun) if r.gun else None,
                    "gun_source": r.gun_source,
                    "gun_candidates": list(r.gun_candidates),
                    "roster_count": len(r.roster),
                    "scoring": {"scorers": r.scoring.scorers,
                                "displacers": r.scoring.displacers},
                    "fastest_pace": r.fastest_pace,
                    "floors": self.floors(r),
                } for k, r in self.races.items()},
                "fastest_pace_default": self.fastest_pace,
                "held": self.held_reads(),
                "ignored": self.ignored_reads(),
                "points": self.points(),
                "reader_points": {k: list(v) for k, v in self.reader_points.items()},
                # reader ids (local mat and/or stations) that are the finish line now
                "finish_reader_ids": self.finish_reader_ids(),
                "pass_window_sec": self.pass_window_sec,
            }

    def _changed(self, race_keys: List[int]):
        self._save()
        if self.on_change:
            try:
                self.on_change([k for k in race_keys if k])
            except Exception:
                logger.exception("races on_change callback failed")

    def _save(self):
        if not self.state_path:
            return
        with self._lock:
            state = {
                "order": self.order,
                "chute_target": self.chute_target,
                "next_read_id": next(self._next_read_id),
                "next_race_key": next(self._next_race_key),
                "races": [{
                    "key": r.key, "name": r.name, "event": r.event,
                    "round": r.round, "heat": r.heat, "gender": r.gender,
                    "distance": r.distance, "status": r.status,
                    "gun": _ts(r.gun) if r.gun else None,
                    "gun_source": r.gun_source,
                    "gun_candidates": r.gun_candidates,
                    "roster": sorted(r.roster),
                    "checkins": r.checkins,
                    "scorers": r.scoring.scorers,
                    "displacers": r.scoring.displacers,
                    "fastest_pace": r.fastest_pace,
                    "waived": {b: list(p) for b, p in r.waived.items()},
                    "chip_to_lif": sorted(r.chip_to_lif),
                    "entries": sorted(r.entries),
                } for r in self.races.values()],
                "reads": [{
                    "id": r.id, "bib": r.bib, "ts": _ts(r.ts),
                    "point": r.point, "race": r.race, "state": r.state,
                    "reason": r.reason, "manual": r.manual, "estimated": r.estimated,
                    "override_ts": _ts(r.override_ts) if r.override_ts else None,
                    "reader": r.reader, "skipped": r.skipped, "early": r.early,
                    "sent_offset": r.sent_offset, "antenna": r.antenna,
                    "unsent": r.unsent, "off_race": r.off_race,
                } for r in self.reads.values()],
                "film_offset": self.film.to_dict(),
            }
            # itertools.count was advanced by the reads above; rebuild
            self._next_read_id = itertools.count(state["next_read_id"])
            self._next_race_key = itertools.count(state["next_race_key"])
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.error(f"Race state save failed: {e}")

    def _load(self):
        try:
            with open(self.state_path) as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error(f"Race state load failed: {e}")
            return
        self.order = state["order"]
        self.chute_target = state.get("chute_target")
        self._next_read_id = itertools.count(state["next_read_id"])
        self._next_race_key = itertools.count(state["next_race_key"])
        for r in state["races"]:
            self.races[r["key"]] = Race(
                key=r["key"], name=r["name"], event=r["event"],
                round=r["round"], heat=r["heat"], gender=r["gender"],
                distance=r["distance"], status=r["status"],
                gun=_parse_ts(r["gun"]) if r["gun"] else None,
                gun_source=r["gun_source"], gun_candidates=r["gun_candidates"],
                roster=set(r["roster"]), checkins=r["checkins"],
                scoring=ScoringProfile(r["scorers"], r["displacers"]),
                fastest_pace=r.get("fastest_pace"),
                waived={str(b): [str(p) for p in pts]
                        for b, pts in (r.get("waived") or {}).items()},
                chip_to_lif={str(b) for b in (r.get("chip_to_lif") or [])},
                entries={str(b) for b in (r.get("entries") or [])},
            )
        for r in state["reads"]:
            self.reads[r["id"]] = Read(
                id=r["id"], bib=r["bib"], ts=_parse_ts(r["ts"]),
                point=r["point"], race=r["race"], state=r["state"],
                reason=r["reason"], manual=r["manual"],
                estimated=bool(r.get("estimated", False)),
                override_ts=_parse_ts(r["override_ts"]) if r["override_ts"] else None,
                reader=str(r.get("reader") or ""),
                skipped=bool(r.get("skipped", False)),
                early=bool(r.get("early", False)),
                sent_offset=float(r.get("sent_offset") or 0.0),
                antenna=int(r.get("antenna") or 0),
                unsent=str(r.get("unsent") or ""),
                off_race=r.get("off_race"),
            )
        self.film = FilmOffset.from_dict(state.get("film_offset"))
        logger.info(f"Race state restored: {len(self.races)} races, "
                    f"{len(self.reads)} reads"
                    + (f", film offset learned for {len(self.film.stats()['races'])} race(s)"
                       if self.film.samples else ""))

    # ------------------------------------------------------- film offset

    def finish_reads_for_film(self, race_key: int) -> Dict[str, Tuple[datetime, float, int]]:
        """bib -> (raw time, offset sent, antenna) of the mat finish results()
        would use for each athlete in the race: the last non-manual finish."""
        with self._lock:
            out: Dict[str, Tuple[datetime, float, int]] = {}
            for r in sorted(self.reads.values(), key=lambda r: (r.eff_ts, r.id)):
                if r.state == "assigned" and r.race == race_key \
                        and r.point in self.finish_readers and not r.manual:
                    out[r.bib] = (r.eff_ts, r.sent_offset, r.antenna)
            return out

    def learn_film_offsets(self, directory: str) -> bool:
        """Compare every pane's FinishLynx .lif in `directory` with our
        finish reads and update that race's film offset. Returns True when
        the offset data changed (results then re-render)."""
        if not directory or not os.path.isdir(directory):
            return False
        with self._lock:
            races = [(r.key, os.path.join(directory, self.lif_name(r)))
                     for r in self.races.values() if r.gun is not None]
        changed = False
        for key, path in races:
            if not os.path.exists(path):
                continue
            try:
                if self.film.scan_file(key, path, self.finish_reads_for_film(key)):
                    changed = True
            except Exception:
                logger.exception(f"Film offset: reading {path} failed")
        if changed:
            self._changed([])
        return changed

    def set_film_offset(self, manual=..., apply: Optional[bool] = None,
                        reset: bool = False) -> dict:
        """Operator controls: manual override (None clears it; omit to keep),
        apply on/off, reset = forget the learned samples."""
        with self._lock:
            if reset:
                self.film.reset()
            if manual is not ...:
                self.film.set_manual(manual)
            if apply is not None:
                self.film.set_apply(apply)
            stats = self.film.stats()
        self._changed([])
        logger.info(f"Film offset: {stats['source']}"
                    + (f" {stats['manual']:+.3f} s for every race" if stats['manual'] is not None else " per race")
                    + f", apply={stats['apply']}, samples={stats['samples']}")
        return stats
