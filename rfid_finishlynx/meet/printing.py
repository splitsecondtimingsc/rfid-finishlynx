"""Printed meet documents: results, start lists, team bib sheets, rosters.

Every document is built once as fixed-width text in the Hy-Tek layout that
coaches and MileSplit readers already know (docs/MEET-RESEARCH.md §1 "Printed
results"), then turned into a PDF or an HTML page from that same text. One
source means the .txt, the PDF and the browser printout can never disagree.

Why a home-made PDF writer (2026-10-03): the timing net is offline and every
new dependency has to be wheeled into `wheels/`. A Courier-only PDF of
monospace lines needs nothing but the standard fonts every reader carries, so
~80 lines of stdlib code replace reportlab.

Input: a final race's frozen results (`race.results`, written by scoring.py):
    {"frozen_at": ..., "finishers": [{place, bib, athlete_id, event, elapsed,
     status, event_place, points, scorer, displacer, fl_only}, ...],
     "teams": {"<event num>": [{rank, team_id, score, complete, count,
     runners: [{athlete_id, bib, points, scorer}], tiebreak_points,
     total_time, avg_time}, ...]}}
Exhibition runners are told apart by their race Entry.status, not by the
results (they simply carry no points).

Conventions decided here (2026-10-03):
- Times print to hundredths rounded UP (NFHS / USATF: a FAT time that is not
  an exact hundredth goes to the next hundredth), h:mm:ss.hh over an hour.
  The thousandths are rounded first so float noise (983.52 stored as
  983.5200000001) can't bump a time up a hundredth.
- Avg/Mile is to the nearest second (it is informational, not a mark).
- Exhibition: "EX" in the Points column (results) / Note column (start list),
  with a legend under the section.
- Partial teams are listed under the ranked teams, unranked, with "3/5" in
  the Total column and "incomplete, not scored".
- DNF / DQ / DNS sit at the bottom of their section with "--" as the place.
- A combined gun start prints one section per scoring event (entry.event),
  Hy-Tek style, each with its own places and team scores.
- Start lists are sorted by team then last/first name (check-in tables work
  team by team); bib sheets by bib, one team per page ("\\f" in the text).
- Columns are fixed width; anything too long is cut, never pushed right.
  A team's full name is used when it fits, else its short name.

Added 2026-10-03 (results filters, rosters report):
- Data first: filter_results() and roster_pages() hold the rules as plain
  data; the text here and the PDF renderer (pdf_reports.py) only lay them
  out, so the two outputs can't apply different rules.
- Results filters (places / grades / school) print real places, never
  renumbered, and say what was filtered on the header's third line (so it
  repeats on every page). Team scores: full block unless a school or grade
  filter is set; a school filter keeps only those schools' rows; a grade
  filter drops them with a one-line note.
- Rosters: a page per school, girls left / boys right; a page per gender
  when either overflows a column. Geometry in the rosters section notes.
- Column definitions are the *_COLS constants: change a width there and
  every row and heading follows.

Added 2026-10-03 (results print options, MeetPro's results dialog):
- hide=("DNF", "DNS", "DQ"), any subset: those rows are left off.
- events=[scoring event nums]: only those scoring divisions of a combined
  gun start (JV only from a combined start), sections and team scores.
- Both say so in the header line like the other filters.

Added 2026-10-06 (divisions, divisions.py):
- A race scored in divisions prints, after each scoring event's section,
  one section per division: places and team scores within the division,
  same columns. The filters apply to them too (places = division places).
- divisions=None/"all" prints them (the default), "none" leaves them out,
  [ids] prints only those divisions' sections, without the race's own.
  A meet without divisions prints exactly as before.
"""

from __future__ import annotations

import copy
import dataclasses
import html
import math
import re
import textwrap
import unicodedata
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rfid_finishlynx.meet import divisions as divs_mod
from rfid_finishlynx.meet import scoring
from rfid_finishlynx.meet.model import (Athlete, Entry, Meet, Race, Team, grade_key,
                                        start_time_text)

APP_NAME = "rfid-meet"
METERS_PER_MILE = 1609.344

# Bottom-of-section order for runners who didn't finish.
STATUS_ORDER = {"finished": 0, "DNF": 1, "DQ": 2, "DNS": 3}

# Free-text years from Hy-Tek / CSV imports shortened to fit the Yr column.
_YEAR_WORDS = {"freshman": "FR", "sophomore": "SO", "junior": "JR", "senior": "SR"}

# A column: (header label, width, align) with align "<" or ">".
Col = Tuple[str, int, str]

RESULT_COLS: List[Col] = [("Pl", 3, ">"), ("Bib", 6, ">"), ("Name", 24, "<"), ("Yr", 2, ">"),
                          ("School", 20, "<"), ("Finals", 11, ">"), ("Avg/Mile", 8, ">"),
                          ("Points", 6, ">")]
START_COLS: List[Col] = [("No", 3, ">"), ("Bib", 6, ">"), ("Name", 24, "<"), ("Yr", 2, ">"),
                         ("School", 20, "<"), ("Seed", 10, ">"), ("Note", 4, "<")]
BIB_COLS: List[Col] = [("Bib", 5, ">"), ("Name", 24, "<"), ("Sex", 3, "<"), ("Yr", 2, ">"),
                       ("Race", 32, "<")]
TEAM_NAME_W = 26
# split_header takes at most 5 lines above the first blank as the repeated
# page header: stamp + title + up to 3 filter lines. (2026-10-03)
HEADER_NOTE_LINES = 3
FILTER_SEP = " · "               # cp1252 has the middle dot, so the PDF keeps it


# --------------------------------------------------------------- formatting

