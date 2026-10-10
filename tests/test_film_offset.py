"""Mat-vs-film finish offset learned from FinishLynx's saved .lif files.
Runs without pytest: python -m unittest tests.test_film_offset"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.film_offset import (
    FilmOffset, densest_cluster, parse_lif, parse_race_time, parse_tod, seconds_of_day,
)
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 9, 18, 16, 46, 48, 986000)          # camera start (PC clock)
FL_START = "16:46:49.0515"                                # FinishLynx's own start
BASE = -0.028                                             # copied rows: raw - lif


def fmt_race(secs: float) -> str:
    m, s = divmod(round(secs, 2), 60)
    return f"{int(m)}:{s:05.2f}"


def lif_text(rows, start=FL_START, event=1):
    """rows: [(bib, race time seconds)] -> FinishLynx-style .lif text."""
    lines = [f"{event},1,1,MS Girls,,,,,,2,{start}"]
    for place, (bib, t) in enumerate(rows, 1):
        lines.append(f"{place},{bib},,Last,First,School,{fmt_race(t)},,,,\"{fmt_race(t)} ({fmt_race(t)})\",{start[:11]},F,,,,,,")
    return "\r\n".join(lines) + "\r\n"


def read_ts(race_secs: float) -> datetime:
    """A mat read whose raw time is race_secs after FinishLynx's start."""
    sod = parse_tod(FL_START) + race_secs
    return datetime(2026, 9, 18) + timedelta(seconds=sod)


class TestParsing(unittest.TestCase):

    def test_race_time_and_tod(self):
        self.assertAlmostEqual(parse_race_time("11:53.44"), 713.44)
        self.assertAlmostEqual(parse_race_time("1:02:03.5"), 3723.5)
        self.assertAlmostEqual(parse_race_time("45.6"), 45.6)
        self.assertIsNone(parse_race_time(""))
        self.assertIsNone(parse_race_time("DNF"))
        self.assertAlmostEqual(parse_tod("17:11:13.3249"), 61873.3249)
        self.assertIsNone(parse_tod(""))

    def test_parse_lif_real_layout(self):
        text = ("2,1,1,MS Boys,,,,,,2,17:11:13.3249\r\n"
                "1,546,,Timothy,Quinn,St. Josephs,11:53.44,,11:53.44,,11:53.44 (11:53.44),17:11:13.33,M,,,11:53.44,11:53.44,10.09,11:53.43\r\n"
                "2,207,,Santacrose,Dominic,Imago Dei,11:58.22,,4.78,,\"11:58.22 (11:58.22)\",17:11:13.33,M,,,4.78,4.78,10.02,11:58.22\r\n"
                ",999,,No,Time,School,,,,,,,,,,,,,\r\n")
        start, rows = parse_lif(text)
        self.assertAlmostEqual(start, 17 * 3600 + 11 * 60 + 13.3249)
        self.assertEqual(rows, {"546": 713.44, "207": 718.22})

    def test_parse_lif_without_start(self):
        start, rows = parse_lif("3,1,1,Varsity Boys,,,,,,5000,\r\n")
        self.assertIsNone(start)
        self.assertEqual(rows, {})

    def test_densest_cluster(self):
        vals = [-0.028, -0.031, -0.024, 0.65, 0.71, 0.58, -0.026, 0.9]
        self.assertEqual(sorted(densest_cluster(vals, 0.012)), [-0.031, -0.028, -0.026, -0.024])
        self.assertEqual(densest_cluster([], 0.012), [])


