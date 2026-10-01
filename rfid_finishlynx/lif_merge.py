"""The merged .lif MeetPro reads: FinishLynx's results plus RFID splits.

Folder layout (fixed, configured once):

    <Lynx results>\\             FinishLynx saves EEE-R-HH.lif, .evn, images
    <Lynx results>\\RFID\\       MeetPro's F.A.T. directory: MeetPro writes
                                lynx.evt/ppl/sch here (FinishLynx's input
                                directory), the console writes the merged
                                EEE-R-HH.lif here, MeetPro reads them

Only the console writes .lif files in RFID\\, so nothing fights over a file.
The merged file is rebuilt from scratch every time from FinishLynx's current
file and the race state - never patched - so a restart recovers by itself.

Merge rules, per athlete (matched by bib):
  place/time/status/names  FinishLynx's row when it has one (film time is
                           official); otherwise the chip finish is
                           published until FinishLynx places them
  splits (field 11)        RFID only, split points only - never the finish
                           - in course order, stopping at the first missing
                           point: MeetPro maps split entries BY POSITION and
                           skips empty ones, so a gap would file Mile 2
                           under Mile 1 (seen in MeetPro 2.3.137)
  on course                a row with blank place and time plus the splits
                           so far (MeetPro imports it as ENR), so Mile 1
                           shows while the race is running
  time base                every split is measured from FinishLynx's start
                           (header field 11) when its file exists, else the
                           pane's gun - splits and finish share one gun
Fields are written with a CSV writer, so a comma in a school or a name is
quoted instead of shifting every later column.
"""

import csv
import io
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .film_offset import parse_race_time, parse_tod
from .lif_generator import _format_race_time

logger = logging.getLogger(__name__)

ROW_FIELDS = 19
SPLITS = 10
FLAG_RE = re.compile(r"^\s*(DNS|DNF|FS|NT|ND|NH|NP|FOUL|DQ|NM|SCR|F)\s*$", re.I)
ISO = "%Y-%m-%dT%H:%M:%S.%f"
ENCODING = "cp1252"          # FinishLynx's ANSI files; MeetPro reads the system charset


# ------------------------------------------------------------------ folders

def meet_dirs(meet_dir: str, lif_dir: str = "", subdir: str = "RFID") -> Tuple[str, str]:
    """(FinishLynx results folder, RFID folder). The meet folder may be
    either the Lynx results folder or its RFID subfolder itself."""
    base = lif_dir or meet_dir or ""
    if not base:
        return "", ""
    base = os.path.normpath(base)
    sub = subdir or "RFID"
    if os.path.basename(base).lower() == sub.lower():
        return os.path.dirname(base), base
    return base, os.path.join(base, sub)


def start_list_dir(meet_dir: str, subdir: str = "RFID") -> str:
    """Where lynx.evt / lynx.ppl live: the RFID subfolder once MeetPro
    writes there, else the meet folder itself (older setups)."""
    if not meet_dir:
        return meet_dir
    lynx, rfid = meet_dirs(meet_dir, "", subdir)
    for d in (rfid, lynx):
        try:
            names = {n.lower() for n in os.listdir(d)}
        except OSError:
            continue
        if "lynx.evt" in names or "lynx.ppl" in names:
            return d
    return meet_dir


def wipe_merged_lifs(rfid_dir: str, archive_dir: Optional[str] = None) -> List[str]:
    """Close meet: copy the console's merged .lif files into the archive
    (when given), then delete them from the RFID folder. MeetPro's
    lynx.evt/ppl/sch there are not ours and are left alone."""
    removed = []
    if not rfid_dir or not os.path.isdir(rfid_dir):
        return removed
    names = [n for n in os.listdir(rfid_dir) if n.lower().endswith((".lif", ".lif.tmp"))]
    if archive_dir and names:
        dest = os.path.join(archive_dir, "rfid-lif")
        os.makedirs(dest, exist_ok=True)
        for n in names:
            if n.lower().endswith(".lif"):
                shutil.copy2(os.path.join(rfid_dir, n), os.path.join(dest, n))
    for n in names:
        os.remove(os.path.join(rfid_dir, n))
        removed.append(n)
    return removed


