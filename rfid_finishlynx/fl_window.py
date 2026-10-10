"""Which event FinishLynx has open, read from its window title.

LapTime packets carry no event - FinishLynx files each read under the event
it has open - and on this rig the scoreboard feed never names the event
(the Middleware entry sends running time only; event 0 all meet). The main
window's title does: "FinishLynx - [Women's 5000 Meters (2-1-1)]" =
event 2, round 1, heat 1. With Single Event Mode on (Options -> Event) that
is the event being timed. Windows only; the console runs on the same
desktop as FinishLynx. (2026-09-30: a Men's 5000 straggler landed in the
girls' results because nothing told the console the girls' event was open.)
"""

import re
import sys
import time
from typing import Optional, Tuple

TITLE_RE = re.compile(r"^FinishLynx - \[(?P<name>.*?)\s*\((?P<e>\d+)-(?P<r>\d+)-(?P<h>\d+)\)\]")
CACHE_SEC = 1.0
_cache = {"at": 0.0, "value": None}


def parse_title(title: str) -> Optional[Tuple[int, int, int, str]]:
    """'FinishLynx - [Name (2-1-1)]' -> (2, 1, 1, 'Name'), else None."""
    m = TITLE_RE.match(title or "")
    if not m:
        return None
    return int(m.group("e")), int(m.group("r")), int(m.group("h")), m.group("name")


def _window_titles(match=lambda t: t.startswith("FinishLynx - [")):
    """Titles of visible top-level windows that `match` (Windows), else []."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    titles = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if match(buf.value):
                    titles.append(buf.value)
        return True

    user32.EnumWindows(proc(each), 0)
    return titles


def open_event() -> Optional[Tuple[int, int, int, str]]:
    """(event, round, heat, name) FinishLynx has open, or None (not running,
    no event open, or not Windows). Cached for a second."""
    now = time.time()
    if now - _cache["at"] < CACHE_SEC:
        return _cache["value"]
    value = None
    try:
        for t in _window_titles():
            value = parse_title(t)
            if value:
                break
    except Exception:
        value = None
    _cache.update(at=now, value=value)
    return value


def running() -> bool:
    """FinishLynx has a window open (Windows; always False elsewhere).
    Close meet waits for it: an open FinishLynx can hold its files or write
    a .lif while they are being moved (2026-10-05)."""
    try:
        return bool(_window_titles(lambda t: t == "FinishLynx" or t.startswith("FinishLynx - ")))
    except Exception:
        return False
