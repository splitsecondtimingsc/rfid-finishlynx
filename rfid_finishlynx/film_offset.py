"""Learn how far the finish mat's RFID time sits from the photo-finish time.

FinishLynx first shows each athlete the time the middleware sent it (the
mat read). When the operator evaluates the image, the .lif it saves carries
the exact torso time instead. Comparing our finish reads with that file
gives, per athlete, how much later (positive) or earlier the chip crossed
the mat than the body crossed the line. The average over the meet is the
"film offset"; the console subtracts it from every mat finish it reports
and sends, so athletes who are never evaluated get film-like times too.
It is learned PER RACE (user's rule 2026-10-01): each pane uses its own
evaluated finishes, and a new race starts at zero until its first
evaluations land in FinishLynx's .lif. A manual override is still one
number for every race.
The mean is ROLLING (user's rule 2026-10-03, "finishers slow down as the
race goes on"): a finish is corrected by the mean of the ROLLING_WINDOW
evaluated finishes nearest before it in race time - the latest 20 for a
read arriving now; a finisher ahead of the 20th evaluation uses the race's
first 20.

Telling an evaluated row from one FinishLynx merely copied from us: copied
rows all sit at one constant distance from our raw time (FinishLynx's own
clock frame and 0.01-s rounding - measured -0.028 s on 2026-09-18, spread
+-0.005), so the densest cluster of differences is the "unevaluated"
baseline and everything outside it is a real evaluation. A row that we sent
already corrected is compared after undoing that correction, so a learned
offset never feeds itself back in.

Sign: value > 0 means the RFID read is LATER than the film time; the
correction subtracts it.
"""

import csv
import hashlib
import logging
import math
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CLUSTER_WIDTH_SEC = 0.012     # copied rows differ from our raw time by rounding only
CLUSTER_MIN_ROWS = 3          # fewer than this is not a baseline, it is evaluated rows
CLUSTER_MIN_SHARE = 0.25      # ...and it must be a real share of the file, not a coincidence,
NEAR_ZERO_SEC = 0.1           # unless it sits where a clock-frame constant lives: near zero
BASELINE_TOL_SEC = 0.007      # a row this close to the baseline was not evaluated
MAX_PLAUSIBLE_SEC = 5.0       # beyond this the .lif row is not the same crossing
ANTENNA_MIN_SAMPLES = 5       # an antenna's own mean is used once it has this many
ROLLING_WINDOW = 20           # evaluated finishes averaged for one correction


def parse_race_time(text: str) -> Optional[float]:
    """'12:34.56', '1:02:03.4', '45.67' -> seconds; None when blank/garbage."""
    t = (text or "").strip()
    if not t:
        return None
    secs = 0.0
    try:
        for part in t.split(":"):
            secs = secs * 60 + float(part)
    except ValueError:
        return None
    return secs


def parse_tod(text: str) -> Optional[float]:
    """'17:11:13.3249' -> seconds since midnight, or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*", text or "")
    if not m:
        return None
    frac = float("0." + m.group(4)) if m.group(4) else 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + frac


def parse_lif(text: str) -> Tuple[Optional[float], Dict[str, float]]:
    """(FinishLynx start time-of-day in seconds, {bib: race time}) from a
    .lif; the start is field 11 of the header, times are field 7 of rows.
    Rows without a bib or a time are skipped."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None, {}
    header = next(csv.reader([lines[0]]))
    start = parse_tod(header[10]) if len(header) > 10 else None
    rows: Dict[str, float] = {}
    for line in lines[1:]:
        row = next(csv.reader([line]))
        if len(row) < 7 or not row[1].strip():
            continue
        t = parse_race_time(row[6])
        if t is not None:
            rows[row[1].strip()] = t
    return start, rows


def seconds_of_day(ts: datetime) -> float:
    return ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1e6


def densest_cluster(values: List[float], width: float) -> List[float]:
    """The largest subset of `values` that fits inside a window of `width`."""
    vals = sorted(values)
    best: List[float] = []
    j = 0
    for i in range(len(vals)):
        while vals[i] - vals[j] > width:
            j += 1
        if i - j + 1 > len(best):
            best = vals[j:i + 1]
    return best


