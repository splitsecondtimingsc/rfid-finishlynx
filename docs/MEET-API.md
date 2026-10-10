# Meet management: console integration contract (2026-10-03)

The contract between the backend (`rfid_finishlynx/meet_service.py`,
`rfid_finishlynx/meet_api.py`) and the console (`templates/console.html`).
Both sides build against this file; change it first, then the code.
UX source: the mockups canvas (A1 Meet tab, B1–B5 meet screens, B1b bib
wearer popover, C2 results drawer, D printouts). Plan: MEET-MANAGEMENT-PLAN.md.

## Modes

- **Legacy mode** (no active meet): everything works exactly as today
  (MeetPro .evt/.ppl, folder monitor, `.ppl` athlete lookup). The Meet tab
  shows "No meet open" with Open / New / Import.
- **Meet mode** (a meet is active): rfid-meet owns the start lists. Every
  edit saves `meet/<id>/meet.json`, rewrites `lynx.evt/.ppl/.sch` into the
  RFID folder (`app.lif_dirs()[1]`; never in preview / hardware_allowed
  False), and emits `meet_update`. Athlete lookup (panes, tag reads, bib
  validation) comes from the meet, falling back to `.ppl` for unknown bibs.
- Active meet id persists in `meet/ACTIVE` (one line). `meet/` lives next
  to the race state file (same base dir as `closed-meets/`).

## Conventions

- JSON in/out. Success: `{"ok": true, ...}`. Failure: HTTP 4xx +
  `{"error": "<one line for the operator>", ...details}`. 409 = a conflict
  the UI resolves (bib taken, unresolved finishers, race final).
- Race keys in URLs: `<event>-<round>-<heat>` (e.g. `3-1-1`), never
  RaceManager integer keys. Athlete ids `a<n>`, team ids `t<n>`.
- Every mutating call returns `"meet": <summary>` (below) so the UI can
  refresh its header/badges without a second request, and the server also
  emits `meet_update` with the same summary.
- Data shapes are the meet modules' own `to_dict()` / dict outputs
  (model.py, bibs.py, idmatch.py, edits.py, reports.py, scoring.py);
  this file names them rather than repeating them.

## Summary object (`GET /api/meet`, `meet_update` payload)

```
{"active": true, "id": "...", "file_only": bool, "closed_to": folder|null,
 "info": {name, date, host, location, kind,
   full_rosters, milesplit: {tc_id, meet_id}, scoring: {scorers, displacers},
   divisions: [{id, name, gender, team_ids, grades, team_scores}]},
 "divisions": [{id, name, gender: ""|"M"|"F", team_ids: [] (= all), grades: [] (= all),
   team_scores, text: "Girls · all teams · Grade 9", races: [rk]}],      # 2026-10-06
 "counts": {athletes, entered, roster_only, teams, races, final_races,
   with_bib, missing_bib, scratched, exhibition, unattached, ms_ids, ms_share},
 "fix": [{"kind": "no_bib"|"id_review"|"id_coverage"|"bib_conflict"|"start_lists"|"stale_final",
          "text": "...", "count": n, "detail": [...]}],
 "start_lists": {"dir": "...", "written_at": iso|null, "warnings": [...],
                 "errors": [...], "skipped": "preview"|"file only"|"no folder"|"empty meet"|null},
 "milesplit": {"logged_in": bool, "remembered": email|null, "live": {...LivePusher.status()} | null},
 "preview": bool}
```
`{"active": false}` in legacy mode. `fix` drives the Meet tab's badge count.

