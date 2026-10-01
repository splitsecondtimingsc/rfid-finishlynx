# Meet-Day Runbook

The one-page reference for race morning. Everything here assumes the
bench-session items in `BENCH-TODO.md` have been completed.

## Setup (target: minutes, not an hour)

1. **Finish machine on.** Console auto-launches (Startup shortcut /
   systemd) and auto-starts: no clicks. Open `http://localhost:8080/`.
   Meet folder restores from last time - for a new meet, paste the Lynx
   folder path and hit **Load** (athletes + LIF streaming both repoint
   and persist).
2. **Drive out the split boxes, plug them in, leave.** Any power-on
   order (radio first, reader first - doesn't matter). Each box boots to
   racing on its own and retries every component forever.
3. **Back at the finish, check the Remote stations panel:**
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
     early. If the local mat then stands out on the course, set its
     Location to that mile. Saves at once, re-labels reads already heard,
     nothing is sent to the box.
4. **Pick the races to time.** After Load, the **Races from .evt** picker
   opens by itself when the meet file has races without a pane: tick the
   ones the RFID mat will time and press **Add selected** (a track .evt
   lists every event; only tick what crosses the mat). Panes carry the
   .evt event numbers so FinishLynx, LapTime and the .lif names line up.
   **+ New race** still works for anything not in the file; **Delete
   pane** (pane footer) removes a pane you do not need - its reads move
   to Held, nothing is lost.
   Open check-in on the first race; the chute builds its roster.

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
- **Quick bib -> FinishLynx**: toolbar button; time field blank = now.
- **Finisher missing a split**: a **Needs attention** strip appears at
  the top of that race's pane the moment they finish, one row per athlete:
  **Estimate** (add dialog with the estimate filled in - press Add, or
  overtype the coach's time), **Add time** (hand time), or **Publish
  without splits** (accept the gap; undo from the same strip). Until a row
  is fixed, that athlete's later splits are held out of the .lif (MeetPro
  would file them under the wrong mile). Closing a race with open rows
  asks first.
- **Publishing switch** (Stations panel, top right): OFF = reads are still
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
  for cross country - splits reach MeetPro through the .lif, so a missed
  mile mat can never turn a finish into a lap. "finish reads + splits the
  panes accept" is for track, with the event's Total Laps set to match.
- MeetPro reads the merged .lif files in `C:\Finish_Lynx_Results\RFID`
  (FinishLynx's rows + RFID splits, rebuilt within ~2 s of any change or
  FinishLynx save). For MileSplit Live run **Start MS Live Sync** on
  MeetPro's XC tab. Setup: `FINISHLYNX-SETUP.md` section 2c.
- **Export meet… / Export CSV… / Save .lif…** ask where to save (Chrome or
  Edge on the timing PC); other browsers download to the usual folder.

## After the meet

- **Export meet** (toolbar) if you want the CSV somewhere handy.
- **Close meet** (toolbar, red): archives the meet CSV and the full race
  state to `closed-meets\<date-time>\` next to the console, then clears
  every pane and read, the live read log and the reader's dedup memory,
  and removes the merged `.lif` files from the `RFID` folder (copies go
  into the archive under `rfid-lif\`; MeetPro's lynx.evt/ppl/sch stay).
  Nothing is cleared if the archive cannot be written. The meet folder
  stays loaded - for the next meet, paste its Lynx folder, **Load**, and
  pick races from the .evt. Old `.lif` files in the previous meet folder
  are left alone (MeetPro already consumed them).
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
| Athlete in wrong race | Row ⋯ -> Move. LIFs for both races regenerate. |
| Read missing entirely | Check Held pane first, then the ledgers: `lora-reads-sent.log` (on the Pi) vs `lora-reads-received.log` (finish). Diff = exactly what never crossed, with true timestamps for manual entry. |
| Console page dead | Process auto-restarts in 5 s; state (races, reads, rosters) reloads from `races-state.json`. |
| Need low-level tools | `/classic` - reader config, serial discovery, time sync. |

## Ground truth

Nothing is ever silently lost. Every read is journaled to disk at both
ends the instant it happens, with its capture timestamp. Worst case for
any failure is *late classification*, recoverable after the race from
the ledgers.