class TestLearning(unittest.TestCase):
    """The 2026-09-18 pattern: copied rows all at raw - lif = -0.028 +-
    0.005 (FinishLynx's frame and 0.01 rounding); evaluated rows scatter
    around +0.65."""

    def _reads(self, spec):
        # spec: bib -> (raw race secs, sent offset)
        return {bib: (read_ts(raw), sent) for bib, (raw, sent) in spec.items()}

    def test_copied_rows_are_ignored_and_evaluated_rows_average(self):
        fo = FilmOffset()
        reads = self._reads({"1": (828.462, 0), "2": (849.294, 0), "3": (861.306, 0),
                             "4": (911.975, 0), "5": (921.498, 0)})
        rows = {"1": 828.462 - BASE, "2": 849.294 - BASE, "3": 861.306 - BASE,   # copied
                "4": 911.975 - 0.725, "5": 921.498 - 0.648}                      # evaluated
        rows = {k: round(v, 2) for k, v in rows.items()}
        self.assertTrue(fo.learn(1, parse_tod(FL_START), rows, reads))
        self.assertEqual(fo.unevaluated[1], 3)
        self.assertEqual(len(fo.samples), 2)
        self.assertAlmostEqual(fo.bases[1], BASE, delta=0.006)
        # value = (raw - lif) - base: the gap as FinishLynx would show it,
        # i.e. what to subtract so the copied time lands on the film time
        self.assertAlmostEqual(fo.learned(1), (0.725 + 0.648) / 2 - BASE, delta=0.012)
        self.assertAlmostEqual(fo.correction(0, 1), fo.learned(1))
        # re-learning the same file changes nothing
        self.assertFalse(fo.learn(1, parse_tod(FL_START), rows, reads))

    def test_rows_we_sent_corrected_do_not_feed_back(self):
        fo = FilmOffset()
        # first race taught 0.65; the second race's reads went out corrected
        reads = self._reads({"1": (700.0, 0.65), "2": (710.0, 0.65), "3": (720.0, 0.65), "4": (730.0, 0.65)})
        # FinishLynx copied the corrected times: lif = raw - 0.65 - base
        rows = {b: round(raw - 0.65 - BASE, 2) for b, (raw, _) in
                {"1": (700.0, 0), "2": (710.0, 0), "3": (720.0, 0)}.items()}
        rows["4"] = round(730.0 - 0.70, 2)                      # evaluated: true gap 0.70
        fo.learn(2, parse_tod(FL_START), rows, reads)
        self.assertEqual(fo.unevaluated[2], 3)
        self.assertEqual([s["bib"] for s in fo.samples.values()], ["4"])
        self.assertAlmostEqual(fo.learned(2), 0.70 - BASE, delta=0.012)

    def test_all_rows_evaluated_uses_known_base(self):
        fo = FilmOffset()
        fo.bases[1] = BASE
        reads = self._reads({"1": (700.0, 0), "2": (710.0, 0)})
        rows = {"1": round(700.0 - 0.60, 2), "2": round(710.0 - 0.70, 2)}
        fo.learn(2, parse_tod(FL_START), rows, reads)
        self.assertEqual(len(fo.samples), 2)
        self.assertAlmostEqual(fo.learned(2), 0.65 - BASE, delta=0.012)

    def test_mostly_evaluated_file_on_a_cold_start_still_finds_the_baseline(self):
        """2026-09-18 girls' file as first seen after a restart: 106 rows
        evaluated, 12 copied. The copied cluster is small but sits near
        zero, where only a clock-frame constant can be."""
        fo = FilmOffset()
        spec = {f"e{i}": 700.0 + i for i in range(118)}
        reads = self._reads({b: (raw, 0) for b, raw in spec.items()})
        rows = {}
        for i, (b, raw) in enumerate(spec.items()):
            gap = BASE if i < 12 else 0.40 + (i % 50) * 0.01
            rows[b] = round(raw - gap, 2)
        fo.learn(1, parse_tod(FL_START), rows, reads)
        self.assertEqual(fo.unevaluated[1], 12)
        self.assertEqual(len(fo.samples), 106)
        self.assertAlmostEqual(fo.bases[1], BASE, delta=0.006)

    def test_reset_freezes_existing_evaluations(self):
        """Operator's 2026-09-18 report: Reset re-learned the same offset
        from the same files. Now the times present at reset never count
        again; only rows that change afterwards (or new athletes) do."""
        fo = FilmOffset()
        reads = self._reads({"1": (700.0, 0), "2": (710.0, 0), "3": (720.0, 0),
                             "4": (730.0, 0), "5": (740.0, 0), "6": (750.0, 0)})
        rows = {"1": round(700.0 - BASE, 2), "2": round(710.0 - BASE, 2), "3": round(720.0 - BASE, 2),
                "4": round(730.0 - 0.60, 2), "5": round(740.0 - 0.70, 2)}
        fo.learn(1, parse_tod(FL_START), rows, reads)
        self.assertEqual(len(fo.samples), 2)
        fo.reset(datetime(2026, 9, 18, 18, 0, 0))
        self.assertIsNone(fo.learned(1))
        self.assertAlmostEqual(fo.bases[1], BASE, delta=0.006)      # frame constant kept
        # the same file again (re-save, or a restart that lost the mtime cache)
        fo.learn(1, parse_tod(FL_START), rows, reads)
        self.assertEqual(fo.samples, {})
        self.assertEqual(fo.stale[1], 2)
        self.assertEqual(fo.unevaluated[1], 3)
        # a re-evaluated athlete and a newly evaluated one count; copied stays copied
        rows["4"] = round(730.0 - 0.66, 2)
        rows["6"] = round(750.0 - 0.80, 2)
        fo.learn(1, parse_tod(FL_START), rows, reads)
        self.assertEqual(sorted(s["bib"] for s in fo.samples.values()), ["4", "6"])
        self.assertAlmostEqual(fo.learned(1), 0.73 - BASE, delta=0.012)
        st = fo.stats()
        self.assertEqual(st["stale"], 1)
        self.assertTrue(st["reset_at"].startswith("2026-09-18T18:00:00"))
        # survives a restart; clears with the meet
        back = FilmOffset.from_dict(json.loads(json.dumps(fo.to_dict())))
        self.assertEqual(back.frozen, fo.frozen)
        self.assertEqual(back.reset_at, fo.reset_at)
        back.clear()
        self.assertEqual(back.frozen, {})
        self.assertIsNone(back.reset_at)

    def test_coincident_evaluated_rows_do_not_become_the_baseline(self):
        """Late in a meet nearly every row is evaluated; a few of them
        landing within 12 ms of each other must not be mistaken for the
        copied-row cluster once the real baseline is known."""
        fo = FilmOffset()
        fo.bases[1] = BASE
        spec = {f"e{i}": 700.0 + i for i in range(20)}
        reads = self._reads({b: (raw, 0) for b, raw in spec.items()})
        gaps = [0.40 + 0.03 * i for i in range(20)]
        gaps[3:6] = [0.55, 0.555, 0.56]                    # three within 12 ms by chance
        rows = {b: round(raw - g, 3) for (b, raw), g in zip(spec.items(), gaps)}
        rows["c1"], rows["c2"] = 1.0, 2.0                  # unmatched
        fo.learn(2, parse_tod(FL_START), rows, reads)
        self.assertEqual(fo.unevaluated[2], 0)
        self.assertEqual(len(fo.samples), 20)
        self.assertAlmostEqual(fo.bases[2], BASE)

    def test_implausible_and_unmatched_rows_skipped(self):
        fo = FilmOffset()
        reads = self._reads({"1": (700.0, 0), "2": (710.0, 0), "3": (720.0, 0), "4": (730.0, 0)})
        rows = {"1": 700.03, "2": 710.03, "3": 720.03, "4": 600.0, "9": 500.0}
        fo.learn(1, parse_tod(FL_START), rows, reads)
        self.assertEqual(fo.outliers[1], 1)
        self.assertEqual(fo.samples, {})
        self.assertIsNone(fo.learned(1))
        self.assertEqual(fo.correction(0, 1), 0.0)

    def test_manual_apply_reset_clear(self):
        fo = FilmOffset()
        fo.samples["1:5"] = {"race": 1, "bib": "5", "raw": 1, "lif": 0, "value": 0.5}
        self.assertAlmostEqual(fo.correction(0, 1), 0.5)
        self.assertEqual(fo.correction(0, 2), 0.0)          # another race learns its own
        fo.set_manual(0.8)
        self.assertAlmostEqual(fo.correction(0, 1), 0.8)
        self.assertAlmostEqual(fo.correction(0, 2), 0.8)    # manual: every race
        self.assertEqual(fo.stats()["source"], "manual")
        fo.set_apply(False)
        self.assertEqual(fo.correction(0, 1), 0.0)
        self.assertAlmostEqual(fo.manual, 0.8)             # still shown
        fo.reset()
        self.assertEqual(fo.samples, {})
        self.assertAlmostEqual(fo.manual, 0.8)              # reset keeps the override
        fo.clear()
        self.assertIsNone(fo.manual)
        self.assertTrue(fo.apply)

    def test_state_roundtrip(self):
        fo = FilmOffset()
        fo.learn(3, parse_tod(FL_START), {"1": 699.4, "2": 709.97, "3": 719.97, "4": 729.97},
                 self._reads({"1": (700.0, 0), "2": (710.0, 0), "3": (720.0, 0), "4": (730.0, 0)}), "x.lif")
        fo.set_manual(0.42)
        back = FilmOffset.from_dict(json.loads(json.dumps(fo.to_dict())))
        self.assertEqual(back.samples, fo.samples)
        self.assertEqual(back.bases, fo.bases)
        self.assertEqual(back.unevaluated, fo.unevaluated)
        self.assertAlmostEqual(back.manual, 0.42)
        self.assertEqual(back.files, {3: "x.lif"})
        self.assertIsNone(FilmOffset.from_dict(None).learned(3))