def format_time(seconds: Optional[float]) -> str:
    """16:23.52 / 1:23:57.35, hundredths rounded up (see module notes)."""
    if seconds is None:
        return ""
    ms = max(0, int(round(float(seconds) * 1000)))
    hund = -(-ms // 10)
    h, rem = divmod(hund, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    if h:
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
    return f"{m}:{s:02d}.{cs:02d}"


def format_pace(seconds: Optional[float], distance_m: Optional[float]) -> str:
    """Minutes per mile, m:ss to the nearest second; '' without a distance."""
    if not seconds or not distance_m or distance_m <= 0:
        return ""
    s = int(seconds * METERS_PER_MILE / distance_m + 0.5)
    return f"{s // 60}:{s % 60:02d}"


def _fit(text, width: int, align: str = "<") -> str:
    """Exactly `width` characters: cut when too long so no column moves."""
    text = unicodedata.normalize("NFC", "" if text is None else str(text))[:width]
    return text.rjust(width) if align == ">" else text.ljust(width)


def _row(cols: Sequence[Col], cells: Sequence) -> str:
    return " ".join(_fit(c, w, a) for (_, w, a), c in zip(cols, cells)).rstrip()


def _col_header(cols: Sequence[Col]) -> str:
    return _row(cols, [label for label, _, _ in cols])


def _width(cols: Sequence[Col]) -> int:
    return sum(w for _, w, _ in cols) + len(cols) - 1


def _center(text: str, width: int) -> str:
    return text.center(width).rstrip() if len(text) < width else text


def _us_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%m/%d/%Y")
    except ValueError:
        return iso


def _stamp(dt: datetime) -> str:
    """'10/25/2026 12:05 PM' (strftime has no portable no-pad hour on Windows)."""
    hour = dt.hour % 12 or 12
    return f"{dt:%m/%d/%Y} {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _header(meet: Meet, what: str, width: int, printed_at: Optional[datetime],
            note: str = "", fit: bool = False) -> List[str]:
    """The two centred Hy-Tek header lines and a blank line. to_pdf repeats
    everything above the first blank line on every page.

    note: what a filtered printout left out, wrapped onto at most
    HEADER_NOTE_LINES centred lines so a long school list can't widen the
    page (2026-10-03). fit: never wider than `width` (drop the host, then
    cut) for documents whose paging depends on the page geometry (rosters)."""
    info = meet.meet
    title = info.name or info.id
    if info.date:
        title += f" - {_us_date(info.date)}"
    host = f" / Hosted by {info.host}" if info.host else ""
    if fit and len(title + host + f" / {what}") > width:
        host = ""
    title += host + f" / {what}"
    if fit and len(title) > width:
        title = title[:width]
    stamp = f"{APP_NAME}  {_stamp(printed_at or datetime.now())}"
    lines = [_center(stamp, width), _center(title, width)]
    if note:
        wrapped = textwrap.wrap(note, width) or [note[:width]]
        if len(wrapped) > HEADER_NOTE_LINES:
            wrapped = wrapped[:HEADER_NOTE_LINES]
            wrapped[-1] = wrapped[-1][:width - 3].rstrip() + "..."
        lines += [_center(l, width) for l in wrapped]
    return lines + [""]


def _grade(meet: Meet, a: Optional[Athlete]) -> str:
    if a is None:
        return ""
    g = meet.grade(a).strip()
    return _YEAR_WORDS.get(g.lower(), g[:2])


def _bib(bib: str) -> str:
    """'#  214' as Hy-Tek prints it; a 6-digit bib drops the '#' to fit."""
    if not bib:
        return ""
    return f"#{bib:>5}" if len(bib) <= 5 else bib


def _name(a: Optional[Athlete], bib: str = "") -> str:
    if a is None:
        return f"Unknown bib {bib}".strip() if bib else "Unknown"
    first = a.first.strip()
    return f"{a.last.strip()}, {first}" if first else a.last.strip()


def _team_label(team: Optional[Team], width: int) -> str:
    """Full name if it fits, else the short name, else the name cut."""
    if team is None:
        return "Unattached"
    if len(team.name) <= width or not team.short:
        return team.name
    return team.short


def _sort_key(text: str) -> str:
    """Accent- and case-blind, so Núñez sorts with the Ns."""
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c)).casefold()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _athlete_team(meet: Meet, a: Optional[Athlete], width: int) -> str:
    if a is None:
        return ""
    return _team_label(meet.team(a.team_id), width)


def _event_name(meet: Meet, num: int, race: Optional[Race] = None) -> str:
    ev = meet.event(num)
    if ev:
        return ev.name
    return (race.name if race and race.name else "") or f"Event {num}"


def _event_heading(meet: Meet, num: int, race: Optional[Race], width: int) -> List[str]:
    return [f"Event {num}  {_event_name(meet, num, race)}", "=" * width]


def _join(blocks: Iterable[List[str]]) -> str:
    return "\n".join(line.rstrip() for block in blocks for line in block) + "\n"


# --------------------------------------------------------------- results

def _race_events(race: Race) -> List[int]:
    """Scoring events a race prints, in number order: every non-scratched
    entry's event plus any the frozen results mention."""
    nums = {e.event for e in race.entries if e.status != "scratched"}
    res = race.results or {}
    nums.update(f.get("event") for f in res.get("finishers", []) if f.get("event") is not None)
    nums.update(int(k) for k in (res.get("teams") or {}))
    return sorted(nums) or [race.event]


# Shared with divisions (model.grade_key, 2026-10-06).
_grade_key = grade_key


def _grade_sort(key: str):
    """Numeric grades in number order, anything else after them."""
    return (not key.isdigit(), int(key) if key.isdigit() else 0, key)


def _place_range(places) -> Tuple[int, Optional[int]]:
    first, last = places
    first = int(first) if first else 1
    last = int(last) if last is not None else None
    if first < 1 or (last is not None and last < first):
        raise ValueError(f"place range {places!r}")
    return first, last


def _hide_set(hide) -> Optional[List[str]]:
    """hide=("DNF", "DQ") -> ["DNF", "DQ"] in the bottom-of-section order;
    None/empty = hide nothing. A string is one status, not its letters."""
    if not hide:
        return None
    if isinstance(hide, str):
        hide = [hide]
    wanted = {str(h).strip().upper() for h in hide}
    bad = wanted - set(scoring.NON_FINISH)
    if bad:
        raise ValueError(f"hide: {', '.join(sorted(bad))} is not one of "
                         f"{', '.join(scoring.NON_FINISH)}")
    return [st for st in scoring.NON_FINISH if st in wanted]


def _event_set(events) -> Optional[List[int]]:
    """events=[3, "2"] -> [2, 3]; None/empty = every scoring event."""
    if not events:
        return None
    if isinstance(events, (int, str)):
        events = [events]
    return sorted({int(e) for e in events})


def describe_filter(meet: Meet, places: Optional[Tuple[int, Optional[int]]] = None,
                    grades: Optional[Iterable] = None,
                    team_ids: Optional[Sequence[Optional[str]]] = None,
                    hide: Optional[Iterable[str]] = None,
                    events: Optional[Iterable[int]] = None,
                    divisions=None) -> str:
    """One line saying what a filtered printout keeps, '' when unfiltered:
    'Places 1-25 · Grades 9, 10 · Ridge HS, Valley HS'. The console shows it
    next to the print button and the printout puts it in its header, so a
    sheet taped to the wall can't pass for the full results. (2026-10-03)

    events (2026-10-03, print options): 'Event 3 Girls 5k Run CC JV' for
    one, 'Events 2, 3' for several, first, since it says which section the
    sheet is; hide: 'No DNF, DNS', last. divisions (2026-10-06): 'Division
    Small Schools' for a division-only printout, after the events."""
    parts = []
    nums = _event_set(events)
    if nums:
        if len(nums) == 1:
            ev = meet.event(nums[0])
            parts.append(f"Event {nums[0]}" + (f" {ev.name}" if ev and ev.name else ""))
        else:
            parts.append("Events " + ", ".join(str(n) for n in nums))
    div_note = divs_mod.describe_pick(meet, divisions)
    if div_note:
        parts.append(div_note)
    if places:
        first, last = _place_range(places)
        if last is None:
            parts.append(f"Places {first}+")
        elif last == first:
            parts.append(f"Place {first}")
        else:
            parts.append(f"Places {first}-{last}")
    if grades:
        keys = sorted({_grade_key(g) for g in grades}, key=_grade_sort)
        parts.append(("Grade " if len(keys) == 1 else "Grades ") + ", ".join(keys))
    if team_ids:
        names = []
        for tid in team_ids:
            team = meet.team(tid)
            names.append(_team_label(team, TEAM_NAME_W) if team or tid is None
                          else f"team {tid}")
        parts.append(", ".join(names))
    hidden = _hide_set(hide)
    if hidden:
        parts.append("No " + ", ".join(hidden))
    return FILTER_SEP.join(parts)


