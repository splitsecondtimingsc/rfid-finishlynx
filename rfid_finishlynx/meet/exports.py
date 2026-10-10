"""Results out of the meet model: MileSplit mpResults payload (+ Hy-Tek semicolon).

mpResults is MileSplit's results-upload endpoint (the one MeetPro's "Upload
Results" posts to), not a file format. The payload rules were worked out in
~/milesplit-probe (dab_to_mpresults.py, a line-for-line port of MeetPro
2.3.139's MileSplitClient) and proven at Woodmont 2026-10-03, where five
uploads were accepted. This module builds the same document from the meet
model's frozen results instead of MeetPro's SQLite (2026-10-03):

- one team object per team with results, its roster = the athletes with a
  result; null fields are left out except the team's milesplit_team_id.
- a finisher sends place + mark (seconds) + fat 1; a non-finisher sends
  status DNF/DQ(/DNS) + fat 0 and no place or mark. score = the runner's
  team-scoring place, only for scorers and displacers.
- events that share gender + distance would merge into one race on
  MileSplit, so each gets a synthetic division (MeetPro's
  separateSharedEvents). Unlike MeetPro we decide that over every event of
  the meet, not just the ones in this upload, so a JV-only upload carries
  the same label the final full upload will (the probe had to build the
  whole meet and filter afterwards for the same reason).
- MileSplit refuses an upload unless at least 50% of athletes AND teams carry
  MileSplit ids (HTTP 201 with status "error" in the body); id_coverage() and
  mp_results_problems() catch that before anything is sent.

Pure functions only: no network here (milesplit.py sends).

Frozen results contract (race.results, written by scoring.freeze):
    {"frozen_at": iso, "finishers": [{place, bib, athlete_id, event, elapsed,
      status (finished|DNF|DNS|DQ), event_place, points, scorer, displacer,
      fl_only}], "teams": {"<event num>": [{rank, team_id, score, complete,
      count, runners: [...], tiebreak_points, total_time, avg_time}]}}
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from typing import Dict, List, Optional, Tuple

from .model import Athlete, Event, Meet, Race, Team

DIV_NAME_LEN = 32          # MileSplitClient.EVENT_DIVISION_NAME_LENGTH
DIV_ID_BASE = 1000000      # MileSplitClient.EVENT_DIVISION_ID_BASE
MILE_M = 1609.344
NON_FINISH = ("DNF", "DNS", "DQ")
UNATTACHED_TEAM = {"name": "Unattached", "code": "UNAT"}


# --------------------------------------------------------------- times

def hundredths(sec) -> float:
    """Seconds rounded UP to the next hundredth, as FinishLynx rounds a FAT
    time (2665.642 -> 2665.65). Thousandths are snapped first so float noise
    (983.52 stored as 983.5200000001) doesn't round up a clean time. The
    probe's MeetPro file kept raw thousandths and Live then read 0.01 s off
    the official results (MSLIVE.md); one helper for both sends avoids that
    (2026-10-03)."""
    ms = round(float(sec) * 1000)
    cs = -(-ms // 10)
    return cs / 100


def fmt_time(sec) -> str:
    """16:23.52 / 1:02:03.45 (report lines; Live text is milesplit.perf_text)."""
    cs = round(float(sec) * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s = f"{rem // 100:02d}.{rem % 100:02d}"
    return f"{h}:{m:02d}:{s}" if h else f"{m}:{s}"


# --------------------------------------------------------------- event codes

def _num(v) -> str:
    """AS3 String(Number): 5000.0 -> "5000", 2.5 -> "2.5"."""
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


def meetpro_event(ev: Event) -> dict:
    """The model event as MeetPro's {code: run|mile, distance, metric}, which
    is what milesplit_code() / tfrrs_code() were ported against. A code like
    "2MILE" / "1.5 Mile", or a distance within 2 m of a whole/half mile,
    counts as a mile race; anything else is metres."""
    if ev.kind == "relay":
        raise ValueError(f"event {ev.num} is a relay: relays are not ported")
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:mile|miles|mi)\s*", ev.code or "", re.I)
    if m:
        return {"code": "mile", "distance": float(m.group(1)), "metric": False}
    d = ev.distance_m or (int(ev.code) if (ev.code or "").isdigit() else 0)
    for n in (1, 1.5, 2, 2.5, 3, 4):
        if d and abs(d - n * MILE_M) <= 2:
            return {"code": "mile", "distance": n, "metric": False}
    return {"code": "run", "distance": d, "metric": True}


def tfrrs_code(ev: dict) -> str:                      # MeetPro displayTFRRSEventCode
    code, d, metric = ev["code"], ev["distance"], ev["metric"]
    if code == "walk":
        return f"{_num(d)}rw" if metric else ("1milerw" if d == 1 else f"{_num(d)}yrw")
    if code == "run":
        return _num(d) if metric else f"{_num(d)}y"
    if code == "mile":
        return f"{_num(d)}mile"
    raise ValueError(f"event code {code!r} is not ported (XC run/mile/walk only)")


def milesplit_code(ev: dict) -> str:                  # MeetPro displayMileSplitEventCode
    code, d, metric = ev["code"], ev["distance"], ev["metric"]
    if (code == "mile" and d == 1.5) or (code == "run" and metric and d == 2414):
        return "1.5Mile"
    if code == "mile":
        if 3 < d <= 3.05:
            d = 3
        elif 3.05 < d < 3.2:
            code, d, metric = "run", 5000, True
        elif 6.1 < d < 6.4:
            code, d, metric = "run", 10000, True
    if code == "walk":
        s = f"{_num(d)}RW" if metric else ("MileRW" if d == 1 else f"{_num(d)}RW")
    elif code == "run":
        s = f"{_num(d)}m" if metric else f"{_num(d)}Y"
    elif code == "mile":
        s = "Mile" if d == 1 else f"{_num(d)}Mile"
    else:
        raise ValueError(f"event code {code!r} is not ported (XC run/mile/walk only)")
    if code == "mile" and d < 6 and float(d) % 1 != 0:
        s = f"{int(d)}.xMile" + ("5.x Mile Run" if int(d) == 5 else "")
    return s


def event_codes(ev: Event) -> Tuple[str, str]:
    """(code, milesplit_code) as the payload's event object carries them:
    ("5000", "5000m") for an XC 5K."""
    mp = meetpro_event(ev)
    return tfrrs_code(mp), milesplit_code(mp)


# --------------------------------------------------------------- divisions

def _trim(s: str) -> str:
    return s.strip(" \t\r\n")


def unique_division_name(name: str, used: dict) -> str:
    """MeetPro: cut to 32 chars, then " 2", " 3"... until unused (case-insensitive)."""
    n = _trim(name[:DIV_NAME_LEN])
    i = 2
    while used.get(n.lower()):
        suffix = f" {i}"
        i += 1
        n = _trim(name[:DIV_NAME_LEN - len(suffix)]) + suffix
    used[n.lower()] = True
    return n


def division_id_for(name: str, used: dict) -> int:
    """MeetPro's synthetic division id: djb2-xor over UTF-16 units, + 1e6,
    bumped past ids another name already holds. Woodmont's "JV Boys" went up
    as 289275230, which the tests pin."""
    h = 5381
    units = name.encode("utf-16-le")
    for i in range(0, len(units), 2):                  # AS3 charCodeAt = UTF-16 units
        h = ((h * 33) & 0xFFFFFFFF) ^ int.from_bytes(units[i:i + 2], "little")
    did = DIV_ID_BASE + h % 1000000000
    while used.get(did) and used[did] != name:
        did += 1
    used[did] = name
    return did


def events_in_use(meet: Meet) -> List[Event]:
    """Scoring events somebody is entered in (not scratched) or has a frozen
    result in, in event-number order."""
    nums = set()
    for r in meet.races:
        nums.update(e.event for e in r.entries if e.status != "scratched")
        for f in (r.results or {}).get("finishers", []):
            nums.add(f.get("event") or r.event)
    return sorted((e for e in meet.events if e.num in nums), key=lambda e: e.num)


def division_labels(meet: Meet) -> Dict[int, Tuple[int, str]]:
    """event num -> (division_id, division_name) for every event that shares
    gender + MileSplit distance with another event of the meet; such races
    would merge into one on MileSplit (MeetPro separateSharedEvents). The
    label is the event's MileSplit division name ("JV Boys") when it has one,
    else its name, so results land under the labels the meet's own MileSplit
    entries used. Events alone in their gender+distance get no division, as
    MeetPro sends them (2026-10-03)."""
    groups: Dict[Tuple[str, str], List[Event]] = collections.OrderedDict()
    for ev in events_in_use(meet):
        try:
            key = ((ev.gender or "").lower(), event_codes(ev)[1])
        except ValueError:
            continue
        groups.setdefault(key, []).append(ev)
    out: Dict[int, Tuple[int, str]] = {}
    ids: dict = {}
    for evs in groups.values():
        if len(evs) < 2:
            continue
        used: dict = {}
        for ev in evs:
            name = unique_division_name(ev.division or ev.name or f"Event {ev.num}", used)
            out[ev.num] = (division_id_for(name, ids), name)
    return out


def apply_division_places(results: List[dict]) -> None:
    """MeetPro applyDivisionPlaces: renumber places 1..n within one group,
    ties keeping a shared place. Used where a result had no event_place and
    fell back to its overall place in a combined start."""
    rs = sorted((r for r in results if r.get("place")), key=lambda r: int(r["place"]))
    nxt, prev_new, prev_old = 1, 0, 0
    for r in rs:
        if r["place"] == prev_old:
            r["place"] = prev_new
        else:
            prev_old = int(r["place"])
            r["place"] = nxt
        prev_new = int(r["place"])
        nxt += 1


# --------------------------------------------------------------- helpers

def _drop_nulls(d: dict) -> dict:
    for k in [k for k, v in d.items() if v is None]:
        del d[k]
    return d


def as3_numbers(obj):
    """AS3 JSON.stringify writes 18.0 as 18; MeetPro's payloads look like that."""
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: as3_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [as3_numbers(v) for v in obj]
    return obj


