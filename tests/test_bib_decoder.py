"""EPC -> bib decoding, with the tags that went wrong at real meets.
python -m unittest tests.test_bib_decoder (runs without pytest on the timing PC)."""

import unittest

from rfid_finishlynx.config import ReaderConfig, TimingConfig
from rfid_finishlynx.rfid_reader import RFIDReader


def decode(epc: str):
    return RFIDReader(ReaderConfig(), TimingConfig())._extract_bib_from_epc(epc)


class TestLiteralDecimalTags(unittest.TestCase):
    """Our supplier writes the bib as decimal digits in the hex."""

    def test_bibs_30_to_39_from_the_meet_logs(self):
        # Read at the finish 2026-09-12 .. 09-19 and reported as 0..9 because
        # the byte 0x3N is also the ASCII character "N" and the NUL padding
        # was stripped before the digit check.
        for epc, bib in [("00000030", "30"), ("00000031", "31"), ("00000032", "32"),
                         ("00000035", "35"), ("00000039", "39")]:
            self.assertEqual(decode(epc), bib, epc)
        for bib in range(30, 40):
            self.assertEqual(decode(f"{bib:08d}"), str(bib))

    def test_96_bit_epc_with_the_bib_in_the_last_word(self):
        self.assertEqual(decode("000000000000000000000035"), "35")
        self.assertEqual(decode("e2000000000000000000" + "0035"), "35")

    def test_four_digit_bibs_in_the_30xx_family(self):
        for epc, bib in [("00003031", "3031"), ("00003535", "3535"), ("00003939", "3939"),
                         ("00003030", "3030")]:
            self.assertEqual(decode(epc), bib, epc)

    def test_everyday_bibs_unchanged(self):
        for epc, bib in [("00000016", "16"), ("00000001", "1"), ("00000000", "0"),
                         ("00000123", "123"), ("00012345", "12345"), ("00000040", "40"),
                         ("00000029", "29")]:
            self.assertEqual(decode(epc), bib, epc)


class TestNoAsciiPath(unittest.TestCase):

    def test_ascii_text_epc_is_unknown_not_misread(self):
        # "000000000021" written as ASCII text - not a format we own
        self.assertIsNone(decode(bytes("000000000021", "ascii").hex()))
        self.assertIsNone(decode("aabbccdd" + bytes("0012", "ascii").hex()))

    def test_hex_letters_still_read_as_integer(self):
        self.assertEqual(decode("00000a2c"), str(0x0A2C))

    def test_empty(self):
        self.assertIsNone(decode(""))


if __name__ == "__main__":
    unittest.main()
