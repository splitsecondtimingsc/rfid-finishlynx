"""Write a race pane's RFID results as the official FinishLynx .lif.

Takes the console's current results (GET /api/races) for one pane and
writes <EEE>-<R>-<HH>.lif in FinishLynx's layout into the results folder,
overwriting whatever is there (FinishLynx's own file included - that is
the point when the LapTime link failed and the RFID reads are the record).

    python tools/write_lif.py --race 1                # with splits (default)
    python tools/write_lif.py --race 1 --finish-only  # finish times only
    python tools/write_lif.py --list                  # show the panes

The layout is the console's merged one (rfid_finishlynx/lif_merge.py). This
is a one-off copy: re-run it when more athletes finish. Day to day MeetPro
reads the console's own merged files in the RFID subfolder instead.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rfid_finishlynx.lif_merge import merge_lif      # noqa: E402

WEB = "http://127.0.0.1:8080"


def split_columns(res: dict, points=None) -> list:
    """Split points in course order: the console's configured points when
    given (GET /api/races "points"), else ordered by when runners reached
    them."""
    if points:
        return [p["id"] for p in points if not p.get("finish")]
    first = {}
    for row in res["finishers"] + res.get("on_course", []):
        for s in row["splits"]:
            if s.get("elapsed") is not None:
                first[s["point"]] = min(first.get(s["point"], s["elapsed"]), s["elapsed"])
    return sorted(first, key=first.get)


def build(race: dict, res: dict, splits: bool, points=None) -> str:
    """The same layout the console streams for MeetPro (lif_merge.py):
    splits stop at an athlete's first missing point, never include the
    finish, and athletes on course are listed with blank place/time."""
    gun = datetime.fromisoformat(race["gun"]) if race.get("gun") else None
    return merge_lif(race["event"], race["round"], race["heat"], race["name"],
                     str(race.get("distance") or ""), gun, res, split_columns(res, points),
                     splits=splits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--race", type=int, help="pane key (see --list)")
    ap.add_argument("--finish-only", action="store_true", help="omit the split points")
    ap.add_argument("--dir", default=r"C:\Finish_Lynx_Results")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    with urllib.request.urlopen(WEB + "/api/races", timeout=10) as r:
        data = json.loads(r.read().decode())
    if a.list or a.race is None:
        for k in data["order"]:
            race, res = data["races"][str(k)], data["results"][str(k)]
            print(f"{k}: {race['name']} (event {race['event']}-{race['round']}-{race['heat']}) "
                  f"{race['status']} finishers={len(res['finishers'])} on course={len(res['on_course'])}")
        if a.race is None:
            sys.exit("pick a pane with --race N")
    race, res = data["races"][str(a.race)], data["results"][str(a.race)]
    if not res["finishers"]:
        sys.exit(f"{race['name']} has no finishers yet")
    content = build(race, res, splits=not a.finish_only, points=data.get("points"))
    name = f"{race['event']:03d}-{race['round']}-{race['heat']:02d}.lif"
    path = os.path.join(a.dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(content)
    os.replace(tmp, path)
    note = "finish times only" if a.finish_only else "with splits"
    print(f"wrote {path}: {len(res['finishers'])} finishers, {note}; a one-off copy - "
          f"re-run when more athletes finish (the console keeps its own merged copy in the RFID folder)")


if __name__ == "__main__":
    main()
