"""Meet edits that touch several records at once (the console's race and
athlete menus call these; single-field edits stay plain assignments).

Score a mixed race by gender (2026-10-03, user's ask: "an option to
score/show results for a mixed race separately"; MeetPro's "Score Genders
Separately"). One gun, one FinishLynx race, but girls and boys placed and
team-scored apart. The model already supports a race whose entries score in
different events (Entry.event vs Race.event, the combined-JV start), and
scoring, printing, mpResults and Live all work per scoring event; so
splitting is just giving each gender its own scoring event and moving each
entry by the athlete's gender. The race keeps its own (mixed) Lynx event.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .model import Athlete, Entry, Event, Meet, Race, bib_number

GENDER_WORD = {"F": "Girls", "M": "Boys"}
_MIXED = re.compile(r"\b(mixed|coed|co-ed|open|combined)\b\s*", re.I)


@dataclass
class GenderSplitReport:
    events: Dict[str, int] = field(default_factory=dict)     # "F"/"M" -> scoring event
    created: List[int] = field(default_factory=list)
    moved: int = 0
    removed: List[int] = field(default_factory=list)          # combine: unused events dropped
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _refuse_if_final(race: Race) -> None:
    if race.status == "final":
        raise ValueError("the race is final: reopen it before changing how it is scored")


def _gender_name(base: Event, gender: str) -> str:
    """'Mixed 2 Mile Run CC Middle School' -> 'Girls 2 Mile Run CC Middle
    School'; a name with no gender word gets one in front."""
    word = GENDER_WORD[gender]
    name = re.sub(r"\b(girls|boys|women|men)\b\s*", "", base.name, flags=re.I).strip()
    if _MIXED.search(name):
        return _MIXED.sub(word + " ", name, count=1).strip()
    return f"{word} {name}".strip()


def _find_or_make(meet: Meet, base: Event, gender: str, rep: GenderSplitReport) -> int:
    for e in meet.events:
        if (e.num != base.num and e.gender == gender and e.code == base.code
                and e.distance_m == base.distance_m and e.division == base.division
                and e.kind == base.kind):
            return e.num
    num = max(e.num for e in meet.events) + 1
    meet.events.append(Event(num, _gender_name(base, gender), gender=gender,
                             distance_m=base.distance_m, division=base.division,
                             code=base.code, kind=base.kind))
    rep.created.append(num)
    return num


def split_genders(meet: Meet, race: Race) -> GenderSplitReport:
    """Score `race` by gender: every entry moves to a scoring event of its
    athlete's gender (an existing one with the same distance, code and
    division, else a new one named after the race's event). Idempotent.
    Refused while the race is final. Athletes with no M/F gender stay where
    they are (warned)."""
    _refuse_if_final(race)
    if race.together:
        # Moving entries by gender would fold the run-together events into
        # one (2026-10-05).
        raise ValueError("this race runs other races with it: run them separately first")
    rep = GenderSplitReport()
    base = meet.event(race.event)
    if base is None:
        raise ValueError(f"race {race.key}: no event {race.event}")
    athletes = {a.id: a for a in meet.athletes}
    for e in race.entries:
        a = athletes.get(e.athlete_id)
        g = a.gender if a else ""
        if g not in GENDER_WORD:
            rep.warnings.append(f"entry {e.athlete_id}: gender {g!r}, left in event {e.event}")
            continue
        if g not in rep.events:
            src = meet.event(e.event) or base
            if src.gender == g:                 # already a one-gender event
                rep.events[g] = src.num
            else:
                rep.events[g] = _find_or_make(meet, base, g, rep)
        if e.event != rep.events[g]:
            e.event = rep.events[g]
            rep.moved += 1
    return rep


def combine_genders(meet: Meet, race: Race, event_num: Optional[int] = None) -> GenderSplitReport:
    """Undo split_genders: every entry scores in one event again (`event_num`,
    default the race's own event). Scoring events left with no entries and
    no race of their own are dropped (they were made by the split).
    Refused while the race is final."""
    _refuse_if_final(race)
    if race.together:
        raise ValueError("this race runs other races with it: run them separately first")
    target = race.event if event_num is None else event_num
    if meet.event(target) is None:
        raise ValueError(f"no event {target}")
    rep = GenderSplitReport()
    before = {e.event for e in race.entries}
    for e in race.entries:
        if e.event != target:
            e.event = target
            rep.moved += 1
    used = {e.event for r in meet.races for e in r.entries} | {r.event for r in meet.races}
    for num in sorted(before - used):
        meet.events = [ev for ev in meet.events if ev.num != num]
        rep.removed.append(num)
    return rep


