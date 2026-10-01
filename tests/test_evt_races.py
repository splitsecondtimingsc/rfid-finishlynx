import os
import tempfile
import unittest

from rfid_finishlynx.web import evt_race_list, find_evt_file

MEETPRO_EVT = (
    ";Created by DirectAthletics MeetPro\r\n"
    "1,1,1,Boys 5K,,,,,,5000\r\n"
    ",101,,Black,Kinsley,Belton-Honea Path\r\n"
    ",102,,Doe,Jane,Somewhere\r\n"
    "2,1,1,Girls 5K,,,,,,5000\r\n"
    "3,2,4,Boys 4x400 Relay,,,,,,1600\r\n"
    ",,1,Somewhere,,Somewhere A\r\n"
)


class TestEvtRaceList(unittest.TestCase):

    def test_parses_headers_in_file_order_with_entry_counts(self):
        races = evt_race_list(MEETPRO_EVT)
        self.assertEqual([(r['event'], r['round'], r['heat']) for r in races],
                         [(1, 1, 1), (2, 1, 1), (3, 2, 4)])
        self.assertEqual([r['name'] for r in races], ['Boys 5K', 'Girls 5K', 'Boys 4x400 Relay'])
        self.assertEqual([r['gender'] for r in races], ['M', 'F', 'M'])
        self.assertEqual([r['entries'] for r in races], [2, 0, 1])
        self.assertEqual(races[0]['distance'], '5000')
        self.assertTrue(races[2]['is_relay'])

    def test_find_evt_prefers_lynx_evt_any_case(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "lynx.EVT"), "w").close()
            open(os.path.join(d, "other.evt"), "w").close()
            self.assertEqual(find_evt_file(d), "lynx.EVT")

    def test_find_evt_single_other_file(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "meet.evt"), "w").close()
            self.assertEqual(find_evt_file(d), "meet.evt")
            open(os.path.join(d, "meet2.evt"), "w").close()
            self.assertIsNone(find_evt_file(d))        # ambiguous
        self.assertIsNone(find_evt_file(r"C:\definitely\not\here"))


if __name__ == "__main__":
    unittest.main()
