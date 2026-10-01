"""Enabling / re-pointing LoRa from the console must rebuild the station
manager on the new transport. It used to rebuild only the receiver, so a
console that booted with LoRa off never listed a station, dropped every
ANNOUNCE and sent commands nowhere - while reads still arrived.
Runs without hardware: python -m unittest tests.test_lora_restart"""

import unittest
from unittest.mock import patch

from rfid_finishlynx import app as app_module
from rfid_finishlynx.app import Application
from rfid_finishlynx.config import Config


class FakeReceiver:
    def __init__(self, config, on_message, on_management_message, journal_path=None):
        self.transport = object()
        self.is_running = False
        self.stopped = False

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False
        self.stopped = True


class FakeManager:
    def __init__(self, transport, config, on_unit_change=None):
        self.transport = transport
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_app(enabled=True):
    app = Application.__new__(Application)
    app.config = Config()
    app.config.mode = "finish_line"
    app.config.lora.enabled = enabled
    app.config.lora.port = "127.0.0.1:1"
    app._running = True
    app.lora_receiver = None
    app.lora_transport = None
    app._lora_seen_up = False
    app.remote_manager = None
    return app


class TestRestartLora(unittest.TestCase):

    def setUp(self):
        self.p1 = patch.object(app_module, "LoRaReceiver", FakeReceiver)
        self.p2 = patch.object(app_module, "RemoteManager", FakeManager)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def test_enabling_after_boot_creates_the_manager(self):
        app = make_app()
        out = app.restart_lora()
        self.assertTrue(out["success"], out)
        self.assertIsNotNone(app.remote_manager)
        self.assertTrue(app.remote_manager.started)
        self.assertIs(app.remote_manager.transport, app.lora_receiver.transport)

    def test_repointing_rebinds_the_manager_to_the_new_transport(self):
        app = make_app()
        app.restart_lora()
        old_mgr, old_rx = app.remote_manager, app.lora_receiver
        app.config.lora.port = "127.0.0.1:2"
        app.restart_lora()
        self.assertTrue(old_mgr.stopped)
        self.assertFalse(old_rx.is_running)
        self.assertIsNot(app.remote_manager, old_mgr)
        self.assertIs(app.remote_manager.transport, app.lora_receiver.transport)

    def test_receiver_waiting_for_the_gateway_is_still_stopped(self):
        # A receiver whose gateway has not answered yet reports is_running
        # False, but its transport thread is alive and retrying; a restart
        # must tear it down rather than leak it.
        app = make_app()
        app.restart_lora()
        waiting = app.lora_receiver
        waiting.is_running = False
        app.restart_lora()
        self.assertTrue(waiting.stopped)
        self.assertIsNot(app.lora_receiver, waiting)

    def test_disabling_drops_the_manager(self):
        app = make_app()
        app.restart_lora()
        mgr = app.remote_manager
        app.config.lora.enabled = False
        out = app.restart_lora()
        self.assertEqual(out["message"], "LoRa disabled")
        self.assertTrue(mgr.stopped)
        self.assertIsNone(app.remote_manager)
        self.assertIsNone(app.lora_receiver)
        self.assertEqual(app.get_remote_units(), [])


if __name__ == "__main__":
    unittest.main()
