"""Results and rosters PDFs in the look of MeetPro's official results.

Coaches, parents and MileSplit readers know MeetPro's "OFFICIAL MEET REPORT"
pages (user's ask, 2026-10-03): meet block top left, boxed race title,
black-to-white section bars, a team scoring summary with the scoring order
(displacers in parentheses), then the individual results ruled every five
rows. This module lays those pages out with pdfdoc (stdlib only); the data
rules stay in printing.py (filter_results, roster_pages) and scoring.py, so
the text printout and the PDF can't disagree on who is listed or where.
No MeetPro / DirectAthletics branding: the timing company's logo top right
when branding/report-logo.png (or .jpg) is in the install, else a plain
"rfid-meet" wordmark (2026-10-03, user's ask).

Data: printing.filter_results (results; real places kept, filter text and
show_team_scores) and printing.roster_pages (rosters; called with this
page's ROSTER_ROWS so the paging rule fits these pages exactly).
The reports.py documents (team scores, awards, finish line sheets, split
scores, Avery 5160 labels) are laid out at the end of this module, from
reports.py's data (2026-10-03).

Page geometry (Letter portrait, points, y down from the top):
- margins 18 pt left/right (MeetPro uses ~14; 18 keeps clear of printers'
  unprintable edge), page header to y=104, title box y=110..150, body from
  y=153 (+12 when a filter line sits under the title box), rows end by
  y=738, footer baseline y=748.
- tables: a 12.2 pt section bar, column labels, a 2 pt rule, then rows of
  12 pt (team rows in 1-column: 15 pt, ruled under every row). A full
  continuation column holds ROWS_PER_COLUMN = 46 individual rows, the same
  as MeetPro's; the first page holds fewer under the team summary.
- 2-column: two 282 pt columns with a 12 pt gutter and a rule between.

Formatting rules (2026-10-03):
- Times: printing.format_time (hundredths rounded UP), so the PDF, text and
  uploads show the same mark.
- Avg. Mile / Avg. kM: m:ss.t from the printed time, nearest tenth (MeetPro
  prints tenths; pace is informational, not a mark).
- Team Total: sum of the scorers' times, whole seconds rounded up, h:mm:ss.
  Spread: 5th scorer's printed time minus the 1st's, m:ss.t.
- YR: SR / JR / SO for grades 12 / 11 / 10 and the digit for 9 and below
  (YEAR_LABELS; MeetPro's habit).
- Score: points for scorers, (n) for displacers, "-" for a finisher who
  doesn't score (incomplete team, unattached, non-scoring team), EX for
  exhibition. DNF / DQ / DNS sit after the finishers, status in the Time
  column.
- Text is cut to its column by measured width, never allowed to overlap.

Splits (user's ask, 2026-10-03: "include mile splits in the one column
version, if they're available"): when the frozen results carry
split_labels, the 1-column individual table shows each finisher's elapsed
time at every split, m:ss.t rounded UP to the tenth. Tenths because splits
are chip reads off a mat, not FAT: hundredths would claim a precision they
don't have (USATF/NFHS round non-FAT times up to the tenth), and the
narrower cells let more splits fit. Estimated splits print as "~5:22.4";
a missed mat prints an em dash; legend lines say so. What gives way, by
number of splits (page width is the constraint):
- 1-3 splits: their own columns right of Avg. kM (everything else stays).
- 4-5 splits: Avg. kM is dropped and the splits take its place.
- 6+ splits: a second, smaller line under each athlete with every split
  ("Mile 1 5:22.4 · Mile 2 ..."), rows 20 pt instead of 12.
The 2-column version never shows splits (no room).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from struct import error as struct_error
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import divisions as divisions_mod
from . import pdfdoc, printing, scoring
from .model import Athlete, Meet, Race
from .pdfdoc import BOLD, BOLD_OBLIQUE, HELVETICA, OBLIQUE

APP_NAME = printing.APP_NAME

# The timing company's logo (2026-10-03): branding/report-logo.png or .jpg at
# the install root (promote.py copies it to the timing PCs). Drawn top right
# in a LOGO_W x LOGO_H box, aspect kept, in place of the wordmark.
BRANDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "branding")
LOGO_NAMES = ("report-logo.png", "report-logo.jpg", "report-logo.jpeg")
LOGO_W, LOGO_H, LOGO_TOP = 220.0, 64.0, 15.0     # fits a stacked logo (Split Second, ~1.8:1) too
_logo_cache: Dict[str, Tuple[float, Optional["pdfdoc.Image"]]] = {}


def report_logo(directory: Optional[str] = None) -> Optional["pdfdoc.Image"]:
    """The logo image, re-read when the file changes; None when there is none
    or it can't be read (the wordmark is used then)."""
    for name in LOGO_NAMES:
        path = os.path.join(directory or BRANDING_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        hit = _logo_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(path, "rb") as f:
                img = pdfdoc.Image(f.read())
        except (OSError, ValueError, IndexError, struct_error) as e:
            import logging
            logging.getLogger(__name__).warning(f"report logo {path} not used: {e}")
            img = None
        _logo_cache[path] = (mtime, img)
        return img
    return None

# ------------------------------------------------------------- geometry

LEFT, RIGHT = 18.0, pdfdoc.PAGE_W - 18.0
CONTENT_W = RIGHT - LEFT                       # 576
GUTTER = 12.0
COL_W = (CONTENT_W - GUTTER) / 2               # 282
TITLE_TOP, TITLE_H = 110.0, 40.0
BODY_TOP = TITLE_TOP + TITLE_H + 3             # 153
FILTER_H = 12.0
BOTTOM = 738.0
FOOTER_Y = 748.0

BAR_H = 12.2
HEAD_H = 25.0                                  # bar + column labels + thick rule
ROW_H = 12.0
TEAM_ROW_H = 15.0
SUB_H = 8.0                                    # the splits line (6+ splits)
ROW_FONT = 8.5
HEAD_FONT = 8.0
SUB_FONT = 7.0
NOTE_FONT = 7.0
RULE_W = 0.9
ROWS_PER_COLUMN = int((BOTTOM - BODY_TOP - HEAD_H) // ROW_H)     # 46
ROSTER_ROWS = ROWS_PER_COLUMN - 1            # room for each gender's total line (2026-10-03)

# MeetPro's habit (user, 2026-10-03): high-school grades as SR/JR/SO,
# freshmen and younger as the digit.
YEAR_LABELS = {"12": "SR", "11": "JR", "10": "SO"}
NOT_FINAL = "Results — not final"
# Live results of a race nobody has marked final yet (2026-10-05, user: print
# the current finishers, marked so the sheet isn't taken for the final one).
NOT_COMPLETE = "NOT MARKED COMPLETE"
NO_SPLIT = "—"
SPLIT_COLS_MAX = 3            # beside both pace columns
SPLIT_COLS_NO_KM = 5          # Avg. kM dropped; more than this: a second line


# ------------------------------------------------------------- formatting

def year_label(grade) -> str:
    key = printing._grade_key(grade)
    return YEAR_LABELS.get(key, key.upper())


def _hund(seconds: Optional[float]) -> Optional[int]:
    """Printed hundredths (printing.format_time's rounding up)."""
    if seconds is None:
        return None
    return -(-max(0, int(round(float(seconds) * 1000))) // 10)


def _clock(tenths: int) -> str:
    h, rem = divmod(tenths, 36000)
    m, rem = divmod(rem, 600)
    s, t = divmod(rem, 10)
    return f"{h}:{m:02d}:{s:02d}.{t}" if h else f"{m}:{s:02d}.{t}"


def format_tenths_up(seconds: Optional[float]) -> str:
    """A split: m:ss.t, rounded UP to the tenth (thousandths first, so
    float noise can't bump it)."""
    if seconds is None:
        return ""
    ms = max(0, int(round(float(seconds) * 1000)))
    return _clock(-(-ms // 100))


def format_pace(seconds: Optional[float], distance_m: float, per_m: float) -> str:
    """Pace per `per_m` metres from the printed time, nearest tenth."""
    hund = _hund(seconds)
    if hund is None or not distance_m or distance_m <= 0:
        return ""
    return _clock(int(hund * per_m / distance_m / 10 + 0.5))


def format_total(seconds: Optional[float]) -> str:
    """Team total: whole seconds rounded up, h:mm:ss (m:ss under an hour)."""
    if seconds is None:
        return ""
    ms = max(0, int(round(float(seconds) * 1000)))
    s = -(-ms // 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _long_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def _printed(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"printed: {dt.month}/{dt.day}/{dt.year} {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _athlete_name(a: Optional[Athlete], bib: str = "") -> str:
    """'LAST, First' as MeetPro prints it."""
    if a is None:
        return f"Unknown bib {bib}".strip() if bib else "Unknown"
    last, first = a.last.strip().upper(), a.first.strip()
    return f"{last}, {first}" if first else last


# ------------------------------------------------------------- page frame

class _Report:
    """The document plus the page furniture every page shares."""

    def __init__(self, meet: Meet, printed_at: Optional[datetime], title: str,
                 official: bool = True):
        self.meet = meet
        self.printed_at = printed_at or datetime.now()
        self.doc = pdfdoc.Document(title=title, producer=APP_NAME)
        # Blank forms (finish line sheets) are not a meet report (2026-10-03).
        self.official = official
        self.logo = report_logo()

    def page(self, left1: str, left2: str, right: str, note: str = "") -> Tuple[pdfdoc.Page, float]:
        """A new page with the header and title box; returns (page, body top)."""
        p = self.doc.new_page()
        info = self.meet.meet
        p.text(LEFT, 50, pdfdoc.fit((info.name or info.id).upper(), BOLD, 10.5, 380),
               BOLD, 10.5)
        y = 61
        for line in (info.location, info.host):
            if line:
                p.text(LEFT, y, pdfdoc.fit(line, HELVETICA, 7.5, 380), HELVETICA, 7.5)
                y += 11
        if info.date:
            p.text(LEFT, 85, _long_date(info.date), HELVETICA, 7.5)
        if self.logo is not None:
            scale = min(LOGO_W / self.logo.width, LOGO_H / self.logo.height)
            w, h = self.logo.width * scale, self.logo.height * scale
            p.image(self.logo, RIGHT - w, LOGO_TOP + (LOGO_H - h) / 2, w, h)
        else:
            p.text(RIGHT, 64, APP_NAME, BOLD_OBLIQUE, 22, align="right")
        if self.official:
            p.text(RIGHT, 95, "OFFICIAL MEET REPORT", BOLD, 8, align="right")
        p.text(RIGHT, 104, _printed(self.printed_at), HELVETICA, 6.5, align="right")

        p.rect(LEFT + 1, TITLE_TOP, CONTENT_W - 2, TITLE_H, stroke=0, line_width=2.2)
        right_w = pdfdoc.measure(right, HELVETICA, 16)
        left_w = CONTENT_W - right_w - 30
        p.text(LEFT + 7, TITLE_TOP + 16, pdfdoc.fit(left1, BOLD_OBLIQUE, 10, left_w),
               BOLD_OBLIQUE, 10)
        p.text(LEFT + 7, TITLE_TOP + 27.5, pdfdoc.fit(left2, BOLD_OBLIQUE, 10, left_w),
               BOLD_OBLIQUE, 10)
        p.text(RIGHT - 7, TITLE_TOP + 27, right, HELVETICA, 16, align="right")
        top = BODY_TOP
        if note:
            p.text(LEFT + 1, BODY_TOP + 8, pdfdoc.fit("Filtered: " + note, BOLD_OBLIQUE, 8,
                                                     CONTENT_W - 2), BOLD_OBLIQUE, 8)
            top += FILTER_H
        return p, top

    def finish(self) -> bytes:
        if not self.doc.pages:
            p, top = self.page("", "", "")
        for i, p in enumerate(self.doc.pages, 1):
            p.text(RIGHT, FOOTER_Y, f"Page {i}", OBLIQUE, 8, align="right")
        return self.doc.to_bytes()


def _bar(page: pdfdoc.Page, x: float, y: float, w: float, text: str) -> None:
    """MeetPro's section bar: black under the label, then fading to white
    (drawn as thin filled steps; no shading objects needed)."""
    text_w = pdfdoc.measure(text, BOLD, 10)
    solid = min(w * 0.9, max(w * 0.3, text_w + 12))
    page.rect(x, y, w, BAR_H, fill=0)
    # Each step runs to the bar's right end over the darker one before it:
    # side-by-side strips leave anti-aliasing seams that print as stripes.
    steps = 64
    sw = (w - solid) / steps
    for i in range(steps):
        page.rect(x + solid + i * sw, y, w - solid - i * sw, BAR_H,
                  fill=round((i + 1) / steps, 3))
    page.text(x + 2, y + 9.9, text, BOLD, 10, gray=1)


# ------------------------------------------------------------- tables

@dataclass
class Col:
    key: str
    label: str
    x: float                         # anchor, relative to the table's left edge
    align: str = "left"              # left | right | center
    width: float = 0                 # max text width; 0 = no limit
    font: str = HELVETICA
    head_x: Optional[float] = None   # label anchor when it differs
    head_align: Optional[str] = None


@dataclass
class Table:
    title: str
    cols: List[Col]
    rows: List[dict]                 # key -> text; "_bold": keys in bold; "_sub": splits
                                     # line; "_rule": ruled under this row
    row_h: float = ROW_H
    baseline: float = 9.0
    rule_every: int = 5              # 1 = under every row; the last row is always ruled
    vrules: Sequence[float] = ()
    sub_x: float = 0.0
    notes: List[str] = field(default_factory=list)


class _Flow:
    """Places blocks top to bottom in one or two columns per page, starting
    a column (or page) when the next block doesn't fit; tables continue in
    the next column under a "(cont'd)" bar."""

    def __init__(self, columns: int, new_page: Callable[[], Tuple[pdfdoc.Page, float]]):
        self.ncols = max(1, min(2, int(columns)))
        self._new_page = new_page
        self.page: Optional[pdfdoc.Page] = None
        self._start_page()

    def _start_page(self) -> None:
        self._close_page()
        self.page, self.top = self._new_page()
        self.col = 0
        self.y = self.top
        self.fresh = True
        self.col_end = [self.top] * self.ncols

    def _close_page(self) -> None:
        if self.page is not None and self.ncols == 2 and max(self.col_end) > self.top:
            x = LEFT + COL_W + GUTTER / 2
            self.page.line(x, self.top, x, max(self.col_end), RULE_W)

    def close(self) -> None:
        self._close_page()

    @property
    def x0(self) -> float:
        return LEFT + self.col * (COL_W + GUTTER)

    @property
    def width(self) -> float:
        return CONTENT_W if self.ncols == 1 else COL_W

    def next_slot(self) -> None:
        if self.col + 1 < self.ncols:
            self.col += 1
            self.y = self.top
            self.fresh = True
        else:
            self._start_page()

    def _advance(self, h: float) -> None:
        self.y += h
        self.col_end[self.col] = max(self.col_end[self.col], self.y)
        self.fresh = False

    def ensure(self, h: float) -> None:
        if BOTTOM - self.y < h and not self.fresh:
            self.next_slot()

    def gap(self, h: float) -> None:
        if not self.fresh:
            self.y += h

    def heading(self, text: str, keep: float) -> None:
        """A bold line kept with the `keep` points that follow it."""
        self.ensure(16 + keep)
        self.page.text(self.x0 + 1, self.y + 11, pdfdoc.fit(text, BOLD, 10, self.width),
                       BOLD, 10)
        self._advance(16)

    def note(self, text: str, font: str = OBLIQUE, size: float = NOTE_FONT) -> None:
        """A small line; too wide for the column, it breaks at the " · "
        between legend items (2-column pages) and only then is cut."""
        h = size + 3.5
        lines, cur = [], ""
        for part in text.split(printing.FILTER_SEP):
            joined = cur + printing.FILTER_SEP + part if cur else part
            if cur and pdfdoc.measure(joined, font, size) > self.width - 2:
                lines.append(cur)
                cur = part
            else:
                cur = joined
        lines.append(cur)
        for line in lines:
            self.ensure(h)
            self.page.text(self.x0 + 1, self.y + size + 1,
                           pdfdoc.fit(line, font, size, self.width - 2), font, size)
            self._advance(h)

    def first_height(self, t: Table, keep: int = 3) -> float:
        return HEAD_H + sum(self._row_h(t, r) for r in t.rows[:keep])

    @staticmethod
    def _row_h(t: Table, row: dict) -> float:
        return t.row_h + (SUB_H if row.get("_sub") else 0)

    def _head(self, t: Table, title: str) -> None:
        p, x0, y = self.page, self.x0, self.y
        bar_w = self.width * (538.0 / CONTENT_W) if self.ncols == 1 else self.width
        _bar(p, x0, y, bar_w, title)
        for c in t.cols:
            if not c.label:
                continue
            p.text(x0 + (c.x if c.head_x is None else c.head_x), y + 21.3, c.label, BOLD,
                   HEAD_FONT, align=c.head_align or c.align)
        p.rect(x0, y + 23, self.width, 1.8, fill=0)
        self._advance(HEAD_H)

    def _vrules(self, t: Table, top: float, bottom: float) -> None:
        for vx in t.vrules:
            self.page.line(self.x0 + vx, top - 0.5, self.x0 + vx, bottom, RULE_W)

    def _row(self, t: Table, row: dict) -> None:
        p, x0 = self.page, self.x0
        base = self.y + t.baseline
        bold = row.get("_bold", ())
        for c in t.cols:
            text = row.get(c.key, "")
            if not text:
                continue
            font = BOLD if c.key in bold else c.font
            if c.width:
                text = pdfdoc.fit(text, font, ROW_FONT, c.width)
            p.text(x0 + c.x, base, text, font, ROW_FONT, align=c.align)
        if row.get("_sub"):
            sub = pdfdoc.fit(row["_sub"], HELVETICA, SUB_FONT, self.width - t.sub_x - 2)
            p.text(x0 + t.sub_x, base + SUB_H, sub, HELVETICA, SUB_FONT, gray=0.2)

    def table(self, t: Table, keep: int = 3) -> None:
        if not t.rows:
            return
        self.ensure(self.first_height(t, keep))
        self._head(t, t.title)
        seg_top = self.y
        for i, row in enumerate(t.rows):
            h = self._row_h(t, row)
            if self.y + h > BOTTOM:
                self._vrules(t, seg_top, self.y)
                self.next_slot()
                self._head(t, f"{t.title} (cont'd)")
                seg_top = self.y
            self._row(t, row)
            self._advance(h)
            # "_rule": a rule under this row whatever rule_every says (the
            # team scores breakdown rules under each team's block, 2026-10-03).
            if (t.rule_every and (i + 1) % t.rule_every == 0) or i == len(t.rows) - 1 \
                    or row.get("_rule"):
                self.page.line(self.x0, self.y, self.x0 + self.width, self.y, RULE_W)
        self._vrules(t, seg_top, self.y)
        for n in t.notes:
            self.note(n)


# ------------------------------------------------------------- results tables

def _team_table(meet: Meet, teams: List[dict], elapsed_of: Dict[str, float],
                columns: int) -> Table:
    sc = meet.meet.scoring
    rows, tied_any = [], False
    for t in teams:
        team = meet.team(t.get("team_id"))
        name = team.name if team else "Unattached"
        if t.get("rank") is None:
            rows.append({"name": name, "order": f"{t.get('count', '')} — not scored"})
            continue
        runners = t.get("runners") or []
        order = "-".join(str(r["points"]) for r in runners if r.get("scorer")) + "".join(
            f"({r['points']})" for r in runners if not r.get("scorer") and r.get("points"))
        score = str(t.get("score", ""))
        tied = t["tied"] if "tied" in t else any(
            o is not t and o.get("rank") is not None and o.get("score") == t.get("score")
            for o in teams)
        if tied:
            score += "*"
            tied_any = True
        scorers = [elapsed_of.get(r.get("athlete_id") or "") for r in runners if r.get("scorer")]
        spread = ""
        if len(scorers) >= sc.scorers and None not in scorers:
            spread = _clock(int((_hund(scorers[-1]) - _hund(scorers[0])) / 10 + 0.5))
        rows.append({"rank": str(t["rank"]), "name": name, "score": score, "order": order,
                     "total": format_total(t.get("total_time")), "spread": spread,
                     "_bold": ("rank", "name", "score")})
    if columns == 1:
        cols = [Col("rank", "Final Standings", 13, "right", 18, head_x=0, head_align="left"),
                Col("name", "", 21, width=100),
                Col("score", "Score", 144, "right", 30),
                Col("order", "Scoring Order", 157, width=135),
                Col("total", "Total", 312, "right", 50),
                Col("spread", "Spread", 354, "right", 38)]
        table = Table("TEAM SCORING SUMMARY", cols, rows, row_h=TEAM_ROW_H, baseline=10,
                      rule_every=1, vrules=(154,))
    else:
        cols = [Col("rank", "Final Standings", 12, "right", 16, head_x=0, head_align="left"),
                Col("name", "", 16, width=92),
                Col("score", "Score", 125, "right", 26),
                Col("order", "Scoring Order", 133, width=98),
                Col("total", "Total", COL_W - 6, "right", 44)]
        table = Table("TEAM SCORING SUMMARY", cols, rows, rule_every=0, vrules=(130,))
    if tied_any:
        table.notes.append(f"* tied score: the better {_ordinal(sc.scorers + 1)} runner "
                           f"(first displacer) wins; no {_ordinal(sc.scorers + 1)} runner loses")
    return table


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _split_mode(n: int) -> str:
    """How n splits are shown in the 1-column table (see module notes)."""
    if n == 0:
        return "none"
    if n <= SPLIT_COLS_MAX:
        return "columns"
    if n <= SPLIT_COLS_NO_KM:
        return "columns_no_km"
    return "line"


def _individual_cols(columns: int, labels: Sequence[str]) -> Tuple[List[Col], Tuple, float]:
    """(columns, vertical rules, splits-line x) for the individual table."""
    if columns == 2:
        return ([Col("place", "Athlete", 13, "right", 16, head_x=0, head_align="left"),
                 Col("name", "", 16, width=78),
                 Col("yr", "YR", 104, "center", 16),
                 Col("bib", "#", 124, "center", 22),
                 Col("team", "Team", 137, width=60),
                 Col("score", "Score", 212, "center", 26),
                 Col("time", "Time", COL_W - 6, "right", 46)], (), 15.0)
    mode = _split_mode(len(labels))
    cols = [Col("place", "Athlete", 13, "right", 17, head_x=0, head_align="left"),
            Col("name", "", 19, width=103),
            Col("yr", "YR", 136, "center", 18),
            Col("bib", "#", 161, "center", 24),
            Col("team", "Team", 177, width=70),
            Col("score", "Score", 267, "center", 28),
            Col("time", "Time", 320, "right", 40, head_x=287, head_align="left"),
            Col("mile", "Avg. Mile", 325, width=40)]
    vrules = [174, 284, 322.5]
    if mode in ("none", "columns", "line"):
        cols.append(Col("km", "Avg. kM", 375, width=40))
    if mode in ("columns", "columns_no_km"):
        start = 422.0 if mode == "columns" else 371.0
        cw = min(52.0, (CONTENT_W - start) / len(labels))
        vrules.append(start - 3)
        for i, label in enumerate(labels):
            right = start + cw * (i + 1) - 2
            cols.append(Col(f"split{i}", pdfdoc.fit(label, BOLD, HEAD_FONT, cw - 4), right,
                            "right", cw - 3))
    return cols, tuple(vrules), 19.0


@dataclass
class _Section:
    num: int
    name: str
    finishers: List[dict]
    teams: Optional[List[dict]]
    team_note: str = ""
    division: str = ""                # a division's name: its standings (2026-10-06)


def _results_for(meet: Meet, race: Race, places, grades, team_ids, hide=None,
                 events=None) -> Optional[dict]:
    """filter_results' dict for the race; a race that isn't final is read
    from its last results as if it were (title says "not final"), so a
    reopened race can still be proof-read on paper. None = no results.
    hide / events: the results print options (2026-10-03)."""
    if race.results is None:
        return None
    probe = race if race.status == "final" else dataclasses.replace(race, status="final")
    return printing.filter_results(meet, probe, places=places, grades=grades, team_ids=team_ids,
                                   hide=hide, events=events)


def _race_sections(meet: Meet, race: Race, res: Optional[dict], divisions=None,
                   **filters) -> List[_Section]:
    """_sections for the race's own results (res None: left out, a
    division-only printout), each scoring event followed by its division
    sections (2026-10-06). The division standings come from the race's
    results as _results_for read them (last results of a reopened race)."""
    out = [(s.num, 0, s) for s in _sections(meet, race, res)] if res is not None else []
    probe = race if race.status == "final" else dataclasses.replace(race, status="final")
    if race.results is not None:
        _, div_res = printing.division_results(meet, probe, divisions, **filters)
        for i, (div, fres, full) in enumerate(div_res, 1):
            active = printing._filter_active(fres)
            entered = set(divisions_mod.entered_events(meet, race, div))
            only = (fres.get("filter") or {}).get("events")
            by_event: Dict[int, List[dict]] = {}
            for f in fres.get("finishers", []):
                by_event.setdefault(f["event"], []).append(f)
            for num in sorted(set(by_event) | {n for n in entered if only is None or n in only}):
                rows = sorted(by_event.get(num, []), key=printing._row_order)
                if not rows and active:
                    continue
                teams, note = None, ""
                if fres.get("show_team_scores", True):
                    teams = (fres.get("teams") or {}).get(str(num)) or None
                elif (full.get("teams") or {}).get(str(num)):
                    note = printing.GRADE_FILTER_NO_TEAMS
                out.append((num, i, _Section(num, printing._event_name(meet, num, race), rows,
                                             teams, note, division=div.name)))
    out.sort(key=lambda x: (x[0], x[1]))
    return [s for _, _, s in out]


def _sections(meet: Meet, race: Race, res: dict) -> List[_Section]:
    """One section per scoring event (combined start), as the text output;
    with an active filter an event with no matching runner is left out."""
    active = printing._filter_active(res)
    only = (res.get("filter") or {}).get("events")
    by_event: Dict[int, List[dict]] = {n: [] for n in printing._race_events(race)
                                       if only is None or n in only}
    for f in res.get("finishers", []):
        by_event.setdefault(f["event"], []).append(f)
    out = []
    for num in sorted(by_event):
        rows = sorted(by_event[num], key=printing._row_order)
        if active and not rows:
            continue
        teams, note = None, ""
        if res.get("show_team_scores", True):
            teams = (res.get("teams") or {}).get(str(num)) or None
        elif ((race.results or {}).get("teams") or {}).get(str(num)):
            note = printing.GRADE_FILTER_NO_TEAMS
        out.append(_Section(num, printing._event_name(meet, num, race), rows, teams, note))
    return out


def _individual_table(meet: Meet, race: Race, sec: _Section, columns: int,
                      labels: Sequence[str]) -> Table:
    entries = {e.athlete_id: e for e in race.entries}
    ev = meet.event(sec.num)
    distance = ev.distance_m if ev else 0
    labels = list(labels) if columns == 1 else []
    mode = _split_mode(len(labels))
    cols, vrules, sub_x = _individual_cols(columns, labels)
    rows = []
    seen = {"(": False, "-": False, "EX": False, "~": False, "missing": False}
    for f in sec.finishers:
        a = printing._finisher_athlete(meet, f)
        entry = entries.get(a.id) if a else None
        finished = f.get("status") == "finished"
        bib = f.get("bib") or (a.bib if a else "")
        team = meet.team(a.team_id) if a else None
        row = {"place": str(f.get("event_place") or "") if finished else "",
               "name": _athlete_name(a, bib), "yr": year_label(meet.grade(a)) if a else "",
               "bib": bib, "team": team.name if team else ("Unattached" if a else "")}
        if finished:
            pts = f.get("points")
            if entry is not None and entry.status == "exhibition":
                row["score"] = "EX"
                seen["EX"] = True
            elif pts is None:
                row["score"] = "-"
                seen["-"] = True
            elif f.get("displacer"):
                row["score"] = f"({pts})"
                seen["("] = True
            else:
                row["score"] = str(pts)
            row["time"] = printing.format_time(f.get("elapsed"))
            row["mile"] = format_pace(f.get("elapsed"), distance, printing.METERS_PER_MILE)
            row["km"] = format_pace(f.get("elapsed"), distance, 1000.0)
        else:
            row["time"] = f.get("status") or ""
        if labels:
            got = {s.get("label"): s for s in f.get("splits") or []}
            cells = []
            for label in labels:
                s = got.get(label)
                if s is None or s.get("elapsed") is None:
                    cell = NO_SPLIT if finished else ""
                    seen["missing"] |= finished
                else:
                    cell = ("~" if s.get("estimated") else "") + format_tenths_up(s["elapsed"])
                    seen["~"] |= bool(s.get("estimated"))
                cells.append(cell)
            if mode == "line":
                if finished or any(c for c in cells):
                    row["_sub"] = "   ".join(f"{l} {c or NO_SPLIT}" for l, c in zip(labels, cells))
            else:
                for i, c in enumerate(cells):
                    row[f"split{i}"] = c
        rows.append(row)
    sc = meet.meet.scoring
    notes = []
    legend = []
    if seen["("]:
        legend.append("(n) = displacer: takes a place, does not score")
    if seen["-"]:
        legend.append(f"- = does not score (past a team's {_ordinal(sc.scorers + sc.displacers)}"
                      f" runner, incomplete team, unattached)")
    if seen["EX"]:
        legend.append("EX = exhibition: does not score or displace")
    if legend:
        notes.append(printing.FILTER_SEP.join(legend))
    if labels:
        split_legend = ["Splits: elapsed time, tenths rounded up"]
        if seen["~"]:
            split_legend.append("~ = estimated (no read at the mat)")
        if seen["missing"]:
            split_legend.append(f"{NO_SPLIT} = no split")
        notes.append(printing.FILTER_SEP.join(split_legend))
    # The splits line runs under the whole row, so no vertical rules then.
    return Table("INDIVIDUAL RESULTS", cols, rows,
                 vrules=vrules if columns == 1 and mode != "line" else (),
                 sub_x=sub_x, notes=notes)


def _race_title(meet: Meet, race: Race) -> Tuple[str, str]:
    num = f"Race #{race.event}"
    if race.round > 1:
        num += f" Round {race.round}"
    if race.heat > 1:
        num += f" Heat {race.heat}"
    return num, race.name or printing._event_name(meet, race.event, race)


def _render_race(rep: _Report, meet: Meet, race: Race, columns: int, res: Optional[dict],
                 sections: List[_Section], right: Optional[str] = None) -> None:
    left1, left2 = _race_title(meet, race)
    right = right or ("Final Results" if scoring.official_results(race) is not None else NOT_FINAL)
    note = ((res or {}).get("filter") or {}).get("text", "")
    flow = _Flow(columns, lambda: rep.page(left1, left2, right, note))
    if res is None:
        flow.note("No results yet.", HELVETICA, 9)
    elif not sections:
        flow.note(printing.NO_MATCH, HELVETICA, 9)
    labels = [l for l in ((res or {}).get("split_labels") or []) if l]
    elapsed_of = {f.get("athlete_id"): f.get("elapsed")
                  for f in (race.results or {}).get("finishers", []) if f.get("athlete_id")}
    for i, sec in enumerate(sections):
        team_t = _team_table(meet, sec.teams, elapsed_of, columns) if sec.teams else None
        ind_t = _individual_table(meet, race, sec, columns, labels)
        if i:
            flow.gap(10)
        if len(sections) > 1 or sec.division:
            first = team_t or ind_t
            flow.heading(f"Event {sec.num} · {sec.name}"
                         + (f" · Division: {sec.division}" if sec.division else ""),
                         flow.first_height(first))
        if team_t:
            flow.table(team_t)
            flow.gap(8)
        elif sec.team_note:
            flow.note(sec.team_note)
            flow.gap(4)
        if ind_t.rows:
            flow.table(ind_t)
        else:
            flow.note("No finishers.", HELVETICA, 9)
    flow.close()


def _check_columns(columns: int) -> int:
    if columns not in (1, 2):
        raise ValueError(f"columns must be 1 or 2, not {columns!r}")
    return columns


def results_pdf(meet: Meet, race: Race, *, columns: int = 1,
                places: Optional[Tuple[int, Optional[int]]] = None, grades=None,
                team_ids: Optional[Sequence[Optional[str]]] = None,
                printed_at: Optional[datetime] = None, hide=None, events=None,
                live: Optional[dict] = None, divisions=None) -> bytes:
    """One race's results, MeetPro style. Filters as printing.filter_results
    (real places kept; hide / events too); the filter is printed under the
    title box on every page. A race that isn't final prints its last
    results titled "Results - not final". live: the current results of a
    race that isn't final (scoring.live_scores) - printed instead, titled
    NOT MARKED COMPLETE, never as an official report. divisions: the race's
    division sections (printing.division_results, 2026-10-06)."""
    _check_columns(columns)
    divisions_mod.check_pick(meet, divisions)
    current = live is not None and scoring.official_results(race) is None
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Results",
                  official=not current)
    if current:
        race = dataclasses.replace(race, results=live)
    res = _results_for(meet, race, places, grades, team_ids, hide, events)
    if res is not None:
        res = dict(res, filter=dict(res.get("filter") or {}, text=printing.describe_filter(
            meet, places, grades, team_ids, hide, events, divisions)))
    overall, _ = divisions_mod.pick(meet, race, divisions)
    sections = _race_sections(meet, race, res if overall else None, divisions, places=places,
                              grades=grades, team_ids=team_ids, hide=hide,
                              events=events) if res else []
    _render_race(rep, meet, race, columns, res, sections,
                 right=NOT_COMPLETE if current else None)
    return rep.finish()


def meet_results_pdf(meet: Meet, *, columns: int = 1, races: Optional[Sequence[Race]] = None,
                     places: Optional[Tuple[int, Optional[int]]] = None, grades=None,
                     team_ids: Optional[Sequence[Optional[str]]] = None,
                     printed_at: Optional[datetime] = None, hide=None, events=None,
                     live: Optional[Dict[tuple, dict]] = None, divisions=None) -> bytes:
    """Every final race (or the final ones of `races`) in run order, each
    starting on a new page; a race with nothing matching the filter is left
    out. live: {race key: current results} of races not marked final -
    printed too, titled NOT MARKED COMPLETE (then no page says official).
    divisions: as results_pdf."""
    _check_columns(columns)
    divisions_mod.check_pick(meet, divisions)
    finals = printing.printable_races(meet, races, live)
    current = {r.key for r in finals if scoring.official_results(r) is None}
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Results",
                  official=not current)
    printed = 0
    for race in finals:
        if race.key in current:
            race = dataclasses.replace(race, results=live[race.key])
        res = _results_for(meet, race, places, grades, team_ids, hide, events)
        res = dict(res, filter=dict(res.get("filter") or {}, text=printing.describe_filter(
            meet, places, grades, team_ids, hide, events, divisions)))
        overall, _ = divisions_mod.pick(meet, race, divisions)
        sections = _race_sections(meet, race, res if overall else None, divisions,
                                  places=places, grades=grades, team_ids=team_ids, hide=hide,
                                  events=events)
        if not sections:
            continue
        _render_race(rep, meet, race, columns, res, sections,
                     right=NOT_COMPLETE if race.key in current else None)
        printed += 1
    if not printed:
        note = printing.describe_filter(meet, places, grades, team_ids, hide, events, divisions)
        p, top = rep.page("Results", meet.meet.name or meet.meet.id, "Results", note)
        p.text(LEFT + 1, top + 12, printing.NO_MATCH if finals else "No final results yet.",
               HELVETICA, 9)
    return rep.finish()


# ------------------------------------------------------------- rosters

ROSTER_COLS = [Col("bib", "#", 24, "right", 26),
               Col("name", "Athlete", 30, width=152),
               Col("yr", "YR", 196, "center", 18),
               Col("race", "Race", 212, width=46),
               Col("ex", "", COL_W - 4, "right", 16)]


def rosters_pdf(meet: Meet, team_ids: Optional[Sequence[Optional[str]]] = None,
                printed_at: Optional[datetime] = None) -> bytes:
    """Per school: girls and boys side by side, the gender with the lower
    bibs on the left (2026-10-05); a page per gender when either overflows
    its column (printing.roster_pages holds the rules; we pass this page's
    capacity, ROSTER_ROWS)."""
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Rosters")
    pages = printing.roster_pages(meet, team_ids, rows_per_column=ROSTER_ROWS)
    for pg in pages:
        name = pg["team"] + (f" ({pg['code']})" if pg.get("code") else "")
        if pg.get("pages", 1) > 1:
            name += f"  ·  page {pg['page']} of {pg['pages']}"
        label_of = dict(printing.ROSTER_GENDERS)
        counts = " · ".join(f"{label_of[g]} {pg['counts'][g]}" for g in pg["genders"])
        p, top = rep.page("Team Rosters", name, counts)
        for i, col in enumerate(pg["columns"]):
            x0 = LEFT + i * (COL_W + GUTTER)
            if not col.get("heading"):
                continue
            title = col["heading"].upper().replace("(CONT'D)", "(cont'd)")
            _bar(p, x0, top, COL_W, title)
            for c in ROSTER_COLS:
                if c.label:
                    p.text(x0 + c.x, top + 21.3, c.label, BOLD, HEAD_FONT, align=c.align)
            p.rect(x0, top + 23, COL_W, 1.8, fill=0)
            y = top + HEAD_H
            for n, r in enumerate(col["rows"], 1):
                name_txt = f"{r['last'].upper()}, {r['first']}" if r.get("first") else r["last"].upper()
                cells = {"bib": r.get("bib") or "----", "name": name_txt,
                         "yr": year_label(r.get("grade", "")), "race": r.get("race", ""),
                         "ex": "EX" if r.get("exhibition") else ""}
                for c in ROSTER_COLS:
                    text = pdfdoc.fit(cells[c.key], c.font, ROW_FONT, c.width) if c.width \
                        else cells[c.key]
                    p.text(x0 + c.x, y + 9, text, c.font, ROW_FONT, align=c.align)
                y += ROW_H
                if n % 5 == 0 or n == len(col["rows"]):
                    p.line(x0, y, x0 + COL_W, y, RULE_W)
            if not col["rows"]:
                p.text(x0 + 2, y + 10, "None entered.", OBLIQUE, ROW_FONT)
                y += ROW_H
            if col.get("total") is not None:
                gender = col["heading"].split(" ")[0].lower()
                p.text(x0 + COL_W - 2, y + 9, f"Total {gender}: {col['total']}", BOLD, ROW_FONT,
                       align="right")
        x = LEFT + COL_W + GUTTER / 2
        p.line(x, top, x, top + HEAD_H + ROW_H * (1 + max(1, max(len(c["rows"]) for c in pg["columns"]))),
               RULE_W)
        legend = printing.ROSTER_LEGEND.replace("    ", printing.FILTER_SEP)
        if pg.get("no_bib"):
            legend += f"{printing.FILTER_SEP}{pg['no_bib']} without a bib"
        p.text(LEFT, FOOTER_Y, legend, OBLIQUE, NOTE_FONT)
    if not pages:
        p, top = rep.page("Team Rosters", "", "")
        p.text(LEFT + 1, top + 12, "No entered athletes.", HELVETICA, 9)
    return rep.finish()


# ------------------------------------------------------------- schedule
#
# The run order with scheduled start times, one page for any XC meet (a
# 46-row column holds far more races than a meet runs), 2026-10-03. Data:
# printing.schedule_rows, the same rows the text version prints. Not an
# official report (no results on it), so no "OFFICIAL MEET REPORT" line.

SCHEDULE_COLS = [Col("start", "Start", 46, "right", 46),
                 Col("n", "#", 66, "right", 16),
                 Col("rk", "Evt", 76, width=40),
                 Col("race", "Race", 122, width=200, font=BOLD),
                 Col("div", "Divisions", 330, width=196),
                 Col("entries", "Entries", CONTENT_W - 4, "right", 40)]


def schedule_pdf(meet: Meet, printed_at: Optional[datetime] = None) -> bytes:
    rows = printing.schedule_rows(meet)
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Schedule",
                  official=False)
    timed = sum(1 for r in rows if r["start"])
    sub = f"{len(rows)} race{'' if len(rows) == 1 else 's'}" + \
        (f" · {sum(r['entries'] for r in rows)} runners" if rows else "")
    flow = _Flow(1, lambda: rep.page("Schedule", sub, "Schedule"))
    table = Table("RUN ORDER", SCHEDULE_COLS, [
        {"start": r["start"] or "—", "n": str(r["n"]), "rk": r["rk"], "race": r["race"],
         "div": "" if r["divisions"] == [r["race"]] else ", ".join(r["divisions"]),
         "entries": str(r["entries"]), "_bold": ("start",)} for r in rows],
        row_h=TEAM_ROW_H, baseline=10, rule_every=1)
    if rows and not timed:
        table.notes.append("No start times set yet (Meet › Races › Start).")
    elif rows:
        table.notes.append("Scheduled times: a race may start later than listed.")
    flow.table(table)
    if not rows:
        flow.note("No races in this meet yet.", HELVETICA, 9)
    flow.close()
    return rep.finish()


# ------------------------------------------------------------- meet reports
#
# The reports.py documents (2026-10-03): team scores (+ breakdown), awards,
# finish line sheets, split scores and Avery 5160 label sheets. The data and
# its rules come from reports.py; this only lays it out, on the same page
# frame, section bars and tables as the results. Team numbers follow the
# results PDF's team summary (Total whole seconds rounded up, Spread and
# Average m:ss.t rounded up) so the two PDFs never show one team two ways;
# the text reports print hundredths as printing.py does.

from . import reports  # noqa: E402  (reports imports printing, never us)

SCORE_COLS = [Col("rank", "Rank", 20, "right", 18, head_x=0, head_align="left"),
              Col("name", "Team", 28, width=180),
              Col("score", "Score", 240, "right", 30),
              Col("order", "Scoring Order", 252, width=170),
              Col("total", "Total", 466, "right", 50),
              Col("avg", "Average", 520, "right", 46),
              Col("spread", "Spread", CONTENT_W - 4, "right", 46)]
# Score Breakdown rows, indented under the team name (no column labels).
BREAKDOWN_COLS = [Col("rpl", "", 52, "right", 20),
                  Col("rname", "", 58, width=150),
                  Col("ryr", "", 220, "center", 18),
                  Col("rbib", "", 262, "right", 34),
                  Col("rtime", "", 330, "right", 52),
                  Col("rpts", "", 372, "right", 30)]


def _tenths_diff(seconds: Optional[float]) -> str:
    return "" if seconds is None else _clock(int(float(seconds) * 10 + 0.5))


def _runner_name(r: dict) -> str:
    if not r.get("last"):
        return r.get("name", "")
    return f"{r['last'].upper()}, {r['first']}" if r.get("first") else r["last"].upper()


def _score_table(meet: Meet, title: str, teams: List[dict], incomplete: List[dict],
                 breakdown: bool) -> Table:
    """reports.team_scores / split_scores rows as one table: a team per
    row (ruled under each), its runners under it with breakdown."""
    sc = meet.meet.scoring
    rows: List[dict] = []
    tied_any = False

    def runners(t: dict) -> None:
        if not breakdown:
            return
        for r in t["runners"]:
            rows.append({"rpl": str(r["place"] or ""), "rname": _runner_name(r),
                         "ryr": year_label(r["grade"]), "rbib": r["bib"], "rtime": r["time"],
                         "rpts": r["points_text"] or "-"})
        rows[-1]["_rule"] = True

    for t in teams:
        tied_any |= t["tied"]
        rows.append({"rank": str(t["rank"]), "name": t["team"],
                     "score": f"{t['score']}{'*' if t['tied'] else ''}", "order": t["order"],
                     "total": format_total(t["total_time"]),
                     "avg": format_tenths_up(t["avg_time"]),
                     "spread": _tenths_diff(t["spread"]),
                     "_bold": ("rank", "name", "score"), "_rule": True})
        runners(t)
    for t in incomplete:
        rows.append({"name": t["team"], "order": f"{t['count']} — not scored",
                     "_rule": True})
        runners(t)
    table = Table(title, SCORE_COLS + (BREAKDOWN_COLS if breakdown else []), rows,
                  row_h=ROW_H if breakdown else TEAM_ROW_H, baseline=9 if breakdown else 10,
                  rule_every=0)
    if tied_any:
        nth = _ordinal(sc.scorers + 1)
        table.notes.append(f"* tied score: the better {nth} runner (first displacer) wins; "
                           f"no {nth} runner loses; a tie neither can break stands")
    if breakdown and any(r["displacer"] for t in teams for r in t["runners"]):
        table.notes.append("(n) = displacer: takes a place, does not score"
                           f"{printing.FILTER_SEP}Pl = place in this event")
    return table


def team_scores_pdf(meet: Meet, *, races=None, events=None, breakdown: bool = False,
                    printed_at: Optional[datetime] = None, divisions=None) -> bytes:
    """reports.team_scores on MeetPro pages: every section one after the
    other (as few pages as possible), a heading per scoring event (and per
    division, 2026-10-06)."""
    data = reports.team_scores(meet, races, events=events, breakdown=breakdown,
                               divisions=divisions)
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Team Scores")
    note = printing.describe_filter(meet, events=events, divisions=divisions)
    left2 = "Score Breakdown" if breakdown else "All final races"
    flow = _Flow(1, lambda: rep.page("Team Scores", left2, "Team Scores", note))
    for i, sec in enumerate(data["sections"]):
        table = _score_table(meet, "TEAM SCORES", sec["teams"], sec["incomplete"], breakdown)
        if i:
            flow.gap(10)
        flow.heading(f"Event {sec['event']} · {sec['event_name']}", flow.first_height(table))
        flow.table(table)
    if not data["sections"]:
        flow.note("No team scores yet.", HELVETICA, 9)
    if data["not_final"]:
        flow.gap(6)
        flow.note("Not final, not included: " + ", ".join(
            f"Event {r['race'][0]} {r['name']}" for r in data["not_final"]))
    flow.close()
    return rep.finish()


AWARD_COLS = [Col("award", "Award", 0, width=40),
              Col("place", "Pl", 62, "right", 20),
              Col("name", "Athlete", 70, width=160),
              Col("yr", "YR", 244, "center", 18),
              Col("bib", "#", 286, "right", 34),
              Col("team", "Team", 296, width=200),
              Col("time", "Time", CONTENT_W - 4, "right", 52)]
AWARD_TEAM_COLS = [Col("award", "Award", 0, width=60),
                   Col("team", "Team", 70, width=300),
                   Col("score", "Score", 420, "right", 30)]


def awards_pdf(meet: Meet, printed_at: Optional[datetime] = None, **kw) -> bytes:
    """The announcer's awards list (reports.award_winners; same arguments):
    per scoring event the individual awards, then the team awards."""
    data = reports.award_winners(meet, **kw)
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Awards")
    note = printing.describe_filter(meet, events=kw.get("events"), divisions=kw.get("divisions"))
    flow = _Flow(1, lambda: rep.page("Awards", "Announcer's list", "Awards", note))
    for i, sec in enumerate(data["sections"]):
        ind = Table(f"INDIVIDUAL AWARDS — TOP {sec['n']}", AWARD_COLS, [
            {"award": reports.ordinal(w["award"]), "place": str(w["place"] or ""),
             "name": _runner_name(w), "yr": year_label(w["grade"]), "bib": w["bib"],
             "team": w["school"], "time": w["time"], "_bold": ("award", "name")}
            for w in sec["individuals"]])
        if sec["short"]:
            ind.notes.append(f"{sec['short']} not awarded: too few eligible finishers")
        if any(w["award"] != w["place"] for w in sec["individuals"]):
            ind.notes.append(f"Pl = place in the {'division' if sec.get('division') else 'race'};"
                             " exhibition runners take no award")
        teams = Table(f"TEAM AWARDS — TOP {sec['m']}", AWARD_TEAM_COLS, [
            {"award": t["award_text"].replace(" Place", ""), "team": t["team"],
             "score": str(t["score"]), "_bold": ("award", "team")} for t in sec["teams"]],
            row_h=TEAM_ROW_H, baseline=10, rule_every=1)
        first = ind if ind.rows else teams
        if i:
            flow.gap(10)
        flow.heading(f"Event {sec['event']} · {sec['event_name']}", flow.first_height(first))
        if ind.rows:
            flow.table(ind)
            flow.gap(8)
        flow.table(teams)
    if not data["sections"]:
        flow.note("No awards: no final results yet.", HELVETICA, 9)
    if data["not_final"]:
        flow.gap(6)
        flow.note("Not final, not included: " + ", ".join(
            f"Event {r['race'][0]} {r['name']}" for r in data["not_final"]))
    flow.close()
    return rep.finish()


def split_scores_pdf(meet: Meet, race: Race, *, results: Optional[dict] = None,
                     breakdown: bool = False, printed_at: Optional[datetime] = None) -> bytes:
    """reports.split_scores: per scoring event a team table per split, with
    who had no time there."""
    data = reports.split_scores(meet, race, results)
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Split Scores")
    left1, left2 = _race_title(meet, race)
    right = "Split Scores" if data["official"] else "Split Scores — unofficial"
    flow = _Flow(1, lambda: rep.page(left1, left2, right))
    for i, sec in enumerate(data["sections"]):
        for j, sp in enumerate(sec["splits"]):
            table = _score_table(meet, sp["label"].upper(), sp["teams"], sp["incomplete"],
                                 breakdown)
            facts = [f"{printing._plural(sp['timed'], 'runner')} timed at {sp['label']}"]
            if sp["estimated"]:
                facts.append(f"{sp['estimated']} estimated")
            if sp["missing"]:
                facts.append(f"no {sp['label']} time, left out: bib "
                             + ", ".join(sp["missing"][:reports.MISSING_SHOWN])
                             + (f" and {len(sp['missing']) - reports.MISSING_SHOWN} more"
                                if len(sp["missing"]) > reports.MISSING_SHOWN else ""))
            table.notes.insert(0, printing.FILTER_SEP.join(facts))
            if i or j:
                flow.gap(10)
            if j == 0:
                flow.heading(f"Event {sec['event']} · {sec['event_name']}",
                             flow.first_height(table))
            if table.rows:
                flow.table(table)
            else:
                flow.note(f"{sp['label']}: no team scores. " + table.notes[0])
    if not data["sections"]:
        flow.note("No splits in this race.", HELVETICA, 9)
    flow.close()
    return rep.finish()


# Finish line sheets: write-in rows, at most FINISH_ROW_MAX tall (40 rows
# fill the body at ~13.9 pt, about the height of a ruled notebook line).
FINISH_ROW_MAX = 18.0
FINISH_X = {"place": 34.0, "bib_l": 44.0, "time_l": 124.0, "name_l": 234.0}


def finish_line_pdf(meet: Meet, *, races=None, rows: Optional[int] = None,
                    rows_per_page: int = reports.FINISH_ROWS_PER_PAGE,
                    printed_at: Optional[datetime] = None) -> bytes:
    """reports.finish_line_sheets: a page per sheet page, a ruled grid of
    Place | Bib | Time | Name / Notes with the places printed."""
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Finish Line Sheets",
                  official=False)
    sheets = reports.finish_line_sheets(meet, races, rows=rows, rows_per_page=rows_per_page)
    for sheet in sheets:
        for pg in sheet["pages"]:
            title = sheet["title"] + (" (cont'd)" if pg["page"] > 1 else "")
            p, top = rep.page(f"Race #{sheet['event']}", title, "Finish Line Sheet")
            p.text(LEFT + 1, top + 8, printing.FILTER_SEP.join([
                f"{sheet['entered']} entered", f"places {pg['first']}-{pg['last']} of "
                f"{sheet['rows']}", f"page {pg['page']} of {pg['pages']}"]), BOLD_OBLIQUE, 8)
            y0 = top + FILTER_H
            _bar(p, LEFT, y0, CONTENT_W, f"PLACES {pg['first']}-{pg['last']}")
            x = {k: LEFT + v for k, v in FINISH_X.items()}
            for label, lx, align in (("Place", x["place"], "right"), ("Bib", x["bib_l"] + 3, "left"),
                                     ("Time", x["time_l"] + 3, "left"),
                                     ("Name / Notes", x["name_l"] + 3, "left")):
                p.text(lx, y0 + 21.3, label, BOLD, HEAD_FONT, align=align)
            p.rect(LEFT, y0 + 23, CONTENT_W, 1.8, fill=0)
            y = y0 + HEAD_H
            n = pg["last"] - pg["first"] + 1
            row_h = min(FINISH_ROW_MAX, (BOTTOM - y) / max(1, sheet["rows_per_page"]))
            for i in range(n):
                p.text(x["place"], y + row_h - 4, str(pg["first"] + i), BOLD, 9, align="right")
                y += row_h
                p.line(LEFT, y, RIGHT, y, RULE_W if (i + 1) % 5 else 1.4)
            for vx in (x["bib_l"], x["time_l"], x["name_l"]):
                p.line(vx, y0 + HEAD_H, vx, y, RULE_W)
    if not sheets:
        p, top = rep.page("Finish Line Sheet", "", "Finish Line Sheet")
        p.text(LEFT + 1, top + 12, "No races.", HELVETICA, 9)
    return rep.finish()


# Labels: drawn straight on the sheet with pdfdoc (no page furniture: every
# inch is a label). Line styles from reports.LABEL_STYLES; a line too wide
# for the label shrinks to LABEL_MIN_SIZE, then is cut; a label whose lines
# don't fit its height is scaled down as a whole and centred.
LABEL_FONTS = {"bold": (BOLD, 10.0), "big": (BOLD, 20.0), "normal": (HELVETICA, 9.0),
               "small": (HELVETICA, 7.0)}
LABEL_PAD = 7.0
LABEL_MIN_SIZE = 6.0
LABEL_LEADING = 1.18


def _label_lines(lines: List[dict], width: float, height: float) -> List[Tuple[str, str, float]]:
    out = []
    for ln in lines:
        font, size = LABEL_FONTS.get(ln.get("style"), LABEL_FONTS["normal"])
        text = ln.get("text", "")
        while size > LABEL_MIN_SIZE and pdfdoc.measure(text, font, size) > width:
            size -= 0.5
        out.append((pdfdoc.fit(text, font, size, width), font, size))
    total = sum(s * LABEL_LEADING for _, _, s in out)
    if total > height:
        k = height / total
        out = [(pdfdoc.fit(t, f, s * k, width), f, s * k) for t, f, s in out]
    return out


def labels_pdf(meet: Meet, sheets: List[dict], title: str = "Labels",
               outline: bool = False) -> bytes:
    """reports.label_sheets on Avery 5160 (or the sheets' own geometry):
    a page per sheet, each label's lines centred in it. outline=True draws
    the label edges, for a test print on plain paper held up to a sheet."""
    doc = pdfdoc.Document(title=f"{meet.meet.name or meet.meet.id} - {title}",
                          producer=APP_NAME)
    for sheet in sheets:
        g = sheet["geometry"]
        page = doc.new_page()
        page.width, page.height = g["page_w"], g["page_h"]
        for lab in sheet["labels"]:
            if outline:
                page.rect(lab["x"], lab["top"], lab["w"], lab["h"], stroke=0.6, line_width=0.3)
            lines = _label_lines(lab["lines"], lab["w"] - 2 * LABEL_PAD, lab["h"] - 6)
            total = sum(s * LABEL_LEADING for _, _, s in lines)
            y = lab["top"] + (lab["h"] - total) / 2
            for text, font, size in lines:
                y += size * LABEL_LEADING
                page.text(lab["x"] + lab["w"] / 2, y - size * 0.25, text, font, size,
                          align="center")
    return doc.to_bytes()


# --------------------------------------------------------------- announcer

ANN_SIZE = 13.0          # read-aloud lines: large enough at arm's length
ANN_LINE = 22.0


def announcer_pdf(meet: Meet, printed_at: Optional[datetime] = None, **kw) -> bytes:
    """reports.announcer in large print (2026-10-03, user's ask): per
    scoring event the top N finishers then the teams, each list Nth -> 1st
    so the announcer reads straight down to the winner. Same arguments as
    reports.announcer (top, teams = count or "all", races, events)."""
    data = reports.announcer(meet, **kw)
    rep = _Report(meet, printed_at, f"{meet.meet.name or meet.meet.id} - Announcer")
    teams = data["teams"]
    sub = (f"Top {data['top']} · "
           + ("all teams" if isinstance(teams, str) else f"top {teams} teams")
           + " · read from the bottom up")
    note = printing.describe_filter(meet, events=kw.get("events"), divisions=kw.get("divisions"))
    state = {"page": None, "y": BOTTOM}

    def new_page():
        state["page"], state["y"] = rep.page("Awards", sub, "Announcer", note)

    def room(h: float) -> None:
        if state["page"] is None or state["y"] + h > BOTTOM:
            new_page()

    def bar(text: str) -> None:
        room(BAR_H + ANN_LINE + 6)
        _bar(state["page"], LEFT, state["y"] + 4, CONTENT_W, text)
        state["y"] += BAR_H + 10

    def line(text: str, bold_prefix: str) -> None:
        room(ANN_LINE)
        p, y = state["page"], state["y"] + ANN_SIZE
        head, _, rest = text.partition(": ")
        p.text(LEFT + 6, y, head + ":", BOLD, ANN_SIZE)
        x = LEFT + 6 + pdfdoc.measure(head + ": ", BOLD, ANN_SIZE)
        p.text(x, y, pdfdoc.fit(rest, HELVETICA, ANN_SIZE, RIGHT - x - 6), HELVETICA, ANN_SIZE)
        p.line(LEFT, state["y"] + ANN_LINE - 3, RIGHT, state["y"] + ANN_LINE - 3, 0.3)
        state["y"] += ANN_LINE

    for sec in data["sections"]:
        ind, tm = reports.announcer_lines(sec)
        # One event per page: the announcer turns the page between races
        # and a race's list never splits across a page break mid-read.
        new_page()
        if ind:
            bar(f"{sec['event_name'].upper()} — INDIVIDUALS ({len(ind)})")
            for l in ind:
                line(l, "")
            if sec["short"]:
                room(ANN_LINE)
                state["page"].text(LEFT + 6, state["y"] + 10,
                                   f"{sec['short']} not awarded: too few eligible finishers",
                                   BOLD_OBLIQUE, 8)
                state["y"] += 14
        if tm:
            state["y"] += 6
            bar(f"{sec['event_name'].upper()} — TEAMS ({len(tm)})")
            for l in tm:
                line(l, "")
    if not data["sections"]:
        room(ANN_LINE)
        state["page"].text(LEFT + 6, state["y"] + 12, "No awards: no final results yet.",
                           HELVETICA, 10)
    if data["not_final"]:
        room(14)
        state["page"].text(LEFT + 6, state["y"] + 12, pdfdoc.fit(
            "Not final, not included: " + ", ".join(
                f"Event {r['race'][0]} {r['name']}" for r in data["not_final"]),
            BOLD_OBLIQUE, 8, CONTENT_W - 12), BOLD_OBLIQUE, 8)
    return rep.finish()
