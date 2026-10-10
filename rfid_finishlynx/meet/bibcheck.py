"""The bib check before a race (2026-10-10, user): do the bibs the runners
were handed, the meet's bibs and FinishLynx's start list still agree?

At the Jim Crawford meet the bibs printed on one PC and the bibs race day
used (another PC's separate import) were 1-4 numbers apart, so FinishLynx
put the right numbers on the wrong names. Meet sync now refuses such a
copy; this is the second line: anything that renumbers runners after the
bibs went out - a bulk renumber, a mistaken edit on another PC - shows up
before the gun instead of in the results.

What a runner was handed is Athlete.printed_bib, recorded when the rosters,
team bib lists or competitor labels are printed (for runners not recorded
yet) and kept up by the deliberate one-off changes at the table - typing a
spare bib for someone, a swap, a walk-up given a bib. A bulk renumber and
another PC's edit don't touch it, which is the point. "Accept" records the
current bibs as handed out.

Three things are compared:
  changed   a runner's bib is no longer the one printed for them
  lynx      FinishLynx's lynx.ppl names someone else for a bib than the
            runner wearing it (who was printed it, else who holds it now),
            or lists a bib nobody has
  missing   a runner wearing a bib that FinishLynx's lynx.ppl doesn't list
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from . import lynx
from .model import Athlete, Meet, bib_number

ENCODING = lynx.ENCODING


def _name(a: Athlete) -> str:
    return f"{a.first} {a.last}".strip()


def _key(last: str, first: str) -> Tuple[str, str]:
    """A name as lynx.ppl carries it, compared without case or spacing."""
    return (" ".join(lynx.clean(last).split()).lower(), " ".join(lynx.clean(first).split()).lower())


def read_ppl(path: str) -> Optional[Dict[int, Tuple[str, str, str]]]:
    """bib number -> (last, first, affiliation) from a lynx.ppl, None when
    there is no file."""
    try:
        with open(path, "rb") as f:
            text = f.read().decode(ENCODING, errors="replace")
    except OSError:
        return None
    out: Dict[int, Tuple[str, str, str]] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        parts = line.split(",")
        n = bib_number(parts[0]) if parts else None
        if n is None or len(parts) < 3:
            continue
        out.setdefault(n, (parts[1].strip(), parts[2].strip(), parts[3].strip() if len(parts) > 3 else ""))
    return out


def _team(meet: Meet, a: Athlete) -> str:
    t = meet.team(a.team_id)
    return (t.short or t.name) if t else ""


def _race_index(meet: Meet) -> Dict[str, List[str]]:
    """athlete id -> the races they run in (one pass: the check runs with
    every meet summary, on meets of 3,000 athletes)."""
    out: Dict[str, List[str]] = {}
    for r in meet.races:
        for e in r.entries:
            if e.status != "scratched":
                out.setdefault(e.athlete_id, []).append(f"{r.event}-{r.round}-{r.heat}")
    return out


def check(meet: Meet, ppl: Optional[Dict[int, Tuple[str, str, str]]] = None,
          ppl_path: str = "") -> dict:
    """{ok, recorded, changed: [...], lynx: {path, checked, wrong: [...],
    missing: [...]}, races: {rk: n}}. ppl: read_ppl() of the lynx.ppl
    FinishLynx reads (None: not checked - no file, or the meet doesn't
    write start lists)."""
    runs = _race_index(meet)
    runners = [a for a in meet.athletes if a.id in runs]
    changed = []
    for a in meet.athletes:
        if a.printed_bib and bib_number(a.printed_bib) != bib_number(a.bib):
            changed.append({"athlete_id": a.id, "name": _name(a), "team": _team(meet, a),
                            "printed": a.printed_bib, "now": a.bib, "races": runs.get(a.id, [])})
    # who wears each number: whoever was handed it, else whoever holds it now
    wearer: Dict[int, Athlete] = {}
    for a in meet.athletes:
        n = bib_number(a.printed_bib) if a.printed_bib else None
        if n is not None:
            wearer.setdefault(n, a)
    for a in meet.athletes:
        n = bib_number(a.bib)
        if n is not None and n not in wearer and not (a.printed_bib and bib_number(a.printed_bib) != n):
            wearer[n] = a
    wrong, missing = [], []
    if ppl is not None:
        for n, (last, first, team) in sorted(ppl.items()):
            a = wearer.get(n)
            if a is None:
                wrong.append({"bib": str(n), "lynx": f"{first} {last}".strip(), "lynx_team": team,
                              "wearer": "", "athlete_id": None, "races": []})
            elif _key(last, first) != _key(a.last, a.first):
                wrong.append({"bib": str(n), "lynx": f"{first} {last}".strip(), "lynx_team": team,
                              "wearer": _name(a), "athlete_id": a.id, "races": runs.get(a.id, [])})
        for n, a in sorted(wearer.items()):
            if n not in ppl and a.id in runs:
                missing.append({"bib": str(n), "wearer": _name(a), "athlete_id": a.id,
                                "races": runs.get(a.id, [])})
    races: Dict[str, int] = {}
    for row in changed + wrong + missing:
        for rk in row["races"]:
            races[rk] = races.get(rk, 0) + 1
    return {"ok": not (changed or wrong or missing),
            "recorded": sum(1 for a in runners if a.printed_bib), "runners": len(runners),
            "changed": changed,
            "lynx": {"path": ppl_path, "checked": ppl is not None, "wrong": wrong, "missing": missing},
            "races": races}


def record(meet: Meet, athlete_ids=None, *, overwrite: bool = False) -> int:
    """Note the current bibs as handed out: the given athletes (None = every
    runner in a race), only those not recorded yet unless overwrite (Accept).
    Returns how many changed."""
    want = set(athlete_ids) if athlete_ids is not None else set(_race_index(meet))
    n = 0
    for a in meet.athletes:
        if a.id not in want:
            continue
        if not a.bib or (a.printed_bib and not overwrite) or a.printed_bib == a.bib:
            continue
        a.printed_bib = a.bib
        n += 1
    return n


def handed_out(meet: Meet) -> bool:
    """Have any bibs gone out yet (printed or recorded)?"""
    return any(a.printed_bib for a in meet.athletes)


def follow(meet: Meet, before: Dict[str, str], athlete_ids) -> None:
    """A deliberate one-off change at the table: the runner now physically
    has the new number, so what was handed out follows it - for runners
    whose bibs were already handed out, and, once bibs have gone out, for a
    runner who had none (a spare given to a walk-up). Before anything was
    handed out, typing bibs is still assigning them: nothing is recorded."""
    out = handed_out(meet)
    for aid in athlete_ids:
        a = meet.athlete(aid)
        if a is None or not a.bib:
            continue
        if a.printed_bib or (out and not before.get(aid)):
            a.printed_bib = a.bib


def restore(meet: Meet, athlete_ids=None) -> dict:
    """Put the printed numbers back on the runners whose bib changed (the
    given ones, None = all of them). Someone else holding one of those
    numbers who wasn't handed it loses it (blank: assign or type them one);
    a printed number two runners claim is left alone. {restored: [{athlete_id,
    name, bib, was}], cleared: [{athlete_id, name, bib}], skipped: [...]}"""
    want = set(athlete_ids) if athlete_ids is not None else None
    targets = [a for a in meet.athletes if a.printed_bib and bib_number(a.printed_bib) != bib_number(a.bib)
               and (want is None or a.id in want)]
    claims: Dict[int, List[Athlete]] = {}
    for a in targets:
        claims.setdefault(bib_number(a.printed_bib), []).append(a)
    rep = {"restored": [], "cleared": [], "skipped": []}
    putting = {}
    for n, who in claims.items():
        if n is None or len(who) > 1:
            rep["skipped"] += [{"athlete_id": a.id, "name": _name(a), "bib": a.printed_bib} for a in who]
            continue
        putting[n] = who[0]
    for o in meet.athletes:
        n = bib_number(o.bib)
        if n in putting and putting[n] is not o:
            rep["cleared"].append({"athlete_id": o.id, "name": _name(o), "bib": o.bib})
            o.bib = ""
    for n, a in putting.items():
        rep["restored"].append({"athlete_id": a.id, "name": _name(a), "bib": a.printed_bib, "was": a.bib})
        a.bib = a.printed_bib
    return rep


def ppl_path(directory: str) -> str:
    """The lynx.ppl FinishLynx reads in `directory` (any case)."""
    try:
        for n in os.listdir(directory):
            if n.lower() == lynx.PPL_NAME.lower():
                return os.path.join(directory, n)
    except OSError:
        pass
    return os.path.join(directory, lynx.PPL_NAME)