class _ResultFilter:
    """What a filtered results printout keeps. Falsy arguments = no filter.

    Places are matched against the real place (event_place); rows are never
    renumbered, so an award list shows the places the athletes actually
    earned. (2026-10-03)"""

    def __init__(self, places=None, grades=None, team_ids=None, hide=None, events=None):
        self.places = _place_range(places) if places else None
        self.grades = {_grade_key(g) for g in grades} if grades else None
        self.teams = list(dict.fromkeys(team_ids)) if team_ids else None   # None = unattached
        self.hide = _hide_set(hide)
        self.events = _event_set(events)

    def keeps(self, meet: Meet, a: Optional[Athlete], f: dict) -> bool:
        if self.events is not None and f.get("event") not in self.events:
            return False
        if self.hide and f.get("status") in self.hide:
            return False
        if self.places:
            place = f.get("event_place") if f.get("status") == "finished" else None
            if place is None:
                return False                     # DNF/DQ/DNS have no place to award
            first, last = self.places
            if place < first or (last is not None and place > last):
                return False
        if self.teams is not None and (a is None or a.team_id not in self.teams):
            return False
        if self.grades is not None and (a is None or _grade_key(meet.grade(a)) not in self.grades):
            return False
        return True


NO_MATCH = "No results match the filter."
GRADE_FILTER_NO_TEAMS = "Team scores not shown: a grade filter leaves out runners who scored."


def _finisher_athlete(meet: Meet, f: dict) -> Optional[Athlete]:
    a = meet.athlete(f["athlete_id"]) if f.get("athlete_id") else None
    return a or meet.athlete_by_bib(f.get("bib") or "")


def _row_order(f: dict):
    """Within a scoring event: finishers by place, then DNF, DQ, DNS."""
    return (STATUS_ORDER.get(f.get("status"), 9),
            (f.get("place") or 0) if f.get("status") == "finished" else 0)


def filter_results(meet: Meet, race: Race, *,
                   places: Optional[Tuple[int, Optional[int]]] = None,
                   grades: Optional[Iterable] = None,
                   team_ids: Optional[Sequence[Optional[str]]] = None,
                   hide: Optional[Iterable[str]] = None,
                   events: Optional[Iterable[int]] = None) -> dict:
    """A NEW dict in the frozen-results shape (scoring.official_results)
    holding only what the filter keeps. The text printout and the PDF
    renderer (pdf_reports.py) both print from it, so they share one set of
    rules. Unfiltered it is a full copy. (2026-10-03)

    - Real places and points are kept, never renumbered. Every finisher
      gets its scoring `event` and `event_place` (the place it prints
      unfiltered) filled in when the frozen copy lacks them.
    - places=(first, last): real event_place in range, inclusive, per
      scoring event (last None = to the end); DNF/DQ/DNS are dropped.
    - grades: meet.grade(a) in the set ('9', 9, 'FR', 'Freshman' all match).
    - team_ids: those schools; None in the list = unattached runners.
    - teams: with team_ids only those schools' rows, real ranks. Every row
      gains "tied" (same score as another ranked team, worked out over all
      teams) so a filtered row still shows its tie-break.
    - "show_team_scores": False with a grade filter (a grade's runners alone
      don't make a team score, and the real ones would read as if they did).
    - hide: any of "DNF", "DNS", "DQ" (a string = one); those rows are
      dropped. Places and team scores don't change: none of them has either.
    - events: scoring event numbers to keep (one division of a combined
      start): the other events' finishers, team scores and sections go.
    - "filter": {places, grades, team_ids, hide, events, text}; text =
      describe_filter; hide in DNF, DQ, DNS order, events sorted, None = off.
    Raises ValueError when the race isn't final."""
    res = scoring.official_results(race)
    if res is None:
        raise ValueError(f"race {race.event}-{race.round}-{race.heat} is not final")
    flt = _ResultFilter(places, grades, team_ids, hide, events)
    entries: Dict[str, Entry] = {e.athlete_id: e for e in race.entries}
    finishers = [copy.deepcopy(f) for f in res.get("finishers", [])]
    by_event: Dict[int, List[dict]] = {}
    for f in finishers:
        if f.get("event") is None:
            a = _finisher_athlete(meet, f)
            e = entries.get(a.id) if a else None
            f["event"] = e.event if e else race.event
        by_event.setdefault(f["event"], []).append(f)
    for rows in by_event.values():
        running = 0
        for f in sorted(rows, key=_row_order):
            if f.get("status") == "finished":
                running += 1                      # counted before filtering: real places
                if not f.get("event_place"):
                    f["event_place"] = running

    teams_out: Dict[str, List[dict]] = {}
    for num, teams in (res.get("teams") or {}).items():
        if flt.events is not None and int(num) not in flt.events:
            continue
        ranked = [t for t in teams if t.get("rank") is not None]
        rows = []
        for t in teams:
            t = copy.deepcopy(t)
            t["tied"] = t.get("rank") is not None and any(
                o.get("team_id") != t.get("team_id") and o.get("score") == t.get("score")
                for o in ranked)
            if flt.teams is None or t.get("team_id") in flt.teams:
                rows.append(t)
        if rows:
            teams_out[num] = rows

    out = copy.deepcopy({k: v for k, v in res.items() if k not in ("finishers", "teams")})
    out["finishers"] = [f for f in finishers if flt.keeps(meet, _finisher_athlete(meet, f), f)]
    out["teams"] = teams_out
    out["show_team_scores"] = flt.grades is None
    out["filter"] = {
        "places": list(flt.places) if flt.places else None,
        "grades": sorted(flt.grades, key=_grade_sort) if flt.grades is not None else None,
        "team_ids": list(flt.teams) if flt.teams is not None else None,
        "hide": flt.hide,
        "events": flt.events,
        "text": describe_filter(meet, places, grades, team_ids, hide, events),
    }
    return out


def _filter_active(res: dict) -> bool:
    f = res.get("filter") or {}
    return any(f.get(k) for k in ("places", "grades", "team_ids", "hide", "events"))


def _not_final(meet: Meet, race: Race) -> List[List[str]]:
    # Official only while the race is final (2026-10-03, scoring contract):
    # reopening keeps the last frozen results but sets status back to
    # "scheduled", so results alone don't make a printout official.
    why = "Results not final (race reopened)." if race.results else "Results not final."
    return [_event_heading(meet, race.event, race, _width(RESULT_COLS)) + [why], [""]]


def _result_sections(meet: Meet, race: Race, res: Optional[dict],
                     div_res: Sequence[Tuple] = ()) -> List[List[str]]:
    """Blocks of lines (one per scoring event, plus its team scores) from
    filter_results' dict. With an active filter a section with no matching
    runner is left out, and [] means nothing in the race matched.

    div_res (2026-10-06): [(Division, its filter_results dict, its
    unfiltered standings)] from division_results(); each event's division
    sections follow the event's own. res None = a division-only printout."""
    width = _width(RESULT_COLS)
    entries: Dict[str, Entry] = {e.athlete_id: e for e in race.entries}
    first = res if res is not None else (div_res[0][1] if div_res else {})
    active = _filter_active(first)
    only = (first.get("filter") or {}).get("events")         # print options (2026-10-03)
    by_event: Dict[int, List[dict]] = {}
    if res is not None:
        by_event = {num: [] for num in _race_events(race) if only is None or num in only}
        for f in res.get("finishers", []):
            by_event.setdefault(f["event"], []).append(f)
    div_maps = []
    for div, dres, full in div_res:
        rows_of: Dict[int, List[dict]] = {}
        for f in dres.get("finishers", []):
            rows_of.setdefault(f["event"], []).append(f)
        entered = set(divs_mod.entered_events(meet, race, div))
        div_maps.append((div, dres, full, rows_of, entered))
    nums = set(by_event)
    for div, dres, full, rows_of, entered in div_maps:
        nums |= {n for n in set(rows_of) | entered if only is None or n in only}

    blocks = []
    for num in sorted(nums):
        if num in by_event:
            rows = sorted(by_event[num], key=_row_order)
            if not (active and not rows):
                blocks += _event_section(meet, race, num, rows, res, (race.results or {}).get("teams"),
                                         _event_heading(meet, num, race, width), entries)
        for div, dres, full, rows_of, entered in div_maps:
            rows = sorted(rows_of.get(num, []), key=_row_order)
            if not rows and (active or num not in entered):
                continue
            heading = [f"Event {num}  {_event_name(meet, num, race)}",
                       f"Division: {div.name}", "=" * width]
            blocks += _event_section(meet, race, num, rows, dres, full.get("teams"), heading,
                                     entries)
    return blocks


