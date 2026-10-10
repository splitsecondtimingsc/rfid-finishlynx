"""Importers: build or refresh a Meet from MileSplit entries (primary), a
Hy-Tek semicolon I/D file, or a plain CSV (fallbacks for meets that are not
on MileSplit). See docs/MEET-MANAGEMENT-PLAN.md and MEET-RESEARCH.md §1-2.

Two steps, so every format shares one merge and a refresh never loses local
work:

    parse_milesplit / parse_hytek / parse_csv  ->  Pull (normalised records)
    merge(meet, pull, previous=None)           ->  ImportReport

A fresh import is a merge into an empty meet (`import_milesplit` etc. do
that); a refresh is a merge into the meet the operator has been editing.

Decisions (2026-10-03):
- Events come from distinct (gender, division name, event code). MileSplit
  division ids are all 0, so the division NAME is the split. Names are
  Hy-Tek style: "Girls 5k Run CC Varsity", "Boys 2 Mile Run CC JV".
- Default numbering of new events (the operator reorders later): Varsity
  (or no level) first, then JV, then every other level alphabetically
  ("Middle School" before "Open"); within a level Girls, Boys, Mixed; then
  distance. So a standard XC meet is 1 Varsity Girls, 2 Varsity Boys,
  3 JV Girls, 4 JV Boys. On a refresh, new events are numbered after the
  existing ones in the same order; existing numbers never move.
- One race per new event (round 1, heat 1), run order = event order,
  appended after existing races on a refresh.
- An athlete listed on two teams (same MileSplit id under two teams) becomes
  TWO Athlete records sharing milesplit_id/universal_id, one per team, each
  with its own bib: a bib maps to (athlete, team), and the model keys a bib
  to one Athlete. Matching on refresh is therefore by (milesplit_id, team).
  Downstream code must not assume milesplit_id is unique in a meet.
- MileSplit's "Unattached" pseudo-team maps to team_id None (the model's
  "no team at all"), not to a Team record.
- Nothing upstream is ever deleted locally: athletes/entries/teams that
  disappeared are reported in `gone`; the operator decides.
- Operator work always survives a refresh: bibs, local athletes, event and
  race names, race order, entry statuses the operator set, entries moved to
  another division. Pass the PREVIOUS pull (the console keeps the raw JSON
  next to meet.json) to get a real three-way merge: then an upstream change
  applies only where the local value still equals what was pulled last
  time, and anything else is reported as a conflict (applied=False). Without
  it, upstream identity fields (name, gender, year) win, upstream scratches
  apply to entries still "entered", and a division move that cannot be told
  apart from an operator move is reported, not applied.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .model import Athlete, Entry, Event, Meet, MeetInfo, Race, Team

SOURCES = ("milesplit", "hytek", "csv")
_SOURCE_LABEL = {"milesplit": "MileSplit entries", "hytek": "Hy-Tek file", "csv": "CSV"}


# --------------------------------------------------------------- pull records

@dataclass
class EntryRec:
    gender: str                       # event gender M | F | X
    division: str = ""                # "Varsity Boys" as the source names it
    code: str = ""                    # "5000", "2MILE"; "" = unknown
    status: str = "entered"           # entered | scratched
    seed: str = ""

    @property
    def event_key(self) -> Tuple[str, str, str]:
        return _event_key(self.gender, self.division, self.code)


@dataclass
class AthleteRec:
    first: str
    last: str
    gender: str                       # M | F
    grad_year: Optional[int] = None
    grade_text: str = ""
    milesplit_id: Optional[int] = None
    universal_id: Optional[str] = None
    dob: str = ""
    bib: str = ""
    entries: List[EntryRec] = field(default_factory=list)
    roster_only: bool = False

    def __post_init__(self):
        self.first, self.last = fix_text(self.first), fix_text(self.last)

    @property
    def key(self) -> tuple:
        """Stable within a team across pulls: the MileSplit id when there is
        one, else the normalised name + gender (Hy-Tek / CSV)."""
        if self.milesplit_id is not None:
            return ("ms", self.milesplit_id)
        return ("n", _norm(self.last), _norm(self.first), self.gender)


@dataclass
class TeamRec:
    key: str                          # "ms:900001" | "code:RID" | "name:ridge" | "unattached"
    name: str
    abbrev: str = ""
    milesplit_id: Optional[int] = None
    city: str = ""
    state: str = ""
    unattached: bool = False
    athletes: List[AthleteRec] = field(default_factory=list)

    def __post_init__(self):
        self.name, self.city = fix_text(self.name), fix_text(self.city)


@dataclass
class Pull:
    """One source file, normalised: teams -> athletes -> entries, with
    duplicates folded (a team listed twice, an athlete listed twice on one
    team) and the lines that could not be used in `notes`."""
    source: str
    teams: List[TeamRec] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    _teams: Dict[str, TeamRec] = field(default_factory=dict, repr=False, compare=False)
    _aths: Dict[tuple, AthleteRec] = field(default_factory=dict, repr=False, compare=False)

    def add(self, team: TeamRec, ath: Optional[AthleteRec] = None) -> None:
        t = self._teams.get(team.key)
        if t is None:
            t = self._teams[team.key] = team
            self.teams.append(t)
        if ath is None:
            return
        have = self._aths.get((t.key, ath.key))
        if have is None:
            # First sighting: keep it, minus repeated entries for one event.
            entries, ath.entries = ath.entries, []
            self._aths[(t.key, ath.key)] = have = ath
            t.athletes.append(ath)
            ath = AthleteRec(ath.first, ath.last, ath.gender, entries=entries)
        for f in ("grad_year", "grade_text", "universal_id", "dob", "bib"):
            if not getattr(have, f) and getattr(ath, f):
                setattr(have, f, getattr(ath, f))
        seen = {e.event_key for e in have.entries}
        for e in ath.entries:
            if e.event_key not in seen:
                have.entries.append(e)
                seen.add(e.event_key)

    def athletes(self) -> Iterator[Tuple[TeamRec, AthleteRec]]:
        for t in self.teams:
            for a in t.athletes:
                yield t, a


# --------------------------------------------------------------- report

@dataclass
class Change:
    kind: str                         # team | event | athlete | entry
    text: str                         # one line for the console
    athlete_id: str = ""
    team_id: str = ""
    event: Optional[int] = None
    applied: bool = True              # False: upstream differs, local kept (needs a decision)


@dataclass
class ImportReport:
    source: str = ""
    added: List[Change] = field(default_factory=list)
    changed: List[Change] = field(default_factory=list)
    gone: List[Change] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def conflicts(self) -> List[Change]:
        return [c for c in self.changed if not c.applied]

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.gone)

    def summary(self) -> str:
        s = f"{len(self.added)} added, {len(self.changed)} changed, {len(self.gone)} gone"
        if self.conflicts:
            s += f" ({len(self.conflicts)} need a decision)"
        return s

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------- public API

def import_milesplit(data, info: MeetInfo, rosters=None) -> Tuple[Meet, ImportReport]:
    """A new Meet from a MileSplit entries pull (list, {"data": [...]},
    JSON text/bytes or a file path).

    rosters: the attending teams' `team-rosters` pull. Given, every rostered
    athlete of a team in the entries becomes an Athlete with MileSplit ids
    and no entry, and the meet is marked full_rosters (scope "entered +
    full rosters"); left out, only entered athletes come in."""
    meet = Meet(meet=info)
    pull = parse_milesplit(data)
    if rosters is not None:
        add_rosters(pull, rosters, class_floor(info.date))
        info.full_rosters = True
    return meet, merge(meet, pull)


def refresh_milesplit(meet: Meet, data, previous=None, rosters=None,
                      previous_rosters=None) -> ImportReport:
    """Merge a newer MileSplit pull into `meet` in place. `previous` is the
    pull the meet was last built/refreshed from (enables the three-way merge,
    see the module docstring). A full_rosters meet needs `rosters` too (and
    `previous_rosters` for the three-way merge); without it every roster-only
    athlete would be reported gone, so it is refused."""
    if meet.meet.full_rosters and rosters is None:
        raise ValueError("this meet carries full rosters: refresh with the "
                         "team-rosters pull as well as the entries")
    pull = parse_milesplit(data)
    floor = class_floor(meet.meet.date)
    if rosters is not None:
        add_rosters(pull, rosters, floor)
    prev = None
    if previous is not None:
        prev = parse_milesplit(previous)
        if previous_rosters is not None:
            add_rosters(prev, previous_rosters, floor)
    return merge(meet, pull, prev)


def roster_teams(raw) -> list:
    """team-rosters as the API returns it - {team id: {universal id:
    {"athlete_id", "athlete": {"first_name", "last_name", "gender",
    "hs_graduation_year", "birth_date", "is_deleted"...}}}} - in the entries
    pull's per-team shape, which the importer and idmatch read. Anything else
    (already that shape) is returned as it is. (2026-10-05: the real shape was never parsed, so
    "full team rosters" added nobody.)"""
    if isinstance(raw, dict) and "data" in raw and len(raw) <= 3:
        raw = raw["data"]
    if not (isinstance(raw, dict) and raw and all(isinstance(v, (dict, list)) for v in raw.values())
            and not {"roster", "teams", "entries"} & set(raw)):
        return raw if raw is not None else []
    teams = []
    for tid, athletes in raw.items():
        if isinstance(athletes, list):                 # an empty roster comes as []
            athletes = dict(enumerate(athletes))
        if not isinstance(athletes, dict):
            continue
        roster = []
        for uid, rec in athletes.items():
            a = rec.get("athlete") if isinstance(rec, dict) else None
            if not isinstance(a, dict) or a.get("is_deleted"):
                continue
            roster.append({"athleteFirstName": a.get("first_name"), "athleteLastName": a.get("last_name"),
                           "athleteMileSplitId": a.get("id") or rec.get("athlete_id"),
                           "athleteYear": a.get("hs_graduation_year"), "athleteGender": a.get("gender"),
                           "athleteDob": a.get("birth_date"),
                           "athleteMileSplitUniversalId": uid if isinstance(uid, str) else None})
        teams.append({"teamMileSplitId": _int(tid), "teamName": "", "roster": roster})
    return teams


def class_floor(day: str) -> Optional[int]:
    """Earliest graduation year still in school on `day` (ISO): the school
    year ends in spring, so Oct 2026 -> 2027, Apr 2027 -> 2027."""
    m = re.match(r"(\d{4})-(\d{2})", day or "")
    return int(m.group(1)) + (int(m.group(2)) >= 7) if m else None


def add_rosters(pull: Pull, rosters, min_grad_year: Optional[int] = None) -> int:
    """Fold a MileSplit `team-rosters` pull (same per-team shape as entries;
    athletes may lack `entries`) into an entries Pull: rostered athletes of
    the teams already in it, without entries. Roster entries are ignored -
    the entries pull is what says who races. Teams that are not in the
    entries (not attending) are skipped. Returns how many athletes were
    added. (2026-10-03, user's ask: full rosters make walk-ups a lookup)
    min_grad_year: athletes who graduated before it are left out (2026-10-05:
    MileSplit rosters still list past seniors, one from 2010)."""
    roster_pull = parse_milesplit(roster_teams(rosters))
    before = sum(1 for _ in pull.athletes())
    skipped = graduated = 0
    for tr in roster_pull.teams:
        team = pull._teams.get(tr.key)
        if team is None:
            skipped += 1
            continue
        for ar in tr.athletes:
            if min_grad_year and ar.grad_year and ar.grad_year < min_grad_year:
                graduated += 1
                continue
            ar.entries = []
            ar.roster_only = True
            pull.add(team, ar)
    added = sum(1 for _ in pull.athletes()) - before
    pull.notes.extend(n for n in roster_pull.notes if "relay" not in n)
    pull.notes.append(f"rosters: {added} athletes not entered added"
                      + (f"; {graduated} graduated athletes left out" if graduated else "")
                      + (f"; {skipped} teams not in the entries skipped" if skipped else ""))
    return added


def import_hytek(src, info: MeetInfo, include_roster_only: bool = False
                 ) -> Tuple[Meet, ImportReport]:
    """A new Meet from a Hy-Tek semicolon entries file (path, text or bytes)."""
    meet = Meet(meet=info)
    return meet, merge(meet, parse_hytek(src, include_roster_only))


def import_csv(src, info: MeetInfo) -> Tuple[Meet, ImportReport]:
    """A new Meet from a plain CSV (path, text or bytes)."""
    meet = Meet(meet=info)
    return meet, merge(meet, parse_csv(src))


# --------------------------------------------------------------- MileSplit

def parse_milesplit(data) -> Pull:
    """MileSplit `.../meets/{meet}/entries` JSON -> Pull.

    Tolerates the {"data": [...]} wrapper, `teamId` for `teamMileSplitId`,
    athleteYear as int or string, lowercase genders, entryStatus as string.
    """
    if not isinstance(data, (list, dict)):
        data = json.loads(_read_text(data))
    if isinstance(data, dict):
        data = data.get("data", data)
        if isinstance(data, dict):
            data = data.get("teams") or data.get("entries") or []
    pull = Pull("milesplit")
    for t in data or []:
        name = str(t.get("teamName") or "").strip()
        tid = _int(t.get("teamMileSplitId", t.get("teamId")))
        unatt = _is_unattached(name)
        key = ("unattached" if unatt else f"ms:{tid}" if tid is not None
               else f"name:{_norm(name)}")
        team = TeamRec(key, name or "Unknown team",
                       abbrev=str(t.get("teamAbbreviation") or "").strip(),
                       milesplit_id=None if unatt else tid,
                       city=str(t.get("teamCity") or "").strip(),
                       state=str(t.get("teamState") or "").strip(), unattached=unatt)
        if t.get("relays"):
            pull.notes.append(f"{name}: {len(t['relays'])} relay entries ignored")
        pull.add(team)
        for a in t.get("roster") or []:
            ents = a.get("entries") or []
            g = _gender(a.get("athleteGender"))
            if g not in ("M", "F") and ents:
                g = _gender(ents[0].get("entryEventGender"))
            first = str(a.get("athleteFirstName") or "").strip()
            last = str(a.get("athleteLastName") or "").strip()
            if g not in ("M", "F") or not (first or last):
                pull.notes.append(f"{name}: skipped {first} {last} "
                                  f"(gender {a.get('athleteGender')!r})")
                continue
            gy, gt = _year(a.get("athleteYear"))
            uid = a.get("athleteMileSplitUniversalId")
            rec = AthleteRec(first, last, g, grad_year=gy, grade_text=gt,
                             milesplit_id=_int(a.get("athleteMileSplitId")),
                             universal_id=str(uid) if uid not in (None, "") else None,
                             dob=_iso_date(a.get("athleteDob")))
            for e in ents:
                rec.entries.append(EntryRec(
                    _gender(e.get("entryEventGender")) or g,
                    str(e.get("entryDivisionName") or "").strip(),
                    str(e.get("entryEventCode") or "").strip().upper(),
                    "scratched" if _int(e.get("entryStatus")) == -1 else "entered",
                    str(e.get("entrySeed") or "").strip()))
            pull.add(team, rec)
    return pull


# --------------------------------------------------------------- Hy-Tek

def parse_hytek(src, include_roster_only: bool = False) -> Pull:
    """Hy-Tek semicolon entries (MEET-RESEARCH.md §1) -> Pull.

    I;Last;First;Init;Gender;DOB;TeamCode;TeamName;Age;SchoolYear;...;Reg#(21);Comp#(22);Email
    D;Last;First;Init;Gender;DOB;TeamCode;TeamName;Age;Year;EventCode(10);Mark;M|E;Div(13);
      Comp#(14);PrevPlace;DeclStatus(16 D/S/A);Note;;Alternate

    As in ~/roster-bib-assigner (real exports): an I line with a lowercase
    gender is a roster-only athlete with no entries, skipped unless
    `include_roster_only` (or unless the file has nothing else). D lines
    carry the entries; D status S = scratched. Comp# becomes the bib.
    """
    text = _read_text(src)
    pull = Pull("hytek")
    other = 0
    roster_only: List[Tuple[TeamRec, AthleteRec]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        rec = [f.strip() for f in line.split(";")]
        kind = rec[0].upper()
        if kind not in ("I", "D"):
            other += 1
            continue
        rec += [""] * (24 - len(rec))
        last, first, graw = rec[1], rec[2], rec[4]
        g = _gender(graw)
        if g not in ("M", "F") or not (last or first):
            pull.notes.append(f"line {n}: skipped ({kind} record, gender {graw!r})")
            continue
        team = _hytek_team(rec[6], rec[7])
        gy, gt = _year(rec[9])
        comp = rec[22] if kind == "I" else rec[14]
        bib = str(int(comp)) if comp.isdigit() and int(comp) > 0 else ""
        if comp and not comp.isdigit():
            pull.notes.append(f"line {n}: competitor number {comp!r} is not a number, ignored")
        ath = AthleteRec(first, last, g, grad_year=gy, grade_text=gt,
                         dob=_iso_date(rec[5]), bib=bib)
        if kind == "I" and graw in ("m", "f"):
            ath.roster_only = True
            if not include_roster_only:
                roster_only.append((team, ath))
                continue
        if kind == "D":
            code = rec[10].upper()
            if not code:
                pull.notes.append(f"line {n}: entry without an event code, skipped")
                continue
            ath.entries.append(EntryRec(g, rec[13], code,
                                        "scratched" if rec[16].upper() == "S" else "entered",
                                        rec[11]))
        pull.add(team, ath)
    if roster_only:
        if any(True for _ in pull.athletes()):
            pull.notes.append(f"skipped {len(roster_only)} roster-only athletes "
                              "(lowercase gender, no entries)")
        else:
            # (2026-10-03) A file of nothing but lowercase I lines is a roster
            # export, not "everyone is roster-only": take them all.
            for team, ath in roster_only:
                pull.add(team, ath)
            pull.notes.append(f"no entered athletes; took all {len(roster_only)} roster lines")
    if other:
        pull.notes.append(f"ignored {other} lines that are not I or D records")
    return pull


def _hytek_team(code: str, name: str) -> TeamRec:
    code, name = code.strip().upper(), name.strip()
    if (not code and not name) or _is_unattached(name) or code in ("UNA", "UNAT"):
        return TeamRec("unattached", "Unattached", unattached=True)
    return TeamRec(f"code:{code}" if code else f"name:{_norm(name)}", name or code, abbrev=code)


# --------------------------------------------------------------- CSV

_CSV_COLS = {
    "first": ("first", "firstname", "fname", "givenname"),
    "last": ("last", "lastname", "lname", "surname", "familyname"),
    "name": ("name", "athlete", "athletename", "fullname"),
    "gender": ("gender", "sex", "mf"),
    "grade": ("grade", "year", "yr", "class", "gradyear", "graduationyear", "schoolyear",
              "classyear"),
    "team": ("team", "school", "teamname", "schoolname", "affiliation", "club"),
    "code": ("teamcode", "code", "abbr", "abbrev", "abbreviation", "teamabbreviation",
             "schoolcode"),
    "bib": ("bib", "bibnumber", "bibno", "comp", "compno", "compnumber", "competitor",
            "competitornumber", "number", "no"),
    "event": ("event", "eventcode", "distance"),
    "division": ("division", "div", "level", "race", "racedivision", "section"),
}


def parse_csv(src) -> Pull:
    """Header-driven CSV (comma, semicolon or tab) -> Pull.

    Columns (any order, header names are forgiving, see _CSV_COLS): first +
    last (or one `name` as "Last, First" / "First Last"), gender, and
    optionally grade/year (a 4-digit value is a graduation year), team,
    team code, bib, event, division. `event` is taken as an event code when
    it reads as a distance ("5000", "5K", "2MILE"); otherwise its text is the
    division. A row with no event or division gives an athlete without
    entries. A blank team = unattached.
    """
    text = _read_text(src)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)
    if not rows:
        raise ValueError("The CSV is empty.")
    header = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in rows[0]]
    col: Dict[str, int] = {}
    for name, aliases in _CSV_COLS.items():
        for i, h in enumerate(header):
            if h in aliases:
                col[name] = i
                break
    if not (("first" in col and "last" in col) or "name" in col) or "gender" not in col:
        raise ValueError("The CSV needs first + last (or name) and gender columns; "
                         f"found {', '.join(rows[0])}.")
    pull = Pull("csv")

    def cell(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(rows[1:], 2):
        if not any(c.strip() for c in row):
            continue
        first, last = cell(row, "first"), cell(row, "last")
        if not (first or last) and cell(row, "name"):
            full = cell(row, "name")
            if "," in full:
                last, first = (p.strip() for p in full.split(",", 1))
            else:
                first, _, last = full.rpartition(" ")
                first, last = first.strip(), last.strip()
        g = _gender(cell(row, "gender"))
        if g not in ("M", "F") or not (first or last):
            pull.notes.append(f"row {n}: skipped {first} {last} "
                              f"(gender {cell(row, 'gender')!r})")
            continue
        team_name, code = cell(row, "team"), cell(row, "code").upper()
        team = _hytek_team(code, team_name)
        gy, gt = _year(cell(row, "grade"))
        bib = cell(row, "bib")
        if bib and not bib.isdigit():
            pull.notes.append(f"row {n}: bib {bib!r} is not a number, ignored")
            bib = ""
        ath = AthleteRec(first, last, g, grad_year=gy, grade_text=gt,
                         bib=str(int(bib)) if bib and int(bib) > 0 else "")
        ev, div = cell(row, "event"), cell(row, "division")
        code_ev = ev.upper() if ev and distance_m(ev) else ""
        if ev and not code_ev and not div:
            div = ev
        if code_ev or div:
            ath.entries.append(EntryRec(g, div, code_ev))
        pull.add(team, ath)
    return pull


# --------------------------------------------------------------- event naming

_GENDER_WORDS = {"boys", "boy", "girls", "girl", "men", "mens", "women", "womens",
                 "male", "female", "mixed", "coed"}


def division_level(division: str) -> str:
    """'Varsity Boys' -> 'Varsity', 'JV Girls' -> 'JV', 'Boys' -> ''."""
    words = [w for w in str(division or "").split()
             if w.lower().replace("'", "").replace("’", "") not in _GENDER_WORDS]
    return " ".join(words)


def distance_m(code: str) -> int:
    """Metres from an event code: '5000' 5000, '5K' 5000, '2MILE' 3219,
    '1MILE' 1609, '3200' 3200; 0 when it is not a distance."""
    c = re.sub(r"\s+", "", str(code or "")).upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:MILES?|MI)", c)
    if m:
        return int(round(float(m.group(1)) * 1609.344))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)KM?", c)
    if m:
        return int(round(float(m.group(1)) * 1000))
    m = re.fullmatch(r"(\d+)M?", c)
    return int(m.group(1)) if m else 0


