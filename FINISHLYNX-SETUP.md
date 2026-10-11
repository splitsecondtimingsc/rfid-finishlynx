# FinishLynx ↔ Middleware Connection Setup

What to set inside FinishLynx so the middleware connects. Two channels,
one in each direction:

| Channel | Direction | Protocol | Port | Who connects to whom |
|---|---|---|---|---|
| **LapTime** (RFID splits into FinishLynx) | middleware → FinishLynx | TCP | **5000** | middleware dials FinishLynx |
| **Scoreboard** (event/heat + running time out) | FinishLynx → middleware | UDP | **5891** | FinishLynx sends datagrams |

Both ports are changeable, but they must match `config.yaml`
(`finishlynx.port` / `scoreboard.port`). The defaults above are what the
shipped config uses. When FinishLynx and the middleware run on the
**same PC** (the normal setup), use `127.0.0.1` everywhere an IP is
asked for and the firewall never gets involved.

---

## 1. LapTime plug-in (splits INTO FinishLynx) — TCP 5000

Requires the FinishLynx **LapTime plug-in** to be installed.

1. **File → Options → LapTime**
2. **Enable** LapTime.
3. **Device: `Lynx`** — this is the generic "Lynx LapTime Protocol" the
   middleware speaks (`S`/`T`/`Z` packets). Not Ipico, not BibLynx; those
   entries expect their own vendor formats and silently ignore ours.
   (Saved in `Lynx.cfg` as `\LapTime\N\Type=11`; `preflight.ps1` checks it.)
4. Connection type **Network**, **Port: 5000**. FinishLynx listens; the
   middleware connects out to it (`finishlynx.host: 127.0.0.1` in
   config when on the same machine).
5. Time source: the middleware streams `T` (time-sync) packets 20×/s
   with millisecond precision, plus `S` (split) packets carrying
   `bib,event,round,heat` — leave the plug-in on its default/internal
   sync setting so it accepts the `T` packets.

**Verify:** start the middleware console (`http://localhost:8080/`) —
the FinishLynx status chip goes green within ~5 s of FinishLynx being
open. Use the console's **Quick bib** button; the split should appear in
the armed FinishLynx event.

Note: splits are timestamped with the middleware PC's local clock and
the `T` sync packets use the same clock, so the pair is self-consistent.

## 2. Scoreboard output (event/heat + gun OUT of FinishLynx) — UDP 5891

This is how the middleware learns which event/round/heat is armed, and
how race guns fire automatically (it parses "Running Time" packets and
derives gun = now − race clock).