def _event_section(meet: Meet, race: Race, num: int, rows: List[dict], res: dict,
                   all_teams: Optional[dict], heading: List[str],
                   entries: Dict[str, Entry]) -> List[List[str]]:
    """One scoring event's (or one division's) section: heading, runner
    rows, legend, team scores. all_teams = the unfiltered team rows, to say
    team scores were left out by a grade filter."""
    width = _width(RESULT_COLS)
    ev = meet.event(num)
    distance = ev.distance_m if ev else 0
    blocks = []
    lines = list(heading) + [_col_header(RESULT_COLS)]
    any_ex = False
    for f in rows:
        a = _finisher_athlete(meet, f)
        entry = entries.get(a.id) if a else None
        exhibition = entry is not None and entry.status == "exhibition"
        any_ex |= exhibition
        if f.get("status") == "finished":
            place = f.get("event_place") or ""
            mark = format_time(f.get("elapsed"))
            pace = format_pace(f.get("elapsed"), distance)
        else:
            place, mark, pace = "--", f.get("status") or "", ""
        pts = f.get("points")
        pts = "EX" if exhibition else ("" if pts is None else pts)
        bib = f.get("bib") or (a.bib if a else "")
        lines.append(_row(RESULT_COLS, [place, _bib(bib), _name(a, bib), _grade(meet, a),
                                        _athlete_team(meet, a, 20), mark, pace, pts]))
    if not rows:
        lines.append("No finishers.")
    if any_ex:
        lines.append("EX = exhibition: runs, does not score or displace")
    blocks.append(lines)
    if not res.get("show_team_scores", True):
        if (all_teams or {}).get(str(num)):
            blocks.append(["", GRADE_FILTER_NO_TEAMS])
    else:
        teams = (res.get("teams") or {}).get(str(num)) or []
        if teams:
            blocks.append([""] + _team_score_lines(meet, teams))
    blocks.append([""])
    return blocks


def division_results(meet: Meet, race: Race, divisions=None, *, places=None, grades=None,
                     team_ids=None, hide=None, events=None) -> Tuple[bool, List[Tuple]]:
    """(print the race's own results, [(Division, filtered standings,
    unfiltered standings)]) for a race whose official results are set
    (the caller swaps in live ones first). divisions: see divisions.pick.
    The filters act on the division's own places (2026-10-06)."""
    overall, picked = divs_mod.pick(meet, race, divisions)
    res = scoring.official_results(race)
    out = []
    for div, dres in divs_mod.race_results(meet, race, res, picked):
        fres = filter_results(meet, divs_mod.as_race(race, dres), places=places, grades=grades,
                              team_ids=team_ids, hide=hide, events=events)
        out.append((div, fres, dres))
    return overall, out


def _team_score_lines(meet: Meet, teams: List[dict]) -> List[str]:
    """The Team Scores block for the rows given (all, or a school filter's).
    A row's "tied" flag (filter_results) wins over comparing the rows given,
    so a filtered row still shows its tie-break."""
    sc = meet.meet.scoring
    n_runners = sc.scorers + sc.displacers
    cols: List[Col] = [("Rank", 4, ">"), ("Team", TEAM_NAME_W, "<"), ("Total", 5, ">")]
    cols += [(str(i) if i <= sc.scorers else f"*{i}", 4, ">") for i in range(1, n_runners + 1)]
    width = _width(cols)
    lines = [_center("Team Scores", width), _col_header(cols), "=" * width]
    ranked = [t for t in teams if t.get("rank") is not None]
    partial = [t for t in teams if t.get("rank") is None]
    for t in ranked:
        label = _team_label(meet.team(t.get("team_id")), TEAM_NAME_W)
        pts = [r["points"] for r in t.get("runners", []) if r.get("points") is not None]
        pts = (pts + [""] * n_runners)[:n_runners]
        lines.append(_row(cols, [t["rank"], label, t.get("score", "")] + pts))
        if t.get("total_time") is not None:
            line = (f"      Total Time: {format_time(t['total_time']):>11}"
                    f"   Average: {format_time(t.get('avg_time'))}")
            if "tied" in t:
                tied = t["tied"]
            else:
                tied = any(o is not t and o.get("score") == t.get("score") for o in ranked)
            if tied:
                tb = t.get("tiebreak_points")
                line += f"   Tie-break *6: {tb if tb is not None else 'none'}"
            lines.append(line)
    for t in partial:
        label = _team_label(meet.team(t.get("team_id")), TEAM_NAME_W)
        lines.append(_row(cols[:3], ["", label, t.get("count", "")]) + "  incomplete, not scored")
    return lines


def results_text(meet: Meet, race: Race, printed_at: Optional[datetime] = None, *,
                 places: Optional[Tuple[int, Optional[int]]] = None,
                 grades: Optional[Iterable] = None,
                 team_ids: Optional[Sequence[Optional[str]]] = None,
                 hide: Optional[Iterable[str]] = None,
                 events: Optional[Iterable[int]] = None,
                 live: Optional[dict] = None, divisions=None) -> str:
    """One race's results: a section per scoring event, then team scores.
    Filters as filter_results (combine freely); the header's third line says
    what was filtered (describe_filter), so it repeats on every page.
    live: current results of a race that isn't final, printed under
    "Results - NOT MARKED COMPLETE" (2026-10-05). divisions: the race's
    division sections, see divisions.pick (2026-10-06)."""
    divs_mod.check_pick(meet, divisions)
    note = describe_filter(meet, places, grades, team_ids, hide, events, divisions)
    current = live is not None and scoring.official_results(race) is None
    head = _header(meet, "Results - NOT MARKED COMPLETE" if current else "Results",
                   _width(RESULT_COLS), printed_at, note=note)
    if current:
        race = dataclasses.replace(race, results=live, status="final")
    if scoring.official_results(race) is None:
        return _join([head] + _not_final(meet, race)).rstrip("\n") + "\n"
    flt = dict(places=places, grades=grades, team_ids=team_ids, hide=hide, events=events)
    overall, div_res = division_results(meet, race, divisions, **flt)
    res = filter_results(meet, race, **flt) if overall else None
    blocks = _result_sections(meet, race, res, div_res)
    if not blocks:
        blocks = [_event_heading(meet, race.event, race, _width(RESULT_COLS)) + [NO_MATCH]]
    return _join([head] + blocks).rstrip("\n") + "\n"


def printable_races(meet: Meet, races: Optional[Sequence[Race]] = None,
                    live: Optional[Dict[tuple, dict]] = None) -> List[Race]:
    """The races a results printout covers, in run order: final ones, and
    (with `live`) the others that have current results."""
    live = live or {}
    return [r for r in (meet.races_in_order() if races is None else races)
            if scoring.official_results(r) or (live.get(r.key) or {}).get("finishers")]


