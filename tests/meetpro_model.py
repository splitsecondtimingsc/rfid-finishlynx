"""How MeetPro2 2.3.137 reads a FinishLynx .lif - a Python model for tests.

Transcribed from MeetPro.swf bytecode (read-only reverse engineering,
2026-09-22; full notes kept outside the repo because the raw dumps contain
the .dab key). Only behaviour seen in the bytecode is modelled:

  CSVParser.fieldDetection   split on ',' and re-join while the quote
                             count so far is odd; first line = header
  FATParser / XCFATParser    fixed columns 0-10; splits from field 10,
    .parseRow                quoted ('"c (l),c (l)"') or spilled unquoted
                             fields matching SPLIT_RE; EMPTY ENTRIES ARE
                             SKIPPED WITHOUT TAKING A SLOT
  XCResultsImportService     path A "Start Splits Auto-Pull": entry i ->
    .matchSplitArray         i-th configured split point, extras ignored,
                             write-once per (athlete, split point)
  MileSplitLiveXCSync-       path B "Start MS Live Sync" (the only path
    Service.updateSyncFound  that sends splits to MileSplit Live during a
                             race): skips heat > 1 files and rows without
                             bib or affiliation; drops a last entry within
                             0.5 s of the result time; caps at the
                             configured split count; positional

Anything the tests infer beyond this (e.g. what MileSplit Live shows) is
checked on the bench, not here.
"""
import re

FLAG_RE = re.compile(r"^\s*(DNS|DNF|FS|NT|ND|NH|NP|FOUL|DQ|NM|SCR|F)\s*$", re.I)
SPLIT_RE = re.compile(r"[0-9:.]+\s+\([0-9:.]+\)")          # unanchored, as in the SWF
LIF_NAME_RE = re.compile(r"(\d+)-(\d+)-(\d+)")
MS_LIVE_FILE_RE = re.compile(r".+?\.lif")                  # case-sensitive (path B)
FINISH_MATCH_TOLERANCE = 0.5


def units(t):
    """convertToUnits: 'H:MM:SS.fff' / 'M:SS.fff' / seconds -> float; '' -> 0."""
    t = (t or "").strip()
    if not t:
        return 0.0
    secs = 0.0
    try:
        for part in t.split(":"):
            secs = secs * 60 + float(part)
    except ValueError:
        return 0.0
    return secs


def fields(line):
    out, quotes = [], 0
    for i, piece in enumerate(line.split(",")):
        if quotes % 2 == 0:
            out.append(piece.strip())
        else:
            out[-1] += "," + piece
        quotes += piece.count('"')
    return out


def records(text):
    text = text.lstrip("﻿")
    return [r for r in re.split(r"[\r|\n]+", text) if r.strip()]


def _split_entries(raw):
    out = []
    for e in raw.replace('"', "").split(","):
        if len(e) == 0:
            continue                                  # no slot for an empty entry
        parts = e.split("(")
        total = parts[0].strip()
        lap = parts[1].replace(")", "").strip() if len(parts) > 1 else ""
        out.append({"total": total, "lap": lap})
    return out


def parse_row(f, xc=True):
    get = lambda i: f[i] if i < len(f) else ""
    row = {"flag": "", "hplace": 0}
    if FLAG_RE.match(get(0)):
        flag = get(0).strip().upper()
        row["flag"] = "DNS" if flag == "SCR" else flag
    else:
        try:
            row["hplace"] = int(get(0))
        except ValueError:
            pass
    try:
        row["comp"] = int(get(1))
    except ValueError:
        row["comp"] = 0
    row["last_name"] = get(3).strip('"')
    row["first_name"] = get(4)
    row["team_name"] = get(5).replace('"', "")
    row["performance"] = get(6)
    f10 = get(10)
    if '"' in f10:
        row["split_array"] = _split_entries(f10)
    elif SPLIT_RE.search(f10):
        spilled, i = [], 10
        while i < len(f) and SPLIT_RE.search(f[i]):
            spilled.append(f[i])
            i += 1
        row["split_array"] = _split_entries(",".join(spilled))
    else:
        row["split_array"] = []
    if xc:
        if not row["performance"] and not row["flag"]:
            row["flag"] = "ENR"
    elif not row["performance"] and row["split_array"] and not row["flag"]:
        row["flag"] = "ENR"
    return row


def is_valid(r):
    return bool((r["hplace"] or r["flag"] or r["performance"])
                and (r["first_name"] or r["last_name"] or r["team_name"] or r["comp"]))


def parse_lif(text, xc=True):
    recs = records(text)
    if not recs:
        return [], []
    rows = [parse_row(fields(r), xc) for r in recs[1:] if not fields(r)[0].startswith(";")]
    return fields(recs[0]), [r for r in rows if is_valid(r)]


# --------------------------------------------------------------- path A

def splits_auto_pull(text, split_points, stored=None):
    """Local import. split_points: names in split_order. stored: existing
    {(bib, point): total} - never overwritten. Returns {bib: {point: total}}
    plus each bib's status under key '_status'."""
    stored = dict(stored or {})
    _, rows = parse_lif(text, xc=True)
    out = {}
    for r in rows:
        bib = r["comp"]
        got = out.setdefault(bib, {"_status": r["flag"] or "OK"})
        for i, e in enumerate(r["split_array"]):
            if i >= len(split_points):
                continue
            point = split_points[i]
            if (bib, point) in stored:
                got[point] = stored[(bib, point)]
                continue
            stored[(bib, point)] = got[point] = e["total"]
    return out


# --------------------------------------------------------------- path B

def ms_live_accepts_file(name):
    m = LIF_NAME_RE.search(name)
    return bool(MS_LIVE_FILE_RE.fullmatch(name) and m and int(m.group(3)) <= 1)


def ms_live_sync(name, text, split_points, team_names):
    """What one changed file sends to MileSplit Live: {bib: {'place',
    'result', 'splits': {n: cumulative}}}; skipped rows are absent."""
    if not ms_live_accepts_file(name):
        return {}
    _, rows = parse_lif(text, xc=False)
    out, prev_place = {}, 0
    for r in rows:
        if not r["comp"] or not r["team_name"]:
            continue
        place = r["hplace"]
        if place <= prev_place:
            place = prev_place + 1
        prev_place = place
        arr = r["split_array"]
        count = len(arr)
        if count and r["performance"]:
            last = arr[-1]["total"]
            if last == r["performance"] or abs(units(last) - units(r["performance"])) < FINISH_MATCH_TOLERANCE:
                count -= 1
        count = min(count, len(split_points))
        out[r["comp"]] = {
            "place": place, "result": r["performance"], "status": r["flag"] or "OK",
            "team_scored": r["team_name"].strip() in team_names,
            "splits": {split_points[n]: arr[n]["total"] for n in range(count)},
        }
    return out
