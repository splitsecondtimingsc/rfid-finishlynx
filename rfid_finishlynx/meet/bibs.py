"""Bib numbers for the meet model: ranges, assign, clear, inline edit,
conflicts and per-team bib sheets.

A bib is what the RFID tag encodes, so the rules here are about never
handing one number to two athletes (2026-10-03):
- A bib is a string of digits, 1..99999, with no leading zeros. The reader
  (`rfid_reader._extract_bib`) strips leading zeros and gives up past five
  digits, so "0101" would be read back as "101" and 100000 never at all.
- Assignment never touches a bib someone already holds. "Retain existing"
  (the default) numbers only the athletes without a bib; renumbering a
  selection releases only that selection's bibs.
- Clearing is its own call. There is no clear-all-then-reassign workflow
  (MeetPro's way, which reshuffles every printed bib in the box).
- An inline edit that would duplicate a bib is refused; one outside every
  range is accepted with a warning (a spare from the box is legitimate).

Ported from ~/roster-bib-assigner/roster_assigner.py (parse_ranges,
build_bib_sequence, sort_records), reworked for the meet model and a
single text box of ranges ("100-399, 500-") instead of Start/End rows.

Nothing here prints or saves: callers save the meet (MeetStore.save) and
the console renders the reports, which are all JSON-serialisable.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

from .model import Athlete, Meet, Race

# The reader decodes at most five digits (rfid_reader._extract_bib), so a
# six-digit bib could be printed but never read (2026-10-03). The old
# roster tool allowed 100000.
MAX_BIB = 99999

SORT_KEYS = ("team", "gender", "last", "first", "grade", "race")
DEFAULT_SORT = ("team", "gender", "last", "first")

# Per-team blocks: a fixed size (int) or the team's count rounded up.
BLOCK_ROUNDING = {"round10": 10, "round25": 25}

# Free-text years from Hy-Tek/CSV imports (Athlete.grade_text).
_GRADE_WORDS = {"freshman": 9, "fr": 9, "sophomore": 10, "so": 10,
                "junior": 11, "jr": 11, "senior": 12, "sr": 12}

Ranges = List[List[Optional[int]]]
RangeInput = Union[None, str, Sequence[Sequence[Optional[int]]]]


# --------------------------------------------------------------- ranges

_RANGE_RE = re.compile(r"(\d+)(?:\s*(-|–|—|to|\+)\s*(\d*))?", re.IGNORECASE)


def parse_ranges(text: str) -> Ranges:
    """'100-399, 500-' -> [[100, 399], [500, None]].

    Grammar, one item per comma, semicolon or line:
      100-399   closed range ("to", en/em dashes and spaces also accepted)
      500-      open-ended: keep numbering upward (also "500+"); last item only
      250       that single bib
    Ranges are used in the order typed. Closed ranges may not overlap, and an
    open-ended range may not start inside an earlier one (a typo, never a
    plan). An open-ended range may start *below* an earlier closed range
    ("1000-1099, 100-"): it skips the bibs that range already covers, as the
    old roster tool did. Raises ValueError with a message for the operator.
    """
    items = [t.strip() for t in re.split(r"[,;\n]", text or "")]
    items = [t for t in items if t]
    if not items:
        raise ValueError("Enter at least one bib range, e.g. 100-399, 500-")
    pairs: List[Tuple[int, Optional[int]]] = []
    for i, tok in enumerate(items, start=1):
        m = _RANGE_RE.fullmatch(tok)
        if not m:
            raise ValueError(f"Range {i} ({tok!r}): not a range. Use 100-399, 500- or 250.")
        start, sep, end_txt = int(m.group(1)), m.group(2), m.group(3)
        if sep is None:
            pairs.append((start, start))
        elif sep == "+":
            if end_txt:
                raise ValueError(f"Range {i} ({tok!r}): not a range. Use 100-399, 500- or 250.")
            pairs.append((start, None))
        else:
            pairs.append((start, int(end_txt) if end_txt else None))
    return _check(pairs, [repr(t) for t in items])


def parse_range_rows(rows: Iterable[Sequence[Optional[str]]]) -> Ranges:
    """The bib dialog's Start / End rows -> ranges, exactly as the Roster Bib
    Number Assigner takes them (2026-10-03, user: the dialog must keep that
    tool's flexibility): rows used in the order listed, any number of them,
    fully blank rows ignored, a blank End = keep numbering upward from Start
    (last row only), a single bib is Start = End. Raises ValueError naming
    the row, in the tool's words."""
    cleaned = [(str(s or "").strip(), str(e or "").strip()) for s, e in rows]
    cleaned = [(s, e) for s, e in cleaned if s or e]
    if not cleaned:
        raise ValueError("Enter at least one bib range.")
    pairs: List[Tuple[int, Optional[int]]] = []
    for i, (s, e) in enumerate(cleaned, start=1):
        if not s:
            raise ValueError(f"Range {i}: the Start box is empty.")
        if not s.isdigit():
            raise ValueError(f"Range {i}: Start '{s}' is not a whole number.")
        if e and not e.isdigit():
            raise ValueError(f"Range {i}: End '{e}' is not a whole number.")
        pairs.append((int(s), int(e) if e else None))
    return _check(pairs, [f"{s}-{e}" for s, e in cleaned])


def _check(pairs: Sequence[Tuple[int, Optional[int]]], labels: Sequence[str]) -> Ranges:
    """Validate ranges in order; the shared half of parse_ranges and
    ranges passed as lists (from meet.bib_ranges or a JSON request)."""
    out: Ranges = []
    for i, ((start, end), label) in enumerate(zip(pairs, labels), start=1):
        tag = f"Range {i} ({label})"
        if not 1 <= start <= MAX_BIB:
            raise ValueError(f"{tag}: bibs run from 1 to {MAX_BIB}.")
        if end is None:
            if i != len(pairs):
                raise ValueError(f"{tag}: only the last range may be open-ended "
                                 "(open-ended means 'keep numbering upward').")
        else:
            if not 1 <= end <= MAX_BIB:
                raise ValueError(f"{tag}: bibs run from 1 to {MAX_BIB}.")
            if end < start:
                raise ValueError(f"{tag}: end {end} is below start {start}.")
        for j, (s, e) in enumerate(out, start=1):
            if e is None:
                continue                     # only the last can be open
            if end is None:
                if s <= start <= e:
                    raise ValueError(f"{tag}: starts inside range {j} ({s}-{e}).")
            elif start <= e and s <= end:
                raise ValueError(f"{tag}: overlaps range {j} ({s}-{e}).")
        out.append([start, end])
    return out


def _as_ranges(ranges: RangeInput) -> Ranges:
    if isinstance(ranges, str):
        return parse_ranges(ranges)
    pairs = []
    for r in ranges or []:
        if len(r) != 2 or not isinstance(r[0], int) or not (r[1] is None or isinstance(r[1], int)):
            raise ValueError(f"Bib range {list(r)!r} is not [start, end] or [start, null].")
        pairs.append((r[0], r[1]))
    if not pairs:
        raise ValueError("No bib ranges: set the meet's ranges first, e.g. 100-399, 500-")
    return _check(pairs, [format_ranges([list(p)]) for p in pairs])


def format_ranges(ranges: Iterable[Sequence[Optional[int]]]) -> str:
    """[[100, 399], [500, None], [250, 250]] -> '100-399, 500-, 250'."""
    parts = []
    for s, e in ranges:
        parts.append(f"{s}-" if e is None else (str(s) if s == e else f"{s}-{e}"))
    return ", ".join(parts)


def set_ranges(meet: Meet, text: str) -> Ranges:
    """Parse and store the meet's ranges; blank text removes them.

    The meet's ranges are what conflicts() checks against. assign() can be
    given other ranges for one run (e.g. walk-ups from 900-) without
    changing these."""
    meet.bib_ranges = parse_ranges(text) if (text or "").strip() else []
    return meet.bib_ranges


def in_ranges(bib: Union[int, str], ranges: Iterable[Sequence[Optional[int]]]) -> bool:
    n = _bib_int(bib)
    if n is None:
        return False
    return any(s <= n and (e is None or n <= e) for s, e in ranges)


def _segments(ranges: Ranges) -> Iterator[Tuple[int, int]]:
    """Ranges as contiguous (lo, hi) runs in use order, with the bibs an
    earlier range covers cut out of a later open-ended one."""
    closed: List[Tuple[int, int]] = []
    for s, e in ranges:
        if e is not None:
            closed.append((s, e))
            yield (s, e)
            continue
        lo = s
        for cs, ce in sorted(closed):
            if ce < lo:
                continue
            if cs > lo:
                yield (lo, cs - 1)
            lo = max(lo, ce + 1)
        if lo <= MAX_BIB:
            yield (lo, MAX_BIB)


def bib_sequence(ranges: RangeInput, count: int, exclude: Iterable[int] = ()) -> List[int]:
    """Up to `count` free bibs, consuming the ranges in order.

    Port of build_bib_sequence. It returns fewer than `count` when the
    ranges run out instead of raising, so assign() can say how many didn't
    fit; `exclude` are bibs held by someone or set aside (skip)."""
    taken = set(exclude)
    out: List[int] = []
    if count <= 0:
        return out
    for lo, hi in _segments(_as_ranges(ranges)):
        for n in range(lo, hi + 1):
            if n not in taken:
                out.append(n)
                if len(out) >= count:
                    return out
    return out


def next_free(meet: Meet, skip: Iterable[Union[int, str]] = ()) -> Optional[int]:
    """The bib assign() would hand the next athlete: the first number in the
    meet's ranges nobody holds, or None when no ranges are set or they are
    used up. `skip` = numbers not to offer (the bib being handed over in the
    "who's wearing N?" flow, as edits._settle_holder skips it). The console
    shows it so a walk-up gets a sensible bib without opening the bib
    dialog (2026-10-03)."""
    if not meet.bib_ranges:
        return None
    held = {n for a in meet.athletes for n in [_bib_int(a.bib)] if n is not None}
    held |= {n for n in (_bib_int(b) for b in skip) if n is not None}
    out = bib_sequence(meet.bib_ranges, 1, exclude=held)
    return out[0] if out else None


def summarize_bibs(bibs: Iterable[Union[int, str]]) -> str:
    """[100, 101, 102, 500] -> '100-102, 500' (for previews and reports)."""
    runs: List[List[int]] = []
    for n in sorted(x for x in (_bib_int(b) for b in bibs) if x is not None):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        elif not runs or n != runs[-1][1]:
            runs.append([n, n])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


def _bib_int(bib: Union[int, str, None]) -> Optional[int]:
    if isinstance(bib, int):
        return bib
    bib = (bib or "").strip()
    return int(bib) if bib.isdigit() else None


def _parse_skip(skip: Union[str, Iterable[Union[int, str]], None]) -> Set[int]:
    """skip='113, 150-155' or [113, '150']: bibs never to hand out
    (a dead tag, a number set aside). Open-ended ranges are refused here."""
    if not skip:
        return set()
    if isinstance(skip, str):
        out: Set[int] = set()
        for s, e in parse_ranges(skip):
            if e is None:
                raise ValueError("Skip list: an open-ended range would skip every bib above it.")
            out.update(range(s, e + 1))
        return out
    out = set()
    for b in skip:
        n = _bib_int(b)
        if n is None:
            raise ValueError(f"Skip list: {b!r} is not a bib number.")
        out.add(n)
    return out


# --------------------------------------------------------------- reports

@dataclass
class AssignReport:
    """What assign() did (or, with dry_run, would do).

    assigned: [(athlete_id, bib)] in numbering order. skipped: athletes in
    the selection left alone, {athlete_id, reason, bib}. unassigned: how
    many needed a bib and didn't get one (ranges ran out); their ids in
    unassigned_ids. blocks: per-team blocks when that option is on.
    applied: False when nothing was changed (dry run, or a renumber that
    would not fit)."""
    assigned: List[Tuple[str, str]] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    unassigned: int = 0
    unassigned_ids: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocks: List[dict] = field(default_factory=list)
    bibs: str = ""                    # summarize_bibs of what was assigned
    applied: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["assigned"] = [list(p) for p in self.assigned]
        return d


@dataclass
class ClearReport:
    cleared: List[Tuple[str, str]] = field(default_factory=list)   # (athlete_id, old bib)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"cleared": [list(p) for p in self.cleared], "warnings": list(self.warnings)}


@dataclass
class EditResult:
    """set_bib's answer. ok False = refused, nothing changed, `error` says
    why; ok True may still carry warnings (out of range, final race)."""
    ok: bool
    athlete_id: str
    bib: str = ""                     # the bib now held (normalised)
    previous: str = ""
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Conflicts:
    """Everything wrong with the meet's bibs. Each list is JSON-ready:
    duplicates [{bib, athlete_ids}], invalid [{athlete_id, bib, problem}],
    out_of_range [{athlete_id, bib}], missing [{athlete_id, races}] where
    races are [event, round, heat] keys the athlete is entered in."""
    duplicates: List[dict] = field(default_factory=list)
    invalid: List[dict] = field(default_factory=list)
    out_of_range: List[dict] = field(default_factory=list)
    missing: List[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.duplicates or self.invalid or self.out_of_range or self.missing)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


# --------------------------------------------------------------- helpers

def _id_key(athlete_id: str) -> Tuple[int, str]:
    m = re.fullmatch(r"[a-z]*(\d+)", athlete_id)
    return (int(m.group(1)), athlete_id) if m else (1 << 30, athlete_id)


def _grade_num(text: str) -> int:
    """'9' -> 9, 'Senior' -> 12; unknown sorts last."""
    t = (text or "").strip().lower().rstrip(".")
    if t.isdigit():
        return int(t)
    return _GRADE_WORDS.get(t, 99)


def _live_entries(meet: Meet, athlete_id: str) -> List[Race]:
    """Races the athlete runs in (entered or exhibition, not scratched)."""
    return [r for r, e in meet.entries_for(athlete_id) if e.status != "scratched"]


def _bib_eligible(meet: Meet, a: Athlete) -> bool:
    """Entered-only rule. Entered in a race now (not scratched); in a
    full-roster meet also an athlete entered on MileSplit whose race was
    cancelled (no entries left, not roster-only). 2026-10-05, user: import
    with full rosters, delete the races, add new ones, assign bibs - only
    the originally entered athletes get numbers."""
    if _live_entries(meet, a.id):
        return True
    return meet.meet.full_rosters and not a.roster_only and not meet.entries_for(a.id)


def _final_race_warning(meet: Meet, a: Athlete, old: str) -> Optional[str]:
    finals = [r for r, _ in meet.entries_for(a.id) if r.status == "final"]
    if not finals or not old:
        return None
    names = ", ".join(r.name or f"event {r.event}" for r in finals)
    return (f"{a.first} {a.last}: bib {old} changed after {names} went final; "
            "the frozen results still show the old bib")


def _team_order_key(meet: Meet, team_id: Optional[str]) -> tuple:
    t = meet.team(team_id)
    if t is None:
        return (1, "", "")                # unattached (no team) after every team
    return (0, t.name.casefold(), t.id)


def _sort_key_fn(meet: Meet, sort: Sequence[str]):
    """Port of sort_records, generalised to an operator-chosen key order.

    gender sorts boys before girls (the old tool's order); grade ascending;
    race by run order (first race the athlete isn't scratched from), not
    entered last. Ties always end in last, first, id so a re-run with the
    same data numbers the same way."""
    keys = list(sort)
    bad = [k for k in keys if k not in SORT_KEYS]
    if bad or len(set(keys)) != len(keys):
        raise ValueError(f"Sort keys must be distinct, from {', '.join(SORT_KEYS)}; got {keys}")
    race_pos = {r.key: i for i, r in enumerate(meet.races_in_order())}

    def race_of(a: Athlete) -> int:
        pos = [race_pos[r.key] for r in _live_entries(meet, a.id)]
        return min(pos) if pos else 1 << 30

    parts = {
        "team": lambda a: _team_order_key(meet, a.team_id),
        "gender": lambda a: {"M": 0, "F": 1}.get(a.gender, 2),
        "last": lambda a: a.last.casefold(),
        "first": lambda a: a.first.casefold(),
        "grade": lambda a: _grade_num(meet.grade(a)),
        "race": race_of,
    }
    chosen = [parts[k] for k in keys]

    def key(a: Athlete):
        return tuple(f(a) for f in chosen) + (a.last.casefold(), a.first.casefold(), _id_key(a.id))
    return key


def _block_size(per_team_blocks, count: int) -> int:
    if isinstance(per_team_blocks, bool):
        raise ValueError("per_team_blocks: give a block size (e.g. 25) or 'round10' / 'round25'")
    if isinstance(per_team_blocks, int):
        if per_team_blocks < 1:
            raise ValueError("per_team_blocks: block size must be at least 1")
        return per_team_blocks
    step = BLOCK_ROUNDING.get(per_team_blocks)
    if step is None:
        raise ValueError(f"per_team_blocks: {per_team_blocks!r}; use a size or 'round10' / 'round25'")
    return max(1, math.ceil(count / step)) * step


def _find_block(segments: List[Tuple[int, int]], cursor: Tuple[int, int], size: int,
                taken: Set[int]) -> Optional[Tuple[int, int, Tuple[int, int]]]:
    """First run of `size` consecutive free bibs at or after cursor that
    lies inside one segment: (start, end, new cursor), or None."""
    si, pos = cursor
    while si < len(segments):
        lo, hi = segments[si]
        pos = max(pos, lo)
        while pos + size - 1 <= hi:
            clash = [n for n in range(pos, pos + size) if n in taken]
            if not clash:
                return pos, pos + size - 1, (si, pos + size)
            pos = clash[-1] + 1
        si, pos = si + 1, 0
    return None


# --------------------------------------------------------------- assign

def assign(meet: Meet, *, ranges: RangeInput = None,
           sort: Sequence[str] = DEFAULT_SORT,
           retain_existing: bool = True,
           athlete_ids: Optional[Iterable[str]] = None,
           per_team_blocks: Union[None, int, str] = None,
           skip: Union[str, Iterable[Union[int, str]], None] = (),
           entered_only: Optional[bool] = None,
           dry_run: bool = False) -> AssignReport:
    """Give bibs to the athletes that need one; Athletic.net's bib dialog.

    ranges: None = the meet's ranges; or text / [[s, e], ...] for this run
      only (not stored; see set_ranges). Bibs outside the meet's ranges are
      warned about, since conflicts() will list them.
    sort: the operator's drag order, any of SORT_KEYS.
    retain_existing: True = only athletes without a bib are numbered and
      every held bib is avoided. False = renumber the selection: its old
      bibs are released and reused; bibs held outside the selection are
      still avoided. A renumber that won't fit changes nothing.
    athlete_ids: limit to these athletes (walk-ups, one team).
    per_team_blocks: None = one continuous sequence. An int N = each team
      gets N consecutive bibs; 'round10' / 'round25' = the team's count
      (athletes numbered in this run) rounded up to 10 / 25. Teams take
      blocks in team-name order (unattached last), each block is a
      contiguous run inside one range with no held or skipped bib in it,
      the athletes take the block's first bibs in sort order and the rest
      stay spare for walk-ups. A team bigger than a fixed block numbers the
      first N and the rest are unassigned (warned).
    skip: bibs never to hand out, '113, 150-155' or a list.
    entered_only: Athletic.net's "Registered only": only athletes with an
      entry that isn't scratched (exhibition counts: they run). None (the
      default) = on when the meet carries full rosters, else off (day-of
      adds may need a number before they are entered). A full-roster meet
      is ALWAYS entered-only, even if False is passed (2026-10-03, user's
      rule): roster-only athletes are there to be found, not numbered.
    dry_run: compute the report without changing the meet (dialog preview).

    Ranges running out never raises: the report says how many didn't get a
    bib and nobody gets a number from outside the ranges. Bad input (no
    ranges, bad sort key, bad block option) raises ValueError.
    """
    rng = _as_ranges(meet.bib_ranges if ranges is None else ranges)
    key = _sort_key_fn(meet, sort)
    skip_set = _parse_skip(skip)
    rep = AssignReport(dry_run=dry_run)
    if meet.meet.full_rosters:
        if entered_only is False:
            rep.warnings.append("full rosters imported: only athletes entered in a race "
                                "get bibs")
        entered_only = True

    wanted: Optional[Set[str]] = None
    if athlete_ids is not None:
        wanted = set(athlete_ids)
        known = {a.id for a in meet.athletes}
        for aid in sorted(wanted - known, key=_id_key):
            rep.warnings.append(f"unknown athlete id {aid}")

    selection: List[Athlete] = []
    for a in meet.athletes:
        if wanted is not None and a.id not in wanted:
            continue
        if entered_only and not _bib_eligible(meet, a):
            rep.skipped.append({"athlete_id": a.id, "reason": "not entered", "bib": a.bib})
            continue
        if retain_existing and a.bib:
            rep.skipped.append({"athlete_id": a.id, "reason": "has bib", "bib": a.bib})
            continue
        selection.append(a)
    renumbered = {a.id for a in selection}

    # Held = every bib on someone this run doesn't renumber. Compared as
    # numbers: "0101" holds 101 because that's what the reader reports.
    held = {n for a in meet.athletes if a.id not in renumbered
            for n in [_bib_int(a.bib)] if n is not None}
    taken = held | skip_set
    selection.sort(key=key)

    plan: List[Tuple[Athlete, int]] = []
    left_out: List[Athlete] = []
    if per_team_blocks is None or per_team_blocks is False:
        bibs = bib_sequence(rng, len(selection), exclude=taken)
        plan = list(zip(selection, bibs))
        left_out = selection[len(bibs):]
    else:
        teams: Dict[Optional[str], List[Athlete]] = {}
        for a in selection:
            teams.setdefault(a.team_id, []).append(a)
        segments = list(_segments(rng))
        cursor = (0, 0)
        for team_id in sorted(teams, key=lambda t: _team_order_key(meet, t)):
            members = teams[team_id]
            t = meet.team(team_id)
            tname = t.name if t else "Unattached"
            size = _block_size(per_team_blocks, len(members))
            found = _find_block(segments, cursor, size, taken)
            if found is None:
                left_out.extend(members)
                rep.warnings.append(f"{tname}: no room left for a block of {size} bibs")
                continue
            start, end, cursor = found
            fit = members[:size]
            if len(members) > size:
                left_out.extend(members[size:])
                rep.warnings.append(f"{tname}: {len(members)} athletes but blocks are {size}; "
                                    f"{len(members) - size} not assigned")
            plan.extend(zip(fit, range(start, start + len(fit))))
            rep.blocks.append({"team_id": team_id, "team": tname, "start": start, "end": end,
                               "used": len(fit)})

    rep.assigned = [(a.id, str(n)) for a, n in plan]
    rep.bibs = summarize_bibs(n for _, n in plan)
    rep.unassigned = len(left_out)
    rep.unassigned_ids = [a.id for a in left_out]
    if left_out:
        rep.warnings.append(
            f"{len(left_out)} athletes could not get a bib: ranges {format_ranges(rng)} ran out. "
            "Add a range (or leave the last one open, e.g. 500-) and assign again"
            + ("; Retain existing keeps the bibs given now." if retain_existing else "."))
    if meet.bib_ranges and ranges is not None:
        outside = [n for _, n in plan if not in_ranges(n, meet.bib_ranges)]
        if outside:
            rep.warnings.append(f"{len(outside)} bibs ({summarize_bibs(outside)}) are outside "
                                f"the meet's ranges {format_ranges(meet.bib_ranges)}")

    if not retain_existing and left_out:
        # Renumbering releases the selection's old bibs first; applying half
        # of it would strip numbers from athletes who already wear them.
        rep.warnings.append("Renumber needs more bibs than the ranges hold: nothing was changed.")
        return rep
    if dry_run:
        return rep
    for a, n in plan:
        if a.bib and a.bib != str(n):
            w = _final_race_warning(meet, a, a.bib)
            if w:
                rep.warnings.append(w)
        a.bib = str(n)
    rep.applied = True
    return rep


# --------------------------------------------------------------- clear / edit

def clear(meet: Meet, athlete_ids: Optional[Iterable[str]] = None) -> ClearReport:
    """Remove bibs: the given athletes, or everyone when athlete_ids is None.

    Separate from assign on purpose (2026-10-03): renumbering the whole
    meet by clearing then assigning reshuffles bibs already handed out.
    Clearing one team and re-assigning it is the supported way."""
    rep = ClearReport()
    if athlete_ids is None:
        targets = list(meet.athletes)
    else:
        ids = list(athlete_ids)
        targets = []
        for aid in ids:
            a = meet.athlete(aid)
            if a is None:
                rep.warnings.append(f"unknown athlete id {aid}")
            else:
                targets.append(a)
    for a in targets:
        if not a.bib:
            continue
        w = _final_race_warning(meet, a, a.bib)
        if w:
            rep.warnings.append(w)
        rep.cleared.append((a.id, a.bib))
        a.bib = ""
    return rep


def normalize_bib(bib: Union[int, str, None]) -> Tuple[str, str]:
    """(canonical bib, error). '0101' -> ('101', ''); '' -> ('', '');
    'A12' -> ('', 'not a number'). Canonical = what the reader reports."""
    text = str(bib if bib is not None else "").strip()
    if not text:
        return "", ""
    if not text.isdigit():
        return "", f"bib {text!r} is not a number"
    n = int(text)
    if not 1 <= n <= MAX_BIB:
        return "", f"bib {text} is outside 1-{MAX_BIB} (the reader decodes at most 5 digits)"
    return str(n), ""


def set_bib(meet: Meet, athlete_id: str, bib: Union[int, str]) -> EditResult:
    """Inline bib edit. '' clears the athlete's bib.

    Refused (ok False, nothing changed): unknown athlete, not a number,
    outside 1..99999, or a bib another athlete holds. A duplicate must be
    refused, not warned: the tag only carries the number, so a read could
    not be told apart, athlete_lookup would silently give the finish to
    whoever comes first, and meet.validate() would fail until it's fixed.
    Warned (ok True, applied): outside every range of the meet (a spare
    from the box is fine), leading zeros dropped, race already final."""
    a = meet.athlete(athlete_id)
    if a is None:
        return EditResult(False, athlete_id, error=f"unknown athlete id {athlete_id}")
    res = EditResult(True, athlete_id, previous=a.bib)
    new, err = normalize_bib(bib)
    if err:
        res.ok, res.bib, res.error = False, a.bib, err
        return res
    if new and new != str(bib).strip():
        res.warnings.append(f"bib {str(bib).strip()} stored as {new} (the tag reads it that way)")
    if new:
        n = int(new)
        holders = [o for o in meet.athletes if o.id != a.id and _bib_int(o.bib) == n]
        if holders:
            o = holders[0]
            res.ok, res.bib = False, a.bib
            res.error = f"bib {new} is already on {o.first} {o.last} ({o.id})"
            return res
        if meet.meet.full_rosters and not meet.entered(a.id):
            res.warnings.append(f"{a.first} {a.last} is not entered in a race yet")
        if meet.bib_ranges and not in_ranges(n, meet.bib_ranges):
            res.warnings.append(f"bib {new} is outside the meet's ranges {format_ranges(meet.bib_ranges)}")
    if new != a.bib:
        w = _final_race_warning(meet, a, a.bib)
        if w:
            res.warnings.append(w)
    a.bib = new
    res.bib = new
    return res


def swap_bibs(meet: Meet, athlete_a: str, athlete_b: str) -> EditResult:
    """Trade two athletes' bibs in one step (2026-10-03, user: it "happens
    all the time"): the grid offers it when a typed bib is already taken.
    Either side may have no bib (then it is a move). Refused when either
    athlete is unknown or they are the same; warned when a race is already
    final (its frozen results keep the old bibs). The result is athlete_a's
    EditResult with the other side named in its warnings."""
    a, b = meet.athlete(athlete_a), meet.athlete(athlete_b)
    if a is None or b is None:
        missing = athlete_a if a is None else athlete_b
        return EditResult(False, athlete_a, error=f"unknown athlete id {missing}")
    if a.id == b.id:
        return EditResult(False, athlete_a, bib=a.bib, error="swap needs two athletes")
    res = EditResult(True, a.id, previous=a.bib)
    for who, old_bib in ((a, a.bib), (b, b.bib)):
        w = _final_race_warning(meet, who, old_bib)
        if w:
            res.warnings.append(w)
    a.bib, b.bib = b.bib, a.bib
    res.bib = a.bib
    res.warnings.append(f"{b.first} {b.last} now has bib {b.bib or '(none)'}")
    return res


# --------------------------------------------------------------- checks

def conflicts(meet: Meet) -> Conflicts:
    """Duplicate bibs (as the reader sees them: '0101' and '101' clash),
    bibs that aren't valid tag numbers, bibs outside every range (only when
    the meet has ranges), and athletes running in a race without a bib."""
    c = Conflicts()
    by_num: Dict[int, List[str]] = {}
    for a in meet.athletes:
        if not a.bib:
            continue
        canon, err = normalize_bib(a.bib)
        if err:
            c.invalid.append({"athlete_id": a.id, "bib": a.bib, "problem": err})
            continue
        if canon != a.bib:
            c.invalid.append({"athlete_id": a.id, "bib": a.bib,
                              "problem": f"leading zeros: the tag reads it as {canon}"})
        n = int(canon)
        by_num.setdefault(n, []).append(a.id)
        if meet.bib_ranges and not in_ranges(n, meet.bib_ranges):
            c.out_of_range.append({"athlete_id": a.id, "bib": a.bib})
    for n in sorted(by_num):
        if len(by_num[n]) > 1:
            c.duplicates.append({"bib": str(n), "athlete_ids": by_num[n]})
    for a in meet.athletes:
        if a.bib:
            continue
        races = _live_entries(meet, a.id)
        if races:
            c.missing.append({"athlete_id": a.id, "races": [list(r.key) for r in races]})
    return c


def team_bib_report(meet: Meet) -> List[dict]:
    """Rows for a printable per-team bib sheet.

    [{team_id, team, short, count, without_bib, rows: [(bib, last, first,
    gender, grade)]}], teams by name with no-team athletes last as
    "Unattached"; rows by bib number, athletes without a bib at the end
    (bib "") so the coach sees who still needs one. count = len(rows)."""
    groups: Dict[Optional[str], List[Athlete]] = {}
    for a in meet.athletes:
        groups.setdefault(a.team_id if meet.team(a.team_id) else None, []).append(a)
    out = []
    for team_id in sorted(groups, key=lambda t: _team_order_key(meet, t)):
        t = meet.team(team_id)
        members = sorted(groups[team_id], key=lambda a: (
            _bib_int(a.bib) is None, _bib_int(a.bib) or 0, a.last.casefold(), a.first.casefold()))
        rows = [(a.bib, a.last, a.first, a.gender, meet.grade(a)) for a in members]
        out.append({"team_id": team_id, "team": t.name if t else "Unattached",
                    "short": (t.short or t.name) if t else "Unattached",
                    "count": len(rows), "without_bib": sum(1 for r in rows if not r[0]),
                    "rows": rows})
    return out