def meet_results_text(meet: Meet, printed_at: Optional[datetime] = None, *,
                      races: Optional[Sequence[Race]] = None,
                      places: Optional[Tuple[int, Optional[int]]] = None,
                      grades: Optional[Iterable] = None,
                      team_ids: Optional[Sequence[Optional[str]]] = None,
                      hide: Optional[Iterable[str]] = None,
                      events: Optional[Iterable[int]] = None,
                      live: Optional[Dict[tuple, dict]] = None, divisions=None) -> str:
    """Every final race (or the final ones of `races`), in run order, under
    one header. Same filters as results_text; a race with nothing matching
    is left out. live: {race key: current results} of races not marked
    final - printed too, each under a NOT MARKED COMPLETE line (2026-10-06).
    divisions: as results_text."""
    divs_mod.check_pick(meet, divisions)
    note = describe_filter(meet, places, grades, team_ids, hide, events, divisions)
    head = _header(meet, "Results", _width(RESULT_COLS), printed_at, note=note)
    blocks = [head]
    finals = printable_races(meet, races, live)
    for race in finals:
        current = scoring.official_results(race) is None
        if current:
            race = dataclasses.replace(race, results=live[race.key], status="final")
        flt = dict(places=places, grades=grades, team_ids=team_ids, hide=hide, events=events)
        overall, div_res = division_results(meet, race, divisions, **flt)
        res = filter_results(meet, race, **flt) if overall else None
        sections = _result_sections(meet, race, res, div_res)
        if current and sections:
            sections = [["*** NOT MARKED COMPLETE ***"]] + sections
        blocks += sections
    if not finals:
        blocks.append(["No final results yet."])
    elif len(blocks) == 1:
        blocks.append([NO_MATCH])
    return _join(blocks).rstrip("\n") + "\n"


# --------------------------------------------------------------- start list

def start_list_text(meet: Meet, race: Race, printed_at: Optional[datetime] = None) -> str:
    """Heat sheet for one gun start: entered + exhibition runners per scoring
    event, sorted by team then name; scratches only counted. A scheduled
    start time prints under the title ("Start: 8:00 AM"), so it repeats on
    every page (2026-10-03)."""
    width = _width(START_COLS)
    when = start_time_text(race)
    blocks = [_header(meet, "Start List", width, printed_at,
                      note=f"Start: {when}" if when else "")]
    blocks += _start_list_blocks(meet, race, width)
    return _join(blocks).rstrip("\n") + "\n"


def start_lists_text(meet: Meet, races: Optional[Sequence[Race]] = None,
                     printed_at: Optional[datetime] = None) -> str:
    """Every race's start list in run order in one document ("Print all
    start lists" is one download, 2026-10-03): one race per page (form
    feed), each opened by a line naming the race, its place in the run
    order and its scheduled start."""
    width = _width(START_COLS)
    races = list(races) if races is not None else meet.races_in_order()
    chunks = []
    for i, race in enumerate(races, 1):
        when = start_time_text(race)
        title = f"Race {i} of {len(races)}: {_race_label(meet, race)}"
        if when:
            title += f"{FILTER_SEP}Start: {when}"
        chunks.append(_join([[title, ""]] + _start_list_blocks(meet, race, width)).rstrip("\n"))
    head = "\n".join(_header(meet, "Start Lists", width, printed_at))
    body = "\n\f".join(chunks) if chunks else "No races."
    return head + "\n" + body + "\n"


def _race_label(meet: Meet, race: Race) -> str:
    ev = meet.event(race.event)
    return race.name or (ev.name if ev else "") or f"Event {race.event}"


def _start_list_blocks(meet: Meet, race: Race, width: int) -> List[List[str]]:
    blocks: List[List[str]] = []
    for num in _race_events(race):
        rows = []
        scratched = 0
        for e in race.entries:
            if e.event != num:
                continue
            if e.status == "scratched":
                scratched += 1
                continue
            a = meet.athlete(e.athlete_id)
            team = _athlete_team(meet, a, 20)
            # Unattached runners after the teams, then team, last, first.
            key = (a is None or a.team_id is None, _sort_key(team),
                   _sort_key(a.last) if a else "", _sort_key(a.first) if a else "")
            rows.append((key, a, e))
        rows.sort(key=lambda r: r[0])
        lines = _event_heading(meet, num, race, width) + [_col_header(START_COLS)]
        for i, (_, a, e) in enumerate(rows, 1):
            bib = a.bib if a else ""
            lines.append(_row(START_COLS, [i, _bib(bib), _name(a),
                                           _grade(meet, a), _athlete_team(meet, a, 20), e.seed,
                                           "EX" if e.status == "exhibition" else ""]))
        n_teams = len({a.team_id for _, a, _ in rows if a and a.team_id})
        summary = f"{_plural(len(rows), 'runner')}, {_plural(n_teams, 'team')}"
        if scratched:
            summary += f" ({scratched} scratched)"
        lines.append(summary)
        if any(e.status == "exhibition" for _, _, e in rows):
            lines.append("EX = exhibition: runs, does not score or displace")
        blocks += [lines, [""]]
    return blocks


# --------------------------------------------------------------- schedule
#
# The run order with scheduled start times (2026-10-03, with the start-times
# ask): what a coach or the announcer wants on one sheet. Data first:
# schedule_rows() is what both the text and the PDF (pdf_reports.
# schedule_pdf) lay out.

SCHEDULE_COLS: List[Col] = [("Start", 8, ">"), ("#", 3, ">"), ("Evt", 7, "<"),
                            ("Race", 30, "<"), ("Divisions", 28, "<"), ("Entries", 7, ">")]


def schedule_rows(meet: Meet) -> List[dict]:
    """One row per race in run order: {n, rk, start_time, start (text),
    race, divisions [names], entries, status}. entries = entered +
    exhibition (scratches don't run)."""
    rows = []
    for i, race in enumerate(meet.races_in_order(), 1):
        nums = sorted({e.event for e in race.entries if e.status != "scratched"}) or [race.event]
        rows.append({"n": i, "rk": "-".join(str(x) for x in race.key),
                     "start_time": race.start_time, "start": start_time_text(race),
                     "race": _race_label(meet, race),
                     "divisions": [_event_name(meet, n, race) for n in nums],
                     "entries": sum(1 for e in race.entries if e.status != "scratched"),
                     "status": race.status})
    return rows


def schedule_text(meet: Meet, printed_at: Optional[datetime] = None) -> str:
    width = _width(SCHEDULE_COLS)
    lines = [_col_header(SCHEDULE_COLS), "=" * width]
    rows = schedule_rows(meet)
    for r in rows:
        divs = r["divisions"]
        # one division that is the race itself says nothing new
        div = "" if divs == [r["race"]] else ", ".join(divs)
        lines.append(_row(SCHEDULE_COLS, [r["start"], r["n"], r["rk"], r["race"], div,
                                          r["entries"]]))
    total = sum(r["entries"] for r in rows)
    lines.append(f"{_plural(len(rows), 'race')}, {_plural(total, 'runner')}" if rows
                 else "No races.")
    if rows and not any(r["start"] for r in rows):
        lines.append("No start times set yet (Meet > Races > Start).")
    return _join([_header(meet, "Schedule", width, printed_at), lines])


# --------------------------------------------------------------- bib sheet

def _races_text(meet: Meet, a: Athlete) -> str:
    parts = []
    for race, e in meet.entries_for(a.id):
        name = _event_name(meet, e.event, race)
        if e.status == "scratched":
            name += " (scr)"
        elif e.status == "exhibition":
            name += " (EX)"
        parts.append(name)
    return "; ".join(parts) or "-"