def is_split_by_gender(meet: Meet, race: Race) -> bool:
    """True when the race's entries score in more than one event and each
    of those events is one gender: what the console's toggle shows. A
    girls race run together with a boys race is not a split (2026-10-05)."""
    if race.together:
        return False
    nums = {e.event for e in race.entries if e.status != "scratched"}
    evs = [meet.event(n) for n in nums]
    return len(nums) > 1 and all(ev is not None and ev.gender in GENDER_WORD for ev in evs) \
        and len({ev.gender for ev in evs}) == len(evs)


# --------------------------------------------------------------- run together

# Run several races at the same time, score them separately (2026-10-05,
# user: both JV boys races go off one gun but "need them to be scored
# separately"; "update the .EVT file so that everything is combined in
# FinishLynx under one race but the middleware will know to separate them
# both out at the end"). The other races' entries move into the target race
# keeping their own scoring event (Entry.event), and those races go, so the
# start lists carry one Lynx event; scoring, printing, mpResults and Live
# already place and team-score per scoring event. run_separately undoes it.

@dataclass
class RunTogetherReport:
    race: str = ""                                            # "4-1-1"
    events: List[int] = field(default_factory=list)           # scoring events now in it
    moved: int = 0
    removed: List[str] = field(default_factory=list)          # races folded in / made again
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _rk(race: Race) -> str:
    return "-".join(str(x) for x in race.key)


def _event_name(meet: Meet, num: int) -> str:
    ev = meet.event(num)
    return ev.name if ev else f"Event {num}"


def together_name(meet: Meet, events: List[int]) -> str:
    """The name a run-together race gets: its events' names joined."""
    return " + ".join(_event_name(meet, n) for n in events)


def run_together(meet: Meet, target: Race, others: List[Race]) -> RunTogetherReport:
    """Fold `others` into `target`: one gun, one Lynx event, every entry
    still scored in its own event. An athlete already in the target keeps
    that entry (warned). The target is renamed after its events unless the
    operator named it. Refused while any of the races is final, and for a
    race scored by gender (its split events would be folded in too)."""
    others = [r for r in others if r is not target]
    if not others:
        raise ValueError("pick at least one other race to run with it")
    for r in [target] + others:
        if r.status == "final":
            raise ValueError(f"race {_rk(r)} is final: reopen it first")
        if is_split_by_gender(meet, r) and not r.together:
            raise ValueError(f"race {_rk(r)} is scored by gender: score genders together first")
    auto = not target.name or target.name == together_name(meet, [target.event] + target.together)
    rep = RunTogetherReport(race=_rk(target))
    have = {e.athlete_id for e in target.entries}
    for r in others:
        for e in r.entries:
            if e.athlete_id in have:
                rep.warnings.append(f"athlete {e.athlete_id} is in both {_rk(r)} and "
                                    f"{_rk(target)}: kept the entry in {_rk(target)}")
                continue
            e.lane = None
            target.entries.append(e)
            have.add(e.athlete_id)
            rep.moved += 1
        for n in [r.event] + r.together:
            if n != target.event and n not in target.together:
                target.together.append(n)
        # its divisions go on being scored, now in the target (2026-10-06)
        for d in r.divisions:
            if d not in target.divisions:
                target.divisions.append(d)
        meet.races.remove(r)
        rep.removed.append(_rk(r))
    rep.events = [target.event] + target.together
    if auto:
        target.name = together_name(meet, rep.events)
    return rep


def is_run_together(meet: Meet, race: Race) -> bool:
    """Other races run with this one (run_together): what the console's
    "Run separately" and "scored separately" note show."""
    return bool(race.together)