class FilmOffset:
    """Learned offset between RFID finish reads and film times, per race."""

    def __init__(self):
        self.samples: Dict[str, dict] = {}     # "race:bib" -> sample
        self.bases: Dict[int, float] = {}      # race key -> unevaluated baseline
        self.unevaluated: Dict[int, int] = {}  # race key -> copied rows seen
        self.outliers: Dict[int, int] = {}     # race key -> rows beyond plausibility
        self.files: Dict[int, str] = {}        # race key -> .lif read
        self.manual: Optional[float] = None    # operator override, seconds
        self.apply: bool = True
        self._mtimes: Dict[str, float] = {}
        # Reset is a watershed: the evaluated .lif times present at that
        # moment are frozen and never count again; only rows that change
        # afterwards (re-evaluated or new athletes) become samples.
        self.reset_at: Optional[datetime] = None
        self.frozen: Dict[str, float] = {}     # "race:bib" -> .lif time at reset
        self.stale: Dict[int, int] = {}        # race -> rows ignored as pre-reset
        self.updated_at: Optional[datetime] = None

    # ------------------------------------------------------------ values

    def _values(self, race: Optional[int] = None) -> List[float]:
        return [s["value"] for s in self.samples.values() if race is None or s["race"] == race]

    def learned(self, race: Optional[int]) -> Optional[float]:
        """Mean of one race's evaluated finishes, None when it has none."""
        vals = self._values(race) if race is not None else []
        return sum(vals) / len(vals) if vals else None

    def spread(self, race: Optional[int]) -> Optional[float]:
        vals = self._values(race) if race is not None else []
        if len(vals) < 2:
            return None
        mean = sum(vals) / len(vals)
        return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))

    def antenna_means(self, race: int) -> Dict[int, Tuple[float, int]]:
        """antenna port -> (mean value, sample count) over the race's
        evaluated finishes heard on that port (port 0 = unknown, e.g. a
        split station, is left out)."""
        acc: Dict[int, List[float]] = {}
        for s in self.samples.values():
            if s["race"] == race:
                acc.setdefault(int(s.get("antenna") or 0), []).append(s["value"])
        return {a: (sum(v) / len(v), len(v)) for a, v in acc.items() if a}

    @staticmethod
    def rolling_mean(samples: List[dict], at: Optional[float] = None) -> Optional[float]:
        """Mean of the ROLLING_WINDOW samples nearest before race time `at`
        (by the read's race time); the latest ones when `at` is None, the
        first ones for a finish ahead of them all. None without samples."""
        if not samples:
            return None
        ordered = sorted(samples, key=lambda s: s.get("raw", 0.0))
        n = len(ordered)
        before = n if at is None else sum(1 for s in ordered if s.get("raw", 0.0) <= at)
        end = max(before, min(ROLLING_WINDOW, n))
        window = ordered[max(0, end - ROLLING_WINDOW):end]
        return sum(s["value"] for s in window) / len(window)

    def correction(self, antenna: int = 0, race: Optional[int] = None,
                   at: Optional[float] = None) -> float:
        """Seconds to subtract from a finish read in `race` at race time
        `at` (seconds after the gun; None = a read arriving now).

        0 when applying is off, and for a race with nothing evaluated yet
        (or no race at all: a held read) - each race learns its own. The
        mat's antennas sit at different distances from the line, so a port
        with enough evaluated finishes in the race uses its own rolling
        mean, else the race's. A manual override is one number for everything.
        """
        if not self.apply:
            return 0.0
        if self.manual is not None:
            return float(self.manual)
        mine = [s for s in self.samples.values() if race is not None and s["race"] == race]
        if not mine:
            return 0.0
        if antenna:
            port = [s for s in mine if int(s.get("antenna") or 0) == int(antenna)]
            if len(port) >= ANTENNA_MIN_SAMPLES:
                return float(self.rolling_mean(port, at))
        return float(self.rolling_mean(mine, at))

    def race_stats(self, key: int) -> dict:
        vals = self._values(key)
        return {
            "race": key,
            "samples": len(vals),
            "mean": self.learned(key),
            "spread": self.spread(key),
            "applied": self.correction(0, key),        # latest ROLLING_WINDOW
            "window": ROLLING_WINDOW,
            "antennas": [{"antenna": a, "samples": n, "mean": m,
                          "applied": self.apply and self.manual is None and n >= ANTENNA_MIN_SAMPLES}
                         for a, (m, n) in sorted(self.antenna_means(key).items())],
            "unevaluated": self.unevaluated.get(key, 0),
            "stale": self.stale.get(key, 0),
            "outliers": self.outliers.get(key, 0),
            "base": self.bases.get(key),
            "file": os.path.basename(self.files[key]) if key in self.files else None,
        }

    def stats(self) -> dict:
        keys = sorted(set(self.bases) | set(self.unevaluated) | {s["race"] for s in self.samples.values()})
        return {
            "apply": self.apply,
            "source": "manual" if self.manual is not None else ("learned" if self.samples else "none"),
            "manual": self.manual,
            "samples": len(self.samples),
            "unevaluated": sum(self.unevaluated.values()),
            "stale": sum(self.stale.values()),
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "antenna_min_samples": ANTENNA_MIN_SAMPLES,
            "races": [self.race_stats(k) for k in keys],
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    # ---------------------------------------------------------- operator

    def set_manual(self, value: Optional[float]):
        self.manual = None if value is None else float(value)

    def set_apply(self, apply: bool):
        self.apply = bool(apply)

    def reset(self, now: Optional[datetime] = None):
        """Forget every learned sample and start over from now: every
        evaluated time already in the files is frozen so a later re-save
        cannot teach it again; only evaluations made after this count.
        The override, if any, and the clock-frame baselines stay."""
        self.reset_at = now or datetime.now()
        # Only the evaluated rows are frozen: copied rows carry nothing and
        # must stay visible so the clock-frame baseline can still be found.
        self.frozen = {key: float(s["lif"]) for key, s in self.samples.items()}
        self.samples.clear()
        self.unevaluated.clear()
        self.stale.clear()
        self.outliers.clear()
        self.updated_at = None

    def forget_race(self, key: int):
        """A pane was deleted: its samples and bookkeeping go with it."""
        for k in [k for k, s in self.samples.items() if s["race"] == key]:
            del self.samples[k]
        for d in (self.bases, self.unevaluated, self.outliers, self.files, self.stale):
            d.pop(key, None)

    def clear(self):
        """Meet closed: back to nothing learned, no override, applying."""
        self.reset()
        self.reset_at = None
        self.frozen.clear()
        self.bases.clear()
        self.files.clear()
        self._mtimes.clear()
        self.manual = None
        self.apply = True

    # ------------------------------------------------------------ learning

    def learn(self, race_key: int, fl_start: float, lif_rows: Dict[str, float],
              finish_reads: Dict[str, Tuple[datetime, float]], source: str = "") -> bool:
        """Update the samples for one race from a parsed .lif.

        finish_reads: bib -> (raw read time, offset that was subtracted when
        it was sent to FinishLynx[, antenna port]). Returns True when
        anything changed.
        """
        rows = []
        stale = 0
        for bib, lif_t in lif_rows.items():
            if bib not in finish_reads:
                continue
            frozen = self.frozen.get(f"{race_key}:{bib}")
            if frozen is not None and abs(frozen - lif_t) < 0.0005:
                stale += 1                    # as it was at the last reset
                continue
            fr = finish_reads[bib]
            raw_ts, sent = fr[0], fr[1]
            antenna = int(fr[2]) if len(fr) > 2 and fr[2] else 0
            raw_race = seconds_of_day(raw_ts) - fl_start
            diff = raw_race - lif_t
            rows.append((bib, raw_race, lif_t, diff, diff - sent, antenna))
        # The copied-row baseline is a property of FinishLynx's clock frame,
        # stable for a session, so once known it is kept; a fresh search
        # only wins with a cluster too big to be evaluated rows landing
        # close together by chance.
        cluster = densest_cluster([r[4] for r in rows], CLUSTER_WIDTH_SEC)
        mid = cluster[len(cluster) // 2] if cluster else 0.0
        if len(cluster) >= CLUSTER_MIN_ROWS and (len(cluster) >= CLUSTER_MIN_SHARE * len(rows)
                                                 or abs(mid) <= NEAR_ZERO_SEC):
            base = mid
        elif race_key in self.bases:
            base = self.bases[race_key]
        elif self.bases:
            known = sorted(self.bases.values())
            base = known[len(known) // 2]
        else:
            base = 0.0

        new_samples: Dict[str, dict] = {}
        unevaluated = outliers = 0
        for bib, raw_race, lif_t, diff, adjusted, antenna in rows:
            if abs(adjusted - base) <= BASELINE_TOL_SEC:
                unevaluated += 1
                continue
            value = diff - base
            if abs(value) > MAX_PLAUSIBLE_SEC:
                outliers += 1
                continue
            new_samples[f"{race_key}:{bib}"] = {
                "race": race_key, "bib": bib, "raw": round(raw_race, 3),
                "lif": round(lif_t, 3), "value": round(value, 3), "antenna": antenna,
            }

        old = {k: v for k, v in self.samples.items() if v["race"] == race_key}
        changed = (old != new_samples or self.bases.get(race_key) != base
                   or self.unevaluated.get(race_key) != unevaluated
                   or self.stale.get(race_key) != stale
                   or self.outliers.get(race_key) != outliers)
        for k in old:
            del self.samples[k]
        self.samples.update(new_samples)
        self.bases[race_key] = base
        self.unevaluated[race_key] = unevaluated
        self.stale[race_key] = stale
        self.outliers[race_key] = outliers
        if source:
            self.files[race_key] = source
        if changed:
            self.updated_at = datetime.now()
            ants = ", ".join(f"A{a} {m:+.3f} ({n})" for a, (m, n) in sorted(self.antenna_means(race_key).items()))
            logger.info(f"Film offset: race {race_key} {len(new_samples)} evaluated, "
                        f"{unevaluated} copied, {stale} from before the reset, {outliers} implausible; race offset "
                        f"{self.learned(race_key):+.3f} s, latest {ROLLING_WINDOW} "
                        f"{self.rolling_mean([s for s in new_samples.values()]):+.3f} s"
                        + (f"; by antenna {ants}" if ants else "")
                        if new_samples else
                        f"Film offset: race {race_key} nothing evaluated yet ({unevaluated} copied rows)")
        return changed

    def scan_file(self, race_key: int, path: str,
                  finish_reads: Dict[str, Tuple[datetime, float]]) -> bool:
        """Learn from one .lif if it changed since last time and FinishLynx
        (not this console) wrote it. Returns True when samples changed."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if self._mtimes.get(path) == mtime:
            return False
        self._mtimes[path] = mtime
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
        except OSError as e:
            logger.debug(f"Film offset: cannot read {path}: {e}")
            return False
        side = path + ".mirror"
        if os.path.exists(side):
            try:
                with open(side) as f:
                    ours = f.read().strip() == hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            except OSError:
                ours = False
            if ours:
                return False          # our own export, nothing to learn from
        fl_start, rows = parse_lif(text)
        if fl_start is None or not rows:
            return False
        return self.learn(race_key, fl_start, rows, finish_reads, source=path)

    # ------------------------------------------------------------ state

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "bases": {str(k): v for k, v in self.bases.items()},
            "unevaluated": {str(k): v for k, v in self.unevaluated.items()},
            "outliers": {str(k): v for k, v in self.outliers.items()},
            "files": {str(k): v for k, v in self.files.items()},
            "manual": self.manual,
            "apply": self.apply,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "frozen": self.frozen,
            "stale": {str(k): v for k, v in self.stale.items()},
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "FilmOffset":
        fo = cls()
        if not d:
            return fo
        fo.samples = {str(k): dict(v) for k, v in (d.get("samples") or {}).items()}
        fo.bases = {int(k): float(v) for k, v in (d.get("bases") or {}).items()}
        fo.unevaluated = {int(k): int(v) for k, v in (d.get("unevaluated") or {}).items()}
        fo.outliers = {int(k): int(v) for k, v in (d.get("outliers") or {}).items()}
        fo.files = {int(k): str(v) for k, v in (d.get("files") or {}).items()}
        fo.manual = float(d["manual"]) if d.get("manual") is not None else None
        fo.apply = bool(d.get("apply", True))
        for attr in ("updated_at", "reset_at"):
            if d.get(attr):
                try:
                    setattr(fo, attr, datetime.fromisoformat(d[attr]))
                except ValueError:
                    pass
        fo.frozen = {str(k): float(v) for k, v in (d.get("frozen") or {}).items()}
        fo.stale = {int(k): int(v) for k, v in (d.get("stale") or {}).items()}
        return fo