def team_bibs_text(meet: Meet, team_ids: Optional[Sequence[str]] = None,
                   printed_at: Optional[datetime] = None) -> str:
    """Per team: every athlete with bib, sex, grade and race, by bib, one
    team per page (form feed) so each coach gets their own sheet at check-in.
    team_ids None = every team with athletes, by name, then the unattached."""
    width = _width(BIB_COLS)
    if team_ids is None:
        groups = [t.id for t in sorted(meet.teams, key=lambda t: _sort_key(t.name))]
        groups.append(None)
    else:
        groups = list(team_ids)
    chunks = []
    for tid in groups:
        athletes = [a for a in meet.athletes if a.team_id == tid]
        if not athletes and (team_ids is None or tid is None):
            continue
        athletes.sort(key=lambda a: (not a.bib, int(a.bib) if a.bib.isdigit() else 0,
                                     _sort_key(a.last), _sort_key(a.first)))
        team = meet.team(tid)
        title = team.name if team else "Unattached"
        if team and team.code:
            title += f" ({team.code})"
        lines = [title, "=" * width, _col_header(BIB_COLS)]
        for a in athletes:
            lines.append(_row(BIB_COLS, [a.bib or "----", _name(a), a.gender, _grade(meet, a),
                                         _races_text(meet, a)]))
        no_bib = sum(1 for a in athletes if not a.bib)
        summary = _plural(len(athletes), "athlete")
        if no_bib:
            summary += f", {no_bib} without a bib"
        chunks.append("\n".join(l.rstrip() for l in lines + [summary]))
    head = "\n".join(_header(meet, "Team Bib Sheet", width, printed_at))
    body = "\n\f".join(chunks) if chunks else "No athletes."
    return head + "\n" + body + "\n"


# --------------------------------------------------------------- PDF

PAGE_LETTER = (612.0, 792.0)        # points, portrait
MARGIN = 36.0                       # 0.5 in
FONT_SIZES = (10.0, 9.0, 8.0)
LEADING = 1.2                       # line height / font size
KEEP_LINES = 5                      # min lines of a block at a page bottom

# Not in WinAnsi and not split by NFKD: spelled out before the fallback.
_PDF_FALLBACK = {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "ı": "i", "Ħ": "H", "ħ": "h",
                 "Ŧ": "T", "ŧ": "t", "ĸ": "k", "Ŀ": "L", "ŀ": "l", "Ŋ": "N", "ŋ": "n",
                 "‐": "-", "‑": "-", "−": "-"}


def _winansi(text: str) -> bytes:
    """Text to WinAnsiEncoding (= cp1252) bytes for the standard Courier font.
    Characters outside it lose their accent (NFKD) rather than vanish; the
    rest become '?'. Each character stays one byte, so columns stay put."""
    out = bytearray()
    for ch in unicodedata.normalize("NFC", text):
        try:
            out += ch.encode("cp1252")
            continue
        except UnicodeEncodeError:
            pass
        ch = _PDF_FALLBACK.get(ch, ch)
        base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                       if not unicodedata.combining(c))[:1]
        try:
            out += (base or "?").encode("cp1252")
        except UnicodeEncodeError:
            out += b"?"
    return bytes(out)


def _pdf_string(text: str) -> str:
    """A PDF literal string, ASCII only (octal escapes for the high bytes)."""
    parts = []
    for b in _winansi(text):
        if b in b"\\()":
            parts.append("\\" + chr(b))
        elif 32 <= b < 127:
            parts.append(chr(b))
        else:
            parts.append(f"\\{b:03o}")
    return "(" + "".join(parts) + ")"


def _info_string(text: str) -> str:
    """Document info strings: plain when ASCII, else UTF-16BE hex."""
    if all(32 <= ord(c) < 127 for c in text):
        return _pdf_string(text)
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def split_header(text: str) -> Tuple[List[str], List[List[str]]]:
    """(header lines, body chunks). The header is what sits above the first
    blank line (the documents above put their 2 header lines there); a form
    feed starts a new chunk, which always starts a new page."""
    chunks = text.rstrip("\n").split("\f")
    first = chunks[0].split("\n")
    header: List[str] = []
    if "" in first and 0 < first.index("") <= 5:
        header = first[:first.index("")]
        first = first[first.index("") + 1:]
    body = [first] + [c.lstrip("\n").split("\n") for c in chunks[1:]]
    return header, body


def paginate(text: str, lines_per_page: int) -> List[List[str]]:
    """Pages of lines, each starting with the repeated header (+ a blank).
    A block (lines between blank lines) never starts at a page bottom with
    fewer than KEEP_LINES of it on the page, so an "Event" heading or the
    "Team Scores" title isn't stranded away from its rows."""
    header, chunks = split_header(text)
    head = header + [""] if header else []
    room = max(1, lines_per_page - len(head))
    pages: List[List[str]] = []

    def close(page: List[str]) -> None:
        while page and page[-1] == "":
            page.pop()
        pages.append(page)

    for chunk in chunks:
        page: List[str] = []
        for i, line in enumerate(chunk):
            if line == "" and not page:
                continue                          # no blank line at a page top
            if line != "" and (i == 0 or chunk[i - 1] == ""):
                block = 0
                while i + block < len(chunk) and chunk[i + block] != "":
                    block += 1
                need = min(block, KEEP_LINES, room)
                if page and room - len(page) < need:
                    close(page)
                    page = []
            if len(page) >= room:
                close(page)
                page = []
                if line == "":
                    continue
            page.append(line)
        if page or not pages:
            close(page)
    return [head + p for p in pages]


def _layout(max_cols: int, font_size: Optional[float],
            landscape: Optional[bool]) -> Tuple[float, float, float]:
    """(page width, page height, font size): the largest size that fits the
    longest line on portrait Letter, else landscape, else 8 pt landscape."""
    def cols(w: float, size: float) -> int:
        return int((w - 2 * MARGIN) / (0.6 * size))
    orientations = [False, True] if landscape is None else [landscape]
    sizes = [font_size] if font_size else list(FONT_SIZES)
    for land in orientations:
        w, h = (PAGE_LETTER[1], PAGE_LETTER[0]) if land else PAGE_LETTER
        for size in sizes:
            if cols(w, size) >= max_cols:
                return w, h, size
    land = orientations[-1]
    w, h = (PAGE_LETTER[1], PAGE_LETTER[0]) if land else PAGE_LETTER
    return w, h, sizes[-1]


def _lines_per_page(page_h: float, size: float) -> int:
    """Lines (header included) to_pdf puts on a page of that height."""
    return int((page_h - 2 * MARGIN) / round(size * LEADING, 2))


def text_lines_per_page(max_cols: int) -> int:
    """Lines per page to_pdf will use for text whose longest line is
    max_cols: lets a document page itself exactly (rosters)."""
    _, h, size = _layout(max_cols, None, None)
    return _lines_per_page(h, size)


