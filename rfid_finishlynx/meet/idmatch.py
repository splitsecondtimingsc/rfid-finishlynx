"""MileSplit ids for athletes that came without them (walk-ups, Hy-Tek/CSV).

mpResults refuses an upload unless at least half the athletes carry a
MileSplit id, and a wrong id puts a result on someone else's profile. So the
rule (docs/MEET-MANAGEMENT-PLAN.md, 2026-10-03) is lopsided on purpose:

- **Auto-attach** only when first name, last name, grade (graduation year)
  and school all match *exactly*, gender doesn't contradict, no other
  candidate on that school shares the name, and the id isn't already on
  another athlete of the same team in this meet.
- **Everything else with a plausible candidate goes to a review queue**
  (`ms_match="pending"`) that lists each candidate field by field with the
  mismatches named, for the operator to approve or decline.
- No plausible candidate: no queue item; the athlete stays `ms_match=""`
  and simply counts as "without id" in `id_coverage`. A queue full of
  "nothing found" rows would bury the ones that need a decision.

Nothing is guessed silently, and an operator decision (approved/declined)
is never touched by a re-run. An auto attachment is also kept on a re-run
(an id that changes under the operator is worse than one that's stale);
`reopen` puts any athlete back up for matching.

Definitions (2026-10-03):
- *Exact* = equal after `str.strip()` only. Case, accents, inner spaces and
  punctuation all count: "avery" vs "Avery" is a near miss for review, not a
  match, because a walk-up typed in a hurry is exactly where that happens.
- *Near* (`norm`) = equal after NFKD accent stripping, case folding and
  dropping everything but a-z/0-9 ("O'Neil" ~ "ONeil", "Zoë" ~ "Zoe",
  "Mary-Kate" ~ "Mary Kate").
- *Plausible* candidate: near-equal first+last on any team; or, on the
  athlete's own team, a nickname-ish first name (one is a prefix of the other,
  "Will"/"William") with the same last name, a last name sharing a part
  ("Garcia" / "Garcia-Lopez") with the same or prefix first name, or first
  and last swapped.
- *School*: the candidate's MileSplit team is mapped to a meet team by team
  milesplit_id first, then by exact (trimmed) team name.
- *Grade*: the candidate's `athleteYear` (graduation year) against
  `Athlete.grad_year`; an athlete with only `grade_text` ("9", "12", "Sr") is
  converted with `Meet.season_end_year`. Unknown grade never auto-attaches.

The candidate pool is built from raw MileSplit JSON (entries and
team-rosters pulls); this module does no network I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple, Union

from .model import Athlete, Meet

# mpResults rejects an upload below this share of athletes with ids
# (~/milesplit-probe/MAPPING.md).
MIN_ID_SHARE = 0.5
# A queue row shows at most this many candidates, best first.
MAX_CANDIDATES = 5

FIELDS = ("first", "last", "grade", "team", "gender")
# Field states in a comparison (what the console highlights).
MATCH, NEAR, MISMATCH, UNKNOWN = "match", "near", "mismatch", "unknown"

# Words a Hy-Tek/CSV "year" column uses instead of a grade number.
_GRADE_WORDS = {"sr": 12, "senior": 12, "jr": 11, "junior": 11, "so": 10, "soph": 10,
                "sophomore": 10, "fr": 9, "fresh": 9, "freshman": 9}


# --------------------------------------------------------------- helpers

def norm(s) -> str:
    """Accent-, case- and punctuation-free key: "O'Néil-Smith " -> "oneilsmith"."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _parts(s) -> set:
    """Name parts split on spaces/hyphens/apostrophes, each normalised."""
    return {norm(p) for p in re.split(r"[\s\-'’]+", str(s or "")) if norm(p)}


def _exact(a, b) -> bool:
    return str(a or "").strip() == str(b or "").strip() and str(a or "").strip() != ""


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def grad_year_of(meet: Meet, a: Athlete) -> Optional[int]:
    """The athlete's graduation year: stored, or from grade_text and the
    season ("9" in fall 2026 -> 2030). None when it can't be known."""
    if a.grad_year:
        return int(a.grad_year)
    season = meet.season_end_year
    text = (a.grade_text or "").strip().lower().rstrip(".")
    if not text or season is None:
        return None
    m = re.fullmatch(r"0*(\d{1,2})(st|nd|rd|th)?", text)
    grade = int(m.group(1)) if m else _GRADE_WORDS.get(text)
    if grade is None or not 1 <= grade <= 12:
        return None
    return season + (12 - grade)