def _distance_label(code: str) -> str:
    c = re.sub(r"\s+", "", str(code or "")).upper()
    if not c:
        return ""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:MILES?|MI)", c)
    if m:
        return f"{_num(m.group(1))} Mile"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)KM?", c)
    if m:
        return f"{_num(m.group(1))}k"
    m = re.fullmatch(r"(\d+)M?", c)
    if m:
        d = int(m.group(1))
        return f"{d // 1000}k" if d and d % 1000 == 0 else f"{d} Meter"
    return str(code).strip()


def event_name(gender: str, code: str, division: str) -> str:
    """Hy-Tek style XC name: 'Girls 5k Run CC Varsity', 'Boys 2 Mile Run CC
    JV', 'Girls 3200 Meter Run CC Middle School'; no code -> 'Girls Varsity'."""
    parts = [{"F": "Girls", "M": "Boys"}.get(gender, "Mixed")]
    label = _distance_label(code)
    if label:
        parts.append(f"{label} Run CC")
    level = division_level(division)
    if level:
        parts.append(level)
    return " ".join(parts)


def _event_order(e: EntryRec) -> tuple:
    """Default numbering, see the module docstring (2026-10-03)."""
    lvl = division_level(e.division).casefold().replace(".", "")
    rank = 0 if lvl in ("", "varsity", "v", "var") else \
        1 if lvl in ("jv", "junior varsity", "j v") else 2
    return (rank, lvl if rank == 2 else "", {"F": 0, "M": 1}.get(e.gender, 2),
            distance_m(e.code), e.division.casefold(), e.code)