def to_pdf(text: str, title: str = "", font_size: Optional[float] = None,
           landscape: Optional[bool] = None) -> bytes:
    """A Courier PDF of the text: Letter, header repeated on every page,
    "Page n of N" in the bottom margin, form feeds start new pages."""
    max_cols = max((len(l) for l in text.replace("\f", "\n").split("\n")), default=0)
    w, h, size = _layout(max_cols, font_size, landscape)
    leading = round(size * LEADING, 2)
    per_page = _lines_per_page(h, size)
    pages = paginate(text, per_page)

    objs: List[bytes] = []                   # object n is objs[n - 1]

    def add(body: str) -> int:
        objs.append(body.encode("ascii"))
        return len(objs)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("")                                   # Pages, filled in below
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
               "/Encoding /WinAnsiEncoding >>")
    info = add(f"<< /Title {_info_string(title)} /Producer ({APP_NAME}) >>")
    kids = []
    for n, lines in enumerate(pages, 1):
        ops = ["BT", f"/F1 {size:g} Tf", f"{leading:g} TL",
               f"{MARGIN:g} {h - MARGIN - size:g} Td"]
        for i, line in enumerate(lines):
            if i:
                ops.append("T*")
            if line:
                ops.append(f"{_pdf_string(line)} Tj")
        ops.append("ET")
        label = f"Page {n} of {len(pages)}"
        x = w - MARGIN - len(label) * 0.6 * 8
        ops += ["BT", "/F1 8 Tf", f"{x:.2f} {MARGIN / 2:g} Td", f"{_pdf_string(label)} Tj", "ET"]
        stream = "\n".join(ops) + "\n"
        content = add(f"<< /Length {len(stream)} >>\nstream\n{stream}endstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:g} {h:g}] "
                        f"/Resources << /Font << /F1 {font} 0 R >> >> "
                        f"/Contents {content} 0 R >>"))
    objs[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
               f"/Count {len(kids)} >>").encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii")
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode("ascii")
    return bytes(out)


# --------------------------------------------------------------- rosters
#
# Per school: every entered athlete with bib, grade and race, girls on the
# left and boys on the right, so a coach checks entries and bibs at a
# glance (2026-10-03). Geometry, decided from to_pdf above: two 43-char
# columns + a 4-space gutter = 90 chars, exactly what portrait Letter holds
# at 10 pt Courier (540 pt / 6 pt per char), i.e. the largest font to_pdf
# offers and 60 lines a page. 60 - 3 repeated header lines - 6 fixed lines
# per page (school, counts, rule, gender headings, column headings, legend)
# - 1 line for each gender's total under its last column (2026-10-03, user's
# ask) = 50 rows per column (ROSTER_ROWS_PER_COLUMN, computed, not typed in).
# The header is cut to 90 chars (fit=True) so a long meet name can't push
# to_pdf to a smaller font or landscape and break the paging.

ROSTER_COLS: List[Col] = [("Bib", 5, ">"), ("Name", 23, "<"), ("Yr", 2, ">"),
                          ("Race", 7, "<"), ("", 2, "<")]
ROSTER_GUTTER = 4
ROSTER_COL_W = _width(ROSTER_COLS)                       # 43
ROSTER_WIDTH = 2 * ROSTER_COL_W + ROSTER_GUTTER          # 90
ROSTER_HEADER_LINES = 3                                  # stamp, title, blank (repeated)
ROSTER_FIXED_LINES = 7                                   # + the gender totals line
ROSTER_ROWS_PER_COLUMN = (text_lines_per_page(ROSTER_WIDTH) - ROSTER_HEADER_LINES
                          - ROSTER_FIXED_LINES)
ROSTER_LEGEND = "EX = exhibition (runs, does not score)    ---- = no bib assigned yet"
ROSTER_GENDERS = (("F", "Girls"), ("M", "Boys"))

# Race short labels: level from the division (else the event name), first
# match wins, so "Junior Varsity" is JV before "Varsity" can claim it.
_LEVELS = [(r"\bjunior varsity\b|\bj\.?v\.?\b", "JV"), (r"\bvarsity\b", "V"),
           (r"\bmiddle school\b|\bms\b|\bjunior high\b|\bjh\b", "MS"),
           (r"\bfreshm[ae]n\b|\bfrosh\b", "FR"), (r"\bopen\b", "O"),
           (r"\breserve\b", "R"), (r"\bnovice\b", "N"), (r"\belementary\b|\belem\b", "EL")]
_KM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:k|km)\b", re.I)
_MILE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", re.I)
_METER_RE = re.compile(r"\b(\d{3,5})\s*(?:m|meters?)?\b", re.I)


def _distance_label(meters: float, name: str = "") -> str:
    """5000 -> '5K', 3218/3200 -> '2M', 2500 -> '2.5K', else '1234m'.
    Whole kilometres win; then within 1% of a half-mile multiple reads as
    miles (XC "2 mile" courses are listed as 3200 or 3218 m); without a
    distance it is read from the event name ('5k', '2 Mile', '3200')."""
    if not meters:
        m = _KM_RE.search(name)
        if m:
            meters = float(m.group(1)) * 1000
        elif _MILE_RE.search(name):
            meters = float(_MILE_RE.search(name).group(1)) * METERS_PER_MILE
        elif _METER_RE.search(name):
            meters = float(_METER_RE.search(name).group(1))
    if not meters or meters <= 0:
        return ""
    if abs(meters - round(meters / 1000) * 1000) < 0.5 and meters >= 1000:
        return f"{round(meters / 1000)}K"
    miles = meters / METERS_PER_MILE
    half = round(miles * 2) / 2
    if half and abs(miles - half) / half < 0.01:
        return f"{half:g}M"
    if abs(meters - round(meters / 100) * 100) < 0.5:
        return f"{round(meters / 100) / 10:g}K"
    return f"{round(meters)}m"


def race_short_label(meet: Meet, event_num: int) -> str:
    """'V 5K', 'JV 5K', 'MS 2M': level + distance, for narrow columns (the
    rosters report). The level comes from the event's division, else its
    name; '' parts are dropped; 'Ev 3' when nothing is known."""
    ev = meet.event(event_num)
    if ev is None:
        return f"Ev {event_num}"
    level = ""
    for text in (ev.division, ev.name):
        for pattern, abbr in _LEVELS:
            if text and re.search(pattern, text, re.I):
                level = abbr
                break
        if level:
            break
    label = " ".join(p for p in (level, _distance_label(ev.distance_m, ev.name)) if p)
    return label or f"Ev {event_num}"


def _roster_row(meet: Meet, a: Athlete, order: Dict[Tuple[int, int, int], int]) -> dict:
    live = sorted(((r, e) for r, e in meet.entries_for(a.id) if e.status != "scratched"),
                  key=lambda re_: order.get(re_[0].key, 0))
    race, entry = live[0]
    label = race_short_label(meet, entry.event) + ("+" if len(live) > 1 else "")
    return {"athlete_id": a.id, "bib": a.bib, "last": a.last.strip(), "first": a.first.strip(),
            "grade": _grade(meet, a), "race": label,
            "exhibition": entry.status == "exhibition"}


def _roster_gender_order(girls: List[dict], boys: List[dict]) -> Tuple[str, str]:
    """Which gender takes the left column: the one holding the school's
    lowest bib, so the numbers read left to right in the order the bib
    dialog handed them out (boys first under its gender sort). Girls first
    when the bibs can't tell (one gender, or no numeric bibs yet).
    (2026-10-05, user's rule)"""
    def low(rows: List[dict]) -> Optional[int]:
        nums = [int(r["bib"]) for r in rows if (r["bib"] or "").isdigit()]
        return min(nums) if nums else None
    g, b = low(girls), low(boys)
    return ("M", "F") if g is not None and b is not None and b < g else ("F", "M")


def _roster_columns(label: str, rows: List[dict], cap: int) -> List[dict]:
    return [{"heading": label if i == 0 else f"{label} (cont'd)", "rows": rows[i:i + cap]}
            for i in range(0, len(rows), cap)]


