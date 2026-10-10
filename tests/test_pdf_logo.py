"""Report logo (2026-10-03): stdlib PNG/JPEG decoding and the header swap."""
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

from rfid_finishlynx.meet import pdf_reports, pdfdoc

sys.path.insert(0, os.path.dirname(__file__))
from test_meet_pdf import roster_meet  # noqa: E402


def png(w, h, ctype, rows, depth=8, plte=b"", trns=b""):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(bytes([f]) + r for f, r in rows)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0))
    if plte:
        out += chunk(b"PLTE", plte)
    if trns:
        out += chunk(b"tRNS", trns)
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class TestImage(unittest.TestCase):
    def test_rgba_with_filters(self):
        # 2x2 RGBA; row 0 filter None, row 1 filter Sub (adds the pixel to its left)
        rows = [(0, bytes([10, 20, 30, 255, 40, 50, 60, 0])),
                (1, bytes([1, 2, 3, 128, 1, 1, 1, 127]))]
        img = pdfdoc.Image(png(2, 2, 6, rows))
        self.assertEqual((img.width, img.height, img.colorspace), (2, 2, "/DeviceRGB"))
        self.assertEqual(zlib.decompress(img.data), bytes([10, 20, 30, 40, 50, 60, 1, 2, 3, 2, 3, 4]))
        self.assertEqual(zlib.decompress(img.alpha), bytes([255, 0, 128, 255]))

    def test_opaque_rgba_needs_no_mask(self):
        img = pdfdoc.Image(png(1, 1, 6, [(0, bytes([1, 2, 3, 255]))]))
        self.assertIsNone(img.alpha)

    def test_palette_4bit_with_transparency(self):
        img = pdfdoc.Image(png(2, 1, 3, [(0, bytes([0x01]))], depth=4,
                               plte=bytes([255, 0, 0, 0, 0, 255]), trns=bytes([0])))
        self.assertEqual(zlib.decompress(img.data), bytes([255, 0, 0, 0, 0, 255]))
        self.assertEqual(zlib.decompress(img.alpha), bytes([0, 255]))

    def test_jpeg_header(self):
        sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 40, 300, 3) + b"\0" * 9
        img = pdfdoc.Image(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + sof + b"\xff\xd9")
        self.assertEqual((img.width, img.height, img.filter), (300, 40, "/DCTDecode"))

    def test_refuses_other_files(self):
        with self.assertRaises(pdfdoc.ImageError):
            pdfdoc.Image(b"GIF89a....")
        with self.assertRaises(pdfdoc.ImageError):
            pdfdoc.Image(png(1, 1, 2, [(0, b"\0" * 6)], depth=16))


class TestReportLogo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="logo-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._old = pdf_reports.BRANDING_DIR
        pdf_reports.BRANDING_DIR = self.dir
        self.addCleanup(setattr, pdf_reports, "BRANDING_DIR", self._old)

    def test_wordmark_without_a_logo(self):
        out = pdf_reports.rosters_pdf(roster_meet(3, 3), ["t1"])
        self.assertNotIn(b"/XObject", out)

    def test_logo_replaces_the_wordmark(self):
        with open(os.path.join(self.dir, "report-logo.png"), "wb") as f:
            f.write(png(4, 1, 6, [(0, bytes([9, 9, 9, 0]) * 4)]))
        out = pdf_reports.rosters_pdf(roster_meet(3, 3), ["t1"])
        self.assertEqual(out.count(b"/Subtype /Image"), 2)     # image + its soft mask, once
        self.assertIn(b"/XObject <<", out)

    def test_unreadable_logo_falls_back(self):
        with open(os.path.join(self.dir, "report-logo.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nbroken")
        self.assertIsNone(pdf_reports.report_logo())


if __name__ == "__main__":
    unittest.main()