# ------------------------------------------------------------------- parsing

def _sod(iso: str) -> float:
    ts = datetime.strptime(iso, ISO)
    return ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1e6


def _clock(sec: float) -> str:
    sec %= 86400
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def parse_lif(text: str) -> Tuple[Optional[List[str]], List[List[str]]]:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None, []
    rows = list(csv.reader(lines))
    return rows[0], rows[1:]


def looks_complete(text: str) -> bool:
    """A FinishLynx file caught mid-write ends without a newline or has no
    usable header; keep the last good copy instead."""
    if not text or not text.endswith(("\n", "\r")):
        return False
    header, _ = parse_lif(text)
    return bool(header) and len(header) >= 3


# -------------------------------------------------------------------- merge

def leading_splits(row: dict, columns: Sequence[str], base: Optional[float]) -> str:
    """'c1 (l1),c2 (l2)' for the split points this athlete has without a
    gap, measured from `base` (seconds of day)."""
    if base is None:
        return ""
    first: Dict[str, dict] = {}
    for s in row.get("splits", []):
        first.setdefault(s["point"], s)
    out, prev = [], 0.0
    for point in columns:
        s = first.get(point)
        if s is None:
            break
        cum = _sod(s["clock"]) - base
        if cum <= 0 or cum < prev:
            break
        out.append(f"{_format_race_time(cum)} ({_format_race_time(cum - prev)})")
        prev = cum
    return ",".join(out)


def fl_bib_status(fl_text: Optional[str]) -> Tuple[set, set]:
    """(untimed, decided) bibs in FinishLynx's file: untimed = listed with
    neither a time nor a flag (entered, not placed); decided = placed (a
    time) or flagged (DNF, DQ, ...) - FinishLynx's word is final for those."""
    untimed, decided = set(), set()
    if not fl_text:
        return untimed, decided
    _, rows = parse_lif(fl_text)
    for row in rows:
        row = list(row) + [""] * max(0, ROW_FIELDS - len(row))
        bib = row[1].strip()
        if not bib:
            continue
        if parse_race_time(row[6]) is None and not FLAG_RE.match(row[0] or ""):
            untimed.add(bib)
        else:
            decided.add(bib)
    return untimed, decided


def fl_timed(fl_text: Optional[str]) -> Dict[str, dict]:
    """bib -> {time (race seconds), last_name, first_name, school} for every
    athlete FinishLynx has placed with a time (not flagged DNF/DQ/...)."""
    out: Dict[str, dict] = {}
    if not fl_text:
        return out
    _, rows = parse_lif(fl_text)
    for row in rows:
        row = list(row) + [""] * max(0, ROW_FIELDS - len(row))
        bib, t = row[1].strip(), parse_race_time(row[6])
        if bib and t is not None and not FLAG_RE.match(row[0] or ""):
            out[bib] = {"time": t, "last_name": row[3].strip(), "first_name": row[4].strip(),
                        "school": row[5].strip()}
    return out


def untimed_bibs(fl_text: Optional[str]) -> set:
    """Bibs FinishLynx lists untimed (see fl_bib_status)."""
    return fl_bib_status(fl_text)[0]