def int_id(our_id: Optional[str], default: int = 0) -> int:
    """'a12' -> 12: MeetPro sent integer internal ids; ours are prefixed
    strings that are never reused, so their number is stable too."""
    m = re.search(r"(\d+)$", our_id or "")
    return int(m.group(1)) if m else default


def bib_value(bib: str):
    """Bibs go up as numbers, as MeetPro's athlete.comp did."""
    bib = str(bib or "").strip()
    return int(bib) if bib.isdigit() else (bib or None)


def race_keys(races) -> Optional[set]:
    """Race objects, (event, round, heat) tuples or bare event numbers -> keys."""
    if races is None:
        return None
    keys = set()
    for r in races:
        if isinstance(r, Race):
            keys.add(r.key)
        elif isinstance(r, int):
            keys.add((r, None, None))
        else:
            keys.add(tuple(r))
    return keys


def selected_races(meet: Meet, races=None) -> List[Race]:
    keys = race_keys(races)
    out = []
    for r in meet.races_in_order():
        if keys is None or r.key in keys or (r.event, None, None) in keys:
            out.append(r)
    return out


def is_official(race: Race) -> bool:
    """Results count as official only while the race is final. Reopening a
    race keeps its last frozen results but sets it back to "scheduled", so
    `race.results is not None` alone would upload a race being re-done
    (2026-10-03, scoring contract)."""
    return race.status == "final" and bool(race.results)