def lookup(bib):
    return {"last_name": "L" + bib, "first_name": "F", "school": "S", "gender": "F"}


class TestPerAntenna(unittest.TestCase):
    """The mat's ports sit at different distances from the line: a port
    with enough evaluated finishes gets its own mean, the rest (and reads
    without a port) use the meet mean; a manual value is one number."""

    def _learn(self, fo, spec):
        # spec: bib -> (raw race secs, gap on film, antenna)
        reads = {b: (read_ts(raw), 0.0, ant) for b, (raw, _, ant) in spec.items()}
        rows = {b: round(raw - gap, 2) for b, (raw, gap, _) in spec.items()}
        # four copied rows so the baseline is found
        for i, b in enumerate(("c1", "c2", "c3", "c4")):
            reads[b] = (read_ts(600.0 + i), 0.0, 1)
            rows[b] = round(600.0 + i - BASE, 2)
        fo.learn(1, parse_tod(FL_START), rows, reads)

    def test_port_uses_own_mean_once_it_has_enough(self):
        fo = FilmOffset()
        # evaluated gaps scatter like real ones (never a tight cluster)
        spec = {f"a{i}": (700.0 + i, 0.40 + 0.04 * i, 1) for i in range(6)}      # A1: 6 evaluated, mean 0.50
        spec.update({f"b{i}": (800.0 + i, 0.80 + 0.1 * i, 7) for i in range(3)})  # A7: only 3, mean 0.90
        self._learn(fo, spec)
        means = fo.antenna_means(1)
        self.assertAlmostEqual(means[1][0], 0.50 - BASE, delta=0.012)
        self.assertEqual(means[1][1], 6)
        self.assertEqual(means[7][1], 3)
        meet = fo.learned(1)
        self.assertAlmostEqual(fo.correction(1, 1), 0.50 - BASE, delta=0.012)   # own mean
        self.assertAlmostEqual(fo.correction(7, 1), meet)                        # too few: race mean
        self.assertAlmostEqual(fo.correction(3, 1), meet)                        # never heard
        self.assertAlmostEqual(fo.correction(0, 1), meet)                        # unknown port
        self.assertEqual(fo.correction(1, 2), 0.0)                               # another race: nothing yet
        st = fo.stats()
        self.assertEqual([(a["antenna"], a["applied"]) for a in st["races"][0]["antennas"]], [(1, True), (7, False)])
        fo.set_manual(0.42)
        self.assertAlmostEqual(fo.correction(1, 1), 0.42)
        self.assertAlmostEqual(fo.correction(7, 2), 0.42)

    def test_reads_carry_their_port_through_the_manager(self):
        m = RaceManager(athlete_lookup=lookup, finish_readers={"FINISH"})
        race = m.create_race("R", event=1, gender="F", distance="2")
        m.set_gun(race, GUN)
        for i in range(6):
            m.ingest(f"{i}", read_ts(700.0 + i), "FINISH", antenna=5)
        m.ingest("x", read_ts(720.0), "FINISH", antenna=3)
        m.ingest("y", read_ts(730.0), "FINISH")
        m.ingest("w", read_ts(735.0), "FINISH")
        fr = m.finish_reads_for_film(race)
        self.assertEqual(fr["0"][2], 5)
        self.assertEqual(fr["y"][2], 0)
        # learn ~0.60 on A5 only (x, y, w copied): A3 and portless athletes
        # get the meet mean, which here equals A5's
        rows = {f"{i}": round(700.0 + i - (0.50 + 0.04 * i), 2) for i in range(6)}
        rows.update({"x": round(720.0 - BASE, 2), "y": round(730.0 - BASE, 2), "w": round(735.0 - BASE, 2)})
        rows.update({"c1": 1, "c2": 2})                                   # unmatched, ignored
        m.film.learn(race, parse_tod(FL_START), rows, fr)
        self.assertEqual(m.film.unevaluated[race], 3)
        self.assertEqual(len(m.film.samples), 6)
        res = {r["bib"]: r["finish"] for r in m.results(race)["finishers"]}
        self.assertEqual(res["0"]["antenna"], 5)
        self.assertAlmostEqual(res["0"]["correction"], m.film.antenna_means(race)[5][0])
        self.assertAlmostEqual(res["x"]["correction"], m.film.learned(race))
        self.assertAlmostEqual(res["y"]["correction"], m.film.learned(race))
        # a new read on A5 is stamped with A5's own mean
        out = m.ingest("z", read_ts(740.0), "FINISH", antenna=5)
        self.assertAlmostEqual(out["finish_offset"], m.film.antenna_means(race)[5][0])


class TestRaceManagerIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = RaceManager(athlete_lookup=lookup, finish_readers={"FINISH"},
                             point_labels={"S1": "Mile 1"},
                             state_path=os.path.join(self.tmp, "state.json"))
        self.race = self.m.create_race("MS Girls", event=1, gender="F", distance="2")
        self.m.set_gun(self.race, GUN)
        for bib, secs in [("1", 828.462), ("2", 849.294), ("3", 861.306), ("4", 911.975), ("5", 921.498)]:
            self.m.ingest(bib, read_ts(secs), "FINISH")

    def _save_lif(self, rows):
        path = os.path.join(self.tmp, self.m.lif_name(self.m.races[self.race]))
        with open(path, "w", newline="") as f:
            f.write(lif_text(rows))
        self._saves = getattr(self, "_saves", 0) + 1
        os.utime(path, (1, 1000.0 + self._saves))          # distinct mtime per save
        return path

    def test_learns_from_finishlynx_file_and_corrects_results(self):
        # nothing evaluated yet: FinishLynx copied every RFID time
        self._save_lif([(b, s - BASE) for b, s in [("1", 828.462), ("2", 849.294), ("3", 861.306), ("4", 911.975), ("5", 921.498)]])
        self.assertTrue(self.m.learn_film_offsets(self.tmp))
        self.assertIsNone(self.m.film.learned(self.race))
        res = self.m.results(self.race)
        self.assertFalse(res["finishers"][0]["finish"]["corrected"])
        raw_first = res["finishers"][0]["finish"]["elapsed"]
        # operator evaluates two athletes, FinishLynx saves again
        self._save_lif([("1", 828.462 - BASE), ("2", 849.294 - BASE), ("3", 861.306 - BASE),
                        ("4", 911.975 - 0.725), ("5", 921.498 - 0.648)])
        self.assertTrue(self.m.learn_film_offsets(self.tmp))
        off = self.m.film.learned(self.race)
        self.assertAlmostEqual(off, (0.725 + 0.648) / 2 - BASE, delta=0.012)
        res = self.m.results(self.race)
        fin = res["finishers"][0]["finish"]
        self.assertTrue(fin["corrected"])
        self.assertAlmostEqual(fin["elapsed"], raw_first - off, places=6)
        self.assertAlmostEqual(fin["raw_elapsed"], raw_first, places=6)
        # the .lif we write and the CSV carry the corrected time
        from rfid_finishlynx.lif_generator import _format_race_time
        self.assertIn(_format_race_time(raw_first - off), self.m.lif_content(self.race))
        self.assertIn(_format_race_time(raw_first - off), self.m.results_csv([self.race]))
        self.assertNotIn(_format_race_time(raw_first), self.m.lif_content(self.race))
        # unchanged file: no re-read, nothing changes
        self.assertFalse(self.m.learn_film_offsets(self.tmp))

    def test_new_finish_reads_are_stamped_and_reported_for_sending(self):
        self.m.film.set_manual(0.5)
        out = self.m.ingest("6", read_ts(950.0), "FINISH")
        self.assertAlmostEqual(out["finish_offset"], 0.5)
        read = next(r for r in self.m.reads.values() if r.bib == "6")
        self.assertAlmostEqual(read.sent_offset, 0.5)
        self.assertEqual(self.m.ingest("7", read_ts(600.0), "S1")["finish_offset"], 0.0)
        self.assertAlmostEqual(self.m.finish_reads_for_film(self.race)["6"][1], 0.5)

    def test_manual_finish_is_never_corrected(self):
        self.m.film.set_manual(0.5)
        self.m.add_manual_read(self.race, "9", GUN + timedelta(seconds=1000.0), "FINISH")
        row = next(r for r in self.m.results(self.race)["finishers"] if r["bib"] == "9")
        self.assertFalse(row["finish"]["corrected"])
        self.assertAlmostEqual(row["finish"]["elapsed"], 1000.0)
        self.assertNotIn("9", self.m.finish_reads_for_film(self.race))

    def test_our_own_export_is_not_learned_from(self):
        self.m.film.set_manual(0.3)            # so our export differs from raw
        self.m.write_lif(self.race, self.tmp)  # writes file + .mirror sidecar
        self.assertFalse(self.m.learn_film_offsets(self.tmp))
        self.assertEqual(self.m.film.samples, {})

    def test_persisted_and_cleared_on_meet_close(self):
        self.m.set_film_offset(manual=0.4, apply=False)
        again = RaceManager(athlete_lookup=lookup, finish_readers={"FINISH"},
                            state_path=os.path.join(self.tmp, "state.json"))
        self.assertAlmostEqual(again.film.manual, 0.4)
        self.assertFalse(again.film.apply)
        self.assertAlmostEqual(next(r for r in again.reads.values()).sent_offset, 0.0)
        again.close_meet()
        self.assertIsNone(again.film.manual)
        self.assertTrue(again.film.apply)
        self.assertEqual(again.film.stats()["samples"], 0)

    def test_set_film_offset_controls(self):
        stats = self.m.set_film_offset(manual=0.7)
        self.assertEqual(stats["source"], "manual")
        stats = self.m.set_film_offset(apply=False)
        self.assertAlmostEqual(stats["manual"], 0.7)
        self.assertEqual(self.m.film.correction(0, self.race), 0.0)
        stats = self.m.set_film_offset(manual=None, apply=True, reset=True)
        self.assertEqual(stats["source"], "none")
        self.assertIsNone(stats["manual"])


