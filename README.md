# RFID-to-FinishLynx Middleware

A comprehensive Python middleware that bridges RFID timing hardware with FinishLynx photo-finish software. Reads RFID tags from Impinj Speedway R420 readers and sends timing data to FinishLynx using the LapTime protocol. Features a web-based control interface, multi-lap support, relay event handling, validation modes, and optional LoRa communication for remote lap points.

> **Operator docs:** [`MEET-DAY.md`](MEET-DAY.md) is the race-morning
> runbook; [`BENCH-TODO.md`](BENCH-TODO.md) is the live queue of
> deployments and validations waiting on the next bench session.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Web Interface](#web-interface)
- [Operation Modes](#operation-modes)
- [FinishLynx Setup](#finishlynx-setup)
- [Event File Integration](#event-file-integration)
- [LIF File Generation](#lif-file-generation)
- [Relay Event Support](#relay-event-support)
- [Remote Lap Points (LoRa)](#remote-lap-points-lora)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)

---

## Features

### Core Functionality
- **RFID Reader Integration**: Connects to Impinj Speedway R420 via LLRP protocol
- **FinishLynx Output**: Sends timing data to FinishLynx LapTime plugin via TCP
- **Scoreboard Reception**: Receives event/heat information from FinishLynx via UDP
- **Web Dashboard**: Real-time browser-based control and monitoring interface
- **Multi-Lap Support**: Track multiple reads per athlete for distance events

### Timing Features
- **Deduplication**: Configurable window (12+ seconds) to filter duplicate tag reads
- **Lap Tracking**: Position calculation and lap counting for each athlete
- **Time Editing**: Adjust read times directly in the interface
- **Manual Entry**: Add bib reads manually when tags fail

### Event Management
- **EVT File Parsing**: Load FinishLynx event files for heat/entry information
- **PPL File Parsing**: Load athlete data (names, schools, gender)
- **Validation Mode**: Verify bibs against heat entries before sending to FinishLynx
- **Relay Mode**: Automatic team/leg tracking for relay events

### Data Export
- **LIF Generation**: Create FinishLynx-compatible interchange files
- **LIF Adjustment**: Edit backup LIF files to correct timing data
- **CSV Export**: Export read logs and timing data
- **Auto-Save**: Automatic LIF backup on event change

### Remote Support
- **LoRa Communication**: Connect remote lap point readers via LoRa transceivers
- **Remote Management**: Start/stop/configure remote units from main station
- **Multi-Reader**: Aggregate reads from multiple RFID readers

### Platform Support
- Cross-platform: Windows, Linux, Raspberry Pi
- Automatic reconnection on connection loss
- Graceful degradation when FinishLynx disconnects

---

## Requirements

### Hardware
- Impinj Speedway R420 RFID reader (or compatible LLRP reader)
- RFID tags encoded with bib numbers
- Computer running FinishLynx with LapTime plugin
- (Optional) DSD Tech LoRa modules for remote lap points

### Software
- Python 3.8 or higher
- FinishLynx with LapTime plugin license
- Modern web browser (Chrome, Firefox, Edge)

---

## Installation

### Windows

Run the installer batch file:
```batch
install.bat
```

Or double-click `install.bat` in File Explorer.

### Linux / Raspberry Pi

```bash
chmod +x install.sh
./install.sh
```

### Manual Installation

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

---

## Quick Start

1. **Configure the system**: Edit `config.yaml` with your reader and FinishLynx IPs

2. **Start the middleware**:
   ```bash
   # Windows
   RFID-FinishLynx.bat

   # Linux
   ./RFID-FinishLynx.sh
   ```

3. **Open the web interface**: Navigate to `http://localhost:8080` in your browser

4. **Load event data**: Use Settings to load your `.evt` and `.ppl` files

5. **Start reading**: Click the Start button to begin capturing RFID reads

---

## Configuration

Edit `config.yaml` to match your setup:

```yaml
# Operating mode: finish_line (main station) or remote_lap (remote reader)
mode: finish_line

# RFID Reader Settings
reader:
  host: 192.168.0.50      # Reader IP address
  port: 5084              # LLRP port (default for Impinj)
  antennas: [1, 2, 3, 4]  # Active antenna ports
  power_dbm: 20.0         # Transmit power (10.0 - 32.5 dBm)

# Timing Settings
timing:
  dedup_window_sec: 12.0  # Minimum seconds between reads of same bib
  lap_mode: false         # Enable multi-lap tracking
  total_laps: 1           # Expected number of laps (for display)

# FinishLynx Connection (sends timing data)
finishlynx:
  host: 127.0.0.1         # FinishLynx computer IP
  port: 5000              # LapTime plugin port
  sync_mode: internal     # Time sync mode
  reconnect_delay_sec: 5.0

# Scoreboard Listener (receives event/heat info)
scoreboard:
  enabled: true           # Enable scoreboard data reception
  host: 0.0.0.0           # Listen interface (0.0.0.0 = all)
  port: 5891              # UDP port for scoreboard output

# LoRa Settings (for remote lap points)
lora:
  enabled: false          # Enable LoRa communication
  port: COM3              # Serial port (COM3 on Windows, /dev/ttyUSB0 on Linux)
  baud: 9600              # Serial baud rate
  reader_id: S1           # Identifier for this reader; keep it short - it
                          # rides in every message on the air
  pre_registered_units: []  # Known remote unit IDs
  announce_interval_sec: 10.0
  command_timeout_sec: 3.0
  offline_timeout_sec: 35.0

# Logging
logging:
  level: INFO             # DEBUG, INFO, WARNING, ERROR
  file: rfid_timing.log   # Log file path

# Meet Data (set via web interface)
event_directory: null     # Path to meet directory
evt_file: null            # Selected .evt file name
```

### Configuration Notes

- **dedup_window_sec**: Enforced minimum of 12 seconds to prevent false duplicates
- **power_dbm**: Higher power = longer range but more interference; start at 20 dBm
- **antennas**: List antenna ports connected to your reader (1-4)

---

## Web Interface

Access the dashboard at `http://localhost:8080` after starting the middleware.

### Dashboard Layout

#### Status Bar (Top)
- **Connection indicators**: Shows status of Reader, FinishLynx, Scoreboard, and LoRa
- **Start/Stop button**: Control RFID reading
- **Operation mode**: Switch between "Current Reads" and "FinishLynx" modes
- **Statistics**: Total reads, unique bibs, last read time

#### Read Log (Center)
The main display showing all captured RFID reads:

- **Bib number**: Athlete identifier from RFID tag
- **Time**: Time of day when tag was read
- **Lap**: Current lap number / total laps (in lap mode)
- **Position**: Calculated race position at that lap
- **Reader**: Source reader ID (LOCAL or remote ID)

**Features**:
- Click on a row to expand lap history
- Edit times by clicking the time field
- Delete reads using the trash icon
- Filter by bib number using the search box
- Toggle sort order (newest first / oldest first)
- Audio beep notification toggle

#### LIF Adjustment Panel (Left Side)
Tool for editing backup LIF files:

1. **Load File**: Select a `.lif` file to edit
2. **Set Start Time**: Enter the actual race start time
3. **Set Expected Splits**: Number of timing points per athlete
4. **Review Athletes**: Expand each athlete to see/edit their splits
5. **Add/Delete Splits**: Fix missing or erroneous reads
6. **Save**: Creates a new file with "-ADJUSTED" suffix

#### Settings Panel (Accordion)
Expandable configuration sections:

- **Reader Settings**: IP, port, power level, antenna selection
- **FinishLynx Settings**: Connection parameters
- **Scoreboard Settings**: UDP listener configuration
- **Timing Settings**: Dedup window, lap mode, total laps
- **Event File**: Load .evt file for heat/entry data
- **Athlete File**: Load .ppl file for athlete names

#### System Log (Bottom)
Real-time application logs:

- Filter by level: DEBUG, INFO, WARNING, ERROR
- Export logs as CSV
- Clear log display

#### Remote Units Panel (When LoRa Enabled)
Manage remote lap point readers:

- View unit status (idle, reading, error, offline)
- Send commands: START, STOP, RESET_DEDUP
- Query and update remote configuration
- Register new units manually

---

## Operation Modes

### Current Reads Mode
Simple passthrough mode for basic operation:
- All RFID reads sent directly to FinishLynx
- No validation or filtering
- Good for testing and simple events

### FinishLynx Mode
Smart mode with advanced features:
- Validates bibs against loaded heat entries
- Filters reads by current event/heat
- Supports validation approval workflow
- Enables relay event processing
- Auto-saves LIF backup on event change

### Validation Workflow (FinishLynx Mode)
When a bib is read that's not in the current heat entries:

1. Read appears as "pending" with yellow highlight
2. User reviews the pending bib
3. **Accept**: Sends read to FinishLynx
4. **Reject**: Discards the read

Validation reasons:
- Bib not in heat entries
- Wrong gender for event
- School not in heat (relay events)

---

## FinishLynx Setup

### LapTime Plugin Configuration

1. Open FinishLynx
2. Go to **File > Options > LapTime**
3. Check **Enable LapTime**
4. Set **Port** to 5000 (or your configured port)
5. Select **Internal Sync** for time synchronization
6. Click **OK**

### Scoreboard Output Configuration

To receive event/heat information from FinishLynx:

1. Go to **File > Options > Scoreboard**
2. Add a new scoreboard output
3. Set **Type** to "Network (UDP)"
4. Set **Port** to 5891 (or your configured port)
5. Select script: "Example" or "Example2" (recommended)
6. Set destination IP to the middleware computer

### Running Time Display (Optional)

For running time display in the middleware:

1. Go to **File > Options > Scoreboard**
2. Add another Network (UDP) output
3. Use "Scoreboard (Running Time)" script
4. Same port as above

---

## Event File Integration

### Loading .evt Files

The middleware can parse FinishLynx `.evt` files to get:
- Event numbers and names
- Heat numbers and entries
- Lane assignments
- Bib-to-athlete mapping

**To load**:
1. Open Settings in the web interface
2. Click "Browse" under Event File
3. Select your meet's `.evt` file
4. The file will be parsed and events populated

### Loading .ppl Files

Athlete data files (`.ppl`) provide:
- First and last names
- School/affiliation
- Gender

**To load**:
1. Open Settings
2. Click "Browse" under Athlete File
3. Select the `.ppl` file from your meet directory

### Heat Selection

After loading files:
1. Current event/heat auto-syncs from FinishLynx scoreboard
2. Or manually select using the heat dropdown
3. Only bibs in selected heat will be processed (FinishLynx mode)

---

## LIF File Generation

### Automatic Backup

When in FinishLynx mode, the middleware automatically saves a LIF backup:
- Triggered when FinishLynx changes to a new event/heat
- Saved to `RFID Events` subdirectory in the meet folder
- Filename format: `NNN-R-HH-HHMMSS.lif` (event-round-heat-time)

### Manual Export

To manually export current reads as LIF:
1. Click "Export LIF" in the interface
2. Fill in event details if not auto-populated
3. Set the race start time
4. Click "Download" or "Save to Directory"

### LIF File Format

The generated LIF files follow FinishLynx format:

**Header Line** (11 fields):
```
Event#,Round,Heat,EventName,Wind,WindUnit,Template,CaptureTime,CaptureDuration,Distance,StartTime
```

**Result Lines** (19 fields per athlete):
```
Place,Bib,Lane,LastName,FirstName,School,Time,License,SplitTime,ReacTime,Splits,TOD,Gender,User1,User2,Gap,Cumulative,Speed,FinalTime
```

### LIF Adjustment Tool

For fixing backup LIF files with missing or incorrect data:

1. **Load**: Select a `.lif` file from the RFID Events folder
2. **Review**: Check each athlete's splits against expected count
3. **Add Splits**: Insert missing timing points
4. **Delete Splits**: Remove erroneous reads
5. **Edit Times**: Adjust individual split times
6. **Set Start Time**: Correct the race start if needed
7. **Save**: Creates `filename-ADJUSTED.lif`

**Workflow Tips**:
- Athletes with wrong split count show warning badges
- Expected splits can be set globally (e.g., 4 for 4-lap race)
- "Remove Splits, Keep Finish Only" for quick cleanup
- Deltas recalculate automatically when times change

---

## Relay Event Support

### Automatic Detection

Relay events are detected from event names containing:
- "4x" (4x100, 4x400, 4x800, etc.)
- "relay"
- "medley"

### Team Tracking

The middleware automatically tracks relay teams:

1. First read from a school → Team assigned to that lane
2. Subsequent reads from same school → Same team, next leg
3. Multiple reads of same bib → Same leg (dedup window applies)

### Leg Management

- **Middle legs** (2 and 3): 2 reads allowed per leg
- **Leg 1**: 1 fewer read (earlier finish)
- **Leg 4**: 1 extra read (final finish)

### Output

In relay mode:
- **Lane number** is sent to FinishLynx (not bib)
- Web interface shows team name, leg number, and athlete info
- LIF export includes lane-based results

---

## Remote Lap Points (LoRa)

### Overview

For events with timing points away from the finish line (XC courses, multi-lap track events), remote readers can send data via LoRa radio.

### Hardware Setup

**Finish Line Station**:
- Main computer with middleware
- LoRa radio, serial or ethernet gateway
- Configure as `mode: finish_line` with `lora.enabled: true`

**Remote Lap Point**:
- Raspberry Pi with RFID reader
- LoRa radio, serial or ethernet gateway
- Configure as `mode: remote_lap` with `lora.enabled: true`

### Radio Connection

`lora.port` selects how the radio is attached:

| Radio | `lora.port` |
|---|---|
| Serial module (DSD Tech, USB) | `/dev/ttyUSB0`, `COM3` |
| Ethernet gateway (Ebyte E90-DTU "E") | `192.168.0.60:8886` |

The link reopens itself with backoff whenever it drops, so a station
recovers from a gateway reboot or a power cycle without intervention.

### Boot-to-racing (no-touch startup)

Both ends start themselves. Power a box on and the service launches the
app with no clicks, then every subsystem retries independently, forever,
in any power-on order: the LoRa link reconnects with 1-30 s backoff, the
RFID reader is retried every 15 s by the hardware supervisor (including
auto-configure, so a factory-fresh replacement reader is adopted without
a keyboard), and FinishLynx reconnects on its own loop. `systemd` restarts
the whole process if it ever dies (`deploy/rfid-remote.service`,
installed by `deploy/install-station.sh` after each rsync).

Field diagnosis without a site visit: each station announces its state
over LoRa, shown in the finish console's Remote stations panel -
green "reading" = radio + reader both up; amber "idle" = radio up,
reader down (supervisor retrying); red "silent" = nothing heard for 40 s
(radio, power, or gateway problem at the station).

A station has no real-time clock and no time source on its network, so
after a power cycle it wakes believing it is whenever it was last shut
down. The finish console's GUN broadcast (sent on every change and at
least every 30 s) carries the console's own clock; the station shifts the
gun into its local time immediately and, when it is more than 2 s out,
steps its OS clock to the finish line's (`sudo -n date`, passwordless on
the station image). Read timestamps radioed back, the idle time-of-day on
the station's clocks and its log therefore all agree with the finish line
within about half a minute of the console being up. The step is logged as
a warning in the station's journal (`journalctl -u rfid-remote`). Split
stations never talk to FinishLynx themselves - only the finish console
does - so they neither connect to it nor retry.

Antenna ports follow `reader.antennas`. The default, `all` (or an empty
list), means every port the reader has: on a Vanch the port count is
probed at connect (the reader refuses port numbers it lacks), so a
4-port or an 8-port reader with antennas on any mix of ports is polled
in full without anyone editing config. Naming specific ports gives the
fastest reads (no dwell on empty ports); ports the reader does not have
are dropped with an error in the log rather than failing the whole set.
The Vanch driver also tries `192.168.0.50`, `.51` and the factory `.128`
when the configured address does not answer, so a swapped reader is
adopted as-is. An Impinj reader's port count is read over SSH when
`sshpass` is installed; when it is not, the configured list is used
exactly as written.

From the finish console, each row in the Remote stations panel has a
**Reader…** button: tick every port or specific ports, set power, and
Send to station. It travels over the radio (allow up to 10 s), the
station reconnects its reader by itself, and Refresh from station reads
back the ports it is actually polling. The same is available in the
legacy dashboard's Remote Units card and via
`POST /api/remote/units/<id>/config` (`{"section":"reader","values":
{"antennas":[1,3,5,7]}}`, `[]` = every port).

### Station Setup

Each station is an island: a PC and its radio alone on a small wired
network, joined to the other station only by radio. Both islands can
therefore use the same addresses. Standard layout per station:

| Device | Finish line | Remote lap point |
|---|---|---|
| PC | `192.168.0.97` | `192.168.0.46` |
| LoRa gateway | `192.168.0.60` | `192.168.0.61` (data on port 8886) |
| RFID reader | `192.168.0.50` | `192.168.0.51` |

The two stations only ever meet over the radio, so they could share one
set of addresses -- but distinct ones let both sit on a single bench
switch while you test, so they are worth keeping.

There is no DHCP server on a station network, so every device needs a
static address. On Windows, run `setup-windows-station.ps1` as
Administrator once per PC; leaving the adapter on "obtain an address
automatically" is the usual cause of a station that works on one machine
but not another.

Then adopt the gateway. It is found whatever address it currently holds:
each candidate address is tried through the routing table first and then
directly on each wired port, so a gateway still on its factory address --
or one whose subnet a VPN has claimed -- is picked up anyway.

```bash
python setup_gateway.py discover              # what is on the wire?
python setup_gateway.py adopt                 # move it to 192.168.0.60, save a baseline
python setup_gateway.py adopt --ip 192.168.0.61   # ... at the remote station
python setup_gateway.py check                 # radio settings + link health
```

Both stations' radios must agree on frequency, channel, network ID,
address and air rate. A mismatch produces no data and no error message,
so `check` at both ends is the fastest way to rule it out:

```bash
python setup_gateway.py check --json    # run at each station, compare
```

### Transmission Pacing

The radio accepts one transmission roughly every 5 seconds. Anything
written inside that window is silently discarded - no error at either
end, the reads simply never arrive. A single transmission, however,
carries a full packet (240 bytes, about six reads) and the radio
fragments anything longer automatically.

`lora.send_interval_sec` (5.0 at both stations) makes the transport
respect this: a read arriving when the radio is idle goes out
immediately, and reads arriving during the gap are coalesced into the
next transmission instead of being dropped. Measured with 12 reads in a
3-second burst - a pack of runners crossing together:

| | Reads delivered |
|---|---|
| Without pacing | 1 of 12 |
| With pacing | 12 of 12 |

Setting it to 0 disables pacing and writes immediately, which is correct
for a directly attached serial radio.

### Testing the Link

With both stations built, verify the radio path end to end:

```bash
python test_lora_link.py listen                        # finish line
python test_lora_link.py send --reader-id S1 -c 10     # remote unit
```

The listener validates each message's checksum, reader ID, sequence
number and latency.

> **Note:** a 1 W (30 dBm) radio can be damaged by transmitting with no
> antenna attached. Connect antennas before sending.

### Range Testing

To measure the link at a real distance, the remote station beacons on its
own from the moment it is powered on - no network, no keyboard. Carry it
out, plug it in, wait, drive further, repeat.

```bash
python range_test.py listen        # finish line, leave running
```

The remote is started automatically by `rfid-range-beacon.service`:
beaconing begins about 10 seconds after power-on and continues for an
hour, one beacon every 6 seconds. Every power-on is a separate run
(`r1`, `r2`, ...) counted in `range-test-run.txt`, so consecutive
distances stay distinct in the logs. Delivery rate is computed from
sequence numbers, so it does not matter that the Pi has no clock of its
own when it boots away from the network.

Both ends keep a log - `range-test-sent.log` on the remote,
`range-test-heard.log` at the finish line. Every arrival is recorded with
the date and time this machine received it, so the logs stay readable
after a session that runs past midnight or across several days:

```
2026-08-31 20:15:21.691321 run=r2 seq=99 sent=20:15:21.329
```

Stopping the listener (Ctrl-C, or `systemctl stop`) prints a per-run
summary and appends it to the log, so closing the window loses nothing.
To report on a log later, with nothing running:

```bash
python range_test.py analyze
```

```
run r2: heard 47/50 beacons (94%) (from seq 12)
  first 2026-08-31 20:15:21   last 2026-08-31 20:20:15
  longest silence 24s
  missed 3: 19, 31, 32
```

Delivery is measured across the beacons actually observed, so starting
the listener part-way through a run does not count the earlier beacons as
lost. `sent=` comes from the remote's clock, which has no battery and may
be minutes off until it reaches a network - use it to line up records,
not to measure latency.

Range testing and normal operation both want the radio, so only one can
run at a time:

```bash
# switch to range testing
sudo systemctl disable --now rfid-remote
sudo systemctl enable --now rfid-range-beacon

# switch back to timing
sudo systemctl disable --now rfid-range-beacon
sudo systemctl enable --now rfid-remote
```

### Remote Unit Management

From the main station web interface:

1. **Discover**: Units announce themselves automatically
2. **Register**: Add known unit IDs manually
3. **Control**: Send START/STOP commands
4. **Configure**: Query and update remote settings
5. **Monitor**: View status and statistics

### Protocol

Messages between stations use checksummed packets:
```
$READ,<bib>,<timestamp>,<reader_id>[,<seq>]*<checksum>
$BATCH,<reader_id>,<seq>,<bib>@<timestamp>[,<bib>@<timestamp>...]*<checksum>
$ANNOUNCE,<reader_id>,<version>,<state>*<checksum>
$CMD,<reader_id>,<seq>,<command>[,<args>]*<checksum>
$ACK,<reader_id>,<seq>,<status>[,<data>]*<checksum>
$CFGQ,<reader_id>,<seq>,<section>*<checksum>
$CFGR,<reader_id>,<seq>,<section>,<k=v;k=v>*<checksum>
$CFGS,<reader_id>,<seq>,<section>,<k=v;k=v>*<checksum>
```

Reads are delivered reliably and in bulk: the remote packs queued reads
into `BATCH` messages (many reads under one sequence number, up to one
480-byte write per transmit window - roughly 25 reads / 5 s, ~300 per
minute sustained), the finish line replies with one `ACK` per batch, and
unacknowledged batches are retransmitted forever with exponential backoff
(starting above the paced round trip, capped at 60 s) until acknowledged.
Reads are queued even while the radio link is down. The finish line
deduplicates each read by content `(reader_id, bib, timestamp)`, so a
retransmitted batch only delivers the reads not yet seen and even an
hours-late retransmit cannot double count a runner. Single `READ` messages
are still accepted (with per-read ACK when they carry a sequence number).

Messages are protected by CRC-32 (a single-byte XOR was measured passing
collision-corrupted frames with wrong timestamps). The two stations share
one channel with no carrier sense, so each transport prefers to transmit
just after the peer's transmission finishes - the peer's own ~5 s gate
then keeps it off the air - and falls back to a jittered slot when
nothing has been heard, so idle stations cannot phase-lock and collide
every cycle (also measured). Bench result: 500 reads queued in one burst
deliver in ~100 s with zero loss, zero duplicates, zero corrupt frames.

Both ends journal every read to disk as it happens: the remote appends
SENT/ACKED/DROP lines to `lora-reads-sent.log`, the finish line appends
RECV/DUP lines to `lora-reads-received.log`. Diffing the two files after a
race shows exactly which reads (if any) never made it across, with their
original timestamps, so results can be reconciled by hand in the worst case.

At the finish line, remote reads go through the same processing pipeline as
local reads: event/heat filtering, race-read tracking for LIF generation,
and buffering while FinishLynx is disconnected (flushed on reconnect).

---

## API Reference

The middleware provides a REST API for integration and automation.

### Core Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | System status and statistics |
| POST | `/api/start` | Start RFID reading |
| POST | `/api/stop` | Stop RFID reading |
| GET | `/api/config` | Get current configuration |
| POST | `/api/config` | Update configuration |

### Read Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/reads` | Get all reads |
| GET | `/api/reads/<bib>` | Get reads for specific bib |
| POST | `/api/manual-read` | Add manual bib entry |
| POST | `/api/reads/<bib>/adjust` | Adjust or delete read |
| POST | `/api/reads/<bib>/edit-time` | Edit read timestamp |
| POST | `/api/reads/clear` | Clear all reads |
| POST | `/api/reset-dedup` | Reset deduplication state |

### Export

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/reads/export` | Export reads as CSV |
| GET/POST | `/api/reads/export/lif` | Export as LIF file |
| POST | `/api/lif/save-adjusted` | Save adjusted LIF |

### Event Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/load-evt` | Load .evt file |
| POST | `/api/athletes/load` | Load .ppl file |
| GET | `/api/athletes/<bib>` | Get athlete info |
| POST | `/api/validation/set-heat` | Set current heat |
| GET | `/api/validation/state` | Get validation state |
| POST | `/api/validation/accept` | Accept pending bib |
| POST | `/api/validation/reject` | Reject pending bib |

### Remote Units

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/remote/units` | List remote units |
| POST | `/api/remote/units/register` | Register unit |
| POST | `/api/remote/units/<id>/command` | Send command |
| GET | `/api/remote/units/<id>/config` | Query config |
| POST | `/api/remote/units/<id>/config` | Set config |

### WebSocket Events

Connect to `ws://localhost:8080/socket.io` for real-time updates:

| Event | Description |
|-------|-------------|
| `tag_read` | New RFID read received |
| `status_update` | Connection status changed |
| `remote_unit_update` | Remote unit state changed |
| `reads_cleared` | Read log cleared |

---

## Architecture

```
FINISH LINE STATION (Windows/Linux)
┌─────────────────────────────────────────────────────────────────────┐
│                         Web Browser                                  │
│                    http://localhost:8080                            │
└─────────────────────────────────────────────────────────────────────┘
                                │ HTTP/WebSocket
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    RFID-FinishLynx Middleware                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │ Web/API  │  │   App    │  │  Config  │  │   LIF Generator  │    │
│  │ (Flask)  │◄─┤ (Core)   │◄─┤ (YAML)   │  │                  │    │
│  └──────────┘  └────┬─────┘  └──────────┘  └──────────────────┘    │
│                     │                                               │
│       ┌─────────────┼─────────────┬─────────────────┐              │
│       ▼             ▼             ▼                 ▼              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │  RFID    │  │FinishLynx│  │Scoreboard│  │  LoRa Manager    │    │
│  │ Reader   │  │ Output   │  │ Listener │  │  (Optional)      │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘    │
└───────┼─────────────┼─────────────┼─────────────────┼──────────────┘
        │             │             │                 │
        ▼             ▼             ▼                 ▼
┌──────────────┐ ┌──────────┐ ┌──────────┐    ┌──────────────┐
│ Impinj R420  │ │FinishLynx│ │FinishLynx│    │ LoRa Radio   │
│ RFID Reader  │ │ LapTime  │ │Scoreboard│    │ Transceiver  │
│ (LLRP:5084)  │ │ (TCP)    │ │ (UDP)    │    │ (Serial)     │
└──────────────┘ └──────────┘ └──────────┘    └──────┬───────┘
                                                      │
                              ┌────────────────────────┘
                              ▼
                    REMOTE LAP POINT (Raspberry Pi)
                    ┌─────────────────────────────────┐
                    │   Remote Agent                  │
                    │  ┌──────────┐  ┌──────────┐    │
                    │  │  RFID    │  │  LoRa    │    │
                    │  │  Reader  │  │  Radio   │    │
                    │  └────┬─────┘  └────┬─────┘    │
                    └───────┼─────────────┼──────────┘
                            ▼             ▼
                    ┌──────────────┐ ┌──────────────┐
                    │ Impinj R420  │ │ LoRa Radio   │
                    └──────────────┘ └──────────────┘
```

### Data Flow

1. **Tag Read**: RFID reader detects tag → middleware receives via LLRP
2. **Processing**: Bib extracted → deduplication check → validation (if enabled)
3. **Output**: Read sent to FinishLynx via TCP → appears in LapTime window
4. **Backup**: Read stored in memory → saved to LIF on event change
5. **Display**: WebSocket pushes update → browser displays in real-time

---

## Troubleshooting

### Reader Not Connecting

**Symptoms**: "Reader disconnected" status, no tag reads

**Solutions**:
1. Verify reader IP address: `ping <reader-ip>`
2. Check LLRP port (5084) is not blocked by firewall
3. Ensure reader is powered and network connected
4. Try power cycling the reader
5. Check antenna connections

### FinishLynx Not Receiving Data

**Symptoms**: Reads appear in middleware but not in FinishLynx

**Solutions**:
1. Verify LapTime plugin is enabled in FinishLynx
2. Check firewall allows port 5000 (or configured port)
3. Confirm IP addresses match configuration
4. Look for "FinishLynx connected" status indicator
5. Check FinishLynx LapTime window is open

### Scoreboard Data Not Received

**Symptoms**: Event/heat not auto-updating from FinishLynx

**Solutions**:
1. Configure Network UDP output in FinishLynx scoreboard settings
2. Use "Example" or "Example2" script format
3. Set correct port (default 5891)
4. Check firewall allows UDP on that port
5. Verify middleware IP in FinishLynx destination

### Tags Not Being Read

**Symptoms**: Athletes pass reader but no reads appear

**Solutions**:
1. Verify antenna connections (check reader LEDs)
2. Increase power level (try 25-30 dBm)
3. Check tag encoding matches expected format
4. Ensure tags are oriented correctly (perpendicular to antenna)
5. Test with a known-good tag at close range

### Duplicate Reads / Missing Reads

**Symptoms**: Same bib appears multiple times, or expected reads missing

**Solutions**:
1. **Too many reads**: Increase `dedup_window_sec` (minimum 12s)
2. **Missing reads**: Decrease power or adjust antenna positioning
3. Check lap mode is enabled for multi-lap events
4. Verify tags aren't damaged or detuned

### LoRa Communication Issues

**Symptoms**: Remote units not appearing, reads not received

**Solutions**:
1. Verify correct serial port in configuration
2. Check baud rate matches module configuration (default 9600)
3. Ensure line of sight between transceivers
4. Check antenna connections on LoRa modules
5. Verify both units use same frequency/settings

### LIF File Issues

**Symptoms**: Missing athletes, wrong times, import errors

**Solutions**:
1. Use LIF Adjustment tool to fix missing splits
2. Verify start time is correct
3. Check expected splits matches actual timing points
4. Ensure all athletes have same number of splits for import

---

## File Structure

```
rfid_finishlynx/
├── config.yaml              # Main configuration file
├── run.py                   # Application entry point
├── install.bat              # Windows installer
├── install.sh               # Linux installer
├── RFID-FinishLynx.bat      # Windows launcher
├── RFID-FinishLynx.sh       # Linux launcher
├── requirements.txt         # Python dependencies
├── rfid_finishlynx/         # Main package
│   ├── __init__.py
│   ├── main.py              # CLI entry point
│   ├── app.py               # Core application logic
│   ├── web.py               # Web interface and API
│   ├── config.py            # Configuration management
│   ├── rfid_reader.py       # RFID reader interface
│   ├── finishlynx.py        # FinishLynx protocol
│   ├── lif_generator.py     # LIF file generation
│   ├── lora_receiver.py     # LoRa reception
│   ├── remote_agent.py      # Remote unit agent
│   ├── remote_manager.py    # Remote unit management
│   ├── remote_protocol.py   # LoRa message protocol
│   ├── transport.py         # LoRa transport layer
│   ├── static/              # Web static files
│   │   ├── bootstrap.min.css
│   │   ├── bootstrap.bundle.min.js
│   │   ├── socket.io.min.js
│   │   └── bootstrap-icons.min.css
│   └── templates/
│       └── index.html       # Web interface template
└── templates/               # FinishLynx event templates
    ├── TEMPLATE_800m_FL.evn
    ├── TEMPLATE_1600m_FL.evn
    └── ...
```

---

## License

MIT License

---

## Support

For issues and feature requests, please use the GitHub issue tracker.

For FinishLynx-specific questions, refer to the FinishLynx documentation or contact Lynx System Developers.
