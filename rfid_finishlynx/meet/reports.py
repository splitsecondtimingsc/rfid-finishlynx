"""Meet reports beyond the results sheet: team scores, awards, finish line
sheets, team / competitor / award labels and split scores.

Why (2026-10-03): the user picked these from MeetPro's Reports and Labels
menus ("1-6"), plus scoring a mixed race separately (edits.split_genders),
which every report here follows because it works per scoring event.

Data first, like printing.filter_results / roster_pages: each report is a
pure function returning plain dicts and lists (JSON-serialisable: race keys
are [event, round, heat] lists, times come both raw in seconds and as the
text printing.format_time makes). The *_text functions lay that data out in
printing's fixed-width Hy-Tek style (same header, same _row / _fit columns,
form feeds between pages); the PDF renderer (pdf_reports.py) lays the same
data out as PDF. Columns are the *_COLS constants and label sheets use
AVERY_5160, so the two outputs share widths and geometry and can't apply
different rules.

Rules decided here (2026-10-03):
- Only final races (scoring.official_results) feed the scored reports;
  chosen races that aren't final are listed under "not_final", not dropped
  silently. Finish line sheets and labels don't need results.
- A race whose entries score in several events (combined JV start, or a
  mixed race split by gender) is one section per scoring event everywhere.
- Team scores reuse the frozen team rows as scoring.py ranked them: shared
  ranks stay shared, and a tied row gets printing's tie-break note.
  Scoring order "1-3-7-8-13(18)(23)": scorers' points, displacers' in
  brackets. Spread = last scorer's time - first's, from the printed
  (hundredths rounded up) times, so a coach's subtraction agrees.
- Awards: the first N eligible finishers per scoring event by real place.
  Exhibition runners are skipped (they don't score; the award passes to
  the next runner) unless include_exhibition; unattached runners are
  eligible. "award" is the award place, "place" the real place. Teams:
  every ranked team with rank <= M, so a tie at the cut gets both.
- Split scores rescore each split with scoring.score_event itself (same
  scorers / displacers / exhibition / incomplete-team rules), ordering the
  runners who have that split by its elapsed time. Runners with no time at
  that split are left out and listed; DNF runners count at the splits they
  reached; DQ / DNS never do.
- Divisions (2026-10-06): team scores, awards, award labels and the
  announcer give each division a race is scored in its own sections, after
  the scoring event's own ("Girls 5k Varsity - Small Schools"; "division"
  holds its name), from the division's standings (divisions.py): places,
  awards and team ranks within the division. divisions= as printing
  (None/"all", "none", or ids for those divisions only).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from rfid_finishlynx.meet import divisions as divs_mod
from rfid_finishlynx.meet import printing, scoring
from rfid_finishlynx.meet.bibs import summarize_bibs
from rfid_finishlynx.meet.model import Athlete, Meet, Race
from rfid_finishlynx.meet.printing import Col, format_time

RaceRef = Union[Race, Sequence[int], int]

# --------------------------------------------------------------- columns

TEAM_SCORE_COLS: List[Col] = [("Rank", 4, ">"), ("Team", 24, "<"), ("Score", 5, ">"),
                              ("Scoring order", 29, "<"), ("Total Time", 10, ">"),
                              ("Average", 9, ">"), ("Spread", 8, ">")]
# Score Breakdown rows sit under their team, indented.
BREAKDOWN_INDENT = 5
BREAKDOWN_COLS: List[Col] = [("Pl", 4, ">"), ("Name", 24, "<"), ("Bib", 6, ">"),
                             ("Yr", 2, ">"), ("Time", 11, ">"), ("Pts", 5, ">")]
AWARD_COLS: List[Col] = [("Award", 5, "<"), ("Pl", 4, ">"), ("Name", 24, "<"),
                         ("School", 24, "<"), ("Time", 11, ">")]
AWARD_TEAM_COLS: List[Col] = [("Award", 10, "<"), ("Team", 30, "<"), ("Score", 5, ">")]
FINISH_COLS: List[Col] = [("Place", 5, ">"), ("Bib", 8, "<"), ("Time", 12, "<"),
                          ("Name / Notes", 40, "<")]

FINISH_ROWS_PER_PAGE = 40
FINISH_SPARE = 0.10                 # blank rows beyond the entries, before page rounding
DEFAULT_AWARDS = 10
DEFAULT_TEAM_AWARDS = 3

# Avery 5160 / 8160 address labels, in PDF points (72 per inch): Letter,
# 3 columns x 10 rows of 2.625 x 1 in, 0.5 in top margin, 0.1875 in side
# margin, 2.75 in column pitch (0.125 in gutter), 1 in row pitch.
POINTS_PER_INCH = 72.0
AVERY_5160 = {
    "name": "Avery 5160",
    "page_w": 8.5 * POINTS_PER_INCH, "page_h": 11 * POINTS_PER_INCH,
    "cols": 3, "rows": 10,
    "label_w": 2.625 * POINTS_PER_INCH, "label_h": 1.0 * POINTS_PER_INCH,
    "top": 0.5 * POINTS_PER_INCH, "left": 0.1875 * POINTS_PER_INCH,
    "col_pitch": 2.75 * POINTS_PER_INCH, "row_pitch": 1.0 * POINTS_PER_INCH,
}
# Label line styles the renderers know: "bold" (a heading), "big" (the bib
# on a competitor label), "small" (secondary), "normal".
LABEL_STYLES = ("bold", "big", "normal", "small")


# --------------------------------------------------------------- helpers

def ordinal(n: int) -> str:
    """1 -> '1st', 12 -> '12th', 23 -> '23rd'."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _hundredths(seconds: float) -> int:
    """The hundredths format_time prints (rounded up, float noise first)."""
    return -(-max(0, int(round(float(seconds) * 1000))) // 10)


def _key(race: Race) -> List[int]:
    return list(race.key)


def _race_title(meet: Meet, race: Race) -> str:
    return race.name or printing._event_name(meet, race.event, race)


def _pick_races(meet: Meet, races: Optional[Iterable[RaceRef]]) -> List[Race]:
    """Races in run order (None = all), or the ones asked for in the order
    given: a Race, a key [event, round, heat], or a bare Lynx event number."""
    if races is None:
        return meet.races_in_order()
    out = []
    for r in races:
        if isinstance(r, Race):
            out.append(r)
            continue
        key = (int(r), 1, 1) if isinstance(r, (int, str)) else tuple(int(x) for x in r)
        race = meet.race(*key)
        if race is None:
            raise ValueError(f"no race {'-'.join(map(str, key))}")
        out.append(race)
    return out


def _final_races(meet: Meet, races) -> Tuple[List[Race], List[dict]]:
    final, not_final = [], []
    for race in _pick_races(meet, races):
        if scoring.official_results(race) is None:
            not_final.append({"race": _key(race), "name": _race_title(meet, race)})
        else:
            final.append(race)
    return final, not_final


def _sources(meet: Meet, race: Race, divisions, events) -> List[Tuple[Optional[object], dict]]:
    """[(division or None, filter_results-shaped dict)] a final race's
    reports read: its own results, then each division's standings."""
    overall, div_res = printing.division_results(meet, race, divisions, events=events)
    out = [(None, printing.filter_results(meet, race, events=events))] if overall else []
    return out + [(div, fres) for div, fres, _ in div_res]


def _section_name(meet: Meet, num: int, race: Race, div) -> str:
    name = printing._event_name(meet, num, race)
    return f"{name} - {div.name}" if div is not None else name


def _per_event(table: Optional[dict], num: int, default: int) -> int:
    """{event: n} from Python (int keys) or JSON (str keys)."""
    table = table or {}
    for k in (num, str(num)):
        if k in table and table[k] is not None:
            return max(0, int(table[k]))
    return max(0, int(default))


def _team_name(meet: Meet, team_id: Optional[str]) -> str:
    t = meet.team(team_id)
    return t.name if t else ("Unattached" if team_id is None else f"Unknown team {team_id}")


def _runner(meet: Meet, f: Optional[dict], athlete_id: Optional[str], bib: str,
            points: Optional[int], scorer: bool) -> dict:
    """A team's runner as the reports print it; f = its (pseudo-)finisher row."""
    a = meet.athlete(athlete_id) if athlete_id else None
    elapsed = (f or {}).get("elapsed")
    displacer = points is not None and not scorer
    return {"athlete_id": athlete_id, "bib": bib or (a.bib if a else ""),
            "name": printing._name(a, bib), "last": a.last.strip() if a else "",
            "first": a.first.strip() if a else "", "grade": printing._grade(meet, a),
            "place": (f or {}).get("event_place"), "overall_place": (f or {}).get("place"),
            "elapsed": elapsed, "time": format_time(elapsed),
            "points": points, "scorer": bool(scorer and points is not None),
            "displacer": displacer,
            "points_text": "" if points is None else (f"({points})" if displacer else str(points))}


def _team_rows(meet: Meet, teams: List[dict], fins: Dict[Tuple[int, str], dict],
               num: int) -> Tuple[List[dict], List[dict]]:
    """(ranked, incomplete) team rows from scoring.score_event-shaped rows.
    `fins` maps (event, athlete_id) to the row each runner's place and time
    come from (a finisher, or a split's pseudo-finisher)."""
    scorers = meet.meet.scoring.scorers
    ranked_in = [t for t in teams if t.get("rank") is not None]
    ranked, incomplete = [], []
    for t in teams:
        runners = [_runner(meet, fins.get((num, r.get("athlete_id"))), r.get("athlete_id"),
                           r.get("bib", ""), r.get("points"), r.get("scorer", False))
                   for r in t.get("runners", [])]
        team = meet.team(t.get("team_id"))
        row = {"rank": t.get("rank"), "team_id": t.get("team_id"),
               "team": _team_name(meet, t.get("team_id")),
               "short": (team.short if team else "") or "", "code": team.code if team else "",
               "label": printing._team_label(team, TEAM_SCORE_COLS[1][1]),
               "score": t.get("score"), "count": t.get("count", ""),
               "complete": bool(t.get("complete")), "runners": runners}
        if t.get("rank") is None:
            row.update(order="", total_time=None, avg_time=None, spread=None,
                       total_text="", avg_text="", spread_text="", tied=False,
                       tiebreak_points=None, tiebreak_text="")
            incomplete.append(row)
            continue
        pts = [r["points"] for r in runners if r["scorer"]]
        disp = [r["points"] for r in runners if r["displacer"]]
        order = "-".join(str(p) for p in pts) + "".join(f"({p})" for p in disp)
        times = [r["elapsed"] for r in runners if r["scorer"]]
        spread = None
        if len(times) >= scorers and all(x is not None for x in times):
            spread = (_hundredths(times[scorers - 1]) - _hundredths(times[0])) / 100
        tied = t["tied"] if "tied" in t else any(
            o is not t and o.get("score") == t.get("score") for o in ranked_in)
        tb = t.get("tiebreak_points")
        row.update(order=order, total_time=t.get("total_time"), avg_time=t.get("avg_time"),
                   spread=spread, total_text=format_time(t.get("total_time")),
                   avg_text=format_time(t.get("avg_time")),
                   spread_text=format_time(spread), tied=bool(tied), tiebreak_points=tb,
                   tiebreak_text=(f"Tie-break *{scorers + 1}: {tb if tb is not None else 'none'}"
                                  if tied else ""))
        ranked.append(row)
    return ranked, incomplete


def _header(meet: Meet, what: str, width: int, printed_at, notes: Sequence[str] = ()) -> str:
    note = printing.FILTER_SEP.join(n for n in notes if n)
    return "\n".join(printing._header(meet, what, width, printed_at, note=note))


def _body(head: str, chunks: List[List[str]], empty: str) -> str:
    """Header, then chunks joined by form feeds (each a new page)."""
    body = "\n\f".join("\n".join(l.rstrip() for l in c) for c in chunks) if chunks else empty
    return head + "\n" + body.rstrip("\n") + "\n"


def _not_final_lines(not_final: List[dict]) -> List[str]:
    if not not_final:
        return []
    names = ", ".join(f"Event {r['race'][0]} {r['name']}" for r in not_final)
    return ["", f"Not final, not included: {names}"]


def _team_table_lines(sec_teams: List[dict], incomplete: List[dict],
                      breakdown: bool) -> List[str]:
    """Rank / team / score / order / times rows, tie-break notes, and with
    breakdown each team's runners under it (a blank line between teams so
    a page break falls between teams, see printing.paginate)."""
    cols = TEAM_SCORE_COLS
    pad = " " * BREAKDOWN_INDENT
    lines = [printing._col_header(cols)]
    if breakdown:
        lines.append(pad + printing._col_header(BREAKDOWN_COLS))
    lines.append("=" * printing._width(cols))

    def runner_lines(t):
        return [pad + printing._row(BREAKDOWN_COLS, [r["place"] or "", r["name"], r["bib"],
                                                     r["grade"], r["time"], r["points_text"]])
                for r in t["runners"]]

    for i, t in enumerate(sec_teams):
        if breakdown and i:
            lines.append("")
        lines.append(printing._row(cols, [t["rank"], t["label"], t["score"], t["order"],
                                          t["total_text"], t["avg_text"], t["spread_text"]]))
        if t["tiebreak_text"]:
            lines.append(pad + t["tiebreak_text"])
        if breakdown:
            lines += runner_lines(t)
    for t in incomplete:
        if breakdown:
            lines.append("")
        lines.append(printing._row(cols[:3], ["", t["label"], t["count"]])
                     + "  incomplete, not scored")
        if breakdown:
            lines += runner_lines(t)
    return lines


# --------------------------------------------------------------- 1. team scores

def team_scores(meet: Meet, races: Optional[Iterable[RaceRef]] = None, *,
                events: Optional[Iterable[int]] = None, breakdown: bool = False,
                divisions=None) -> dict:
    """Team scores of every final race (or the chosen ones), one section per
    scoring event that has team rows, in run order.

    {"breakdown": bool, "scorers": 5, "displacers": 2,
     "sections": [{"race": [ev, rd, ht], "race_name", "event", "event_name",
       "teams": [{rank, team_id, team, short, code, label, score, count,
                  complete, order "1-3-7-8-13(18)(23)", total_time, avg_time,
                  spread (s), total_text, avg_text, spread_text, tied,
                  tiebreak_points, tiebreak_text, runners: [{athlete_id,
                  bib, name, last, first, grade, place (event place),
                  overall_place, elapsed, time, points, scorer, displacer,
                  points_text "13" | "(18)" | ""}]}],
       "incomplete": [same keys, rank/score None, count "3/5"]}],
     "not_final": [{"race", "name"}]}
    Runners (up to scorers + displacers) are always there; `breakdown` only
    tells the renderers to print them (MeetPro's Score Breakdown option)."""
    sc = meet.meet.scoring
    divs_mod.check_pick(meet, divisions)
    finals, not_final = _final_races(meet, races)
    sections = []
    for race in finals:
        mine = []
        for i, (div, res) in enumerate(_sources(meet, race, divisions, events)):
            fins = {(f["event"], f.get("athlete_id")): f for f in res["finishers"]}
            for key in sorted(res["teams"], key=int):
                num = int(key)
                ranked, incomplete = _team_rows(meet, res["teams"][key], fins, num)
                mine.append(((num, i), {"race": _key(race), "race_name": _race_title(meet, race),
                                        "event": num,
                                        "event_name": _section_name(meet, num, race, div),
                                        "division": div.name if div is not None else "",
                                        "teams": ranked, "incomplete": incomplete}))
        sections += [sec for _, sec in sorted(mine, key=lambda x: x[0])]
    return {"breakdown": bool(breakdown), "scorers": sc.scorers, "displacers": sc.displacers,
            "sections": sections, "not_final": not_final}


def team_scores_text(meet: Meet, printed_at: Optional[datetime] = None, *,
                     races: Optional[Iterable[RaceRef]] = None,
                     events: Optional[Iterable[int]] = None,
                     breakdown: bool = False, divisions=None) -> str:
    """The Team Scores report: every section one after another under one
    header (no page per event, so a whole meet fits on as few pages as
    possible)."""
    data = team_scores(meet, races, events=events, breakdown=breakdown, divisions=divisions)
    width = printing._width(TEAM_SCORE_COLS)
    head = _header(meet, "Team Scores", width, printed_at,
                   ["Score Breakdown" if breakdown else "",
                    printing.describe_filter(meet, events=events, divisions=divisions)])
    lines: List[str] = []
    for sec in data["sections"]:
        if lines:
            lines.append("")
        lines.append(f"Event {sec['event']}  {sec['event_name']}")
        lines += _team_table_lines(sec["teams"], sec["incomplete"], breakdown)
    if not data["sections"]:
        lines.append("No team scores yet.")
    lines += _not_final_lines(data["not_final"])
    return _body(head, [lines], "")


# --------------------------------------------------------------- 3. awards

def award_winners(meet: Meet, races: Optional[Iterable[RaceRef]] = None, *,
                  awards: Optional[Dict[int, int]] = None, default_n: int = DEFAULT_AWARDS,
                  team_awards: Optional[Dict[int, int]] = None,
                  default_teams: int = DEFAULT_TEAM_AWARDS,
                  events: Optional[Iterable[int]] = None,
                  include_exhibition: bool = False, divisions=None) -> dict:
    """Who gets an award, per scoring event of every final race (or the
    chosen ones): the top N individuals (awards[event], else default_n) and
    the top M teams (team_awards[event], else default_teams). Who is
    eligible and how ties go: see the module notes.

    {"sections": [{"race", "race_name", "event", "event_name", "n", "m",
       "individuals": [{award, award_text "3rd Place", place, athlete_id,
                        bib, name "Last, First", label_name "First Last",
                        last, first, grade, team_id, school, school_short,
                        elapsed, time}],
       "short": how many fewer eligible finishers than N,
       "teams": [{rank, award_text "3rd Place" / "3rd Place (tie)",
                  team_id, team, code, score, tied}]}],
     "not_final": [...]}"""
    divs_mod.check_pick(meet, divisions)
    finals, not_final = _final_races(meet, races)
    sections = []
    for race in finals:
        entries = {e.athlete_id: e for e in race.entries}
        mine = []
        for src, (div, res) in enumerate(_sources(meet, race, divisions, events)):
            nums = sorted({f["event"] for f in res["finishers"]} | {int(k) for k in res["teams"]})
            for num in nums:
                n = _per_event(awards, num, default_n)
                m = _per_event(team_awards, num, default_teams)
                rows = sorted((f for f in res["finishers"]
                               if f["event"] == num and f.get("status") == scoring.FINISHED),
                              key=lambda f: (f.get("event_place") or 0, f.get("place") or 0))
                individuals = []
                for f in rows:
                    if len(individuals) >= n:
                        break
                    a = printing._finisher_athlete(meet, f)
                    e = entries.get(a.id) if a else None
                    if e is not None and e.status == "exhibition" and not include_exhibition:
                        continue
                    team = meet.team(a.team_id) if a else None
                    k = len(individuals) + 1
                    individuals.append({
                        "award": k, "award_text": f"{ordinal(k)} Place",
                        "place": f.get("event_place"), "athlete_id": a.id if a else None,
                        "bib": f.get("bib") or (a.bib if a else ""),
                        "name": printing._name(a, f.get("bib") or ""),
                        "label_name": (f"{a.first.strip()} {a.last.strip()}".strip() if a
                                       else printing._name(None, f.get("bib") or "")),
                        "last": a.last.strip() if a else "", "first": a.first.strip() if a else "",
                        "grade": printing._grade(meet, a),
                        "team_id": a.team_id if a else None,
                        "school": team.name if team else ("Unattached" if a else ""),
                        "school_short": ((team.short or team.name) if team
                                         else ("Unattached" if a else "")),
                        "elapsed": f.get("elapsed"), "time": format_time(f.get("elapsed"))})
                teams = []
                for t in (res["teams"].get(str(num)) or []):
                    rank = t.get("rank")
                    if rank is None or rank > m:
                        continue
                    team = meet.team(t.get("team_id"))
                    tied = _shares_rank(t, res["teams"][str(num)])
                    teams.append({"rank": rank,
                                  "award_text": f"{ordinal(rank)} Place" + (" (tie)" if tied else ""),
                                  "team_id": t.get("team_id"),
                                  "team": _team_name(meet, t.get("team_id")),
                                  "code": team.code if team else "", "score": t.get("score"),
                                  "tied": tied})
                if not individuals and not teams:
                    continue
                mine.append(((num, src), {
                    "race": _key(race), "race_name": _race_title(meet, race),
                    "event": num, "event_name": _section_name(meet, num, race, div),
                    "division": div.name if div is not None else "",
                    "n": n, "m": m, "individuals": individuals,
                    "short": max(0, n - len(individuals)), "teams": teams}))
        sections += [sec for _, sec in sorted(mine, key=lambda x: x[0])]
    return {"sections": sections, "not_final": not_final}


def _shares_rank(t: dict, teams: List[dict]) -> bool:
    """A tie that stands (same rank as another team), not one the 6th
    runner broke: only that one changes how many trophies are handed out."""
    return any(o is not t and o.get("rank") is not None and o.get("rank") == t.get("rank")
               for o in teams)


def awards_text(meet: Meet, printed_at: Optional[datetime] = None, **kw) -> str:
    """The announcer's awards list: per scoring event the individual awards
    (award, real place, name, school, time) then the team awards (place,
    team, score). Arguments as award_winners."""
    data = award_winners(meet, **kw)
    width = printing._width(AWARD_COLS)
    head = _header(meet, "Awards", width, printed_at,
                   [printing.describe_filter(meet, events=kw.get("events"),
                                             divisions=kw.get("divisions"))])
    lines: List[str] = []
    for sec in data["sections"]:
        if lines:
            lines.append("")
        lines += [f"Event {sec['event']}  {sec['event_name']}", "=" * width]
        if sec["n"]:
            lines.append(f"Individual awards: top {sec['n']}"
                         + (f" ({sec['short']} not awarded: too few finishers)"
                            if sec["short"] else ""))
            lines.append(printing._col_header(AWARD_COLS))
            for w in sec["individuals"]:
                lines.append(printing._row(AWARD_COLS, [
                    ordinal(w["award"]), w["place"], w["name"],
                    printing._team_label(meet.team(w["team_id"]), AWARD_COLS[3][1])
                    if w["team_id"] else w["school"], w["time"]]))
        if sec["teams"]:
            lines += ["", f"Team awards: top {sec['m']}", printing._col_header(AWARD_TEAM_COLS)]
            for t in sec["teams"]:
                award = ordinal(t["rank"]) + (" (tie)" if t["tied"] else "")
                lines.append(printing._row(AWARD_TEAM_COLS, [
                    award, printing._team_label(meet.team(t["team_id"]), AWARD_TEAM_COLS[1][1]),
                    t["score"]]))
    if not data["sections"]:
        lines.append("No awards: no final results yet.")
    lines += _not_final_lines(data["not_final"])
    return _body(head, [lines], "")


# --------------------------------------------------------------- announcer

def _team_limit(teams) -> int:
    """'all' (any case) = every scored team; else a count >= 0."""
    if isinstance(teams, str) and teams.strip().lower() == "all":
        return 10 ** 6
    n = int(teams)
    if n < 0:
        raise ValueError("teams must be a count or 'all'")
    return n


def announcer(meet: Meet, races: Optional[Iterable[RaceRef]] = None, *,
              top: int = DEFAULT_AWARDS, teams=DEFAULT_TEAM_AWARDS,
              events: Optional[Iterable[int]] = None,
              include_exhibition: bool = False, divisions=None) -> dict:
    """The announcer version of the awards (2026-10-03, user's ask): per
    scoring event the top `top` finishers and `teams` teams (a count or
    "all"), both in REVERSE order so they read aloud from Nth up to the
    winner: individuals first, then teams, so the team champion is last.
    Same eligibility and tie rules as award_winners. Incomplete teams are
    never ranked, so "all" means every scored team.

    {"top", "teams" (as given), "sections": [{race, race_name, event,
      event_name, individuals: [award rows, Nth -> 1st], teams: [team rows,
      last -> 1st], short, scored_teams}], "not_final"}"""
    if int(top) < 0:
        raise ValueError("top must be a count >= 0")
    data = award_winners(meet, races, default_n=int(top), default_teams=_team_limit(teams),
                         events=events, include_exhibition=include_exhibition,
                         divisions=divisions)
    sections = []
    for sec in data["sections"]:
        sections.append(dict(sec, individuals=list(reversed(sec["individuals"])),
                             teams=list(reversed(sec["teams"])),
                             scored_teams=len(sec["teams"])))
    return {"top": int(top), "teams": teams, "sections": sections,
            "not_final": data["not_final"]}


def announcer_lines(sec: dict) -> Tuple[List[str], List[str]]:
    """The read-aloud lines of one section: (individuals, teams)."""
    ind = [f"{w['award_text']}: {w['label_name']}, {w['school_short'] or w['school']}"
           + (f", {w['time']}" if w.get("time") else "") for w in sec["individuals"]]
    tm = [f"{t['award_text']}: {t['team']}, {t['score']} points" for t in sec["teams"]]
    return ind, tm


def announcer_text(meet: Meet, printed_at: Optional[datetime] = None, **kw) -> str:
    """announcer() as large, spaced text for reading aloud."""
    data = announcer(meet, **kw)
    width = 72
    teams = data["teams"]
    what = (f"Announcer: top {data['top']}, "
            + ("all teams" if isinstance(teams, str) else f"top {teams} teams")
            + ", read from the bottom up")
    head = _header(meet, what, width, printed_at,
                   [printing.describe_filter(meet, events=kw.get("events"),
                                             divisions=kw.get("divisions"))])
    lines: List[str] = []
    for sec in data["sections"]:
        if lines:
            lines += ["", ""]
        lines += [f"{sec['event_name']}", "=" * width]
        ind, tm = announcer_lines(sec)
        if ind:
            lines.append(f"INDIVIDUALS ({len(ind)})")
            for l in ind:
                lines += [l, ""]
        if sec["short"]:
            lines.append(f"({sec['short']} not awarded: too few finishers)")
        if tm:
            lines.append(f"TEAMS ({len(tm)})")
            for l in tm:
                lines += [l, ""]
    if not data["sections"]:
        lines.append("No awards: no final results yet.")
    lines += _not_final_lines(data["not_final"])
    return _body(head, [lines], "")


# --------------------------------------------------------------- labels

def _line(text, style: str = "normal") -> dict:
    return {"text": "" if text is None else str(text), "style": style}


def award_labels(data: dict, *, teams: bool = True) -> List[dict]:
    """One label per award from award_winners' data, in its order:
    individual {kind "award", lines: event name, "3rd Place", First Last,
    school, time}; team {kind "team_award", lines: event name, "1st Place
    Team", team, "Score 35"}."""
    out = []
    for sec in data["sections"]:
        for w in sec["individuals"]:
            out.append({"kind": "award", "event": sec["event"], "athlete_id": w["athlete_id"],
                        "lines": [_line(sec["event_name"], "small"),
                                  _line(w["award_text"], "bold"), _line(w["label_name"]),
                                  _line(w["school"]), _line(w["time"])]})
        if teams:
            for t in sec["teams"]:
                place = f"{ordinal(t['rank'])} Place Team" + (" (tie)" if t["tied"] else "")
                out.append({"kind": "team_award", "event": sec["event"], "team_id": t["team_id"],
                            "lines": [_line(sec["event_name"], "small"), _line(place, "bold"),
                                      _line(t["team"]), _line(f"Score {t['score']}")]})
    return out


def _team_groups(meet: Meet, team_ids: Optional[Sequence[Optional[str]]]) -> List[Optional[str]]:
    if team_ids is not None:
        return list(team_ids)
    return [t.id for t in sorted(meet.teams, key=lambda t: printing._sort_key(t.name))] + [None]


def _entered(meet: Meet, team_id: Optional[str]) -> List[Athlete]:
    """Entered athletes of a team (roster-only and scratched-only left out,
    as roster_pages), by last then first name."""
    out = [a for a in meet.athletes if a.team_id == team_id and meet.entered(a.id)]
    out.sort(key=lambda a: (printing._sort_key(a.last), printing._sort_key(a.first),
                            int(a.bib) if a.bib.isdigit() else 0))
    return out


def team_labels(meet: Meet, team_ids: Optional[Sequence[str]] = None, *,
                copies: int = 1) -> List[dict]:
    """A label per team (copies each) for its check-in packet: {kind "team",
    team_id, team, code, counts {"F", "M"}, bibs "101-112, 150", races
    [{event, gender, label}] (run order), lines: team name, "RID · Girls 6
    · Boys 7", "Bibs 101-112", "Girls V 5K, JV 5K · Boys V 5K"}. Teams by
    name (or team_ids order); a team with nobody entered is skipped unless
    asked for. Unattached runners are no team and get no label."""
    order = {r.key: i for i, r in enumerate(meet.races_in_order())}
    out = []
    for tid in _team_groups(meet, team_ids):
        team = meet.team(tid)
        if team is None:
            continue
        athletes = _entered(meet, tid)
        if not athletes and team_ids is None:
            continue
        counts = {"F": sum(a.gender == "F" for a in athletes)}
        counts["M"] = len(athletes) - counts["F"]
        bibs = summarize_bibs(a.bib for a in athletes if a.bib)
        seen = {}
        for a in athletes:
            for race, e in meet.entries_for(a.id):
                if e.status == "scratched" or e.event in seen:
                    continue
                ev = meet.event(e.event)
                seen[e.event] = (order.get(race.key, 0), e.event,
                                 (ev.gender if ev and ev.gender in ("F", "M") else a.gender))
        races = [{"event": num, "gender": g, "label": printing.race_short_label(meet, num)}
                 for _, num, g in sorted(seen.values())]
        parts = []
        for g, word in printing.ROSTER_GENDERS:
            mine = [r["label"] for r in races if r["gender"] == g]
            if mine:
                parts.append(f"{word} " + ", ".join(dict.fromkeys(mine)))
        sep = printing.FILTER_SEP
        lines = [_line(team.name, "bold"),
                 _line(sep.join(p for p in (team.code, f"Girls {counts['F']}",
                                            f"Boys {counts['M']}") if p)),
                 _line(f"Bibs {bibs}" if bibs else "No bibs yet"),
                 _line(sep.join(parts), "small")]
        label = {"kind": "team", "team_id": tid, "team": team.name, "code": team.code,
                 "counts": counts, "bibs": bibs, "races": races, "lines": lines}
        out += [label] * max(1, int(copies))
    return out


def competitor_labels(meet: Meet, team_ids: Optional[Sequence[Optional[str]]] = None
                      ) -> List[dict]:
    """A label per entered athlete (full_rosters' roster-only athletes and
    scratched-only ones left out), by team name (unattached last) then last,
    first name; team_ids = only those (None in it = unattached). {kind
    "competitor", athlete_id, team_id, bib, race "V 5K" ("+" = more than one
    race), lines: "LAST, First" bold, school, bib big ("----" = none yet),
    race}."""
    order = {r.key: i for i, r in enumerate(meet.races_in_order())}
    out = []
    for tid in _team_groups(meet, team_ids):
        team = meet.team(tid)
        school = team.name if team else ("Unattached" if tid is None else "")
        for a in _entered(meet, tid):
            race = printing._roster_row(meet, a, order)["race"]
            name = f"{a.last.strip().upper()}, {a.first.strip()}" if a.first.strip() \
                else a.last.strip().upper()
            out.append({"kind": "competitor", "athlete_id": a.id, "team_id": tid,
                        "bib": a.bib, "race": race,
                        "lines": [_line(name, "bold"), _line(school),
                                  _line(a.bib or "----", "big"), _line(race)]})
    return out


def label_sheets(labels: List[dict], geometry: Optional[dict] = None,
                 skip: int = 0) -> List[dict]:
    """Labels laid out on sheets, across then down. skip: label positions
    already used on the first sheet (a part-used sheet goes back in).

    [{"sheet": 1, "sheets": 2, "geometry": AVERY_5160,
      "labels": [{slot (1-30), row (1-10), col (1-3), x, y, top, w, h, ...the
                  label's own keys (kind, lines, ...)}]}]
    x, y: the label's lower-left corner in PDF points (origin bottom-left);
    top: its top edge measured down from the page top (pdfdoc's origin)."""
    g = dict(geometry or AVERY_5160)
    per = g["cols"] * g["rows"]
    skip = max(0, min(int(skip), per - 1)) if labels else 0
    sheets: List[dict] = []
    for i, label in enumerate(labels):
        pos = i + skip
        n, slot = divmod(pos, per)
        if n == len(sheets):
            sheets.append({"sheet": n + 1, "sheets": 0, "geometry": g, "labels": []})
        row, col = divmod(slot, g["cols"])
        placed = {"slot": slot + 1, "row": row + 1, "col": col + 1,
                  "x": g["left"] + col * g["col_pitch"],
                  "y": g["page_h"] - g["top"] - row * g["row_pitch"] - g["label_h"],
                  "top": g["top"] + row * g["row_pitch"],
                  "w": g["label_w"], "h": g["label_h"]}
        placed.update(label)
        sheets[n]["labels"].append(placed)
    for s in sheets:
        s["sheets"] = len(sheets)
    return sheets


LABEL_TEXT_WIDTH = 60


def labels_text(meet: Meet, sheets: List[dict], what: str = "Labels",
                printed_at: Optional[datetime] = None) -> str:
    """A proof list of label sheets (a page per sheet): slot, row/column and
    the label's lines. The PDF draws the real sheet."""
    n = sum(len(s["labels"]) for s in sheets)
    geom = (sheets[0]["geometry"] if sheets else AVERY_5160)["name"]
    head = _header(meet, what, LABEL_TEXT_WIDTH, printed_at,
                   [f"{geom}{printing.FILTER_SEP}{printing._plural(n, 'label')}"
                    f"{printing.FILTER_SEP}{printing._plural(len(sheets), 'sheet')}"])
    chunks = []
    for s in sheets:
        lines = [f"Sheet {s['sheet']} of {s['sheets']}", "=" * LABEL_TEXT_WIDTH]
        for lab in s["labels"]:
            tag = f"{lab['slot']:>2}  r{lab['row']:<2} c{lab['col']}  "
            for i, ln in enumerate(lab["lines"]):
                lines.append((tag if i == 0 else " " * len(tag)) + ln["text"])
            lines.append("")
        chunks.append(lines)
    return _body(head, chunks, "No labels.")


# --------------------------------------------------------------- 4. finish line sheets

def finish_line_rows(entered: int, rows_per_page: int = FINISH_ROWS_PER_PAGE) -> int:
    """Rows for a race of `entered` runners: +10 % rounded up, then up to a
    full page (a backup sheet that runs out mid-race is no backup)."""
    rpp = max(1, int(rows_per_page))
    want = -(-max(0, int(entered)) * int(round((1 + FINISH_SPARE) * 100)) // 100)
    return max(1, math.ceil(want / rpp)) * rpp


def finish_line_sheets(meet: Meet, races: Optional[Iterable[RaceRef]] = None, *,
                       rows: Optional[int] = None,
                       rows_per_page: int = FINISH_ROWS_PER_PAGE) -> List[dict]:
    """Blank backup sheets for the chute, per race (run order, or the ones
    given): [{race, event, title, meet, date, entered (not scratched),
    rows, rows_per_page, pages: [{page, pages, first, last}]}]. rows = a
    given count, else finish_line_rows(entered)."""
    rpp = max(1, int(rows_per_page))
    out = []
    for race in _pick_races(meet, races):
        entered = sum(1 for e in race.entries if e.status != "scratched")
        total = max(1, int(rows)) if rows else finish_line_rows(entered, rpp)
        n_pages = math.ceil(total / rpp)
        out.append({"race": _key(race), "event": race.event, "title": _race_title(meet, race),
                    "meet": meet.meet.name or meet.meet.id, "date": meet.meet.date,
                    "entered": entered, "rows": total, "rows_per_page": rpp,
                    "pages": [{"page": p, "pages": n_pages, "first": (p - 1) * rpp + 1,
                               "last": min(total, p * rpp)} for p in range(1, n_pages + 1)]})
    return out


def finish_line_text(meet: Meet, printed_at: Optional[datetime] = None, *,
                     races: Optional[Iterable[RaceRef]] = None, rows: Optional[int] = None,
                     rows_per_page: int = FINISH_ROWS_PER_PAGE) -> str:
    """Finish line sheets as text: a page per sheet page (form feeds), each
    with the race, the page count and numbered write-in rows."""
    width = printing._width(FINISH_COLS)
    head = _header(meet, "Finish Line Sheet", width, printed_at)
    blanks = ["_" * w for _, w, _ in FINISH_COLS[1:]]
    chunks = []
    for sheet in finish_line_sheets(meet, races, rows=rows, rows_per_page=rows_per_page):
        for p in sheet["pages"]:
            title = f"Event {sheet['event']}  {sheet['title']}"
            if p["page"] > 1:
                title += " (cont'd)"
            tag = f"page {p['page']} of {p['pages']}"
            lines = [printing._fit(title, width - len(tag) - 2) + "  " + tag,
                     f"{sheet['entered']} entered{printing.FILTER_SEP}"
                     f"places {p['first']}-{p['last']} of {sheet['rows']}",
                     "=" * width, printing._col_header(FINISH_COLS)]
            lines += [printing._row(FINISH_COLS, [n] + blanks)
                      for n in range(p["first"], p["last"] + 1)]
            chunks.append(lines)
    return _body(head, chunks, "No races.")


# --------------------------------------------------------------- 6. split scores

def _split_of(f: dict, label: str) -> Optional[dict]:
    return next((s for s in f.get("splits") or []
                 if s.get("label") == label and s.get("elapsed") is not None), None)


def split_scores(meet: Meet, race: Race, results: Optional[dict] = None) -> dict:
    """Team scores at each split of one race. results: the frozen shape
    (default the race's official results) or scoring.live_scores' dict.

    {"race", "race_name", "official": bool (frozen_at set), "split_labels",
     "sections": [{"event", "event_name",
       "splits": [{"label", "timed": runners with that split,
                   "missing": [bibs with no time there, left out],
                   "estimated": how many of the times are estimates,
                   "teams": [team_scores' row shape; place / time = at
                             the split], "incomplete": [...]}]}]}
    Raises ValueError without results."""
    res = results if results is not None else scoring.official_results(race)
    if res is None:
        raise ValueError(f"race {'-'.join(map(str, race.key))} is not final")
    entries = {e.athlete_id: e for e in race.entries}
    finishers = []
    for f in res.get("finishers", []):
        f = dict(f)
        if f.get("event") is None:
            a = printing._finisher_athlete(meet, f)
            e = entries.get(a.id) if a else None
            f["event"] = e.event if e else race.event
        finishers.append(f)
    labels = list(res.get("split_labels") or [])
    for f in finishers:                       # live dicts made before split_labels
        for s in f.get("splits") or []:
            if s.get("label") and s["label"] not in labels:
                labels.append(s["label"])
    # On course at the split: finishers and DNFs (a DNF ran until they
    # stopped); DQ'd runners are out of the results and DNS never ran.
    ran = [f for f in finishers if f.get("status") in (scoring.FINISHED, "DNF")]
    nums = sorted({f["event"] for f in finishers})
    sections = []
    for num in nums:
        field = [f for f in ran if f["event"] == num]
        splits = []
        for label in labels:
            timed, missing = [], []
            for i, f in enumerate(field):
                sp = _split_of(f, label)
                if sp is None:
                    missing.append(f.get("bib") or "")
                else:
                    timed.append((sp["elapsed"], f.get("place") or math.inf, i, f, sp))
            timed.sort(key=lambda x: x[:3])
            pseudo = [{"place": k, "bib": f.get("bib"), "athlete_id": f.get("athlete_id"),
                       "event": num, "elapsed": el, "status": scoring.FINISHED,
                       "event_place": k}
                      for k, (el, _, _, f, _) in enumerate(timed, 1)]
            rows = scoring.score_event(meet, pseudo, num, race=race)
            fins = {(num, p["athlete_id"]): p for p in pseudo}
            ranked, incomplete = _team_rows(meet, rows, fins, num)
            splits.append({"label": label, "timed": len(pseudo), "missing": missing,
                           "estimated": sum(1 for t in timed if t[4].get("estimated")),
                           "teams": ranked, "incomplete": incomplete})
        if splits:
            sections.append({"event": num, "event_name": printing._event_name(meet, num, race),
                             "splits": splits})
    return {"race": _key(race), "race_name": _race_title(meet, race),
            "official": bool(res.get("frozen_at")), "split_labels": labels,
            "sections": sections}


MISSING_SHOWN = 10


def split_scores_text(meet: Meet, race: Race, printed_at: Optional[datetime] = None, *,
                      results: Optional[dict] = None, breakdown: bool = False) -> str:
    """Split Scores: per scoring event, a block per split (team rank, score,
    scoring order, times), with who was left out for having no time there."""
    data = split_scores(meet, race, results)
    width = printing._width(TEAM_SCORE_COLS)
    head = _header(meet, "Split Scores", width, printed_at,
                   ["" if data["official"] else "UNOFFICIAL: race not final",
                    "Score Breakdown" if breakdown else ""])
    lines: List[str] = []
    for sec in data["sections"]:
        if lines:
            lines.append("")
        lines += [f"Event {sec['event']}  {sec['event_name']}"]
        for sp in sec["splits"]:
            note = f"{printing._plural(sp['timed'], 'runner')} timed"
            if sp["estimated"]:
                note += f", {sp['estimated']} estimated"
            lines += ["", f"Split: {sp['label']}  ({note})"]
            lines += _team_table_lines(sp["teams"], sp["incomplete"], breakdown)
            if not sp["teams"] and not sp["incomplete"]:
                lines.append("No team scores at this split.")
            if sp["missing"]:
                shown = ", ".join(sp["missing"][:MISSING_SHOWN])
                if len(sp["missing"]) > MISSING_SHOWN:
                    shown += f" and {len(sp['missing']) - MISSING_SHOWN} more"
                lines.append(f"No {sp['label']} time, left out: bib {shown}")
    if not data["sections"]:
        lines.append("No splits in this race.")
    return _body(head, [lines], "")
