import unittest
from datetime import datetime

from rfid_finishlynx.config import FinishLynxConfig
from rfid_finishlynx.finishlynx import (
    EOT,
    OPCODE_SPLIT,
    OPCODE_TIME,
    OPCODE_ZERO,
    SOT_NO_SYNC,
    SOT_SYNC_OK,
    FinishLynxOutput,
    ScoreboardListener,
)


class TestScoreboardParsing(unittest.TestCase):

    def setUp(self):
        self.listener = ScoreboardListener()

    def test_parse_example_format(self):
        """Key-value LSS format updates event/round/heat and race name."""
        data = (
            "*** Start List Header ***\r\n"
            "OFFICIAL/UNOFFICIAL: UNOFFICIAL\r\n"
            "Event name         : Men's 800 Meters\r\n"
            "Wind               : nwi\r\n"
            "Event number       : 3\r\n"
            "Round number       : 1\r\n"
            "Heat number        : 2\r\n"
            "*** Start List Trailer ***\r\n"
        )
        changed = self.listener._parse_example_format(data)
        self.assertTrue(changed)
        self.assertEqual(self.listener.event, 3)
        self.assertEqual(self.listener.heat, 2)
        self.assertEqual(self.listener.round, 1)
        self.assertEqual(self.listener.race_name, "Men's 800 Meters")

    def test_parse_example_format_same_event_not_changed(self):
        """Re-sending the same event/heat does not report a change."""
        data = "Event number: 3\nHeat number: 2\n"
        self.assertTrue(self.listener._parse_example_format(data))
        self.assertFalse(self.listener._parse_example_format(data))

    def test_running_time_packet_does_not_reset_event(self):
        """Packets without event info must not clobber the current event."""
        self.listener._parse_example_format("Event number: 5\nHeat number: 1\n")
        changed = self.listener._parse_example_format("Running Time: 1:23.456\n")
        self.assertFalse(changed)
        self.assertEqual(self.listener.event, 5)
        self.assertEqual(self.listener.heat, 1)

    def test_parse_pipe_format(self):
        """Pipe-delimited format extracts event/round/heat."""
        data = "unofficial|Boys 1600m|nwi|4|1|3|4-1-3|auto;"
        changed = self.listener._parse_pipe_format(data)
        self.assertTrue(changed)
        self.assertEqual(self.listener.event, 4)
        self.assertEqual(self.listener.heat, 3)
        self.assertEqual(self.listener.round, 1)

    def test_parse_generic_format(self):
        """Free-text format falls back to regex extraction."""
        changed = self.listener._parse_generic_format("Event 7 Heat 2 now loading")
        self.assertTrue(changed)
        self.assertEqual(self.listener.event, 7)
        self.assertEqual(self.listener.heat, 2)


class TestLapTimePackets(unittest.TestCase):

    def setUp(self):
        self.output = FinishLynxOutput(FinishLynxConfig())
        self.dt = datetime(2026, 8, 4, 14, 30, 15, 123000)

    def test_format_time(self):
        self.assertEqual(FinishLynxOutput.format_time(self.dt), "14:30:15.123")

    def test_split_packet_data_sot_and_eot(self):
        """S packets use the no-sync SOT byte and CRLF EOT."""
        packet = self.output._build_packet(OPCODE_SPLIT, "14:30:15.123", "42")
        self.assertEqual(packet[0], SOT_NO_SYNC)
        self.assertTrue(packet.endswith(EOT))
        self.assertEqual(packet[1:-2], b"S,14:30:15.123,42")

    def test_split_packet_with_event_round_heat(self):
        packet = self.output._build_packet(OPCODE_SPLIT, "14:30:15.123", "42", 3, 1, 2)
        self.assertEqual(packet[1:-2], b"S,14:30:15.123,42,3,1,2")

    def test_split_packet_omits_partial_event_info(self):
        """Event/round/heat only appended when all three are present."""
        packet = self.output._build_packet(OPCODE_SPLIT, "14:30:15.123", "42", 3, None, 2)
        self.assertEqual(packet[1:-2], b"S,14:30:15.123,42")

    def test_time_sync_packet_uses_sync_sot(self):
        """T packets use the sync-OK SOT byte."""
        packet = self.output._build_packet(OPCODE_TIME, "14:30:15.123")
        self.assertEqual(packet[0], SOT_SYNC_OK)
        self.assertEqual(packet[1:-2], b"T,14:30:15.123")

    def test_zero_packet(self):
        packet = self.output._build_packet(OPCODE_ZERO, "14:30:15.123")
        self.assertEqual(packet[0], SOT_NO_SYNC)
        self.assertEqual(packet[1:-2], b"Z,14:30:15.123")


if __name__ == '__main__':
    unittest.main()