def _grade_text(season: Optional[int], grad_year: Optional[int]) -> str:
    return str(12 - (grad_year - season)) if grad_year and season else ""


# --------------------------------------------------------------- pool

@dataclass
class Candidate:
    """One MileSplit athlete on one MileSplit team. The same athlete on two
    teams is two candidates (MAPPING.md: that happens)."""
    milesplit_id: int
    universal_id: Optional[str] = None
    first: str = ""
    last: str = ""
    gender: str = ""                    # M | F | "" (unknown)
    grad_year: Optional[int] = None
    team_name: str = ""
    team_milesplit_id: Optional[int] = None
    team_id: Optional[str] = None       # meet team it maps to, None = not in meet
    source: str = "entries"             # entries | roster

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        """From a queue item's candidate dict (what the console posts back)."""
        ms = _int(d.get("milesplit_id"))
        if ms is None:
            raise ValueError("candidate has no milesplit_id")
        return cls(milesplit_id=ms, universal_id=d.get("universal_id") or None,
                   first=d.get("first", ""), last=d.get("last", ""),
                   gender=d.get("gender", ""), grad_year=_int(d.get("grad_year")),
                   team_name=d.get("team", d.get("team_name", "")),
                   team_milesplit_id=_int(d.get("team_milesplit_id")),
                   team_id=d.get("team_id"), source=d.get("source", "entries"))


def _team_list(raw) -> list:
    """Teams from an entries or team-rosters pull, tolerating a {"data": ...}
    wrapper, a {"teams": [...]} object, or a dict keyed by team id."""
    while isinstance(raw, dict):
        for k in ("data", "teams", "rosters"):
            if k in raw:
                raw = raw[k]
                break
        else:
            vals = list(raw.values())
            if vals and all(isinstance(v, dict) for v in vals):
                return vals
            return [raw] if ("roster" in raw or "athletes" in raw) else []
    return [t for t in raw or [] if isinstance(t, dict)]


class CandidatePool:
    """Every MileSplit athlete we could attach, mapped onto the meet's teams.

    Built from the meet's entries pull (athletes entered) plus the attending
    teams' full rosters (walk-ups are usually on the roster but not entered).
    An athlete seen in both is one candidate; entries win, the roster fills
    fields the entry lacks."""

    def __init__(self, meet: Meet, candidates: Iterable[Candidate] = ()):
        self.candidates: List[Candidate] = []
        self._by_key: Dict[tuple, Candidate] = {}
        # Plausible candidates are near-equal names anywhere or anyone on the
        # athlete's team; index both so a 1,000-athlete Hy-Tek import doesn't
        # compare every athlete with every candidate (2026-10-03).
        self._by_name: Dict[Tuple[str, str], List[Candidate]] = {}
        self._by_team: Dict[str, List[Candidate]] = {}
        self._by_ms_team = {t.milesplit_id: t.id for t in meet.teams if t.milesplit_id}
        self._team_by_name: Dict[str, str] = {}
        for t in meet.teams:                    # first team wins a duplicate name
            self._team_by_name.setdefault(t.name.strip(), t.id)
        for c in candidates:
            self._add(c)

    @classmethod
    def from_milesplit(cls, meet: Meet, entries=None, rosters=None) -> "CandidatePool":
        pool = cls(meet)
        from .importers import roster_teams
        for raw, source in ((entries, "entries"), (roster_teams(rosters), "roster")):
            for t in _team_list(raw):
                pool.add_team(t, source)
        return pool

    def add_team(self, t: dict, source: str) -> None:
        tms = _int(t.get("teamMileSplitId") or t.get("teamId"))
        tname = str(t.get("teamName") or t.get("name") or "").strip()
        team_id = self._by_ms_team.get(tms) if tms else None
        if team_id is None:
            team_id = self._team_by_name.get(tname)
        for a in t.get("roster") or t.get("athletes") or []:
            if not isinstance(a, dict):
                continue
            ms = _int(a.get("athleteMileSplitId") or a.get("athleteId"))
            if ms is None:
                continue                        # nothing to attach
            self._add(Candidate(
                milesplit_id=ms,
                universal_id=a.get("athleteMileSplitUniversalId") or None,
                first=str(a.get("athleteFirstName") or "").strip(),
                last=str(a.get("athleteLastName") or "").strip(),
                gender=str(a.get("athleteGender") or "").strip().upper()[:1],
                grad_year=_int(a.get("athleteYear")),
                team_name=tname, team_milesplit_id=tms, team_id=team_id, source=source))

    def _add(self, c: Candidate) -> None:
        key = (c.milesplit_id, c.team_milesplit_id or norm(c.team_name))
        have = self._by_key.get(key)
        if have is None:
            self._by_key[key] = c
            self.candidates.append(c)
            self._by_name.setdefault((norm(c.first), norm(c.last)), []).append(c)
            if c.team_id:
                self._by_team.setdefault(c.team_id, []).append(c)
            return
        for f in ("universal_id", "first", "last", "gender", "grad_year"):
            if not getattr(have, f) and getattr(c, f):
                setattr(have, f, getattr(c, f))

    def __len__(self) -> int:
        return len(self.candidates)

    def near(self, a: Athlete) -> List[Candidate]:
        """Candidates worth comparing with a: same normalised name, or on a's
        team. Pool order, each once."""
        pool = self._by_name.get((norm(a.first), norm(a.last)), [])
        if a.team_id:
            pool = pool + self._by_team.get(a.team_id, [])
        seen, out = set(), []
        for c in pool:
            if id(c) not in seen:
                seen.add(id(c))
                out.append(c)
        return out


