"""XC team scoring over the meet model, and freezing a race's results.

RaceManager._team_scores (races.py) ranks teams by school name but drops
what each runner scored; MileSplit Live, the printed team block and the
mpResults upload all need per-runner points and scorer/displacer flags, and
they need exhibition / unattached / non-scoring teams kept out, which a
school string can't tell. This module scores from the meet model instead.
See docs/MEET-MANAGEMENT-PLAN.md (scoring.py, UX patterns 6-9).

Rules (decided 2026-10-03):
- `meet.meet.scoring` scorers / displacers (5 / 2). Only complete teams
  (>= scorers eligible finishers) score; their next `displacers` runners
  take a place (displace); anyone beyond that takes no place.
- Never score and never displace: exhibition entries, `athlete.unattached`,
  teams with `score=False`, athletes with no team, and members of incomplete
  teams (so a 3-runner team never pushes a scoring team's runner back).
- Tie on total: the team whose 6th runner (first displacer) placed better
  wins; a team with no 6th runner loses the tie. Two tied teams that both
  lack a 6th runner share the rank (the tie stands; nothing to compare).
  No ghost runners, no NCAA tie-break.
- One gun start can carry several scoring events (combined JV start): every
  finisher is scored in their entry's event, with places and points counted
  within that event's field; the overall race place is kept too.
- Partial teams are listed after the ranked ones, unranked, as "3/5".

Flagged runners (2026-10-06, user): a finisher who isn't entered in the race
(not_entered), whose bib nobody in the meet holds (unknown_bib) or whose
entry here is scratched (scratched) is no longer a reason to refuse Mark
final. They are left out of the results - no place, no points, nobody moves
back because of them - and listed in results["issues"] (crossing order) until
the operator resolves them: Ignore (Race.ignored), Swap/Give the bib, or
Override (enter them here). A runner with a DNF/DQ/DNS mark is not flagged.

Frozen shape (shared contract with printing.py and exports.py; the model's
Race.results holds it once the race is final):

    {"frozen_at": "2026-10-10T11:42:05",            # None from live_scores
     "finishers": [{"place", "bib", "athlete_id", "event", "elapsed",
                    "status", "event_place", "points", "scorer",
                    "displacer", "fl_only"}, ...],   # race order, then DNF/DQ/DNS
     "issues": [{"code", "text", "bib", "athlete_id", "event", "elapsed",
                 "crossed" (n-th across, or None on course), "status"
                 ("finished" | "on_course")}, ...],   # left out, see above
     "ignored": [{"bib", "athlete_id"}, ...],
     "teams": {"<event num>": [{"rank", "team_id", "score", "complete",
                                "count", "runners": [{"athlete_id", "bib",
                                "points", "scorer"}], "tiebreak_points",
                                "total_time", "avg_time"}, ...]}}

Reopen (decided 2026-10-03): `reopen` sets the race back to "scheduled"
and KEEPS race.results, the copy last made final, so the console can show
what changed and exports can tell a re-push from a first push. race.status
is the authority: results are official only while the race is "final"
(`official_results`). Re-freezing overwrites them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .model import Athlete, Entry, Meet, Race


def tie_key(elapsed) -> "Optional[int]":
    """A time as whole thousandths, truncated: equal keys are a true tie -
    the same place and the same points (owner, 2026-10-13; FinishLynx dead
    heat, C-008). None for no time."""
    if elapsed is None:
        return None
    return int(float(elapsed) * 1000 + 1e-6) if elapsed >= 0 else -int(-float(elapsed) * 1000 + 1e-6)

FINISHED = "finished"
NON_FINISH = ("DNF", "DQ", "DNS")          # also the order they're listed in

# Warning codes that stop a race from going final (UX pattern 8): each one
# either changes someone's place or points, or leaves a nameless row in what
# gets printed and uploaded. The others are reported but don't block.
BLOCKING = frozenset({
    "lif_hold",       # chip finish waiting on "Save to .lif" (fl_untimed / off_list)
    "no_time",        # finish row without an elapsed time
    "on_course",      # splits but no finish, and no DNF/DQ given
})
# Flagged runners (2026-10-06): left out of the results until resolved, but
# they no longer stop Mark final (see the module notes).
ISSUES = ("not_entered", "unknown_bib", "scratched")


class Unresolved(Exception):
    """freeze() refused: `issues` are the blocking warnings (dicts with
    code / bib / text), every one of which must be fixed first."""

    def __init__(self, issues: List[dict]):
        self.issues = issues
        super().__init__("; ".join(i["text"] for i in issues))


def _warn(code: str, bib: str, text: str) -> dict:
    return {"code": code, "bib": bib, "text": text}


def _bib_key(bib: str):
    """Numeric bibs in number order, anything else after them."""
    return (0, int(bib), "") if str(bib).isdigit() else (1, 0, str(bib))


def _round(x: Optional[float]) -> Optional[float]:
    # Milliseconds are plenty (times print to hundredths); keeps float
    # noise like 983.5200000001 out of meet.json.
    return None if x is None else round(float(x), 3)


# --------------------------------------------------------------- scoring

def _entry_for(meet: Meet, athlete_id: str, event_num: int,
               race: Optional[Race]) -> Optional[Entry]:
    """The athlete's entry in `event_num`: in `race` when given (what
    build_results uses), else anywhere in the meet."""
    races = [race] if race is not None else meet.races
    for r in races:
        for e in r.entries:
            if e.athlete_id == athlete_id and e.event == event_num:
                return e
    return None


def _scoring_team(meet: Meet, athlete: Optional[Athlete],
                  entry: Optional[Entry]) -> Optional[str]:
    """The team id this runner scores for, or None when they never score
    or displace (exhibition, unattached, no team, non-scoring team,
    scratched / missing entry)."""
    if athlete is None or entry is None or entry.status != "entered":
        return None
    if athlete.unattached or not athlete.team_id:
        return None
    team = meet.team(athlete.team_id)
    if team is None or not team.score:
        return None
    return team.id


def score_event(meet: Meet, finishers_in_order: List[dict], event_num: int,
                race: Optional[Race] = None) -> List[dict]:
    """Team rows for one scoring event. Pure: reads the finishers (frozen
    shape; only status "finished" rows with event == event_num count, in
    the order given) and the meet; changes nothing.

    `race` limits the entry lookup (exhibition / scratched) to that race;
    without it the athlete's entry for the event is looked up meet-wide."""
    sc = meet.meet.scoring
    scorers, limit = sc.scorers, sc.scorers + sc.displacers

    field = [f for f in finishers_in_order
             if f.get("status") == FINISHED and f.get("event") == event_num]
    # Indexed once: live scoring runs on every edit, over meets of 1,000+
    # athletes, and the model's lookups are linear scans.
    athletes = {a.id: a for a in meet.athletes}
    by_team: Dict[str, List[dict]] = {}
    team_of: Dict[int, str] = {}                    # id(finisher) -> team id
    for f in field:
        aid = f.get("athlete_id")
        athlete = athletes.get(aid) if aid else None
        entry = _entry_for(meet, aid, event_num, race) if athlete else None
        tid = _scoring_team(meet, athlete, entry)
        if tid:
            by_team.setdefault(tid, []).append(f)
            team_of[id(f)] = tid

    complete = {t for t, rows in by_team.items() if len(rows) >= scorers}

    # Points are handed out in finish order to complete teams' first
    # scorers + displacers only; everyone else is skipped, not counted.
    points: Dict[int, int] = {}                     # id(finisher) -> points
    taken: Dict[str, int] = {}
    n, pts, prev_key = 0, 0, object()
    for f in field:
        tid = team_of.get(id(f))
        if tid not in complete or taken.get(tid, 0) >= limit:
            continue
        taken[tid] = taken.get(tid, 0) + 1
        n += 1
        key = tie_key(f.get("elapsed"))
        if key is None or key != prev_key:          # a true tie: the same points (owner, 2026-10-13)
            pts = n
        prev_key = key
        points[id(f)] = pts

    ranked, partial = [], []
    for tid, rows in by_team.items():
        count = f"{len(rows)}/{scorers}"
        if tid not in complete:
            partial.append({
                "rank": None, "team_id": tid, "score": None, "complete": False,
                "count": count,
                "runners": [{"athlete_id": r["athlete_id"], "bib": r["bib"],
                             "points": None, "scorer": False} for r in rows[:limit]],
                "tiebreak_points": None, "total_time": None, "avg_time": None,
            })
            continue
        top = rows[:limit]
        scoring = top[:scorers]
        times = [r["elapsed"] for r in scoring]
        total = None if any(t is None for t in times) else sum(times)
        ranked.append({
            "rank": None, "team_id": tid,
            "score": sum(points[id(r)] for r in scoring),
            "complete": True, "count": count,
            "runners": [{"athlete_id": r["athlete_id"], "bib": r["bib"],
                         "points": points[id(r)], "scorer": i < scorers}
                        for i, r in enumerate(top)],
            "tiebreak_points": points[id(top[scorers])] if len(top) > scorers else None,
            "total_time": _round(total),
            "avg_time": _round(None if total is None else total / scorers),
        })

    def order(t):
        # No 6th runner loses the tie; team id only fixes the listing order
        # of a tie that stands.
        tb = t["tiebreak_points"]
        return (t["score"], tb is None, tb or 0, t["team_id"])

    ranked.sort(key=order)
    prev = None
    for i, t in enumerate(ranked, 1):
        same = prev is not None and prev["score"] == t["score"] \
            and prev["tiebreak_points"] is None and t["tiebreak_points"] is None
        t["rank"] = prev["rank"] if same else i
        prev = t

    def place_of(f):
        return f.get("event_place") or f.get("place") or 0

    partial.sort(key=lambda t: (-len(by_team[t["team_id"]]),
                                place_of(by_team[t["team_id"]][0]), t["team_id"]))
    return ranked + partial


