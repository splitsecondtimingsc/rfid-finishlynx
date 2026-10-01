"""The .lif contract for live splits: what MeetPro's Splits Auto-Pull reads.

Splits reach MeetPro (and through it MileSplit Live) only through the
EEE-R-HH.lif files in the Lynx results folder; LapTime carries no split
number (see test_split_laptime_wire). These tests pin the file the console
streams against the layout FinishLynx itself writes, so a change that would
move a split into the wrong MeetPro column fails here, not at a meet.

Ground truth: tests/fixtures/lynx/fl-001-1-01-finish-only.lif is FinishLynx's
own 001-1-01.lif from 2026-09-09 (names anonymised): 11-field header,
19-field rows, splits in field 11 as "cumulative (lap)".

The findings this suite started with (a missed split filed under the
wrong point, the finish stored as a split, athletes on course invisible,
commas shifting fields) are fixed by lif_merge.py; the tests below now
hold those rules in place.
Hardware-free: python -m unittest tests.test_split_lif_contract
"""
import csv
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.film_offset import parse_race_time
from rfid_finishlynx.races import RaceManager

HERE = os.path.dirname(os.path.abspath(__file__))
FL_FIXTURE = os.path.join(HERE, "fixtures", "lynx", "fl-001-1-01-finish-only.lif")

HEADER_FIELDS = 11
ROW_FIELDS = 19
SPLITS = 10          # field 11: "cum (lap),cum (lap),...,finish (lap)"
TIME = 6             # field 7: the result time
DELTA = 8            # field 9: FinishLynx writes the gap to the PREVIOUS place

ATHLETES = {
    "1": {"last_name": "Ames", "first_name": "Ava", "school": "Red", "gender": "F"},
    "2": {"last_name": "Bell", "first_name": "Bo", "school": "Blue", "gender": "F"},
    "3": {"last_name": "Cole", "first_name": "Cy", "school": "Red", "gender": "F"},
}
GUN = datetime(2026, 9, 20, 9, 0, 0)


def parse(text):
    """(header, rows) the way a CSV-aware reader such as MeetPro sees them."""
    lines = [l for l in text.splitlines() if l.strip()]
    return next(csv.reader([lines[0]])), [next(csv.reader([l])) for l in lines[1:]]


def entries(field):
    """'6:00.000 (6:00.000),,18:20.5 (6:10.2)' -> [(cum, lap) | None, ...]; '' -> []"""
    if not field:
        return []
    out = []
    for e in field.split(","):
        e = e.strip()
        if not e:
            out.append(None)
            continue
        cum, _, lap = e.partition(" (")
        out.append((parse_race_time(cum), parse_race_time(lap.rstrip(")"))))
    return out


