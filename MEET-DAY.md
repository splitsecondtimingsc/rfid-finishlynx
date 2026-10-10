# Meet-Day Runbook

The one-page reference for race morning. Everything here assumes the
bench-session items in `BENCH-TODO.md` have been completed, and that the
meet was built beforehand on the console's **Meet** tab (entries from
MileSplit or a file, bibs, run order, start times: `QUICK-START.html`
section 5). The open meet writes FinishLynx's start lists itself and has
a race pane for every race. (Updated 2026-10-05 for the Meet tab.)

## Setup (target: minutes, not an hour)

1. **Finish machine on.** Console auto-launches (Startup shortcut /
   systemd) and auto-starts: no clicks. Open `http://localhost:8080/` on
   that PC (the finish console answers on this PC only). The meet that
   was open is open again (Meet tab; if not: **Open** → the meet). Its
   start lists are already in FinishLynx's input folder (`RFID`); the Info
   page's **Start lists** row says when they were last written.
2. **Drive out the split boxes, plug them in, leave.** Any power-on
   order (radio first, reader first - doesn't matter). Each box boots to
   racing on its own and retries every component forever.
3. **Back at the finish, check the Stations chip** (one dot per station;
   click it for the table, **⚙ Reader…** for a station's state):
   - GREEN `reading` - radio + RFID reader both up. Done.
   - AMBER `idle` - radio up, reader down; the box retries every 15 s.
     If it stays amber >2 min, the reader end has a real problem
     (power? cable?) - that's the half to bring a spare for.
   - RED `silent` - nothing heard for 40 s: station power, gateway, or
     radio. That box needs a visit.
   - **Location** column: where each station stands - "This PC" is the
     local mat, then every LoRa box. Pick **Finish** for a box standing
     at the finish line instead of a mile marker: its reads then place
     athletes, promote their held splits, go to FinishLynx's LapTime and
     get the film offset, and the console alarms in red if it goes
     silent mid-race. "Mile 2, then Finish (one mat)" is one mat crossed
     twice; "Finish, skip 1st crossing (loop)" a finish mat a loop passes
     early. **Check-in (chute)** marks the chute reader; **Not in use**
     hides a reader. If the local mat then stands out on the course, set
     its Location to that mile. Saves at once, re-labels reads already
     heard, nothing is sent to the box.
   - **One read per station per race.** Each station counts an athlete
     once in a race: the first read of their first crossing. A later
     crossing (a walk back over the finish mat, a lap past Mile 1 on the
     way home) is ignored, kept out of FinishLynx and listed under the
     pane's Needs attention as *Crossed more often than expected*
     (**Checked** dismisses it; **Assign** in Ignored makes one count).
     Per race, **Settings → Crossings…** (or **More ▾ → Crossings…**) sets
     each station's **Reads before** (crossings to skip first - a course
     that crosses the finish mat mid-race) and **Reads after** (later
     crossings to expect quietly, e.g. 1 when runners walk back over the
     mat to the team area). Changing them renumbers reads already heard.
   - **Fixing one runner** (someone cut the course, crossed the mat
     backwards, a stray read): **⋯** on their row (or **Reads…** in Needs
     attention) lists every read they have in the race, counted or not,
     with why. **Use this read** makes any of them count at its point
     (the reader's other crossings there are then ignored - more
     crossings later can't move it); **Delete** drops one (their next
     crossing counts); **Edit time** takes a race time (26:33.4) or a
     clock time and shows exactly what you typed (film offset allowed
     for). A typed time (**+ Finish**, **+ Mile 1**) also wins over the
     reader. **Remove from race** / **Move** work on all their reads.
4. **Races.** Every race in the meet already has its pane on the Races
   tab, carrying its FinishLynx event/round/heat so FinishLynx, LapTime
   and the .lif names line up. A race added on the day: Meet tab →
   **Races** → **+ New race** (new event or another heat); it gets its
   pane and its start list at once. **Delete pane…** (pane **Settings**)
   removes a pane you do not need - its reads move to Held, nothing is
   lost. With no meet open (MeetPro writing the start lists instead)
   nothing makes panes for you: Races tab **+ New race** with the
   event/round/heat.
   **Two races off one gun, scored separately** (e.g. JV Boys 7-9 and
   JV Boys 10-12): Meet tab → **Races** → the first race's ⋮ → **Run at
   the same time as…**, tick the other race(s). FinishLynx then gets one
   event with every runner; each runner keeps their own race, so results,
   team scores, printouts and MileSplit come out per race. Do it before
   the gun; **Run separately** (same menu) undoes it until the race has
   reads.
   **Divisions** (optional; e.g. Large / Small schools, a Freshmen
   award): Meet tab → **Info** → *Divisions* **Set up…**. Give the number
   of divisions, then for each: a name, **Both / Boys / Girls**, **All
   teams** or **Pick teams**, **All** grades or some, and *Team scores*
   (off = individual places only). Then each race scores the ones you
   tick: **Races** → the race's ⋮ → **Divisions…** (tick *Use these
   divisions for every race* to set them all at once). A division is
   placed and team-scored among its own runners only, separately in each
   event of a race run with another (or girls and boys scored apart); the
   race's own results, Mark final and MileSplit don't change. No divisions
   = the meet works as always. They can be added or changed after a race
   is final: nothing needs re-finalizing.
