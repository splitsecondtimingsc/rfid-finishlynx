"""Correct finish times in FinishLynx's .lif from the middleware's reads.

For every athlete FinishLynx lists whom the middleware also has a finish
read for, if the two race times differ by more than --threshold seconds
the FinishLynx time is replaced by the middleware's (RFID) time. Athletes
the middleware has no finish for (missed while the console was down, hand
entered from the camera) are left exactly as they are. Rows are re-sorted
by time, places renumbered, and the derived columns (gap to previous,
cumulative, speed, pace, the finish entry of the splits column) recomputed.

The original is copied to <meet dir>\\lif-exports\\<name>.finishlynx-<HHMMSS>.lif
and a report of every change goes to <meet dir>\\lif-exports\\<name>.corrections.txt.

    python tools/fix_lif_times.py --race 3 [--file C:\\Finish_Lynx_Results\\003-1-01.lif]
                                  [--threshold 1.0] [--dry-run]
"""
import argparse
import csv
import io
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime

WEB = "http://127.0.0.1:8080"
RESULTS = r"C:\Finish_Lynx_Results"


def parse_t(v: str) -> float:
    secs = 0.0
    for p in v.strip().split(":"):
        secs = secs * 60 + float(p)
    return secs


def fmt_t(sec: float, decimals: int) -> str:
    sec = round(sec, decimals)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    body = f"{int(m)}:{s:0{3 + decimals}.{decimals}f}"
    return f"{h}:{int(m):02d}:{s:0{3 + decimals}.{decimals}f}" if h else body


def decimals_of(v: str) -> int:
    return len(v.split(".")[1]) if "." in v else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--race", type=int, required=True, help="console pane key")
    ap.add_argument("--file", help="FinishLynx .lif (default: <EEE>-<R>-<HH>.lif in the meet folder)")
    ap.add_argument("--threshold", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    with urllib.request.urlopen(WEB + "/api/races", timeout=10) as r:
        data = json.loads(r.read().decode())
    race = data["races"][str(a.race)]
    res = data["results"][str(a.race)]
    ours = {row["bib"]: row["finish"]["elapsed"] for row in res["finishers"]}
    name = f"{race['event']:03d}-{race['round']}-{race['heat']:02d}.lif"
    path = a.file or os.path.join(RESULTS, name)
    with open(path, newline="") as f:
        raw = f.read()
    lines = raw.splitlines()
    header = lines[0]
    rows = [next(csv.reader([l])) for l in lines[1:] if l.strip()]
    if not rows or len(rows[0]) < 19:
        sys.exit(f"{path}: not a 19-column FinishLynx results file")

    dist_mi = None
    try:
        dist_mi = float(header.split(",")[9]) if header.split(",")[9] else None
    except ValueError:
        pass
    dec = max((decimals_of(r[6]) for r in rows if r[6]), default=2)

    changes, unmatched = [], []
    for r in rows:
        bib = r[1]
        if not r[6]:
            continue
        if bib not in ours:
            unmatched.append(bib)
            continue
        fl, mw = parse_t(r[6]), ours[bib]
        if abs(fl - mw) > a.threshold:
            changes.append((bib, f"{r[3]}, {r[4]}", r[6], fmt_t(mw, dec), mw - fl))
            r[6] = fmt_t(mw, dec)
            # finish entry of the splits column: "cum (lap)" with the previous split's cum
            parts = [p for p in r[10].split(",") if p.strip()]
            prev = parse_t(parts[-2].split(" (")[0]) if len(parts) > 1 else 0.0
            if parts:
                parts[-1] = f"{fmt_t(mw, dec)} ({fmt_t(mw - prev, dec)})"
                r[10] = ",".join(parts)

    # re-sort timed rows, renumber places, recompute the derived columns
    timed = [r for r in rows if r[6]]
    untimed = [r for r in rows if not r[6]]
    timed.sort(key=lambda r: parse_t(r[6]))
    prev_t = None
    for place, r in enumerate(timed, 1):
        t = parse_t(r[6])
        r[0] = str(place)
        gap = t if prev_t is None else t - prev_t
        r[8] = fmt_t(gap, dec) if gap >= 60 else f"{gap:.{dec}f}"
        r[15] = r[8]
        r[16] = r[8]
        if dist_mi and t > 0:
            r[17] = f"{dist_mi / (t / 3600):.2f}"          # mph, as FinishLynx wrote it
            r[18] = fmt_t(t / dist_mi, dec)                 # pace per mile
        prev_t = t
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    for r in timed + untimed:
        w.writerow(r)
    content = header + "\r\n" + out.getvalue()

    stamp = datetime.now().strftime("%H%M%S")
    exp = os.path.join(os.path.dirname(path), "lif-exports")
    os.makedirs(exp, exist_ok=True)
    report = [f"{name}: {len(rows)} rows; middleware finishers {len(ours)}; "
              f"threshold {a.threshold:.1f}s; {len(changes)} time(s) replaced; "
              f"{len(unmatched)} row(s) kept as entered (no middleware finish): {' '.join(unmatched)}"]
    for bib, who, old, new, d in changes:
        report.append(f"  bib {bib:>4} {who:<28} FinishLynx {old:>10} -> middleware {new:>10}  ({d:+.2f}s)")
    print("\n".join(report))
    if a.dry_run:
        print("dry run - nothing written")
        return
    backup = os.path.join(exp, name.replace(".lif", f".finishlynx-{stamp}.lif"))
    shutil.copy2(path, backup)
    with open(os.path.join(exp, name.replace(".lif", ".corrections.txt")), "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n" + "\n".join(report) + "\n\n")
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(content)
    os.replace(tmp, path)
    print(f"backup: {backup}\nwrote:  {path}")


if __name__ == "__main__":
    main()