def roster_pages(meet: Meet, team_ids: Optional[Sequence[Optional[str]]] = None,
                 rows_per_column: int = ROSTER_ROWS_PER_COLUMN) -> List[dict]:
    """The rosters report as data: the paging rule, shared by rosters_text
    and the PDF renderer (each passes the rows its page holds). (2026-10-03)

    Who: athletes entered in a race (meet.entered: roster-only athletes
    from a full-roster import and scratched-only ones are left out);
    exhibition entries are kept and flagged; no bib = bib "". A gender
    other than F counts with the boys (validate() flags it anyway).
    Schools in name order then the unattached, or team_ids order (None =
    unattached); a school with nobody entered is skipped unless asked for.

    Paging: one page per school, one gender in each column: the gender
    with the school's lower bibs on the left, so the numbers run left to
    right (_roster_gender_order; girls left when there are no bibs to go
    by). When either gender has more than rows_per_column, the school
    gets a page per gender instead (in that same order), each flowing down
    the left column then the right, onto more pages if it overflows both.
    A one-gender school flows the same way from the left column.

    Each page: {team_id, team, code, counts {"F", "M"}, genders (("F", "M")
    or ("M", "F"): the column order, for the counts line), no_bib, page,
    pages (for this school), columns: always 2 of {heading: "Girls" |
    "Boys" | "Girls (cont'd)" | "" (empty column), rows: [{athlete_id, bib,
    last, first, grade, race, exhibition}], total: the gender's athlete
    count on its last column, else None}}; rows by last, first name."""
    cap = max(1, int(rows_per_column))
    order = {r.key: i for i, r in enumerate(meet.races_in_order())}
    if team_ids is None:
        groups: List[Optional[str]] = [t.id for t in sorted(meet.teams,
                                                            key=lambda t: _sort_key(t.name))]
        groups.append(None)
    else:
        groups = list(team_ids)
    out: List[dict] = []
    for tid in groups:
        athletes = [a for a in meet.athletes if a.team_id == tid and meet.entered(a.id)]
        if not athletes and (team_ids is None or tid is None):
            continue
        athletes.sort(key=lambda a: (_sort_key(a.last), _sort_key(a.first),
                                     int(a.bib) if a.bib.isdigit() else 0))
        rows = {g: [_roster_row(meet, a, order) for a in athletes
                    if (a.gender == "F") == (g == "F")] for g, _ in ROSTER_GENDERS}
        girls, boys = rows["F"], rows["M"]
        genders = _roster_gender_order(girls, boys)
        label_of = dict(ROSTER_GENDERS)
        seq = [(label_of[g], rows[g]) for g in genders]          # left column first
        if girls and boys and len(girls) <= cap and len(boys) <= cap:
            pages = [[{"heading": label, "rows": rs} for label, rs in seq]]
        else:
            # Girls' columns and boys' columns never share a page.
            pages = []
            for label, rs in seq:
                mine = _roster_columns(label, rs, cap)
                pages += [mine[i:i + 2] for i in range(0, len(mine), 2)]
            if not pages:
                pages = [[{"heading": label, "rows": []} for label, _ in seq]]
        # Each gender's athlete count under its last column (2026-10-03).
        for label, rs in (("Girls", girls), ("Boys", boys)):
            mine = [c for pg in pages for c in pg if c["heading"].startswith(label)]
            for c in mine:
                c["total"] = None
            if mine:
                mine[-1]["total"] = len(rs)
        team = meet.team(tid)
        for i, columns in enumerate(pages, 1):
            columns = columns + [{"heading": "", "rows": [], "total": None}] * (2 - len(columns))
            out.append({"team_id": tid,
                        "team": team.name if team else ("Unattached" if tid is None
                                                        else f"Unknown team {tid}"),
                        "code": team.code if team else "",
                        "counts": {"F": len(girls), "M": len(boys)}, "genders": genders,
                        "no_bib": sum(1 for a in athletes if not a.bib),
                        "page": i, "pages": len(pages), "columns": columns})
    return out


def _roster_text_page(p: dict) -> List[str]:
    title = p["team"] + (f" ({p['code']})" if p["code"] else "")
    if p["pages"] > 1:
        tag = f"page {p['page']} of {p['pages']}"
        title = _fit(title, ROSTER_WIDTH - len(tag) - 2) + "  " + tag
    else:
        title = _fit(title, ROSTER_WIDTH)
    label_of = dict(ROSTER_GENDERS)
    counts = FILTER_SEP.join(f"{label_of[g]} {p['counts'][g]}" for g in p["genders"])
    if p["no_bib"]:
        counts += f"{FILTER_SEP}{p['no_bib']} without a bib"

    def pair(left: str, right: str) -> str:
        return (left.ljust(ROSTER_COL_W) + " " * ROSTER_GUTTER + right).rstrip()

    left, right = p["columns"]
    lines = [title.rstrip(), counts, "=" * ROSTER_WIDTH,
             pair(left["heading"].upper(), right["heading"].upper()),
             pair(_col_header(ROSTER_COLS) if left["heading"] else "",
                  _col_header(ROSTER_COLS) if right["heading"] else "")]

    def cells(r: Optional[dict]) -> str:
        if r is None:
            return ""
        name = f"{r['last']}, {r['first']}" if r["first"] else r["last"]
        return _row(ROSTER_COLS, [r["bib"] or "----", name, r["grade"], r["race"],
                                  "EX" if r["exhibition"] else ""])

    n = max(len(left["rows"]), len(right["rows"]))
    for i in range(n):
        lines.append(pair(cells(left["rows"][i] if i < len(left["rows"]) else None),
                          cells(right["rows"][i] if i < len(right["rows"]) else None)))
    if n == 0:
        lines.append("No entered athletes.")

    def total(c: dict) -> str:
        if c.get("total") is None:
            return ""
        return f"Total {c['heading'].split(' ')[0].lower()}: {c['total']}".rjust(ROSTER_COL_W)
    if left.get("total") is not None or right.get("total") is not None:
        lines.append(pair(total(left), total(right)))
    lines.append(ROSTER_LEGEND)
    return lines


def rosters_text(meet: Meet, team_ids: Optional[Sequence[Optional[str]]] = None,
                 printed_at: Optional[datetime] = None) -> str:
    """Each school's entered athletes and their bibs, a school per page
    (form feeds), girls and boys side by side in bib order; see roster_pages
    for who, the column order and the paging rule, and the section notes above for the page geometry."""
    head = "\n".join(_header(meet, "Rosters", ROSTER_WIDTH, printed_at, fit=True))
    pages = roster_pages(meet, team_ids)
    body = "\n\f".join("\n".join(_roster_text_page(p)) for p in pages) or "No entered athletes."
    return head + "\n" + body + "\n"


# --------------------------------------------------------------- HTML

def to_html(text: str, title: str = "") -> str:
    """The text in <pre> blocks for the browser's print dialog. Each form-feed
    chunk is its own printed page and repeats the header lines on top."""
    header, chunks = split_header(text)
    pages = []
    for chunk in chunks:
        lines = (header + [""] if header else []) + chunk
        pages.append('<pre class="page">' + html.escape("\n".join(lines).rstrip("\n"))
                     + "</pre>")
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<title>{html.escape(title)}</title>\n<style>\n"
            "@page { size: letter; margin: 0.5in; }\n"
            "body { margin: 0.5in; background: #fff; color: #000; }\n"
            "pre { font: 10pt/1.2 \"Courier New\", Courier, monospace; margin: 0; }\n"
            ".page + .page { margin-top: 2em; break-before: page; page-break-before: always; }\n"
            "@media print { body { margin: 0; } .page + .page { margin-top: 0; } }\n"
            "</style>\n</head>\n<body>\n" + "\n".join(pages) + "\n</body>\n</html>\n")