# --------------------------------------------------------------- compare

def _relation(a: Athlete, c: Candidate, same_team: bool) -> Optional[str]:
    """How the names relate, or None when the candidate isn't plausible."""
    af, al, cf, cl = norm(a.first), norm(a.last), norm(c.first), norm(c.last)
    if _exact(a.first, c.first) and _exact(a.last, c.last):
        return "exact"
    if af and al and (af, al) == (cf, cl):
        return "normalized"
    if not same_team or not (af and al and cf and cl):
        return None
    prefix = af.startswith(cf) or cf.startswith(af)
    nick_ok = prefix and min(len(af), len(cf)) >= 2
    if al == cl and nick_ok:
        return "nickname"
    if (af == cf or nick_ok) and _parts(a.last) & _parts(c.last):
        return "partial_last"
    if (af, al) == (cl, cf):
        return "swapped"
    return None


def _name_state(ours, theirs) -> str:
    if _exact(ours, theirs):
        return MATCH
    return NEAR if norm(ours) and norm(ours) == norm(theirs) else MISMATCH


def compare(meet: Meet, a: Athlete, c: Candidate) -> Dict[str, str]:
    """Field -> match | near | mismatch | unknown, for every field in FIELDS."""
    ours_year = grad_year_of(meet, a)
    if ours_year is None or c.grad_year is None:
        grade = UNKNOWN
    else:
        grade = MATCH if ours_year == c.grad_year else MISMATCH
    if a.team_id is None:
        team = UNKNOWN
    elif c.team_id is not None:
        team = MATCH if c.team_id == a.team_id else MISMATCH
    else:
        t = meet.team(a.team_id)
        names = {norm(t.name), norm(t.short)} - {""} if t else set()
        team = NEAR if norm(c.team_name) in names else MISMATCH
    if not a.gender or not c.gender:
        gender = UNKNOWN
    else:
        gender = MATCH if a.gender.upper() == c.gender.upper() else MISMATCH
    return {"first": _name_state(a.first, c.first), "last": _name_state(a.last, c.last),
            "grade": grade, "team": team, "gender": gender}


def _conflict(meet: Meet, a: Athlete, ms_id: int) -> Optional[Athlete]:
    """Another athlete on a's team already holding this MileSplit id."""
    if a.team_id is None:
        return None
    return next((b for b in meet.athletes if b.id != a.id and b.milesplit_id == ms_id
                 and b.team_id == a.team_id), None)


# --------------------------------------------------------------- queue

@dataclass
class Ranked:
    candidate: Candidate
    relation: str
    fields: Dict[str, str]
    conflict: Optional[str] = None      # athlete id already holding the id

    @property
    def score(self) -> int:
        return sum(v == MATCH for v in self.fields.values())

    @property
    def mismatches(self) -> List[str]:
        """Fields that aren't an exact match (gender unknown isn't flagged:
        rosters may not carry it and it's not part of the rule)."""
        return [f for f in FIELDS if self.fields[f] != MATCH
                and not (f == "gender" and self.fields[f] == UNKNOWN)]

    def sort_key(self):
        near = sum(v == NEAR for v in self.fields.values())
        return (-self.score, -near, self.conflict is not None,
                self.candidate.source != "entries", self.candidate.milesplit_id,
                self.candidate.team_milesplit_id or 0)


