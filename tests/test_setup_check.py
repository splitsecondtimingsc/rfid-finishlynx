"""Setup check rows for a new timing PC (2026-09-30).
python -m unittest tests.test_setup_check"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from rfid_finishlynx import lynx_cfg, setup_check
from rfid_finishlynx.config import Config
from tests.test_lynx_cfg import DATABASE, LAPTIME_IPICO, SCOREBOARDS, text


def fake_app(results, status=None, reader=None, antennas=None):
    cfg = Config()
    cfg.event_directory = results
    cfg.reader.type, cfg.reader.host = "vanch", "192.168.0.128"
    cfg.reader.antennas = antennas or []
    cfg.camera_start.enabled = True
    st = {"reader_connected": True, "finishlynx_connected": True, "scoreboard_packets": 12,
          "led_clocks": [{"label": "Finish", "port": "COM10", "status": "driving"}],
          "camera_start": {"receiving": True, "replies": 900}}
    st.update(status or {})
    rd = reader if reader is not None else SimpleNamespace(active_antennas=[1, 2, 3, 4, 5, 6, 7, 8],
                                                          port_count=8, buzzer_on=False)
    return SimpleNamespace(config=cfg, get_status=lambda: st, reader=rd)


class SetupCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.results = os.path.join(d, "Finish_Lynx_Results")
        self.cfg_path = os.path.join(d, "Lynx.cfg")
        self.prefs = os.path.join(d, "preferences.json")
        patches = [
            mock.patch.object(setup_check, "local_ipv4", return_value=["192.168.0.200", "10.1.1.5"]),
            mock.patch.object(setup_check, "installed_driver_infs", return_value={"ch341ser.inf", "ser2pl_1.inf"}),
            mock.patch.object(setup_check, "MEETPRO_PREFS", self.prefs),
            mock.patch.object(lynx_cfg, "find_cfg", lambda explicit=None: self.cfg_path if os.path.exists(self.cfg_path) else None),
            mock.patch("rfid_finishlynx.camera_start.find_tshark", return_value=r"C:\Program Files\Wireshark\tshark.exe"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_cfg(self, good=True):
        if good:
            lynx_cfg_obj = lynx_cfg.LynxCfg(text(LAPTIME_IPICO, SCOREBOARDS, DATABASE))
            lynx_cfg.apply(lynx_cfg_obj, self.results)
            data = lynx_cfg_obj.dump()
        else:
            data = text(LAPTIME_IPICO, SCOREBOARDS, DATABASE)
        with open(self.cfg_path, "wb") as f:
            f.write(data.encode("latin-1"))

    def meetpro(self, directory):
        with open(self.prefs, "w", encoding="utf-8") as f:
            json.dump({"fat": {"vendor": "lynx", "directory": directory}}, f)

    def rows(self, app):
        return {r["id"]: r for r in setup_check.run(app)}


class TestReadyPc(SetupCase):

    def test_everything_green(self):
        self.write_cfg(good=True)
        os.makedirs(os.path.join(self.results, "RFID"))
        self.meetpro(os.path.join(self.results, "RFID"))
        r = self.rows(fake_app(self.results))
        bad = {k: v for k, v in r.items() if v["ok"] is False}
        self.assertEqual(bad, {})
        self.assertTrue(all(not v["fix"] for v in r.values()))
        self.assertIn("no start lists yet", r["meetpro_dir"]["detail"])   # judged from MeetPro's prefs


class TestNewPc(SetupCase):

    def test_each_problem_has_a_fix(self):
        self.write_cfg(good=False)
        self.meetpro(self.results)                                 # still the old folder
        app = fake_app(self.results, status={"finishlynx_connected": False, "scoreboard_packets": 0,
                                             "camera_start": {"receiving": False, "error": "tshark not found"}},
                       reader=SimpleNamespace(active_antennas=[1], port_count=8, buzzer_on=True))
        with mock.patch.object(setup_check, "local_ipv4", return_value=["10.1.1.5"]), \
                mock.patch.object(setup_check, "installed_driver_infs", return_value={"ch341ser.inf"}), \
                mock.patch("rfid_finishlynx.camera_start.find_tshark", return_value=None):
            r = self.rows(app)
        for rid in ("reader_net", "reader_antennas", "reader_beep", "clock_drivers", "fl_laptime",
                    "fl_scoreboard", "fl_input", "fl_output", "fl_link", "rfid_dir", "meetpro_dir", "tshark"):
            self.assertIs(r[rid]["ok"], False, rid)
            self.assertTrue(r[rid]["fix"], rid)
        self.assertIn("PL2303", r["clock_drivers"]["detail"])
        self.assertIsNone(r["camera_seen"]["ok"])                  # nothing to judge without tshark
        self.assertIsNone(r["fl_feed"]["ok"])                      # FinishLynx not open: feed not judged

    def test_no_finishlynx_and_no_meetpro(self):
        r = self.rows(fake_app(self.results))
        self.assertIs(r["fl_installed"]["ok"], False)
        self.assertIsNone(r["meetpro_dir"]["ok"])
        self.assertIn("no start lists yet", r["meetpro_dir"]["detail"])

    def test_reader_down_does_not_judge_antennas_or_beep(self):
        self.write_cfg()
        r = self.rows(fake_app(self.results, status={"reader_connected": False}))
        self.assertIs(r["reader_up"]["ok"], False)
        self.assertIsNone(r["reader_antennas"]["ok"])
        self.assertIsNone(r["reader_beep"]["ok"])

    def test_explicit_antenna_list(self):
        self.write_cfg()
        app = fake_app(self.results, antennas=[1, 3, 5, 7],
                       reader=SimpleNamespace(active_antennas=[1, 3, 5, 7], port_count=8, buzzer_on=False))
        self.assertIs(self.rows(app)["reader_antennas"]["ok"], True)


class TestMeetProEvidence(SetupCase):
    """MeetPro may run on another laptop: where its newest start lists land
    beats its (possibly stale) preferences on this PC."""

    def touch(self, folder, name, mtime):
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, name)
        open(p, "w").close()
        os.utime(p, (mtime, mtime))

    def test_start_lists_in_rfid_win_over_stale_prefs(self):
        self.meetpro(self.results)                                 # this PC's MeetPro: old folder
        self.touch(os.path.join(self.results, "RFID"), "lynx.EVT", 2_000_000_000)
        self.touch(self.results, "lynx.evt", 1_900_000_000)        # older leftovers
        r = self.rows(fake_app(self.results))["meetpro_dir"]
        self.assertIs(r["ok"], True, r)

    def test_newest_start_lists_still_in_the_old_folder(self):
        self.touch(os.path.join(self.results, "RFID"), "lynx.ppl", 1_900_000_000)
        self.touch(self.results, "lynx.PPL", 2_000_000_000)
        r = self.rows(fake_app(self.results))["meetpro_dir"]
        self.assertIs(r["ok"], False)
        self.assertIn("Directory =", r["fix"])


class TestHelpers(unittest.TestCase):

    def test_same_subnet(self):
        self.assertTrue(setup_check.same_subnet("192.168.0.200", "192.168.0.128"))
        self.assertFalse(setup_check.same_subnet("192.168.1.200", "192.168.0.128"))
        self.assertFalse(setup_check.same_subnet("junk", "192.168.0.128"))

    def test_meetpro_prefs_missing_or_odd(self):
        self.assertIsNone(setup_check.meetpro_fat_directory(os.path.join(tempfile.gettempdir(), "nope.json")))


if __name__ == "__main__":
    unittest.main()
