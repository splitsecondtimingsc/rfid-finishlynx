"""A bib typed by the operator (break test 2026-10-13).

The camera page (place, new result, Edit result) and the race card's typed
finish time take bibs by hand. A keypad habit ("01417") made a second runner
next to 1417, and anything at all ("<b>1</b>", 5,000 characters, a JSON
object) was stored as a bib. clean_bib() is the one rule for both:

- digits only: leading zeros dropped ("01417" -> "1417"); all zeros is not a bib;
- otherwise letters, digits and hyphens, at most MAX_LEN characters;
- anything else (other characters, longer, not text) -> ValueError.
"""

from __future__ import annotations

import re

MAX_LEN = 10
_OK = re.compile(r"[A-Za-z0-9-]{1,%d}" % MAX_LEN)


def clean_bib(value, allow_empty: bool = False) -> str:
    """The bib as stored, or ValueError with a message for the operator."""
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("a bib is a number (or letters, digits and hyphens)")
    text = str(value).strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError("a bib is required")
    if text.isdigit() and text.isascii():
        text = text.lstrip("0")
        if not text:
            raise ValueError("0 is not a bib")
        if len(text) > MAX_LEN:
            raise ValueError(f"a bib is at most {MAX_LEN} digits")
        return text
    if not _OK.fullmatch(text):
        raise ValueError(f"{text[:20]!r} is not a bib: use digits, letters and hyphens, "
                         f"at most {MAX_LEN} characters")
    return text


def clean_key(value) -> str:
    """A bib-or-placement-id key: a digits-only key is a bib (leading zeros
    dropped), anything else is taken as it is (a placement id)."""
    text = str(value if value is not None else "").strip()
    if text.isdigit() and text.isascii() and text.lstrip("0"):
        return text.lstrip("0")
    return text
