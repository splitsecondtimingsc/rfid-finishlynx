"""Range-test log parsing and reporting."""
import unittest
from datetime import datetime, timedelta

from range_test import format_beacon, parse_beacon, parse_log_line, summarise


class TestBeaconMessages(unittest.TestCase):

    def test_roundtrip(self):
        now = datetime(2026, 8, 31, 20, 3, 15, 560000)
        line = format_beacon("r4", 7, now)
        self.assertEqual(parse_beacon(line), ("r4", 7, "20:03:15.560"))

    def test_rejects_corrupted_beacon(self):
        line = format_beacon("r4", 7, datetime.now())
        self.assertIsNone(parse_beacon(line.replace(b"r4", b"r9")))  # checksum
        self.assertIsNone(parse_beacon(b"$READ,100,20:00:00.000,SPLIT1*00\n"))
        self.assertIsNone(parse_beacon(b"garbage\n"))


class TestLogParsing(unittest.TestCase):

    def test_parses_dated_line(self):
        arrival, run, seq = parse_log_line(
            "2026-08-31 20:13:33.482594 run=r2 seq=81 sent=20:13:33.118")
        self.assertEqual((run, seq), ("r2", 81))
        self.assertEqual(arrival, datetime(2026, 8, 31, 20, 13, 33, 482594))

    def test_parses_legacy_undated_line(self):
        # Logs written before dates were added must still be readable.
        arrival, run, seq = parse_log_line("20:06:50.695432 run=r2 seq=14 sent=x")
        self.assertEqual((run, seq), ("r2", 14))
        self.assertIsNone(arrival)

    def test_ignores_rubbish(self):
        self.assertIsNone(parse_log_line("# summary 2026-08-31"))
        self.assertIsNone(parse_log_line(""))


class TestSummary(unittest.TestCase):

    def _entries(self, seqs, start=None, step=6):
        start = start or datetime(2026, 8, 31, 20, 0, 0)
        return [(start + timedelta(seconds=step * i), s) for i, s in enumerate(seqs)]

    def test_perfect_run(self):
        report = summarise({"r1": self._entries(range(10))})
        self.assertIn("heard 10/10 beacons (100%)", report[0])
        self.assertFalse([line for line in report if "missed" in line])

    def test_counts_gaps_within_observed_window(self):
        report = summarise({"r1": self._entries([0, 1, 4, 5])})
        self.assertIn("heard 4/6 beacons (67%)", report[0])
        self.assertIn("missed 2: 2, 3", " ".join(report))

    def test_late_listener_is_not_penalised(self):
        # Joining at seq 80 must not read as 80 lost beacons.
        report = summarise({"r2": self._entries([80, 81, 82])})
        self.assertIn("heard 3/3 beacons (100%)", report[0])
        self.assertIn("from seq 80", report[0])

    def test_reports_longest_silence(self):
        entries = [(datetime(2026, 8, 31, 20, 0, 0), 0),
                   (datetime(2026, 8, 31, 20, 0, 6), 1),
                   (datetime(2026, 8, 31, 20, 3, 0), 2)]
        self.assertIn("longest silence 174s", " ".join(summarise({"r1": entries})))


if __name__ == "__main__":
    unittest.main()
