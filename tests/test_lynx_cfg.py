"""Lynx.cfg check + optional auto-configure for a new timing PC (2026-09-30).
python -m unittest tests.test_lynx_cfg"""

import os
import tempfile
import unittest
from unittest import mock

from rfid_finishlynx import lynx_cfg
from rfid_finishlynx.lynx_cfg import LynxCfg, apply, check

BINARY = "\\:\x8e\xc1\x18\x06\xc0X7\x05_\x18\x0f\xa2$\xca\xa7,1="     # FinishLynx's own line 2

LAPTIME_IPICO = [
    "\\LapTime\\:,1=", "\\LapTime\\Boot:Int,1,,=1",
    "\\LapTime\\1\\:,1=", "\\LapTime\\1\\Name:String,1=Ipico Super Elite", "\\LapTime\\1\\Type:Int,1,,=9",
    "\\LapTime\\1\\SerialPort:String,1=0,9600,8,N,1.0",
    "\\LapTime\\2\\:,1=", "\\LapTime\\2\\Name:String,1=Trident", "\\LapTime\\2\\Type:Int,1,,=9",
    "\\LapTime\\2\\SerialPort:String,1=-1,192.168.0.101,10001",
]
SCOREBOARDS = [
    "\\Scoreboards\\:,1=", "\\Scoreboards\\Boot:Bool,1=1",
    "\\Scoreboards\\1\\:,1=", "\\Scoreboards\\1\\Name:String,1=AllSports5000",
    "\\Scoreboards\\1\\Script:String,1=Powertime.lss", "\\Scoreboards\\1\\SerialPort:String,1=0,9600,8,E,1.0",
    "\\Scoreboards\\1\\TimeMode:Int,1,,=1", "\\Scoreboards\\1\\ResultsMode:Int,1,,=1",
    "\\Scoreboards\\1\\SortBy:Int,1,,=0",
]
DATABASE = [
    "\\Database\\:,1=", "\\Database\\File\\:,1=",
    "\\Database\\File\\DataInDir:String,1=C:\\Lynx\\Data",
    "\\Database\\File\\DataOutDir:String,1=C:\\Lynx\\Data",
    "\\Database\\File\\WriteLIF:Bool,1=1",
]


def text(*blocks, event_dir="C:\\Lynx\\Data"):
    lines = ["Registry Version: 1.00", BINARY, "\\General\\:,1=",
             f"\\Event\\Directory:String,1={event_dir}"]
    for b in blocks:
        lines += b
    lines += ["\\Language\\:,1=", "\\Language\\Choice:String,1=en_US"]
    return "\r\n".join(lines) + "\r\n"


def rows(cfg, results=r"C:\Finish_Lynx_Results"):
    return {r["id"]: r for r in check(cfg, results)}


class TestFreshMachine(unittest.TestCase):
    """FinishLynx with Ipico entries, one serial scoreboard, default folders."""

    def setUp(self):
        self.src = text(LAPTIME_IPICO, SCOREBOARDS, DATABASE)
        self.cfg = LynxCfg(self.src)

    def test_round_trip_is_byte_exact(self):
        self.assertEqual(self.cfg.dump(), self.src)

    def test_check_flags_everything(self):
        r = rows(self.cfg)
        self.assertEqual({k: v["ok"] for k, v in r.items()},
                         {"fl_laptime": False, "fl_scoreboard": False, "fl_input": False, "fl_output": False})
        self.assertIn("no LapTime entry", r["fl_laptime"]["detail"])

    def test_apply_adds_entries_and_sets_folders(self):
        changes = apply(self.cfg)
        self.assertTrue(changes)
        self.assertTrue(all(v["ok"] for v in rows(self.cfg).values()), rows(self.cfg))
        lt = self.cfg.entries("LapTime")[3]
        self.assertEqual((lt["Type"], lt["SerialPort"]), ("11", "-2,0.0.0.0,5000"))
        sb = self.cfg.entries("Scoreboards")[2]
        self.assertEqual((sb["Name"], sb["Script"], sb["SerialPort"], sb["TimeMode"]),
                         ("Middleware", "Example2.lss", "-3,127.0.0.1,5891", "1"))
        self.assertEqual(self.cfg.get(r"\Database\File\DataInDir"), r"C:\Finish_Lynx_Results\RFID")
        self.assertEqual(self.cfg.get(r"\Event\Directory"), r"C:\Finish_Lynx_Results")

    def test_untouched_lines_survive_including_binary(self):
        apply(self.cfg)
        out = self.cfg.dump()
        self.assertEqual(out.split("\r\n")[1], BINARY)
        for line in LAPTIME_IPICO + SCOREBOARDS:
            self.assertIn(line, out)
        # new entries sit inside their own section, before what follows it
        lines = out.split("\r\n")
        self.assertLess(lines.index("\\LapTime\\3\\:,1="), lines.index("\\Scoreboards\\:,1="))
        self.assertLess(lines.index("\\Scoreboards\\2\\:,1="), lines.index("\\Database\\:,1="))

    def test_second_apply_changes_nothing(self):
        apply(self.cfg)
        once = self.cfg.dump()
        self.assertEqual(apply(self.cfg), [])
        self.assertEqual(self.cfg.dump(), once)