def run_separately(meet: Meet, race: Race) -> RunTogetherReport:
    """Undo run_together: each folded-in event gets its race back (same
    run order and start time as this one) with its entries. Refused while
    the race is final."""
    if race.status == "final":
        raise ValueError(f"race {_rk(race)} is final: reopen it first")
    if not race.together:
        raise ValueError(f"race {_rk(race)} isn't running other races with it")
    rep = RunTogetherReport(race=_rk(race))
    auto = race.name == together_name(meet, [race.event] + race.together)
    for n in race.together:
        home = meet.race(n)
        if home is None:
            # each race scored in the same divisions as the gun it left (2026-10-06)
            home = Race(n, order=race.order, start_time=race.start_time,
                        divisions=list(race.divisions))
            meet.races.append(home)
        for e in [e for e in race.entries if e.event == n]:
            race.entries.remove(e)
            home.entries.append(e)
            rep.moved += 1
        rep.removed.append(_rk(home))
    race.together = []
    if auto:
        race.name = ""
    rep.events = [race.event]
    return rep


# --------------------------------------------------------------- bib wearer

# "Someone else is wearing this bib" (2026-10-03, user: the Swap bibs flow
# must also let the operator enter a NEW athlete, with as much filled in as
# possible, when the runner isn't on the loaded roster for that team). The
# bib's current holder tells us the likely team, gender and race; the
# console searches that team first (roster-only athletes too, when full
# rosters were imported) and only then offers the prefilled new-athlete
# form. A new athlete is source "local": idmatch looks up its MileSplit id.

HOLDER_ACTIONS = ("next_free", "swap", "clear")     # clear = no bib, out of their races


@dataclass
class WearerReport:
    athlete_id: str
    bib: str
    created: bool = False
    entered_in: Optional[List[int]] = None        # race key it was entered in
    holder_id: Optional[str] = None
    holder_bib: str = ""                          # the holder's bib afterwards
    holder_out_of: List[List[int]] = field(default_factory=list)   # races "no bib" took them out of
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _view(meet: Meet, a: Athlete) -> dict:
    races = [list(r.key) for r, e in meet.entries_for(a.id) if e.status != "scratched"]
    t = meet.team(a.team_id)
    return {"athlete_id": a.id, "first": a.first, "last": a.last, "gender": a.gender,
            "grade": meet.grade(a), "team_id": a.team_id,
            "team": (t.short or t.name) if t else "", "bib": a.bib,
            "races": races, "entered": bool(races), "milesplit_id": a.milesplit_id}


def bib_context(meet: Meet, bib: str) -> dict:
    """What the 'who is wearing this bib?' panel opens with: the holder,
    the prefill for a new athlete (bib, team, gender, race and scoring event
    from the holder's entry), whether that race is final, and the holder's
    team roster to search first (entered athletes first)."""
    n = bib_number(bib)
    if n is None:
        raise ValueError(f"bib {bib!r} is not a valid bib")
    holder = meet.athlete_by_bib(str(n))
    prefill = {"bib": str(n), "team_id": None, "gender": "", "race": None, "event": None,
               "first": "", "last": "", "grade": ""}
    final = False
    if holder is not None:
        prefill.update(team_id=holder.team_id, gender=holder.gender)
        live = [(r, e) for r, e in meet.entries_for(holder.id) if e.status != "scratched"]
        if live:
            r, e = live[0]
            prefill.update(race=list(r.key), event=e.event)
            final = r.status == "final"
    roster = [_view(meet, a) for a in meet.athletes
              if holder is not None and a.team_id == holder.team_id and a.id != holder.id]
    roster.sort(key=lambda v: (not v["entered"], v["last"].casefold(), v["first"].casefold()))
    return {"bib": str(n), "holder": _view(meet, holder) if holder else None,
            "prefill": prefill, "race_final": final, "team_roster": roster}


def search_team(meet: Meet, team_id: Optional[str], first: str = "", last: str = "",
                limit: int = 8) -> List[dict]:
    """The team's athletes (entered and roster-only) ranked against a typed
    name: exact last+first, then last name + first-name prefix (Will /
    William), then last name only, then first name only. Empty name = all,
    entered first. Accent/case/punctuation-blind (idmatch.norm).

    team_id ... (Ellipsis) = every team, unattached included: the console's
    "Potential matches" when no team is picked yet (2026-10-03, MEET-API.md)."""
    from .idmatch import norm
    f, l = norm(first), norm(last)
    scored = []
    for a in meet.athletes:
        if team_id is not ... and a.team_id != team_id:
            continue
        af, al = norm(a.first), norm(a.last)
        if not (f or l):
            rank = 0
        elif l and al == l and f and af == f:
            rank = 0
        elif l and al == l and f and (af.startswith(f) or f.startswith(af)):
            rank = 1
        elif l and al == l:
            rank = 2
        elif f and af == f and not l:
            rank = 3
        else:
            continue
        v = _view(meet, a)
        scored.append((rank, not v["entered"], al, af, v))
    scored.sort(key=lambda s: s[:4])
    return [s[4] for s in scored[:limit]]


