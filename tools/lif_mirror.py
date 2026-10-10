"""Stopgap: publish the race panes' .lif files under FinishLynx's file name.

The console writes <event>-<round>-<heat>.lif; FinishLynx (and MeetPro's
Splits Auto-Pull, which watches the Lynx results directory) use
<EEE>-<R>-<HH>.lif, e.g. 002-1-01.lif. This loop converts every console
file into that name in FinishLynx's 19-field row layout, once a second,
and NEVER overwrites a target it did not write itself (FinishLynx saving
its own results for the event wins; this script then leaves that file
alone and says so in its log).

A sidecar <target>.mirror holds a hash of what this script last wrote, so a
restart still knows which targets are its own.

Run:  python tools/lif_mirror.py  [results-dir]     (Ctrl+C stops)
"""
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta

RESULTS = sys.argv[1] if len(sys.argv) > 1 else r"C:\Finish_Lynx_Results"
SRC_RE = re.compile(r"^(\d+)-(\d+)-(\d+)\.lif$")   # console's name (no zero padding)
written = {}                                       # target path -> content we wrote last


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def convert(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    head = lines[0].split(",")
    gun = None
    if len(head) >= 11 and head[10]:
        try:
            gun = datetime.strptime(head[10], "%H:%M:%S.%f")
        except ValueError:
            gun = None
    out = [lines[0]]
    for line in lines[1:]:
        f = line.split(",")
        if len(f) < 10:
            continue
        place, bib, lane, last, first, affil, ftime, lic, gap, reac = f[:10]
        splits = [s for s in f[10:] if s.strip()]
        tod = ""
        if gun and ftime:
            try:
                secs = _parse(ftime)
                t = gun + timedelta(seconds=secs)
                tod = t.strftime("%H:%M:%S.") + f"{t.microsecond // 1000:03d}"
            except ValueError:
                pass
        row = [place, bib, lane, last, first, affil, ftime, lic, gap, reac,
               '"' + ",".join(splits) + '"' if splits else "",
               tod, "", "", "", gap, ftime, "", ftime]
        out.append(",".join(row))
    return "\r\n".join(out) + "\r\n"


def _parse(v: str) -> float:
    parts = v.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def target_name(m):
    e, r, h = (int(x) for x in m.groups())
    return f"{e:03d}-{r}-{h:02d}.lif"


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def _read(path: str) -> str:
    # newline="" keeps the CRLFs so hashes of what we wrote match exactly
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def ours(dst: str) -> bool:
    """True if dst is absent, or its content is what this script last wrote
    (per the sidecar). Anything else was written by another program -
    FinishLynx saving the event - and is never touched."""
    if not os.path.exists(dst):
        return True
    side = dst + ".mirror"
    if not os.path.exists(side):
        return False
    with open(side) as fh:
        return fh.read().strip() == _hash(_read(dst))


def main():
    log(f"mirroring console .lif files in {RESULTS}")
    foreign = set()
    while True:
        try:
            for name in os.listdir(RESULTS):
                m = SRC_RE.match(name)
                if not m or name == target_name(m):
                    continue            # only the console's unpadded names are sources
                src = os.path.join(RESULTS, name)
                dst = os.path.join(RESULTS, target_name(m))
                content = convert(_read(src))
                if not content:
                    continue
                if os.path.exists(dst) and _read(dst) == content:
                    continue
                if not ours(dst):
                    if dst not in foreign:
                        log(f"{os.path.basename(dst)} was written by another program (FinishLynx?) - leaving it alone")
                        foreign.add(dst)
                    continue
                foreign.discard(dst)
                tmp = dst + ".tmp"
                with open(tmp, "w", newline="") as fh:
                    fh.write(content)
                os.replace(tmp, dst)
                with open(dst + ".mirror", "w") as fh:
                    fh.write(_hash(content))
                log(f"wrote {os.path.basename(dst)} ({content.count(chr(10)) - 1} rows) from {name}")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
