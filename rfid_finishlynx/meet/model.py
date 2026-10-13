"""Meet model: who and what a meet is, one JSON file per meet.

The shared contract for the meet-management modules (importers, bibs,
idmatch, lynx, scoring, printing, exports, milesplit). Reads and places stay
in races-state.json (RaceManager); this file holds teams, athletes, events,
races + entries, bib ranges, and a frozen copy of each race's results once it
goes final, so printing/uploading still works after close_meet and an
archived meet is self-contained. See docs/MEET-MANAGEMENT-PLAN.md.

Store layout: <root>/<meet-id>/meet.json, written atomically (tmp +
os.replace) like races-state.json. `meet/` is gitignored and never promoted.

Conventions (2026-10-03):
- ids are ours and never reused: athlete "a<n>", team "t<n>". A rename or
  team move keeps the id, so history and MileSplit ids stay attached.
  Meet sync merges the PCs' copies by these ids, so anything added after
  the first import gets an id no other PC can hand out (2026-10-10, bibs
  scrambled at the Jim Crawford meet): "ms<MileSplit id>" / "tms<MileSplit
  id>" when MileSplit knows the athlete or team (the same runner added on
  two PCs is one record), else "a<n>-<random>" / "t<n>-<random>".
- meet.origin: a random id given when the meet is created and carried by
  every PC's copy of it. Two imports of the same meet have different ones,
  and meet sync refuses to merge them (their bibs were numbered apart).
- bib is a string of digits, unique across the meet (it is what the tag
  encodes); "" = not assigned yet. Stored canonical (no leading zeros,
  1..MAX_BIB); lookups compare by number, as the reader reports it.
- gender is "M" / "F" (MileSplit's lowercase m/f is upper-cased on import).
- grade: MileSplit gives the graduation year (athleteYear); grade is derived
  from the meet date. `grade_text` holds a free-text year from Hy-Tek/CSV
  imports ("12", "Senior") when there is no graduation year.
- An entry's `event` is the scoring event; a race's `event` is the Lynx event
  number FinishLynx sees. They differ only when one gun start carries several
  scoring divisions (combined JV start): entries in one race, scored per
  entry.event.
- Divisions (2026-10-06): optional extra standings inside a race (MeetInfo.
  divisions, Race.divisions). A division picks its runners by gender, team
  and grade and is re-scored among them alone; see divisions.py. Not to be
  confused with Event.division (MileSplit's name for a scoring event). A
  meet without divisions works exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# The reader strips leading zeros and decodes at most 5 digits
# (rfid_reader.py), so a bib is 1..99999 and "0101" is the same tag as
# "101" (2026-10-03).
MAX_BIB = 99999

ENTRY_STATUSES = ("entered", "scratched", "exhibition")
RACE_MARKS = ("DNF", "DQ", "DNS")      # Race.marks values
RACE_STATUSES = ("scheduled", "final")
# MileSplit id state for an athlete (idmatch.py): "" = came from MileSplit
# with ids (nothing to match), "auto" = attached by the exact-match rule,
# "pending" = in the review queue, "approved" / "declined" = operator's call.
MS_MATCH_STATES = ("", "auto", "pending", "approved", "declined")
# Division.gender: "" = girls and boys, else only that gender (2026-10-06).
DIVISION_GENDERS = ("", "M", "F")


# --------------------------------------------------------------- records

@dataclass
class MilesplitRef:
    tc_id: Optional[int] = None       # timing company
    meet_id: Optional[int] = None


@dataclass
class Scoring:
    scorers: int = 5
    displacers: int = 2


@dataclass
class Division:
    """Extra standings scored inside a race (2026-10-06, user: "set a
    division number, and then be able to assign divisions given the
    following categories: M/F/Both, Teams (individual groups or all), or
    grade based"). A runner is in the division when all three match. Which
    races score it is Race.divisions. Scoring: divisions.py."""
    id: str                           # "d<n>"
    name: str
    gender: str = ""                  # see DIVISION_GENDERS
    team_ids: List[str] = field(default_factory=list)    # [] = every team (and unattached)
    grades: List[str] = field(default_factory=list)      # [] = every grade; grade_key() form
    team_scores: bool = True          # False: individual places only


@dataclass
class MeetInfo:
    id: str
    name: str = ""
    date: str = ""                    # ISO YYYY-MM-DD (first day)
    host: str = ""
    location: str = ""
    kind: str = "xc"                  # xc | track
    # True: the attending teams' full MileSplit rosters are athletes in this
    # meet (not entered = no entry in any race), so a walk-up already on a
    # roster is "enter in a race", ids attached, no matching needed. A
    # refresh then needs the rosters pull too. (2026-10-03)
    full_rosters: bool = False
    milesplit: MilesplitRef = field(default_factory=MilesplitRef)
    scoring: Scoring = field(default_factory=Scoring)
    # [] = no divisions: the meet scores as it always has (2026-10-06).
    divisions: List[Division] = field(default_factory=list)
    # Which import this is (2026-10-10): set once by MeetStore.create, the
    # same in every PC's copy; "" = a meet made before it was kept.
    origin: str = ""


@dataclass
class Team:
    id: str
    name: str
    short: str = ""                   # what .evt/.ppl and printouts show
    code: str = ""                    # <=4 chars, unique (Hy-Tek / MeetPro)
    score: bool = True                # False: runs, never scores or displaces
    milesplit_id: Optional[int] = None
    city: str = ""
    state: str = ""


@dataclass
class Athlete:
    id: str
    first: str
    last: str
    gender: str                       # M | F
    team_id: Optional[str] = None     # None = unattached (no team at all)
    bib: str = ""
    grad_year: Optional[int] = None
    grade_text: str = ""
    # Listed with a school but runs unattached: never scores or displaces.
    unattached: bool = False
    milesplit_id: Optional[int] = None
    universal_id: Optional[str] = None
    ms_match: str = ""                # see MS_MATCH_STATES
    source: str = "milesplit"         # milesplit | hytek | csv | local
    dob: str = ""                     # ISO, rarely present
    # When a walk-up was added on this console (local ISO seconds; "" for
    # imported athletes): the server-side "Recently added walk-ups" list,
    # newest first, the same on every browser (2026-10-03).
    added_at: str = ""
    # Came in from a team roster, never entered upstream (2026-10-05): in a
    # full-roster meet only athletes entered now, or entered on MileSplit
    # before their race was cancelled, get bibs (bibs.assign).
    roster_only: bool = False
    # The number on the bib this runner was handed (2026-10-10): set when the
    # rosters / bib lists / labels are printed, kept up by one-off changes at
    # the table. The bib check flags a bib that no longer matches (bibcheck.py).
    printed_bib: str = ""

    def grade(self, season_end_year: Optional[int]) -> str:
        """'12', '9', '7' ... from the graduation year, else grade_text."""
        if self.grad_year and season_end_year:
            return str(12 - (self.grad_year - season_end_year))
        return self.grade_text


@dataclass
class Event:
    num: int
    name: str                         # "Boys 5k Run CC Varsity"
    gender: str = ""                  # M | F | X
    distance_m: int = 0
    division: str = ""                # "Varsity Boys" (MileSplit division name)
    code: str = ""                    # Hy-Tek / MileSplit event code ("5000")
    kind: str = "run"                 # run | relay


@dataclass
class Entry:
    athlete_id: str
    event: int                        # scoring event (usually == race.event)
    lane: Optional[int] = None
    status: str = "entered"           # see ENTRY_STATUSES
    seed: str = ""                    # "21:17.71" as given
    # Entered by the console because they finished an open race (one with
    # no entries of its own), not by the operator or an import (2026-10-06).
    auto: bool = False


@dataclass
class Race:
    event: int                        # Lynx event number
    round: int = 1
    heat: int = 1
    order: int = 0                    # run order; .sch is written in this order
    name: str = ""                    # defaults to the event's name
    status: str = "scheduled"         # see RACE_STATUSES
    # Scheduled local start, "HH:MM" 24 h, "" = none (2026-10-03, user: "an
    # option to set start times ... so spectators/athletes can see when a
    # race is expected to start"). Stored canonical; start_time_text() is
    # what people read ("8:00 AM") on printouts and MileSplit Live.
    start_time: str = ""
    entries: List[Entry] = field(default_factory=list)
    # bib -> DNF | DQ | DNS the operator decided; scoring reads it (a DQ'd
    # finisher drops out of the places). (2026-10-03)
    marks: Dict[str, str] = field(default_factory=dict)
    # Scoring events of other races run at the same time as this one, scored
    # separately (edits.run_together, 2026-10-05). Marks the race so it is
    # never mistaken for a gender split, and run_separately knows what to
    # give back. [] = none.
    together: List[int] = field(default_factory=list)
    # Finishers the operator took out of this race's results ("Ignore" on a
    # flagged runner, 2026-10-06): bib -> the athlete id holding it then
    # ("" = nobody). Applies only while that bib still belongs to that
    # athlete, so a bib handed to someone else later is not hidden.
    ignored: Dict[str, str] = field(default_factory=dict)
    # Division ids (MeetInfo.divisions) this race is also scored in, on top
    # of its own results (2026-10-06). [] = none.
    divisions: List[str] = field(default_factory=list)
    # Written by scoring.freeze. Official only while status == "final":
    # reopen keeps the last results until the race is frozen again, so use
    # scoring.official_results(race), not `results is not None`.
    # {"frozen_at": iso,
    #  "finishers": [{place, bib, athlete_id, event, elapsed,
    #                 status (finished|DNF|DNS|DQ), event_place, points,
    #                 scorer, displacer, fl_only,
    #                 splits: [{label, elapsed, estimated}]}],  # race order, then DNF/DQ/DNS
    #  "split_labels": ["Mile 1", "Mile 2"],   # split columns, course order ([] = none)
    #  "teams": {"<event num>": [{rank, team_id, score, complete, count,
    #            runners: [{athlete_id, bib, points, scorer}],
    #            tiebreak_points, total_time, avg_time}]}}
    results: Optional[dict] = None

    @property
    def key(self) -> Tuple[int, int, int]:
        return (self.event, self.round, self.heat)


@dataclass
class Meet:
    meet: MeetInfo
    teams: List[Team] = field(default_factory=list)
    athletes: List[Athlete] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    races: List[Race] = field(default_factory=list)
    # [[start, end], ...]; end None = open-ended
    bib_ranges: List[List[Optional[int]]] = field(default_factory=list)
    schema: int = SCHEMA_VERSION
    updated_at: str = ""

    # ---------------------------------------------------------- lookups

    def team(self, team_id: Optional[str]) -> Optional[Team]:
        return next((t for t in self.teams if t.id == team_id), None) if team_id else None

    def athlete(self, athlete_id: str) -> Optional[Athlete]:
        return next((a for a in self.athletes if a.id == athlete_id), None)

    def athlete_by_bib(self, bib: str) -> Optional[Athlete]:
        n = bib_number(bib)
        if n is None:
            return None
        return next((a for a in self.athletes if bib_number(a.bib) == n), None)

    def event(self, num: int) -> Optional[Event]:
        return next((e for e in self.events if e.num == num), None)

    def division(self, division_id: str) -> Optional[Division]:
        return next((d for d in self.meet.divisions if d.id == division_id), None)

    def race(self, event: int, round_: int = 1, heat: int = 1) -> Optional[Race]:
        return next((r for r in self.races if r.key == (event, round_, heat)), None)

    def races_in_order(self) -> List[Race]:
        """Run order, then event/round/heat: stable, never re-sorted by edits."""
        return sorted(self.races, key=lambda r: (r.order, r.key))

    def entries_for(self, athlete_id: str) -> List[Tuple[Race, Entry]]:
        return [(r, e) for r in self.races for e in r.entries if e.athlete_id == athlete_id]

    def entered(self, athlete_id: str) -> bool:
        """In a race and not scratched (a roster-only athlete is not)."""
        return any(e.status != "scratched" for _, e in self.entries_for(athlete_id))

    @property
    def season_end_year(self) -> Optional[int]:
        """The calendar year the school year ends in: fall 2026 -> 2027,
        spring 2027 -> 2027 (July onward counts as the next school year)."""
        try:
            d = date.fromisoformat(self.meet.date)
        except ValueError:
            return None
        return d.year + 1 if d.month >= 7 else d.year

    def grade(self, a: Athlete) -> str:
        return a.grade(self.season_end_year)

    def athlete_lookup(self, bib: str) -> Optional[dict]:
        """RaceManager's athlete_lookup shape: {last_name, first_name,
        school, gender} (school = the team's short name, as in .ppl)."""
        a = self.athlete_by_bib(bib)
        if a is None:
            return None
        t = self.team(a.team_id)
        return {"last_name": a.last, "first_name": a.first,
                "school": (t.short or t.name) if t else "", "gender": a.gender}

    # ---------------------------------------------------------- id helpers

    def _next_num(self, prefix: str, items) -> int:
        n = 0
        for it in items:
            m = re.fullmatch(prefix + r"(\d+)(?:-[0-9a-z]+)?", it.id)
            if m:
                n = max(n, int(m.group(1)))
        return n + 1

    def _next_id(self, prefix: str, items) -> str:
        """Sequential: only for a meet's first import (one PC, nothing shared yet)."""
        return f"{prefix}{self._next_num(prefix, items)}"

    def _unique_id(self, prefix: str, items, milesplit_id: Optional[int]) -> str:
        """An id no other PC's copy can hand out for someone else (see the
        module notes, 2026-10-10)."""
        taken = {it.id for it in items}
        if milesplit_id is not None:
            ms = f"{'t' if prefix == 't' else ''}ms{milesplit_id}"
            if ms not in taken:
                return ms
        while True:
            new = f"{prefix}{self._next_num(prefix, items)}-{secrets.token_hex(2)}"
            if new not in taken:
                return new

    def new_athlete_id(self, milesplit_id: Optional[int] = None) -> str:
        return self._unique_id("a", self.athletes, milesplit_id)

    def new_team_id(self, milesplit_id: Optional[int] = None) -> str:
        return self._unique_id("t", self.teams, milesplit_id)

    def new_division_id(self, taken=()) -> str:
        """Next "d<n>" past every division id in the meet, its races, or
        `taken` (ids a save is about to use)."""
        n = 0
        ids = [d.id for d in self.meet.divisions] + [i for r in self.races for i in r.divisions]
        for i in list(ids) + list(taken):
            m = re.fullmatch(r"d(\d+)", i)
            if m:
                n = max(n, int(m.group(1)))
        return f"d{n + 1}"

    def used_bibs(self) -> Dict[str, List[str]]:
        """bib -> athlete ids holding it (a list longer than 1 is a conflict)."""
        out: Dict[str, List[str]] = {}
        for a in self.athletes:
            n = bib_number(a.bib)
            if n is not None:
                out.setdefault(str(n), []).append(a.id)
        return out

    # ---------------------------------------------------------- validation

    def validate(self) -> List[str]:
        """Every problem as one readable line; [] = consistent."""
        errs: List[str] = []
        seen = set()
        for t in self.teams:
            if t.id in seen:
                errs.append(f"team id {t.id} used twice")
            seen.add(t.id)
        codes: Dict[str, str] = {}
        for t in self.teams:
            if t.code:
                if len(t.code) > 4:
                    errs.append(f"team {t.name}: code {t.code!r} longer than 4")
                other = codes.get(t.code.upper())
                if other:
                    errs.append(f"team code {t.code} on both {other} and {t.name}")
                codes[t.code.upper()] = t.name
        team_ids = {t.id for t in self.teams}
        seen = set()
        for a in self.athletes:
            if a.id in seen:
                errs.append(f"athlete id {a.id} used twice")
            seen.add(a.id)
            if a.gender not in ("M", "F"):
                errs.append(f"athlete {a.id} {a.first} {a.last}: gender {a.gender!r}")
            if a.team_id is not None and a.team_id not in team_ids:
                errs.append(f"athlete {a.id} {a.first} {a.last}: no team {a.team_id}")
            if a.bib and (bib_number(a.bib) is None or str(bib_number(a.bib)) != a.bib):
                errs.append(f"athlete {a.id} {a.first} {a.last}: bib {a.bib!r} is not "
                            f"a number 1-{MAX_BIB} without leading zeros")
            if a.ms_match not in MS_MATCH_STATES:
                errs.append(f"athlete {a.id}: ms_match {a.ms_match!r}")
        for bib, ids in self.used_bibs().items():
            if len(ids) > 1:
                errs.append(f"bib {bib} on {len(ids)} athletes: {', '.join(ids)}")
        athlete_ids = {a.id for a in self.athletes}
        event_nums = {e.num for e in self.events}
        if len(event_nums) != len(self.events):
            errs.append("event number used twice")
        keys = set()
        for r in self.races:
            tag = f"race {r.event}-{r.round}-{r.heat}"
            if r.key in keys:
                errs.append(f"{tag} listed twice")
            keys.add(r.key)
            if r.event not in event_nums:
                errs.append(f"{tag}: no event {r.event}")
            if r.status not in RACE_STATUSES:
                errs.append(f"{tag}: status {r.status!r}")
            if r.start_time and not _HHMM.fullmatch(r.start_time):
                errs.append(f"{tag}: start time {r.start_time!r} is not HH:MM (24 h)")
            for bib, mark in r.marks.items():
                if mark not in RACE_MARKS:
                    errs.append(f"{tag}: bib {bib} marked {mark!r}")
            in_race = set()
            for e in r.entries:
                if e.athlete_id not in athlete_ids:
                    errs.append(f"{tag}: entry for unknown athlete {e.athlete_id}")
                if e.athlete_id in in_race:
                    errs.append(f"{tag}: athlete {e.athlete_id} entered twice")
                in_race.add(e.athlete_id)
                if e.event not in event_nums:
                    errs.append(f"{tag}: entry {e.athlete_id} scores in unknown event {e.event}")
                if e.status not in ENTRY_STATUSES:
                    errs.append(f"{tag}: entry {e.athlete_id} status {e.status!r}")
        div_ids = set()
        for d in self.meet.divisions:
            if d.id in div_ids:
                errs.append(f"division id {d.id} used twice")
            div_ids.add(d.id)
            if not d.name.strip():
                errs.append(f"division {d.id} has no name")
            if d.gender not in DIVISION_GENDERS:
                errs.append(f"division {d.name or d.id}: gender {d.gender!r}")
            for tid in d.team_ids:
                if tid not in team_ids:
                    errs.append(f"division {d.name or d.id}: no team {tid}")
        names = [d.name.strip().casefold() for d in self.meet.divisions if d.name.strip()]
        if len(set(names)) != len(names):
            errs.append("two divisions have the same name")
        for r in self.races:
            for i in r.divisions:
                if i not in div_ids:
                    errs.append(f"race {r.event}-{r.round}-{r.heat}: no division {i}")
            if len(set(r.divisions)) != len(r.divisions):
                errs.append(f"race {r.event}-{r.round}-{r.heat}: a division listed twice")
        for rng in self.bib_ranges:
            if len(rng) != 2 or rng[0] is None or (rng[1] is not None and rng[1] < rng[0]):
                errs.append(f"bib range {rng} invalid")
        return errs

    # ---------------------------------------------------------- (de)serialise

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Meet":
        info = dict(d["meet"])
        info["milesplit"] = _build(MilesplitRef, info.get("milesplit") or {})
        info["scoring"] = _build(Scoring, info.get("scoring") or {})
        info["divisions"] = [_build(Division, x) for x in info.get("divisions") or []]
        races = []
        for r in d.get("races", []):
            r = dict(r)
            r["entries"] = [_build(Entry, e) for e in r.get("entries", [])]
            races.append(_build(Race, r))
        meet = cls(meet=_build(MeetInfo, info),
                   teams=[_build(Team, t) for t in d.get("teams", [])],
                   athletes=[_build(Athlete, a) for a in d.get("athletes", [])],
                   events=[_build(Event, e) for e in d.get("events", [])],
                   races=races,
                   bib_ranges=[list(r) for r in d.get("bib_ranges", [])],
                   schema=d.get("schema", SCHEMA_VERSION),
                   updated_at=d.get("updated_at", ""))
        raw = d.get("athletes", [])
        if meet.meet.full_rosters and raw and not any("roster_only" in a for a in raw):
            # Saved before 2026-10-05: roster-only was "not entered", so mark
            # them as such (bibs.assign would otherwise number them).
            entered = {e.athlete_id for r in meet.races for e in r.entries}
            for a in meet.athletes:
                a.roster_only = a.id not in entered
        return meet


_GRADE_WORDS = {"freshman": "9", "fr": "9", "sophomore": "10", "so": "10",
                "junior": "11", "jr": "11", "senior": "12", "sr": "12"}


def grade_key(grade) -> str:
    """'09' / 9 / 'Freshman' / 'FR' -> '9', so a grade filter or division
    matches a graduation-year grade and a Hy-Tek free-text year alike."""
    g = str(grade if grade is not None else "").strip().lower()
    if g.isdigit():
        return str(int(g))
    return _GRADE_WORDS.get(g, g)


def bib_number(bib) -> Optional[int]:
    """The number a tag read would report for this bib, or None when it is
    blank, not digits, 0 or over MAX_BIB."""
    text = str(bib if bib is not None else "").strip()
    if not text.isdigit():
        return None
    n = int(text)
    return n if 1 <= n <= MAX_BIB else None


# --------------------------------------------------------------- start times

_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# "8", "8:00", "08:00", "800", "0800", with or without an am/pm suffix
_TIME = re.compile(r"(\d{1,2})(?::?([0-5]\d))?\s*(?:([ap])\.?\s*m?\.?)?", re.I)


def parse_start_time(text) -> str:
    """What the operator typed -> stored "HH:MM" (24 h); "" clears.

    Lenient on purpose (2026-10-03): "8:00 AM", "8am", "8:00a", "08:00",
    "8:00", "0800", "20:15" all work. Without AM/PM a bare 1:00-6:59 is the
    afternoon ("1:30" is 1:30 PM: XC meets run from about 7 AM to 6 PM, so
    nobody means 1:30 AM), 7:00-12:59 is as typed; a leading zero ("01:30")
    or a 24 h hour ("13:30") is taken literally. The console shows the
    stored time back as "1:30 PM" at once. Raises ValueError otherwise."""
    t = str(text if text is not None else "").strip()
    if not t:
        return ""
    m = _TIME.fullmatch(t)
    if m:
        h, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ap:
            if not 1 <= h <= 12:
                raise ValueError(f"start time {t!r}: the hour must be 1-12 with AM/PM")
            return f"{h % 12 + (12 if ap == 'p' else 0):02d}:{mm:02d}"
        if h <= 23:
            if 1 <= h <= 6 and not t.startswith("0"):    # meet hours: afternoon
                h += 12
            return f"{h:02d}:{mm:02d}"
    raise ValueError(f"start time {t!r}: use a time like 8:00 AM or 20:15")


def time_text(hhmm: str) -> str:
    """Stored "HH:MM" as people read it: "08:00" -> "8:00 AM", "20:15" ->
    "8:15 PM", "" -> "" (no leading zero, MeetPro style: MileSplit Live
    shows the text exactly as sent)."""
    if not hhmm or not _HHMM.fullmatch(hhmm):
        return ""
    h, mm = int(hhmm[:2]), hhmm[3:]
    return f"{h % 12 or 12}:{mm} {'AM' if h < 12 else 'PM'}"


def start_time_text(race: "Race") -> str:
    return time_text(race.start_time)


def _build(cls, d: dict):
    """Dataclass from a dict, ignoring keys a newer file may carry."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


# --------------------------------------------------------------- store

def slug(text: str) -> str:
    """'2026-10-10 Woodmont Invitational' -> '2026-10-10-woodmont-invitational'."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "meet"


class MeetStore:
    """<root>/<meet-id>/meet.json, saved atomically and durably, the copy
    before it kept as meet.json.bak.

    Break test 2026-10-13: a damaged meet.json (power cut, disk full, a sync
    tool's half write) dropped the console into legacy mode without a word
    and the meet was lost. save() now writes a temp file, flushes and fsyncs
    it, keeps the previous meet.json as meet.json.bak and only then replaces
    it; load() falls back to the .bak (logged, and noted in `restored` so the
    console can say so)."""

    FILENAME = "meet.json"
    BACKUP = ".bak"

    def __init__(self, root: str = "meet"):
        self.root = root
        self.on_save = None            # meet sync: told after every save (2026-10-07)
        # meet id -> the warning text, for a meet loaded from its .bak
        # because meet.json could not be read (until it is saved again)
        self.restored: Dict[str, str] = {}

    def path(self, meet_id: str) -> str:
        return os.path.join(self.root, meet_id, self.FILENAME)

    def backup_path(self, meet_id: str) -> str:
        return self.path(meet_id) + self.BACKUP

    def list(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isfile(os.path.join(self.root, d, self.FILENAME)))

    def exists(self, meet_id: str) -> bool:
        return os.path.isfile(self.path(meet_id))

    def _read(self, meet_id: str) -> Tuple[dict, "Meet"]:
        """(meet.json as a dict, the Meet), from the .bak when meet.json
        can't be read (a warning is logged and kept in `restored`). Raises
        meet.json's error when neither can be read."""
        try:
            with open(self.path(meet_id), encoding="utf-8") as f:
                d = json.load(f)
            meet = Meet.from_dict(d)
            self.restored.pop(meet_id, None)
            return d, meet
        except (OSError, ValueError, KeyError, TypeError) as e:
            first = e
        try:
            with open(self.backup_path(meet_id), encoding="utf-8") as f:
                d = json.load(f)
            meet = Meet.from_dict(d)
        except (OSError, ValueError, KeyError, TypeError):
            raise first from None
        msg = (f"{self.FILENAME} of meet {meet_id} could not be read ({first}); restored from "
               f"its backup ({self.FILENAME}{self.BACKUP}, saved {d.get('updated_at') or '?'}): "
               f"the last change before the damage may be missing - check it")
        logger.warning(msg)
        self.restored[meet_id] = msg
        return d, meet

    def read(self, meet_id: str) -> dict:
        """meet.json as a dict (from the .bak if need be, see _read)."""
        return self._read(meet_id)[0]

    def load(self, meet_id: str) -> Meet:
        return self._read(meet_id)[1]

    def save(self, meet: Meet) -> str:
        meet.updated_at = datetime.now().isoformat(timespec="seconds")
        path = self.path(meet.meet.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meet.to_dict(), f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(path):
            if meet.meet.id in self.restored:
                # meet.json is the damaged one: keep the good .bak, set the
                # damaged file aside for a look
                try:
                    os.replace(path, path + ".damaged")
                except OSError:
                    pass
            else:
                try:
                    shutil.copyfile(path, path + self.BACKUP + ".tmp")
                    os.replace(path + self.BACKUP + ".tmp", path + self.BACKUP)
                except OSError:
                    logger.warning(f"Could not keep {path}{self.BACKUP}", exc_info=True)
        os.replace(tmp, path)
        self.restored.pop(meet.meet.id, None)
        if self.on_save is not None:
            try:
                self.on_save(meet.meet.id)
            except Exception:
                pass
        return path

    # Raw MileSplit pulls kept beside meet.json (meet/<id>/pulls/<kind>.json):
    # idmatch rebuilds its candidate pool from them, and importers'
    # refresh uses the last one as `previous` for its three-way merge.
    # They hold real names, like meet.json, so they stay in the gitignored
    # store. kind: "entries" | "rosters". (2026-10-03)

    def pull_path(self, meet_id: str, kind: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
            raise ValueError(f"pull kind {kind!r}")
        return os.path.join(self.root, meet_id, "pulls", f"{kind}.json")

    def save_pull(self, meet_id: str, kind: str, data) -> str:
        path = self.pull_path(meet_id, kind)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def load_pull(self, meet_id: str, kind: str):
        """The last saved pull of that kind, or None."""
        try:
            with open(self.pull_path(meet_id, kind), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def create(self, name: str, date_iso: str, **info) -> Meet:
        """A new empty meet with an id from date + name (suffixed if taken)."""
        base = slug(f"{date_iso} {name}")
        meet_id, n = base, 2
        while self.exists(meet_id):
            meet_id, n = f"{base}-{n}", n + 1
        meet = Meet(meet=MeetInfo(id=meet_id, name=name, date=date_iso, **info))
        meet.meet.origin = meet.meet.origin or secrets.token_hex(8)
        self.save(meet)
        return meet

    def find(self, *, milesplit_meet_id: Optional[int] = None, name: str = "",
             date_iso: str = "") -> List[str]:
        """Meets here that are this one (2026-10-10): the same MileSplit meet,
        or the same date + name (the id create() would start from). Meet
        sync brings the other PCs' meets in, so after a sync round this
        also finds a meet another PC made."""
        base = slug(f"{date_iso} {name}") if name and date_iso else ""
        out = []
        for meet_id in self.list():
            if base and re.fullmatch(re.escape(base) + r"(-\d+)?", meet_id):
                out.append(meet_id)
                continue
            if milesplit_meet_id:
                try:
                    with open(self.path(meet_id), encoding="utf-8") as f:
                        info = json.load(f).get("meet") or {}
                except (OSError, ValueError):
                    continue
                if (info.get("milesplit") or {}).get("meet_id") == milesplit_meet_id:
                    out.append(meet_id)
        return out