def ms_ids_usable(a: Athlete) -> bool:
    """An athlete in the id review queue (or declined) goes up as a
    write-in: nothing is guessed silently (plan: idmatch.py)."""
    return a.ms_match not in ("pending", "declined")


def finisher_rows(meet: Meet, race: Race, results: Optional[dict] = None):
    """(finisher, athlete or None, entry or None, scoring event num) for a
    race's results (frozen unless given), in results order."""
    res = results if results is not None else race.results
    if not res:
        return []
    entries = {e.athlete_id: e for e in race.entries}
    out = []
    for f in res.get("finishers", []):
        a = meet.athlete(f.get("athlete_id")) if f.get("athlete_id") else None
        if a is None and f.get("bib"):
            a = meet.athlete_by_bib(f["bib"])
        e = entries.get(a.id) if a else None
        ev = f.get("event") or (e.event if e else None) or race.event
        out.append((f, a, e, int(ev)))
    return out


def is_scored(f: dict) -> bool:
    return bool(f.get("scorer") or f.get("displacer")) and f.get("points") is not None


# --------------------------------------------------------------- mpResults

def mp_results(meet: Meet, races=None, include_dns: bool = False) -> dict:
    """The mpResults upload document for the given races (default: every
    final race). Only final races go in (is_official). DNS rows are left out by default: a frozen
    race lists every no-show, and MeetPro only ever sent the DNS an
    operator typed in (2026-10-03)."""
    labels = division_labels(meet)
    teams: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
    athletes: Dict[str, dict] = {}
    renumber: set = set()                 # events where a place fell back to the race place
    per_event: Dict[int, List[dict]] = collections.defaultdict(list)

    for race in selected_races(meet, races):
        if not is_official(race):
            continue
        for f, a, e, ev_num in finisher_rows(meet, race):
            if a is None:
                continue                      # unknown bib: mp_results_problems says so
            status = f.get("status") or "finished"
            if status == "DNS" and not include_dns:
                continue
            ev = meet.event(ev_num)
            if ev is None:
                continue
            code, ms_code = event_codes(ev)
            finished = status == "finished" and f.get("elapsed") is not None
            exhibition = bool(e and e.status == "exhibition")
            place = f.get("event_place")
            if finished and place is None:
                place = f.get("place")
                renumber.add(ev_num)
            res = {"place": place if finished else None,
                   "score": int(f["points"]) if is_scored(f) and not exhibition else None}
            if finished:
                res["mark"] = hundredths(f["elapsed"])
            else:
                res["status"] = status if status in NON_FINISH else "DNF"
            res["fat"] = 1 if finished else 0
            res["event"] = _drop_nulls({"code": code, "milesplit_code": ms_code,
                                        "gender": (ev.gender or "").lower(), "event_id": ev.num,
                                        "name": ev.name or None, "event_num": ev.num})
            if ev_num in labels:
                res["division_id"], res["division_name"] = labels[ev_num]
            _drop_nulls(res)
            per_event[ev_num].append(res)

            tkey = a.team_id or ""
            if tkey not in teams:
                t = meet.team(a.team_id)
                teams[tkey] = ({"name": t.name, "code": t.code or (t.short or t.name)[:4].upper(),
                                "milesplit_team_id": t.milesplit_id, "relays": [], "roster": []}
                               if t else {**UNATTACHED_TEAM, "milesplit_team_id": None,
                                          "relays": [], "roster": []})
            ath = athletes.get(a.id)
            if ath is None:
                use_ids = ms_ids_usable(a)
                ath = _drop_nulls({
                    "bib": bib_value(a.bib), "year": meet.grade(a) or None,
                    "first_name": a.first, "last_name": a.last, "gender": a.gender.lower(),
                    "athlete_id": int_id(a.id),
                    "milesplit_universal_id": (a.universal_id or None) if use_ids else None,
                    "results": [],
                    "milesplit_athlete_id": (str(a.milesplit_id) if a.milesplit_id else None)
                    if use_ids else None})
                athletes[a.id] = ath
                teams[tkey]["roster"].append(ath)
            ath["results"].append(res)

    for ev_num in renumber:
        apply_division_places([r for r in per_event[ev_num] if "mark" in r])

    ordered = sorted(teams.values(), key=lambda t: ((t["code"] or "").upper(), t["name"]))
    return as3_numbers({"meet_id": meet.meet.milesplit.meet_id or 0,
                        "teams": [t for t in ordered if t["roster"]]})


