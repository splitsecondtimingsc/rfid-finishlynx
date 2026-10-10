import os
import tempfile
import unittest
from pathlib import Path

from rfid_finishlynx.main import find_config_path


class TestConfigExample(unittest.TestCase):
    """config.yaml is per-machine and not in git; a fresh clone only has
    config.example.yaml, which becomes config.yaml on first run."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        os.chdir(self.base)  # keep the repo's own config.yaml out of the search

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_example_copied_when_config_missing(self):
        (self.base / "config.example.yaml").write_text("mode: remote_lap\n")
        path = find_config_path(self.base)
        self.assertEqual(Path(path), self.base / "config.yaml")
        self.assertEqual((self.base / "config.yaml").read_text(), "mode: remote_lap\n")

    def test_existing_config_never_overwritten(self):
        (self.base / "config.example.yaml").write_text("mode: remote_lap\n")
        (self.base / "config.yaml").write_text("mode: finish_line\n")
        path = find_config_path(self.base)
        self.assertEqual(Path(path), self.base / "config.yaml")
        self.assertEqual((self.base / "config.yaml").read_text(), "mode: finish_line\n")

    def test_no_config_and_no_example(self):
        self.assertIsNone(find_config_path(self.base))


if __name__ == "__main__":
    unittest.main()
