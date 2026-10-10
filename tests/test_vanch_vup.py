"""VUP (Vanch native protocol) framing for the polling-antenna commands,
checked against the examples in 'VUP Protocol Development Handbook V2.1.9'."""

import threading
import socket
import unittest

from rfid_finishlynx.vanch_reader import (
    get_polling_antennas, set_polling_antennas, vup_checksum, vup_frame, vup_parse_reply,
)


class TestVupFraming(unittest.TestCase):

    def test_set_one_antenna_matches_handbook(self):
        self.assertEqual(vup_frame(0x15, b"\x01").hex(), "400004011501a5")

    def test_get_matches_handbook(self):
        self.assertEqual(vup_frame(0x16).hex(), "4000030116a6")

    def test_set_two_antennas_length_counts_params(self):
        # Handbook example prints '40 00 04 01 15 01 02 A2' - its checksum A2
        # only holds with length 05, so the 04 is a typo in the document.
        self.assertEqual(vup_frame(0x15, b"\x01\x02").hex(), "40000501150102a2")

    def test_relay_example_six_params(self):
        self.assertEqual(vup_frame(0x09, bytes.fromhex("010003000301")).hex(),
                         "400009010901000300030 1a5".replace(" ", ""))

    def test_parse_replies(self):
        ok, params = vup_parse_reply(bytes.fromhex("F0000301 15F7".replace(" ", "")))
        self.assertTrue(ok)
        self.assertEqual(params, b"")
        ok, params = vup_parse_reply(bytes.fromhex("F00005011601 02F1".replace(" ", "")))
        self.assertTrue(ok)
        self.assertEqual(list(params), [1, 2])
        with self.assertRaises(IOError):
            vup_parse_reply(bytes.fromhex("F0000301 15F8".replace(" ", "")))   # bad checksum

    def test_checksum(self):
        self.assertEqual(vup_checksum(bytes.fromhex("400004011501")), 0xA5)


class FakeVupReader:
    """UDP server answering 15H/16H like the reader does."""

    def __init__(self, antennas):
        self.antennas = list(antennas)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(3.0)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def reply(self, cmd, params=b""):
        body = bytes([0x01, cmd]) + params
        head = bytes([0xF0]) + (len(body) + 1).to_bytes(2, "big") + body
        return head + bytes([vup_checksum(head)])

    def serve(self):
        for _ in range(4):
            try:
                data, addr = self.sock.recvfrom(1024)
            except (socket.timeout, OSError):     # timeout, or socket closed by the test
                return
            cmd = data[4]
            if cmd == 0x16:
                self.sock.sendto(self.reply(0x16, bytes(self.antennas)), addr)
            elif cmd == 0x15:
                self.antennas = list(data[5:-1])
                self.sock.sendto(self.reply(0x15), addr)


class TestPollingAntennaRoundTrip(unittest.TestCase):

    def test_get_then_set(self):
        import rfid_finishlynx.vanch_reader as vr
        srv = FakeVupReader([1])
        old = vr.VUP_PORT
        vr.VUP_PORT = srv.port
        try:
            self.assertEqual(get_polling_antennas("127.0.0.1"), [1])
            set_polling_antennas("127.0.0.1", [7, 1, 5, 3])
            self.assertEqual(get_polling_antennas("127.0.0.1"), [1, 3, 5, 7])
        finally:
            vr.VUP_PORT = old
            srv.sock.close()

    def test_rejects_empty_or_out_of_range(self):
        with self.assertRaises(ValueError):
            set_polling_antennas("127.0.0.1", [0, 9])


if __name__ == "__main__":
    unittest.main()
