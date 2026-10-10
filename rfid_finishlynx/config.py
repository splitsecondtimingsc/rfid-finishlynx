"""Configuration handling for RFID-FinishLynx middleware."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


def parse_antennas(value) -> List[int]:
    """reader.antennas from YAML/API/radio: a list of port numbers, or
    "all" (also blank/None/[]) meaning every port the reader has - the
    default for a station that may get either the 4- or the 8-port Vanch."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "all", "auto", "any"):
            return []
        value = text.replace(",", " ").replace(".", " ").split()
    if isinstance(value, int):
        value = [value]
    ports = []
    for a in value:
        a = int(a)
        if a >= 1 and a not in ports:
            ports.append(a)
    return ports


@dataclass
class ReaderConfig:
    """RFID reader configuration."""
    host: str = "10.0.15.104"
    port: int = 5084  # LLRP default
    antennas: List[int] = field(default_factory=list)  # [] = every port the reader has
    power_dbm: float = 30.0
    type: str = "impinj"  # impinj (LLRP) | vanch (Modbus TCP)
    beep: bool = False    # Vanch buzzer on every tag read; set on each connect


@dataclass
class TimingConfig:
    """Timing behavior configuration."""
    dedup_window_sec: float = 12.0  # Ignore re-reads within this window (fixed at 12s)
    lap_mode: bool = False  # True = allow multiple reads per bib
    total_laps: int = 1  # Expected laps per race (display only, reads continue beyond)
    # Remote split mats read tagged spectators loitering beside the course
    # every dedup window; a racer crosses a split once. Repeats of a bib
    # within this window are not transmitted (LoRa airtime is the scarce
    # resource). Short enough that a spectator-turned-competitor's real
    # crossing still goes out: they must travel start -> split first.
    remote_repeat_suppress_sec: float = 60.0


@dataclass
class FinishLynxConfig:
    """FinishLynx LapTime plugin configuration."""
    host: str = "192.168.1.100"
    port: int = 5000
    sync_mode: str = "internal"  # internal, external, or none
    reconnect_delay_sec: float = 5.0
    # What goes to FinishLynx's LapTime plug-in: "finish" = only the
    # finish-line reader's reads (splits reach MeetPro through the .lif
    # instead), "all" = every read including split stations. LapTime
    # packets carry no split number - FinishLynx drops each one into the
    # athlete's next lap slot - so with "all" a split can land as the final
    # time whenever the event's Total Laps is short (2026-09-17).
    feed: str = "finish"


@dataclass
class ScoreboardConfig:
    """FinishLynx Scoreboard listener configuration (receives event/heat info via scoreboard output)."""
    enabled: bool = True     # Enable scoreboard listener
    host: str = "0.0.0.0"    # IP address to listen on (0.0.0.0 for all interfaces)
    port: int = 5891         # Port to listen on (FinishLynx connects to this)


# Alias for backwards compatibility
FinishLynxListenerConfig = ScoreboardConfig


@dataclass
class LoRaConfig:
    """LoRa transceiver configuration."""
    enabled: bool = False
    port: str = "COM3"  # device path, or host:port for an ethernet gateway
    # Minimum gap between radio transmissions. Reads arriving inside the gap
    # are coalesced into the next one; separate transmissions sent closer
    # together than this are silently dropped by the radio. 0 disables pacing.
    send_interval_sec: float = 0.0
    baud: int = 9600
    reader_id: str = "LAP1"  # Identifier for this reader
    pre_registered_units: List[str] = field(default_factory=list)
    announce_interval_sec: float = 10.0
    command_timeout_sec: float = 3.0
    offline_timeout_sec: float = 35.0
    # One-way radio delay of a GUN broadcast, seconds: the frame's airtime
    # (~55 bytes at 2.4 kbps = 0.18 s) plus the gateway's serial/TCP hop.
    # A split station subtracts it when it lines its clock up with the
    # finish console's, so its race clock and read times are not that much
    # behind. Measure with a stopwatch against the finish clock if the air
    # rate changes.
    air_latency_sec: float = 0.3


@dataclass
class LoggingConfig:
    """Logging configuration."""
    level: str = "INFO"
    file: Optional[str] = "rfid_timing.log"


@dataclass
class RemoteReaderEntry:
    """A remote reader endpoint for the centralized view."""
    name: str = "Remote Reader"
    host: str = ""
    web_port: int = 8080


