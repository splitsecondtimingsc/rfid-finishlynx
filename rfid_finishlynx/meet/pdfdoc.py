"""A small stdlib PDF writer: Letter pages, the four Helvetica faces, text,
lines, filled/stroked rectangles and PNG/JPEG images (the timing company's
logo, 2026-10-03). Enough for the MeetPro-style results and rosters
(pdf_reports.py), nothing more.

Why home-made (2026-10-03): the timing net is offline and every dependency
has to be wheeled into `wheels/`; reportlab is ~4 MB of wheels per Python
version for what is a few hundred lines here. printing.to_pdf already does a
Courier-only PDF; this one adds proportional fonts, which need their glyph
widths to measure, right-align and truncate text (a PDF reader positions
glyphs itself, so we must know their widths to keep columns apart).

Fonts: the standard 14 Helvetica / Helvetica-Bold / -Oblique / -BoldOblique,
not embedded (every PDF reader carries them, the file stays small), with
WinAnsiEncoding. Their widths are the Adobe AFM ones for the WinAnsi range
below; an oblique face has the same widths as its upright face. The tables
were taken from Arial (metric-compatible with Helvetica by design) and
checked against the AFM values, with the four glyphs where Arial differs
(macron, plusminus, mu, divide) set to the AFM widths.

Coordinates: points, origin at the TOP-left, y down (the reports lay out
top to bottom); a text y is its baseline. Converted to PDF's bottom-left
origin when written.

Text outside WinAnsi: an accent the encoding lacks is dropped (NFKD), a few
letters NFKD can't split are spelled out ("Ł" -> "L"), anything else
becomes "?". One output byte per character, so measuring a str measures what
is printed.
"""

from __future__ import annotations

import struct
import unicodedata
import zlib
from typing import Dict, List, Optional, Tuple

PAGE_W, PAGE_H = 612.0, 792.0                  # Letter portrait, points

HELVETICA = "Helvetica"
BOLD = "Helvetica-Bold"
OBLIQUE = "Helvetica-Oblique"
BOLD_OBLIQUE = "Helvetica-BoldOblique"
FONTS = (HELVETICA, BOLD, OBLIQUE, BOLD_OBLIQUE)
_FONT_TAG = {f: f"F{i}" for i, f in enumerate(FONTS, 1)}

# Advance widths (1/1000 em) for WinAnsi codes 0x20..0xFF; 0 = no glyph
# (codes cp1252 leaves undefined, never emitted by _encode).
_HELVETICA = (
     278,  278,  355,  556,  556,  889,  667,  191,  333,  333,  389,  584,  278,  333,  278,  278,  # 0x20
     556,  556,  556,  556,  556,  556,  556,  556,  556,  556,  278,  278,  584,  584,  584,  556,  # 0x30
    1015,  667,  667,  722,  722,  667,  611,  778,  722,  278,  500,  667,  556,  833,  722,  778,  # 0x40
     667,  778,  722,  667,  611,  722,  667,  944,  667,  667,  611,  278,  278,  278,  469,  556,  # 0x50
     333,  556,  556,  500,  556,  556,  278,  556,  556,  222,  222,  500,  222,  833,  556,  556,  # 0x60
     556,  556,  333,  500,  278,  556,  500,  722,  500,  500,  500,  334,  260,  334,  584,  278,  # 0x70
     556,    0,  222,  556,  333, 1000,  556,  556,  333, 1000,  667,  333, 1000,    0,  611,    0,  # 0x80
       0,  222,  222,  333,  333,  350,  556, 1000,  333, 1000,  500,  333,  944,    0,  500,  667,  # 0x90
     278,  333,  556,  556,  556,  556,  260,  556,  333,  737,  370,  556,  584,  333,  737,  333,  # 0xa0
     400,  584,  333,  333,  333,  556,  537,  333,  333,  333,  365,  556,  834,  834,  834,  611,  # 0xb0
     667,  667,  667,  667,  667,  667, 1000,  722,  667,  667,  667,  667,  278,  278,  278,  278,  # 0xc0
     722,  722,  778,  778,  778,  778,  778,  584,  778,  722,  722,  722,  722,  667,  667,  611,  # 0xd0
     556,  556,  556,  556,  556,  556,  889,  500,  556,  556,  556,  556,  278,  278,  278,  278,  # 0xe0
     556,  556,  556,  556,  556,  556,  556,  584,  611,  556,  556,  556,  556,  500,  556,  500,  # 0xf0
)
_HELVETICA_BOLD = (
     278,  333,  474,  556,  556,  889,  722,  238,  333,  333,  389,  584,  278,  333,  278,  278,  # 0x20
     556,  556,  556,  556,  556,  556,  556,  556,  556,  556,  333,  333,  584,  584,  584,  611,  # 0x30
     975,  722,  722,  722,  722,  667,  611,  778,  722,  278,  556,  722,  611,  833,  722,  778,  # 0x40
     667,  778,  722,  667,  611,  722,  667,  944,  667,  667,  611,  333,  278,  333,  584,  556,  # 0x50
     333,  556,  611,  556,  611,  556,  333,  611,  611,  278,  278,  556,  278,  889,  611,  611,  # 0x60
     611,  611,  389,  556,  333,  611,  556,  778,  556,  556,  500,  389,  280,  389,  584,  278,  # 0x70
     556,    0,  278,  556,  500, 1000,  556,  556,  333, 1000,  667,  333, 1000,    0,  611,    0,  # 0x80
       0,  278,  278,  500,  500,  350,  556, 1000,  333, 1000,  556,  333,  944,    0,  500,  667,  # 0x90
     278,  333,  556,  556,  556,  556,  280,  556,  333,  737,  370,  556,  584,  333,  737,  333,  # 0xa0
     400,  584,  333,  333,  333,  611,  556,  333,  333,  333,  365,  556,  834,  834,  834,  611,  # 0xb0
     722,  722,  722,  722,  722,  722, 1000,  722,  667,  667,  667,  667,  278,  278,  278,  278,  # 0xc0
     722,  722,  778,  778,  778,  778,  778,  584,  778,  722,  722,  722,  722,  667,  667,  611,  # 0xd0
     556,  556,  556,  556,  556,  556,  889,  556,  556,  556,  556,  556,  278,  278,  278,  278,  # 0xe0
     611,  611,  611,  611,  611,  611,  611,  584,  611,  611,  611,  611,  611,  556,  611,  556,  # 0xf0
)
WIDTHS = {HELVETICA: _HELVETICA, OBLIQUE: _HELVETICA,
          BOLD: _HELVETICA_BOLD, BOLD_OBLIQUE: _HELVETICA_BOLD}