5. **Walk-ups and changes** (Meet tab → **Athletes**): **+ Add athlete**
   offers the next free bib and any roster match (**Use this athlete**);
   the row ⋮ menu scratches, moves race, marks exhibition. Typing a bib
   someone else holds offers **Swap bibs** or **Someone else is wearing
   N…** (a borrowed bib). The start lists follow every change.
6. **Check-in** (pane or card button **Check-in…**): pick the reader
   that checks this race in (the CHUTE station by default; any reader
   works, and goes back to its Location once check-in closes) and optionally **Close in** N
   minutes, then **Open check-in now**. Several races can check in at
   once, one per reader. **Close check-in** stops it: nothing more joins,
   who checked in stays. Set-and-forget: with start times in the meet,
   the dialog's **Meet default** (e.g. open 30 min before, close 5 before
   or at the gun) opens and closes every race's check-in by itself; a race
   can have its own times or be Off. The card shows "check-in opens
   10:00 AM" until then. Runners reaching the chute up to 2 min after the
   gun still join, but only if check-in was open when the gun went.

## During racing

- **Gun**: fires automatically from FinishLynx running-time packets.
  False trigger (sensor knock)? **Gun ▾** lists every recorded start
  signal - pick the real one; all times re-derive instantly.
- **Advance early**: open the next race whenever - stragglers keep
  auto-assigning to their own race by roster/gender. Never wait.
- **Clocks for a straggler**: the LED clocks follow the latest racing
  pane by default. Press **Show on clocks** in the earlier race's gun row
  to pin every clock (finish + split stations) to that race's time; no
  gun changes. It releases itself when you close that race, or use
  Back to auto / Follow race in any clock chip.
- **Runner in the "wrong" race** (a JV athlete who ran Varsity): their
  read lands in the race that was running and they place there. Once
  FinishLynx's .lif times them in that race, their entry moves over by
  itself (same gender or a mixed race, and no reads in the race they were
  entered in). Anyone left is listed when you **Close race** / **Mark
  final**: **Switch & close** moves the ticked ones in and out of their
  old race. Nothing is dropped meanwhile; Mark final waits until each is
  switched or resolved.
- **Fixes** (athlete row `⋯`): Edit time / Delete / Move to another
  race. Moving re-times against the target race's gun automatically.
  Raw timestamps are never lost.
