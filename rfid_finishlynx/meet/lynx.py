"""FinishLynx start lists from the meet model: lynx.evt, lynx.ppl, lynx.sch.

rfid-meet replaces MeetPro and owns the start lists (2026-10-03). Every edit
rewrites the three files in the FinishLynx input directory (the RFID folder,
see lif_merge.start_list_dir) in MeetPro's dialect, so FinishLynx and the
console's existing readers (web.evt_race_list, web.parse_ppl_file, the .evt /
.ppl folder monitor) keep working unchanged.

Layouts (official FinishLynx layouts in docs/MEET-RESEARCH.md section 1;
MeetPro's dialect from our real files and tests/test_evt_races.py):

    lynx.evt   ;Created by ...                       comment, ignored
               Event#,Round,Heat,Name,,,Template,,,Distance
               ,Bib,Lane,Last,First,Affiliation      one per runner
    lynx.ppl   Bib,Last,First,Affiliation,,Sex       whole roster
    lynx.sch   Event,Round,Heat                      run order

Everything that is a guess until the files are compared with real
MeetPro-written ones (golden comparison on the trailer PCs) is a constant
at the top of this file, so adjusting the dialect is a one-line change.

No field is quoted: none of the readers (FinishLynx, web.py, MeetPro) is
known to honour CSV quotes in these files, so a comma in a name or school
would shift every later field (bench/SPLIT-LAB.md). Fields are cleaned
instead: commas, semicolons, quotes and line breaks are replaced, and
names are folded to ASCII (see ASCII_ONLY).
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

from .model import Athlete, Meet, Race

logger = logging.getLogger(__name__)

# ------------------------------------------------------------ dialect knobs
# Each of these is a guess to confirm against real MeetPro files (2026-10-03).

EVT_NAME = "lynx.evt"
PPL_NAME = "lynx.ppl"
SCH_NAME = "lynx.sch"

# First line of the .evt. FinishLynx skips ';' lines and nothing in this
# codebase keys off the text (grep 2026-10-03: only a test sample), so we
# say who wrote it instead of impersonating MeetPro. No timestamp: the
# content must be identical when nothing changed (see write_start_lists).
EVT_HEADER: Optional[str] = ";Created by rfid-meet"
PPL_HEADER: Optional[str] = None   # MeetPro's .ppl/.sch headers not seen yet
SCH_HEADER: Optional[str] = None

# CRLF and a final line break, as in the MeetPro sample (tests/test_evt_races.py)
# and what the console's own .evt rewrite produces on Windows.
NEWLINE = "\r\n"

# FinishLynx reads ANSI files (lif_merge.ENCODING); the console reads .evt as
# UTF-8 (strict when it rewrites templates). Folding names to ASCII makes
# both readings agree; meet.json keeps the accents.
ENCODING = "cp1252"
ASCII_ONLY = True

# Template column (field 7). MeetPro leaves it empty (sample in
# tests/test_evt_races.py: "1,1,1,Boys 5K,,,,,,5000"); the TEMPLATE_..._FL
# names seen in real files are what the console's own template pass
# (web.process_evt_file_with_templates) wrote in afterwards, together with
# copying the .evn files. "meetpro" = leave it empty, the console pass fills
# it as today; "console" = write what that pass would write, so its rewrite
# is a no-op (the .evn files must then be in the Lynx folder).
TEMPLATE_MODE = "meetpro"
XC_TEMPLATE = "TEMPLATE_XC"        # = web.XC_TEMPLATE
TRACK_TEMPLATES = [                # = web.TRACK_EVENT_TEMPLATES, relays first
    ("4 x 800", "TEMPLATE_4x800m"), ("4x800", "TEMPLATE_4x800m"),
    ("4 x 1600", "TEMPLATE_4x1600m"), ("4x1600", "TEMPLATE_4x1600m"),
    ("3200", "TEMPLATE_3200m"), ("1600", "TEMPLATE_1600m"), ("800", "TEMPLATE_800m"),
]
TRACK_TEMPLATE_SUFFIX = "_FL"      # finish line only; "_SPLITS" with a 200 m split

# An entered athlete without a bib can't be identified by FinishLynx or a
# tag: leave them out of the .evt (with a warning) rather than write a row
# with a blank ID that no read or .lif row could ever match.
EVT_INCLUDE_UNBIBBED = False
# MeetPro writes the whole roster to the .ppl, bib or not (web.parse_ppl_file:
# "bib can be empty if athlete has no assigned number"); readers skip blank bibs.
PPL_INCLUDE_UNBIBBED = True

RUNS = ("entered", "exhibition")   # entry statuses that start the race

# atomic replace retries: FinishLynx or a virus scanner may hold the file a moment
REPLACE_TRIES = 5
REPLACE_DELAY = 0.1


# ------------------------------------------------------------ field cleaning

_FOLD = str.maketrans({
    "ø": "o", "Ø": "O", "ß": "ss", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Þ": "Th", "þ": "th", "ð": "d",
    "Ð": "D", "ı": "i", "‘": "'", "’": "'", "‚": "'", "´": "'", "`": "'",
    "“": "", "”": "", "„": "", "–": "-", "—": "-", " ": " ",
})
_UNSAFE = re.compile(r'[,;"\x00-\x1f\x7f]')     # field breakers + control chars
_SPACES = re.compile(r"\s+")


def clean(value, warnings: Optional[List[str]] = None, what: str = "") -> str:
    """One field as it can go into a Lynx file unquoted.

    A comma would shift every later field, a ';' could start a comment, a
    quote may be taken as CSV quoting, a line break splits the record; all
    become a space (then runs of spaces collapse): "Smith, Jr." -> "Smith Jr.".
    With ASCII_ONLY, accents are folded ("José" -> "Jose"); a character that
    has no ASCII form becomes '?' and is reported in `warnings`."""
    s = "" if value is None else str(value)
    s = _UNSAFE.sub(" ", s)
    if ASCII_ONLY:
        s = unicodedata.normalize("NFKD", s.translate(_FOLD))
        s = "".join(c for c in s if not unicodedata.combining(c))
        folded = s.encode("ascii", "replace").decode("ascii")
        if folded != s and warnings is not None:
            warnings.append(f"{what or 'field'} {value!r}: characters without an ASCII form written as '?'")
        s = _UNSAFE.sub(" ", folded)
    return _SPACES.sub(" ", s).strip()


def _line(fields) -> str:
    return ",".join("" if f is None else str(f) for f in fields)


# ------------------------------------------------------------ helpers

def _name_gender(name: str) -> str:
    """Gender the console reads from an event name; a copy of
    web._determine_event_gender (web.py pulls in Flask, this module stays
    stdlib). tests/test_meet_lynx.py checks the two agree."""
    n = name.lower()
    if "girl" in n or "women" in n:
        return "F"
    if "boy" in n or "men" in n:
        return "M"
    return ""


def affiliation(meet: Meet, a: Athlete) -> str:
    """The athlete's school as Lynx files show it: the team's short name,
    the same string Meet.athlete_lookup gives the race panes, so a .lif row,
    a pane and the .ppl all name the team alike (team scores group by it)."""
    t = meet.team(a.team_id)
    return (t.short or t.name) if t else ""


def race_name(meet: Meet, race: Race) -> str:
    ev = meet.event(race.event)
    return race.name or (ev.name if ev else "") or f"Event {race.event}"


def template_for(meet: Meet, race: Race, mode: Optional[str] = None) -> str:
    """Field 7 of the event line (see TEMPLATE_MODE)."""
    mode = mode or TEMPLATE_MODE
    if mode != "console":
        return ""
    if meet.meet.kind == "xc":
        return XC_TEMPLATE
    name = race_name(meet, race).lower()
    for pattern, base in TRACK_TEMPLATES:
        if pattern in name:
            return base + TRACK_TEMPLATE_SUFFIX
    return ""


def _entry_sort_key(meet: Meet, a: Athlete, lane: Optional[int]):
    """Lane first (track), then team, last, first: what an official scans
    for on the FinishLynx start list. Independent of edit order, so adding
    one runner moves one line."""
    return (lane is None, lane or 0, affiliation(meet, a).lower(), a.last.lower(),
            a.first.lower(), a.bib)


def _bib_key(bib: str):
    return (0, int(bib), "") if bib.isdigit() else (1, 0, bib)


# ------------------------------------------------------------ rendering

def render(meet: Meet, templates: Optional[str] = None) -> dict:
    """All three files as text plus what was left out and why:
    {"evt": str, "ppl": str, "sch": str, "warnings": [str], "counts": {...}}.
    Pure: no file access. `templates` overrides TEMPLATE_MODE."""
    warnings: List[str] = []
    counts = {"races": 0, "entries": 0, "athletes": 0, "athletes_without_bib": 0,
              "skipped_no_bib": 0, "scratched": 0, "relay_races_without_entries": 0}

    for bib, ids in sorted(meet.used_bibs().items(), key=lambda kv: _bib_key(kv[0])):
        if len(ids) > 1:
            warnings.append(f"bib {bib} on {len(ids)} athletes ({', '.join(ids)}): "
                            "FinishLynx and the console keep only one")
    for a in meet.athletes:
        if a.bib and not a.bib.isdigit():
            warnings.append(f"athlete {a.id} {a.first} {a.last}: bib {a.bib!r} is not a number; "
                            "the console ignores it")

    # ---- .evt + .sch in run order
    evt: List[str] = [EVT_HEADER] if EVT_HEADER else []
    sch: List[str] = [SCH_HEADER] if SCH_HEADER else []
    for race in meet.races_in_order():
        counts["races"] += 1
        ev = meet.event(race.event)
        name = race_name(meet, race)
        if ev is None:
            warnings.append(f"race {race.event}-{race.round}-{race.heat}: no event {race.event} in the meet")
        elif ev.gender in ("M", "F") and _name_gender(name) != ev.gender:
            # the console takes the race's gender from its name (wrong-gender check)
            seen = _name_gender(name) or "mixed"
            warnings.append(f"race {race.event}-{race.round}-{race.heat} {name!r}: the console reads "
                            f"gender {seen} from the name, the event is {ev.gender}")
        distance = ev.distance_m if ev and ev.distance_m else ""
        evt.append(_line([race.event, race.round, race.heat,
                          clean(name, warnings, "race name"), "", "",
                          template_for(meet, race, templates), "", "", distance]))
        sch.append(_line([race.event, race.round, race.heat]))
        if ev is not None and ev.kind == "relay":
            # Relays come later (track). MeetPro's relay row is
            # ",,Lane,TeamName,,CODE A"; it goes here, keyed on ev.kind.
            counts["relay_races_without_entries"] += 1
            if race.entries:
                warnings.append(f"race {race.event}-{race.round}-{race.heat} {name!r}: relay entries "
                                "are not written yet")
            continue
        evt.extend(_entry_lines(meet, race, warnings, counts))

    # ---- .ppl: the whole roster
    ppl: List[str] = [PPL_HEADER] if PPL_HEADER else []
    bibbed = sorted((a for a in meet.athletes if a.bib), key=lambda a: _bib_key(a.bib))
    # Without a bib only the entered ones: a full-roster meet carries every
    # rostered athlete, and a no-bib, not-entered line is noise (2026-10-03).
    unbibbed = sorted((a for a in meet.athletes if not a.bib and meet.entered(a.id)),
                      key=lambda a: _entry_sort_key(meet, a, None))
    counts["athletes_without_bib"] = len(unbibbed)
    for a in bibbed + (unbibbed if PPL_INCLUDE_UNBIBBED else []):
        sex = a.gender if a.gender in ("M", "F") else ""
        if not sex:
            warnings.append(f"athlete {a.id} {a.first} {a.last}: gender {a.gender!r} written blank")
        ppl.append(_line([a.bib, clean(a.last, warnings, "last name"),
                          clean(a.first, warnings, "first name"),
                          clean(affiliation(meet, a), warnings, "team"), "", sex]))
        counts["athletes"] += 1

    def text(lines):
        return "".join(l + NEWLINE for l in lines)

    return {"evt": text(evt), "ppl": text(ppl), "sch": text(sch),
            "warnings": warnings, "counts": counts}


def _entry_lines(meet: Meet, race: Race, warnings: List[str], counts: dict) -> List[str]:
    """,Bib,Lane,Last,First,Affiliation for each runner who starts:
    entered + exhibition (exhibition runs, it just doesn't score), never
    scratched."""
    tag = f"race {race.event}-{race.round}-{race.heat}"
    rows: List[Tuple[tuple, str]] = []
    seen = set()
    for e in race.entries:
        if e.status not in RUNS:
            counts["scratched"] += 1
            continue
        a = meet.athlete(e.athlete_id)
        if a is None:
            warnings.append(f"{tag}: entry for unknown athlete {e.athlete_id} left out")
            continue
        if a.id in seen:
            warnings.append(f"{tag}: {a.first} {a.last} entered twice; written once")
            continue
        seen.add(a.id)
        if not a.bib:
            counts["skipped_no_bib"] += 1
            if not EVT_INCLUDE_UNBIBBED:
                warnings.append(f"{tag}: {a.first} {a.last} ({affiliation(meet, a) or 'no team'}) "
                                "has no bib; left out of the .evt")
                continue
        lane = e.lane if isinstance(e.lane, int) and e.lane > 0 else None
        line = _line(["", a.bib, lane, clean(a.last, warnings, "last name"),
                      clean(a.first, warnings, "first name"),
                      clean(affiliation(meet, a), warnings, "team")])
        rows.append((_entry_sort_key(meet, a, lane), line))
        counts["entries"] += 1
    return [line for _, line in sorted(rows, key=lambda r: r[0])]


def _dedupe(warnings: List[str]) -> List[str]:
    """A name with a bad character shows up once per file it is in."""
    out, seen = [], set()
    for w in warnings:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def evt_text(meet: Meet, templates: Optional[str] = None) -> str:
    return render(meet, templates)["evt"]


def ppl_text(meet: Meet) -> str:
    return render(meet)["ppl"]


def sch_text(meet: Meet) -> str:
    return render(meet)["sch"]


# ------------------------------------------------------------ writing

def _write_if_changed(path: str, data: bytes) -> bool:
    """Atomic write (tmp + os.replace) only when the bytes differ.
    FinishLynx and the console's monitor react to the mtime, so an unchanged
    file is left alone. Returns True when the file was written."""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_DELAY)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True


def write_start_lists(meet: Meet, directory: str, templates: Optional[str] = None) -> dict:
    """Write lynx.ppl, lynx.sch, lynx.evt into `directory` (the FinishLynx
    input folder; the caller resolves it, e.g. the meet's RFID folder).

    .ppl first so a new bib's name is there before the .evt names it; .evt
    last because that is the file the console's monitor re-reads the races
    from. A file whose content would not change is not touched.

    Returns {"paths": {evt, ppl, sch}, "written": [names], "unchanged":
    [names], "errors": [str], "warnings": [str], "counts": {...}}. A file
    that can't be replaced (held open) is reported in errors, the others
    are still written."""
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"FinishLynx input folder not found: {directory}")
    out = render(meet, templates)
    report = {"paths": {}, "written": [], "unchanged": [], "errors": [],
              "warnings": _dedupe(out["warnings"]), "counts": out["counts"]}
    for kind, name in (("ppl", PPL_NAME), ("sch", SCH_NAME), ("evt", EVT_NAME)):
        path = os.path.join(directory, name)
        report["paths"][kind] = path
        data = out[kind].encode(ENCODING, errors="replace")
        try:
            changed = _write_if_changed(path, data)
        except OSError as e:
            logger.error(f"Could not write {path}: {e}")
            report["errors"].append(f"{name}: {e}")
            continue
        (report["written"] if changed else report["unchanged"]).append(name)
    if report["written"]:
        logger.info(f"Start lists in {directory}: wrote {', '.join(report['written'])} "
                    f"({out['counts']['races']} races, {out['counts']['entries']} entries, "
                    f"{out['counts']['athletes']} athletes)")
    for w in report["warnings"]:
        logger.warning(f"Start lists: {w}")
    return report