def _event_key(gender: str, division: str, code: str) -> Tuple[str, str, str]:
    return (gender, _norm(division), str(code or "").strip().upper())


# --------------------------------------------------------------- teams

_SCHOOL_SUFFIX = re.compile(r"\s+(senior high school|high school|senior high|high|h\.?\s?s\.?)$",
                            re.I)
_NAME_FILLER = {"high", "school", "hs", "the", "of", "senior", "middle"}


def short_name(name: str, teams: List[Team], code: str = "") -> str:
    """'Ridge High School' -> 'Ridge', 'Ridge Middle School' -> 'Ridge MS';
    the full name when the short one is taken (short is what .evt/.ppl and
    the team scores key on, so it must be unique)."""
    s = _SCHOOL_SUFFIX.sub("", name.strip()).strip()
    s = re.sub(r"\s+Middle School$", " MS", s, flags=re.I) or name.strip()
    taken = {(t.short or t.name).casefold() for t in teams}
    for cand in (s, name.strip(), f"{name.strip()} ({code})"):
        if cand and cand.casefold() not in taken:
            return cand
    return f"{name.strip()} ({len(teams) + 1})"


def team_code(abbrev: str, name: str, teams: List[Team]) -> str:
    """Upper-cased MileSplit abbreviation cut to 4; derived from the name
    when blank; a digit suffix when taken ('RID' -> 'RID2', 'RID1' -> 'RID2')."""
    taken = {t.code.upper() for t in teams if t.code}
    base = re.sub(r"[^A-Z0-9]", "", str(abbrev or "").upper())[:4]
    if not base:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in _NAME_FILLER]
        base = ("".join(w[0] for w in words) if len(words) >= 2
                else words[0] if words else "TEAM")[:4].upper()
    if base not in taken:
        return base
    for n in range(2, 10000):
        cand = base[:4 - len(str(n))] + str(n)
        if cand not in taken:
            return cand
    raise ValueError("no free team code")  # pragma: no cover


