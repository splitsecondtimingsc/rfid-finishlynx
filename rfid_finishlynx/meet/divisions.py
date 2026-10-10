"""Divisions: extra standings scored inside a race (2026-10-06).

The user's ask: "set a division number, and then be able to assign divisions
given the following categories: M/F/Both, Teams (individual groups or all),
or grade based", set up on the Meet info page, and "within each race ... set
the divisions that should be scored for it". "Unless otherwise set, a meet
should not contain divisions and can work as usual."

Decided with the user (2026-10-06):
- A division combines all three filters: gender (both / M / F), teams (all,
  or a list), grades (all, or a list). A runner is in it when all match.
  "All teams" takes unattached runners too; a team list doesn't.
- Re-scored inside the division: places and team points are counted among
  the division's runners alone, as if they had run by themselves, with the
  meet's scorers / displacers and every scoring.py rule (exhibition,
  unattached and non-scoring teams never score; incomplete teams don't
  score or displace). Division.team_scores False = individual places only.
- Within each scoring event: a race running several events (run together,
  or girls and boys scored apart) scores the division separately in each
  one, so a "both genders" division never pools a girls race with a boys
  race. A mixed race that is one event pools girls and boys, as it does now.

Nothing here is stored: the division standings are worked out from a race's
results (frozen or live) whenever they are shown or printed. So the race's
own results, Mark final, MileSplit upload and Live are untouched, and a
division added or changed after a race went final shows at once without
re-finalizing. Who is in a division is read from the athletes as they are
now (team, gender, grade).

Output: the frozen-results shape (scoring.py), so printing and the PDF
renderer print it with their usual code:
    {"frozen_at", "split_labels",
     "finishers": [... as scoring, with "event_place" = place in the
                   division (event), "points" / "scorer" / "displacer" from
                   the division's scoring, plus "overall_place" = the place
                   in the event's full field],
     "teams": {"<event num>": [score_event rows]},   # {} without team scores
     "division": {"id", "name", "team_scores"}}
"""

from __future__ import annotations

import copy
from typing import Dict, Iterable, List, Optional, Tuple

from . import scoring
from .model import Athlete, Division, Meet, Race, grade_key

GENDER_LABELS = {"": "Both genders", "M": "Boys", "F": "Girls"}


def member(div: Division, athlete: Optional[Athlete], meet: Meet) -> bool:
    """Is this athlete in the division (gender, team and grade all match)?"""
    if athlete is None:
        return False
    if div.gender and athlete.gender != div.gender:
        return False
    if div.team_ids and (athlete.team_id is None or athlete.team_id not in div.team_ids):
        return False
    if div.grades and grade_key(meet.grade(athlete)) not in {grade_key(g) for g in div.grades}:
        return False
    return True


def describe(div: Division, meet: Meet) -> str:
    """'Girls · 3 teams · Grades 9, 10 · no team scores': what the console
    lists under a division's name."""
    parts = [GENDER_LABELS.get(div.gender, div.gender)]
    if div.team_ids:
        if len(div.team_ids) <= 2:
            names = [(t.short or t.name) if (t := meet.team(i)) else i for i in div.team_ids]
            parts.append(", ".join(names))
        else:
            parts.append(f"{len(div.team_ids)} teams")
    else:
        parts.append("all teams")
    if div.grades:
        keys = sorted({grade_key(g) for g in div.grades},
                      key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0, k))
        parts.append(("Grade " if len(keys) == 1 else "Grades ") + ", ".join(keys))
    if not div.team_scores:
        parts.append("individuals only")
    return " · ".join(parts)


def for_race(meet: Meet, race: Race) -> List[Division]:
    """The divisions a race is scored in, in the meet's division order."""
    ids = set(race.divisions)
    return [d for d in meet.meet.divisions if d.id in ids]


def pick(meet: Meet, race: Race, divisions=None) -> Tuple[bool, List[Division]]:
    """What a printout covers for one race: (print the race's own results,
    division standings to print after them).

    divisions: None / "all" = the race's results and every division it is
    scored in; "none" = the race's results only; a list of division ids (or
    a comma string) = only those divisions' standings, without the race's
    own results (a division the race isn't scored in is skipped)."""
    mine = for_race(meet, race)
    if divisions is None or (isinstance(divisions, str) and divisions.strip().lower() in ("", "all")):
        return True, mine
    if isinstance(divisions, str) and divisions.strip().lower() == "none":
        return True, []
    ids = parse_ids(divisions)
    return False, [d for d in mine if d.id in ids]


def parse_ids(divisions) -> List[str]:
    if isinstance(divisions, str):
        divisions = divisions.split(",")
    return [str(x).strip() for x in divisions or [] if str(x).strip()]


