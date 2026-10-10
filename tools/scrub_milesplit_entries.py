"""Scrub a raw MileSplit entries pull into a test fixture (no real people).

Keeps the shape and the values that matter to the importer (genders,
graduation years, division names, seeds, entry counts, statuses) and
replaces everything that identifies anyone: athlete and team names, cities,
DOBs, MileSplit ids (offset into a fake range), the source-meet notes.
Deterministic: the same input and --seed give the same fixture.

    python tools/scrub_milesplit_entries.py RAW.json tests/fixtures/milesplit/entries-xc.json --teams 10
"""

import argparse
import json
import random
import sys

FIRST = ["Avery", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray", "Harper", "Indy",
         "Jules", "Kai", "Lane", "Morgan", "Noel", "Oakley", "Parker", "Quinn", "Reese",
         "Sage", "Tatum", "Umber", "Vale", "Wren", "Xen", "Yael", "Zion"]
LAST = ["Abbot", "Birch", "Calder", "Dunmore", "Elwood", "Fairley", "Garrow", "Holt",
        "Ingram", "Jessup", "Kestrel", "Lomax", "Marlow", "Norcross", "Oakes", "Pryor",
        "Quill", "Rowan", "Stroud", "Thane", "Upton", "Voss", "Whitlock", "Yarrow", "Zeller"]
MASCOTS = ["Ridge", "Valley", "Harbor", "Summit", "Prairie", "Creek", "Forest", "Mesa",
           "Grove", "Bluff", "Lake", "Glen", "Hollow", "Point", "Crossing", "Heights"]


def scrub(raw: list, teams: int, seed: int) -> list:
    rng = random.Random(seed)
    names = [(f, l) for f in FIRST for l in LAST]
    rng.shuffle(names)
    name_iter = iter(names * 4)
    picked = raw[:teams] if teams else raw
    notes = {}
    out = []
    for ti, t in enumerate(picked, 1):
        mascot = MASCOTS[(ti - 1) % len(MASCOTS)]
        team_name = f"{mascot} High School" if ti <= len(MASCOTS) else f"{mascot} High School {ti}"
        abbrev = (mascot[:3] + str(ti))[:4].upper() if len(t.get("teamAbbreviation") or "") == 4 \
            else mascot[:2].upper() + str(ti % 10)
        nt = dict(t, teamName=team_name, teamAbbreviation=abbrev,
                  teamMileSplitId=900000 + ti, teamCity="Testville", teamZipcode="00000")
        if "teamId" in nt:
            nt["teamId"] = 900000 + ti
        roster = []
        for ai, a in enumerate(t.get("roster", []), 1):
            first, last = next(name_iter)
            na = dict(a, athleteFirstName=first, athleteLastName=last,
                      athleteMileSplitId=9000000 + ti * 1000 + ai,
                      athleteMileSplitUniversalId=f"U{ti:03d}{ai:04d}",
                      athleteCity="Testville",
                      athleteDob="2010-01-01" if a.get("athleteDob") else a.get("athleteDob"))
            ents = []
            for e in a.get("entries", []):
                note = e.get("entryNote") or ""
                if note:
                    note = notes.setdefault(note, f"Prior Meet {len(notes) + 1}")
                ents.append(dict(e, entryNote=note))
            na["entries"] = ents
            roster.append(na)
        nt["roster"] = roster
        out.append(nt)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("raw")
    ap.add_argument("out")
    ap.add_argument("--teams", type=int, default=10, help="keep the first N teams (0 = all)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "data" in raw:
        raw = raw["data"]
    out = scrub(raw, args.teams, args.seed)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    n = sum(len(t["roster"]) for t in out)
    print(f"{len(out)} teams, {n} athletes -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