class SamePerson(ValueError):
    """A "new" athlete who is already in the meet: same first and last name
    on the same team (roster-only or entered). view = the one to use."""

    def __init__(self, view: dict):
        super().__init__(f"{view['first']} {view['last']} is already on "
                         f"{view.get('team') or 'the unattached list'}: use them, or add anyway")
        self.view = view


def same_person(meet: Meet, team_id: Optional[str], first: str, last: str) -> Optional[dict]:
    """The athlete a walk-up or a new bib wearer already is: the same name
    (accent/case/punctuation-blind) on the same team, None = unattached.
    2026-10-08, user: changing athletes or adding one with a new number
    must take them from the roster list first if they are there."""
    from .idmatch import norm
    f, l = norm(first), norm(last)
    if not (f and l):
        return None
    for v in search_team(meet, team_id, first, last, limit=3):
        if norm(v["first"]) == f and norm(v["last"]) == l:
            return v
    return None


def _guard_final(meet: Meet, race_key) -> Optional[Race]:
    if race_key is None:
        return None
    race = meet.race(*race_key)
    if race is None:
        raise ValueError(f"no race {race_key}")
    if race.status == "final":
        raise ValueError(f"race {race.name or race.event} is final: reopen it before "
                         "giving its bib to another athlete")
    return race


def _gender_fit(meet: Meet, race: Optional[Race], gender: str, from_holder: bool,
                rep: "WearerReport") -> Optional[Race]:
    """The race to enter the wearer in, or None. 2026-10-05: a boy's bib
    given to a girl took the holder's race - the boys' race. Taken from the
    holder, a race of the other gender is skipped (warned: her reads find
    her race at close); asked for by name, it is refused."""
    if race is None:
        return None
    ev = meet.event(race.event)
    g, mine = (ev.gender if ev else "X").upper(), (gender or "").upper()[:1]
    if g not in ("M", "F") or mine not in ("M", "F") or mine == g:
        return race
    name = race.name or (ev.name if ev else f"race {race.event}")
    if not from_holder:
        raise ValueError(f"{name} is a {'boys' if g == 'M' else 'girls'}' race")
    rep.warnings.append(f"not entered in {name} (the bib's old race is a "
                        f"{'boys' if g == 'M' else 'girls'}' race): enter them in a race, "
                        "or the close dialog offers it from their reads")
    return None


def _settle_holder(meet: Meet, holder: Athlete, wearer_old_bib: str, action: str,
                   rep: WearerReport) -> None:
    """The bib's previous holder after the wearer takes it."""
    rep.holder_id = holder.id
    holder.bib = wearer_old_bib if action == "swap" else ""
    if action == "clear":
        # 2026-10-05, user: "no bib" is for a holder who isn't here (a coach
        # handed their bib on): out of their races too. A final race keeps
        # its entry (its results are frozen).
        for race, e in meet.entries_for(holder.id):
            name = race.name or (meet.event(race.event).name if meet.event(race.event) else f"race {race.event}")
            if race.status == "final":
                rep.warnings.append(f"{holder.first} {holder.last} stays in {name}: it is final")
                continue
            race.entries.remove(e)
            rep.holder_out_of.append(list(race.key))
            rep.warnings.append(f"{holder.first} {holder.last}: no bib, taken out of {name}")
    if action == "next_free":
        from . import bibs
        try:
            # skip the bib being handed over: nobody holds it at this point,
            # so as the lowest free bib it would go straight back to the
            # holder (2026-10-03, found wiring the console API).
            out = bibs.assign(meet, athlete_ids=[holder.id], skip=[rep.bib])
            rep.warnings.extend(out.warnings)
        except ValueError as e:                       # no ranges set
            rep.warnings.append(f"{holder.first} {holder.last} left without a bib: {e}")
    rep.holder_bib = holder.bib