def check_pick(meet: Meet, divisions) -> None:
    """ValueError for a division id the meet doesn't have (a print asked
    for something that would silently print nothing)."""
    if divisions is None or (isinstance(divisions, str) and
                             divisions.strip().lower() in ("", "all", "none")):
        return
    ids = parse_ids(divisions)
    if not ids:
        raise ValueError("divisions: give all, none or division ids")
    unknown = [i for i in ids if meet.division(i) is None]
    if unknown:
        raise ValueError(f"no division {', '.join(unknown)} in this meet")


def describe_pick(meet: Meet, divisions) -> str:
    """Header note for a division-only printout ('Division Small Schools');
    '' when the printout is the usual one (with or without divisions)."""
    if divisions is None or (isinstance(divisions, str) and
                             divisions.strip().lower() in ("", "all", "none")):
        return ""
    names = [d.name for i in parse_ids(divisions) if (d := meet.division(i))]
    if not names:
        return ""
    return ("Division " if len(names) == 1 else "Divisions ") + ", ".join(names)


def results(meet: Meet, race: Race, res: Optional[dict], div: Division) -> Optional[dict]:
    """The division's standings in `race`, from the race's results `res`
    (frozen or live, scoring.py shape). None when there are no results.
    Pure: neither the meet nor `res` is changed."""
    if res is None:
        return None
    athletes = {a.id: a for a in meet.athletes}
    by_bib = {}
    for a in meet.athletes:
        if a.bib:
            by_bib.setdefault(a.bib, a)

    def athlete_of(f: dict) -> Optional[Athlete]:
        aid = f.get("athlete_id")
        return athletes.get(aid) if aid else by_bib.get(str(f.get("bib") or "").strip())

    finishers: List[dict] = []
    for f in res.get("finishers") or []:
        if not member(div, athlete_of(f), meet):
            continue
        g = copy.deepcopy(f)
        if g.get("event") is None:
            g["event"] = race.event
        g["overall_place"] = f.get("event_place")
        g.update(points=None, scorer=False, displacer=False)
        finishers.append(g)

    # Places within the division, per scoring event, in the race's order
    # (the finishers list is race order, then DNF/DQ/DNS).
    count: Dict[int, int] = {}
    for g in finishers:
        if g.get("status") == scoring.FINISHED:
            count[g["event"]] = count.get(g["event"], 0) + 1
            g["event_place"] = count[g["event"]]
        else:
            g["event_place"] = None

    teams: Dict[str, List[dict]] = {}
    if div.team_scores:
        events = sorted({g["event"] for g in finishers})
        for ev in events:
            rows = scoring.score_event(meet, finishers, ev, race=race)
            if not rows:
                continue
            teams[str(ev)] = rows
            mine = {g["athlete_id"]: g for g in finishers
                    if g["event"] == ev and g.get("athlete_id") and g.get("status") == scoring.FINISHED}
            for t in rows:
                if not t["complete"]:
                    continue
                for r in t["runners"]:
                    g = mine[r["athlete_id"]]
                    g["points"] = r["points"]
                    g["scorer"] = r["scorer"]
                    g["displacer"] = not r["scorer"]

    return {"frozen_at": res.get("frozen_at"),
            "split_labels": list(res.get("split_labels") or []),
            "finishers": finishers, "teams": teams,
            "division": {"id": div.id, "name": div.name, "team_scores": div.team_scores}}


def race_results(meet: Meet, race: Race, res: Optional[dict],
                 divisions: Optional[Iterable[Division]] = None) -> List[Tuple[Division, dict]]:
    """[(division, its standings)] for every division the race is scored in
    (or the ones given); [] when the race has no results or no divisions."""
    if res is None:
        return []
    divs = for_race(meet, race) if divisions is None else list(divisions)
    return [(d, results(meet, race, res, d)) for d in divs]


def entered_events(meet: Meet, race: Race, div: Division) -> List[int]:
    """Scoring events of `race` with at least one (non-scratched) entry in
    the division: the sections a division prints even before anyone
    finishes."""
    athletes = {a.id: a for a in meet.athletes}
    return sorted({e.event for e in race.entries
                   if e.status != "scratched" and member(div, athletes.get(e.athlete_id), meet)})


def as_race(race: Race, div_res: dict) -> Race:
    """A stand-in race whose official results are the division's standings,
    so printing.filter_results and the reports read them like any race's
    (places, grades, schools, hide and events filters all apply)."""
    import dataclasses
    return dataclasses.replace(race, results=div_res, status="final")


def view(meet: Meet, div: Division) -> dict:
    """A division as the console shows it."""
    return {"id": div.id, "name": div.name, "gender": div.gender,
            "team_ids": list(div.team_ids), "grades": list(div.grades),
            "team_scores": div.team_scores, "text": describe(div, meet),
            "races": [f"{r.event}-{r.round}-{r.heat}" for r in meet.races_in_order()
                      if div.id in r.divisions]}