def _find_team(meet: Meet, tr: TeamRec) -> Optional[Team]:
    if tr.milesplit_id is not None:
        t = next((t for t in meet.teams if t.milesplit_id == tr.milesplit_id), None)
        if t:
            return t
    if tr.abbrev and tr.milesplit_id is None:
        t = next((t for t in meet.teams if t.code.upper() == tr.abbrev.upper()), None)
        if t:
            return t
    return next((t for t in meet.teams if _norm(t.name) == _norm(tr.name)
                 and (t.milesplit_id is None or tr.milesplit_id is None)), None)


# --------------------------------------------------------------- merge

_IDENTITY = ("first", "last", "gender", "grad_year", "grade_text", "universal_id", "dob")


def merge(meet: Meet, pull: Pull, previous: Optional[Pull] = None) -> ImportReport:
    """Merge `pull` into `meet` in place and report what happened.

    Match teams by MileSplit id (else code, else name); athletes by
    (MileSplit id, team), falling back to the MileSplit id alone when the
    operator moved the athlete to another team, or by (name, gender, team)
    for Hy-Tek/CSV; entries by (athlete, scoring event); events by
    (gender, division, code), so renamed events and races still match.
    Never deletes anything and never touches bibs, local athletes, event or
    race names, race order or lanes.
    """
    src = _SOURCE_LABEL.get(pull.source, pull.source)
    rep = ImportReport(source=pull.source, notes=list(pull.notes))
    prev_index = ({(t.key, a.key): a for t, a in previous.athletes()}
                  if previous is not None else {})

    # ---- teams
    team_map: Dict[str, Optional[str]] = {}
    for tr in pull.teams:
        if tr.unattached:
            team_map[tr.key] = None
            continue
        t = _find_team(meet, tr)
        if t is None:
            code = team_code(tr.abbrev, tr.name, meet.teams)
            t = Team(id=meet.new_team_id(), name=tr.name,
                     short=short_name(tr.name, meet.teams, code), code=code,
                     milesplit_id=tr.milesplit_id, city=tr.city, state=tr.state)
            meet.teams.append(t)
            rep.added.append(Change("team", f"team {t.name} ({t.code})", team_id=t.id))
        else:
            if t.milesplit_id is None and tr.milesplit_id is not None:
                t.milesplit_id = tr.milesplit_id
            t.city = t.city or tr.city
            t.state = t.state or tr.state
        team_map[tr.key] = t.id
    if pull.source == "milesplit":
        pulled = {tr.milesplit_id for tr in pull.teams if tr.milesplit_id is not None}
        for t in meet.teams:
            if t.milesplit_id is not None and t.milesplit_id not in pulled:
                rep.gone.append(Change("team", f"team {t.name} is not in the {src} (kept)",
                                       team_id=t.id))

    # ---- events (+ one race each), new ones in the default order
    ev_by_key: Dict[tuple, int] = {}
    for ev in meet.events:
        ev_by_key.setdefault(_event_key(ev.gender, ev.division, ev.code), ev.num)
    wanted: Dict[tuple, EntryRec] = {}
    for _, ar in pull.athletes():
        for e in ar.entries:
            wanted.setdefault(e.event_key, e)
    num = max((e.num for e in meet.events), default=0)
    order = max((r.order for r in meet.races), default=0)
    for k in sorted((k for k in wanted if k not in ev_by_key),
                    key=lambda k: _event_order(wanted[k])):
        e = wanted[k]
        num += 1
        ev = Event(num, event_name(e.gender, e.code, e.division), gender=e.gender,
                   distance_m=distance_m(e.code), division=e.division, code=e.code)
        meet.events.append(ev)
        if meet.race(num) is None:
            order += 1
            meet.races.append(Race(num, order=order))
        ev_by_key[k] = num
        rep.added.append(Change("event", f"event {num} {ev.name}", event=num))

    home: Dict[int, Race] = {}

    def race_for(n: int) -> Race:
        """Where a new entry for scoring event n goes: the race already
        holding most of that event's entries (combined starts), else the
        event's own race."""
        if n not in home:
            counts = Counter(r.key for r in meet.races for en in r.entries if en.event == n)
            r = meet.race(*counts.most_common(1)[0][0]) if counts else meet.race(n)
            if r is None:
                r = Race(n, order=max((x.order for x in meet.races), default=0) + 1)
                meet.races.append(r)
            home[n] = r
        return home[n]

    def ev_label(n: int) -> str:
        ev = meet.event(n)
        return ev.name if ev else f"event {n}"

    # ---- athletes: match
    teams_by_id = {t.id: t for t in meet.teams}

    def label(a) -> str:
        t = teams_by_id.get(a.team_id)
        return f"{a.first} {a.last} ({(t.short or t.name) if t else 'unattached'})"

    by_ms_team: Dict[tuple, Athlete] = {}
    by_name_team: Dict[tuple, Athlete] = {}
    by_ms: Dict[int, List[Athlete]] = {}
    for a in meet.athletes:
        if a.milesplit_id is not None:
            by_ms_team.setdefault((a.team_id, a.milesplit_id), a)
            by_ms.setdefault(a.milesplit_id, []).append(a)
        by_name_team.setdefault((a.team_id, _norm(a.last), _norm(a.first), a.gender), a)
    pairs = list(pull.athletes())
    matched: Dict[int, Athlete] = {}
    claimed = set()
    for i, (tr, ar) in enumerate(pairs):
        tid = team_map[tr.key]
        a = (by_ms_team.get((tid, ar.milesplit_id)) if ar.milesplit_id is not None
             else by_name_team.get((tid, _norm(ar.last), _norm(ar.first), ar.gender)))
        if a is not None and a.id not in claimed:
            matched[i] = a
            claimed.add(a.id)
    for i, (tr, ar) in enumerate(pairs):     # moved to another team locally
        if i in matched or ar.milesplit_id is None:
            continue
        cands = [a for a in by_ms.get(ar.milesplit_id, []) if a.id not in claimed]
        if len(cands) == 1:
            matched[i] = cands[0]
            claimed.add(cands[0].id)

    used_bibs = set(meet.used_bibs())
    next_id = int(meet.new_athlete_id()[1:])

    for i, (tr, ar) in enumerate(pairs):
        tid = team_map[tr.key]
        base = prev_index.get((tr.key, ar.key)) if previous is not None else None
        up: Dict[int, EntryRec] = {}
        for e in ar.entries:
            up.setdefault(ev_by_key[e.event_key], e)
        a = matched.get(i)

        if a is None:                                    # ---- new athlete
            a = Athlete(id=f"a{next_id}", first=ar.first, last=ar.last, gender=ar.gender,
                        team_id=tid, grad_year=ar.grad_year, grade_text=ar.grade_text,
                        milesplit_id=ar.milesplit_id, universal_id=ar.universal_id,
                        source=pull.source, dob=ar.dob, roster_only=ar.roster_only)
            next_id += 1
            if ar.bib:
                if ar.bib in used_bibs:
                    rep.notes.append(f"{label(a)}: bib {ar.bib} already used, left blank")
                else:
                    a.bib = ar.bib
                    used_bibs.add(ar.bib)
            if ar.milesplit_id is not None:
                for o in meet.athletes:
                    if (o.milesplit_id is None and o.team_id == tid and o.gender == a.gender
                            and _norm(o.first) == _norm(a.first)
                            and _norm(o.last) == _norm(a.last)):
                        rep.notes.append(f"{label(a)} from the {src} has the same name as "
                                         f"local athlete {o.id}; both kept (review ids)")
            meet.athletes.append(a)
            claimed.add(a.id)
            for n, e in up.items():
                _add_entry(rep, race_for(n), a, n, e, label)
            where = ", ".join(ev_label(n) for n in up) or "no entries"
            rep.added.append(Change("athlete", f"{label(a)}: {where}", athlete_id=a.id,
                                    team_id=tid or ""))
            continue

        if not ar.roster_only:
            a.roster_only = False                        # entered upstream: stays bib-eligible
        # ---- matched athlete: identity fields (three-way when base known)
        for f in _IDENTITY:
            new, cur = getattr(ar, f), getattr(a, f)
            if new in (None, "") or new == cur:
                continue
            if base is not None and getattr(base, f) == new:
                continue                                 # upstream unchanged: local edit stays
            if base is not None and getattr(base, f) != cur:
                rep.changed.append(Change("athlete", f"{label(a)}: {f} is {new!r} upstream, "
                                          f"kept local {cur!r}", athlete_id=a.id,
                                          applied=False))
                continue
            setattr(a, f, new)
            rep.changed.append(Change("athlete", f"{label(a)}: {f} {cur!r} -> {new!r}",
                                      athlete_id=a.id))
        if ar.bib and not a.bib and ar.bib not in used_bibs:
            a.bib = ar.bib
            used_bibs.add(ar.bib)
            rep.changed.append(Change("athlete", f"{label(a)}: bib {ar.bib}", athlete_id=a.id))

        # ---- matched athlete: entries
        local: Dict[int, Tuple[Race, Entry]] = {}
        for r, en in meet.entries_for(a.id):
            local.setdefault(en.event, (r, en))
        base_up: Dict[int, EntryRec] = {}
        if base is not None:
            for e in base.entries:
                n = ev_by_key.get(e.event_key)
                if n is not None:
                    base_up.setdefault(n, e)
        for n, e in up.items():                          # statuses + seeds
            if n not in local:
                continue
            en = local[n][1]
            was = base_up[n].status if n in base_up else "entered"
            if e.status != was:                          # changed upstream
                if en.status == was:
                    rep.changed.append(Change("entry", f"{label(a)}: {ev_label(n)} "
                                              f"{en.status} -> {e.status}",
                                              athlete_id=a.id, event=n))
                    en.status = e.status
                elif en.status != e.status:
                    rep.changed.append(Change("entry", f"{label(a)}: {ev_label(n)} is "
                                              f"{e.status} upstream, kept local {en.status}",
                                              athlete_id=a.id, event=n, applied=False))
            if e.seed and e.seed != en.seed:
                en.seed = e.seed
        missing = [n for n in up if n not in local]      # upstream has, local doesn't
        extra = [n for n in local if n not in up]        # local has, upstream doesn't
        if base is not None:
            # Three-way: only what changed upstream since the last pull acts.
            removed = [n for n in extra if n in base_up]
            added = [n for n in missing if n not in base_up]
            if len(removed) == 1 and len(added) == 1:
                r_old, en = local[removed[0]]
                _move_entry(r_old, en, race_for(added[0]), added[0])
                rep.changed.append(Change("entry", f"{label(a)}: moved {ev_label(removed[0])} "
                                          f"-> {ev_label(added[0])}", athlete_id=a.id,
                                          event=added[0]))
                removed, added = [], []
            for n in added:
                if _add_entry(rep, race_for(n), a, n, up[n], label):
                    rep.added.append(Change("entry", f"{label(a)}: {ev_label(n)}",
                                            athlete_id=a.id, event=n))
            for n in removed:
                rep.gone.append(Change("entry", f"{label(a)}: {ev_label(n)} is not in the "
                                       f"{src} (kept)", athlete_id=a.id, event=n))
        elif missing and extra:
            # (2026-10-03) No previous pull: an upstream division move looks
            # exactly like an operator move. Keep local, ask.
            rep.changed.append(Change(
                "entry", f"{label(a)}: {src} has {', '.join(ev_label(n) for n in missing)}; "
                f"local has {', '.join(ev_label(n) for n in extra)} (kept local)",
                athlete_id=a.id, event=missing[0], applied=False))
        else:
            for n in missing:
                if _add_entry(rep, race_for(n), a, n, up[n], label):
                    rep.added.append(Change("entry", f"{label(a)}: {ev_label(n)}",
                                            athlete_id=a.id, event=n))
            for n in extra:
                rep.gone.append(Change("entry", f"{label(a)}: {ev_label(n)} is not in the "
                                       f"{src} (kept)", athlete_id=a.id, event=n))

    # ---- athletes of this source the pull no longer has
    for a in meet.athletes:
        if a.id in claimed or a.source != pull.source:
            continue
        n = len(meet.entries_for(a.id))
        rep.gone.append(Change("athlete", f"{label(a)} is not in the {src} "
                               f"(kept, {n} {'entry' if n == 1 else 'entries'})",
                               athlete_id=a.id, team_id=a.team_id or ""))
    return rep