# --------------------------------------------------------------- results

def build_results(meet: Meet, race: Race, rm_results: dict, *,
                  statuses: Optional[Dict[str, str]] = None, final: bool = True,
                  now: Optional[datetime] = None) -> Tuple[dict, List[dict]]:
    """Frozen-shape results for `race` from RaceManager.results(key), plus
    warnings ({code, bib, text}; codes in BLOCKING stop freeze()).

    statuses: bib -> "DNF" / "DQ" / "DNS" the operator decided (a DQ'd
    finisher drops out of the places; an on-course runner becomes DNF).
    None = the race's own marks (Race.marks); pass a dict to override.

    final=True (freeze): entered athletes who never showed are listed DNS,
    on-course runners without a status are warned as "on_course", and
    frozen_at is set. final=False (live_scores): the race may still be
    running, so neither, and frozen_at is None."""
    if statuses is None:
        statuses = race.marks
    statuses = {str(k).strip(): str(v).upper() for k, v in (statuses or {}).items()}
    for bib, st in statuses.items():
        if st not in NON_FINISH:
            raise ValueError(f"bib {bib}: status must be one of {', '.join(NON_FINISH)}, not {st!r}")

    warnings: List[dict] = []
    issues: List[dict] = []
    entry_of = {e.athlete_id: e for e in race.entries}
    athletes = {a.id: a for a in meet.athletes}
    bib_of = {}
    for a in meet.athletes:
        if a.bib:
            bib_of.setdefault(a.bib, a)       # first holder, as athlete_by_bib

    def who(bib: str):
        """(athlete, entry, event) for a bib; event falls back to the race's
        own event for a runner with no entry here, so they still print."""
        a = bib_of.get(bib.strip())
        e = entry_of.get(a.id) if a else None
        return a, e, (e.event if e else race.event)

    def label(a: Optional[Athlete]) -> str:
        if a is None:
            return ""
        t = meet.team(a.team_id)
        return f" ({a.last}, {a.first}{', ' + (t.short or t.name) if t else ''})"

    def issue_of(bib: str, a: Optional[Athlete], e: Optional[Entry]) -> Optional[dict]:
        """The flag on a runner left out of the results, or None."""
        if a is None:
            return _warn("unknown_bib", bib, f"bib {bib} crossed but nobody in the meet has it")
        if e is None:
            if all(en.auto for en in race.entries):
                # an open race - no entries of its own (owner's rule 2026-10-06:
                # "if there are 0 entries to that race, no flagging"). The meet
                # enters its finishers by itself, but not on a preview console and
                # not at once: no flag per runner meanwhile (field test 2026-10-13)
                return None
            return _warn("not_entered", bib, f"bib {bib}{label(a)} isn't entered in this race")
        if e.status == "scratched":
            return _warn("scratched", bib, f"bib {bib}{label(a)} is scratched in this race")
        return None

    def ignored(bib: str, a: Optional[Athlete]) -> bool:
        """Taken out by the operator while the bib is still the same
        athlete's (Race.ignored)."""
        held_by = race.ignored.get(bib)
        return held_by is not None and held_by == (a.id if a else "")

    def flag(issue: dict, a: Optional[Athlete], ev: int, elapsed, crossed, status: str):
        warnings.append(issue)
        issues.append(dict(issue, athlete_id=a.id if a else None, event=ev,
                           elapsed=_round(elapsed), crossed=crossed, status=status))

    finishers: List[dict] = []
    others: List[dict] = []
    seen = set()
    shown_ignored: List[dict] = []
    for crossed, row in enumerate(rm_results.get("finishers", []), 1):
        bib = str(row["bib"])
        seen.add(bib)
        fin = row.get("finish") or {}
        a, e, ev = who(bib)
        if ignored(bib, a):
            shown_ignored.append({"bib": bib, "athlete_id": a.id if a else None})
            continue
        rec = {"place": None, "bib": bib, "athlete_id": a.id if a else None, "event": ev,
               "elapsed": _round(fin.get("elapsed")), "status": FINISHED,
               "event_place": None, "points": None, "scorer": False, "displacer": False,
               "fl_only": bool(fin.get("fl_only")),
               # Splits for the printout (2026-10-03, user: mile splits in the
               # 1-column PDF when there are any): course order, label as the
               # console shows it, ~estimated ones flagged.
               "splits": [{"label": sp.get("label") or sp.get("point", ""),
                           "elapsed": _round(sp["elapsed"]),
                           "estimated": bool(sp.get("estimated"))}
                          for sp in row.get("splits") or [] if sp.get("elapsed") is not None]}
        if bib in statuses:
            rec.update(status=statuses[bib], elapsed=None)
            others.append(rec)
            continue
        issue = issue_of(bib, a, e)
        if issue:
            flag(issue, a, ev, fin.get("elapsed"), crossed, FINISHED)
            continue
        why = fin.get("lif_why") or ("fl_untimed" if fin.get("fl_untimed") else "")
        if why == "fl_untimed":
            warnings.append(_warn("lif_hold", bib,
                                  f"bib {bib}{label(a)}: FinishLynx lists them without a "
                                  f"time - save the chip time to the .lif or time them in FinishLynx"))
        elif why:
            warnings.append(_warn("lif_hold", bib,
                                  f"bib {bib}{label(a)}: chip finish held off the .lif "
                                  f"({why}) - save it to the .lif or remove the read"))
        if rec["elapsed"] is None:
            warnings.append(_warn("no_time", bib, f"bib {bib}{label(a)} has a finish but no time"))
        if row.get("missing"):
            warnings.append(_warn("missing_splits", bib,
                                  f"bib {bib}{label(a)} is missing {', '.join(row['missing'])}"))
        finishers.append(rec)

    for row in rm_results.get("on_course", []):
        bib = str(row["bib"])
        seen.add(bib)
        a, e, ev = who(bib)
        if ignored(bib, a):
            shown_ignored.append({"bib": bib, "athlete_id": a.id if a else None})
            continue
        issue = issue_of(bib, a, e) if bib not in statuses else None
        if issue:
            flag(issue, a, ev, None, None, "on_course")
            continue
        if bib in statuses:
            others.append({"place": None, "bib": bib, "athlete_id": a.id if a else None,
                           "event": ev, "elapsed": None, "status": statuses[bib],
                           "event_place": None, "points": None, "scorer": False,
                           "displacer": False, "fl_only": False})
        elif final:
            warnings.append(_warn("on_course", bib,
                                  f"bib {bib}{label(a)} has splits but no finish: find "
                                  f"the finish or mark them DNF"))

    # Statuses for bibs RaceManager didn't list (a DQ given before any read).
    for bib, st in statuses.items():
        if bib in seen:
            continue
        a, e, ev = who(bib)
        seen.add(bib)
        others.append({"place": None, "bib": bib, "athlete_id": a.id if a else None,
                       "event": ev, "elapsed": None, "status": st, "event_place": None,
                       "points": None, "scorer": False, "displacer": False, "fl_only": False})

    if final:
        # Entered (or exhibition) and never seen: DNS. Scratched athletes
        # aren't listed at all; checked-in bibs RaceManager reports as
        # no-shows are added too.
        dns: List[Tuple[str, Optional[str], int]] = []
        for e in race.entries:
            if e.status == "scratched":
                continue
            a = athletes.get(e.athlete_id)
            bib = a.bib if a else ""
            if bib and bib in seen:
                continue
            if bib:
                seen.add(bib)
            dns.append((bib, e.athlete_id if a else None, e.event))
        for bib in rm_results.get("no_show", []):
            bib = str(bib)
            if bib in seen:
                continue
            seen.add(bib)
            a, e, ev = who(bib)
            if e is not None and e.status == "scratched":
                continue
            if ignored(bib, a):
                continue
            dns.append((bib, a.id if a else None, ev))
        for bib, aid, ev in dns:
            others.append({"place": None, "bib": bib, "athlete_id": aid, "event": ev,
                           "elapsed": None, "status": "DNS", "event_place": None,
                           "points": None, "scorer": False, "displacer": False,
                           "fl_only": False})

    # Overall place = race order as RaceManager gave it; event place counts
    # within each scoring event's own field (exhibition runners included:
    # they ran in it, they just don't score).
    # True ties (owner, 2026-10-13): the same time to the thousandth shares the
    # place (and the event place); the next one is skipped.
    per_event: Dict[int, int] = {}
    last: Dict[int, tuple] = {}                     # event -> (tie key, its event place)
    prev_key, place = object(), 0
    for i, f in enumerate(finishers, 1):
        key = tie_key(f.get("elapsed"))
        if key is None or key != prev_key:
            place = i
        f["place"], prev_key = place, key
        per_event[f["event"]] = per_event.get(f["event"], 0) + 1
        lk = last.get(f["event"])
        f["event_place"] = lk[1] if (lk and key is not None and lk[0] == key) else per_event[f["event"]]
        last[f["event"]] = (key, f["event_place"])

    events = sorted({e.event for e in race.entries if e.status != "scratched"}
                    | {f["event"] for f in finishers})
    teams: Dict[str, List[dict]] = {}
    for ev in events:
        rows = score_event(meet, finishers, ev, race=race)
        teams[str(ev)] = rows
        mine = {f["athlete_id"]: f for f in finishers
                if f["event"] == ev and f["athlete_id"]}
        for t in rows:
            if not t["complete"]:
                continue
            for r in t["runners"]:
                f = mine[r["athlete_id"]]
                f["points"] = r["points"]
                f["scorer"] = r["scorer"]
                f["displacer"] = not r["scorer"]

    others.sort(key=lambda f: (NON_FINISH.index(f["status"]), _bib_key(f["bib"]),
                               f["athlete_id"] or ""))
    # Who they were when frozen (break test 2026-10-13): a team or gender
    # changed after Mark final shows the race as stale (meet_service.stale_finals)
    for f in finishers + others:
        a = athletes.get(f["athlete_id"]) if f["athlete_id"] else None
        f["team_id"] = a.team_id if a else None
        f["gender"] = a.gender if a else ""
    # The split columns every printout shares: the points RaceManager expects
    # in this race (any real read there), labelled as the splits are.
    labels = {}
    for row in rm_results.get("finishers", []):
        for sp in row.get("splits") or []:
            labels.setdefault(sp.get("point"), sp.get("label") or sp.get("point"))
    split_labels = [labels.get(p, p) for p in rm_results.get("expected_points") or []]
    results = {
        "frozen_at": (now or datetime.now()).isoformat(timespec="seconds") if final else None,
        "finishers": finishers + others,
        "teams": teams,
        "split_labels": split_labels,
        "issues": issues,
        "ignored": shown_ignored,
    }
    return results, warnings