- **Missing mile split** (athlete row `⋯` -> `+ Mile 2 (estimate)`, or
  the pane's Add athlete with a split point): the dialog suggests a time
  from the athlete's own pace over the course distances and from the
  runners within two places of them at their nearest recorded point,
  blended 60/40. **Use** fills it in and marks the read *estimated*: an
  `est` flag on the row, a `~` on the split and an "Estimated" column in
  the CSV. Type your own time instead if you know better. Finishes are
  never estimated. Split distances: `races.point_distances` in
  config.yaml (Mile 1 = 1609, Mile 2 = 3218); finish = the .evt distance.
- **Coach gives you a split** ("she was 6:12 at the mile"): same dialog,
  type it in the **Race time** box exactly as said (`6:12`, `6:12.3`,
  `1:02:03`). It is measured from the pane's gun and stored as a plain
  manual read (flag `manual`, not `est`). A bare M:SS in the Clock box
  is treated as race time too.
- **Held reads pane**: anything unmatched waits there with a reason;
  Assign ▾ drops it into a race. Held reads auto-promote when their
  bib finishes.
- **Finisher missing a split**: a **Needs attention** strip appears at
  the top of that race's pane the moment they finish, one row per athlete:
  **Estimate** (add dialog with the estimate filled in - press Add, or
  overtype the coach's time), **Add time** (hand time), or **Publish
  without splits** (accept the gap; undo from the same strip). Until a row
  is fixed, that athlete's later splits are held out of the .lif (they
  would land under the wrong mile). Closing a race with open rows
  asks first.
- **Finish Reads switch** (top bar, right): OFF = reads are still
  heard, shown and backed up, but nothing goes to FinishLynx and no .lif
  is written - use it for warm-ups over the mats, tag checks or a test
  start. A red strip reminds you while it is off; it is always ON after
  a restart. Switching back on asks whether the reads kept back should go
  to FinishLynx now (say Cancel for warm-ups).
- **Read backup**: every read the console hears is appended to
  `reads-<date>.txt` in the RFID folder (clock, reader, bib, antenna,
  point, what the panes did with it) - the raw material if anything has
  to be rebuilt.
- **FinishLynx "Sends"** (FinishLynx chip): keep **finish-line reads only**
  for cross country - splits travel in the merged .lif, so a missed mile
  mat can never turn a finish into a lap. "finish reads + splits the
  panes accept" is for track, with the event's Total Laps set to match.
- **Merged .lif files** in `C:\Finish_Lynx_Results\RFID` (FinishLynx's
  rows + RFID splits, rebuilt within ~2 s of any change or FinishLynx
  save) are what the results and MileSplit splits come from (and what
  MeetPro reads, for a meet still run from MeetPro).
- **MileSplit Live**: the **Live Results** switch (top bar) for a meet
  imported from MileSplit; it needs the MileSplit login (Meet → Info).
- **Results** (Meet tab → **Results**, or a final strip's **Results**):
  clear the race's *To resolve before final* list, **Mark final**, print
  from *Print* (its drop-down picks the races - the one shown, any others,
  or **All races**; a race not marked final prints as it stands, titled
  NOT MARKED COMPLETE), **Send MileSplit Results** (every final race, in one upload: each upload
  replaces all of the meet's results on MileSplit).
  **Reopen** to fix a race; MileSplit Live follows by itself, the official
  results go again only with **Send again**. A final race is locked (no
  DNF/DNS/DQ, no edits). Reopened, the Results page itself has the tools:
  **+ Add runner**, and **⋯** on each row - **Edit time** (a race time or
  clock; it wins over the reader and a picked .lif), **Swap bib with…**,
  **Change bib number…**, **Reads…** (every read: use another crossing,
  delete one), **Remove from the results** (Undo under the table). Then
  **Mark final** on the same page. **Sort by** Place, Time, Bib, Last
  name or School; the race list and the Print column stay on screen as
  the results scroll.
  **Select .lif file…** (the race pane, a final strip, or the race's
  Results) when the reads place someone wrong - e.g. a runner the mat
  read at the wrong crossing: pick FinishLynx's .lif and it becomes the race's results. Its times
  replace the mat's, its DNF/DQ rows take the finish away, runners it
  doesn't list still finish from the reads, and a final race is
  re-finalized from it at once (then send it again). The pane shows
  *from <file>*; **More ▾ → Stop using** it goes back to the reads.
  A race scored in divisions shows a *Standings* row (**Race** and each
  division): pick one to see its places and team scores, and the print
  options follow it. *Divisions* in the print box: **All** (each
  division after the race's results - the default), **None** (the race
  only), or tick divisions to print only those. Results, team scores,
  awards, labels and the announcer sheet all follow it.
- **Flagged runners** (2026-10-06): a runner who crosses a race they are
  not entered in, wearing a bib nobody has, or scratched there gets a
  pulsing amber chip on their row (**Not in this race** / **Unknown bib** /
  **Scratched**) and is left out of the results: no place, no points,
  nobody behind them moves back. The Results page shows **N issues** next to
  the race; click it to jump to the row. The chip's menu:
  **Swap bib N with…** (the bib was someone else's: pick the kid who really
  wore it - this race's runners with no finish come first; the bib's owner
  gets the picked kid's bib), **Give bib N to…** / **Add a new athlete
  with bib N…** (a bib nobody has), **Override** (score them here; they
  leave the race they were entered in, or are un-scratched) and **Ignore**
  (out of the results; *Undo* under the table). You can close the race or
  Mark final without deciding: they stay out, flag and all, and Info → To
  fix lists them. Deciding after final re-finalizes the race by itself and
  offers to send MileSplit the corrected results. Races run together never
  flag each other's runners. A race with no entries at all (an open race)
  enters whoever finishes it instead of flagging them.
- **Bibs fixed after the finish** (a coach says two runners swapped bibs,
  or the wrong kid wore one): fix them on Meet → **Athletes** (type the
  bib: **Swap bibs** / **Someone else is wearing N…**). A race still open
  re-scores at once and Live follows. A **final** race keeps its frozen
  results, so the console offers **Re-finalize** right away (also under
  Info → To fix and on the race's Results page): Reopen + Mark final in
  one step, with the bibs as they are now. A finish that now belongs to
  someone not entered in that race (or to nobody) is flagged on its row
  instead (see *Flagged runners*). Results already sent to
  MileSplit: it offers **Send the corrected results**.
- **Every printout opens in a new browser tab** (2026-10-06): print it
  from there, or use the browser's Save / Download button to keep it under
  any name and folder. **Export meet… / Export CSV… / Save .lif…** open the
  timing PC's own Save dialog (last folder remembered).
- **Divisions and late fixes**: who is in a division is read from the
  athletes as they are now, so a grade or team fixed after the race moves
  that runner between divisions at once; the race's own team scores
  follow on **Re-finalize**, as with a bib fix. A runner switched in from
  another race, a walk-up added with the bib that crossed, and a DQ/DNF
  mark all re-score the divisions too.

## After the meet

- Every race final and sent to MileSplit (Meet → **Results**); **Print
  final races** there prints them together.
- **Export meet…** (Meet tab bar) if you want the CSV somewhere handy.
- **Close FinishLynx first**, then **Close meet** (Meet tab bar, red;
  the button in its dialog is **Close meet & clear**). The
  dialog shows the meet folder it will make inside the Lynx results folder,
  `<date> <meet name>\` (MileSplit meet name; ` (2)` if taken), and how
  many files go into it. It then:
  1. saves meet.json, the results CSV and the race state into
     `<meet folder>\Middleware\` and clears every pane and read, the live
     read log and the reader's dedup memory (nothing is cleared if this
     save fails);
  2. copies the files in the results folder and in `RFID` into the meet
     folder (folders there are left alone). Free space is checked first;
     each copy is read back and must match the original (size + SHA-256);
  3. only when every copy has passed, deletes the originals, so the
     results and `RFID` folders are empty for the next meet.
  If anything goes wrong (full disk, a locked file, the PC shutting off)
  the error says what; no original is deleted without a checked copy.
  Fix it and click **Close meet** again: it finishes that close (the
  dialog says so) without closing whatever is loaded now.
  `<meet folder> - MEET FILE.json` at the top of each meet folder is the
  file to open the meet with later; it records every file's checksum and
  marks the folder. With no results folder set (or in preview) the archive
  goes to `closed-meets\<date-time>\` next to the console as before.
  In the `Middleware` folder: `meet\meet.json` (teams, athletes, bibs,
  races, entries, frozen results), `meet\pulls\` (raw MileSplit entries),
  `meet-results.csv`, and `races-state.json` (every read, place and gun).

## Opening a closed meet again

Meet → **Open**: a closed meet shows **CLOSED** in the saved-meets list.
If its meet folder was moved (NAS, USB), use **Browse…** and pick
`<meet folder> - MEET FILE.json` in its new place (renaming the folder is
fine). Either way Open asks how (2026-10-05):

- **Open without FinishLynx** - to print results, send them to MileSplit
  again, or fix names, bibs or teams. Only the meet file opens: nothing is
  copied into the results folder, no start lists are written, no race
  panes come back, and results cannot be changed (Mark final / Reopen are
  refused). The bar shows **NO FINISHLYNX**; **Save & close** writes the
  meet file back into its meet folder and changes nothing else. Needs only
  that no other meet's panes or reads are loaded.
- **Open with FinishLynx** - to fix a result. Its FinishLynx files and
  `RFID` files are copied back into the results folder and its race
  panes, reads and places come back, exactly as during the meet. Open
  FinishLynx, fix the result (or add the read in the pane), reopen the
  race, make it final, send it to MileSplit, then **Close meet**: it saves
  back into the same meet folder (a meet reopened from somewhere else gets
  a new meet folder in the results folder; the moved copy is not changed).
  Only offered when FinishLynx is closed, no other meet is loaded and the
  results and `RFID` folders are empty: the dialog lists every file found
  there, since nothing may be copied over another meet's files.

If the meet folder is gone, Open offers the console's own copy, opened
without FinishLynx: results to print or send, but no reads.

- LED clocks show time of day between meets; **12-hour / 24-hour** is a
  select in any clock chip's popover ("Time of day as") and persists.
  The split stations mirror the finish line: the LoRa GUN broadcast
  carries the gun (or "time of day") and the 12/24 setting, and repeats
  every 30 s, so a Pi's clocks match the finish clocks within half a
  minute of booting. (Pis need the 2026-09-15 package or later to follow
  the 12/24 part; older ones still follow the gun.)

## If things go wrong

| Symptom | Do |
|---|---|
| Station amber (reader down) | It self-heals when the reader answers; swap the reader if not - the box auto-adopts a factory-fresh one, no keyboard. |
| Station red (silent) | Check station power first, then gateway power/LEDs. The app itself is systemd-restarted forever; software is the least likely cause. |
| Wrong gun time | Gun ▾ picker. Everything recomputes. |
| Athlete in wrong race | Close race / Mark final lists them: **Switch & close**. By hand: pane row ⋯ -> Move (LIFs for both races regenerate), or Meet → Athletes row ⋮ -> Move race. |
| FinishLynx has no events / old names | Meet → Info, **Start lists** row: it says why (e.g. no results folder: the **Lynx folder** button → `C:\Finish_Lynx_Results`). |
| Read missing entirely | Check Held pane first, then the ledgers: `lora-reads-sent.log` (on the Pi) vs `lora-reads-received.log` (finish). Diff = exactly what never crossed, with true timestamps for manual entry. A station that restarts sends again, by itself, every read of the last 6 h the finish line never acknowledged (RESTORED in its ledger); the finish line, restarted or not, counts each once. |
| Console page dead | Process auto-restarts in 5 s; state (races, reads, rosters) reloads from `races-state.json`. |
| Need low-level tools | `/classic` - reader config, serial discovery, time sync. |

## Ground truth

Nothing is ever silently lost. Every read is journaled to disk at both
ends the instant it happens, with its capture timestamp. Worst case for
any failure is *late classification*, recoverable after the race from
the ledgers.
