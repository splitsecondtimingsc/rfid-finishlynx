"""Startup / deployment fixes from the 2026-10-13 audit (ported from the camera branch).

S-01  --no-web refuses preview and a held hardware lock before any driver
      is built; otherwise it holds the lock while it runs
S-03  INSTALL.bat never copies the per-PC settings files over an install
W9    the native file dialogs trust the peer address only, never Host

All drivers are mocks: nothing here opens a device.
"""

import fnmatch
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfid_finishlynx import main as main_mod
from rfid_finishlynx.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Drivers:
    """Every device the CLI could open, mocked; counts what was built."""

    def __enter__(self):
        self.output = mock.MagicMock()
        self.output.connect.return_value = False
        self.output.is_connected = False
        self.listener = mock.MagicMock()
        self.reader = mock.MagicMock()
        self.reader.connect.return_value = False      # ends the CLI loop at once
        self.lora = mock.MagicMock()
        self._p = [mock.patch("rfid_finishlynx.finishlynx.FinishLynxOutput", return_value=self.output),
                   mock.patch("rfid_finishlynx.finishlynx.FinishLynxListener", return_value=self.listener),
                   mock.patch("rfid_finishlynx.rfid_reader.create_reader", return_value=self.reader),
                   mock.patch("rfid_finishlynx.lora_receiver.LoRaReceiver", return_value=self.lora),
                   mock.patch("rfid_finishlynx.transport.LoRaTransport", return_value=self.lora),
                   mock.patch("rfid_finishlynx.main.signal.signal")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()

    def touched(self) -> int:
        return (self.output.connect.call_count + self.listener.start.call_count
                + self.reader.connect.call_count + self.lora.connect.call_count + self.lora.start.call_count)


class FakeLock:
    def __init__(self, ok=True):
        self.ok, self.held, self.owner_pid = ok, False, 4242

    def acquire(self):
        self.held = self.ok
        return self.ok

    def release(self):
        self.held = False


class S01CliGuards(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cliguard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        Path(self.cfg_path).write_text("{}", encoding="utf-8")
        # a staging copy carries a PREVIEW_ONLY in its folder: look in the temp folder only
        p = mock.patch("rfid_finishlynx.hw_guard.Path.cwd", return_value=Path(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def test_preview_refuses_before_any_driver(self):
        with mock.patch.dict(os.environ, {"RFID_PREVIEW": "1"}), Drivers() as d:
            lock = FakeLock()
            self.assertEqual(main_mod.run_cli_mode(Config(), self.cfg_path, lock=lock), 2)
        self.assertEqual(d.touched(), 0)
        self.assertFalse(lock.held)

    def test_preview_marker_beside_the_config_refuses(self):
        Path(self.tmp, "PREVIEW_ONLY").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"RFID_PREVIEW": ""}), Drivers() as d:
            self.assertEqual(main_mod.run_cli_mode(Config(), self.cfg_path, lock=FakeLock()), 2)
        self.assertEqual(d.touched(), 0)

    def test_hardware_lock_held_elsewhere_refuses(self):
        with mock.patch.dict(os.environ, {"RFID_PREVIEW": ""}), Drivers() as d:
            self.assertEqual(main_mod.run_cli_mode(Config(), self.cfg_path, lock=FakeLock(ok=False)), 2)
        self.assertEqual(d.touched(), 0)

    def test_allowed_cli_holds_the_lock_while_running(self):
        lock = FakeLock()
        held_during = []
        with mock.patch.dict(os.environ, {"RFID_PREVIEW": ""}), Drivers() as d:
            d.reader.connect.side_effect = lambda: held_during.append(lock.held) or False
            self.assertEqual(main_mod.run_cli_mode(Config(), self.cfg_path, lock=lock), 0)
        self.assertEqual(held_during, [True])
        self.assertFalse(lock.held)                    # released at the end

    def test_main_no_web_preview_exits_2(self):
        argv = ["rfid_finishlynx", "--no-web", "--preview", "-c", self.cfg_path]
        with mock.patch.dict(os.environ, {"RFID_PREVIEW": ""}), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(main_mod, "setup_logging"), Drivers() as d:
            with self.assertRaises(SystemExit) as e:
                main_mod.main()
        self.assertEqual(e.exception.code, 2)
        self.assertEqual(d.touched(), 0)


class S03InstallerKeepsPerPcFiles(unittest.TestCase):

    def test_installer_excludes_what_promote_excludes(self):
        promote = os.path.join(ROOT, "tools", "promote.py")
        if not os.path.isfile(promote):
            self.skipTest("tools/promote.py is not in a package")
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import promote as promote_mod
        finally:
            sys.path.pop(0)
        with open(os.path.join(ROOT, "INSTALL.bat"), encoding="utf-8", errors="replace") as f:
            text = f.read()
        cmd = re.search(r"robocopy \"%SRC%\.\".*?(?:\n(?![ \t])|$)", text.replace("^\r\n", "^\n"), re.S).group(0)
        xf = re.search(r"/xf\s+(.*?)\s+/xd", cmd.replace("^\n", " "), re.S).group(1).split()
        uncovered = [n for n in sorted(promote_mod.EXCLUDE_FILES)
                     if n not in xf and not any(fnmatch.fnmatch(n, g) for g in xf if "*" in g)]
        self.assertEqual(uncovered, [])
        for name in ("meet-folders.json", "camera-prefs.json", "camera-profiles.json", "camera-seen.json"):
            self.assertIn(name, xf)
            self.assertIn(name + ".tmp", xf)


class W9LocalOnlyDialogs(unittest.TestCase):

    def setUp(self):
        from rfid_finishlynx import web
        self.web = web
        self.client = web.app.test_client()
        p = mock.patch.object(web, "_native_pick", return_value=("C:/chosen", None))
        self.pick = p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(web, "_own_addresses", return_value=frozenset({"192.0.2.1"}))
        p2.start()
        self.addCleanup(p2.stop)

    def post(self, remote, host):
        return self.client.post("/api/fs/pick", json={"mode": "dir"},
                                environ_overrides={"REMOTE_ADDR": remote}, headers={"Host": host})

    def test_spoofed_host_is_refused(self):
        self.assertEqual(self.post("192.0.2.50", "192.0.2.1:8080").status_code, 403)
        self.assertEqual(self.post("192.0.2.50", "192.0.2.50:8080").status_code, 403)   # 200 before
        self.assertEqual(self.pick.call_count, 0)

    def test_this_pc_is_allowed(self):
        for remote in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "192.0.2.1"):
            self.assertEqual(self.post(remote, "example:8080").status_code, 200, remote)
        self.assertEqual(self.pick.call_count, 4)


class W9OwnAddresses(unittest.TestCase):

    def test_interface_addresses_from_the_host_name(self):
        from rfid_finishlynx import web
        saved = dict(web._OWN_ADDRS)
        self.addCleanup(web._OWN_ADDRS.update, saved)
        web._OWN_ADDRS.update(at=0.0, addrs=frozenset())
        infos = [(2, 1, 6, "", ("192.0.2.9", 0)), (23, 1, 6, "", ("fe80::1%12", 0, 0, 12))]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            self.assertEqual(web._own_addresses(), frozenset({"192.0.2.9", "fe80::1"}))


if __name__ == "__main__":
    unittest.main()