def id_coverage(payload: dict) -> dict:
    """MileSplit's rule: at least 50% of athletes AND teams carry MileSplit ids."""
    athletes = [a for t in payload.get("teams", []) for a in t["roster"]]
    teams = payload.get("teams", [])
    a_ids = sum(1 for a in athletes if a.get("milesplit_athlete_id"))
    t_ids = sum(1 for t in teams if t.get("milesplit_team_id"))
    return {"athletes": len(athletes), "athletes_with_id": a_ids,
            "teams": len(teams), "teams_with_id": t_ids,
            "write_ins": len(athletes) - a_ids,
            "ok": bool(athletes) and a_ids * 2 >= len(athletes) and t_ids * 2 >= len(teams)}


def fingerprint(payload: dict) -> str:
    """Content hash of a payload, ignoring "_" bookkeeping keys and key order,
    so a rebuild that changes nothing is recognisably the same."""
    body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _race_label(r: dict) -> str:
    ev = r["event"]
    return r.get("division_name") or ev.get("name") or f"Event {ev.get('event_num')}"


def race_summary(payload: dict) -> List[dict]:
    """Per race in the payload: label, finishers, DNF/DNS/DQ counts, winner time."""
    races: "collections.OrderedDict[tuple, list]" = collections.OrderedDict()
    for t in payload.get("teams", []):
        for a in t["roster"]:
            for r in a["results"]:
                races.setdefault((r["event"].get("event_num") or 0, _race_label(r)), []).append(r)
    out = []
    for (num, label), rs in sorted(races.items()):
        fin = [r for r in rs if "mark" in r]
        flags = collections.Counter(r.get("status") for r in rs if "mark" not in r)
        best = min((r["mark"] for r in fin), default=None)
        out.append({"event_num": num, "label": label, "finishers": len(fin),
                    "DNF": flags["DNF"], "DNS": flags["DNS"], "DQ": flags["DQ"],
                    "winner": fmt_time(best) if best is not None else "",
                    "places": sorted(r["place"] for r in fin if r.get("place"))})
    return out