## Meet

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | /api/meet | | summary |
| GET | /api/meet/list | | `{meets: [{id, name, date, updated_at}]}` |
| POST | /api/meet/new | `{name, date, host?, location?, kind?}` | summary (activated) |
| POST | /api/meet/open | `{id, mode?: "finishlynx"\|"file", console_copy?}` | summary |
| GET | /api/meet/open/plan | `?id=` or `?path=` | `{closed: false}`, or for a closed meet `{closed, folder, files, loaded, finishlynx_running, unfinished, results_folder, loose: [rel], finishlynx_ok, finishlynx_why: [text], file_only_ok}` (`folder_missing: true` when its folder is gone) — the Open dialog's choice (2026-10-05) |
| POST | /api/meet/open-file | `{path, mode?}` | summary — a meet.json (or its folder) from anywhere; copied into the store first unless it is the store's own copy, new id `<id>-copy[N]` if taken (2026-10-03) |
| POST | /api/meet/detach | | `{active:false}` (back to legacy mode; files kept) |
| POST | /api/meet/info | any of info fields | summary |
| POST | /api/meet/divisions | `{divisions: [{id?, name, gender?: ""\|"M"\|"F", team_ids?: [], grades?: [], team_scores?: true}]}` | summary — the meet's whole division list (2026-10-06, see Divisions below). A row without `id` is new; a division left out is deleted and taken off every race; `[]` = no divisions. 400 for a blank or repeated name, a bad gender, an unknown team or id |
| POST | /api/meet/close | (existing route, extended) | also archives `meet/<id>/` and detaches; with a results folder (2026-10-05) archives into `<results>/<date> <name>/Middleware` and moves the FinishLynx + RFID files (not folders) there (meet_folder.py), back into the same folder for a reopened meet; 409 while FinishLynx is open, 500 with `cleared` when the file move must be finished by calling again |
| GET | /api/meet/close/plan | — | `{folder, resume, again, files, rfid_files, bytes, finishlynx_running}`; `folder: null` = closed-meets fallback; `{file_only: true, folder}` for a meet opened without FinishLynx |

2026-10-05: `/api/meet/open` for a meet with `closed_to` (in `/api/meet/list`), and `/api/meet/open-file` for any path inside a closed meet folder, reopen it (`web.reopen_closed_meet`): files copied back, race state restored; response adds `reopened: {folder, files, races, reads}`. 409 while FinishLynx is open, panes/reads are loaded or the results folder has files (every file named).

