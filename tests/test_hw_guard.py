"""Hardware ownership guard (hw_guard.py): a second middleware process on the
same machine must never touch the reader, clocks, radio, camera or
FinishLynx. python -m unittest tests.test_hw_guard"""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from rfid_finishlynx import hw_guard
from rfid_finishlynx.hw_guard import HardwareLock, preview_requested


class TestLock(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hw.lock"

    def tearDown(self):
        for _ in range(20):                     # Windows frees a killed process's handles a beat later
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.25)
        self.tmp.cleanup()

    def test_second_lock_in_another_process_is_refused(self):
        """The real case: the live console holds it, a staging copy asks."""
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {repr(str(Path(__file__).resolve().parents[1]))})
                from rfid_finishlynx.hw_guard import HardwareLock
                lk = HardwareLock({repr(str(self.path))})
                print("held" if lk.acquire() else "refused", flush=True)
                time.sleep(30)
            """)],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            lk = HardwareLock(self.path)
            self.assertFalse(lk.acquire())
            self.assertEqual(lk.owner_pid, holder.pid)      # says who has it
            holder.kill()
            holder.wait(timeout=10)
            # released with the process, no stale lock to clean up
            deadline = time.time() + 5
            while not lk.acquire() and time.time() < deadline:
                time.sleep(0.1)
            self.assertTrue(lk.held)
            lk.release()
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=10)
            holder.stdout.close()

    def test_release_then_reacquire(self):
        a = HardwareLock(self.path)
        self.assertTrue(a.acquire())
        self.assertTrue(a.acquire())                    # idempotent
        self.assertEqual(a.owner_pid, os.getpid())
        a.release()
        b = HardwareLock(self.path)
        self.assertTrue(b.acquire())
        b.release()


class TestPreviewRequested(unittest.TestCase):

    def test_env_var(self):
        with mock.patch.dict(os.environ, {hw_guard.PREVIEW_ENV: "1"}):
            self.assertIn("RFID_PREVIEW", preview_requested())
        with mock.patch.dict(os.environ, {hw_guard.PREVIEW_ENV: "0"}):
            self.assertIsNone(preview_requested(None) if not (Path.cwd() / hw_guard.MARKER_NAME).exists() else None)

    def test_marker_next_to_config(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {hw_guard.PREVIEW_ENV: ""}):
            cfg = Path(d) / "config.yaml"
            cfg.write_text("mode: finish_line\n")
            (Path(d) / hw_guard.MARKER_NAME).write_text("preview\n")
            self.assertIn(hw_guard.MARKER_NAME, preview_requested(str(cfg)))


class TestApplicationInPreview(unittest.TestCase):
    """Every path that could open a device answers 'preview' instead."""

    def _app(self):
        from rfid_finishlynx.app import Application
        app = Application.__new__(Application)
        app.hardware_allowed = False
        app.preview_reason = "unit test"
        app._running = True
        app.reader = None
        app.camera_start = None
        app.finishlynx = None
        app.scoreboard_listener = None
        app.lora_receiver = None
        app.remote_manager = None
        return app

    def test_restart_handlers_refuse(self):
        app = self._app()
        self.assertFalse(app.restart_reader())
        self.assertFalse(app.restart_scoreboard_listener())
        self.assertFalse(app._try_adopt_other_reader_type())
        for fn in (app.restart_camera_start, app.reconnect_finishlynx, app.restart_lora):
            d = fn()
            self.assertFalse(d["success"])
            self.assertIn("preview", d["message"])
        self.assertIsNone(app.reader)
        self.assertIsNone(app.camera_start)
        self.assertIsNone(app.finishlynx)

    def test_start_in_preview_opens_nothing(self):
        """The real start() with a finish-line config that names a reader,
        clocks, LoRa gateway and camera: in preview none of them is built."""
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config
        cfg = Config()
        cfg.mode = "finish_line"
        cfg.reader.host = "192.0.2.1"            # documentation range, never routable
        cfg.lora.enabled = True
        cfg.lora.port = "192.0.2.2:8886"
        cfg.led_clock.enabled = True
        cfg.camera_start.enabled = True
        cfg.event_directory = ""
        with mock.patch.dict(os.environ, {hw_guard.PREVIEW_ENV: "1"}):
            app = Application(cfg)
        try:
            with mock.patch("rfid_finishlynx.app.create_reader") as mk_reader, \
                    mock.patch("rfid_finishlynx.app.FinishLynxOutput") as mk_fl, \
                    mock.patch("rfid_finishlynx.app.ScoreboardListener") as mk_sb, \
                    mock.patch("rfid_finishlynx.app.LoRaReceiver") as mk_lora, \
                    mock.patch("rfid_finishlynx.app.CameraStartListener") as mk_cam:
                self.assertTrue(app.start())
            for mk in (mk_reader, mk_fl, mk_sb, mk_lora, mk_cam):
                mk.assert_not_called()
            self.assertTrue(app._running)
            self.assertIsNone(app.reader)
            self.assertIsNone(app.led_clock_service)
            self.assertIsNone(app.camera_start)
            self.assertIsNone(app.lora_receiver)
            self.assertIsNone(app.finishlynx)
            self.assertIsNone(app.scoreboard_listener)
            st = app.get_status()
            self.assertTrue(st["preview"])
            self.assertIn("RFID_PREVIEW", st["preview_reason"])
            self.assertFalse(app.test_reader_connection()["success"])
        finally:
            app.stop()

    def test_construction_under_env_flag_is_preview(self):
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config
        with mock.patch.dict(os.environ, {hw_guard.PREVIEW_ENV: "1"}):
            app = Application(Config())
        self.assertFalse(app.hardware_allowed)
        self.assertIn("RFID_PREVIEW", app.preview_reason)
        self.assertFalse(app.hw_lock.held)             # never even asked for it

    def test_construction_is_preview_when_lock_is_held(self):
        from rfid_finishlynx.app import Application
        from rfid_finishlynx.config import Config
        # (the staging copy carries a PREVIEW_ONLY marker, which would win;
        # here the lock alone must decide)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("rfid_finishlynx.app.preview_requested", lambda cfg=None: None), \
                mock.patch.dict(os.environ, {hw_guard.LOCK_PATH_ENV: str(Path(d) / "hw.lock")}):
            holder = HardwareLock()
            owner = None
            try:
                self.assertTrue(holder.acquire())
                app = Application(Config())
                self.assertFalse(app.hardware_allowed)
                self.assertIn("owns the hardware", app.preview_reason)
                self.assertEqual(app.hw_lock.owner_pid, os.getpid())
                holder.release()
                # ...and with nobody holding it, the next one owns the hardware
                owner = Application(Config())
                self.assertTrue(owner.hardware_allowed)
                self.assertTrue(owner.hw_lock.held)
            finally:
                holder.release()
                if owner is not None:
                    owner.hw_lock.release()


if __name__ == "__main__":
    unittest.main()