def _add_entry(rep: ImportReport, race: Race, a: Athlete, n: int,
               e: EntryRec, label) -> bool:
    if any(x.athlete_id == a.id for x in race.entries):
        # Two scoring events in one combined start: the model allows one
        # entry per athlete per race.
        rep.notes.append(f"{label(a)} is already in race {race.event}; "
                         f"entry for event {n} not added")
        return False
    race.entries.append(Entry(a.id, n, status=e.status, seed=e.seed))
    return True


def _move_entry(old: Race, en: Entry, new: Race, n: int) -> None:
    en.event = n
    if new is not old:
        old.entries.remove(en)
        en.lane = None
        new.entries.append(en)


# --------------------------------------------------------------- helpers

def _read_text(src) -> str:
    """Text from bytes, a path, or text; UTF-8 (BOM ok), else cp1252 (Hy-Tek
    exports from Windows)."""
    if isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    elif isinstance(src, os.PathLike) or (isinstance(src, str) and "\n" not in src
                                          and len(src) < 1024 and os.path.isfile(src)):
        with open(src, "rb") as f:
            data = f.read()
    else:
        return str(src)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


_MOJIBAKE = re.compile("[Â-ô][\u0080-¿ŒœŠšŸŽžƒˆ˜–-›€™]")


def _cp1252_bytes(s: str) -> bytes:
    """The bytes text was misread from: cp1252, and Latin-1 for the five
    codes cp1252 leaves undefined (a decoder passes those straight through)."""
    out = bytearray()
    for ch in s:
        out += bytes([ord(ch)]) if ord(ch) < 256 else ch.encode("cp1252")
    return bytes(out)