@dataclass
class QueueItem:
    """One athlete awaiting the operator, with candidates best first."""
    athlete_id: str
    athlete: dict
    reasons: List[str]
    candidates: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Outcome:
    action: str                         # auto | review | none | skip
    ranked: List[Ranked] = field(default_factory=list)


def _candidates_for(meet: Meet, pool: CandidatePool, a: Athlete) -> List[Ranked]:
    out = []
    for c in pool.near(a):
        same_team = a.team_id is not None and c.team_id == a.team_id
        rel = _relation(a, c, same_team)
        if rel is None:
            continue
        other = _conflict(meet, a, c.milesplit_id)
        out.append(Ranked(c, rel, compare(meet, a, c), other.id if other else None))
    out.sort(key=Ranked.sort_key)
    return out


def match_athlete(meet: Meet, pool: CandidatePool, a: Athlete) -> Outcome:
    """What the rule says for one athlete; changes nothing."""
    if a.milesplit_id is not None or a.ms_match in ("approved", "declined"):
        return Outcome("skip")
    ranked = _candidates_for(meet, pool, a)
    if not ranked:
        return Outcome("none")
    best = ranked[0]
    # "No other candidate shares that name+school": any other MileSplit id on
    # the same team whose name normalises the same (2026-10-03).
    rivals = [r for r in ranked[1:] if r.candidate.milesplit_id != best.candidate.milesplit_id
              and r.candidate.team_id == best.candidate.team_id
              and r.relation in ("exact", "normalized")]
    exact = (best.relation == "exact" and best.conflict is None and not rivals
             and all(best.fields[f] == MATCH for f in ("first", "last", "grade", "team"))
             and best.fields["gender"] != MISMATCH)
    return Outcome("auto" if exact else "review", ranked)


def _reasons(ranked: List[Ranked]) -> List[str]:
    best = ranked[0]
    out = []
    if len({(r.candidate.milesplit_id, r.candidate.team_id) for r in ranked}) > 1:
        out.append("several_candidates")
    if best.relation != "exact":
        out.append("name_" + best.relation)
    for f in ("grade", "team", "gender"):
        if best.fields[f] == MISMATCH:
            out.append(f + "_differs")
        elif best.fields[f] == NEAR:            # team named alike but not mapped
            out.append(f + "_near")
        elif best.fields[f] == UNKNOWN and f != "gender":
            out.append(f + "_unknown")
    if best.conflict:
        out.append("id_in_use")
    return out


def _athlete_view(meet: Meet, a: Athlete) -> dict:
    t = meet.team(a.team_id)
    return {"first": a.first, "last": a.last, "gender": a.gender,
            "grade": meet.grade(a) or _grade_text(meet.season_end_year, grad_year_of(meet, a)),
            "grad_year": grad_year_of(meet, a), "team": t.name if t else "",
            "team_id": a.team_id, "bib": a.bib, "source": a.source}


def _candidate_view(meet: Meet, r: Ranked) -> dict:
    c = r.candidate
    return {"milesplit_id": c.milesplit_id, "universal_id": c.universal_id,
            "first": c.first, "last": c.last, "gender": c.gender,
            "grade": _grade_text(meet.season_end_year, c.grad_year), "grad_year": c.grad_year,
            "team": c.team_name, "team_id": c.team_id, "team_milesplit_id": c.team_milesplit_id,
            "source": c.source, "relation": r.relation, "fields": dict(r.fields),
            "mismatches": r.mismatches, "score": r.score, "conflict": r.conflict}


def queue_item(meet: Meet, a: Athlete, ranked: List[Ranked]) -> QueueItem:
    shown = ranked[:MAX_CANDIDATES]
    return QueueItem(a.id, _athlete_view(meet, a), _reasons(ranked),
                     [_candidate_view(meet, r) for r in shown])


# --------------------------------------------------------------- apply

@dataclass
class MatchReport:
    auto: List[str] = field(default_factory=list)          # athlete ids attached now
    queue: List[QueueItem] = field(default_factory=list)   # now pending
    unmatched: List[str] = field(default_factory=list)     # no plausible candidate

    def to_dict(self) -> dict:
        return {"auto": list(self.auto), "queue": [q.to_dict() for q in self.queue],
                "unmatched": list(self.unmatched)}


def _attach(a: Athlete, c: Candidate, state: str) -> None:
    a.milesplit_id = int(c.milesplit_id)
    a.universal_id = c.universal_id or None
    a.ms_match = state