@dataclass
class RacesConfig:
    """Race management (concurrent race panes, rosters, .lif streaming).

    Enabled by default: the finish-line console (console.html) needs the
    race manager, and a config.yaml without a ``races`` block used to fall
    back to the legacy dashboard silently. Remote stations never build a
    race manager regardless (gated on mode == finish_line in app.py).
    """
    enabled: bool = True
    # FinishLynx's results folder, where it saves its own EEE-R-HH.lif;
    # empty = the meet folder. The console reads those files and writes the
    # merged ones (FinishLynx's rows + RFID splits) into rfid_subdir below
    # it - the folder MeetPro's F.A.T. directory points at, and where
    # MeetPro writes lynx.evt/ppl/sch (FinishLynx's input directory).
    lif_dir: str = ""
    rfid_subdir: str = "RFID"
    chute_reader: str = "CHUTE"  # reader_id designated as check-in chute
    finish_readers: List[str] = field(default_factory=lambda: ["FINISH"])
    point_labels: Dict[str, str] = field(
        default_factory=lambda: {"S1": "Mile 1", "S2": "Mile 2"})
    # Course distance (m) of each split point, for pace-based estimates of a
    # missing split. The finish distance comes from the .evt (else 5000).
    point_distances: Dict[str, float] = field(
        default_factory=lambda: {"S1": 1609.0, "S2": 3218.0})
    # A reader that stands at more than one split point - e.g. one Pi where
    # the Mile 1 and Mile 2 markers share the same spot on the course:
    #   reader_points: {S1: [S1, S2]}
    # Each pass of a bib at that reader takes the next point in the list
    # (first pass = S1, second = S2; extra passes keep the last point). A
    # single-item list simply renames the reader's reads to that point.
    # "-" means "not a split": a loop course that crosses the Mile 2 mat
    # once before Mile 1 is {S2: ["-", S2]} - the first crossing is ignored
    # (restorable), the second is Mile 2. Works for the finish mat too.
    # A split station can BE the finish line: {S1: [FINISH]} makes every
    # S1 read a finish (places, promotion of held splits, the FinishLynx
    # LapTime feed, film offset); {S1: [S2, FINISH]} is one mat crossed at
    # Mile 2 and again at the finish; {S1: ["-", FINISH]} a finish mat a
    # loop crosses once early. The local mat can be a split the same way
    # ({FINISH: [S1]}), or not in use at all ({FINISH: [off]}: its reads are
    # ignored, it is neither finish nor split, no alarm when it is down).
    # Which readers are "the finish" follows this map.
    reader_points: Dict[str, List[str]] = field(default_factory=dict)
    # Reads of one bib at one reader closer together than this are the same
    # pass (a runner lingering on the mat), not the next split.
    pass_window_sec: float = 90.0
    # Nobody runs faster than this (seconds per mile). A read at a point
    # sooner after the gun than its distance allows - Mile 1 before 4:00,
    # Mile 2 before 8:00 at 4:00/mile - is held as "too early" instead of
    # becoming a split. Each race pane can override it.
    fastest_pace_sec_per_mile: float = 240.0
    state_file: str = "races-state.json"


@dataclass
class LEDClockConfig:
    """LED race clock on USB-serial (two interchangeable models)."""
    enabled: bool = True  # harmless when no clock attached (discovery idles)
    port: str = "auto"  # "auto" = find by USB VID:PID, else device path
    type: str = "auto"  # auto | ch340 (newer clock) | electro (Electro Numerics)
    hour_format: str = "24"  # time-of-day between races: "12" or "24" hour
    # Optional named clocks: [{port: COM7, type: electro, label: Finish}, ...].
    # Every discovered adapter is driven regardless; entries here pin a label
    # to a port and keep its chip visible (red) while that clock is unplugged.
    clocks: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class CameraStartConfig:
    """Start-gun signal sniffed from the EtherLynx camera's status replies
    (see camera_start.py). Needs Wireshark/Npcap on the FinishLynx PC."""
    enabled: bool = True
    camera_host: str = "192.168.0.201"
    camera_port: int = 41925
    interface: str = "auto"      # capture device/GUID/alias; auto = NIC on the camera subnet
    tshark_path: str = "auto"


@dataclass
class SupportConfig:
    """Updates from the public GitHub release and "Send logs" to the private
    logs repo (updater.py, support.py). logs_token is a GitHub fine-grained
    token that can only write to logs_repo; it is never sent to the page."""
    update_repo: str = "splitsecondtimingsc/rfid-finishlynx"
    logs_repo: str = "splitsecondtimingsc/rfid-logs"
    logs_token: str = ""
    site_name: str = ""          # folder name in the logs repo; blank = this PC's name


