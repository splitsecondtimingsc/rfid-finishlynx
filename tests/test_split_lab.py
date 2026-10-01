"""bench/split_lab.py scores live FinishLynx runs; check the scorer itself.

A hand-written FinishLynx .lif where the known failure modes happened -
a missed Mile 1 shifting Mile 2 into split 1, a late-delivered Mile 1
landing as the final time - must be reported as exactly that.
Hardware-free: python -m unittest tests.test_split_lab
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "split_lab", os.path.join(HERE, os.pardir, "bench", "split_lab.py"))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def run_for(bibs=("9001", "9002", "9003", "9004"), spacing=15.0, gun="10:00:00.000"):
    g = lab.parse_tod(gun)
    sent = []
    for step in lab.plan(list(bibs), spacing):
        t = g + step["elapsed"]
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        sent.append(dict(step, tod=f"{int(h):02d}:{int(m):02d}:{s:06.3f}"))
    return {"bibs": dict(zip("ABCD", bibs)), "sent": sent}


class TestPlan(unittest.TestCase):

    def test_hazards_are_in_the_script(self):
        steps = lab.plan(["A", "B", "C", "D"], 15.0)
        pts = lambda b: [s["point"] for s in steps if s["bib"] == b]
        self.assertEqual(pts("A"), ["S1", "S2", "FINISH"])
        self.assertEqual(pts("B"), ["S2", "FINISH"])
        self.assertEqual(pts("C"), ["S2", "FINISH", "S1"])          # Mile 1 delivered last
        self.assertEqual(pts("D"), ["S1", "S1", "S2", "FINISH"])
        late = [s for s in steps if s["bib"] == "C" and s["point"] == "S1"][0]
        self.assertLess(late["elapsed"], late["send_at"])            # true time kept

    def test_send_order_is_monotonic(self):
        at = [s["send_at"] for s in lab.plan(["A", "B", "C", "D"], 15.0)]
        self.assertEqual(at, sorted(at))

    def test_ports(self):
        self.assertEqual(lab.parse_ports("5000"), {"S1": 5000, "S2": 5000, "FINISH": 5000})
        self.assertEqual(lab.parse_ports("S1=5001,S2=5002,FINISH=5000")["S2"], 5002)
        with self.assertRaises(SystemExit):
            lab.parse_ports("S1=5001,FINISH=5000")


class TestClassify(unittest.TestCase):

    HEADER = ["1", "1", "1", "Lab", "", "", "", "", "", "5000", "10:00:00.000"]

    def row(self, bib, splits, time):
        return ["1", bib, "", "", "", "", time, "", "", "", splits, "", "", "", "", "", "", "", time]

    def test_everything_where_meetpro_looks(self):
        run = run_for()
        rows = [self.row("9001", "0:15.000 (0:15.000),0:30.000 (0:15.000),0:45.000 (0:15.000)", "0:45.000"),
                self.row("9002", ",0:31.000 (0:31.000),0:46.000 (0:15.000)", "0:46.000"),
                self.row("9003", "0:17.000 (0:17.000),0:32.000 (0:15.000),0:47.000 (0:15.000)", "0:47.000"),
                self.row("9004", "0:18.000 (0:18.000),0:33.000 (0:15.000),0:48.000 (0:15.000)", "0:48.000")]
        res = lab.classify(run, self.HEADER, rows)
        self.assertTrue(all(line.endswith("OK") for line in lab.verdict(res, run)), lab.verdict(res, run))

    def test_arrival_order_failures_are_named(self):
        run = run_for()
        rows = [self.row("9002", "0:31.000 (0:31.000),0:46.000 (0:15.000)", "0:46.000"),          # shifted
                self.row("9003", "0:32.000 (0:32.000),0:47.000 (0:15.000),0:17.000 (-)", "0:17.000")]  # late M1 final
        landed = {(r["bib"], r["point"]): r["landed"] for r in lab.classify(run, self.HEADER, rows)}
        self.assertEqual(landed[("9002", "S2")], "split 1")
        self.assertEqual(landed[("9002", "FINISH")], "FINAL")
        self.assertEqual(landed[("9003", "S1")], "FINAL")
        self.assertEqual(landed[("9001", "S1")], "no row")
        verdict = "\n".join(lab.verdict(lab.classify(run, self.HEADER, rows), run))
        self.assertIn("Mile 2 -> split 1", verdict)
        self.assertIn("Mile 1 -> FINAL", verdict)


class TestMeetProStages(unittest.TestCase):
    """The bench variants, read through the MeetPro model: 'console' should
    reproduce today's problems and 'proposed' should not have them."""

    ROSTER = [{"bib": "11", "last": "Ames", "first": "Ava", "team": "Red"},
              {"bib": "12", "last": "Bell", "first": "Bo", "team": "Hammond, SC"},
              {"bib": "13", "last": "Cole", "first": "Cy", "team": "Red"},
              {"bib": "14", "last": "Dunn", "first": "Di", "team": "Blue"}]
    TEAMS = {"Red", "Blue", "Hammond, SC"}
    POINTS = ["Mile 1", "Mile 2"]

    def live(self, variant):
        from tests import meetpro_model as mp
        return [(label, mp.ms_live_sync("009-1-01.lif", text, self.POINTS, self.TEAMS))
                for label, text in lab.meetpro_states(self.ROSTER, 9, variant)]

    def test_console_shows_nothing_live_until_the_finish(self):
        stages = self.live("console")
        self.assertEqual(stages[0][1], {})
        self.assertEqual(stages[1][1], {})

    def test_proposed_shows_mile_1_while_racing(self):
        first = self.live("proposed")[0][1]
        self.assertEqual({b: v["splits"] for b, v in first.items()},
                         {11: {"Mile 1": "6:00.000"}, 13: {"Mile 1": "6:05.000"}, 14: {"Mile 1": "6:10.000"}})

    def test_proposed_never_mislabels_a_split(self):
        for _, sent in self.live("proposed"):
            self.assertNotIn("Mile 1", sent.get(12, {}).get("splits", {}))
            for v in sent.values():
                self.assertNotIn(v["result"], v["splits"].values())

    def test_proposed_keeps_a_comma_team(self):
        final = self.live("proposed")[-1][1]
        self.assertTrue(final[12]["team_scored"])

    def test_sandbox_guard(self):
        with self.assertRaises(SystemExit):
            lab._guard_sandbox(lab.RESULTS_DIR)


if __name__ == "__main__":
    unittest.main()