def give_bib_to(meet: Meet, bib: str, athlete_id: str, *, race=..., event=...,
                holder_action: str = "next_free") -> WearerReport:
    """An athlete already in the meet (found on the team roster) is the one
    wearing `bib`: they take it, are entered in `race` if not already in a
    race, and the holder is settled per holder_action (next_free | swap |
    clear). race / event: left out (...) = the holder's race and scoring
    event; None = don't enter them anywhere."""
    if holder_action not in HOLDER_ACTIONS:
        raise ValueError(f"holder_action must be one of {HOLDER_ACTIONS}")
    ctx = bib_context(meet, bib)
    from_holder = race is ...
    race = ctx["prefill"]["race"] if race is ... else race
    event = ctx["prefill"]["event"] if event is ... else event
    target = _guard_final(meet, race)
    a = meet.athlete(athlete_id)
    if a is None:
        raise ValueError(f"unknown athlete {athlete_id}")
    holder = meet.athlete_by_bib(ctx["bib"])
    rep = WearerReport(athlete_id=a.id, bib=ctx["bib"])
    if not meet.entered(a.id):
        target = _gender_fit(meet, target, a.gender, from_holder, rep)
    old = a.bib
    if holder is not None and holder.id != a.id:
        a.bib = ""
        _settle_holder(meet, holder, old, holder_action, rep)
    a.bib = ctx["bib"]
    if target is not None and not meet.entered(a.id):
        target.entries.append(Entry(a.id, event if event is not None else target.event))
        rep.entered_in = list(target.key)
    return rep


def add_wearer(meet: Meet, bib: str, first: str, last: str, *, gender=...,
               team_id=..., grade_text: str = "", grad_year: Optional[int] = None,
               race=..., event=..., holder_action: str = "next_free",
               allow_same: bool = False) -> WearerReport:
    """A runner who is NOT on the loaded roster is wearing `bib`: add them
    to the meet with everything the bib's holder tells us (team, gender,
    race and scoring event), give them the bib and enter them; the holder
    is settled per holder_action. The new athlete is source "local" so
    idmatch looks for its MileSplit id (auto only on an exact match, else
    the review queue).

    Every prefilled field can be changed or left blank (2026-10-03, user):
    an argument left out (...) takes the prefill; None / "" is blank -
    team None = unattached, race None = added but not entered in any race
    (enter them later), grade blank. Gender can be changed but not left
    blank: start lists and scoring need M or F.

    Someone already on the team's roster by that name raises SamePerson
    (2026-10-08: they were added again, without their MileSplit id) unless
    allow_same - the operator said it's a different runner."""
    if holder_action not in HOLDER_ACTIONS:
        raise ValueError(f"holder_action must be one of {HOLDER_ACTIONS}")
    if not (first.strip() and last.strip()):
        raise ValueError("first and last name are needed")
    ctx = bib_context(meet, bib)
    pre = ctx["prefill"]
    gender = str((pre["gender"] if gender is ... else gender) or "").upper()
    if gender not in GENDER_WORD:
        raise ValueError("gender is needed (M or F)")
    team_id = pre["team_id"] if team_id is ... else (team_id or None)
    from_holder = race is ...
    race = pre["race"] if race is ... else (race or None)
    event = pre["event"] if event is ... else event
    if team_id is not None and meet.team(team_id) is None:
        raise ValueError(f"unknown team {team_id}")
    target = _guard_final(meet, race)
    rep = WearerReport(athlete_id="", bib=ctx["bib"], created=True)
    target = _gender_fit(meet, target, gender, from_holder, rep)
    same = same_person(meet, team_id, first, last)
    if same is not None:
        if not allow_same:
            raise SamePerson(same)
        rep.warnings.append(f"{first} {last} is already on this team's roster "
                            f"({same['athlete_id']}); added anyway as asked")
    holder = meet.athlete_by_bib(ctx["bib"])
    if holder is not None:
        _settle_holder(meet, holder, "", holder_action, rep)
    a = Athlete(meet.new_athlete_id(), first.strip(), last.strip(), gender, team_id=team_id,
                bib=ctx["bib"], grad_year=grad_year, grade_text=str(grade_text or "").strip(),
                source="local")
    meet.athletes.append(a)
    rep.athlete_id = a.id
    if target is not None:
        target.entries.append(Entry(a.id, event if event is not None else target.event))
        rep.entered_in = list(target.key)
    return rep