def merge_lif(event: int, round_: int, heat: int, name: str, distance: str,
              gun: Optional[datetime], results: dict, columns: Sequence[str],
              fl_text: Optional[str] = None, splits: bool = True,
              publish_chip: Optional[set] = None, hold_chip: Optional[set] = None) -> str:
    """The merged file. `results` is RaceManager.results() (finishers with
    film-corrected chip finishes, on-course athletes); `columns` the split
    points in course order; `fl_text` FinishLynx's own file for the event,
    if any; `publish_chip` bibs FinishLynx lists untimed whose chip finish
    the operator released ("Save to .lif" in the pane) - never automatic,
    since a stray read at the mat (a DNF walking past) must not become a
    result (user's rule, 2026-09-30); `hold_chip` finishers the race's
    start list (.evt entries or check-in) does not name - their chip time
    waits for "Save to .lif" too (in `publish_chip` once released)."""
    publish_chip = publish_chip or set()
    hold_chip = hold_chip or set()
    fl_header, fl_rows = parse_lif(fl_text) if fl_text else (None, [])
    base = parse_tod(fl_header[10]) if fl_header and len(fl_header) > 10 else None
    if base is None and gun is not None:
        base = gun.hour * 3600 + gun.minute * 60 + gun.second + gun.microsecond / 1e6
    ours = {r["bib"]: r for r in results.get("finishers", []) + results.get("on_course", [])}
    split_field = (lambda r: leading_splits(r, columns, base)) if splits else (lambda r: "")

    finishers: List[Tuple[float, List[str], bool]] = []     # (time, fields, from FinishLynx)
    trailing: List[List[str]] = []                          # flagged / timeless FinishLynx rows
    in_fl = set()
    decided = set()        # FinishLynx placed them (a time) or flagged them (DNF, DQ, ...)
    for row in fl_rows:
        row = list(row) + [""] * max(0, ROW_FIELDS - len(row))
        bib = row[1].strip()
        if bib:
            in_fl.add(bib)
        row[SPLITS] = split_field(ours[bib]) if bib in ours else ""
        t = parse_race_time(row[6])
        flagged = bool(FLAG_RE.match(row[0] or ""))
        if bib and (t is not None or flagged):
            decided.add(bib)
        if t is not None and not flagged:
            finishers.append((t, row, True))
        else:
            trailing.append(row)
    fl_names = {row[1].strip(): row for row in trailing if row[1].strip()}

    # FinishLynx's row wins - including an entered athlete it has not timed
    # (empty row) - unless the operator released that athlete's chip finish
    # (2026-09-30: a Men's straggler finished after the event was done in
    # FinishLynx; the pane flags him and "Save to .lif" puts him in).
    chip = set()
    for r in results.get("finishers", []):
        if not r.get("finish") or r["bib"] in decided or r["finish"].get("fl_only"):
            continue                     # FinishLynx's own row carries those
        if (r["bib"] in in_fl or r["bib"] in hold_chip) and r["bib"] not in publish_chip:
            continue
        fin = r["finish"]
        t = (_sod(fin["clock"]) - base) if base is not None else fin.get("elapsed")
        if t is None or t <= 0:
            continue
        row = ["", r["bib"], "", r.get("last_name", ""), r.get("first_name", ""), r.get("school", ""),
               _format_race_time(t), "", "", "", split_field(r), _clock(base + t) if base is not None else "",
               "", "", "", "", _format_race_time(t), "", _format_race_time(t)]
        entered = fl_names.get(r["bib"])
        if entered:                      # FinishLynx's entry: its lane/id, names, team, gender
            for i in (2, 3, 4, 5, 12):
                row[i] = entered[i] or row[i]
        finishers.append((t, row, False))
        chip.add(r["bib"])
    trailing = [row for row in trailing if row[1].strip() not in chip]

    finishers.sort(key=lambda f: f[0])
    lines = []
    prev = None
    for place, (t, row, from_fl) in enumerate(finishers, 1):
        row[0] = str(place)
        if not from_fl and prev is not None:
            row[8] = row[15] = _format_race_time(t - prev)
        prev = t
        lines.append(row)
    lines.extend(trailing)

    on_course = []
    for r in results.get("on_course", []):
        if r["bib"] in in_fl or (r["bib"] in hold_chip and r["bib"] not in publish_chip):
            continue                    # off the start list: splits wait with the finish
        field = split_field(r)
        if not field:
            continue                    # nothing MeetPro could file correctly yet
        n = field.count("(")
        last = parse_race_time(field.rsplit(",", 1)[-1].split(" (")[0]) or 0.0
        row = ["", r["bib"], "", r.get("last_name", ""), r.get("first_name", ""), r.get("school", ""),
               "", "", "", "", field] + [""] * (ROW_FIELDS - 11)
        on_course.append((-n, last, row))
    lines.extend(row for _, _, row in sorted(on_course, key=lambda x: (x[0], x[1])))

    header = list(fl_header) if fl_header else [
        str(event), str(round_), str(heat), name, "", "", "", "", "", str(distance or ""),
        _clock(base) if base is not None else ""]
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows([header] + lines)
    return out.getvalue()