def match_meet(meet: Meet, pool: CandidatePool) -> MatchReport:
    """Run the rule over every athlete without an id; sets milesplit_id /
    universal_id / ms_match in place (the caller saves the meet).

    Athletes are visited in list order and an auto attachment is visible to
    the next athlete's conflict check, so two walk-ups typed twice for the
    same runner can't both take the id."""
    rep = MatchReport()
    for a in meet.athletes:
        out = match_athlete(meet, pool, a)
        if out.action == "skip":
            continue
        if out.action == "auto":
            _attach(a, out.ranked[0].candidate, "auto")
            rep.auto.append(a.id)
        elif out.action == "review":
            a.ms_match = "pending"
            rep.queue.append(queue_item(meet, a, out.ranked))
        else:
            a.ms_match = ""                 # pool no longer offers anything
            rep.unmatched.append(a.id)
    return rep


def review_queue(meet: Meet, pool: CandidatePool) -> List[QueueItem]:
    """The pending athletes' queue rows, recomputed against the pool (pure)."""
    items = []
    for a in meet.athletes:
        if a.ms_match != "pending":
            continue
        out = match_athlete(meet, pool, a)
        if out.ranked:
            items.append(queue_item(meet, a, out.ranked))
    return items


class IdConflict(ValueError):
    """The MileSplit id is already on another athlete of the same team."""


def approve(meet: Meet, athlete_id: str,
            candidate: Union[Candidate, dict]) -> Athlete:
    """Operator approves a candidate: attach its ids, ms_match="approved".
    Refuses (IdConflict) when a teammate already holds that id: the operator
    fixes the duplicate first rather than us double-booking a profile."""
    a = meet.athlete(athlete_id)
    if a is None:
        raise KeyError(athlete_id)
    c = candidate if isinstance(candidate, Candidate) else Candidate.from_dict(candidate)
    other = _conflict(meet, a, c.milesplit_id)
    if other is not None:
        raise IdConflict(f"MileSplit id {c.milesplit_id} is already on {other.id} "
                         f"{other.first} {other.last} (same team)")
    _attach(a, c, "approved")
    return a


def decline(meet: Meet, athlete_id: str) -> Athlete:
    """Operator declines: no ids, ms_match="declined"; the athlete is pushed
    to MileSplit as a write-in and never re-matched until reopened. Ids that
    came with a MileSplit import aren't the matcher's to remove."""
    a = meet.athlete(athlete_id)
    if a is None:
        raise KeyError(athlete_id)
    if a.ms_match == "" and a.milesplit_id is not None:
        raise ValueError(f"{a.id} came from MileSplit with its ids; nothing to decline")
    a.milesplit_id = None
    a.universal_id = None
    a.ms_match = "declined"
    return a


def reopen(meet: Meet, athlete_id: str) -> Athlete:
    """Undo for auto/approved/declined: clear the ids and let the next
    match_meet decide again."""
    a = meet.athlete(athlete_id)
    if a is None:
        raise KeyError(athlete_id)
    if a.ms_match in ("auto", "approved", "declined", "pending"):
        a.milesplit_id = None
        a.universal_id = None
        a.ms_match = ""
    return a


# --------------------------------------------------------------- coverage

@dataclass
class Coverage:
    athletes: int
    with_id: int

    @property
    def share(self) -> float:
        return self.with_id / self.athletes if self.athletes else 1.0

    @property
    def ok(self) -> bool:
        """False = mpResults would reject: the console should warn."""
        return self.share >= MIN_ID_SHARE

    def to_dict(self) -> dict:
        return {"athletes": self.athletes, "with_id": self.with_id,
                "share": round(self.share, 4), "ok": self.ok}


def _coverage(meet: Meet, athlete_ids: Iterable[str]) -> Coverage:
    ids = set(athlete_ids)
    with_id = sum(1 for a in meet.athletes if a.id in ids and a.milesplit_id is not None)
    return Coverage(len(ids), with_id)


def id_coverage(meet: Meet) -> Coverage:
    """Share of athletes entered in any race (scratches left out: they're not
    uploaded) who carry a MileSplit id."""
    return _coverage(meet, (e.athlete_id for r in meet.races for e in r.entries
                            if e.status != "scratched"))


def coverage_by_race(meet: Meet) -> Dict[Tuple[int, int, int], Coverage]:
    """Per race, since mpResults is uploaded (and judged) one race at a time."""
    return {r.key: _coverage(meet, (e.athlete_id for e in r.entries if e.status != "scratched"))
            for r in meet.races_in_order() if r.entries}