def summary_line(payload: dict) -> str:
    """One line for the console/log: '<race>: 287 timed +2 DNF/DNS/DQ ... [ids ...]'."""
    cov = id_coverage(payload)
    parts = []
    for s in race_summary(payload):
        flags = s["DNF"] + s["DNS"] + s["DQ"]
        parts.append(f"{s['label']}: {s['finishers']} timed" + (f" +{flags} DNF/DNS/DQ" if flags else ""))
    return (f"{', '.join(parts) or 'no results yet'}  [ids: athletes {cov['athletes_with_id']}/"
            f"{cov['athletes']}, teams {cov['teams_with_id']}/{cov['teams']}]")


def mp_results_problems(meet: Meet, races=None, payload: Optional[dict] = None) -> List[str]:
    """Everything that should stop (or be looked at before) an upload, one
    readable line each; [] = ready. Mirrors the probe's report() checks plus
    what the meet model can tell us (unfrozen races, unknown bibs, id review
    queue)."""
    problems: List[str] = []
    if payload is None:
        payload = mp_results(meet, races)
    ms = meet.meet.milesplit
    if not ms.meet_id:
        problems.append("meet is not linked to a MileSplit meet (no MileSplit meet id)")
    if not ms.tc_id:
        problems.append("no MileSplit timing company set for this meet")

    chosen = selected_races(meet, races)
    if races is not None and not chosen:
        problems.append("none of the chosen races are in this meet")
    for r in chosen:
        tag = r.name or (meet.event(r.event).name if meet.event(r.event) else f"race {r.event}")
        if not is_official(r):
            if races is not None:
                problems.append(f"{tag}: not final" + (" (reopened; left out until final again)"
                                                       if r.results else " yet"))
            elif r.results:
                problems.append(f"{tag}: reopened, left out until it is final again")
            continue
        unknown = sorted({str(f.get("bib")) for f, a, _e, _n in finisher_rows(meet, r) if a is None},
                         key=lambda b: (len(b), b))
        if unknown:
            problems.append(f"{tag}: finisher bib(s) not in the meet, left out: {', '.join(unknown)}")
        missing_ev = sorted({n for f, a, _e, n in finisher_rows(meet, r) if a and not meet.event(n)})
        if missing_ev:
            problems.append(f"{tag}: results score in unknown event(s) {missing_ev}")

    teams = payload.get("teams", [])
    if not teams:
        problems.append("no results to send")
        return problems

    for s in race_summary(payload):
        places = s["places"]
        if places and places[0] != 1:
            problems.append(f"{s['label']}: places start at {places[0]}, not 1")
        if len(places) != s["finishers"]:
            problems.append(f"{s['label']}: {s['finishers'] - len(places)} finisher(s) with a time but no place")

    # Two events in the payload that would land in one race on MileSplit.
    seen: Dict[tuple, set] = collections.defaultdict(set)
    for t in teams:
        for a in t["roster"]:
            for r in a["results"]:
                ev = r["event"]
                seen[(ev.get("gender"), ev.get("milesplit_code"), r.get("division_name"))].add(
                    ev.get("event_num"))
    for (g, code, div), nums in seen.items():
        if len(nums) > 1:
            problems.append(f"events {sorted(nums)} ({g} {code}{', ' + div if div else ''}) "
                            "would merge into one race on MileSplit")

    pending = sorted({a.id for a in meet.athletes if a.ms_match == "pending"}
                     & {a.id for r in chosen for _f, a, _e, _n in finisher_rows(meet, r) if a})
    if pending:
        problems.append(f"{len(pending)} athlete(s) with results are still in the MileSplit id "
                        "review queue (they go up as write-ins until approved)")

    cov = id_coverage(payload)
    if not cov["ok"]:
        problems.append(
            f"under MileSplit's 'at least 50% of athletes and teams must have valid IDs' rule: "
            f"athletes {cov['athletes_with_id']}/{cov['athletes']}, teams "
            f"{cov['teams_with_id']}/{cov['teams']}")
    if payload.get("meet_id") and ms.meet_id and payload["meet_id"] != ms.meet_id:
        problems.append(f"payload meet id {payload['meet_id']} is not the meet's ({ms.meet_id})")
    return problems