class TestPerRace(unittest.TestCase):
    """2026-10-01, user's rule: the learned offset is each race pane's own;
    a new race starts at zero until its own evaluations land."""

    def setUp(self):
        self.m = RaceManager(athlete_lookup=lookup, finish_readers={"FINISH"})
        self.girls = self.m.create_race("Girls", event=1, gender="F", distance="2")
        self.m.set_gun(self.girls, GUN)
        for i in range(5):
            self.m.ingest(f"{i}", read_ts(700.0 + i), "FINISH")
        rows = {f"{i}": round(700.0 + i - BASE, 2) for i in range(3)}         # copied
        rows.update({"3": round(703.0 - 0.60, 2), "4": round(704.0 - 0.70, 2)})  # evaluated
        self.m.film.learn(self.girls, parse_tod(FL_START), rows, self.m.finish_reads_for_film(self.girls))
        self.girls_off = self.m.film.learned(self.girls)

    def test_new_race_starts_at_zero(self):
        boys = self.m.create_race("Boys", event=2, gender="M", distance="2")
        self.m.set_gun(boys, GUN + timedelta(minutes=30))
        self.m.set_entries(boys, ["50"])
        out = self.m.ingest("50", read_ts(2500.0), "FINISH")
        self.assertEqual(out["race"], boys)
        self.assertEqual(out["finish_offset"], 0.0)                      # nothing evaluated in Boys
        fin = self.m.results(boys)["finishers"][0]["finish"]
        self.assertFalse(fin["corrected"])
        self.assertEqual(self.m.results(boys)["film"]["applied"], 0.0)
        # Girls keep theirs
        self.assertAlmostEqual(self.m.results(self.girls)["film"]["applied"], self.girls_off)
        self.assertTrue(self.m.results(self.girls)["finishers"][0]["finish"]["corrected"])

    def test_each_race_learns_its_own_value(self):
        boys = self.m.create_race("Boys", event=2, gender="M", distance="2")
        self.m.set_gun(boys, GUN)
        self.m.set_entries(boys, [str(i) for i in range(10, 15)])
        for i in range(10, 15):
            self.m.ingest(f"{i}", read_ts(800.0 + i), "FINISH")
        rows = {f"{i}": round(800.0 + i - BASE, 2) for i in range(10, 13)}
        rows.update({"13": round(813.0 - 0.30, 2), "14": round(814.0 - 0.36, 2)})
        self.m.film.learn(boys, parse_tod(FL_START), rows, self.m.finish_reads_for_film(boys))
        self.assertAlmostEqual(self.m.film.learned(boys), 0.33 - BASE, delta=0.012)
        self.assertAlmostEqual(self.m.film.learned(self.girls), self.girls_off)
        self.assertNotAlmostEqual(self.m.film.correction(0, boys), self.m.film.correction(0, self.girls), places=2)
        self.assertEqual([r["race"] for r in self.m.film.stats()["races"]], [self.girls, boys])

    def test_held_read_is_not_corrected(self):
        self.assertEqual(self.m.film.correction(0, None), 0.0)

    def test_deleting_a_pane_forgets_its_samples(self):
        self.m.delete_race(self.girls)
        self.assertEqual(self.m.film.samples, {})
        self.assertEqual(self.m.film.stats()["races"], [])