2026-10-05 (user's ask): `mode: "file"` opens a closed meet **without FinishLynx** — its meet file only, from its meet folder: nothing copied into the results folder, no race state, no start lists written (`start_lists.skipped = "file only"`), no panes made, Mark final / Reopen refused (409). Needs only no panes/reads loaded. Summary `file_only: true`, persisted in `meet/ACTIVE` (second line `file-only`). `/api/meet/close` is then **Save & close**: the meet file goes back into `<folder>/Middleware/meet` and the meet detaches; race panes, reads, the archived race state and the results folder are untouched. Leaving it any other way (another meet, detach) saves it back too. A closed meet whose folder is gone (`console_copy: true`) opens this way. Default `mode` stays `"finishlynx"`.

## Import / refresh

| POST | /api/meet/import/milesplit | `{entries: <json>, rosters?: <json>}` or `{fetch: true, tc_id, meet_id, full_rosters: bool}` | `{report: ImportReport}` + summary; creates + activates when no meet is active, else refreshes (3-way with stored pulls) |
| POST | /api/meet/import/file | multipart `file` (Hy-Tek .txt / .csv), `include_roster_only` | same |
| POST | /api/meet/refresh | `{fetch: true}` or uploaded JSON | `{report}` + summary |

## MileSplit

| POST | /api/meet/milesplit/login | `{email, password, remember?: bool}` (never logged) | `{logged_in, expires_at, remembered: email|null}` — remember saves the login DPAPI-encrypted for this Windows user (`%LOCALAPPDATA%\rfid_finishlynx\milesplit-login.bin`) and the client logs in again by itself when the token expires or is rejected |
| GET | /api/meet/milesplit/status | | `{logged_in, remembered}` (works with no meet open) |
| POST | /api/meet/milesplit/logout | `{forget?: bool}` | `{logged_in:false, remembered}` — forget also deletes the saved login |
| GET | /api/meet/milesplit/meets | `?month&year` | `{companies, meets}` — each meet as MileSplit sends it (`startDate`, `entryCount`, `venueName`, `hostTeam`, ...) plus `tc_id`, and `setup` (the meet's website Setup page: `{entries_close: "2026-09-29 9:00 PM", entries_open, date_start, date_end, venue, host, timer, timezone, levels, stats: {teamsEntered, teamsScheduled, teamsTotal, athletesTotal, eventsEntered, ...}}`) or `setup_error`. `web: {ok, error?}` — ok=false (no console login this session) means no `setup` on any meet |
| GET | /api/meet/milesplit/meets/<id>/counts | `?tc` | `{meet_id, teams, athletes, entries}` — not-scratched entries, from the meet's entries (2026-10-03) |
| GET | /api/meet/milesplit/meets/<id>/page | `?url=<meetUrl>&page=details\|setup\|teams\|athletes\|divisions\|events[&raw=1]` | MileSplit **website** manager pages (milesplit_web, 2026-10-03). `details`: `{name, date_start, date_end, venue, host, timer, entries_open, entries_close, timezone, levels, stats: {teamsEntered, teamsScheduled, teamsTotal, athletesTotal, eventsEntered, ...}, teams: [{name, milesplit_id, entries, status}]}`; other pages: `{title, fields, links, stats, url}`. Needs the console login this session (website login, memory only); 401 otherwise. Read-only pages only |
| POST | /api/meet/races/<rk>/milesplit/upload | | `{result: UploadResult, problems: [...], races: [rk...]}`: every final race in ONE upload, from any final race (2026-10-09: an upload replaces the meet's whole results page on MileSplit); 409 if <rk> is not final, problems block, or a race already on MileSplit was reopened |
| POST | /api/meet/milesplit/live | `{on: bool}` | `{live: status}` — status also carries `start_times` (events Live was sent a start time for) and `start_time_error` ("" or why Live refused them) |
| POST | /api/meet/milesplit/live/start-times | | `{sent, races: [{rk, start_time_text}], entries: [the PATCH body], message}` — every upcoming race's scheduled start to Live now (pusher on or off); 409 switched off / not linked / nothing to send, 401 logged out, 502 `{error, sent}` when Live refuses |
All network calls refuse in preview mode (`{"error": "preview mode"}`).

### Start times on MileSplit Live (2026-10-03) — UNVERIFIED wire format

`milesplit.live_schedule_payload(meet, races)` builds `PATCH /meets/<id>/events`
entries for races that are still **upcoming** (no Live status: not final, no
finisher yet), one per scoring event, e.g.
`[{"number": 3, "rounds": {"F": {"session": {"time": "9:30 AM"}}}}]`.
They never carry a status, so a race already In Progress / Completed can't
be pushed back to Upcoming. Live is said to show the time only while a race
is Upcoming and only with the meet's `showStartTimes` = `"visible"`. The field
path is `milesplit.LIVE_START_TIME_PATH`; `milesplit.LIVE_SEND_START_TIMES =
False` switches it off everywhere. **Confirm against milesplit.live before
relying on it.** The pusher diffs start times per event like statuses, sends
them in their own PATCH after the statuses, clears a time it sent when the
operator blanks it (never sends a blank otherwise), and a refusal never
fails or backs off the results push (`start_time_error`; the same body is
not retried until the times change).

## Teams / athletes

| GET | /api/meet/teams | | `{teams: [{...Team, girls, boys, entered, roster_only, bibs}]}` |
| POST | /api/meet/teams | `{name, short?, code?}` | `{team_id}` + summary; code made from the name when omitted (unique), 409 when the code or name is taken |
| POST | /api/meet/teams/<tid> | `{name?, short?, code?, score?}` | summary |
| GET | /api/meet/athletes | `?q=&team=&gender=&race=<rk>&roster=0|1&source=&limit=&offset=&sort=&desc=1&nobib=1` | `{athletes: [view], total, nobib_total}`; `nobib=1` (2026-10-08): entered/exhibition athletes without a bib first (`nobib_total` of them), by team, gender, name, or by `sort` when given, then everyone else; `sort` = bib/name/grade/gender/team/race/milesplit/status (a clicked header; blanks last both ways, ties in meet order, 400 otherwise); view = edits._view + `status` (entered/scratched/exhibition/roster), `unattached`, `ms_match`, `scratched_race` (rk of a scratched entry or null), `source`, `added_at`; `source=local` = walk-ups, newest first |
| GET | /api/meet/athletes/search | `?first=&last=&team=` | `{matches: [view]}` (edits.search_team, all teams when team omitted) — "Potential matches" |
| POST | /api/meet/athletes | `{first, last, gender, team_id?, grade?, race?: rk|null, event?, bib?, force?}` | `{athlete: view, id_match: QueueItem|null}` + summary (runs idmatch for the new athlete); 409 `{duplicate: view}` when that name is already on the team (roster-only or entered, 2026-10-08): use them, or resend with `force: true` |
| POST | /api/meet/athletes/<aid> | `{first?, last?, gender?, grade?, team_id?, unattached?}` | `{athlete}` + summary |
| POST | /api/meet/athletes/<aid>/entry | `{race: rk|null, event?, status?: entered|scratched|exhibition}` | `{athlete}` + summary (move race / scratch / exhibition / un-enter) |
| DELETE | /api/meet/athletes/<aid> | | summary (refused 409 if in a final race) |
| POST | /api/meet/undo | | summary (undo the last athlete/bib edit; one level, in memory) |

## Bibs

| POST | /api/meet/athletes/<aid>/bib | `{bib}` | `{result: EditResult}` + summary; 409 `{error, holder: view, swap: {a, b}}` when taken |
| POST | /api/meet/bibs/swap | `{a, b}` | `{result}` + summary |
| GET | /api/meet/bibs/next | | `{bib: "105"|null}` — next free bib in the meet's ranges (held bibs skipped; null = no ranges / used up) |
| GET | /api/meet/bibs/<bib>/context | | edits.bib_context + `next_free` (what the holder gets with `next_free`; that bib skipped) |
| POST | /api/meet/bibs/<bib>/wearer | `{athlete_id}` or `{new: {first, last, gender?, team_id?, grade?, race?, event?, force?}}`, `holder_action` | `{report: WearerReport, id_match}` + summary — fields omitted = prefill, `null`/`""` = blank; 409 `{duplicate: view}` as for a new athlete |
| POST | /api/meet/bibs/assign | `{rows: [[start, end|""], ...], sort: [...], retain: bool, per_team_blocks?, entered_only?, athlete_ids?, dry_run: bool}` | `{report: AssignReport}` (+ summary unless dry_run) |
| POST | /api/meet/bibs/clear | `{athlete_ids?: [...], all?: true}` | `{report}` + summary |
| GET | /api/meet/bibs/conflicts | | bibs.conflicts().to_dict() |

## MileSplit id review

| GET | /api/meet/idreview | | `{queue: [QueueItem], coverage: {...}, by_race: {rk: coverage}}` |
| POST | /api/meet/idreview/<aid>/approve | `{candidate}` | summary |
| POST | /api/meet/idreview/<aid>/decline | | summary |

## Races

| GET | /api/meet/races | | `{races: [{rk, event, name, order, status, divisions: [{event, name}] (its scoring events), division_ids: [division id] (the meet divisions it is scored in, 2026-10-06), split_genders, entries, scratched, on_course, finished, pane_key, start_time ("HH:MM"|""), start_time_text ("8:00 AM"|""), uploaded_at (last mpResults upload or null), upload_ok}]}` (counts from RaceManager when linked) |
| POST | /api/meet/races | `{event?: existing num, new?: {name, gender (M/F/X), distance_m?, division?}, round?, heat?, name?, start_time?}` | `{race: rk}` + summary — last in the run order; heat defaults to the next free one; its pane is created |
| DELETE | /api/meet/races/<rk> | | summary — cancel: entries become not entered, pane removed; 409 once it has reads, a gun, finishers or results |
| POST | /api/meet/races/order | `{order: [rk, ...]}` | summary |
| POST | /api/meet/races/start-times | `{first: "08:00", every_min: 30, overwrite?: false}` | `{filled}` + summary — the k-th race in run order gets first + k·every; only races without a time unless overwrite; 400 past midnight |
| POST | /api/meet/races/<rk> | `{name?, start_time?, divisions?: [division id]}` | summary — start_time typed leniently ("8:00 AM", "8am", "08:00", "20:15"; no AM/PM = 24 h), stored HH:MM, `""` clears, 400 otherwise. `divisions`: the divisions this race is scored in (2026-10-06; 400 for an unknown id); neither rewrites the start lists |
| POST | /api/meet/races/divisions | `{divisions: [division id], races?: [rk]}` | summary — the same divisions for those races (omitted = every race), 2026-10-06 |
| POST | /api/meet/races/<rk>/split-genders | `{on: bool}` | summary. 400 on a race running other races with it |
| POST | /api/meet/races/<rk>/run-together | `{races: [rk]}` | `{report: {race, events, moved, removed, warnings}, meet: summary}` (2026-10-05). Runs those races at the same time as this one, scored separately: their entries join it keeping their own scoring event, the races go, the start lists carry one Lynx event; race summary `run_together: true`. 409 if any has reads, a gun or is final; 400 for a gender-split race |
| POST | /api/meet/races/<rk>/run-separately | | `{report, meet: summary}`. Gives each folded-in event its race back; 409 once it has reads or a gun |
| POST | /api/meet/races/<rk>/switch-in | `{athletes: [athlete id]}` | `{switched: [{athlete_id, bib, name, from}], skipped: [{athlete_id, reason}], meet: summary}`. Moves each into this race and out of every other (exhibition stays, a scratch becomes entered); one undo; anyone in a final race elsewhere is skipped. Also done by itself for a runner FinishLynx's .lif times in this race (same gender or mixed race, no reads in their own race) |
| POST | /api/meet/races/<rk>/clear-entries | | summary. Empties the race (athletes stay, not entered; bibs kept); 409 once it has reads, a gun or results; `/api/meet/undo` takes it back |

## Results (drawer)

| GET | /api/meet/races/<rk>/results | | `{status, official: bool, results: frozen-or-live, unresolved: [warning], team_scores, split_labels, milesplit: {uploaded_at, ok}, issues: [{code, label, text, bib, athlete_id, name, team, team_id, gender, event, elapsed, crossed, status, entered_in: [race name], blocked}], ignored: [{bib, athlete_id, name}], not_entered: [{bib, athlete_id, name, team, gender, from: [race name], blocked}], divisions: [{id, name, team_scores, text, results}]}`. issues = flagged runners left out of `results` (2026-10-06); not_entered = the not_entered ones in the old shape. divisions (2026-10-06): the standings of each division the race is scored in, worked out from `results` (same shape; `event_place` = place in the division, `points` from the division's scoring, `overall_place` = place in the event; `teams` `{}` without team scores); `[]` without divisions |
| POST | /api/meet/races/<rk>/final | | summary; 409 `{error, unresolved: [...]}` (scoring.Unresolved); also sets the pane final |
| POST | /api/meet/races/<rk>/reopen | | summary |
| POST | /api/meet/races/<rk>/refinalize | | `{meet, warnings, milesplit_sent}` — Reopen + Mark final in one step after a bib change (2026-10-05): the results are worked out again from the reads with the current bibs; all or nothing (409 `{error, unresolved}`, the race keeps its old frozen results); `milesplit_sent` = its results were uploaded before (the console offers Send again). Summary `fix` kind `stale_final` (detail: rks) lists final races whose frozen results show a bib the athlete no longer holds |
| POST | /api/meet/races/<rk>/import-lif | multipart `file` (a .lif) | `{lif: {name, header, timed, flagged, finishers, moved, gone, unknown}, refinalized, refinalize_error?, unresolved?, warnings, milesplit_sent, meet}` — "Select .lif file" (2026-10-09): the picked .lif is the truth for the race's pane (its times replace the mat's finishes, its DNF/DQ rows take the finish away, other runners still finish from the reads); a final race is re-finalized from it. The pane's own routes: `POST /api/races/<key>/import_lif`, `POST /api/races/<key>/clear_lif` |
| POST | /api/meet/races/<rk>/marks | `{bib, mark: "DNF"|"DQ"|"DNS"|null}` | summary |
| POST | /api/meet/races/<rk>/issues/<bib> | `{action: "ignore"\|"unignore"\|"override"\|"swap"\|"give", athlete_id?}` | `{meet: summary, report: {action, refinalized: [rk], errors: [text], milesplit_sent: [rk]}}` — a flagged runner (2026-10-06, see Flagged runners). ignore: out of this race's results (`Race.ignored`, while the bib is still that athlete's); override: its holder is entered here and scores (out of their other race; a scratch here becomes entered), 409 across genders; swap + athlete_id: the runner who wore it swaps bibs with the holder; give + athlete_id: a bib nobody has goes to that athlete. Every final race touched is re-finalized by itself |

## Printing

`GET /api/meet/print/<report>.<pdf|txt>` → file download (`Content-Disposition: attachment`).
Reports: `results` (`race=rk`), `meet-results`, `team-scores` (`breakdown=1`),
`awards`, `award-labels`, `finish-line` (`race=rk`), `rosters`, `team-labels`,
`competitor-labels`, `split-scores` (`race=rk`), `start-list` (`race=rk`
optional: without it every race in run order, one per page, one file),
`schedule` (run order with scheduled start times, divisions, entries),
`team-bibs`, `announcer` (`race=rk` optional = all final races, `top=<n>`,
`team_count=<n|all>`; reverse order for reading aloud, one race per page).
A start list prints "Start: 8:00 AM" under its title when the race has a time.
`meet-results`, `team-scores`, `awards`, `award-labels` and `announcer` also take
`races=rk,rk` (not with `race`): only those races, in run order; races that aren't final
are left out.
Query: `race`, `races`, `columns=1|2`, `places=1-25`, `grades=9,10`, `teams=t1,t2`,
`hide=DNF,DNS,DQ`, `events=3,4`, `breakdown=1`, `skip=<labels already used>`,
`divisions=all|none|d1,d2` (2026-10-06: `results`, `meet-results`, `team-scores`, `awards`,
`award-labels`, `announcer`; default `all` = each division section after its scoring
event's own, `none` = as before divisions, ids = only those divisions' sections; an
unknown id is a 400; the filters act on division places).
Unknown combination → 400. Labels and finish-line are PDF only.

## Flagged runners (2026-10-06)

A finisher (or a runner seen on course) who is **not entered** in the race, wears a bib
**nobody in the meet has**, or is **scratched** there is flagged: left out of the results
(no place, no points, nobody behind them moves back; divisions, printouts, MileSplit and
Live leave them out too) and listed in `results.issues`, frozen with the results. Flags no
longer block Mark final or Re-finalize (`scoring.BLOCKING` is lif_hold / no_time /
on_course); a DNF/DQ/DNS mark clears a flag. The console shows a chip on the row (race pane:
`/api/races` payload `issues: {pane: {issues: {bib: {...}}, ignored: [bib]}}`; Results page:
`issues` above; race summary `issues`: how many) whose menu calls
`POST /api/meet/races/<rk>/issues/<bib>`. Summary `fix` kind `issues` (detail: rks) lists
final races that leave flagged runners out. Races run together never flag each other's
runners (one start list).

Open races: a race with no entries of its own (none, or only `Entry.auto` ones) enters every
known athlete who finishes it (`auto: true`), moving them out of their own race unless that
race is final, the gender rules them out or they were ignored; an unknown bib is still
flagged. Every other race only flags: nothing is switched by itself any more, also when
FinishLynx times a runner (that used to switch them and hid swapped bibs). The close
dialog that offered switching is gone; `/switch-in` stays for scripts.

## Divisions (2026-10-06)

Extra standings inside a race (`meet/divisions.py`). A division is gender (both / M / F)
+ teams (all, or a list; "all" takes unattached runners too) + grades (all, or a list),
all three must match. Each race lists the divisions it is scored in (`division_ids`).
A division is re-scored among its own runners in each scoring event of the race
separately (a run-together or gender-split race never pools events): places 1..n, and
with `team_scores` the meet's scorers/displacers and every scoring rule (exhibition,
unattached, non-scoring and incomplete teams). Nothing is stored: the standings are worked
out from the race's results (frozen or live) when shown or printed, so Mark final,
MileSplit upload and Live are unchanged, and a division added or edited after a race is
final shows at once. Who is in a division is read from the athletes as they are now (a
team or grade fixed after the race moves them; the race's own frozen results wait for
Re-finalize as before). Run together merges the races' divisions; Run separately gives
each race the gun's divisions. A meet without divisions behaves exactly as before.

## Meet sync (2026-10-07, meet_sync.py, MEET-SYNC-PLAN.md)

Meets shared between a team's PCs: a shared folder (Google Drive for desktop) or a private git
repo (the owner's NAS). Each PC writes only `<meet-id>/<PC>.json` (git) or its `<meet-id>/<PC>/` folder split by section
(folder, since 2026-10-10: meet_split.py; the older single `<PC>.json.gz` is still read); others are merged in per
record. Reads, race state and FinishLynx files never travel. Since 2026-10-08 `mode: both` runs the
NAS and a shared folder at once (each its own sync and state, `meet-sync/` and `meet-sync/drive/`;
one offline holds nothing up), and the NAS place (`remote`) may be a folder (a network share)
instead of a git remote.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /api/meet/sync | — | `{enabled, mode, folder, remote, machine, preview, transport, last_ok, last_error, last_sent, last_received, waiting_to_send, conflicts: {meet: n}, targets: [{target: git\|folder, label, last_ok, last_sent, last_received, last_error, ...}], clashes: [{target, meet, index, path, machine, mine, theirs, at}], separate: {meet: n}, separate_copies: [{target, meet, machine, name, why: origin\|bibs\|ids, bib_diffs, other_runner, examples: [{name, mine, theirs}], kept, at}], bib_dups: {meet: [bib]}, bib_dup_list: [{target, meet, bib, names, machine}], last: {target: report}}`; `last_ok` = the oldest of the places' last check (a check runs every 5 min whether or not anything moved); `last_sent` / `last_received` = the newest time a meet actually went out / came in |
| POST | /api/meet/sync | `{}` | Sync now, every place: `{report: {published, merged, added, waiting, errors: {target: msg}} (+ error when all are offline), sync}`; 409 when off |
| POST | /api/meet/sync/settings | `{mode: off\|folder\|git\|both, folder, remote, machine}` | saved to config.yaml, sync restarted; 400 when a folder doesn't exist or the NAS has no place |
| POST | /api/meet/sync/conflicts/<meet>/<i> | `{take: theirs\|mine, target}` | settle one clash (target: the place it was recorded on; default the first); 409 while a race runs |
| POST | /api/meet/sync/separate/<meet> | `{machine, take: theirs\|mine, target}` | another PC's separate import of the meet (2026-10-10): `theirs` replaces this PC's copy with it (ours saved in `<state>/replaced/`), `mine` keeps ours until that copy changes; 409 while a race runs, 404 when nothing is recorded |

Socket `meet_sync` after every round (re-read GET /api/meet/sync).

Separate imports (2026-10-10): a copy of a meet that another PC imported on its own (a different `meet.origin`,
or, for a meet without one, a first look at that PC's copy where a runner has another bib or an athlete id is
another runner) is never merged; it waits in `separate_copies` until a PC picks one. Creating a meet that is
already here (same MileSplit meet, or same date + name, after a sync round) is refused with 409
`{error, existing, existing_name}`: Refresh is how a meet takes MileSplit's changes. Bib edits are sent at once.

## Socket

`meet_update` → summary object. Clients also re-fetch `GET /api/meet` on
connect. Existing `races_update` is unchanged.

## As built (backend, 2026-10-03)

- `start_lists.skipped`: `"preview"` | `"no folder"` | `"empty meet"` (an
  empty meet never blanks a start list FinishLynx may be reading).
- Summary also carries `bib_ranges`, `updated_at`, `can_undo`.
- Races list items add `meet_status`, `round`, `heat`; results add `rk`,
  `pane_key`, `milesplit.message`; final also returns `warnings`; login
  returns `expires_at`.
- Meet mode creates the race panes itself from the meet's races (no
  "Races from .evt" step); entries keep syncing via the .evt monitor.
  Mark final is 409 only if a race somehow has no pane.
- MileSplit upload: every `mp_results_problems` item blocks except
  "still in the id review queue" (sent as write-ins) and "not in the meet,
  left out". Auth failures 401 `{logged_in: false}`; network/API errors 502.
- Print: query params a report doesn't use → 400; `columns=2` only on PDF;
  `finish-line` race optional; `announcer` uses `top` and `team_count`.
- Athletes: `roster=1` = only athletes in no race, `roster=0` = entered
  only, omitted = both; `team=none` = no team.
- Bib assign with `rows` stores them as the meet's ranges when applied.
- Close archives the meet into `<archive>/meet/` (`meet_archived` in the
  reply). MileSplit upload records: `meet/<id>/milesplit-uploads.json`.

## As built (start times + UI gaps, 2026-10-03)

- `Race.start_time` ("HH:MM", meet.json); `model.parse_start_time` /
  `time_text` / `start_time_text`. A start-time-only edit (or Fill times)
  saves the meet and nudges the Live pusher but does not rewrite
  lynx.evt/.ppl/.sch (FinishLynx has no use for it).
- `Athlete.added_at` (walk-ups added on the console, incl. a new wearer
  from the bib popover) orders `?source=local`; imported athletes have "".
- Cancel race also drops an event that nothing else uses any more (a "+ New
  race" made by mistake leaves nothing behind).
- `start-list.<fmt>` without `race` downloads as `<meet>-start-list.<fmt>`.