# --------------------------------------------------------------- Hy-Tek

def _mmddyyyy(iso: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else ""


def hytek_results(meet: Meet, races=None, include_dns: bool = False) -> str:
    """Hy-Tek semicolon results (H + one 33-field E record per result), the
    layout from hytek.active.com "Export Results in Semicolon Delimited
    Format" (MEET-RESEARCH.md §1). Not yet checked against a real Hy-Tek
    import (2026-10-03): field meanings follow the published list; XC
    results go up as T(rack) running events, Mark is the time text, Measure
    blank (only field events use M/E)."""
    lines = [";".join(["H", meet.meet.name, _mmddyyyy(meet.meet.date), _mmddyyyy(meet.meet.date),
                       "rfid-meet"])]
    labels = division_labels(meet)
    for race in selected_races(meet, races):
        if not is_official(race):
            continue
        for f, a, e, ev_num in finisher_rows(meet, race):
            ev = meet.event(ev_num)
            if a is None or ev is None:
                continue
            status = f.get("status") or "finished"
            if status == "DNS" and not include_dns:
                continue
            finished = status == "finished" and f.get("elapsed") is not None
            t: Optional[Team] = meet.team(a.team_id)
            exhibition = bool(e and e.status == "exhibition")
            div_id, div_name = labels.get(ev_num, ("", ev.division))
            fields_ = [
                "E", "T", str(ev.num), "", event_codes(ev)[0], ev.gender or "", "",
                str(div_id) if div_id else "", div_name or "", "F",
                fmt_time(hundredths(f["elapsed"])) if finished else status, "", "",
                str(f.get("event_place") or f.get("place") or "") if finished else "",
                str(f.get("place") or "") if finished else "", str(race.heat),
                "Y" if exhibition else "",
                str(int(f["points"])) if is_scored(f) and not exhibition else "", "", "", "", "",
                a.last, a.first, "", a.gender, a.dob or "",
                (t.code if t else "") or "", (t.name if t else "Unattached"), "",
                meet.grade(a), a.bib, ""]
            lines.append(";".join(x.replace(";", ",") for x in fields_))
    return "\r\n".join(lines) + "\r\n"