1. **File → Options → Scoreboard**
2. **New** scoreboard entry:
   - **Script:** `Example` (or `Example2`) — the middleware parses that
     start-list format.
   - **Output:** Network (UDP)
   - **IP:** `127.0.0.1` (middleware on the same PC — otherwise the
     middleware machine's IP)
   - **Port:** `5891`
   - Time option: **running time enabled** (this is what auto-fires the
     gun in the race panes; without it you set guns manually).
3. If your setup splits start-list and running time across two
   scoreboard entries, both go to the same UDP `127.0.0.1:5891`.

**Verify:** arm an event in FinishLynx — the console header shows the
event/heat. Start the clock — a gun candidate appears in the race pane's
**Gun ▾** picker.

## 2b. Start gun straight from the camera (no FinishLynx dependency)

The start sensor (wired, or a RadioLynx transmitter) reports to the
EtherLynx camera, and the camera reports each firing inside the status
reply it sends FinishLynx roughly six times a second (Kirk command channel, camera UDP port 41925). The middleware sniffs
those replies with **tshark** (Wireshark + Npcap; no admin needed) and gets
the start time from the camera's own 10 kHz clock - a few ms accuracy,
regardless of whether FinishLynx accepted the start.

- Requirements: Wireshark installed on this PC, camera NIC on the camera's
  subnet (auto-detected; override `camera_start.interface` with the
  `\Device\NPF_{...}` GUID or the adapter name if needed).
- Camera IP: leave it on `auto` (blank in the console). FinishLynx gives
  the camera this PC's camera-NIC address + 1 (.67 next to a PC at .66),
  so the sniffer listens for any camera and follows the first one that
  answers; the popover's **Camera** line and the Setup check show the
  address it found. Set an address only to pick one of two cameras.
  `192.168.0.201`, the old default every earlier `config.yaml` carries,
  counts as `auto`.
- Config block (`config.yaml`): `camera_start: {enabled, camera_host,
  camera_port, interface, tshark_path}`. Defaults: `auto`, port 41925.
- Which inputs count: the wired start sensor and RadioLynx start impulses
  are guns; photo-eyes and RadioLynx transmitter status messages are not.
- Console: the **Camera start** chip is green while it hears the camera's
  replies, amber if tshark runs but no replies arrive (FinishLynx not
  polling the camera - is it booted?), red if tshark is missing. The chip
  shows the last start time; its popover has the settings.
- Each start lands in the race pane waiting for one (or the racing pane's
  Gun picker), and drives the LED clocks even with no pane.
- Protocol notes: `rfid_finishlynx/camera_start.py` docstring; capture
  `etherlynx/captures/06-start-sensor.pcapng` (three firings 08:42:35.639,
  08:42:43.930, 08:42:52.790 on 2026-09-12).

## 2c. Folders: live splits to MeetPro / MileSplit (one-time setup)

FinishLynx and the console must not write the same `.lif`, so the console
keeps its own copy in a fixed `RFID` subfolder of the Lynx results folder:

```
C:\Finish_Lynx_Results\        FinishLynx OUTPUT: its .lif, .evn, images
C:\Finish_Lynx_Results\RFID\   MeetPro writes lynx.evt/ppl/sch here,
                               FinishLynx reads them here (INPUT),
                               the console writes the merged .lif here,
                               MeetPro reads the .lif here
```

Set once, on each machine:

1. **MeetPro:** Interfaces → F.A.T. → Setup → **Directory** =
   `C:\Finish_Lynx_Results\RFID` (create the folder first). Leave
   "Don't Write Start Lists" unticked.
2. **FinishLynx:** in the Options dialog's database / directory settings
   (exact labels unverified), set the **input** directory to
   `C:\Finish_Lynx_Results\RFID` and leave the **output** directory at
   `C:\Finish_Lynx_Results`. These are `\Database\File\DataInDir` and
   `DataOutDir` in `Lynx.cfg` (both `C:\Finish_Lynx_Results` on
   2026-09-22); check with `python bench/split_lab.py lynxcfg`.
3. **Console:** Meet folder stays `C:\Finish_Lynx_Results`. It reads the
   start lists from `RFID\` automatically, reads FinishLynx's `.lif` files
   from the parent, and writes the merged ones into `RFID\`
   (`races.rfid_subdir`, default `RFID`).

The merged file per race (`rfid_finishlynx/lif_merge.py`): FinishLynx's
row wins for place/time/names; the chip time is published until
FinishLynx places an athlete; the console supplies the mile splits (split
points only, never the finish, stopping at an athlete's first missing
split - MeetPro files splits by position); athletes still on course are
listed with blank place/time so Mile 1 shows during the race. Verified on
MeetPro 2.3.137 Splits Auto-Pull on 2026-09-23 (`bench/results/`).

For **MileSplit Live** during the race use MeetPro's XC tab **Start MS
Live Sync** (after MileSplit → MileSplit Live → Start), not Splits
Auto-Pull - only MS Live Sync sends splits live. Each XC event needs its
split points (Mile 1 order 1, Mile 2 order 2) in MeetPro's event setup.

**Fallback:** if the console dies mid-meet, point MeetPro's F.A.T.
directory back at `C:\Finish_Lynx_Results` - results keep flowing from
FinishLynx, only the mile splits stop.

## 3. Optional: Electro Numerics clock driven by FinishLynx directly

The middleware normally drives the LED clock itself (any USB port,
auto-detected). If you instead want FinishLynx to drive the Electro
clock the traditional way:

1. Copy `docs/ledclock/old-electro/lss/Electro-led.lss` into `C:\Lynx\`.
2. **File → Options → Scoreboard** → new entry: Script
   `Electro-led.lss`, **Serial Port** `<the clock's COM port>`,
   `9600,8,N,1` (N, not E — parity garbage renders as wrong digits).
3. Known-good full settings block:
   `docs/ledclock/old-electro/reference/finishlynx-scoreboard3.txt`
   (includes `TimeMode=1`, `ResultsIfArmed=0`, `TimePause=3000`).

**Never let both FinishLynx and the middleware drive the same clock.**
If the middleware should leave it alone, set `led_clock.enabled: false`
in `config.yaml`. Also note FinishLynx only holds the COM port while the
scoreboard is armed.

## 4. Firewall (only when middleware is on a DIFFERENT machine)

- On the middleware machine: allow inbound **UDP 5891**.
- On the FinishLynx machine: allow inbound **TCP 5000**.
- Set `finishlynx.host` in config.yaml to the FinishLynx machine's IP,
  and the scoreboard UDP destination to the middleware machine's IP.

## 5. Quick triage

| Symptom | Check |
|---|---|
| FinishLynx chip stays red | LapTime plug-in enabled? Port 5000 both sides? FinishLynx actually running? |
| Chip green, log says "Split sent successfully", nothing in FinishLynx | LapTime **Device** must be `Lynx`. Also: an event must be open and started (gun) — splits earlier than the start, or within the 3 s Start Mask, are dropped. |
| Splits sent but never appear | Event armed? `S` packets carry event/round/heat — they must match the armed event (scoreboard channel supplies these; if it's down the middleware can't tag splits correctly). |
| Event/heat never updates | Scoreboard entry outputs to UDP 5891? Script set to Example/Example2? |
| Gun never auto-fires | Running-time option enabled on the scoreboard entry. |
| Clock garbage digits | Serial parity — this clock is `9600,8,N,1`; another scoreboard in the config uses `8,E,1`, don't copy it. |
