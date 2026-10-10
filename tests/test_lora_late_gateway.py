"""The finish console boots before the LoRa gateway answers (the Ebyte box
takes about a minute after power-up). The receiver's first connect fails,
the transport keeps retrying and the link opens later - and from then on
split reads arrive. The receiver must report itself running once that
happens; it used to stay "not running" forever, so the console alarmed
"radio link down" all meet while reads were flowing (2026-09-26).
Runs without hardware: python -m unittest tests.test_lora_late_gateway"""

import socket
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from rfid_finishlynx.config import LoRaConfig
from rfid_finishlynx.lora_receiver import LoRaReceiver
from rfid_finishlynx.transport import LoRaTransport, format_lora_message


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


class TestLateGateway(unittest.TestCase):

    def test_receiver_runs_once_the_gateway_comes_up(self):
        # Bound but not listening: connections are refused, like a gateway
        # that is still booting.
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        host, port = server.getsockname()

        reads = []
        receiver = LoRaReceiver(
            LoRaConfig(enabled=True, port=f"{host}:{port}", send_interval_sec=0),
            on_message=lambda msg: reads.append(msg),
        )
        with patch.object(LoRaTransport, "RECONNECT_MIN_SEC", 0.05), \
                patch.object(LoRaTransport, "RECONNECT_MAX_SEC", 0.2):
            try:
                self.assertFalse(receiver.start())      # gateway not up yet
                self.assertFalse(receiver.is_running)
                self.assertIsNotNone(receiver.transport)

                server.listen(1)                        # gateway finishes booting
                conn, _ = server.accept()
                self.assertTrue(wait_for(lambda: receiver.is_running),
                                "receiver never reported running after the link opened")

                conn.sendall(format_lora_message("644", datetime.now(), "S1"))
                self.assertTrue(wait_for(lambda: reads))
                self.assertEqual(reads[0].bib, "644")
                conn.close()
            finally:
                receiver.stop()
                server.close()

    def test_stop_while_waiting_ends_the_retry_thread(self):
        receiver = LoRaReceiver(
            LoRaConfig(enabled=True, port="127.0.0.1:1", send_interval_sec=0),
            on_message=lambda msg: None,
        )
        with patch.object(LoRaTransport, "RECONNECT_MIN_SEC", 0.05), \
                patch.object(LoRaTransport, "RECONNECT_MAX_SEC", 0.1):
            self.assertFalse(receiver.start())
            transport = receiver.transport
            receiver.stop()
            time.sleep(0.3)
            self.assertFalse(receiver.is_running)
            self.assertFalse(transport.is_connected)
            self.assertIsNone(transport._receive_thread)


if __name__ == "__main__":
    unittest.main()
