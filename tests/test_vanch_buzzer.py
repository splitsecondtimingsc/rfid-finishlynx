"""A fresh Vanch beeps on every tag read; the console turns the buzzer off on
each connect (reader.beep, default off) so a new timer never has to
(2026-09-30; set by hand on this rig on 9/26).
unittest style: python -m unittest tests.test_vanch_buzzer"""

import unittest
from unittest import mock

import rfid_finishlynx.vanch_reader as vr
from rfid_finishlynx.config import ReaderConfig, TimingConfig, load_config
from rfid_finishlynx.vanch_reader import VanchReader, get_buzzer, set_buzzer
from tests.test_vanch_ports import FakeVupReader


class FakeBuzzerReader(FakeVupReader):
    """Also answers 0BH (set buzzer) / 0CH (get buzzer)."""

    def __init__(self, buzzer=True):
        self.buzzer = buzzer
        self.sets = 0
        super().__init__(ports=8)

    def serve(self):
        import socket
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except (socket.timeout, OSError):
                return
            cmd = data[4]
            if cmd == 0x0C:
                self.sock.sendto(self.reply(0x0C, bytes([1 if self.buzzer else 0])), addr)
            elif cmd == 0x0B:
                self.buzzer = data[5] != 0
                self.sets += 1
                self.sock.sendto(self.reply(0x0B), addr)


class BuzzerCase(unittest.TestCase):
    start_on = True

    def setUp(self):
        self.srv = FakeBuzzerReader(buzzer=self.start_on)
        self._old = vr.VUP_PORT
        vr.VUP_PORT = self.srv.port

    def tearDown(self):
        vr.VUP_PORT = self._old
        self.srv.close()

    def reader(self, **kw):
        return VanchReader(ReaderConfig(host="127.0.0.1", port=502, type="vanch", **kw), TimingConfig())


class TestBuzzerOffByDefault(BuzzerCase):
    start_on = True

    def test_get_and_set(self):
        self.assertTrue(get_buzzer("127.0.0.1"))
        set_buzzer("127.0.0.1", False)
        self.assertFalse(get_buzzer("127.0.0.1"))

    def test_connect_turns_a_beeping_reader_off(self):
        r = self.reader()
        r._apply_buzzer("127.0.0.1")
        self.assertFalse(self.srv.buzzer)
        self.assertIs(r.buzzer_on, False)

    def test_beep_true_leaves_it_on(self):
        r = self.reader(beep=True)
        r._apply_buzzer("127.0.0.1")
        self.assertTrue(self.srv.buzzer)
        self.assertEqual(self.srv.sets, 0)
        self.assertIs(r.buzzer_on, True)


class TestAlreadyOff(BuzzerCase):
    start_on = False

    def test_already_off_is_not_rewritten(self):
        r = self.reader()
        r._apply_buzzer("127.0.0.1")
        self.assertEqual(self.srv.sets, 0)
        self.assertIs(r.buzzer_on, False)


class TestNoVup(unittest.TestCase):

    def test_unreachable_is_not_fatal(self):
        with mock.patch.object(vr, "vup_request", side_effect=OSError("timed out")):
            r = VanchReader(ReaderConfig(host="127.0.0.1", port=502, type="vanch"), TimingConfig())
            r._apply_buzzer("127.0.0.1")                 # must not raise
            self.assertIsNone(r.buzzer_on)


class TestConfig(unittest.TestCase):

    def test_beep_defaults_off_and_loads(self):
        import os
        import tempfile
        self.assertFalse(ReaderConfig().beep)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.yaml")
            with open(p, "w") as f:
                f.write("reader:\n  type: vanch\n  host: 192.168.0.128\n  beep: true\n")
            self.assertTrue(load_config(p).reader.beep)


if __name__ == "__main__":
    unittest.main()