def drifting(fo, race=1, n=60, antenna=0):
    """n evaluated finishes 10 s apart from 15:00 on, the offset growing
    by 0.01 s each (finishers slow down): sample i = 0.30 + 0.01 i."""
    for i in range(n):
        fo.samples[f"{race}:{i}"] = {"race": race, "bib": str(i), "raw": 900.0 + 10 * i,
                                     "lif": 900.0, "value": round(0.30 + 0.01 * i, 3),
                                     "antenna": antenna}


class TestRollingWindow(unittest.TestCase):
    """User's rule 2026-10-03: the offset is the rolling mean of the 20
    evaluated finishes nearest before the athlete, not the race mean."""

    def setUp(self):
        self.fo = FilmOffset()
        drifting(self.fo)

    def test_read_now_uses_the_latest_20(self):
        # samples 40..59 -> mean of 0.70..0.89
        self.assertAlmostEqual(self.fo.correction(0, 1), 0.795, places=6)

    def test_finisher_gets_the_20_before_them(self):
        # 15:00 + 10 s x 30 -> samples 11..30 are at or before it
        self.assertAlmostEqual(self.fo.correction(0, 1, at=1200.0), 0.505, places=6)

    def test_early_finisher_uses_the_first_20(self):
        self.assertAlmostEqual(self.fo.correction(0, 1, at=600.0), 0.395, places=6)

    def test_fewer_than_20_is_the_plain_mean(self):
        fo = FilmOffset()
        drifting(fo, n=5)
        self.assertAlmostEqual(fo.correction(0, 1, at=0.0), 0.32, places=6)
        self.assertAlmostEqual(fo.correction(0, 1), 0.32, places=6)

    def test_antenna_rolls_on_its_own_samples(self):
        drifting(self.fo, race=1, n=10, antenna=3)      # replaces bibs 0..9 with port 3
        port = sorted((s for s in self.fo.samples.values() if s["antenna"] == 3), key=lambda s: s["raw"])
        want = sum(s["value"] for s in port) / len(port)
        self.assertAlmostEqual(self.fo.correction(3, 1), want, places=6)

    def test_manual_still_wins(self):
        self.fo.set_manual(0.5)
        self.assertEqual(self.fo.correction(0, 1, at=1200.0), 0.5)

    def test_results_correct_each_finisher_by_their_own_window(self):
        m = RaceManager(athlete_lookup=lookup, finish_readers={"FINISH"})
        key = m.create_race("Girls 5K", event=1, gender="F", distance="5000")
        m.set_gun(key, GUN, "camera start")
        m.ingest("500", GUN + timedelta(seconds=1000), "FINISH")
        m.ingest("501", GUN + timedelta(seconds=1200), "FINISH")
        m.ingest("502", GUN + timedelta(seconds=1600), "FINISH")
        drifting(m.film, race=key)
        fin = {r["bib"]: r["finish"] for r in m.results(key)["finishers"]}
        self.assertAlmostEqual(fin["500"]["correction"], 0.395, places=6)   # first 20 (only 11 before)
        self.assertAlmostEqual(fin["501"]["correction"], 0.505, places=6)   # samples 11..30
        self.assertAlmostEqual(fin["502"]["correction"], 0.795, places=6)   # after them all: latest 20


if __name__ == "__main__":
    unittest.main()