# Not in WinAnsi and not split by NFKD: spelled out before the fallback
# (the same list printing.to_pdf uses for Courier).
_FALLBACK = {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "ı": "i", "Ħ": "H", "ħ": "h",
             "Ŧ": "T", "ŧ": "t", "ĸ": "k", "Ŀ": "L", "ŀ": "l", "Ŋ": "N", "ŋ": "n",
             "‐": "-", "‑": "-", "−": "-", " ": " ", " ": " "}


def _encode_char(ch: str) -> int:
    """One WinAnsi byte for one character (see module notes)."""
    if ch < " " or ch == "\x7f":
        return 0x20                              # tabs, newlines: a space
    try:
        return ch.encode("cp1252")[0]
    except UnicodeEncodeError:
        pass
    ch = _FALLBACK.get(ch, ch)
    base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if not unicodedata.combining(c))[:1]
    try:
        return (base or "?").encode("cp1252")[0]
    except UnicodeEncodeError:
        return 0x3F


def encode(text: str) -> bytes:
    """Text as WinAnsiEncoding bytes, one per (NFC) character."""
    return bytes(_encode_char(c) for c in unicodedata.normalize("NFC", text or ""))


def measure(text: str, font: str = HELVETICA, size: float = 10.0) -> float:
    """Printed width of `text` in points."""
    table = WIDTHS[font]
    return sum(table[b - 32] for b in encode(text)) * size / 1000.0


def fit(text: str, font: str, size: float, width: float, ellipsis: str = "") -> str:
    """`text` cut (from the right) until it fits `width` points; the
    printouts cut without an ellipsis, as MeetPro does, so a name keeps as
    many letters as the column holds."""
    text = unicodedata.normalize("NFC", text or "")
    if measure(text, font, size) <= width:
        return text
    table = WIDTHS[font]
    room = width * 1000.0 / size - sum(table[b - 32] for b in encode(ellipsis))
    used = 0.0
    out = []
    for ch, b in zip(text, encode(text)):
        used += table[b - 32]
        if used > room:
            break
        out.append(ch)
    return "".join(out).rstrip() + ellipsis if out else ""


def pdf_string(text: str) -> str:
    """A PDF literal string, ASCII only: ( ) \\ escaped, high bytes octal."""
    parts = []
    for b in encode(text):
        if b in b"\\()":
            parts.append("\\" + chr(b))
        elif 32 <= b < 127:
            parts.append(chr(b))
        else:
            parts.append(f"\\{b:03o}")
    return "(" + "".join(parts) + ")"


