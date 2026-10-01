import os
import tempfile
import unittest

import yaml

from rfid_finishlynx.config import RacesConfig, load_config


class TestRacesConfig(unittest.TestCase):

    def test_races_enabled_by_default(self):
        """A config.yaml without a races block must still build the finish-line
        console (race manager) - it used to fall back to the legacy dashboard."""
        self.assertTrue(RacesConfig().enabled)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            with open(path, "w") as f:
                yaml.dump({"mode": "finish_line", "finishlynx": {"host": "127.0.0.1"}}, f)
            cfg = load_config(path)
        self.assertEqual(cfg.mode, "finish_line")
        self.assertTrue(cfg.races.enabled)

    def test_races_block_is_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            with open(path, "w") as f:
                yaml.dump({"races": {"enabled": False, "lif_dir": "C:/lif"}}, f)
            cfg = load_config(path)
        self.assertFalse(cfg.races.enabled)
        self.assertEqual(cfg.races.lif_dir, "C:/lif")


if __name__ == "__main__":
    unittest.main()
