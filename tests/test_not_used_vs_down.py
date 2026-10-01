"""Not used at this meet vs. down (2026-09-30, user's rule): the split-station
radio alarm and the Clocks chip only count a link / clock that has been up
since the console started. A LoRa gateway that never answered, or a
configured clock that was never plugged in, is simply not in use today; one
that was up and then went away is a real problem and still shows.
Runs without hardware: python -m unittest tests.test_not_used_vs_down"""

import socket
import unittest
from types import SimpleNamespace

from rfid_finishlynx.app import Application
from rfid_finishlynx.config import LoRaConfig
from rfid_finishlynx.transport import LoRaTransport
from tests.test_led_clock_multi import Harness


class TestLoraEverConnected(unittest.TestCase):

    def test_transport_remembers_that_the_link_opened(self):
        refused = LoRaTransport(LoRaConfig(enabled=True, port="127.0.0.1:1", send_interval_sec=0))
        self.assertFalse(refused._open())
        self.assertFalse(refused.ever_connected)

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        t = LoRaTransport(LoRaConfig(enabled=True, port=f"{host}:{port}", send_interval_sec=0))
        try:
            self.assertTrue(t._open())
            self.assertTrue(t.ever_connected)
            t._drop()                                   # gateway goes away
            self.assertFalse(t.is_connected)
            self.assertTrue(t.ever_connected)           # ...but it WAS here
        finally:
            t.disconnect()
            server.close()

    def _app(self, receiver_transport=None, transport=None):
        return SimpleNamespace(
            _lora_seen_up=False,
            lora_receiver=SimpleNamespace(transport=receiver_transport) if receiver_transport else None,
            lora_transport=transport)

    def test_never_answered_since_start_is_not_an_outage(self):
        app = self._app(receiver_transport=SimpleNamespace(ever_connected=False))
        self.assertFalse(Application.lora_link_seen_up(app))

    def test_survives_a_radio_restart(self):
        """Restart in the console builds a fresh transport (ever_connected
        False); a link that was up before must still count."""
        app = self._app(receiver_transport=SimpleNamespace(ever_connected=True))
        self.assertTrue(Application.lora_link_seen_up(app))
        app.lora_receiver = SimpleNamespace(transport=SimpleNamespace(ever_connected=False))
        self.assertTrue(Application.lora_link_seen_up(app))
        app.lora_receiver = None
        self.assertTrue(Application.lora_link_seen_up(app))

    def test_station_side_transport_counts_too(self):
        app = self._app(transport=SimpleNamespace(ever_connected=True))
        self.assertTrue(Application.lora_link_seen_up(app))


class TestClockSeen(unittest.TestCase):

    def test_configured_clock_never_plugged_in_is_not_seen(self):
        h = Harness([("ch340", "COM10")],
                    clocks=[{"port": "COM10", "type": "ch340", "label": "Finish"},
                            {"port": "COM5", "type": "electro", "label": "Mile 1"}])
        h.svc._connect()
        by_port = {c["port"]: c for c in h.svc.clocks}
        self.assertEqual((by_port["COM10"]["status"], by_port["COM10"]["seen"]), ("driving", True))
        self.assertEqual((by_port["COM5"]["status"], by_port["COM5"]["seen"]), ("missing", False))

    def test_clock_unplugged_after_being_used_stays_seen(self):
        h = Harness([("electro", "COM5")], clocks=[{"port": "COM5", "type": "electro", "label": "Mile 1"}])
        h.svc._connect()
        self.assertEqual([(c["status"], c["seen"]) for c in h.svc.clocks], [("driving", True)])
        h.drivers["COM5"].fail_next = True              # cable pulled
        h.svc._drive_all()
        h.found = []
        h.svc._connect()
        self.assertEqual([(c["status"], c["seen"]) for c in h.svc.clocks], [("missing", True)])

    def test_plugged_in_but_port_busy_counts_as_seen(self):
        """FinishLynx holding the port: the clock is here, just not ours."""
        h = Harness([("ch340", "COM11")], busy_ports={"COM11"},
                    clocks=[{"port": "COM11", "type": "ch340", "label": "Finish"}])
        h.svc._connect()
        self.assertEqual([(c["status"], c["seen"]) for c in h.svc.clocks], [("busy", True)])


if __name__ == "__main__":
    unittest.main()