class TestIpicoOnTheRightPort(unittest.TestCase):

    def test_ipico_on_5000_is_switched_to_lynx(self):
        lt = LAPTIME_IPICO + ["\\LapTime\\3\\:,1=", "\\LapTime\\3\\Name:String,1=LapTime",
                              "\\LapTime\\3\\Type:Int,1,,=9", "\\LapTime\\3\\SerialPort:String,1=-2,0.0.0.0,5000"]
        cfg = LynxCfg(text(lt, SCOREBOARDS, DATABASE))
        self.assertIn("Ipico", rows(cfg)["fl_laptime"]["detail"])
        apply(cfg)
        self.assertEqual(cfg.entries("LapTime")[3]["Type"], "11")
        self.assertNotIn(4, cfg.entries("LapTime"))            # fixed in place, no new entry


class TestAlreadyRight(unittest.TestCase):
    """This rig on 2026-09-30."""

    def test_rig_passes_and_apply_is_a_no_op(self):
        lt = LAPTIME_IPICO + ["\\LapTime\\3\\:,1=", "\\LapTime\\3\\Type:Int,1,,=11",
                              "\\LapTime\\3\\SerialPort:String,1=-2,0.0.0.0,5000"]
        sb = SCOREBOARDS + ["\\Scoreboards\\5\\:,1=", "\\Scoreboards\\5\\Name:String,1=Middleware",
                            "\\Scoreboards\\5\\Script:String,1=Example2.lss",
                            "\\Scoreboards\\5\\SerialPort:String,1=-3,127.0.0.1,5891",
                            "\\Scoreboards\\5\\TimeMode:Int,1,,=1"]
        db = ["\\Database\\File\\:,1=", "\\Database\\File\\DataInDir:String,1=C:\\Finish_Lynx_Results\\RFID",
              "\\Database\\File\\DataOutDir:String,1=C:\\Finish_Lynx_Results"]
        src = text(lt, sb, db, event_dir="C:\\Finish_Lynx_Results")
        cfg = LynxCfg(src)
        self.assertTrue(all(v["ok"] for v in rows(cfg).values()), rows(cfg))
        self.assertEqual(apply(cfg), [])
        self.assertEqual(cfg.dump(), src)

    def test_running_time_off_is_flagged(self):
        sb = ["\\Scoreboards\\:,1=", "\\Scoreboards\\1\\:,1=", "\\Scoreboards\\1\\Name:String,1=Middleware",
              "\\Scoreboards\\1\\Script:String,1=Example2.lss",
              "\\Scoreboards\\1\\SerialPort:String,1=-3,127.0.0.1,5891", "\\Scoreboards\\1\\TimeMode:Int,1,,=0"]
        cfg = LynxCfg(text(LAPTIME_IPICO, sb, DATABASE))
        self.assertIn("running time off", rows(cfg)["fl_scoreboard"]["detail"])
        apply(cfg)
        self.assertEqual(cfg.entries("Scoreboards")[1]["TimeMode"], "1")


class TestNoPluginNoScoreboards(unittest.TestCase):

    def test_missing_laptime_plugin_is_reported_not_invented(self):
        cfg = LynxCfg(text(DATABASE))
        self.assertIn("plug-in is not installed", rows(cfg)["fl_laptime"]["detail"])
        apply(cfg)
        self.assertEqual(cfg.entries("LapTime"), {})
        sb = cfg.entries("Scoreboards")[1]                      # from the built-in template
        self.assertEqual((sb["Script"], sb["SerialPort"], sb["SortBy"]), ("Example2.lss", "-3,127.0.0.1,5891", "0"))


class TestCommandLine(unittest.TestCase):

    def test_apply_backs_up_and_refuses_while_running(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Lynx.cfg")
            with open(path, "wb") as f:
                f.write(text(LAPTIME_IPICO, SCOREBOARDS, DATABASE).encode("latin-1"))
            results = os.path.join(d, "results")
            with mock.patch.object(lynx_cfg, "finishlynx_running", return_value=True):
                self.assertEqual(lynx_cfg.main(["apply", "--cfg", path, "--results", results]), 3)
            with mock.patch.object(lynx_cfg, "finishlynx_running", return_value=False):
                self.assertEqual(lynx_cfg.main(["apply", "--cfg", path, "--results", results]), 0)
            self.assertTrue(any(n.startswith("Lynx.cfg.before-rfid-") for n in os.listdir(d)))
            self.assertTrue(os.path.isdir(os.path.join(results, "RFID")))
            self.assertEqual(lynx_cfg.main(["check", "--cfg", path, "--results", results]), 0)


if __name__ == "__main__":
    unittest.main()