def make_mgr(athletes=None):
    book = athletes or ATHLETES
    return RaceManager(athlete_lookup=lambda bib: book.get(bib), on_change=lambda k: None,
                       chute_reader="CHUTE", finish_readers={"FINISH"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                       point_distances={"S1": 1609.0, "S2": 3218.0}, state_path=None)


def race_with_splits(mgr=None):
    """Three finishers: 1 has both miles, 2 missed Mile 1, 3's Mile 2 read
    arrived before its Mile 1 read (a late LoRa batch)."""
    mgr = mgr or make_mgr()
    key = mgr.create_race("Girls 5K", event=2, gender="F", distance="5000")
    mgr.set_gun(key, GUN, "camera start")
    at = lambda s: GUN + timedelta(seconds=s)
    mgr.ingest("1", at(360.0), "S1")
    mgr.ingest("1", at(730.25), "S2")
    mgr.ingest("1", at(1100.5), "FINISH")
    mgr.ingest("2", at(740.0), "S2")
    mgr.ingest("2", at(1110.0), "FINISH")
    mgr.ingest("3", at(745.0), "S2")
    mgr.ingest("3", at(365.0), "S1")
    mgr.ingest("3", at(1125.0), "FINISH")
    return mgr, key


def load_tool(name):
    path = os.path.join(HERE, os.pardir, "tools", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"tools_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFinishLynxGroundTruth(unittest.TestCase):
    """What FinishLynx itself writes - the layout MeetPro was built against."""

    def setUp(self):
        with open(FL_FIXTURE, newline="") as f:
            self.header, self.rows = parse(f.read())

    def test_shape(self):
        self.assertEqual(len(self.header), HEADER_FIELDS)
        for row in self.rows:
            self.assertEqual(len(row), ROW_FIELDS, row)

    def test_splits_field_ends_with_the_finish(self):
        for row in self.rows:
            last = entries(row[SPLITS])[-1]
            self.assertAlmostEqual(last[0], parse_race_time(row[TIME]), places=3)

    def test_delta_field_is_gap_to_previous_place(self):
        times = [parse_race_time(r[TIME]) for r in self.rows]
        for prev, row in zip(times, self.rows[1:]):
            self.assertAlmostEqual(parse_race_time(row[DELTA]), parse_race_time(row[TIME]) - prev, places=3)


class TestStreamedLif(unittest.TestCase):
    """races.lif_content - the file MeetPro auto-pulls during the race."""

    def setUp(self):
        self.mgr, self.key = race_with_splits()
        self.header, self.rows = parse(self.mgr.lif_content(self.key))
        self.by_bib = {r[1]: r for r in self.rows}

    def test_file_name_is_finishlynx_name(self):
        self.assertEqual(self.mgr.lif_name(self.mgr.races[self.key]), "002-1-01.lif")

    def test_shape_matches_finishlynx(self):
        self.assertEqual(len(self.header), HEADER_FIELDS)
        self.assertEqual(self.header[:3], ["2", "1", "1"])
        self.assertEqual(self.header[10], "09:00:00.000")          # gun, time of day
        for row in self.rows:
            self.assertEqual(len(row), ROW_FIELDS, row)

    def test_one_entry_per_split_point_the_athlete_has(self):
        """Split points only - no finish entry (MeetPro's Splits Auto-Pull
        stores a trailing finish as a split when a slot is free)."""
        self.assertEqual(len(entries(self.by_bib["1"][SPLITS])), 2)
        self.assertEqual(len(entries(self.by_bib["3"][SPLITS])), 2)

    def test_full_athlete_cumulative_and_laps(self):
        e = entries(self.by_bib["1"][SPLITS])
        self.assertEqual([c for c, _ in e], [360.0, 730.25])
        self.assertEqual([l for _, l in e], [360.0, 370.25])

    def test_finish_is_never_a_split_entry(self):
        for row in self.rows:
            fin = parse_race_time(row[TIME])
            self.assertNotIn(fin, [e[0] for e in entries(row[SPLITS]) if e], row)

    def test_missing_split_holds_the_later_ones(self):
        """Bib 2 missed Mile 1. MeetPro skips an empty entry without keeping
        its slot, so its Mile 2 is held back until a Mile 1 exists (read,
        typed or accepted estimate) instead of being filed as Mile 1."""
        self.assertEqual(self.by_bib["2"][SPLITS], "")
        mgr, key = race_with_splits()
        mgr.add_manual_read(key, "2", GUN + timedelta(seconds=362), "S1", estimated=True)
        _, rows = parse(mgr.lif_content(key))
        e = entries({r[1]: r for r in rows}["2"][SPLITS])
        self.assertEqual([c for c, _ in e], [362.0, 740.0])

    def test_columns_follow_course_order_not_arrival(self):
        e = entries(self.by_bib["3"][SPLITS])
        self.assertEqual([c for c, _ in e], [365.0, 745.0])

    def test_first_crossing_counts_for_a_duplicate(self):
        mgr, key = race_with_splits()
        mgr.add_manual_read(key, "1", GUN + timedelta(seconds=380), "S1", note="second Mile 1")
        _, rows = parse(mgr.lif_content(key))
        e = entries({r[1]: r for r in rows}["1"][SPLITS])
        self.assertEqual(e[0][0], 360.0)

    def test_on_course_athletes_are_listed_with_their_splits(self):
        """An athlete still racing is a row with blank place and time plus
        the splits so far - MeetPro imports it as ENR, so Mile 1 reaches
        MileSplit Live during the race, not after the finish."""
        mgr, key = race_with_splits()
        ATHLETES["4"] = {"last_name": "Dunn", "first_name": "Di", "school": "Blue", "gender": "F"}
        try:
            mgr.ingest("4", GUN + timedelta(seconds=400), "S1")
            _, rows = parse(mgr.lif_content(key))
        finally:
            del ATHLETES["4"]
        row = {r[1]: r for r in rows}["4"]
        self.assertEqual((row[0], row[TIME]), ("", ""))
        self.assertEqual(row[SPLITS], "6:40.000 (6:40.000)")
        self.assertEqual(rows[-1][1], "4")                      # after every finisher

    def test_finish_only_mode(self):
        _, rows = parse(self.mgr.lif_content(self.key, splits=False))
        for row in rows:
            self.assertEqual(row[SPLITS], "")

    def test_delta_field_matches_finishlynx(self):
        """Field 9 is the gap to the previous place, as FinishLynx writes it."""
        times = [parse_race_time(r[TIME]) for r in self.rows]
        for prev, row in zip(times, self.rows[1:]):
            self.assertAlmostEqual(parse_race_time(row[DELTA]), parse_race_time(row[TIME]) - prev, places=3)

    def test_comma_in_a_school_name_does_not_shift_fields(self):
        """'Hammond, SC' is quoted, so it stays one field and every later
        column (splits included) stays where MeetPro reads it."""
        book = dict(ATHLETES)
        book["1"] = dict(book["1"], school="Hammond, SC")
        mgr, key = race_with_splits(make_mgr(book))
        _, rows = parse(mgr.lif_content(key))
        for row in rows:
            self.assertEqual(len(row), ROW_FIELDS, row)


class TestStreamOwnership(unittest.TestCase):
    """The console and FinishLynx write the SAME file name. Once FinishLynx
    saves its own .lif for the event the console stops streaming, so from
    then on MeetPro only sees FinishLynx's rows - which carry no mile splits
    unless FinishLynx got them via LapTime. When FinishLynx writes that file
    during a live race is measured with bench/split_lab.py watch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_streams_until_finishlynx_saves_then_leaves_its_file_alone(self):
        mgr, key = race_with_splits()
        path = mgr.write_lif(key, self.dir)
        self.assertIsNotNone(path)
        with open(FL_FIXTURE, newline="") as f:
            fl_text = f.read()
        with open(path, "w", newline="") as f:                   # FinishLynx saves
            f.write(fl_text)
        ATHLETES["4"] = {"last_name": "Dunn", "first_name": "Di", "school": "Blue", "gender": "F"}
        try:
            mgr.ingest("4", GUN + timedelta(seconds=400), "S1")
            mgr.ingest("4", GUN + timedelta(seconds=1200), "FINISH")
            self.assertIsNone(mgr.write_lif(key, self.dir))
        finally:
            del ATHLETES["4"]
        with open(path, newline="") as f:
            _, rows = parse(f.read())
        self.assertTrue(all(len(entries(r[SPLITS])) == 1 for r in rows))   # no mile splits left

    def test_resumes_streaming_after_the_mirror_owns_the_file_again(self):
        mgr, key = race_with_splits()
        path = mgr.write_lif(key, self.dir)
        with open(path, newline="") as f:
            first = f.read()
        mgr.ingest("2", GUN + timedelta(seconds=361), "S1")       # late Mile 1 for bib 2
        self.assertEqual(mgr.write_lif(key, self.dir), path)
        with open(path, newline="") as f:
            self.assertNotEqual(f.read(), first)


if __name__ == "__main__":
    unittest.main()