def live_scores(meet: Meet, race: Race, rm_results: dict,
                statuses: Optional[Dict[str, str]] = None) -> dict:
    """The same shape as the frozen results, recomputed from the current
    reads (UX pattern 9: re-scoring is automatic, there is no Re-Score
    button). Nothing is frozen: frozen_at is None, runners still out are
    left out, and no one is DNS yet."""
    return build_results(meet, race, rm_results, statuses=statuses, final=False)[0]


def unresolved(warnings: List[dict]) -> List[dict]:
    """The warnings that stop a race from going final."""
    return [w for w in warnings if w["code"] in BLOCKING]


def freeze(meet: Meet, race: Race, rm_results: dict, now: Optional[datetime] = None,
           statuses: Optional[Dict[str, str]] = None) -> List[dict]:
    """Mark `race` final with a frozen copy of its results (UX pattern 8).

    Raises Unresolved, changing nothing, while any finisher is unresolved
    (see BLOCKING). Otherwise sets race.results and race.status = "final"
    and returns the remaining, non-blocking warnings (missing splits).
    Freezing a race that is already final re-freezes it. The caller saves
    the meet (MeetStore.save) and pushes."""
    results, warnings = build_results(meet, race, rm_results, statuses=statuses,
                                      final=True, now=now)
    issues = unresolved(warnings)
    if issues:
        raise Unresolved(issues)
    race.results = results
    race.status = "final"
    return warnings


def reopen(meet: Meet, race: Race) -> None:
    """Back to "scheduled" so reads can be fixed; race.results keeps the
    last frozen copy until the race is frozen again (see module doc)."""
    race.status = "scheduled"


def official_results(race: Race) -> Optional[dict]:
    """race.results while the race is final, else None: what printing and
    exports should treat as official."""
    return race.results if race.status == "final" else None