def fix_text(s):
    """Repair UTF-8 text that was read as Windows-1252 somewhere upstream:
    "Nyâ€™An" -> "Ny'An" (with a curly apostrophe), "JosÃ©" -> "José". Kept
    as it is unless it looks garbled and the bytes decode as strict UTF-8,
    so real accented names (José, Zoë, SÃO in capitals) are left alone; a
    name garbled twice is repaired twice. (2026-10-08: MileSplit's roster
    for Crescent had "Nyâ€™An")"""
    if not isinstance(s, str):
        return s
    for _ in range(2):
        if not _MOJIBAKE.search(s):
            break
        try:
            s = _cp1252_bytes(s).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
    return s


def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def _num(s: str) -> str:
    f = float(s)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _gender(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("m", "male", "boy", "boys", "b", "men", "man"):
        return "M"
    if s in ("f", "female", "girl", "girls", "g", "w", "women", "woman"):
        return "F"
    if s in ("x", "c", "mixed", "coed"):
        return "X"
    return ""


def _year(v) -> Tuple[Optional[int], str]:
    """(graduation year, free-text year): '2027' -> (2027, ''), '12' -> (None, '12')."""
    s = str(v).strip() if v is not None else ""
    if re.fullmatch(r"(19|20)\d\d", s):
        return int(s), ""
    return None, "" if s == "0" else s


def _iso_date(v) -> str:
    s = str(v or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def _is_unattached(name: str) -> bool:
    return _norm(name).startswith("unattached")