def _info_string(text: str) -> str:
    """Document info strings: plain when printable ASCII, else UTF-16BE hex."""
    if all(32 <= ord(c) < 127 for c in text):
        return pdf_string(text)
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def _n(v: float) -> str:
    """Compact number: 2 decimals at most, no trailing zeros."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class ImageError(ValueError):
    pass


class Image:
    """A PNG or JPEG for Page.image. JPEG goes in as is (DCTDecode); PNG is
    unfiltered here and re-compressed, its alpha channel becoming a soft
    mask. PNG: 8-bit gray/RGB/gray+alpha/RGBA, or palette at 1/2/4/8 bits
    (tRNS honoured); not interlaced."""

    def __init__(self, data: bytes):
        if data[:2] == b"\xff\xd8":
            self._jpeg(data)
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            self._png(data)
        else:
            raise ImageError("not a PNG or JPEG")

    def _jpeg(self, data: bytes) -> None:
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                raise ImageError("bad JPEG marker")
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                comps = data[i + 9]
                self.width, self.height = w, h
                self.colorspace = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(comps)
                if not self.colorspace:
                    raise ImageError(f"JPEG with {comps} components")
                self.extra = " /Decode [1 0 1 0 1 0 1 0]" if comps == 4 else ""
                self.data, self.filter, self.alpha = data, "/DCTDecode", None
                return
            i += 2 + seg
        raise ImageError("JPEG without a frame header")

    def _png(self, data: bytes) -> None:
        pos, idat, plte, trns, ihdr = 8, [], b"", b"", None
        while pos + 8 <= len(data):
            n, kind = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + n]
            if kind == b"IHDR":
                ihdr = struct.unpack(">IIBBBBB", body)
            elif kind == b"PLTE":
                plte = body
            elif kind == b"tRNS":
                trns = body
            elif kind == b"IDAT":
                idat.append(body)
            elif kind == b"IEND":
                break
            pos += 12 + n
        if not ihdr:
            raise ImageError("PNG without IHDR")
        w, h, depth, ctype, _, _, interlace = ihdr
        if interlace:
            raise ImageError("interlaced PNG (save it non-interlaced)")
        chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
        if chans is None or (ctype != 3 and depth != 8) or depth not in (1, 2, 4, 8):
            raise ImageError(f"PNG type {ctype} at {depth} bits not supported (save as 8-bit RGBA)")
        raw = zlib.decompress(b"".join(idat))
        bpp = max(1, chans * depth // 8)
        stride = (w * chans * depth + 7) // 8
        rows, prev, i = [], bytearray(stride), 0
        for _ in range(h):
            ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
            i += 1 + stride
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                if ftype == 1:
                    line[x] = (line[x] + a) & 255
                elif ftype == 2:
                    line[x] = (line[x] + b) & 255
                elif ftype == 3:
                    line[x] = (line[x] + ((a + b) >> 1)) & 255
                elif ftype == 4:
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            rows.append(bytes(line))
            prev = line
        color, alpha = bytearray(), bytearray()
        if ctype == 3:
            pal = [plte[k:k + 3] for k in range(0, len(plte), 3)]
            per = 8 // depth
            for row in rows:
                for x in range(w):
                    idx = (row[x // per] >> (8 - depth * (x % per + 1))) & ((1 << depth) - 1)
                    color += pal[idx] if idx < len(pal) else b"\0\0\0"
                    alpha.append(trns[idx] if idx < len(trns) else 255)
            chans_out, has_alpha = 3, bool(trns)
        else:
            has_alpha = ctype in (4, 6)
            chans_out = chans - (1 if has_alpha else 0)
            for row in rows:
                if has_alpha:
                    for x in range(w):
                        px = row[x * chans:(x + 1) * chans]
                        color += px[:-1]
                        alpha.append(px[-1])
                else:
                    color += row
        self.width, self.height = w, h
        self.colorspace = "/DeviceRGB" if chans_out == 3 else "/DeviceGray"
        self.extra = ""
        self.data, self.filter = zlib.compress(bytes(color), 9), "/FlateDecode"
        self.alpha = zlib.compress(bytes(alpha), 9) if has_alpha and min(alpha) < 255 else None


class Page:
    """One page's drawing operations (top-left origin, see module notes)."""

    def __init__(self, width: float = PAGE_W, height: float = PAGE_H):
        self.width, self.height = width, height
        self.ops: List[str] = []
        self.images: List[Image] = []

    def _y(self, y: float) -> float:
        return self.height - y

    def text(self, x: float, y: float, s: str, font: str = HELVETICA, size: float = 10.0,
             align: str = "left", gray: float = 0.0) -> float:
        """Draw `s` with its baseline at y; x is the left edge, the right
        edge (align="right") or the centre ("center"). Returns its width."""
        if not s:
            return 0.0
        w = measure(s, font, size)
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        self.ops.append(f"BT {_n(gray)} g /{_FONT_TAG[font]} {_n(size)} Tf "
                        f"{_n(x)} {_n(self._y(y))} Td {pdf_string(s)} Tj ET")
        return w

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5,
             gray: float = 0.0) -> None:
        self.ops.append(f"{_n(gray)} G {_n(width)} w {_n(x1)} {_n(self._y(y1))} m "
                        f"{_n(x2)} {_n(self._y(y2))} l S")

    def rect(self, x: float, y: float, w: float, h: float, *, fill: Optional[float] = None,
             stroke: Optional[float] = None, line_width: float = 0.5) -> None:
        """Rectangle with its top-left corner at (x, y). fill / stroke are
        gray levels (0 black .. 1 white); None = don't fill / don't stroke."""
        if fill is None and stroke is None:
            return
        ops = []
        if fill is not None:
            ops.append(f"{_n(fill)} g")
        if stroke is not None:
            ops.append(f"{_n(stroke)} G {_n(line_width)} w")
        paint = "B" if fill is not None and stroke is not None else ("f" if fill is not None
                                                                      else "S")
        ops.append(f"{_n(x)} {_n(self._y(y + h))} {_n(w)} {_n(h)} re {paint}")
        self.ops.append(" ".join(ops))

    def image(self, img: Image, x: float, y: float, w: float, h: float) -> None:
        """Draw img into the box with its top-left corner at (x, y)."""
        if img not in self.images:
            self.images.append(img)
        tag = f"Im{id(img)}"
        self.ops.append(f"q {_n(w)} 0 0 {_n(h)} {_n(x)} {_n(self._y(y + h))} cm /{tag} Do Q")

    def content(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("ascii")


class Document:
    """Pages in order; to_bytes() writes the PDF (content streams Flate-
    compressed: a results page is ~2,000 short operators)."""

    def __init__(self, title: str = "", producer: str = "rfid-meet", compress: bool = True):
        self.title = title
        self.producer = producer
        self.compress = compress
        self.pages: List[Page] = []

    def new_page(self) -> Page:
        page = Page()
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        objs: List[bytes] = []                   # object n is objs[n - 1]

        def add(body) -> int:
            objs.append(body if isinstance(body, bytes) else body.encode("ascii"))
            return len(objs)

        add("<< /Type /Catalog /Pages 2 0 R >>")
        add("")                                   # Pages, filled in below
        fonts = {f: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{f} "
                        f"/Encoding /WinAnsiEncoding >>") for f in FONTS}
        font_res = " ".join(f"/{_FONT_TAG[f]} {n} 0 R" for f, n in fonts.items())
        info = add(f"<< /Title {_info_string(self.title)} /Producer "
                   f"{_info_string(self.producer)} >>")
        pages = self.pages or [Page()]
        ximg: Dict[int, int] = {}                 # id(Image) -> object number, each image once
        for page in pages:
            for img in page.images:
                if id(img) in ximg:
                    continue
                smask = ""
                if img.alpha is not None:
                    m = add(f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
                            f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                            f"/Length {len(img.alpha)} >>".encode("ascii") + b"\nstream\n" + img.alpha
                            + b"\nendstream")
                    smask = f" /SMask {m} 0 R"
                ximg[id(img)] = add(f"<< /Type /XObject /Subtype /Image /Width {img.width} "
                                    f"/Height {img.height} /ColorSpace {img.colorspace} "
                                    f"/BitsPerComponent 8 /Filter {img.filter}{img.extra}{smask} "
                                    f"/Length {len(img.data)} >>".encode("ascii")
                                    + b"\nstream\n" + img.data + b"\nendstream")
        kids = []
        for page in pages:
            data = page.content()
            head = f"<< /Length {{}} >>"
            if self.compress:
                data = zlib.compress(data, 9)
                head = "<< /Length {} /Filter /FlateDecode >>"
            content = add(head.format(len(data)).encode("ascii") + b"\nstream\n" + data
                          + b"\nendstream")
            xres = ""
            if page.images:
                xres = " /XObject << " + " ".join(f"/Im{id(i)} {ximg[id(i)]} 0 R"
                                                  for i in page.images) + " >>"
            kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_n(page.width)} "
                            f"{_n(page.height)}] /Resources << /Font << {font_res} >>{xres} >> "
                            f"/Contents {content} 0 R >>"))
        objs[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                   f"/Count {len(kids)} >>").encode("ascii")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{n} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii")
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode("ascii")
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode("ascii")
        return bytes(out)