@dataclass
class MeetSyncConfig:
    """Meets shared between machines (meet_sync.py, docs/MEET-SYNC-PLAN.md).
    mode "git": a private repo over SSH (remote, e.g. myserver:me/meets.git);
    mode "folder": a shared folder such as Google Drive for desktop (folder).
    mode "both": the two at once, each a backup of the other (2026-10-08);
    remote may also be a folder (a network share on the NAS).
    machine: this PC's name in file names (blank = the computer name)."""
    enabled: bool = False
    mode: str = "off"            # off | git | folder | both
    remote: str = ""
    folder: str = ""
    machine: str = ""
    git: str = ""                # git.exe when not on PATH / MinGit


@dataclass
class Config:
    """Main configuration container."""
    camera_start: CameraStartConfig = field(default_factory=CameraStartConfig)
    mode: str = "finish_line"  # or 'remote_lap'
    display_name: str = ""  # Custom name shown in header (e.g. "Split Reader 1")
    header_color: str = ""  # CSS color for navbar (e.g. "#198754"), empty = default
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    finishlynx: FinishLynxConfig = field(default_factory=FinishLynxConfig)
    scoreboard: ScoreboardConfig = field(default_factory=ScoreboardConfig)
    lora: LoRaConfig = field(default_factory=LoRaConfig)
    races: RacesConfig = field(default_factory=RacesConfig)
    led_clock: LEDClockConfig = field(default_factory=LEDClockConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    support: SupportConfig = field(default_factory=SupportConfig)
    meet_sync: MeetSyncConfig = field(default_factory=MeetSyncConfig)
    remote_readers: List[RemoteReaderEntry] = field(default_factory=list)
    event_directory: Optional[str] = None  # Directory containing lynx.evt and lynx.ppl
    evt_file: Optional[str] = None  # Name of the .evt file (e.g., "lynx.evt")

    # Alias for backwards compatibility
    @property
    def finishlynx_listener(self) -> ScoreboardConfig:
        return self.scoreboard


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, searches for config.yaml
                    in current directory and script directory.

    Returns:
        Config object with loaded or default values.
    """
    if config_path is None:
        # Search for config file
        search_paths = [
            Path.cwd() / "config.yaml",
            Path(__file__).parent.parent / "config.yaml",
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    config = Config()

    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}

        config.mode = data.get("mode", config.mode)
        config.display_name = data.get("display_name", config.display_name)
        config.header_color = data.get("header_color", config.header_color)

        if "remote_readers" in data:
            config.remote_readers = [
                RemoteReaderEntry(
                    name=entry.get("name", "Remote Reader"),
                    host=entry.get("host", ""),
                    web_port=int(entry.get("web_port", 8080)),
                )
                for entry in data["remote_readers"]
            ]

        if "reader" in data:
            r = data["reader"]
            rtype = r.get("type", config.reader.type)
            config.reader = ReaderConfig(
                host=r.get("host", config.reader.host),
                port=r.get("port", 502 if rtype == "vanch" else config.reader.port),
                antennas=parse_antennas(r.get("antennas", config.reader.antennas)),
                power_dbm=float(r.get("power_dbm", config.reader.power_dbm)),
                type=rtype,
                beep=bool(r.get("beep", config.reader.beep)),
            )

        if "timing" in data:
            t = data["timing"]
            # Enforce minimum 12 second dedup window
            dedup = max(12.0, float(t.get("dedup_window_sec", config.timing.dedup_window_sec)))
            config.timing = TimingConfig(
                dedup_window_sec=dedup,
                lap_mode=t.get("lap_mode", config.timing.lap_mode),
                total_laps=int(t.get("total_laps", config.timing.total_laps)),
                remote_repeat_suppress_sec=float(t.get(
                    "remote_repeat_suppress_sec",
                    config.timing.remote_repeat_suppress_sec)),
            )

        if "finishlynx" in data:
            f = data["finishlynx"]
            config.finishlynx = FinishLynxConfig(
                host=f.get("host", config.finishlynx.host),
                port=f.get("port", config.finishlynx.port),
                sync_mode=f.get("sync_mode", config.finishlynx.sync_mode),
                reconnect_delay_sec=f.get("reconnect_delay_sec", config.finishlynx.reconnect_delay_sec),
                feed=("all" if str(f.get("feed", config.finishlynx.feed)).strip().lower() == "all"
                      else "finish"),
            )

        if "races" in data:
            rc = data["races"]
            config.races = RacesConfig(
                enabled=rc.get("enabled", config.races.enabled),
                lif_dir=rc.get("lif_dir", config.races.lif_dir),
                rfid_subdir=str(rc.get("rfid_subdir") or config.races.rfid_subdir),
                chute_reader=rc.get("chute_reader", config.races.chute_reader),
                finish_readers=rc.get("finish_readers", config.races.finish_readers),
                point_labels=rc.get("point_labels", config.races.point_labels),
                point_distances={str(k): float(v) for k, v in
                                 (rc.get("point_distances") or config.races.point_distances).items()},
                reader_points={str(k): [str(p) for p in (v if isinstance(v, (list, tuple)) else [v])]
                               for k, v in (rc.get("reader_points") or {}).items()},
                pass_window_sec=float(rc.get("pass_window_sec", config.races.pass_window_sec)),
                fastest_pace_sec_per_mile=float(rc.get("fastest_pace_sec_per_mile",
                                                       config.races.fastest_pace_sec_per_mile)),
                state_file=rc.get("state_file", config.races.state_file),
            )

        if "camera_start" in data:
            cs = data["camera_start"]
            config.camera_start = CameraStartConfig(
                enabled=bool(cs.get("enabled", config.camera_start.enabled)),
                camera_host=str(cs.get("camera_host", config.camera_start.camera_host)),
                camera_port=int(cs.get("camera_port", config.camera_start.camera_port)),
                interface=str(cs.get("interface", config.camera_start.interface)),
                tshark_path=str(cs.get("tshark_path", config.camera_start.tshark_path)),
            )

        if "led_clock" in data:
            lc = data["led_clock"]
            config.led_clock = LEDClockConfig(
                enabled=bool(lc.get("enabled", config.led_clock.enabled)),
                port=lc.get("port", config.led_clock.port),
                type=lc.get("type", config.led_clock.type),
                hour_format=("12" if str(lc.get("hour_format", config.led_clock.hour_format)) == "12"
                             else "24"),
                clocks=[dict(c) for c in (lc.get("clocks") or []) if isinstance(c, dict)],
            )

        # Support both old "finishlynx_listener" and new "scoreboard" config names
        scoreboard_data = data.get("scoreboard") or data.get("finishlynx_listener")
        if scoreboard_data:
            config.scoreboard = ScoreboardConfig(
                enabled=scoreboard_data.get("enabled", config.scoreboard.enabled),
                host=scoreboard_data.get("host", config.scoreboard.host),
                port=scoreboard_data.get("port", config.scoreboard.port),
            )

        if "lora" in data:
            l = data["lora"]
            config.lora = LoRaConfig(
                enabled=l.get("enabled", config.lora.enabled),
                port=l.get("port", config.lora.port),
                baud=l.get("baud", config.lora.baud),
                send_interval_sec=float(l.get("send_interval_sec", config.lora.send_interval_sec)),
                reader_id=l.get("reader_id", config.lora.reader_id),
                pre_registered_units=l.get("pre_registered_units", config.lora.pre_registered_units),
                announce_interval_sec=float(l.get("announce_interval_sec", config.lora.announce_interval_sec)),
                command_timeout_sec=float(l.get("command_timeout_sec", config.lora.command_timeout_sec)),
                offline_timeout_sec=float(l.get("offline_timeout_sec", config.lora.offline_timeout_sec)),
                air_latency_sec=float(l.get("air_latency_sec", config.lora.air_latency_sec)),
            )

        if "logging" in data:
            lg = data["logging"]
            config.logging = LoggingConfig(
                level=lg.get("level", config.logging.level),
                file=lg.get("file", config.logging.file),
            )

        if "support" in data:
            sp = data["support"] or {}
            config.support = SupportConfig(
                update_repo=str(sp.get("update_repo") or config.support.update_repo),
                logs_repo=str(sp.get("logs_repo") or config.support.logs_repo),
                logs_token=str(sp.get("logs_token") or ""),
                site_name=str(sp.get("site_name") or ""),
            )

        if "meet_sync" in data:
            ms = data["meet_sync"] or {}
            mode = str(ms.get("mode") or "off").strip().lower()
            config.meet_sync = MeetSyncConfig(
                enabled=bool(ms.get("enabled", mode in ("git", "folder", "both"))),
                mode=mode if mode in ("off", "git", "folder", "both") else "off",
                remote=str(ms.get("remote") or ""),
                folder=str(ms.get("folder") or ""),
                machine=str(ms.get("machine") or ""),
                git=str(ms.get("git") or ""),
            )

        # Load event directory and file
        config.event_directory = data.get("event_directory", config.event_directory)
        config.evt_file = data.get("evt_file", config.evt_file)

    return config
